"""Read-only observational follow-up for applied learning records.

A completed interaction is a source-native or history-confirmed terminal reply,
not proof that the requested task succeeded. Absence of a detector finding is
never treated as successful completion or as evidence of a lesson's utility.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from twill_autonomy import ANALYZER_VERSION, DETECTOR_VERSION, FAMILIES
from twill_interactions import (ORIGIN_ASSISTANT_TEXT, TERMINAL_COMPLETED,
                                TERMINAL_INTERRUPTED, TERMINAL_NONE)


SCHEMA = "twill-learning-impact/v1"
MAX_RECORDS = 32
MAX_SESSIONS = 1000
MAX_EPISODES = 4000
_ID = re.compile(r"LC-[0-9a-f]{16}\Z")
_REQUIRED = {
    "interaction": {"session_key", "session_id", "ts_utc", "ordinal", "actor", "origin", "sidechain",
                    "terminal_state", "truncated", "truncated_fields"},
    "interaction_summary": {"session_key", "omitted_over_cap", "truncated_fields"},
    "execution_episode": {"episode_id", "session_key", "start_ordinal", "end_ordinal", "authority_state",
                          "context_state", "advice_only", "input_sha"},
}


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    # Table names are module constants, never source or instruction text.
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _within(directory: str, roots: list[str]) -> bool:
    return any(directory == root or directory.startswith(root.rstrip("/") + "/") for root in roots)


def _recurrence(connection: sqlite3.Connection, record: dict, episodes: list[tuple],
                sessions: set[str], beginning: str, end: str) -> dict:
    detector = record.get("source", {}).get("detector")
    unknown = {"status": "unknown", "events": None, "reason": "source detector has no supported follow-up projection"}
    if detector in FAMILIES:
        required = {"episode_id", "family", "input_sha", "detector_version", "analyzer_version", "disposition", "candidate_id"}
        if not required <= _columns(connection, "autonomy_assessment"):
            return dict(unknown, reason="autonomy assessment projection unavailable")
        if not {"candidate_id"} <= _columns(connection, "autonomy_adjudication"):
            return dict(unknown, reason="adjudication coverage unavailable")
        supported = 0
        uncertain = 0
        for episode in episodes:
            episode_id, _, _, _, _, _, _, input_sha = episode
            assessments = connection.execute(
                    "SELECT candidate_id,input_sha,detector_version,analyzer_version,disposition "
                    "FROM autonomy_assessment WHERE episode_id=? AND family=? LIMIT 65", (episode_id, detector)).fetchall()
            if not assessments or len(assessments) > 64:
                uncertain += 1
                continue
            for candidate_id, stored_sha, version, analyzer, disposition in assessments:
                override = connection.execute("SELECT 1 FROM autonomy_adjudication WHERE candidate_id=?",
                                               (candidate_id,)).fetchone()
                if (override or stored_sha != input_sha or version != DETECTOR_VERSION or
                        analyzer != ANALYZER_VERSION or disposition == "needs_review"):
                    uncertain += 1
                elif disposition == "supported":
                    supported += 1
        return {"status": "observed" if not uncertain else "partial", "events": supported,
                "unknown_assessments": uncertain, "detector_version": DETECTOR_VERSION,
                "reason": "current source-hash-matched inferred findings; external adjudication journals are not reconciled here"}
    if detector != "D-02" or not {"obs_id", "signature", "session_id", "ts_utc", "kind"} <= _columns(connection, "observation"):
        return unknown
    refs = record.get("source_evidence", {}).get("references", [])
    signatures = set()
    for ref in refs[:64]:
        identifier = ref.get("observation_id")
        if type(identifier) is int:
            row = connection.execute("SELECT signature FROM observation WHERE obs_id=?", (identifier,)).fetchone()
            if row and row[0]:
                signatures.add(row[0])
    if len(signatures) != 1:
        return dict(unknown, reason="source signature is unavailable or ambiguous")
    signature = signatures.pop()
    hits = set()
    for session_key in sessions:
        rows = connection.execute(
            "SELECT obs_id FROM observation WHERE signature=? AND kind IN ('run_failed','tool_error') "
            "AND julianday(ts_utc)>=julianday(?) AND julianday(ts_utc)<julianday(?) "
            "AND session_id IN (SELECT session_id FROM interaction WHERE session_key=?) LIMIT 1001",
            (signature, beginning, end, session_key)).fetchall()
        if len(rows) > 1000:
            return {"status": "partial", "events": None, "reason": "recurrence bound exceeded"}
        hits.update(row[0] for row in rows)
    return {"status": "observed", "events": len(hits), "reason": "matching recorded D-02 source signature; absence is not success"}


def _lesson(connection: sqlite3.Connection, record: dict, scope: dict, end: str) -> dict:
    beginning = _timestamp(record.get("application", {}).get("at"))
    result = {"lesson_id": record["id"], "state": record["state"], "window_start": beginning,
              "window_end": end, "status": "unknown", "task_success": None,
              "continued_exposure_verified": False}
    if beginning is None or beginning >= end:
        return dict(result, reason="application timestamp unavailable or follow-up window has not started")
    roots = scope.get("launchdirs", [])
    if scope.get("global") is not True and (not roots or not isinstance(roots, list) or
            any(not isinstance(root, str) or not root.startswith("/") for root in roots)):
        return dict(result, reason="scope has no explicit global or source launch-directory mapping")
    if scope.get("global") is not True and not {"session_id", "cwd", "launch_dir"} <= _columns(connection, "observation"):
        return dict(result, reason="source directory projection unavailable")
    rows = connection.execute(
        "SELECT DISTINCT session_key FROM interaction WHERE julianday(ts_utc)>=julianday(?) "
        "AND julianday(ts_utc)<julianday(?) ORDER BY session_key LIMIT ?", (beginning, end, MAX_SESSIONS + 1)).fetchall()
    if len(rows) > MAX_SESSIONS:
        return dict(result, reason="observed session bound exceeded; narrow the follow-up window")
    sessions = set()
    scope_unknown = 0
    for (key,) in rows:
        if scope.get("global") is True:
            sessions.add(key)
            continue
        directories = connection.execute(
            "SELECT DISTINCT COALESCE(NULLIF(cwd,''),launch_dir) FROM observation WHERE session_id IN "
            "(SELECT session_id FROM interaction WHERE session_key=?) LIMIT 17", (key,)).fetchall()
        if not directories or len(directories) > 16 or any(not value[0] for value in directories):
            scope_unknown += 1
        elif all(_within(value[0], roots) for value in directories):
            sessions.add(key)
    counts = {"observed_sessions": len(sessions), "scope_unknown_sessions": scope_unknown,
              "eligible_sessions": 0, "eligible_episodes": 0, "opportunities_unknown": 0,
              "verified_terminal_completions": 0, "terminal_interrupted": 0, "terminal_unknown": 0,
              "coverage_unknown_sessions": 0, "omitted_interactions": 0, "truncated_fields": 0}
    eligible_sessions = set()
    eligible_episodes = []
    episode_count = 0
    for key in sessions:
        coverage = connection.execute(
            "SELECT omitted_over_cap,truncated_fields FROM interaction_summary WHERE session_key=?", (key,)).fetchone()
        row_truncation = connection.execute(
            "SELECT COALESCE(sum(CASE WHEN truncated<>0 OR truncated_fields<>0 THEN 1 ELSE 0 END),0) "
            "FROM interaction WHERE session_key=? AND julianday(ts_utc)>=julianday(?) AND julianday(ts_utc)<julianday(?)",
            (key, beginning, end)).fetchone()[0]
        covered = coverage is not None and not (coverage[0] or coverage[1] or row_truncation)
        counts["coverage_unknown_sessions"] += int(not covered)
        counts["omitted_interactions"] += coverage[0] if coverage else 0
        counts["truncated_fields"] += coverage[1] if coverage else row_truncation
        episodes = connection.execute(
            "SELECT e.episode_id,e.session_key,e.start_ordinal,e.end_ordinal,e.authority_state,"
            "e.context_state,e.advice_only,e.input_sha FROM execution_episode e "
            "JOIN interaction i ON i.session_key=e.session_key AND i.ordinal=e.start_ordinal "
            "WHERE e.session_key=? AND julianday(i.ts_utc)>=julianday(?) AND julianday(i.ts_utc)<julianday(?) "
            "ORDER BY e.start_ordinal LIMIT ?", (key, beginning, end, MAX_EPISODES + 1)).fetchall()
        episode_count += len(episodes)
        if episode_count > MAX_EPISODES:
            return dict(result, reason="episode bound exceeded; narrow the follow-up window")
        if not episodes:
            counts["opportunities_unknown"] += 1
        for episode in episodes:
            _, _, first, last, authority, context, advice, _ = episode
            if advice or authority in {"withheld", "revoked"}:
                continue
            if authority != "authorized" or context != "known":
                counts["opportunities_unknown"] += 1
                continue
            counts["eligible_episodes"] += 1
            eligible_sessions.add(key)
            eligible_episodes.append(episode)
            terminal = connection.execute(
                "SELECT terminal_state,truncated,truncated_fields FROM interaction "
                "WHERE session_key=? AND ordinal BETWEEN ? AND ? AND actor='assistant' AND origin=? "
                "AND sidechain=0 AND terminal_state<>? AND julianday(ts_utc)<julianday(?) "
                "ORDER BY ordinal DESC LIMIT 1", (key, first, last, ORIGIN_ASSISTANT_TEXT, TERMINAL_NONE, end)).fetchone()
            if not covered or not terminal or terminal[1] or terminal[2]:
                counts["terminal_unknown"] += 1
            elif terminal[0] == TERMINAL_COMPLETED:
                counts["verified_terminal_completions"] += 1
            elif terminal[0] == TERMINAL_INTERRUPTED:
                counts["terminal_interrupted"] += 1
            else:
                counts["terminal_unknown"] += 1
    counts["eligible_sessions"] = len(eligible_sessions)
    recurrence = _recurrence(connection, record, eligible_episodes, sessions, beginning, end)
    status = "partial" if counts["coverage_unknown_sessions"] or scope_unknown or counts["opportunities_unknown"] else "observed"
    return dict(result, status=status, **counts, recurrence=recurrence,
                reason="source telemetry after application; terminal replies do not prove task correctness or lesson benefit")


def snapshot(records, *, state_dir: Path, scopes: dict) -> dict:
    """Return bounded aggregate telemetry; never write state or source artifacts."""
    now = datetime.now(timezone.utc).isoformat()
    selected = [record for record in records if isinstance(record, dict) and
                record.get("state") in {"active", "invalidated"} and record.get("application") and
                _ID.fullmatch(str(record.get("id", "")))]
    result = {"schema": SCHEMA, "at": now, "status": "observed", "lessons": [],
              "attribution": "observational; no causal attribution", "task_success": None,
              "completion_definition": "source-native or history-confirmed terminal reply, not verified task success",
              "records_over_bound": max(0, len(selected) - MAX_RECORDS)}
    if not selected:
        return dict(result, status="no_applied_lessons")
    path = Path(state_dir) / "twill.db"
    if not path.is_file() or path.is_symlink():
        return dict(result, status="unknown", reason="source store unavailable")
    connection = None
    try:
        connection = sqlite3.connect(f"file:{quote(str(path.resolve()), safe='/')}?mode=ro", uri=True, timeout=1)
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 3
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        missing = [name for name, columns in _REQUIRED.items() if not columns <= _columns(connection, name)]
        if missing:
            return dict(result, status="unknown", reason="source interaction or episode schema unavailable")
        connection.execute("BEGIN")
        for record in selected[:MAX_RECORDS]:
            result["lessons"].append(_lesson(connection, record, scopes.get(record.get("scope"), {}), now))
        if any(lesson["status"] != "observed" for lesson in result["lessons"]) or result["records_over_bound"]:
            result["status"] = "partial"
        return result
    except sqlite3.Error:
        return dict(result, status="unknown", reason="source query unavailable or observational budget exceeded")
    finally:
        if connection is not None:
            connection.close()
