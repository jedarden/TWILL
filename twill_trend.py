"""Build the durable weekly cluster series used by trend analysis."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import sqrt
from typing import Sequence

import twill_detectors
from twill_detectors import Detector, read_clusters, read_weekly_counts


WEEKLY_HISTORY_DAYS = 180
DEFAULT_TREND_WEEKS = 12
MIN_TREND_HISTORY_WEEKS = 6
EWMA_ALPHA = 0.3
EWMA_BAND_SIGMAS = 2.0
_REFERENCE_TABLES = ("observation", "rule_doc", "session_usage")
_WEEKLY_UPSERT = """
INSERT INTO cluster_week(detector_id, key, week, sessions, events)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(detector_id, key, week) DO UPDATE SET
  sessions=excluded.sessions, events=excluded.events
"""


def _clock(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("now must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError("now must be an ISO-8601 timestamp or datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _positive_days(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("history_days must be a positive integer")
    return value


def _monday(value: datetime) -> datetime:
    return (value - timedelta(days=value.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _week_label(value: datetime) -> str:
    iso_year, iso_week, _ = value.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _copy_reference_table(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> None:
    columns = tuple(source.execute(f"PRAGMA table_info({_quote_identifier(table)})"))
    if not columns:
        return
    names = tuple(str(row[1]) for row in columns)
    declarations = ", ".join(
        f"{_quote_identifier(str(row[1]))} {str(row[2] or 'TEXT')}"
        for row in columns
    )
    target.execute(f"CREATE TABLE {_quote_identifier(table)} ({declarations})")
    quoted = ", ".join(_quote_identifier(name) for name in names)
    if table == "observation" and start is not None and end is not None:
        rows = source.execute(
            f"SELECT {quoted} FROM {_quote_identifier(table)} "
            "WHERE ts_utc >= ? AND ts_utc < ?",
            (start, end),
        )
    else:
        rows = source.execute(
            f"SELECT {quoted} FROM {_quote_identifier(table)}"
        )
    placeholders = ", ".join("?" for _ in names)
    target.executemany(
        f"INSERT INTO {_quote_identifier(table)} ({quoted}) VALUES ({placeholders})",
        rows,
    )


def _filtered_connection(
    source: sqlite3.Connection,
    start: str,
    end: str,
) -> sqlite3.Connection:
    target = sqlite3.connect(":memory:")
    try:
        for table in _REFERENCE_TABLES:
            _copy_reference_table(source, target, table, start=start, end=end)
        target.execute("PRAGMA query_only = ON")
        return target
    except BaseException:
        target.close()
        raise


def _has_reference_data(connection: sqlite3.Connection) -> bool:
    for table in _REFERENCE_TABLES:
        columns = connection.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        ).fetchall()
        if not columns:
            continue
        if connection.execute(
            f"SELECT 1 FROM {_quote_identifier(table)} LIMIT 1"
        ).fetchone() is not None:
            return True
    return False


def _filtered_has_detector_data(
    connection: sqlite3.Connection, detector: Detector
) -> bool:
    if connection.execute("SELECT 1 FROM observation LIMIT 1").fetchone() is not None:
        return True
    for table in ("rule_doc", "session_usage"):
        if table in detector.cluster_sql and connection.execute(
            f"SELECT 1 FROM {table} LIMIT 1"
        ).fetchone() is not None:
            return True
    return False


def _replay_by_week(
    connection: sqlite3.Connection,
    detector: Detector,
    start: datetime,
    end: datetime,
) -> dict[tuple[str, str], tuple[int, int]]:
    counts: dict[tuple[str, str], tuple[int, int]] = {}
    if not _has_reference_data(connection):
        return counts
    first = _monday(start)
    last = _monday(end)
    while first <= last:
        week_start = max(first, start)
        week_end = min(first + timedelta(days=7), end)
        filtered = _filtered_connection(
            connection,
            week_start.isoformat(),
            (week_end + timedelta(microseconds=1)).isoformat(),
        )
        try:
            if not _filtered_has_detector_data(filtered, detector):
                first += timedelta(days=7)
                continue
            emitted = read_clusters(
                filtered,
                detector,
                window_start_utc=first.isoformat(),
                window_days=7,
            )
        finally:
            filtered.close()
        label = _week_label(first)
        for key, values in emitted.items():
            # A detector may emit non-observation findings with zero counts
            # (D-09 is one example).  They are valid clusters, but they are
            # not weekly activity and do not belong in the rate series.
            if values[0] == 0 and values[1] == 0:
                continue
            counts[(key, label)] = (values[0], values[1])
        first += timedelta(days=7)
    return counts


def _weekly_counts(
    connection: sqlite3.Connection,
    detector: Detector,
    *,
    start: datetime,
    end: datetime,
    history_days: int,
) -> dict[tuple[str, str], tuple[int, int]]:
    if detector.weekly_hits_sql is not None:
        return read_weekly_counts(
            connection,
            detector,
            window_start_utc=start.isoformat(),
            window_days=history_days,
            window_end_utc=end.isoformat(),
        )
    return _replay_by_week(connection, detector, start, end)


def _persist_counts(
    connection: sqlite3.Connection,
    detector: Detector,
    counts: dict[tuple[str, str], tuple[int, int]],
    *,
    manage_transaction: bool,
) -> int:
    savepoint = "twill_cluster_week"
    owns_transaction = False
    started_transaction = False
    if connection.in_transaction:
        connection.execute(f"SAVEPOINT {savepoint}")
    else:
        connection.execute("BEGIN IMMEDIATE")
        started_transaction = True
        owns_transaction = manage_transaction
    try:
        connection.executemany(
            _WEEKLY_UPSERT,
            (
                (detector.detector_id, key, week, sessions, events)
                for (key, week), (sessions, events) in sorted(counts.items())
            ),
        )
        if owns_transaction:
            connection.commit()
        elif not started_transaction:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        if started_transaction:
            connection.rollback()
        elif connection.in_transaction:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    return len(counts)


def aggregate_detector_weeks(
    connection: sqlite3.Connection,
    detector: Detector,
    *,
    now: str | datetime | None = None,
    history_days: int = WEEKLY_HISTORY_DAYS,
    manage_transaction: bool = True,
) -> int:
    """Aggregate one detector's weekly sessions and events into ``cluster_week``."""

    days = _positive_days(history_days)
    current = _clock(now)
    start = current - timedelta(days=days)
    end = current + timedelta(microseconds=1)
    counts = _weekly_counts(
        connection,
        detector,
        start=start,
        end=end,
        history_days=days,
    )
    return _persist_counts(
        connection,
        detector,
        counts,
        manage_transaction=manage_transaction,
    )


