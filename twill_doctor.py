"""Health checks and bounded recovery actions for the Phase 1 TWILL pipeline.

The ordinary doctor command is a diagnostic surface: it opens the derived
database without creating or migrating it, evaluates each Phase 1 signal
independently, and aggregates the most severe result.  Explicit recovery flags
are separate mutating operations and are run by the CLI under its state lock.
"""

from __future__ import annotations

import math
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import twill_detectors
import twill_rulecorpus
import twill_schema
from twill_redactor import Redactor, redact_text
from twill_status import read_status


HEALTHY = "healthy"
DEGRADED = "degraded"
BROKEN = "broken"
HEALTH_STATUSES = (HEALTHY, DEGRADED, BROKEN)

EXIT_HEALTHY = 0
EXIT_DEGRADED = 1
EXIT_BROKEN = 2

# EC-13 uses two thresholds: ingest refuses to start below the floor, while
# doctor warns earlier so an operator has room to recover before writes fail.
FREE_DISK_INGEST_FLOOR_BYTES = 2 * 1024**3
FREE_DISK_WARN_BYTES = 5 * 1024**3
TIMER_INTERVALS = {"ingest": 3600.0}
DEAD_MAN_WINDOW_SECONDS = 24 * 3600.0
# §13.3's detector self-test replays the registry over a fixture with the
# detectors' default trailing window.
SELFTEST_WINDOW_DAYS = 30
# What the self-test fixture is known to hold for the shipped catalog: a
# detector listed here that emits fewer clusters than this against the fixture
# has broken semantics even though its SQL still parses and runs — the
# runtime analogue of §10.1's "fixture-driven test with a known expected
# count", and the alarm for a detector that quietly stops selecting anything.
# A detector absent from the map (a custom or test registry) is only required
# to parse and run; the suite pins the map to the shipped registry's ids so a
# new catalog entry cannot quietly opt out of the expectation.
SELFTEST_EXPECTED_CLUSTERS = {
    "D-01": 1,
    "D-02": 1,
    "D-03": 1,
    "D-05": 1,
    "D-06": 1,
    "D-07": 1,
    "D-08": 1,
    "D-09": 1,
}
REQUIRED_TABLES = (
    "cluster",
    "cluster_week",
    "cursor",
    "measurement",
    "meta",
    "observation",
    "parse_shape",
    "rule_doc",
    "rule_fts",
    "session_usage",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    message: str
    details: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "message": redact_text(self.message),
            "details": _safe_value(self.details),
        }


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]

    @property
    def status(self) -> str:
        if any(check.status == BROKEN for check in self.checks):
            return BROKEN
        if any(check.status == DEGRADED for check in self.checks):
            return DEGRADED
        return HEALTHY

    @property
    def exit_code(self) -> int:
        if self.status == BROKEN:
            return EXIT_BROKEN
        if self.status == DEGRADED:
            return EXIT_DEGRADED
        return EXIT_HEALTHY

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(
            f"{check.name}: {redact_text(check.message)}"
            for check in self.checks
            if check.status != HEALTHY
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "checks": [check.as_dict() for check in self.checks],
        }


def _safe_value(value: object) -> object:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, Path):
        return redact_text(str(value))
    return value


