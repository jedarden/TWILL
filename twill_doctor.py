"""Read-only health checks for the Phase 1 TWILL pipeline.

The doctor command is a diagnostic surface rather than a recovery surface. It
opens the derived database without creating or migrating it, evaluates each
Phase 1 signal independently, and aggregates the most severe result.
"""

from __future__ import annotations

import math
import shutil
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import twill_schema
from twill_redactor import redact_text
from twill_status import read_status


HEALTHY = "healthy"
DEGRADED = "degraded"
BROKEN = "broken"
HEALTH_STATUSES = (HEALTHY, DEGRADED, BROKEN)

EXIT_HEALTHY = 0
EXIT_DEGRADED = 1
EXIT_BROKEN = 2

FREE_DISK_WARN_BYTES = 5 * 1024**3
TIMER_INTERVALS = {"ingest": 3600.0}
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


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return candidate
        candidate = parent
    return candidate


def _check_disk_space(
    state_dir: Path,
    disk_usage: Callable[[Path], Any] | None,
) -> CheckResult:
    target = _nearest_existing_path(state_dir)
    usage_function = disk_usage or shutil.disk_usage
    try:
        usage = usage_function(target)
        free = int(usage.free)
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


def run_doctor(
    state_dir: Path,
    *,
    now: datetime | None = None,
    disk_usage: Callable[[Path], Any] | None = None,
    intervals: Mapping[str, float] | None = None,
) -> DoctorReport:
    """Evaluate the Phase 1 health checks without changing state."""

    state_dir = Path(state_dir).expanduser()
    reference = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    effective_intervals = dict(TIMER_INTERVALS if intervals is None else intervals)

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
    finally:
        if connection is not None:
            connection.close()

    timer = _check_timer_freshness(state_dir, reference, effective_intervals)
    disk = _check_disk_space(state_dir, disk_usage)
    return DoctorReport((integrity, schema, timer, cursor, disk))
