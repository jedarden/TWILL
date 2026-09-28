"""Render the weekly digest as a reproducible week-over-week report."""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Sequence

import twill_detectors
import twill_lessons
import twill_measure
import twill_rules
import twill_trend
from twill_contract import ValidationError
from twill_detectors import (
    MAX_ERROR_LENGTH,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_REFUSED,
    Detector,
    DetectorContractError,
    read_clusters,
)
from twill_redactor import redact_text
from twill_ranker import EstimatedWaste
from twill_schema import connect_read_only, state_db_path


MAX_LINE_LENGTH = 240
DIGEST_DIR_MODE = 0o700
DIGEST_FILE_MODE = 0o600
LESSON_FLOW_WINDOW_DAYS = 60
VERDICT_NEW = "new"
VERDICT_WORSENING = "worsening"
VERDICT_IMPROVING = "improving"
VERDICT_GONE = "gone"
VERDICT_ORDER = (
    VERDICT_NEW,
    VERDICT_WORSENING,
    VERDICT_IMPROVING,
    VERDICT_GONE,
)
TREND_ORDER = (
    twill_trend.TREND_NEW,
    twill_trend.TREND_ACCELERATING,
)
VERDICT_TREND = "trend"
STATUS_NO_DATABASE = "no-database"
_WEEK_PATTERN = re.compile(r"^(\d{4})-W(\d{2})$")
Week = tuple[int, int]
Counts = tuple[int, int, str, str]


class WeekError(ValueError):
    pass


def parse_week(value: str) -> Week:
    match = _WEEK_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise WeekError(f"invalid ISO week {value!r}; expected YYYY-Www")
    year, week = (int(part) for part in match.groups())
    try:
        date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise WeekError(f"invalid ISO week {value!r}; {exc}") from exc
    return year, week


def format_week(week: Week) -> str:
    return f"{week[0]:04d}-W{week[1]:02d}"


def week_bounds(week: Week) -> tuple[str, str]:
    start = date.fromisocalendar(week[0], week[1], 1)
    end = start + timedelta(days=7)
    return (
        datetime(start.year, start.month, start.day, tzinfo=timezone.utc).isoformat(),
        datetime(end.year, end.month, end.day, tzinfo=timezone.utc).isoformat(),
    )


def previous_week(week: Week) -> Week:
    start = date.fromisocalendar(week[0], week[1], 1)
    return (start - timedelta(days=7)).isocalendar()[:2]


def default_week(now: datetime | None = None) -> Week:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    instant = instant.astimezone(timezone.utc)
    year, week, _ = instant.isocalendar()
    monday = date.fromisocalendar(year, week, 1)
    return (monday - timedelta(days=7)).isocalendar()[:2]


def reproduction_command(state_dir: Path, week: Week) -> str:
    state_text = str(Path(state_dir).expanduser().resolve())
    if any(not character.isprintable() for character in state_text):
        raise ValueError("state directory path must contain only printable characters")
    state_argument = shlex.quote(state_text)
    command = (
        f"twill digest --week {format_week(week)} --stdout "
        f"--state-dir {state_argument}"
    )
    if (
        redact_text(state_text) != state_text
        or len(command) > MAX_LINE_LENGTH - len(" | $ ") - 1
    ):
        state_argument = (
            '"${TWILL_STATE_DIR:?set TWILL_STATE_DIR to the state directory '
            'used for this report}"'
        )
        command = (
            f"twill digest --week {format_week(week)} --stdout "
            f"--state-dir {state_argument}"
        )
    return command


def _one_line(value: object, limit: int) -> str:
    return " ".join(redact_text(value).split())[:limit]


def _display_key(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)[1:-1]


def _line(claim: str, command: str) -> str:
    suffix = f" | $ {command}"
    available = MAX_LINE_LENGTH - len(suffix)
    if available < 1:
        raise ValueError("digest reproduction command exceeds the line budget")
    safe_claim = _one_line(claim, MAX_LINE_LENGTH)
    if len(safe_claim) <= available:
        return safe_claim + suffix
    if available <= 3:
        return safe_claim[:available] + suffix
    return safe_claim[: available - 3] + "..." + suffix


@dataclass(frozen=True)
class _DetectorWindow:
    detector: Detector
    status: str
    clusters: dict[str, Counts]
    error: str | None
    estimated_waste: dict[str, EstimatedWaste]


@dataclass(frozen=True)
class DetectorSummary:
    detector_id: str
    version: int
    current_status: str
    previous_status: str
    current_clusters: int
    previous_clusters: int
    current_error: str | None
    previous_error: str | None

    @property
    def full_id(self) -> str:
        return f"{self.detector_id}@{self.version}"


@dataclass(frozen=True)
class Finding:
    n: int
    verdict: str
    detector: str
    key: str
    current: Counts | None
    previous: Counts | None
    week: str
    reproduce: str
    estimated_waste: EstimatedWaste | None = None
    trend_status: str | None = None
    trend_metric: str | None = None
    trend_excess: float | None = None