def _result(
    name: str,
    status: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> CheckResult:
    if status not in HEALTH_STATUSES:
        raise ValueError(f"invalid health status: {status}")
    return CheckResult(name, status, message, details or {})


def _exception_text(exc: Exception) -> str:
    return redact_text(str(exc) or exc.__class__.__name__)


_REDACTION_RESCAN_FIELDS = (
    # ``transcript_event.text`` is the legacy source row from which the
    # walking-skeleton detector rebuilds observations.  It is bounded and
    # redacted at persistence just like ``observation.excerpt``.
    ("transcript_event", "event_id", "text"),
    ("observation", "obs_id", "excerpt"),
)


def rescan_redaction(
    connection: sqlite3.Connection,
    *,
    content_fences: Iterable[str] = (),
) -> dict[str, int]:
    """Re-apply the current excerpt redactor to stored derived text.

    This is the documented recovery for a redactor regression (§8.2).  The
    operation is deliberately limited to bounded excerpt fields: identifiers,
    signatures, and detector keys have their own semantics and are not
    rewritten as a side effect.  The source ``transcript_event.text`` mirror
    is included so a later ingest resume cannot regenerate a stale excerpt.

    Rows are updated in one transaction.  Missing legacy tables are skipped so
    a database containing only the v1 corpus schema can still be repaired.
    ``None`` stays ``None``; a nullable excerpt is not turned into an empty
    string merely because the recovery ran.
    """

    redactor = Redactor(content_fences)
    rows_scanned = 0
    rows_changed = 0
    fields_changed = 0
    with connection:
        for table, key_column, excerpt_column in _REDACTION_RESCAN_FIELDS:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if table_exists is None:
                continue
            rows = connection.execute(
                f"SELECT {key_column}, {excerpt_column} FROM {table}"
            ).fetchall()
            for key, stored_excerpt in rows:
                rows_scanned += 1
                if stored_excerpt is None:
                    continue
                redacted_excerpt = redactor.redact_excerpt(stored_excerpt)
                if redacted_excerpt == stored_excerpt:
                    continue
                connection.execute(
                    f"UPDATE {table} SET {excerpt_column} = ? WHERE {key_column} = ?",
                    (redacted_excerpt, key),
                )
                rows_changed += 1
                fields_changed += 1
    return {
        "rows_scanned": rows_scanned,
        "rows_changed": rows_changed,
        "fields_changed": fields_changed,
    }


def _newest_schema_version() -> int:
    return max(
        [twill_schema.BASELINE_VERSION]
        + [migration.version for migration in twill_schema.MIGRATIONS]
    )


def _connect_for_doctor(state_dir: Path) -> sqlite3.Connection:
    db_path = twill_schema.state_db_path(state_dir)
    if not db_path.is_file():
        return twill_schema.connect_read_only(state_dir)
    wal_path = db_path.with_name(db_path.name + "-wal")
    shm_path = db_path.with_name(db_path.name + "-shm")
    if wal_path.is_file() and shm_path.is_file():
        return twill_schema.connect_read_only(state_dir)
    uri = f"file:{quote(str(db_path), safe='/')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        twill_schema._verify_observation_shape(connection, db_path)
    except BaseException:
        connection.close()
        raise
    return connection


def _check_database_integrity(
    connection: sqlite3.Connection | None,
    open_error: str | None,
) -> CheckResult:
    if connection is None:
        return _result(
            "db_integrity",
            BROKEN,
            f"database unavailable: {open_error or 'could not open state database'}",
            {"error": open_error or "could not open state database"},
        )
    try:
        rows = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
    except Exception as exc:
        return _result(
            "db_integrity",
            BROKEN,
            f"SQLite integrity check failed: {_exception_text(exc)}",
            {"error": _exception_text(exc)},
        )
    if rows == ("ok",):
        return _result("db_integrity", HEALTHY, "SQLite integrity check passed")
    details = {"errors": [redact_text(str(row)) for row in rows]}
    return _result(
        "db_integrity",
        BROKEN,
        "SQLite integrity check reported errors",
        details,
    )


def _check_database_schema(
    connection: sqlite3.Connection | None,
    open_error: str | None,
) -> CheckResult:
    if connection is None:
        return _result(
            "db_schema",
            BROKEN,
            f"database unavailable: {open_error or 'could not open state database'}",
            {"error": open_error or "could not open state database"},
        )
    expected = _newest_schema_version()
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = ?",
            (twill_schema.SCHEMA_VERSION_KEY,),
        ).fetchone()
    except Exception as exc:
        return _result(
            "db_schema",
            BROKEN,
            f"schema version cannot be read: {_exception_text(exc)}",
            {"error": _exception_text(exc)},
        )
    if row is None:
        return _result(
            "db_schema",
            BROKEN,
            f"schema version is missing; expected {expected}",
            {"expected": expected},
        )
    raw_version = row[0]
    if isinstance(raw_version, bool):
        return _result(
            "db_schema",
            BROKEN,
            "schema version is not an integer",
            {"expected": expected, "actual": redact_text(str(raw_version))},
        )
    try:
        actual = int(raw_version)
    except (TypeError, ValueError):
        return _result(
            "db_schema",
            BROKEN,
            "schema version is not an integer",
            {"expected": expected, "actual": redact_text(str(raw_version))},
        )
    if actual < expected:
        return _result(
            "db_schema",
            BROKEN,
            f"schema version {actual} is behind the release version {expected}",
            {"expected": expected, "actual": actual},
        )
    try:
        required_tables = [*REQUIRED_TABLES]
        if expected >= 2:
            required_tables.append("detector_run")
        if expected >= 3:
            required_tables.append("cluster_session")
        for table in required_tables:
            connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
    except Exception as exc:
        return _result(
            "db_schema",
            BROKEN,
            f"schema tables are missing or unreadable: {_exception_text(exc)}",
            {"expected": expected, "actual": actual, "error": _exception_text(exc)},
        )
    if actual > expected:
        message = f"schema version {actual} is newer than release version {expected}"
    else:
        message = f"schema version {actual} matches the release"
    return _result(
        "db_schema",
        HEALTHY,
        message,
        {"expected": expected, "actual": actual},
    )


