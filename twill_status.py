from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Mapping

import twill_schema
from twill_contract import (
    EXIT_RUNTIME_ERROR,
    EXIT_VALIDATION_FAILURE,
    SCHEMA_VERSION,
    CliError,
    generated_at,
    success_envelope,
)
from twill_redactor import redact_text


STATUS_FILENAME = "status.json"
STATUS_MODE = 0o600
COUNT_FIELDS = frozenset(
    {
        "bytes",
        "clusters",
        "covered_clusters",
        "db_bytes",
        "detectors",
        "events",
        "files",
        "lessons",
        "measurements",
        "missing_paths",
        "observations",
        "pruned_observations",
        "remaining_observations",
        "records",
        "resolved",
        "rows",
        "sessions",
    }
)
PERFORMANCE_FIELDS = frozenset(
    {
        "checked_at",
        "wall_time_seconds",
        "single_file_parse_seconds",
        "peak_rss_bytes",
        "budgets",
        "misses",
    }
)
PERFORMANCE_BUDGET_FIELDS = frozenset(
    {"wall_time_seconds", "single_file_parse_seconds", "peak_rss_bytes"}
)
DETECT_PERFORMANCE_FIELDS = frozenset(
    {"checked_at", "wall_time_seconds", "budgets", "misses"}
)
DETECT_PERFORMANCE_BUDGET_FIELDS = frozenset({"wall_time_seconds"})
DB_SIZE_PERFORMANCE_FIELDS = frozenset(
    {"checked_at", "db_bytes", "budgets", "misses"}
)
DB_SIZE_PERFORMANCE_BUDGET_FIELDS = frozenset({"db_bytes"})


def status_path(state_dir: Path) -> Path:
    return Path(state_dir) / STATUS_FILENAME


def _empty_status() -> dict[str, object]:
    return success_envelope({"stages": {}})


def _load_status(state_dir: Path) -> tuple[dict[str, object], bool]:
    path = status_path(state_dir)
    if path.exists() and not path.is_file():
        _invalid_status(path, "path is not a regular file")
    if not path.is_file():
        return _empty_status(), False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(
            EXIT_RUNTIME_ERROR,
            f"status file is unreadable: {path}",
            "remove the derived status file and run a successful stage to rebuild it",
        ) from exc
    _validate_status(payload, path)
    return payload, True


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_status(payload: object, path: Path) -> None:
    if not isinstance(payload, dict):
        _invalid_status(path, "top level must be an object")
    if set(payload) != {"schema_version", "generated_at", "data", "warnings"}:
        _invalid_status(
            path,
            "top level must contain schema_version, generated_at, data and warnings",
        )
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        _invalid_status(path, f"schema_version must be {SCHEMA_VERSION}")
    if not _is_timestamp(payload["generated_at"]):
        _invalid_status(path, "generated_at must be a timestamp string")
    if not isinstance(payload["warnings"], list) or not all(
        isinstance(warning, str) for warning in payload["warnings"]
    ):
        _invalid_status(path, "warnings must be a list of strings")
    data = payload["data"]
    if not isinstance(data, dict) or set(data) != {"stages"}:
        _invalid_status(path, "data must contain only stages")
    stages = data["stages"]
    if not isinstance(stages, dict):
        _invalid_status(path, "data.stages must be an object")
    for name, record in stages.items():
        if not isinstance(name, str) or not name:
            _invalid_status(path, "stage names must be non-empty strings")
        if not isinstance(record, dict):
            _invalid_status(path, f"stage {name!r} must be an object")
        allowed_fields = {"stage", "duration", "counts", "last_success", "performance", "last_failure"}
        if not set(record).issubset(allowed_fields) or not {
            "stage", "duration", "counts", "last_success"
        }.issubset(record):
            _invalid_status(
                path,
                f"stage {name!r} has an invalid field set",
            )
        if record["stage"] != name:
            _invalid_status(path, f"stage {name!r} does not match its record")
        duration = record["duration"]
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < 0
        ):
            _invalid_status(path, f"stage {name!r} duration must be a non-negative number")
        last_success = record["last_success"]
        if last_success is not None and not _is_timestamp(last_success):
            _invalid_status(path, f"stage {name!r} last_success must be a timestamp or null")
        counts = record["counts"]
        if not isinstance(counts, dict):
            _invalid_status(path, f"stage {name!r} counts must be an object")
        _validate_counts(counts, path, name)
        if "performance" in record:
            _validate_performance(record["performance"], path, f"stage {name!r}")
        if "last_failure" in record:
            _validate_failure(record["last_failure"], path, f"stage {name!r}")