def aggregate_cluster_weeks(
    connection: sqlite3.Connection,
    detector: Detector,
    *,
    now: str | datetime | None = None,
    history_days: int = WEEKLY_HISTORY_DAYS,
    manage_transaction: bool = True,
) -> int:
    """Compatibility name for aggregating one detector's weekly series."""

    return aggregate_detector_weeks(
        connection,
        detector,
        now=now,
        history_days=history_days,
        manage_transaction=manage_transaction,
    )


def refresh_cluster_weeks(
    connection: sqlite3.Connection,
    *,
    registry: Sequence[Detector] | None = None,
    only: Sequence[str] | None = None,
    now: str | datetime | None = None,
    history_days: int = WEEKLY_HISTORY_DAYS,
) -> int:
    """Refresh the weekly series for the selected detector registry."""

    active = twill_detectors.build_registry(
        *(tuple(registry) if registry is not None else twill_detectors.REGISTRY)
    )
    if only:
        active = twill_detectors.select_detectors(active, only)
    return sum(
        aggregate_detector_weeks(
            connection,
            detector,
            now=now,
            history_days=history_days,
        )
        for detector in active
    )


# ---------------------------------------------------------------------------
# Change-point detection


TREND_NEW = "new"
TREND_ACCELERATING = "accelerating"
TREND_INSUFFICIENT_HISTORY = "insufficient_history"
TREND_SIGNAL_STATUSES = frozenset({TREND_NEW, TREND_ACCELERATING})