@dataclass(frozen=True)
class CoveredEscalation:
    """A recurring weekly cluster covered by a live rule document.

    This is intentionally separate from :class:`EscalationProposal`, which
    belongs to an already-applied TWILL lesson.  A covered cluster has no
    lesson to draft; its rule path and week-over-week counts are the digest
    representation of the escalation lane.
    """

    n: int
    detector: str
    key: str
    covered_by: str
    state: str
    current: Counts
    previous: Counts | None
    week: str
    reproduce: str

    def as_dict(self) -> dict[str, object]:
        previous = self.previous
        return {
            "n": self.n,
            "verdict": "covered",
            "escalation": True,
            "detector": self.detector,
            "key": self.key,
            "covered_by": self.covered_by,
            "state": self.state,
            "sessions": self.current[0],
            "events": self.current[1],
            "first_seen": self.current[2],
            "last_seen": self.current[3],
            "previous": (
                {
                    "sessions": previous[0],
                    "events": previous[1],
                    "first_seen": previous[2],
                    "last_seen": previous[3],
                }
                if previous is not None
                else None
            ),
            "week": self.week,
            "reproduce": self.reproduce,
            "reason": "covered recurrence is routed to escalation, not Explain",
        }


@dataclass(frozen=True)
class LessonFlowHealth:
    """The lesson lifecycle counts for one completed digest window."""

    window_days: int
    window_start: str | None
    window_end: str | None
    drafted: int
    accepted: int
    applied: int
    resolved: int
    available: bool = True
    warning: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "window_days": self.window_days,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "drafted": self.drafted,
            "accepted": self.accepted,
            "applied": self.applied,
            "resolved": self.resolved,
            "available": self.available,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class DigestReport:
    state_dir: Path
    week: Week
    prior_week: Week
    command: str
    database: bool
    observations: int
    observations_in_week: int
    observations_in_previous_week: int
    detectors: tuple[DetectorSummary, ...]
    findings: tuple[Finding, ...]
    warnings: tuple[str, ...]
    lesson_flow: LessonFlowHealth
    escalations: tuple[twill_measure.EscalationProposal, ...] = ()
    covered_escalations: tuple[CoveredEscalation, ...] = ()
    retirements: tuple[twill_rules.RetirementProposal, ...] = ()
    trend: twill_trend.TrendReport | None = None

    @property
    def week_id(self) -> str:
        return format_week(self.week)

    @property
    def previous_week_id(self) -> str:
        return format_week(self.prior_week)

    @property
    def clean(self) -> bool:
        return (
            self.database
            and not self.findings
            and not self.escalations
            and not self.covered_escalations
            and not self.retirements
            and all(
                detector.current_status == STATUS_OK
                and detector.previous_status == STATUS_OK
                for detector in self.detectors
            )
        )


def _parse_lesson_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("lesson timestamp is not text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def lesson_flow_health(
    artifacts_root: Path | None,
    *,
    as_of: datetime,
    window_days: int = LESSON_FLOW_WINDOW_DAYS,
) -> LessonFlowHealth:
    """Count current lesson states in the trailing completed digest window.

    The lesson files predate lifecycle history, so draft, accepted, and
    resolved timestamps come from their file metadata.  Application has an
    explicit timestamp in the lesson frontmatter and uses that instead.  The
    digest's ``as_of`` value keeps historical reports reproducible rather than
    comparing them with the wall clock at render time.
    """

    if artifacts_root is None:
        return LessonFlowHealth(
            window_days=window_days,
            window_start=None,
            window_end=None,
            drafted=0,
            accepted=0,
            applied=0,
            resolved=0,
            available=False,
            warning="artifacts_root is not configured; lesson flow is unavailable",
        )
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    as_of = as_of.astimezone(timezone.utc)
    start = as_of - timedelta(days=window_days)
    records = twill_lessons.list_lessons(Path(artifacts_root))
    counts = {"draft": 0, "accepted": 0, "applied": 0, "resolved": 0}
    for record in records:
        category = twill_lessons.state_category(record.state)
        if category == "applied":
            applied_at = record.routing.get("applied_at")
            if applied_at is None:
                continue
            event_at = _parse_lesson_timestamp(applied_at)
        else:
            event_at = datetime.fromtimestamp(record.path.stat().st_mtime, timezone.utc)
        if start <= event_at <= as_of and category in counts:
            counts[category] += 1
    warning = (
        "no lessons reached applied in this 60-day window"
        if counts["applied"] == 0
        else None
    )
    return LessonFlowHealth(
        window_days=window_days,
        window_start=start.isoformat().replace("+00:00", "Z"),
        window_end=as_of.isoformat().replace("+00:00", "Z"),
        drafted=counts["draft"],
        accepted=counts["accepted"],
        applied=counts["applied"],
        resolved=counts["resolved"],
        warning=warning,
    )