def _validate_performance(value: object, path: Path, context: str) -> None:
    if not isinstance(value, dict):
        _invalid_status(path, f"{context} performance has an invalid field set")
    fields = frozenset(value)
    if fields not in (
        PERFORMANCE_FIELDS,
        DETECT_PERFORMANCE_FIELDS,
        DB_SIZE_PERFORMANCE_FIELDS,
    ):
        _invalid_status(path, f"{context} performance has an invalid field set")
    if not _is_timestamp(value["checked_at"]):
        _invalid_status(path, f"{context} performance checked_at must be a timestamp")
    if fields == PERFORMANCE_FIELDS:
        for name in ("wall_time_seconds", "single_file_parse_seconds"):
            metric = value[name]
            if (
                isinstance(metric, bool)
                or not isinstance(metric, (int, float))
                or not math.isfinite(metric)
                or metric < 0
            ):
                _invalid_status(path, f"{context} performance {name} must be non-negative numeric")
        rss = value["peak_rss_bytes"]
        if rss is not None and (
            isinstance(rss, bool) or not isinstance(rss, int) or rss < 0
        ):
            _invalid_status(path, f"{context} performance peak_rss_bytes must be a non-negative integer or null")
        budget_fields = PERFORMANCE_BUDGET_FIELDS
    elif fields == DETECT_PERFORMANCE_FIELDS:
        metric = value["wall_time_seconds"]
        if (
            isinstance(metric, bool)
            or not isinstance(metric, (int, float))
            or not math.isfinite(metric)
            or metric < 0
        ):
            _invalid_status(path, f"{context} performance wall_time_seconds must be non-negative numeric")
        budget_fields = DETECT_PERFORMANCE_BUDGET_FIELDS
    else:
        db_bytes = value["db_bytes"]
        if isinstance(db_bytes, bool) or not isinstance(db_bytes, int) or db_bytes < 0:
            _invalid_status(path, f"{context} performance db_bytes must be a non-negative integer")
        budget_fields = DB_SIZE_PERFORMANCE_BUDGET_FIELDS
    budgets = value["budgets"]
    if not isinstance(budgets, dict) or set(budgets) != budget_fields:
        _invalid_status(path, f"{context} performance budgets have an invalid field set")
    for name, budget in budgets.items():
        if (
            isinstance(budget, bool)
            or not isinstance(budget, (int, float))
            or not math.isfinite(budget)
            or budget <= 0
        ):
            _invalid_status(path, f"{context} performance budget {name} must be positive numeric")
    misses = value["misses"]
    if not isinstance(misses, list) or not all(isinstance(miss, str) for miss in misses):
        _invalid_status(path, f"{context} performance misses must be a list of strings")


def _validate_failure(value: object, path: Path, context: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "attempted_at", "duration", "counts", "performance"
    }:
        _invalid_status(path, f"{context} last_failure has an invalid field set")
    if not _is_timestamp(value["attempted_at"]):
        _invalid_status(path, f"{context} last_failure attempted_at must be a timestamp")
    duration = value["duration"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        _invalid_status(path, f"{context} last_failure duration must be non-negative numeric")
    counts = value["counts"]
    if not isinstance(counts, dict):
        _invalid_status(path, f"{context} last_failure counts must be an object")
    _validate_counts(counts, path, f"{context} last_failure")
    _validate_performance(value["performance"], path, f"{context} last_failure")


def _validate_counts(
    counts: Mapping[str, object], path: Path, stage: str = "status"
) -> None:
    for name, value in counts.items():
        if name not in COUNT_FIELDS:
            _invalid_status(path, f"stage {stage!r} has unknown count {name!r}")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            _invalid_status(path, f"stage {stage!r} count {name!r} must be non-negative numeric")


def _invalid_status(path: Path, reason: str) -> None:
    raise CliError(
        EXIT_VALIDATION_FAILURE,
        f"status file {path} is invalid: {reason}",
        "remove the derived status file and run a successful stage to rebuild it",
    )


def _normalise_counts(counts: Mapping[str, object]) -> dict[str, int | float]:
    normalised: dict[str, int | float] = {}
    for name, value in counts.items():
        if name not in COUNT_FIELDS:
            raise ValueError(f"unknown status count {name!r}")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"status count {name!r} must be a non-negative number")
        normalised[name] = value
    return dict(sorted(normalised.items()))