def _check_cursor_health(
    connection: sqlite3.Connection | None,
    open_error: str | None,
) -> CheckResult:
    if connection is None:
        return _result(
            "cursor_health",
            BROKEN,
            f"database unavailable: {open_error or 'could not open state database'}",
            {"error": open_error or "could not open state database"},
        )
    try:
        rows = connection.execute(
            "SELECT path, parse_errors, path_missing FROM cursor"
        ).fetchall()
    except Exception as exc:
        return _result(
            "cursor_health",
            BROKEN,
            f"cursor health cannot be read: {_exception_text(exc)}",
            {"error": _exception_text(exc)},
        )
    parse_errors: list[dict[str, object]] = []
    missing_paths: list[str] = []
    for row_index, (path, error_count, path_missing) in enumerate(rows):
        if (
            not isinstance(path, str)
            or isinstance(error_count, bool)
            or not isinstance(error_count, int)
            or error_count < 0
            or isinstance(path_missing, bool)
            or not isinstance(path_missing, int)
            or path_missing < 0
        ):
            return _result(
                "cursor_health",
                BROKEN,
                f"cursor row {row_index} has invalid health fields",
                {"row": row_index},
            )
        safe_path = redact_text(path)
        if error_count > 0:
            parse_errors.append({"path": safe_path, "parse_errors": error_count})
        if path_missing != 0:
            missing_paths.append(safe_path)
    parse_errors.sort(key=lambda item: str(item["path"]))
    missing_paths.sort()
    details = {
        "parse_error_files": parse_errors,
        "missing_paths": missing_paths,
        "parse_error_file_count": len(parse_errors),
        "missing_path_count": len(missing_paths),
    }
    if not parse_errors and not missing_paths:
        return _result("cursor_health", HEALTHY, "no cursor anomalies", details)
    return _result(
        "cursor_health",
        DEGRADED,
        f"{len(parse_errors)} cursor file(s) have parse errors; "
        f"{len(missing_paths)} path(s) are missing",
        details,
    )


def _check_rule_corpus(
    connection: sqlite3.Connection | None,
    open_error: str | None,
) -> CheckResult:
    """Report rule rows whose indexed content or path is no longer current.

    The indexer marks vanished paths during a corpus run, but doctor must not
    depend on that run having happened: a file can disappear after indexing,
    and a file can change without the rank timer running again.  This check
    therefore reads every stored path directly and never mutates ``rule_doc``
    or ``rule_fts``.
    """

    if connection is None:
        return _result(
            "rule_corpus",
            BROKEN,
            f"database unavailable: {open_error or 'could not open state database'}",
            {"error": open_error or "could not open state database"},
        )
    try:
        rows = connection.execute(
            "SELECT path, sha, stale FROM rule_doc ORDER BY path"
        ).fetchall()
    except Exception as exc:
        return _result(
            "rule_corpus",
            BROKEN,
            f"rule corpus cannot be read: {_exception_text(exc)}",
            {"error": _exception_text(exc)},
        )

    hash_mismatches: list[dict[str, object]] = []
    vanished_paths: list[str] = []
    stale_rows: list[str] = []
    unreadable_paths: list[dict[str, str]] = []
    for row_index, row in enumerate(rows):
        if len(row) != 3:
            return _result(
                "rule_corpus",
                BROKEN,
                f"rule_doc row {row_index} has an invalid shape",
                {"row": row_index},
            )
        path, indexed_sha, stale = row
        if (
            not isinstance(path, str)
            or not isinstance(indexed_sha, str)
            or isinstance(stale, bool)
            or not isinstance(stale, int)
            or stale < 0
        ):
            return _result(
                "rule_corpus",
                BROKEN,
                f"rule_doc row {row_index} has invalid staleness fields",
                {"row": row_index},
            )

        safe_path = redact_text(path)
        if stale != 0:
            stale_rows.append(safe_path)
        rule_path = Path(path)
        if not rule_path.is_file():
            vanished_paths.append(safe_path)
            continue
        try:
            current_sha = twill_rulecorpus.content_sha(rule_path.read_bytes())
        except OSError as exc:
            unreadable_paths.append(
                {"path": safe_path, "error": _exception_text(exc)}
            )
            continue
        if current_sha != indexed_sha:
            hash_mismatches.append(
                {
                    "path": safe_path,
                    "indexed_sha": indexed_sha,
                    "current_sha": current_sha,
                }
            )

    hash_mismatches.sort(key=lambda item: str(item["path"]))
    vanished_paths = sorted(set(vanished_paths))
    stale_rows = sorted(set(stale_rows))
    unreadable_paths.sort(key=lambda item: str(item["path"]))
    details = {
        "document_count": len(rows),
        "hash_mismatches": hash_mismatches,
        "hash_mismatch_count": len(hash_mismatches),
        "vanished_paths": vanished_paths,
        "vanished_path_count": len(vanished_paths),
        "stale_rows": stale_rows,
        "stale_row_count": len(stale_rows),
        "unreadable_paths": unreadable_paths,
        "unreadable_path_count": len(unreadable_paths),
    }
    if not hash_mismatches and not vanished_paths and not stale_rows and not unreadable_paths:
        return _result("rule_corpus", HEALTHY, "rule corpus is fresh", details)

    issues: list[str] = []
    if hash_mismatches:
        issues.append(f"{len(hash_mismatches)} indexed hash mismatch(es)")
    if vanished_paths:
        issues.append(f"{len(vanished_paths)} rule path(s) vanished")
    if stale_rows:
        issues.append(f"{len(stale_rows)} stale row(s)")
    if unreadable_paths:
        issues.append(f"{len(unreadable_paths)} unreadable path(s)")
    return _result(
        "rule_corpus",
        DEGRADED,
        "rule corpus coverage needs attention: " + "; ".join(issues),
        details,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return _as_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _check_timer_freshness(
    state_dir: Path,
    now: datetime,
    intervals: Mapping[str, float],
) -> CheckResult:
    if not intervals:
        return _result("timer_freshness", HEALTHY, "no Phase 1 timers configured")
    for stage, interval in intervals.items():
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(float(interval))
            or interval <= 0
        ):
            return _result(
                "timer_freshness",
                BROKEN,
                f"timer interval for {stage} is invalid",
                {"stage": redact_text(str(stage))},
            )
    try:
        payload = read_status(state_dir)
    except Exception as exc:
        return _result(
            "timer_freshness",
            BROKEN,
            f"status cannot be read: {_exception_text(exc)}",
            {"error": _exception_text(exc)},
        )
    data = payload.get("data") if isinstance(payload, Mapping) else None
    stages = data.get("stages") if isinstance(data, Mapping) else None
    if not isinstance(stages, dict):
        return _result(
            "timer_freshness",
            BROKEN,
            "status stage data is invalid",
        )
    issues: list[dict[str, object]] = []
    stage_details: dict[str, object] = {}
    for stage, interval in intervals.items():
        record = stages.get(stage)
        stage_interval = float(interval)
        if not isinstance(record, dict):
            issues.append({"stage": stage, "reason": "missing"})
            stage_details[stage] = {"interval_seconds": stage_interval}
            continue
        last_success = record.get("last_success")
        parsed = _parse_timestamp(last_success)
        if parsed is None:
            issues.append({"stage": stage, "reason": "no successful run"})
            stage_details[stage] = {
                "interval_seconds": stage_interval,
                "last_success": last_success,
            }
            continue
        age = max(0.0, (now - parsed).total_seconds())
        stage_details[stage] = {
            "interval_seconds": stage_interval,
            "age_seconds": round(age, 3),
            "last_success": last_success,
        }
        if age > stage_interval * 3:
            issues.append(
                {
                    "stage": stage,
                    "reason": "stale",
                    "age_seconds": round(age, 3),
                }
            )
    details = {"stages": stage_details, "issues": issues}
    if not issues:
        return _result("timer_freshness", HEALTHY, "all Phase 1 timers are fresh", details)
    return _result(
        "timer_freshness",
        DEGRADED,
        f"{len(issues)} timer check(s) need attention",
        details,
    )