@dataclass(frozen=True)
class TrendPoint:
    """One zero-filled weekly bucket for a detector/key pair."""

    week: str
    sessions: int
    events: int

    def as_dict(self) -> dict[str, object]:
        return {
            "week": self.week,
            "sessions": self.sessions,
            "events": self.events,
        }


@dataclass(frozen=True)
class TrendFinding:
    """A change-point (or an explicit insufficient-history result)."""

    detector_id: str
    key: str
    status: str
    latest_week: str
    history_weeks: int
    minimum_history_weeks: int
    current_sessions: int
    current_events: int
    sessions_ewma: float | None
    events_ewma: float | None
    sessions_band: float | None
    events_band: float | None
    signal_metric: str | None
    signal_excess: float | None
    series: tuple[TrendPoint, ...]

    @property
    def verdict(self) -> str:
        """Compatibility name for callers that call a finding a verdict."""

        return self.status

    def as_dict(self) -> dict[str, object]:
        return {
            "detector_id": self.detector_id,
            "key": self.key,
            "status": self.status,
            "verdict": self.status,
            "latest_week": self.latest_week,
            "history_weeks": self.history_weeks,
            "minimum_history_weeks": self.minimum_history_weeks,
            "current": {
                "week": self.latest_week,
                "sessions": self.current_sessions,
                "events": self.current_events,
            },
            "current_sessions": self.current_sessions,
            "current_events": self.current_events,
            "sessions_ewma": self.sessions_ewma,
            "events_ewma": self.events_ewma,
            "sessions_band": self.sessions_band,
            "events_band": self.events_band,
            "signal_metric": self.signal_metric,
            "signal_excess": self.signal_excess,
            "series": [point.as_dict() for point in self.series],
        }


@dataclass(frozen=True)
class TrendReport:
    """The read-only result rendered by ``twill trend``."""

    as_of: str
    weeks: int
    minimum_history_weeks: int
    latest_week: str | None
    history_weeks: int
    detector_ids: tuple[str, ...]
    findings: tuple[TrendFinding, ...]
    database: bool = True
    warnings: tuple[str, ...] = ()

    @property
    def history_sufficient(self) -> bool:
        return self.history_weeks >= self.minimum_history_weeks

    @property
    def new_findings(self) -> tuple[TrendFinding, ...]:
        return tuple(finding for finding in self.findings if finding.status == TREND_NEW)

    @property
    def accelerating_findings(self) -> tuple[TrendFinding, ...]:
        return tuple(
            finding for finding in self.findings if finding.status == TREND_ACCELERATING
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of,
            "database": self.database,
            "weeks": self.weeks,
            "minimum_history_weeks": self.minimum_history_weeks,
            "latest_week": self.latest_week,
            "history_weeks": self.history_weeks,
            "history_sufficient": self.history_sufficient,
            "detectors": list(self.detector_ids),
            "findings": [finding.as_dict() for finding in self.findings],
            "summary": {
                "findings": len(self.findings),
                "new": len(self.new_findings),
                "accelerating": len(self.accelerating_findings),
                "insufficient_history": sum(
                    finding.status == TREND_INSUFFICIENT_HISTORY
                    for finding in self.findings
                ),
            },
        }