def _read_window(
    source: sqlite3.Connection,
    detectors: Sequence[Detector],
    start: str,
    end: str,
) -> tuple[_DetectorWindow, ...]:
    columns = tuple(
        (str(row[1]), str(row[2] or "TEXT"))
        for row in source.execute("PRAGMA table_info(observation)")
    )
    if not columns:
        raise sqlite3.DatabaseError("observation table has no columns")
    quoted_columns = ", ".join(
        '"' + name.replace('"', '""') + '"' for name, _ in columns
    )
    declarations = ", ".join(
        '"' + name.replace('"', '""') + '" ' + declaration
        for name, declaration in columns
    )
    placeholders = ", ".join("?" for _ in columns)
    memory = sqlite3.connect(":memory:")
    try:
        memory.execute(f"CREATE TABLE observation ({declarations})")
        memory.executemany(
            f"INSERT INTO observation ({quoted_columns}) VALUES ({placeholders})",
            source.execute(
                f"SELECT {quoted_columns} FROM observation "
                "WHERE ts_utc >= ? AND ts_utc < ?",
                (start, end),
            ),
        )
        rule_columns = tuple(
            (str(row[1]), str(row[2] or "TEXT"))
            for row in source.execute("PRAGMA table_info(rule_doc)")
        )
        if rule_columns:
            rule_quoted_columns = ", ".join(
                '"' + name.replace('"', '""') + '"' for name, _ in rule_columns
            )
            rule_declarations = ", ".join(
                '"' + name.replace('"', '""') + '" ' + declaration
                for name, declaration in rule_columns
            )
            rule_placeholders = ", ".join("?" for _ in rule_columns)
            memory.execute(f"CREATE TABLE rule_doc ({rule_declarations})")
            memory.executemany(
                f"INSERT INTO rule_doc ({rule_quoted_columns}) VALUES ({rule_placeholders})",
                source.execute(f"SELECT {rule_quoted_columns} FROM rule_doc"),
            )
            if source.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'rule_fts'"
            ).fetchone():
                memory.execute(
                    "CREATE VIRTUAL TABLE rule_fts USING fts5("
                    "text, path UNINDEXED, tokenize='porter unicode61')"
                )
                memory.executemany(
                    "INSERT INTO rule_fts(text, path) VALUES (?, ?)",
                    source.execute("SELECT text, path FROM rule_fts"),
                )
        usage_columns = tuple(
            (str(row[1]), str(row[2] or "TEXT"))
            for row in source.execute("PRAGMA table_info(session_usage)")
        )
        if usage_columns:
            usage_quoted_columns = ", ".join(
                '"' + name.replace('"', '""') + '"'
                for name, _ in usage_columns
            )
            usage_declarations = ", ".join(
                '"' + name.replace('"', '""') + '" ' + declaration
                for name, declaration in usage_columns
            )
            usage_placeholders = ", ".join("?" for _ in usage_columns)
            memory.execute(f"CREATE TABLE session_usage ({usage_declarations})")
            memory.executemany(
                f"INSERT INTO session_usage ({usage_quoted_columns}) VALUES ({usage_placeholders})",
                source.execute(f"SELECT {usage_quoted_columns} FROM session_usage"),
            )
        memory.execute("PRAGMA query_only = ON")

        results: list[_DetectorWindow] = []
        session_hits: list[tuple[str, str, str]] = []
        for detector in detectors:
            try:
                twill_detectors.validate_detector_semantics(source, detector)
            except DetectorContractError as exc:
                results.append(
                    _DetectorWindow(
                        detector=detector,
                        status=STATUS_REFUSED,
                        clusters={},
                        error=_one_line(exc, MAX_ERROR_LENGTH),
                        estimated_waste={},
                    )
                )
                continue
            except sqlite3.Error as exc:
                results.append(
                    _DetectorWindow(
                        detector=detector,
                        status=STATUS_ERROR,
                        clusters={},
                        error=_one_line(exc, MAX_ERROR_LENGTH),
                        estimated_waste={},
                    )
                )
                continue
            try:
                clusters = read_clusters(
                    memory,
                    detector,
                    window_start_utc=start,
                    window_days=7,
                )
                if detector.session_hits_sql is not None:
                    hit_cursor = memory.execute(
                        detector.session_hits_sql,
                        {"window_start_utc": start, "window_days": 7},
                    )
                    hits = twill_detectors._collect_session_hits(
                        detector,
                        hit_cursor,
                        clusters,
                    )
                    session_hits.extend(
                        (detector.detector_id, key, session_id)
                        for key, session_id in hits
                    )
            except (sqlite3.Error, DetectorContractError) as exc:
                results.append(
                    _DetectorWindow(
                        detector=detector,
                        status=STATUS_ERROR,
                        clusters={},
                        error=_one_line(exc, MAX_ERROR_LENGTH),
                        estimated_waste={},
                    )
                )
            else:
                results.append(
                    _DetectorWindow(
                        detector=detector,
                        status=STATUS_OK,
                        clusters=clusters,
                        error=None,
                        estimated_waste={},
                    )
                )
        estimates = _attribute_waste(memory, session_hits)
        return tuple(
            replace(
                result,
                estimated_waste={
                    key: estimates[(result.detector.detector_id, key)]
                    for key in result.clusters
                    if (result.detector.detector_id, key) in estimates
                },
            )
            for result in results
        )
    finally:
        memory.close()


def _dismissed_cluster_ids(connection: sqlite3.Connection) -> frozenset[tuple[str, str]]:
    """Return the durable suppression set for the digest's detector replay."""

    return frozenset(
        (str(detector_id), str(key))
        for detector_id, key in connection.execute(
            "SELECT detector_id, key FROM cluster WHERE state = 'dismissed'"
        )
    )


def _suppress_dismissed(
    results: Sequence[_DetectorWindow],
    dismissed: frozenset[tuple[str, str]],
) -> tuple[_DetectorWindow, ...]:
    """Keep permanently dismissed clusters out of both digest windows."""

    if not dismissed:
        return tuple(results)
    filtered: list[_DetectorWindow] = []
    for result in results:
        detector_id = result.detector.detector_id
        clusters = {
            key: value
            for key, value in result.clusters.items()
            if (detector_id, key) not in dismissed
        }
        estimates = {
            key: value
            for key, value in result.estimated_waste.items()
            if (detector_id, key) not in dismissed
        }
        filtered.append(replace(result, clusters=clusters, estimated_waste=estimates))
    return tuple(filtered)