def _mtime_timestamp(value: object) -> datetime | None:
    """Convert a cursor's nanosecond mtime to an aware UTC timestamp."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1_000_000_000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _check_dead_man_switch(
    connection: sqlite3.Connection | None,
    open_error: str | None,
    now: datetime,
    window_seconds: float = DEAD_MAN_WINDOW_SECONDS,
) -> CheckResult:
    """Fail when recent transcript arrivals produced no observations.

    ``cursor`` is the durable record of files that entered the ingest path.  A
    row's first-seen time catches new session files; its stored mtime catches a
    known file that received an append or rewrite.  We deliberately scope the
    observation query to the sessions represented by those rows: observations
    from an older, healthy session must not mask a parser that stopped emitting
    rows for newly arriving files.

    Real ingested state has the ``session`` table, whose ``ingested_at`` value
    is the strongest timestamp for this check.  Minimal/pre-session databases
    remain diagnosable by falling back to observation timestamps instead of
    making the doctor unable to run during an additive upgrade.
    """

    if connection is None:
        return _result(
            "dead_man_switch",
            BROKEN,
            f"database unavailable: {open_error or 'could not open state database'}",
            {"error": open_error or "could not open state database"},
        )
    if (
        isinstance(window_seconds, bool)
        or not isinstance(window_seconds, (int, float))
        or not math.isfinite(float(window_seconds))
        or window_seconds <= 0
    ):
        return _result(
            "dead_man_switch",
            BROKEN,
            "dead-man's switch window is invalid",
            {"window_seconds": redact_text(str(window_seconds))},
        )

    cutoff = now - timedelta(seconds=float(window_seconds))
    try:
        cursor_rows = connection.execute(
            "SELECT path, source, session_id, first_seen, mtime_ns "
            "FROM cursor WHERE path_missing = 0"
        ).fetchall()
    except Exception as exc:
        return _result(
            "dead_man_switch",
            BROKEN,
            f"transcript arrival history cannot be read: {_exception_text(exc)}",
            {"error": _exception_text(exc)},
        )

    arrivals: list[dict[str, object]] = []
    for row_index, row in enumerate(cursor_rows):
        if len(row) != 5:
            return _result(
                "dead_man_switch",
                BROKEN,
                f"cursor row {row_index} has an invalid arrival shape",
                {"row": row_index},
            )
        path, source, session_id, first_seen, mtime_ns = row
        first_seen_at = _parse_timestamp(first_seen)
        mtime_at = _mtime_timestamp(mtime_ns)
        if (
            not isinstance(path, str)
            or not isinstance(source, str)
            or not isinstance(session_id, str)
            or first_seen_at is None
            or mtime_at is None
        ):
            return _result(
                "dead_man_switch",
                BROKEN,
                f"cursor row {row_index} has invalid transcript arrival fields",
                {"row": row_index},
            )
        arrival_at = max(first_seen_at, mtime_at)
        if arrival_at >= cutoff and arrival_at <= now:
            arrivals.append(
                {
                    "path": redact_text(path),
                    "source": redact_text(source),
                    "session_id": redact_text(session_id),
                    "arrived_at": arrival_at.isoformat(),
                }
            )

    details: dict[str, object] = {
        "window_seconds": float(window_seconds),
        "window_start": cutoff.isoformat(),
        "recent_file_count": len(arrivals),
        "recent_files": sorted(
            arrivals,
            key=lambda item: (str(item["arrived_at"]), str(item["path"])),
        ),
        "observations_ingested": 0,
    }
    if not arrivals:
        return _result(
            "dead_man_switch",
            HEALTHY,
            "no transcript files arrived in the last 24 hours",
            details,
        )

    session_ids = tuple(sorted({str(item["session_id"]) for item in arrivals}))
    placeholders = ", ".join("?" for _ in session_ids)
    try:
        has_session_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session'"
        ).fetchone()
        if has_session_table:
            observation_row = connection.execute(
                "SELECT count(*) FROM observation AS o "
                "JOIN session AS s ON s.session_id = o.session_id "
                f"WHERE o.session_id IN ({placeholders}) AND s.ingested_at >= ?",
                (*session_ids, cutoff.isoformat()),
            ).fetchone()
        else:
            observation_row = connection.execute(
                "SELECT count(*) FROM observation "
                f"WHERE session_id IN ({placeholders}) AND ts_utc >= ?",
                (*session_ids, cutoff.isoformat()),
            ).fetchone()
    except Exception as exc:
        return _result(
            "dead_man_switch",
            BROKEN,
            f"recent observations cannot be read: {_exception_text(exc)}",
            {**details, "error": _exception_text(exc)},
        )
    observations = int(observation_row[0]) if observation_row else 0
    details["observations_ingested"] = observations
    if observations:
        return _result(
            "dead_man_switch",
            HEALTHY,
            f"{len(arrivals)} recent transcript file(s) produced {observations} observation(s)",
            details,
        )
    return _result(
        "dead_man_switch",
        BROKEN,
        "dead-man's switch: transcript files arrived but zero observations were ingested in the last 24 hours",
        details,
    )


def _seed_selftest_observation(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    session_id: str,
    days_ago: float,
    kind: str,
    **columns: object,
) -> None:
    observed_at = (now - timedelta(days=days_ago)).isoformat()
    row: dict[str, object] = {
        "session_id": session_id,
        "ts_utc": observed_at,
        "ts_local": observed_at,
        "kind": kind,
        **columns,
    }
    names = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    connection.execute(
        f"INSERT INTO observation({names}) VALUES ({placeholders})",
        tuple(row.values()),
    )


def _seed_selftest_fixture(connection: sqlite3.Connection, now: datetime) -> None:
    """Seed the self-test corpus: one known-true finding per shipped detector.

    Every timestamp is relative to ``now`` so the fixture is deterministic
    under any clock.  Rows exist to exercise a detector's real join and
    grouping paths, not to look like production traffic: each block below
    names the detector it is known to make fire, and
    :data:`SELFTEST_EXPECTED_CLUSTERS` is the contract those blocks keep.
    """

    # D-01 (missing binary): command-not-found for one program across two
    # sessions, inside the window.
    for session_id, days_ago in (("selftest-a", 1.0), ("selftest-b", 2.0)):
        _seed_selftest_observation(
            connection,
            now=now,
            session_id=session_id,
            days_ago=days_ago,
            kind="run_failed",
            program="sqlite3",
            command="sqlite3 --dump",
            signature="sqlite3: command not found",
            sig_hash="selftest-sha-command-not-found",
        )
    # D-02 (recurring signature): one normalized error signature recurring
    # across two sessions, independent of the command-not-found rows.
    for session_id in ("selftest-a", "selftest-b"):
        _seed_selftest_observation(
            connection,
            now=now,
            session_id=session_id,
            days_ago=1.0,
            kind="tool_error",
            signature="TypeError: unsupported operand type(s) for +",
            sig_hash="selftest-sha-typeerror",
        )
    # D-03 (retry loop): one command failing three times in a single session
    # with no later success to cancel it.
    for _ in range(3):
        _seed_selftest_observation(
            connection,
            now=now,
            session_id="selftest-c",
            days_ago=1.0,
            kind="run_failed",
            command="cargo build --release",
            signature="error: could not compile twill",
            sig_hash="selftest-sha-cargo",
        )
    # D-05 (rejected tool call): a rejection immediately followed by the
    # corrective user turn, with nothing between.
    _seed_selftest_observation(
        connection,
        now=now,
        session_id="selftest-d",
        days_ago=1.0,
        kind="tool_rejected",
        tool="Bash",
    )
    _seed_selftest_observation(
        connection,
        now=now,
        session_id="selftest-d",
        days_ago=1.0,
        kind="user_turn_after_correction",
        excerpt="use the file reader instead of a raw grep",
    )
    # D-06 (interrupt correction): an interrupt immediately followed by a
    # corrective user turn, in its own session so it cannot pair with D-05's.
    _seed_selftest_observation(
        connection,
        now=now,
        session_id="selftest-e",
        days_ago=1.0,
        kind="interrupt",
    )
    _seed_selftest_observation(
        connection,
        now=now,
        session_id="selftest-e",
        days_ago=1.0,
        kind="user_turn_after_correction",
        excerpt="stop and summarise the plan instead",
    )
    # D-07 (rediscovery): one never-edited path read on two days across two
    # sessions, and one exploratory command run in two sessions.
    for session_id, days_ago in (("selftest-a", 2.0), ("selftest-b", 1.0)):
        _seed_selftest_observation(
            connection,
            now=now,
            session_id=session_id,
            days_ago=days_ago,
            kind="file_read",
            path="/docs/selftest-arch.md",
        )
        _seed_selftest_observation(
            connection,
            now=now,
            session_id=session_id,
            days_ago=1.0,
            kind="run",
            command="grep -r selftest /docs",
        )
    # D-08 and D-09 (rule corpus): one live rule document that names the
    # missing binary and a host that has gone silent, and has never been
    # read.  The retired host's only observation sits outside the window so
    # the silent-host path has a real EXCEPT to compute.
    connection.execute(
        "INSERT INTO rule_doc(path, layer, sha, indexed_at, last_read_by_agent, stale) "
        "VALUES ('/rules/selftest.md', 'memory', 'selftest-sha-rule', ?, NULL, 0)",
        ((now - timedelta(days=1)).isoformat(),),
    )
    connection.execute(
        "INSERT INTO rule_fts(text, path) VALUES (?, '/rules/selftest.md')",
        ("run sqlite3 for local dumps; the retired-host mirror is gone",),
    )
    _seed_selftest_observation(
        connection,
        now=now,
        session_id="selftest-f",
        days_ago=SELFTEST_WINDOW_DAYS + 10.0,
        kind="file_read",
        path="/mirror/selftest.log",
        host="retired-host",
    )


def _selftest_fixture(now: datetime) -> sqlite3.Connection:
    """Build the in-memory fixture the self-test replays the registry over.

    The schema is exactly what a fresh state database would carry, so a
    detector whose SQL no longer matches the shipped tables or columns fails
    here before a digest is built from it.  ``:memory:`` keeps the doctor
    surface read-only: no file, no lock, no state-directory side effects.
    """

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(twill_schema.V1_SCHEMA)
        _seed_selftest_fixture(connection, now)
    except BaseException:
        connection.close()
        raise
    return connection


def _selftest_detector(
    connection: sqlite3.Connection, detector: twill_detectors.Detector, now: datetime
) -> int:
    """Run one detector's whole query family against the fixture.

    Every SQL variant a digest, backtest or weekly series can execute is
    executed: the cluster query, the session-hit query (whose counts must
    agree with the cluster rows), the week-hit query for each emitted key,
    and the weekly-count query.  Returns the number of clusters emitted.
    """

    window_start = (now - timedelta(days=SELFTEST_WINDOW_DAYS)).isoformat()
    parameters = {
        "window_start_utc": window_start,
        "window_days": SELFTEST_WINDOW_DAYS,
    }
    clusters = twill_detectors.read_clusters(
        connection,
        detector,
        window_start_utc=window_start,
        window_days=SELFTEST_WINDOW_DAYS,
    )
    if detector.session_hits_sql is not None:
        cursor = connection.execute(detector.session_hits_sql, parameters)
        twill_detectors._collect_session_hits(detector, cursor, clusters)
    if detector.week_hits_sql is not None:
        for key in clusters:
            twill_detectors.read_cluster_weeks(
                connection,
                detector,
                key,
                window_start_utc=window_start,
                window_days=SELFTEST_WINDOW_DAYS,
            )
    if detector.weekly_hits_sql is not None:
        twill_detectors.read_weekly_counts(
            connection,
            detector,
            window_start_utc=window_start,
            window_days=SELFTEST_WINDOW_DAYS,
            window_end_utc=now.isoformat(),
        )
    return len(clusters)


def _check_detector_self_test(
    registry: Sequence[twill_detectors.Detector], now: datetime
) -> CheckResult:
    """§13.3's detector self-test: the registry must run before a digest.

    The weekly digest replays every registered detector's SQL over an
    in-memory window, so a detector that cannot parse against the shipped
    schema or breaks its emission contract would surface there as an errored
    line in a report a human skims.  This check runs the same replay over a
    fixed corpus up front, where a failure names the detector and blocks
    trust in everything downstream of the registry.  A failure is BROKEN,
    not degraded: §14 classes a detector self-test failure as a validation
    failure, and a digest built from a broken registry is not partially
    trustworthy.
    """

    detectors = tuple(registry)
    if not detectors:
        return _result(
            "detector_self_test",
            HEALTHY,
            "no detectors registered",
        )
    try:
        connection = _selftest_fixture(now)
    except Exception as exc:
        return _result(
            "detector_self_test",
            BROKEN,
            f"self-test fixture cannot be built: {_exception_text(exc)}",
            {"error": _exception_text(exc)},
        )
    outcomes: dict[str, dict[str, object]] = {}
    failed: list[str] = []
    try:
        for detector in detectors:
            entry: dict[str, object]
            try:
                clusters = _selftest_detector(connection, detector, now)
                expected = SELFTEST_EXPECTED_CLUSTERS.get(detector.detector_id)
                if expected is not None and clusters < expected:
                    entry = {
                        "status": "error",
                        "clusters": clusters,
                        "error": (
                            f"emitted {clusters} cluster(s); the self-test "
                            f"fixture is known to hold at least {expected}"
                        ),
                    }
                else:
                    entry = {"status": "ok", "clusters": clusters}
            except (sqlite3.Error, twill_detectors.DetectorContractError) as exc:
                entry = {
                    "status": "error",
                    "clusters": 0,
                    "error": _exception_text(exc),
                }
            outcomes[detector.full_id] = entry
            if entry["status"] != "ok":
                failed.append(detector.full_id)
    finally:
        connection.close()
    details: dict[str, object] = {
        "window_days": SELFTEST_WINDOW_DAYS,
        "detectors": outcomes,
    }
    if failed:
        return _result(
            "detector_self_test",
            BROKEN,
            f"{len(failed)} of {len(detectors)} registered detector(s) "
            f"failed the fixture self-test: {', '.join(sorted(failed))}",
            details,
        )
    return _result(
        "detector_self_test",
        HEALTHY,
        f"all {len(detectors)} registered detector(s) passed the fixture self-test",
        details,
    )


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return candidate
        candidate = parent
    return candidate


def _read_free_disk(
    state_dir: Path,
    disk_usage: Callable[[Path], Any] | None,
) -> tuple[Path, int]:
    target = _nearest_existing_path(state_dir)
    usage_function = disk_usage or shutil.disk_usage
    usage = usage_function(target)
    return target, int(usage.free)


def _check_disk_space(
    state_dir: Path,
    disk_usage: Callable[[Path], Any] | None,
) -> CheckResult:
    try:
        target, free = _read_free_disk(state_dir, disk_usage)
    except Exception as exc:
        return _result(
            "disk_space",
            BROKEN,
            f"free disk cannot be read: {_exception_text(exc)}",
            {"error": _exception_text(exc)},
        )
    details = {
        "path": redact_text(str(target)),
        "free_bytes": free,
        "threshold_bytes": FREE_DISK_WARN_BYTES,
    }
    if free < FREE_DISK_WARN_BYTES:
        return _result(
            "disk_space",
            DEGRADED,
            f"free disk is below {FREE_DISK_WARN_BYTES} bytes",
            details,
        )
    return _result(
        "disk_space",
        HEALTHY,
        f"free disk is at least {FREE_DISK_WARN_BYTES} bytes",
        details,
    )


def _check_performance_budgets(state_dir: Path) -> CheckResult:
    """Report the latest ingest, detect, and DB-size budgets."""

    try:
        payload = read_status(state_dir)
    except Exception as exc:
        return _result(
            "performance_budgets",
            BROKEN,
            f"performance status cannot be read: {_exception_text(exc)}",
            {"error": _exception_text(exc)},
        )

    stages = payload.get("data", {}).get("stages", {})
    if not isinstance(stages, Mapping):
        return _result(
            "performance_budgets",
            HEALTHY,
            "no performance budget run has been recorded",
            {"measured": False},
        )

    measurements: dict[str, Mapping[str, object]] = {}
    misses: list[tuple[str, list[str]]] = []
    for stage_name in ("ingest", "detect", "prune"):
        stage = stages.get(stage_name)
        if not isinstance(stage, Mapping):
            continue
        # A failed attempt is kept beside the last successful stage so the
        # operator can still see when the stage last succeeded.  It takes
        # priority over that older successful measurement for health.
        performance = stage.get("last_failure", {}).get("performance")
        if not isinstance(performance, Mapping):
            performance = stage.get("performance")
        if not isinstance(performance, Mapping):
            continue
        measurements[stage_name] = performance
        stage_misses = performance.get("misses")
        if isinstance(stage_misses, list) and stage_misses:
            misses.append((stage_name, [str(miss) for miss in stage_misses]))

    if not measurements:
        return _result(
            "performance_budgets",
            HEALTHY,
            "no performance measurement has been recorded",
            {"measured": False},
        )

    if misses:
        stage_name, stage_misses = misses[0]
        details = dict(measurements[stage_name])
        details["stage"] = stage_name
        details["measurements"] = {
            name: dict(value) for name, value in measurements.items()
        }
        return _result(
            "performance_budgets",
            BROKEN,
            f"{stage_name} aborted after a performance budget miss: "
            + "; ".join(stage_misses),
            details,
        )
    if len(measurements) == 1:
        details = dict(next(iter(measurements.values())))
    else:
        details = {
            "measurements": {
                name: dict(value) for name, value in measurements.items()
            }
        }
    details["measured"] = True
    return _result(
        "performance_budgets",
        HEALTHY,
        "performance budgets passed",
        details,
    )


def _check_ingest_performance(state_dir: Path) -> CheckResult:
    """Backward-compatible name for the aggregate performance check."""

    return _check_performance_budgets(state_dir)


def check_ingest_disk_space(
    state_dir: Path,
    disk_usage: Callable[[Path], Any] | None = None,
) -> CheckResult:
    """Check EC-13's hard free-space floor before an ingest can write.

    This check does not create the state directory or database.  A missing
    state directory is measured via its nearest existing parent, and failure
    to read the filesystem is treated as a blocked ingest rather than as
    permission to proceed blindly.
    """

    try:
        target, free = _read_free_disk(state_dir, disk_usage)
    except Exception as exc:
        return _result(
            "disk_space",
            BROKEN,
            f"free disk cannot be read: {_exception_text(exc)}; ingest refused",
            {"error": _exception_text(exc)},
        )
    details = {
        "path": redact_text(str(target)),
        "free_bytes": free,
        "threshold_bytes": FREE_DISK_INGEST_FLOOR_BYTES,
    }
    if free < FREE_DISK_INGEST_FLOOR_BYTES:
        return _result(
            "disk_space",
            BROKEN,
            f"free disk is below {FREE_DISK_INGEST_FLOOR_BYTES} bytes; ingest refused",
            details,
        )
    return _result(
        "disk_space",
        HEALTHY,
        f"free disk is at least {FREE_DISK_INGEST_FLOOR_BYTES} bytes",
        details,
    )


def run_doctor(
    state_dir: Path,
    *,
    now: datetime | None = None,
    disk_usage: Callable[[Path], Any] | None = None,
    intervals: Mapping[str, float] | None = None,
    registry: Sequence[twill_detectors.Detector] | None = None,
) -> DoctorReport:
    """Evaluate the Phase 1 health checks without changing state."""

    state_dir = Path(state_dir).expanduser()
    reference = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    effective_intervals = dict(TIMER_INTERVALS if intervals is None else intervals)
    effective_registry = (
        twill_detectors.REGISTRY if registry is None else tuple(registry)
    )

    connection: sqlite3.Connection | None
    open_error: str | None
    try:
        connection = _connect_for_doctor(state_dir)
        open_error = None
    except Exception as exc:
        connection = None
        open_error = _exception_text(exc)

    try:
        integrity = _check_database_integrity(connection, open_error)
        schema = _check_database_schema(connection, open_error)
        cursor = _check_cursor_health(connection, open_error)
        rule_corpus = _check_rule_corpus(connection, open_error)
        dead_man = _check_dead_man_switch(connection, open_error, reference)
    finally:
        if connection is not None:
            connection.close()

    timer = _check_timer_freshness(state_dir, reference, effective_intervals)
    performance = _check_ingest_performance(state_dir)
    self_test = _check_detector_self_test(effective_registry, reference)
    disk = _check_disk_space(state_dir, disk_usage)
    return DoctorReport(
        (
            integrity,
            schema,
            timer,
            performance,
            cursor,
            rule_corpus,
            dead_man,
            self_test,
            disk,
        )
    )
