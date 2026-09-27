"""Build the on-demand, pull-only pre-flight brief (plan §6.1, §14).

The brief deliberately reads the derived state and accepted lesson files as
they stand. It does not refresh detectors or coverage, write a status record,
or alter the lesson lifecycle. A target is matched against both the session's
``cwd`` and the transcript file's ``launch_dir`` so callers may ask about
either a repository or the directory from which sessions were launched.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from twill_lessons import LessonRecord
from twill_redactor import redact_text


TARGET_COLUMNS = ("cwd", "launch_dir")


@dataclass(frozen=True)
class BriefLesson:
    """One accepted lesson whose evidence belongs to the requested target."""

    lesson_id: str
    summary: str
    state: str
    detector: str
    key: str
    sessions: int
    events: int
    first_seen: str | None
    last_seen: str | None
    matched_sessions: int
    matched_events: int
    matched_by: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.lesson_id,
            "summary": self.summary,
            "state": self.state,
            "detector": self.detector,
            "key": self.key,
            "sessions": self.sessions,
            "events": self.events,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "matched_sessions": self.matched_sessions,
            "matched_events": self.matched_events,
            "matched_by": list(self.matched_by),
        }


@dataclass(frozen=True)
class BriefCluster:
    """One open cluster with evidence in the requested target."""

    detector_id: str
    key: str
    window_days: int
    sessions: int
    events: int
    first_seen: str
    last_seen: str
    score: float
    covered_by: str | None
    matched_sessions: int
    matched_events: int
    matched_by: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "detector_id": self.detector_id,
            "key": self.key,
            "window_days": self.window_days,
            "sessions": self.sessions,
            "events": self.events,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "score": self.score,
            "covered_by": self.covered_by,
            "matched_sessions": self.matched_sessions,
            "matched_events": self.matched_events,
            "matched_by": list(self.matched_by),
        }


@dataclass(frozen=True)
class BriefReport:
    """The complete read-only answer for one target."""

    target: str
    matched_by: tuple[str, ...]
    accepted_lessons: tuple[BriefLesson, ...]
    open_clusters: tuple[BriefCluster, ...]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "matched_by": list(self.matched_by),
            "accepted_lessons": [item.as_dict() for item in self.accepted_lessons],
            "open_clusters": [item.as_dict() for item in self.open_clusters],
        }


def normalize_target(value: str | Path) -> tuple[str, tuple[str, ...]]:
    """Return a safe display target and equivalent path spellings to query."""

    text = str(value).strip()
    if not text:
        raise ValueError("brief target must not be empty")
    path = Path(text).expanduser()
    candidates = {str(path), str(path.resolve())}
    safe_candidates = tuple(sorted({redact_text(item) for item in candidates if item}))
    if not safe_candidates:
        raise ValueError("brief target must not be empty")
    return redact_text(str(path)), safe_candidates


def _placeholders(values: Sequence[str]) -> str:
    return ", ".join("?" for _ in values)


def _scope_clause(
    candidates: Sequence[str], alias: str = "o"
) -> tuple[str, tuple[str, ...]]:
    if not candidates:
        raise ValueError("brief target has no queryable path")
    placeholders = _placeholders(candidates)
    return (
        f"({alias}.cwd IN ({placeholders}) OR "
        f"{alias}.launch_dir IN ({placeholders}))",
        tuple(candidates) + tuple(candidates),
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _matching_rows(
    connection: sqlite3.Connection,
    candidates: Sequence[str],
    *,
    session_ids: Sequence[str] | None = None,
    detector_id: str | None = None,
    key: str | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """Return matching ``(session_id, timestamp, field)`` rows.

    ``cluster_session`` is optional for compatibility with a pre-Phase-2
    state database. Lesson evidence still works without it; open clusters
    simply have no target attribution until detector runs populate the table.
    """

    scope, parameters = _scope_clause(candidates)
    joins = ""
    predicates = [scope]
    values: list[object] = list(parameters)
    if session_ids is not None:
        if not session_ids:
            return ()
        placeholders = _placeholders(session_ids)
        predicates.append(f"o.session_id IN ({placeholders})")
        values.extend(session_ids)
    if detector_id is not None or key is not None:
        if detector_id is None or key is None:
            raise ValueError("detector_id and key must be supplied together")
        if not _table_exists(connection, "cluster_session"):
            return ()
        joins = " JOIN cluster_session AS cs ON cs.session_id = o.session_id "
        predicates.extend(("cs.detector_id = ?", "cs.key = ?"))
        values.extend((detector_id, key))
    query = (
        "SELECT o.session_id, o.ts_utc, CASE WHEN o.cwd IN ("
        + _placeholders(candidates)
        + ") THEN 'cwd' ELSE 'launch_dir' END AS matched_by "
        "FROM observation AS o"
        + joins
        + " WHERE "
        + " AND ".join(predicates)
        + " ORDER BY o.ts_utc ASC, o.obs_id ASC"
    )
    rows = connection.execute(
        query,
        tuple(candidates) + tuple(values),
    ).fetchall()
    return tuple(
        (str(session_id), str(ts_utc), str(matched_by))
        for session_id, ts_utc, matched_by in rows
    )


def _match_summary(
    rows: Iterable[tuple[str, str, str]],
) -> tuple[int, int, str | None, str | None, tuple[str, ...]]:
    materialized = tuple(rows)
    sessions = {row[0] for row in materialized}
    present_fields = {row[2] for row in materialized}
    fields = tuple(field for field in TARGET_COLUMNS if field in present_fields)
    timestamps = [row[1] for row in materialized]
    return (
        len(sessions),
        len(materialized),
        min(timestamps) if timestamps else None,
        max(timestamps) if timestamps else None,
        fields,
    )


def _lesson_session_ids(record: LessonRecord) -> tuple[str, ...]:
    values = record.evidence.get("session_ids", ())
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values if isinstance(value, str) and value)


def _base_detector(detector: str) -> str:
    return detector.split("@", 1)[0]


def _accepted_lessons(
    connection: sqlite3.Connection,
    records: Iterable[LessonRecord],
    candidates: Sequence[str],
    top_k: int,
) -> tuple[BriefLesson, ...]:
    result: list[BriefLesson] = []
    for record in records:
        # The brief's source is explicitly accepted lessons. Drafts and
        # terminal lessons are intentionally not presented as current advice.
        if record.state != "accepted":
            continue
        session_ids = _lesson_session_ids(record)
        rows = _matching_rows(connection, candidates, session_ids=session_ids)
        if not rows:
            # Evidence IDs are authoritative for historical lessons. If the
            # current detector attribution is available, use it as a fallback
            # for a lesson whose evidence rows were pruned or rebuilt.
            rows = _matching_rows(
                connection,
                candidates,
                detector_id=_base_detector(record.detector),
                key=record.key,
            )
        matched_sessions, matched_events, first_seen, last_seen, matched_by = _match_summary(rows)
        if not rows:
            continue
        evidence = record.evidence
        result.append(
            BriefLesson(
                lesson_id=record.lesson_id,
                summary=record.summary,
                state=record.state,
                detector=record.detector,
                key=record.key,
                sessions=int(evidence["sessions"]),
                events=int(evidence["events"]),
                first_seen=first_seen or str(evidence["first_seen"]),
                last_seen=last_seen,
                matched_sessions=matched_sessions,
                matched_events=matched_events,
                matched_by=matched_by,
            )
        )
    result.sort(
        key=lambda item: (-item.matched_sessions, -item.matched_events, item.lesson_id)
    )
    return tuple(result[:top_k])


def _open_clusters(
    connection: sqlite3.Connection,
    candidates: Sequence[str],
    top_k: int,
) -> tuple[BriefCluster, ...]:
    rows = connection.execute(
        "SELECT detector_id, key, window_days, sessions, events, first_seen, "
        "last_seen, score, covered_by FROM cluster WHERE state = 'open' "
        "ORDER BY score DESC, sessions DESC, events DESC, last_seen DESC, "
        "detector_id ASC, key ASC"
    ).fetchall()
    result: list[BriefCluster] = []
    for (
        detector_id,
        key,
        window_days,
        sessions,
        events,
        first_seen,
        last_seen,
        score,
        covered_by,
    ) in rows:
        matches = _matching_rows(
            connection,
            candidates,
            detector_id=str(detector_id),
            key=str(key),
        )
        if not matches:
            continue
        matched_sessions, matched_events, _, _, matched_by = _match_summary(matches)
        result.append(
            BriefCluster(
                detector_id=str(detector_id),
                key=str(key),
                window_days=int(window_days),
                sessions=int(sessions),
                events=int(events),
                first_seen=str(first_seen),
                last_seen=str(last_seen),
                score=float(score),
                covered_by=None if covered_by is None else str(covered_by),
                matched_sessions=matched_sessions,
                matched_events=matched_events,
                matched_by=matched_by,
            )
        )
        if len(result) >= top_k:
            break
    return tuple(result)


def build_brief(
    connection: sqlite3.Connection | None,
    target: str | Path,
    records: Iterable[LessonRecord] = (),
    *,
    top_k: int = 10,
) -> BriefReport:
    """Build a target brief using only read-only inputs.

    ``connection`` may be ``None`` for a first run with no state database;
    that is an honest empty brief rather than a request to create state.
    """

    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    display_target, candidates = normalize_target(target)
    if connection is None:
        return BriefReport(
            display_target,
            (),
            (),
            (),
            ("state database does not exist; no clusters are available",),
        )
    lessons = _accepted_lessons(connection, records, candidates, top_k)
    clusters = _open_clusters(connection, candidates, top_k)
    matched_by = tuple(
        field
        for field in TARGET_COLUMNS
        if any(field in item.matched_by for item in (*lessons, *clusters))
    )
    return BriefReport(display_target, matched_by, lessons, clusters)


build_report = build_brief


__all__ = [
    "BriefCluster",
    "BriefLesson",
    "BriefReport",
    "build_brief",
    "build_report",
    "normalize_target",
]