def _active_coverage(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[str, str]]:
    """Return live rule coverage for clusters in an escalation lane."""

    rows = connection.execute(
        "SELECT c.detector_id, c.key, c.covered_by, c.state "
        "FROM cluster AS c "
        "JOIN rule_doc AS d ON d.path = c.covered_by "
        "WHERE c.covered_by IS NOT NULL AND d.stale = 0 "
        "AND c.state IN ('open', 'escalation') "
        "ORDER BY c.detector_id, c.key"
    ).fetchall()
    return {
        (str(detector_id), str(key)): (str(covered_by), str(state))
        for detector_id, key, covered_by, state in rows
    }


def _covered_escalations(
    current_results: Sequence[_DetectorWindow],
    previous_results: Sequence[_DetectorWindow],
    coverage: dict[tuple[str, str], tuple[str, str]],
    *,
    week: str,
    reproduce: str,
) -> tuple[CoveredEscalation, ...]:
    """Build the digest's covered-recurring-cluster lane."""

    previous_by_detector = {
        result.detector.full_id: result for result in previous_results
    }
    result: list[CoveredEscalation] = []
    for current_result in current_results:
        if current_result.status != STATUS_OK:
            continue
        detector_id = current_result.detector.detector_id
        previous_result = previous_by_detector.get(current_result.detector.full_id)
        for key, current in current_result.clusters.items():
            covered = coverage.get((detector_id, key))
            if covered is None or current[0] < 1:
                continue
            previous = (
                previous_result.clusters.get(key)
                if previous_result is not None
                and previous_result.status == STATUS_OK
                else None
            )
            result.append(
                CoveredEscalation(
                    n=0,
                    detector=current_result.detector.full_id,
                    key=key,
                    covered_by=covered[0],
                    state=covered[1],
                    current=current,
                    previous=previous,
                    week=week,
                    reproduce=reproduce,
                )
            )
    result.sort(key=lambda item: (item.detector, item.key))
    return tuple(replace(item, n=index) for index, item in enumerate(result, start=1))