def _positive_weeks(value: object, name: str = "weeks") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _fraction(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not 0.0 < result <= 1.0:
        raise ValueError(f"{name} must be greater than 0 and at most 1")
    return result


def _nonnegative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if result < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _week_start_label(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 8 or text[4] != "-" or text[5] != "W":
        return None
    try:
        return date.fromisocalendar(int(text[:4]), int(text[6:]), 1)
    except (TypeError, ValueError):
        return None


def _week_label_from_start(value: date) -> str:
    iso = value.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def _ewma(values: Sequence[float], alpha: float) -> float | None:
    if not values:
        return None
    level = float(values[0])
    for value in values[1:]:
        level = alpha * float(value) + (1.0 - alpha) * level
    return level


def _band(values: Sequence[float], alpha: float, sigmas: float) -> tuple[float, float] | None:
    level = _ewma(values, alpha)
    if level is None:
        return None
    mean = sum(float(value) for value in values) / len(values)
    variance = sum((float(value) - mean) ** 2 for value in values) / len(values)
    return level, level + sigmas * sqrt(variance)


def _detector_filter(
    detector: str | Sequence[str] | None,
    only: Sequence[str] | None,
    detector_id: str | None,
) -> tuple[str, ...] | None:
    supplied = [value for value in (detector, only, detector_id) if value is not None]
    if len(supplied) > 1:
        raise ValueError("pass only one of detector, detector_id, or only")
    if not supplied:
        return None
    value = supplied[0]
    if isinstance(value, str):
        values = (value,)
    else:
        values = tuple(value)
    result = tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
    if not result:
        raise ValueError("detector must not be empty")
    return result


def _weekly_rows(
    connection: sqlite3.Connection,
    detector_ids: tuple[str, ...] | None,
) -> tuple[
    dict[tuple[str, str], dict[str, tuple[int, int]]],
    dict[str, set[str]],
    tuple[str, ...],
]:
    query = (
        "SELECT detector_id, key, week, sessions, events FROM cluster_week "
        "ORDER BY detector_id, key, week"
    )
    rows = connection.execute(query).fetchall()
    series: dict[tuple[str, str], dict[str, tuple[int, int]]] = {}
    valid_weeks: dict[str, set[str]] = {}
    seen_detectors: set[str] = set()
    for detector_id, key, week, sessions, events in rows:
        detector_text = str(detector_id)
        if detector_ids is not None and detector_text not in detector_ids:
            continue
        week_text = str(week)
        if _week_start_label(week_text) is None:
            # A malformed manually repaired row cannot be placed in a
            # contiguous calendar series.  The caller reports the omission.
            continue
        try:
            session_count = int(sessions)
            event_count = int(events)
        except (TypeError, ValueError):
            continue
        if session_count < 0 or event_count < 0:
            continue
        identity = (detector_text, str(key))
        series.setdefault(identity, {})[week_text] = (session_count, event_count)
        valid_weeks.setdefault(detector_text, set()).add(week_text)
        seen_detectors.add(detector_text)
    return series, valid_weeks, tuple(sorted(seen_detectors))


def _signal_for_series(
    identity: tuple[str, str],
    values: dict[str, tuple[int, int]],
    labels: tuple[str, ...],
    *,
    history_weeks: int,
    minimum_history_weeks: int,
    alpha: float,
    sigmas: float,
) -> TrendFinding | None:
    detector_id, key = identity
    points = tuple(
        TrendPoint(label, values.get(label, (0, 0))[0], values.get(label, (0, 0))[1])
        for label in labels
    )
    latest = points[-1]
    if history_weeks < minimum_history_weeks:
        return TrendFinding(
            detector_id=detector_id,
            key=key,
            status=TREND_INSUFFICIENT_HISTORY,
            latest_week=latest.week,
            history_weeks=history_weeks,
            minimum_history_weeks=minimum_history_weeks,
            current_sessions=latest.sessions,
            current_events=latest.events,
            sessions_ewma=None,
            events_ewma=None,
            sessions_band=None,
            events_band=None,
            signal_metric=None,
            signal_excess=None,
            series=points,
        )

    prior = points[:-1]
    session_stats = _band(tuple(point.sessions for point in prior), alpha, sigmas)
    event_stats = _band(tuple(point.events for point in prior), alpha, sigmas)
    assert session_stats is not None and event_stats is not None
    sessions_ewma, sessions_band = session_stats
    events_ewma, events_band = event_stats

    previous_observation = any(
        session_count > 0 or event_count > 0
        for week, (session_count, event_count) in values.items()
        if week != latest.week and _week_start_label(week) is not None
    )
    is_new = (
        not previous_observation
        and (latest.sessions > 0 or latest.events > 0)
    )
    event_excess = float(latest.events) - events_band
    session_excess = float(latest.sessions) - sessions_band
    is_accelerating = event_excess > 0.0 or session_excess > 0.0
    if not is_new and not is_accelerating:
        return None
    status = TREND_NEW if is_new else TREND_ACCELERATING
    if event_excess >= session_excess:
        metric, excess = "events", event_excess
    else:
        metric, excess = "sessions", session_excess
    return TrendFinding(
        detector_id=detector_id,
        key=key,
        status=status,
        latest_week=latest.week,
        history_weeks=history_weeks,
        minimum_history_weeks=minimum_history_weeks,
        current_sessions=latest.sessions,
        current_events=latest.events,
        sessions_ewma=sessions_ewma,
        events_ewma=events_ewma,
        sessions_band=sessions_band,
        events_band=events_band,
        signal_metric=metric,
        signal_excess=excess,
        series=points,
    )


def build_trend_report(
    connection: sqlite3.Connection,
    *,
    weeks: int = DEFAULT_TREND_WEEKS,
    detector: str | Sequence[str] | None = None,
    detector_id: str | None = None,
    only: Sequence[str] | None = None,
    new_only: bool = False,
    minimum_history_weeks: int = MIN_TREND_HISTORY_WEEKS,
    alpha: float = EWMA_ALPHA,
    band_sigmas: float = EWMA_BAND_SIGMAS,
    now: str | datetime | None = None,
) -> TrendReport:
    """Detect new and accelerating weekly friction with an EWMA band.

    The current bucket is compared with an EWMA and population standard
    deviation computed from the preceding buckets of the same detector/key.
    Missing rows are zero-filled, which lets a signature that appears after a
    quiet period be identified as new without allowing a busy signature to
    borrow another signature's baseline.  The report is read-only.
    """

    requested_weeks = _positive_weeks(weeks)
    minimum = _positive_weeks(minimum_history_weeks, "minimum_history_weeks")
    resolved_alpha = _fraction(alpha, "alpha")
    resolved_sigmas = _nonnegative_number(band_sigmas, "band_sigmas")
    selected = _detector_filter(detector, only, detector_id)
    as_of = _clock(now)
    series, valid_weeks_by_detector, seen_detectors = _weekly_rows(
        connection, selected
    )
    valid_weeks = set().union(*valid_weeks_by_detector.values())
    if not valid_weeks:
        warnings = (
            "no cluster_week history is available; run twill detect to build the weekly series",
        )
        if selected:
            seen_detectors = selected
        return TrendReport(
            as_of=as_of.isoformat(),
            weeks=requested_weeks,
            minimum_history_weeks=minimum,
            latest_week=None,
            history_weeks=0,
            detector_ids=seen_detectors,
            findings=(),
            database=True,
            warnings=warnings,
        )

    latest_start = max(_week_start_label(week) for week in valid_weeks)
    assert latest_start is not None
    detector_latest = {
        detector_id: max(_week_start_label(week) for week in detector_weeks)
        for detector_id, detector_weeks in valid_weeks_by_detector.items()
    }
    detector_labels = {
        detector_id: tuple(
            _week_label_from_start(latest - timedelta(days=7 * offset))
            for offset in range(requested_weeks - 1, -1, -1)
        )
        for detector_id, latest in detector_latest.items()
        if latest is not None
    }
    detector_history = {
        detector_id: sum(
            label in valid_weeks_by_detector[detector_id]
            for label in labels
        )
        for detector_id, labels in detector_labels.items()
    }
    history_weeks = min(detector_history.values()) if detector_history else 0
    findings: list[TrendFinding] = []
    for identity, values in sorted(series.items()):
        detector_id = identity[0]
        labels = detector_labels[detector_id]
        finding = _signal_for_series(
            identity,
            values,
            labels,
            history_weeks=detector_history[detector_id],
            minimum_history_weeks=minimum,
            alpha=resolved_alpha,
            sigmas=resolved_sigmas,
        )
        if finding is None:
            continue
        if new_only and finding.status != TREND_NEW:
            continue
        findings.append(finding)

    findings.sort(
        key=lambda finding: (
            0 if finding.status == TREND_NEW else 1,
            -(finding.signal_excess or 0.0),
            finding.detector_id,
            finding.key,
        )
    )
    warnings: list[str] = []
    if history_weeks < minimum:
        warnings.append(
            f"cluster_week has {history_weeks} week(s) in the requested window; "
            f"EWMA trend detection needs at least {minimum} weeks of history",
        )
    return TrendReport(
        as_of=as_of.isoformat(),
        weeks=requested_weeks,
        minimum_history_weeks=minimum,
        latest_week=_week_label_from_start(latest_start),
        history_weeks=history_weeks,
        detector_ids=selected or seen_detectors,
        findings=tuple(findings),
        database=True,
        warnings=tuple(warnings),
    )


def empty_trend_report(
    *,
    weeks: int = DEFAULT_TREND_WEEKS,
    minimum_history_weeks: int = MIN_TREND_HISTORY_WEEKS,
    now: str | datetime | None = None,
) -> TrendReport:
    """Return the successful empty result used when no state DB exists."""

    requested_weeks = _positive_weeks(weeks)
    minimum = _positive_weeks(minimum_history_weeks, "minimum_history_weeks")
    return TrendReport(
        as_of=_clock(now).isoformat(),
        weeks=requested_weeks,
        minimum_history_weeks=minimum,
        latest_week=None,
        history_weeks=0,
        detector_ids=(),
        findings=(),
        database=False,
        warnings=("no state database exists; run twill detect first",),
    )


# Descriptive aliases for callers that use the component name from the plan.
detect_trends = build_trend_report
change_points = build_trend_report


def render_trend_text(report: TrendReport) -> str:
    """Render the concise operator-facing trend view."""

    lines = [
        "TWILL trend",
        f"weeks: {report.weeks}",
        f"history: {report.history_weeks}/{report.minimum_history_weeks} week(s)",
    ]
    if report.latest_week is not None:
        lines.append(f"latest week: {report.latest_week}")
    if not report.findings:
        lines.append("no new or accelerating friction")
        return "\n".join(lines)
    for finding in report.findings:
        if finding.status == TREND_INSUFFICIENT_HISTORY:
            lines.append(
                f"- {finding.detector_id} {finding.key}: insufficient history "
                f"({finding.history_weeks}/{finding.minimum_history_weeks} week(s))"
            )
            continue
        lines.append(
            f"- {finding.detector_id} {finding.key}: {finding.status} "
            f"({finding.signal_metric}={getattr(finding, 'current_' + finding.signal_metric)}; "
            f"week {finding.latest_week})"
        )
    return "\n".join(lines)


run_cluster_week_aggregation = refresh_cluster_weeks
aggregate_all_cluster_weeks = refresh_cluster_weeks


__all__ = [
    "DEFAULT_TREND_WEEKS",
    "EWMA_ALPHA",
    "EWMA_BAND_SIGMAS",
    "MIN_TREND_HISTORY_WEEKS",
    "TREND_ACCELERATING",
    "TREND_INSUFFICIENT_HISTORY",
    "TREND_NEW",
    "TrendFinding",
    "TrendPoint",
    "TrendReport",
    "WEEKLY_HISTORY_DAYS",
    "aggregate_all_cluster_weeks",
    "aggregate_cluster_weeks",
    "aggregate_detector_weeks",
    "build_trend_report",
    "change_points",
    "detect_trends",
    "empty_trend_report",
    "render_trend_text",
    "refresh_cluster_weeks",
    "run_cluster_week_aggregation",
]