def _normalise_performance(performance: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(performance, Mapping):
        raise ValueError("status performance must be an object")
    fields = frozenset(performance)
    if fields not in (
        PERFORMANCE_FIELDS,
        DETECT_PERFORMANCE_FIELDS,
        DB_SIZE_PERFORMANCE_FIELDS,
    ):
        raise ValueError("status performance has an invalid field set")
    checked_at = performance["checked_at"]
    if not _is_timestamp(checked_at):
        raise ValueError("status performance checked_at must be a timestamp")
    normalised: dict[str, object] = {
        "checked_at": checked_at,
        "budgets": dict(sorted(performance["budgets"].items()))
        if isinstance(performance["budgets"], Mapping)
        else performance["budgets"],
        "misses": list(performance["misses"])
        if isinstance(performance["misses"], list)
        else performance["misses"],
    }
    if fields == PERFORMANCE_FIELDS:
        normalised.update(
            {
                "wall_time_seconds": float(performance["wall_time_seconds"]),
                "single_file_parse_seconds": float(performance["single_file_parse_seconds"]),
                "peak_rss_bytes": performance["peak_rss_bytes"],
            }
        )
    elif fields == DETECT_PERFORMANCE_FIELDS:
        normalised["wall_time_seconds"] = float(performance["wall_time_seconds"])
    else:
        normalised["db_bytes"] = performance["db_bytes"]
    # Reuse the same strict rules used for data loaded from disk.  A temporary
    # path is not needed: these checks raise ValueError before any write.
    try:
        _validate_performance(normalised, Path("status.json"), "status")
    except CliError as exc:
        raise ValueError(exc.message) from exc
    return normalised


def _write_status(state_dir: Path, payload: Mapping[str, object]) -> None:
    directory = twill_schema.prepare_state_dir(state_dir)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory, prefix=".status-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, STATUS_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, status_path(state_dir))
        os.chmod(status_path(state_dir), STATUS_MODE)
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def record_stage(
    state_dir: Path,
    stage: str,
    duration: float,
    counts: Mapping[str, object],
    *,
    succeeded: bool = True,
    performance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not isinstance(stage, str) or not stage:
        raise ValueError("status stage must be a non-empty string")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise ValueError("status duration must be a non-negative number")
    normalised_counts = _normalise_counts(counts)
    normalised_performance = (
        _normalise_performance(performance) if performance is not None else None
    )
    payload, _ = _load_status(state_dir)
    stages = payload["data"]["stages"]
    now = generated_at()
    if not succeeded:
        if stage in stages:
            if normalised_performance is None:
                return dict(stages[stage])
            failed = dict(stages[stage])
            failed["last_failure"] = {
                "attempted_at": now,
                "duration": round(float(duration), 6),
                "counts": normalised_counts,
                "performance": normalised_performance,
            }
            stages[stage] = failed
            payload["generated_at"] = now
            _write_status(state_dir, payload)
            return dict(failed)
        failed = {
            "stage": stage,
            "duration": round(float(duration), 6),
            "counts": normalised_counts,
            "last_success": None,
        }
        if normalised_performance is not None:
            failed["performance"] = normalised_performance
        stages[stage] = failed
        payload["generated_at"] = now
        _write_status(state_dir, payload)
        return dict(failed)
    succeeded_record: dict[str, object] = {
        "stage": stage,
        "duration": round(float(duration), 6),
        "counts": normalised_counts,
        "last_success": now,
    }
    if normalised_performance is not None:
        succeeded_record["performance"] = normalised_performance
    stages[stage] = succeeded_record
    payload["generated_at"] = now
    _write_status(state_dir, payload)
    return dict(succeeded_record)


def read_status(state_dir: Path) -> dict[str, object]:
    payload, _ = _load_status(state_dir)
    stages = payload["data"]["stages"]
    if not any(record["last_success"] for record in stages.values()):
        warnings = list(payload["warnings"])
        if "no stage has recorded a successful run" not in warnings:
            warnings.append("no stage has recorded a successful run")
        payload["warnings"] = warnings
    payload["warnings"] = [redact_text(warning) for warning in payload["warnings"]]
    return payload
