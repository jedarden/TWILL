"""Render the inverted rule earnings and decay report (plan Phase 3).

The ranker answers which clusters are not covered by a rule.  This module
answers the inverse question: for every live rule document, which current
clusters it covers, how their weekly recurrence changed, and whether the
document has gone unread long enough to be a deletion candidate.

The report is deliberately read-only.  Rule indexing and detector refresh are
separate mutating stages; ``twill rules`` only consumes their durable rows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


RECURRENCE_UP = "up"
RECURRENCE_DOWN = "down"
RECURRENCE_FLAT = "flat"
RECURRENCE_UNKNOWN = "unknown"
RECURRENCE_DIRECTIONS = frozenset(
    {RECURRENCE_UP, RECURRENCE_DOWN, RECURRENCE_FLAT, RECURRENCE_UNKNOWN}
)


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clock(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = _timestamp(value)
    if parsed is None:
        raise ValueError("now must be an ISO-8601 timestamp")
    return parsed


def _positive_days(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("unread_days must be a positive integer")
    return value


def _iso(value: datetime) -> str:
    return value.isoformat()


def _age_days(as_of: datetime, value: str | None) -> float | None:
    parsed = _timestamp(value)
    if parsed is None:
        return None
    return max(0.0, (as_of - parsed).total_seconds() / 86400.0)


def _week_start(value: str) -> date | None:
    if not isinstance(value, str) or len(value) != 8:
        return None
    try:
        year = int(value[:4])
        week = int(value[6:])
        return date.fromisocalendar(year, week, 1)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class WeeklyRecurrence:
    """The latest adjacent weekly counts for one covered cluster."""

    direction: str
    current_week: str | None
    previous_week: str | None
    current_sessions: int | None
    previous_sessions: int | None
    current_events: int | None
    previous_events: int | None

    @property
    def sessions_delta(self) -> int | None:
        if self.current_sessions is None or self.previous_sessions is None:
            return None
        return self.current_sessions - self.previous_sessions

    @property
    def events_delta(self) -> int | None:
        if self.current_events is None or self.previous_events is None:
            return None
        return self.current_events - self.previous_events

    @property
    def comparable(self) -> bool:
        return self.direction != RECURRENCE_UNKNOWN

    def as_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "trend": self.direction,
            "comparable": self.comparable,
            "current": {
                "week": self.current_week,
                "sessions": self.current_sessions,
                "events": self.current_events,
            }
            if self.current_week is not None
            else None,
            "previous": {
                "week": self.previous_week,
                "sessions": self.previous_sessions,
                "events": self.previous_events,
            }
            if self.previous_week is not None
            else None,
            "sessions_delta": self.sessions_delta,
            "events_delta": self.events_delta,
        }


@dataclass(frozen=True)
class CoveredCluster:
    """One current cluster covered by a rule document."""

    detector_id: str
    key: str
    window_days: int
    sessions: int
    events: int
    first_seen: str
    last_seen: str
    recurrence: WeeklyRecurrence

    def as_dict(self) -> dict[str, object]:
        return {
            "detector_id": self.detector_id,
            "key": self.key,
            "window_days": self.window_days,
            "sessions": self.sessions,
            "events": self.events,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "direction": self.recurrence.direction,
            "trend": self.recurrence.direction,
            "recurrence": self.recurrence.as_dict(),
        }


@dataclass(frozen=True)
class RuleEntry:
    """One live rule document and the evidence attached to it."""

    path: str
    layer: str
    sha: str
    indexed_at: str
    last_read: str | None
    unread_since: str
    unread_age_days: float | None
    unread: bool
    deletion_candidate: bool
    clusters: tuple[CoveredCluster, ...]

    @property
    def covered(self) -> bool:
        return bool(self.clusters)

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "layer": self.layer,
            "sha": self.sha,
            "indexed_at": self.indexed_at,
            "last_read": self.last_read,
            # Keep the storage name available to callers that join this
            # report directly to rule_doc rows.
            "last_read_by_agent": self.last_read,
            "unread_since": self.unread_since,
            "unread_age_days": self.unread_age_days,
            "unread": self.unread,
            "covered": self.covered,
            "deletion_candidate": self.deletion_candidate,
            "clusters": [cluster.as_dict() for cluster in self.clusters],
        }


@dataclass(frozen=True)
class RulesReport:
    """The complete rule earnings and decay report."""

    as_of: str
    unread_days: int
    database: bool
    rules: tuple[RuleEntry, ...]
    stale_rules: tuple[dict[str, object], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def deletion_candidates(self) -> tuple[RuleEntry, ...]:
        return tuple(rule for rule in self.rules if rule.deletion_candidate)

    @property
    def covered_rules(self) -> tuple[RuleEntry, ...]:
        return tuple(rule for rule in self.rules if rule.covered)

    @property
    def unread_rules(self) -> tuple[RuleEntry, ...]:
        return tuple(rule for rule in self.rules if rule.unread)

    def as_dict(self) -> dict[str, object]:
        candidates = self.deletion_candidates
        return {
            "as_of": self.as_of,
            "unread_days": self.unread_days,
            "database": self.database,
            "rules": [rule.as_dict() for rule in self.rules],
            "deletion_candidates": [rule.as_dict() for rule in candidates],
            "deletion_candidate_paths": [rule.path for rule in candidates],
            "stale_rules": list(self.stale_rules),
            "summary": {
                "rules": len(self.rules),
                "covered_rules": len(self.covered_rules),
                "unread_rules": len(self.unread_rules),
                "deletion_candidates": len(candidates),
                "stale_rules": len(self.stale_rules),
            },
        }


def _direction(
    current_sessions: int,
    previous_sessions: int,
    current_events: int,
    previous_events: int,
) -> str:
    """Classify recurrence by event volume, then session breadth on ties."""

    if current_events > previous_events:
        return RECURRENCE_UP
    if current_events < previous_events:
        return RECURRENCE_DOWN
    if current_sessions > previous_sessions:
        return RECURRENCE_UP
    if current_sessions < previous_sessions:
        return RECURRENCE_DOWN
    return RECURRENCE_FLAT


def _recurrences(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], WeeklyRecurrence]:
    rows = connection.execute(
        "SELECT detector_id, key, week, sessions, events FROM cluster_week "
        "ORDER BY detector_id, key, week"
    ).fetchall()
    series: dict[tuple[str, str], list[tuple[str, date, int, int]]] = {}
    for detector_id, key, week, sessions, events in rows:
        label = str(week)
        start = _week_start(label)
        if start is None:
            # Detector aggregation validates ISO weeks, but a report should
            # remain readable if a manually repaired state DB contains a bad
            # row.  It contributes no comparison rather than aborting all
            # rule reporting.
            continue
        series.setdefault((str(detector_id), str(key)), []).append(
            (label, start, int(sessions), int(events))
        )

    result: dict[tuple[str, str], WeeklyRecurrence] = {}
    for identity, values in series.items():
        values.sort(key=lambda row: row[1])
        current_label, current_start, current_sessions, current_events = values[-1]
        previous = values[-2] if len(values) > 1 else None
        if previous is None or previous[1] != current_start - timedelta(days=7):
            result[identity] = WeeklyRecurrence(
                RECURRENCE_UNKNOWN,
                current_label,
                None,
                current_sessions,
                None,
                current_events,
                None,
            )
            continue
        previous_label, _, previous_sessions, previous_events = previous
        result[identity] = WeeklyRecurrence(
            _direction(
                current_sessions,
                previous_sessions,
                current_events,
                previous_events,
            ),
            current_label,
            previous_label,
            current_sessions,
            previous_sessions,
            current_events,
            previous_events,
        )
    return result


def _last_reads_by_sha(
    connection: sqlite3.Connection,
) -> dict[str, str]:
    """Combine indexed read markers and observed file reads by content hash."""

    rows = connection.execute(
        "SELECT path, sha, last_read_by_agent FROM rule_doc"
    ).fetchall()
    by_path = {str(path): str(sha) for path, sha, _ in rows}
    latest: dict[str, datetime] = {}
    rendered: dict[str, str] = {}

    def add(sha: str, value: object) -> None:
        parsed = _timestamp(value)
        if parsed is None:
            return
        if sha not in latest or parsed > latest[sha]:
            latest[sha] = parsed
            rendered[sha] = _iso(parsed)

    for _, sha, last_read in rows:
        add(str(sha), last_read)

    observations = connection.execute(
        "SELECT path, ts_utc FROM observation "
        "WHERE kind = 'file_read' AND path IS NOT NULL"
    ).fetchall()
    for path, timestamp in observations:
        path_text = str(path).strip()
        sha = by_path.get(path_text)
        if sha is not None:
            add(sha, timestamp)
    return rendered


def _stale_rules(connection: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        "SELECT path, layer, sha, indexed_at, last_read_by_agent "
        "FROM rule_doc WHERE stale <> 0 ORDER BY path"
    ).fetchall()
    return tuple(
        {
            "path": str(path),
            "layer": str(layer),
            "sha": str(sha),
            "indexed_at": str(indexed_at),
            "last_read": None if last_read is None else str(last_read),
            "stale": True,
        }
        for path, layer, sha, indexed_at, last_read in rows
    )


def build_rules_report(
    connection: sqlite3.Connection,
    *,
    unread_days: int = 90,
    now: str | datetime | None = None,
) -> RulesReport:
    """Build a deterministic report from the persisted rule and cluster rows.

    ``connection`` may be a read-only SQLite connection.  The function never
    indexes files, refreshes detectors, updates read markers, or writes any
    state.  A rule's read time follows its content hash, matching D-09 and the
    rule corpus move/duplicate semantics.
    """

    days = _positive_days(unread_days)
    as_of = _clock(now)
    cutoff = as_of - timedelta(days=days)
    last_reads = _last_reads_by_sha(connection)
    recurrences = _recurrences(connection)

    cluster_rows = connection.execute(
        "SELECT detector_id, key, window_days, sessions, events, first_seen, "
        "last_seen, covered_by FROM cluster "
        "WHERE covered_by IS NOT NULL ORDER BY covered_by, detector_id, key"
    ).fetchall()
    clusters_by_rule: dict[str, list[CoveredCluster]] = {}
    for (
        detector_id,
        key,
        window_days,
        sessions,
        events,
        first_seen,
        last_seen,
        covered_by,
    ) in cluster_rows:
        identity = (str(detector_id), str(key))
        clusters_by_rule.setdefault(str(covered_by), []).append(
            CoveredCluster(
                detector_id=str(detector_id),
                key=str(key),
                window_days=int(window_days),
                sessions=int(sessions),
                events=int(events),
                first_seen=str(first_seen),
                last_seen=str(last_seen),
                recurrence=recurrences.get(
                    identity,
                    WeeklyRecurrence(
                        RECURRENCE_UNKNOWN,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ),
                ),
            )
        )

    rows = connection.execute(
        "SELECT path, layer, sha, indexed_at FROM rule_doc "
        "WHERE stale = 0 ORDER BY path"
    ).fetchall()
    rules: list[RuleEntry] = []
    for path, layer, sha, indexed_at in rows:
        path_text = str(path)
        sha_text = str(sha)
        indexed_text = str(indexed_at)
        last_read = last_reads.get(sha_text)
        unread_since = last_read or indexed_text
        unread_since_at = _timestamp(unread_since)
        unread = last_read is None or unread_since_at is None or unread_since_at < cutoff
        unread_age = _age_days(as_of, unread_since)
        clusters = tuple(clusters_by_rule.get(path_text, ()))
        # A never-read document only becomes a deletion candidate once it has
        # existed for the requested period.  A read document uses its last
        # read as the beginning of its current decay interval.
        old_enough = unread_since_at is not None and unread_since_at <= cutoff
        rules.append(
            RuleEntry(
                path=path_text,
                layer=str(layer),
                sha=sha_text,
                indexed_at=indexed_text,
                last_read=last_read,
                unread_since=unread_since,
                unread_age_days=unread_age,
                unread=unread,
                deletion_candidate=bool(unread and old_enough and not clusters),
                clusters=clusters,
            )
        )

    stale = _stale_rules(connection)
    warnings: tuple[str, ...] = ()
    if stale:
        warnings = (
            f"{len(stale)} stale rule document(s) are excluded from live coverage and deletion candidates",
        )
    return RulesReport(
        as_of=_iso(as_of),
        unread_days=days,
        database=True,
        rules=tuple(rules),
        stale_rules=stale,
        warnings=warnings,
    )


def empty_rules_report(
    *,
    unread_days: int = 90,
    now: str | datetime | None = None,
) -> RulesReport:
    """Return the successful empty result used when no state DB exists."""

    days = _positive_days(unread_days)
    return RulesReport(
        as_of=_iso(_clock(now)),
        unread_days=days,
        database=False,
        rules=(),
        warnings=("no state database exists; run twill ingest first",),
    )


# Descriptive aliases for callers that use the component name from the plan.
render_rules_report = build_rules_report
rules_report = build_rules_report


def render_text(report: RulesReport, *, deletion_candidates: bool = False) -> str:
    """Render the concise operator-facing report."""

    lines = [
        "TWILL rules",
        f"as of: {report.as_of}",
        f"unread threshold: {report.unread_days} day(s)",
    ]
    if not report.database:
        lines.append("no rule documents")
        return "\n".join(lines)
    if not report.rules:
        lines.append("no live rule documents")
    else:
        for rule in report.rules:
            last_read = rule.last_read or "never"
            status = "unread" if rule.unread else "read"
            lines.append(
                f"- {rule.path} [{rule.layer}]: {status}; last read {last_read}; "
                f"clusters {len(rule.clusters)}"
            )
            for cluster in rule.clusters:
                recurrence = cluster.recurrence
                comparison = recurrence.direction
                if recurrence.current_week is not None and recurrence.previous_week is not None:
                    comparison = (
                        f"{comparison} ({recurrence.previous_week} → {recurrence.current_week}; "
                        f"events {recurrence.previous_events} → {recurrence.current_events}; "
                        f"sessions {recurrence.previous_sessions} → {recurrence.current_sessions})"
                    )
                lines.append(
                    f"  - {cluster.detector_id} {cluster.key}: {comparison}"
                )
    if deletion_candidates:
        candidates = report.deletion_candidates
        lines.append("deletion candidates:")
        if candidates:
            lines.extend(f"- {rule.path}" for rule in candidates)
        else:
            lines.append("- none")
    return "\n".join(lines)


__all__ = [
    "RECURRENCE_DOWN",
    "RECURRENCE_FLAT",
    "RECURRENCE_UNKNOWN",
    "RECURRENCE_UP",
    "CoveredCluster",
    "RuleEntry",
    "RulesReport",
    "WeeklyRecurrence",
    "build_rules_report",
    "empty_rules_report",
    "render_rules_report",
    "render_text",
    "rules_report",
]
