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
        "db_bytes",
        "detectors",
        "events",
        "files",
        "lessons",
        "measurements",
        "missing_paths",
        "observations",
        "records",
        "rows",
        "sessions",
    }
)


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
        if set(record) != {"stage", "duration", "counts", "last_success"}:
            _invalid_status(
                path,
                f"stage {name!r} must contain stage, duration, counts and last_success",
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
    payload, _ = _load_status(state_dir)
    stages = payload["data"]["stages"]
    now = generated_at()
    if not succeeded:
        if stage in stages:
            return dict(stages[stage])
        stages[stage] = {
            "stage": stage,
            "duration": round(float(duration), 6),
            "counts": normalised_counts,
            "last_success": None,
        }
        payload["generated_at"] = now
        _write_status(state_dir, payload)
        return dict(stages[stage])
    stages[stage] = {
        "stage": stage,
        "duration": round(float(duration), 6),
        "counts": normalised_counts,
        "last_success": now,
    }
    payload["generated_at"] = now
    _write_status(state_dir, payload)
    return dict(stages[stage])


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