def _known_token(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _known_cost(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    resolved = float(value)
    if resolved < 0 or not isfinite(resolved):
        return None
    return resolved


def _attribute_waste(
    connection: sqlite3.Connection,
    session_hits: Sequence[tuple[str, str, str]],
) -> dict[tuple[str, str], EstimatedWaste]:
    """Attribute this digest window's usage across all distinct cluster hits."""

    hits = set(session_hits)
    if not hits:
        return {}
    try:
        usage_rows = connection.execute(
            "SELECT session_id, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd FROM session_usage"
        ).fetchall()
    except sqlite3.Error:
        usage_rows = []
    usage = {str(row[0]): row[1:] for row in usage_rows}
    cluster_hits_by_session: dict[str, int] = {}
    for _, _, session_id in hits:
        cluster_hits_by_session[session_id] = (
            cluster_hits_by_session.get(session_id, 0) + 1
        )

    token_columns = ("input_tokens", "output_tokens", "cache_read_tokens")
    token_values: dict[tuple[str, str], dict[str, float]] = {}
    token_counts: dict[tuple[str, str], dict[str, int]] = {}
    cost_values: dict[tuple[str, str], float] = {}
    cost_counts: dict[tuple[str, str], int] = {}
    hit_counts: dict[tuple[str, str], int] = {}
    for detector_id, key, session_id in sorted(hits):
        cluster = (detector_id, key)
        hit_counts[cluster] = hit_counts.get(cluster, 0) + 1
        values = token_values.setdefault(
            cluster, {column: 0.0 for column in token_columns}
        )
        counts = token_counts.setdefault(
            cluster, {column: 0 for column in token_columns}
        )
        session_usage = usage.get(session_id)
        if session_usage is None:
            continue
        denominator = cluster_hits_by_session[session_id]
        for index, column in enumerate(token_columns):
            token = _known_token(session_usage[index])
            if token is None:
                continue
            values[column] += token / denominator
            counts[column] += 1
        cost = _known_cost(session_usage[3])
        if cost is not None:
            cost_values[cluster] = (
                cost_values.get(cluster, 0.0) + cost / denominator
            )
            cost_counts[cluster] = cost_counts.get(cluster, 0) + 1

    estimates: dict[tuple[str, str], EstimatedWaste] = {}
    for cluster, hits_for_cluster in hit_counts.items():
        values = token_values[cluster]
        counts = token_counts[cluster]
        components = {
            column: values[column] if counts[column] == hits_for_cluster else None
            for column in token_columns
        }
        estimates[cluster] = EstimatedWaste(
            components["input_tokens"],
            components["output_tokens"],
            components["cache_read_tokens"],
            (
                cost_values[cluster]
                if cost_counts.get(cluster, 0) == hits_for_cluster
                else None
            ),
        )
    return estimates


def _verdict(current: Counts | None, previous: Counts | None) -> str | None:
    if current is not None and previous is None:
        return VERDICT_NEW
    if current is None and previous is not None:
        return VERDICT_GONE
    if current is None or previous is None:
        return None
    if current[0] > previous[0] or current[1] > previous[1]:
        return VERDICT_WORSENING
    if current[0] < previous[0] or current[1] < previous[1]:
        return VERDICT_IMPROVING
    return None


def _finding_data(finding: Finding) -> dict[str, object]:
    current = finding.current
    previous = finding.previous
    return {
        "n": finding.n,
        "verdict": finding.verdict,
        "detector": finding.detector,
        "key": finding.key,
        "sessions": current[0] if current is not None else None,
        "events": current[1] if current is not None else None,
        "first_seen": current[2] if current is not None else None,
        "last_seen": current[3] if current is not None else None,
        "previous": (
            {
                "sessions": previous[0],
                "events": previous[1],
                "first_seen": previous[2],
                "last_seen": previous[3],
            }
            if previous is not None
            else None
        ),
        "week": finding.week,
        "reproduce": finding.reproduce,
        "trend": finding.trend_status,
        "trend_status": finding.trend_status,
        "trend_metric": finding.trend_metric,
        "trend_excess": finding.trend_excess,
        **_finding_waste_data(finding),
    }


def _trend_data(report: twill_trend.TrendReport | None) -> dict[str, object]:
    if report is None:
        return {
            "available": False,
            "latest_week": None,
            "history_weeks": 0,
            "minimum_history_weeks": twill_trend.MIN_TREND_HISTORY_WEEKS,
            "new": 0,
            "accelerating": 0,
            "warnings": [],
            "findings": [],
        }
    findings = tuple(
        finding
        for finding in report.findings
        if finding.status in twill_trend.TREND_SIGNAL_STATUSES
    )
    return {
        "available": report.database,
        "latest_week": report.latest_week,
        "history_weeks": report.history_weeks,
        "minimum_history_weeks": report.minimum_history_weeks,
        "new": len(report.new_findings),
        "accelerating": len(report.accelerating_findings),
        "warnings": list(report.warnings),
        "findings": [
            {
                "detector": finding.detector_id,
                "key": finding.key,
                "status": finding.status,
                "latest_week": finding.latest_week,
                "current_sessions": finding.current_sessions,
                "current_events": finding.current_events,
                "signal_metric": finding.signal_metric,
                "signal_excess": finding.signal_excess,
            }
            for finding in findings
        ],
    }


def build_digest(
    state_dir: Path,
    week: Week,
    *,
    registry: Sequence[Detector] = twill_detectors.REGISTRY,
    artifacts_root: Path | None = None,
) -> DigestReport:
    state = Path(state_dir).expanduser().resolve()
    prior = previous_week(week)
    start, end = week_bounds(week)
    prior_start, prior_end = week_bounds(prior)
    command = reproduction_command(state, week)
    detectors = twill_detectors.build_registry(*registry)
    report_as_of = datetime.fromisoformat(end) - timedelta(microseconds=1)
    lesson_flow = lesson_flow_health(artifacts_root, as_of=report_as_of)
    escalations: tuple[twill_measure.EscalationProposal, ...] = ()
    if artifacts_root is not None:
        # A report for a completed week must not use a measurement recorded in
        # a later week.  Subtract a microsecond because week_bounds' end is an
        # exclusive boundary.
        escalations = twill_measure.escalation_proposals(
            artifacts_root,
            as_of=report_as_of,
        )
    retirements: tuple[twill_rules.RetirementProposal, ...] = ()
    db_path = state_db_path(state)
    if not db_path.is_file():
        summaries = tuple(
            DetectorSummary(
                detector_id=detector.detector_id,
                version=detector.version,
                current_status=STATUS_NO_DATABASE,
                previous_status=STATUS_NO_DATABASE,
                current_clusters=0,
                previous_clusters=0,
                current_error=None,
                previous_error=None,
            )
            for detector in detectors
        )
        warning = _one_line(
            f"no state database at {db_path}; nothing has been ingested yet, "
            "so no detector ran",
            MAX_ERROR_LENGTH,
        )
        return DigestReport(
            state_dir=state,
            week=week,
            prior_week=prior,
            command=command,
            database=False,
            observations=0,
            observations_in_week=0,
            observations_in_previous_week=0,
            detectors=summaries,
            findings=(),
            warnings=(warning,),
            lesson_flow=lesson_flow,
            escalations=escalations,
            retirements=retirements,
        )

    source = connect_read_only(state)
    trend_report: twill_trend.TrendReport | None = None
    trend_error: str | None = None
    coverage: dict[tuple[str, str], tuple[str, str]] = {}
    try:
        source.execute("BEGIN")
        observations = int(
            source.execute("SELECT count(*) FROM observation").fetchone()[0]
        )
        observations_in_week = int(
            source.execute(
                "SELECT count(*) FROM observation "
                "WHERE ts_utc >= ? AND ts_utc < ?",
                (start, end),
            ).fetchone()[0]
        )
        observations_in_previous_week = int(
            source.execute(
                "SELECT count(*) FROM observation "
                "WHERE ts_utc >= ? AND ts_utc < ?",
                (prior_start, prior_end),
            ).fetchone()[0]
        )
        dismissed = _dismissed_cluster_ids(source)
        current_results = _suppress_dismissed(
            _read_window(source, detectors, start, end), dismissed
        )
        previous_results = _suppress_dismissed(
            _read_window(source, detectors, prior_start, prior_end), dismissed
        )
        coverage = _active_coverage(source)
        try:
            retirements = twill_rules.build_rules_report(
                source,
                now=report_as_of,
            ).retirement_proposals
        except sqlite3.Error:
            # A database created before the rule corpus tables shipped still
            # produces a valid digest, just without retirement evidence.
            retirements = ()
        if detectors:
            try:
                trend_report = twill_trend.build_trend_report(
                    source,
                    detector=tuple(detector.detector_id for detector in detectors),
                    through_week=format_week(week),
                    now=report_as_of,
                )
            except sqlite3.Error as exc:
                trend_error = _one_line(
                    f"trend report unavailable: {exc}", MAX_ERROR_LENGTH
                )
    finally:
        source.close()

    summaries: list[DetectorSummary] = []
    warnings: list[str] = []
    if trend_error is not None:
        warnings.append(trend_error)
    covered_escalations = _covered_escalations(
        current_results,
        previous_results,
        coverage,
        week=format_week(week),
        reproduce=command,
    )
    covered_ids = {
        (item.detector, item.key) for item in covered_escalations
    }
    changes: list[tuple[str, str, str, Counts | None, Counts | None]] = []
    current_by_detector = {
        result.detector.full_id: result for result in current_results
    }
    previous_by_detector = {
        result.detector.full_id: result for result in previous_results
    }
    for current_result, previous_result in zip(current_results, previous_results):
        full_id = current_result.detector.full_id
        summaries.append(
            DetectorSummary(
                detector_id=current_result.detector.detector_id,
                version=current_result.detector.version,
                current_status=current_result.status,
                previous_status=previous_result.status,
                current_clusters=len(current_result.clusters),
                previous_clusters=len(previous_result.clusters),
                current_error=current_result.error,
                previous_error=previous_result.error,
            )
        )
        if current_result.error is not None and previous_result.error is not None:
            warnings.append(
                f"{full_id} failed for {format_week(prior)} and {format_week(week)}: "
                f"{current_result.error}; {previous_result.error}"
            )
        elif current_result.error is not None:
            warnings.append(
                f"{full_id} failed for {format_week(week)}: {current_result.error}"
            )
        elif previous_result.error is not None:
            warnings.append(
                f"{full_id} failed for {format_week(prior)}; "
                f"{format_week(week)} is not comparable"
            )
        if current_result.status != STATUS_OK or previous_result.status != STATUS_OK:
            continue
        keys = current_result.clusters.keys() | previous_result.clusters.keys()
        for key in keys:
            if (full_id, key) in covered_ids:
                continue
            verdict = _verdict(
                current_result.clusters.get(key),
                previous_result.clusters.get(key),
            )
            if verdict is not None:
                changes.append(
                    (
                        verdict,
                        full_id,
                        key,
                        current_result.clusters.get(key),
                        previous_result.clusters.get(key),
                    )
                )

    trend_by_cluster: dict[tuple[str, str], twill_trend.TrendFinding] = {}
    if trend_report is not None:
        full_ids = {detector.detector_id: detector.full_id for detector in detectors}
        trend_by_cluster = {
            (full_ids[finding.detector_id], finding.key): finding
            for finding in trend_report.findings
            if finding.status in twill_trend.TREND_SIGNAL_STATUSES
            and finding.detector_id in full_ids
        }
        seen = {(detector, key) for _, detector, key, _, _ in changes}
        for (detector, key), finding in sorted(trend_by_cluster.items()):
            current_result = current_by_detector.get(detector)
            previous_result = previous_by_detector.get(detector)
            if (
                current_result is None
                or current_result.status != STATUS_OK
                or key not in current_result.clusters
                or (detector, key) in seen
                or (detector, key) in covered_ids
            ):
                continue
            changes.append(
                (
                    VERDICT_TREND,
                    detector,
                    key,
                    current_result.clusters[key],
                    previous_result.clusters.get(key)
                    if previous_result is not None
                    else None,
                )
            )
            seen.add((detector, key))

    verdict_rank = {verdict: index for index, verdict in enumerate(VERDICT_ORDER)}
    current_estimates = {
        (result.detector.full_id, key): estimate
        for result in current_results
        for key, estimate in result.estimated_waste.items()
    }
    previous_estimates = {
        (result.detector.full_id, key): estimate
        for result in previous_results
        for key, estimate in result.estimated_waste.items()
    }
    findings_list: list[Finding] = []
    for verdict, detector, key, current, previous in changes:
        current_waste = current_estimates.get((detector, key))
        previous_waste = previous_estimates.get((detector, key))
        trend_finding = trend_by_cluster.get((detector, key))
        findings_list.append(
            Finding(
                n=0,
                verdict=verdict,
                detector=detector,
                key=key,
                current=current,
                previous=previous,
                week=format_week(week),
                reproduce=command,
                estimated_waste=(
                    current_waste if current is not None else previous_waste
                ),
                trend_status=(
                    trend_finding.status if trend_finding is not None else None
                ),
                trend_metric=(
                    trend_finding.signal_metric if trend_finding is not None else None
                ),
                trend_excess=(
                    trend_finding.signal_excess if trend_finding is not None else None
                ),
            )
        )
    findings_list.sort(key=lambda finding: _finding_sort_key(finding, verdict_rank))
    findings = tuple(
        replace(finding, n=index)
        for index, finding in enumerate(findings_list, start=1)
    )
    return DigestReport(
        state_dir=state,
        week=week,
        prior_week=prior,
        command=command,
        database=True,
        observations=observations,
        observations_in_week=observations_in_week,
        observations_in_previous_week=observations_in_previous_week,
        detectors=tuple(summaries),
        findings=findings,
        warnings=tuple(warnings),
        lesson_flow=lesson_flow,
        escalations=escalations,
        covered_escalations=covered_escalations,
        retirements=retirements,
        trend=trend_report,
    )


def _detector_data(detector: DetectorSummary) -> dict[str, object]:
    return {
        "detector": detector.full_id,
        "current_status": detector.current_status,
        "previous_status": detector.previous_status,
        "current_clusters": detector.current_clusters,
        "previous_clusters": detector.previous_clusters,
        "current_error": detector.current_error,
        "previous_error": detector.previous_error,
    }


def _finding_sort_key(
    finding: Finding,
    verdict_rank: dict[str, int],
) -> tuple[object, ...]:
    estimate = finding.estimated_waste
    dollars = estimate.waste_usd if estimate is not None else None
    tokens = estimate.tokens if estimate is not None else None
    known_dollars = isinstance(dollars, (int, float)) and isfinite(float(dollars))
    known_tokens = isinstance(tokens, (int, float)) and isfinite(float(tokens))
    trend_rank = {
        status: index for index, status in enumerate(TREND_ORDER)
    }
    return (
        trend_rank.get(finding.trend_status, len(TREND_ORDER)),
        verdict_rank.get(finding.verdict, len(verdict_rank)),
        0 if known_dollars else 1,
        -float(dollars) if known_dollars else 0.0,
        0 if known_tokens else 1,
        -float(tokens) if known_tokens else 0.0,
        finding.detector,
        finding.key,
    )


def _finding_waste_data(finding: Finding) -> dict[str, object]:
    estimate = finding.estimated_waste or EstimatedWaste(None, None, None, None)
    return {
        **estimate.as_dict(),
        "estimated_waste_window": (
            "current" if finding.current is not None else "previous"
        ),
    }


def render_data(report: DigestReport) -> dict[str, object]:
    start, end = week_bounds(report.week)
    prior_start, prior_end = week_bounds(report.prior_week)
    return {
        "week": report.week_id,
        "week_start": start,
        "week_end": end,
        "previous_week": report.previous_week_id,
        "previous_week_start": prior_start,
        "previous_week_end": prior_end,
        "clean": report.clean,
        "observations": report.observations,
        "observations_in_week": report.observations_in_week,
        "observations_in_previous_week": report.observations_in_previous_week,
        "detectors": [_detector_data(detector) for detector in report.detectors],
        "findings": [_finding_data(finding) for finding in report.findings],
        "trend": _trend_data(report.trend),
        "lesson_flow": report.lesson_flow.as_dict(),
        "escalations": [proposal.as_dict() for proposal in report.escalations],
        "covered_escalations": [
            proposal.as_dict() for proposal in report.covered_escalations
        ],
        "retirements": [proposal.as_dict() for proposal in report.retirements],
        "reproduction_command": report.command,
    }


def _count_text(count: tuple[int, int] | None) -> str:
    if count is None:
        return "? sessions/? events"
    return f"{count[0]} sessions/{count[1]} events"


def _waste_text(estimate: EstimatedWaste | None) -> str:
    if estimate is None or estimate.waste_usd is None:
        dollars = "unavailable"
    else:
        dollars = f"{estimate.waste_usd:.6f}"
    if estimate is None or estimate.tokens is None:
        tokens = "unavailable"
    else:
        tokens = f"{estimate.tokens:,.2f}"
    return f"estimated waste: {dollars} USD; estimated tokens: {tokens}"


def render_text(report: DigestReport) -> str:
    start, end = week_bounds(report.week)
    prior_start, prior_end = week_bounds(report.prior_week)
    lines = [
        _line("TWILL digest", report.command),
        _line(f"week: {report.week_id} ({start} to {end})", report.command),
        _line(
            f"previous week: {report.previous_week_id} "
            f"({prior_start} to {prior_end})",
            report.command,
        ),
        _line(
            f"observations: {report.observations} total, "
            f"{report.observations_in_week} current, "
            f"{report.observations_in_previous_week} previous",
            report.command,
        ),
    ]
    detector_text = ", ".join(
        f"{detector.full_id} {detector.current_status} "
        f"{detector.current_clusters}/{detector.previous_clusters}"
        for detector in report.detectors
    )
    lines.append(
        _line(
            f"detectors current/previous: {detector_text or 'none registered'}",
            report.command,
        )
    )
    flow = report.lesson_flow
    if flow.available:
        flow_text = (
            f"lesson flow (last {flow.window_days} days): "
            f"drafted {flow.drafted}, accepted {flow.accepted}, "
            f"applied {flow.applied}, resolved {flow.resolved}"
        )
        if flow.warning is not None:
            flow_text += f" — WARNING: {flow.warning}"
    else:
        flow_text = (
            f"lesson flow (last {flow.window_days} days): unavailable — "
            f"WARNING: {flow.warning}"
        )
    lines.append(_line(flow_text, report.command))
    if report.trend is None:
        trend_text = "trend: unavailable"
    else:
        trend_text = (
            f"trend: {len(report.trend.new_findings)} new, "
            f"{len(report.trend.accelerating_findings)} accelerating; "
            f"history {report.trend.history_weeks}/"
            f"{report.trend.minimum_history_weeks} week(s)"
        )
    lines.append(_line(trend_text, report.command))
    if report.trend is not None:
        for warning in report.trend.warnings:
            lines.append(_line(f"trend warning: {warning}", report.command))
    for detector in report.detectors:
        if detector.current_status != STATUS_OK:
            lines.append(
                _line(
                    f"detector skipped: {detector.full_id} {report.week_id} "
                    f"({detector.current_status})",
                    report.command,
                )
            )
        if detector.previous_status != STATUS_OK:
            lines.append(
                _line(
                    f"detector skipped: {detector.full_id} "
                    f"{report.previous_week_id} ({detector.previous_status})",
                    report.command,
                )
            )
        if detector.current_error is not None:
            lines.append(
                _line(
                    f"detector error: {detector.full_id} {report.week_id}: "
                    f"{detector.current_error}",
                    report.command,
                )
            )
        if detector.previous_error is not None:
            lines.append(
                _line(
                    f"detector error: {detector.full_id} {report.previous_week_id}: "
                    f"{detector.previous_error}",
                    report.command,
                )
            )
    def render_findings(label: str, selected: Sequence[Finding]) -> None:
        if not selected:
            return
        lines.append(_line(f"{label}: {len(selected)}", report.command))
        for finding in selected:
            current = (
                (finding.current[0], finding.current[1])
                if finding.current is not None
                else None
            )
            previous = (
                (finding.previous[0], finding.previous[1])
                if finding.previous is not None
                else None
            )
            lines.append(
                _line(
                    f"- {finding.n} {finding.verdict} {finding.detector} "
                    f"{_display_key(finding.key)} "
                    f"{_count_text(previous)}->{_count_text(current)}; "
                    f"{_waste_text(finding.estimated_waste)}"
                    + (
                        f"; trend={finding.trend_status}"
                        if finding.trend_status is not None
                        else ""
                    ),
                    report.command,
                )
            )

    for trend_status in TREND_ORDER:
        render_findings(
            f"trend {trend_status}",
            tuple(
                finding
                for finding in report.findings
                if finding.trend_status == trend_status
            ),
        )
    for verdict in VERDICT_ORDER:
        render_findings(
            verdict,
            tuple(
                finding
                for finding in report.findings
                if finding.trend_status is None and finding.verdict == verdict
            ),
        )
    if report.escalations:
        lines.append(_line(f"escalation proposals: {len(report.escalations)}", report.command))
        for proposal in report.escalations:
            lines.append(
                _line(
                    f"- {proposal.lesson_id} {proposal.detector_id} "
                    f"{_display_key(proposal.key)}: propose "
                    f"{proposal.current_layer}->{proposal.next_layer}; "
                    f"{proposal.sessions} sessions/{proposal.events} events "
                    f"vs {proposal.baseline_sessions} sessions/"
                    f"{proposal.baseline_events} events after 21 days",
                    report.command,
                )
            )
    if report.covered_escalations:
        lines.append(
            _line(
                f"covered recurrence escalations: {len(report.covered_escalations)}",
                report.command,
            )
        )
        for escalation in report.covered_escalations:
            current = escalation.current
            previous = escalation.previous
            comparison = (
                f" vs {previous[0]} sessions/{previous[1]} events"
                if previous is not None
                else " with no prior-week comparison"
            )
            lines.append(
                _line(
                    f"- {escalation.n} {escalation.detector} "
                    f"{_display_key(escalation.key)} covered by "
                    f"{escalation.covered_by}: "
                    f"{current[0]} sessions/{current[1]} events{comparison}; "
                    "route to escalation, not Explain",
                    report.command,
                )
            )
    if report.retirements:
        lines.append(
            _line(f"retirement proposals: {len(report.retirements)}", report.command)
        )
        for proposal in report.retirements:
            lines.append(
                _line(
                    f"- {proposal.path} [{proposal.layer}]: last read "
                    f"{proposal.last_read or 'never'}; last occurrence "
                    f"{proposal.last_occurrence or 'none'}; "
                    f"{proposal.covered_clusters} covered cluster(s); "
                    "removal is a human edit in the owning layer",
                    report.command,
                )
            )
    ran = ", ".join(detector.full_id for detector in report.detectors) or "none registered"
    if report.clean:
        clean = f"clean week: no findings; detectors ran: {ran}"
    elif not report.database:
        clean = "clean week: no; no state database; no detector ran"
    else:
        failed = [
            detector.full_id
            for detector in report.detectors
            if detector.current_status != STATUS_OK
            or detector.previous_status != STATUS_OK
        ]
        if failed:
            clean = f"clean week: no; detector failures: {', '.join(failed)}"
        else:
            total_signals = len(report.findings) + len(report.covered_escalations)
            clean = f"clean week: no; {total_signals} finding(s)"
    lines.append(_line(clean, report.command))
    return "\n".join(lines) + "\n"


def _validate_digest_text(text: str) -> None:
    """Refuse digest text whose lines break the committed-artifact invariants.

    Plan §3 and §8.3: every digest line reaching the committed artifact is
    post-redaction and at most 240 characters.  ``render_text`` guarantees
    this by construction; this check makes the write boundary itself fail
    closed, so a caller that bypasses the renderer cannot commit an
    unbounded or unredacted line.
    """

    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > MAX_LINE_LENGTH:
            raise ValidationError(
                f"digest line {number} exceeds {MAX_LINE_LENGTH} characters",
                "render the report with render_text, which bounds every line, "
                "before committing it",
            )
        if redact_text(line) != line:
            raise ValidationError(
                f"digest line {number} contains redacted content",
                "render the report with render_text, which redacts every line, "
                "before committing it",
            )


def write_digest_file(text: str, artifacts_root: Path, week: Week) -> Path:
    _validate_digest_text(text)
    directory = Path(artifacts_root).expanduser().resolve() / "digests"
    if directory.is_symlink():
        raise ValueError("digest directory may not be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True, mode=DIGEST_DIR_MODE)
    os.chmod(directory, DIGEST_DIR_MODE)
    path = directory / f"{format_week(week)}.txt"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("digest path is not a regular file")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory,
        prefix=f".{format_week(week)}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, DIGEST_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, DIGEST_FILE_MODE)
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return path
