"""Replay accepted lesson detectors and persist durable measurement series."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import twill_detectors
import twill_lessons
from twill_config import ConfigError
from twill_contract import (
    EXIT_RUNTIME_ERROR,
    EXIT_VALIDATION_FAILURE,
    CliError,
    ValidationError,
)


MEASUREMENT_DIRNAME = "measurements"
MEASUREMENT_DIR_MODE = 0o700
MEASUREMENT_FILE_MODE = 0o600
DEFAULT_MEASUREMENT_WINDOW_DAYS = 7
MEASURABLE_STATES = frozenset(
    {"accepted"} | {f"applied:{layer}" for layer in twill_lessons.ROUTING_LAYERS}
)
_DETECTOR_ID_RE = re.compile(r"^D-\d{2,}@\d+$")
_TIMESTAMP_FORMATS = ("%Y-%m-%d",)


class MeasurementError(CliError):
    """A measurement could not be completed within the CLI contract."""


class MeasurementValidationError(MeasurementError, ValidationError):
    """A measurement input or durable mirror failed validation."""

    def __init__(self, code: int, message: str, hint: str = "") -> None:
        CliError.__init__(self, code, message, hint)


@dataclass(frozen=True)
class Measurement:
    lesson_id: str
    detector_id: str
    measured_at: str
    window_days: int
    sessions: int
    events: int

    @property
    def day(self) -> str:
        return _timestamp(self.measured_at).date().isoformat()

    def as_dict(self) -> dict[str, object]:
        return {
            "lesson_id": self.lesson_id,
            "detector_id": self.detector_id,
            "measured_at": self.measured_at,
            "window_days": self.window_days,
            "sessions": self.sessions,
            "events": self.events,
        }


@dataclass(frozen=True)
class MeasurementReport:
    window_days: int
    window_start_utc: str
    measured_at: str
    measurements: tuple[Measurement, ...]
    skipped: tuple[str, ...] = ()

    @property
    def points(self) -> tuple[Measurement, ...]:
        return self.measurements

    @property
    def records(self) -> tuple[Measurement, ...]:
        return self.measurements

    def as_dict(self) -> dict[str, object]:
        return {
            "window_days": self.window_days,
            "window_start_utc": self.window_start_utc,
            "measured_at": self.measured_at,
            "measurements": [item.as_dict() for item in self.measurements],
            "skipped": list(self.skipped),
        }


@dataclass
class _StagedFile:
    path: Path
    temporary: Path | None
    previous: bytes | None


def _error(code: int, message: str, hint: str = "") -> MeasurementError:
    return MeasurementError(code, message, hint)


def _validation(message: str, hint: str = "") -> MeasurementValidationError:
    return MeasurementValidationError(EXIT_VALIDATION_FAILURE, message, hint)


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp must not be empty")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is None:
            for fmt in _TIMESTAMP_FORMATS:
                try:
                    parsed = datetime.strptime(value.strip(), fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            raise ValueError("timestamp must be ISO-8601")
    else:
        raise ValueError("timestamp must be a string or datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clock(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    try:
        return _timestamp(value)
    except ValueError as exc:
        raise _validation(str(exc), "pass an ISO-8601 measurement time") from exc


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_root(artifacts_root: Path, repo_root: Path | None = None) -> Path:
    root = Path(artifacts_root).expanduser().resolve()
    repository = (
        Path(__file__).resolve().parent if repo_root is None else Path(repo_root)
    ).resolve()
    if root == repository or repository in root.parents:
        raise ConfigError(
            f"{root} resolves inside the TWILL repository tree ({repository})",
            "measurements belong under artifacts_root outside the public repository",
        )
    return root


def measurement_dir(
    artifacts_root: Path, *, repo_root: Path | None = None
) -> Path:
    root = _validate_root(artifacts_root, repo_root)
    directory = root / MEASUREMENT_DIRNAME
    if directory.is_symlink():
        raise _validation(
            f"measurement directory may not be a symbolic link: {directory.name}",
            "remove the symlink and choose a regular directory under artifacts_root",
        )
    return directory


def measurement_path(
    artifacts_root: Path,
    lesson_id: str,
    *,
    repo_root: Path | None = None,
) -> Path:
    if not isinstance(lesson_id, str) or not twill_lessons.LESSON_ID_RE.fullmatch(lesson_id):
        raise _validation("measurement lesson id is invalid")
    directory = measurement_dir(artifacts_root, repo_root=repo_root)
    path = directory / f"{lesson_id}.jsonl"
    if path.is_symlink():
        raise _validation(
            f"measurement file may not be a symbolic link: {path.name}",
            "remove the symlink before measuring",
        )
    resolved_root = _validate_root(artifacts_root, repo_root)
    resolved = path.resolve()
    if resolved_root not in resolved.parents:
        raise _validation("measurement path escapes artifacts_root")
    return path


def _ensure_directory(artifacts_root: Path, repo_root: Path | None = None) -> Path:
    directory = measurement_dir(artifacts_root, repo_root=repo_root)
    try:
        directory.mkdir(mode=MEASUREMENT_DIR_MODE, parents=True, exist_ok=True)
        os.chmod(directory, MEASUREMENT_DIR_MODE)
    except OSError as exc:
        raise _error(
            EXIT_RUNTIME_ERROR,
            f"measurement directory cannot be created: {directory}",
            "check artifacts_root permissions and free space",
        ) from exc
    if directory.is_symlink() or not directory.is_dir():
        raise _validation("measurement path is not a regular directory")
    return directory


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _validation(f"measurement {field} must be a non-negative integer")
    return value


def _measurement(
    lesson_id: str,
    detector_id: str,
    measured_at: str,
    window_days: object,
    sessions: object,
    events: object,
) -> Measurement:
    if not isinstance(lesson_id, str) or not twill_lessons.LESSON_ID_RE.fullmatch(lesson_id):
        raise _validation("measurement lesson_id is invalid")
    if not isinstance(detector_id, str) or not _DETECTOR_ID_RE.fullmatch(detector_id):
        raise _validation("measurement detector_id must be a full D-NN@version id")
    try:
        _timestamp(measured_at)
    except ValueError as exc:
        raise _validation("measurement measured_at must be ISO-8601") from exc
    days = _count(window_days, "window_days")
    if days < 1:
        raise _validation("measurement window_days must be positive")
    return Measurement(
        lesson_id=lesson_id,
        detector_id=detector_id,
        measured_at=str(measured_at),
        window_days=days,
        sessions=_count(sessions, "sessions"),
        events=_count(events, "events"),
    )


def _measurement_from_row(row: Sequence[object]) -> Measurement:
    if len(row) != 6:
        raise _validation("measurement database row has the wrong shape")
    return _measurement(*row)


def _read_mirror_file(path: Path, lesson_id: str) -> tuple[Measurement, ...]:
    if path.is_symlink():
        raise _validation(f"measurement file may not be a symbolic link: {path.name}")
    if not path.is_file():
        raise _validation(f"measurement file is not a regular file: {path.name}")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _error(
            EXIT_RUNTIME_ERROR,
            f"measurement file cannot be read: {path.name}",
            "check artifacts_root permissions and file encoding",
        ) from exc
    result: list[Measurement] = []
    seen_days: set[str] = set()
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _validation(
                f"measurement file {path.name} has invalid JSON on line {line_number}"
            ) from exc
        if not isinstance(payload, dict):
            raise _validation(
                f"measurement file {path.name} line {line_number} must be an object"
            )
        required = {
            "lesson_id",
            "detector_id",
            "measured_at",
            "window_days",
            "sessions",
            "events",
        }
        if set(payload) != required:
            raise _validation(
                f"measurement file {path.name} line {line_number} has an invalid shape"
            )
        item = _measurement(
            payload["lesson_id"],
            payload["detector_id"],
            payload["measured_at"],
            payload["window_days"],
            payload["sessions"],
            payload["events"],
        )
        if item.lesson_id != lesson_id:
            raise _validation(
                f"measurement file {path.name} contains a different lesson id"
            )
        if item.day in seen_days:
            raise _validation(
                f"measurement file {path.name} contains more than one point for a day"
            )
        seen_days.add(item.day)
        result.append(item)
    return tuple(result)


def read_measurements(
    artifacts_root: Path,
    lesson_id: str | None = None,
    *,
    repo_root: Path | None = None,
) -> tuple[Measurement, ...]:
    """Read one lesson mirror, or all valid lesson mirrors under the root."""

    directory = measurement_dir(artifacts_root, repo_root=repo_root)
    if not directory.exists():
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise _validation("measurement directory is not a regular directory")
    if lesson_id is not None:
        path = measurement_path(artifacts_root, lesson_id, repo_root=repo_root)
        if not path.exists():
            return ()
        return _read_mirror_file(path, lesson_id)
    result: list[Measurement] = []
    for path in sorted(directory.glob("L-*.jsonl")):
        match = re.fullmatch(r"(L-[0-9a-f]{8})\.jsonl", path.name)
        if match is None:
            raise _validation(f"measurement filename is invalid: {path.name}")
        result.extend(_read_mirror_file(path, match.group(1)))
    return tuple(result)


def _read_database(connection: sqlite3.Connection) -> tuple[Measurement, ...]:
    try:
        rows = connection.execute(
            "SELECT lesson_id, detector_id, measured_at, window_days, sessions, events "
            "FROM measurement ORDER BY lesson_id, measured_at"
        ).fetchall()
    except sqlite3.Error as exc:
        raise _error(
            EXIT_RUNTIME_ERROR,
            "measurement table cannot be read",
            "run twill doctor to inspect the derived state database",
        ) from exc
    return tuple(_measurement_from_row(row) for row in rows)


def _merge_history(
    database_rows: Iterable[Measurement], mirror_rows: Iterable[Measurement]
) -> dict[tuple[str, str], Measurement]:
    history: dict[tuple[str, str], Measurement] = {}
    for item in database_rows:
        key = (item.lesson_id, item.day)
        previous = history.get(key)
        if previous is None or _timestamp(item.measured_at) > _timestamp(previous.measured_at):
            history[key] = item
    for item in mirror_rows:
        history[(item.lesson_id, item.day)] = item
    return history


def _detector_for(
    detector_id: str, registry: Sequence[twill_detectors.Detector]
) -> twill_detectors.Detector:
    if not isinstance(detector_id, str) or not detector_id:
        raise _validation("lesson detector must be a non-empty identifier")
    base, separator, requested_version = detector_id.partition("@")
    if separator and (not requested_version.isdecimal() or int(requested_version) < 1):
        raise _validation(
            f"lesson {detector_id!r} has an invalid detector version",
            "use a base detector id or the active full detector id",
        )
    try:
        detector = twill_detectors.select_detectors(registry, (base,))[0]
    except (ValueError, IndexError) as exc:
        raise _validation(
            f"lesson {detector_id!r} names an unregistered detector",
            "register the detector or correct the lesson before measuring",
        ) from exc
    if separator and int(requested_version) != detector.version:
        raise _validation(
            f"lesson {detector_id!r} is not the active detector version",
            "update the lesson to the active detector version before measuring",
        )
    return detector


def _replay_read_only(
    connection: sqlite3.Connection,
    detector: twill_detectors.Detector,
    key: str,
    *,
    window_start_utc: str,
    window_days: int,
) -> tuple[str, dict[str, tuple[int, int, str, str]]]:
    previous = connection.execute("PRAGMA query_only").fetchone()
    previous_value = bool(previous and previous[0])
    if not previous_value:
        connection.execute("PRAGMA query_only = ON")
    try:
        twill_detectors.validate_detector_semantics(connection, detector)
        canonical_key = twill_detectors.normalize_detector_key(detector, key)
        emitted = twill_detectors.read_clusters(
            connection,
            detector,
            window_start_utc=window_start_utc,
            window_days=window_days,
        )
        return canonical_key, emitted
    finally:
        if not previous_value:
            connection.execute("PRAGMA query_only = OFF")


def _replay_lesson(
    connection: sqlite3.Connection,
    lesson: twill_lessons.LessonRecord,
    *,
    registry: Sequence[twill_detectors.Detector],
    window_start_utc: str,
    window_days: int,
) -> Measurement:
    detector = _detector_for(lesson.detector, registry)
    try:
        canonical_key, emitted = _replay_read_only(
            connection,
            detector,
            lesson.key,
            window_start_utc=window_start_utc,
            window_days=window_days,
        )
    except twill_detectors.DetectorContractError as exc:
        raise _validation(
            f"{detector.full_id} replay failed validation: {exc}",
            "bump the detector version when its semantics change",
        ) from exc
    except sqlite3.Error as exc:
        raise _error(
            EXIT_RUNTIME_ERROR,
            f"{detector.full_id} replay failed",
            "run twill doctor to inspect the derived state database",
        ) from exc
    row = emitted.get(canonical_key)
    sessions, events = (0, 0) if row is None else (row[0], row[1])
    return Measurement(
        lesson_id=lesson.id,
        detector_id=detector.full_id,
        measured_at="",
        window_days=window_days,
        sessions=sessions,
        events=events,
    )


def _write_temp(path: Path, content: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=".measurement-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, MEASUREMENT_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return temporary


def _render_mirror(items: Iterable[Measurement]) -> bytes:
    ordered = sorted(items, key=lambda item: (item.lesson_id, item.day, item.measured_at))
    return b"".join(
        json.dumps(item.as_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        + b"\n"
        for item in ordered
    )


def _stage_mirrors(
    artifacts_root: Path,
    history: Mapping[tuple[str, str], Measurement],
    *,
    repo_root: Path | None = None,
) -> tuple[Path, list[_StagedFile]]:
    directory = _ensure_directory(artifacts_root, repo_root)
    grouped: dict[str, list[Measurement]] = {}
    for item in history.values():
        grouped.setdefault(item.lesson_id, []).append(item)
    staged: list[_StagedFile] = []
    try:
        paths: list[Path] = []
        for lesson_id in sorted(grouped):
            path = measurement_path(artifacts_root, lesson_id, repo_root=repo_root)
            paths.append(path)
            if path.exists() and (path.is_symlink() or not path.is_file()):
                raise _validation(f"measurement path is not a regular file: {path.name}")
        for path in paths:
            lesson_id = path.stem
            content = _render_mirror(grouped[lesson_id])
            try:
                previous = path.read_bytes() if path.exists() else None
            except OSError as exc:
                raise _error(
                    EXIT_RUNTIME_ERROR,
                    f"measurement file cannot be read: {path.name}",
                    "check artifacts_root permissions",
                ) from exc
            if previous == content:
                if path.exists():
                    try:
                        os.chmod(path, MEASUREMENT_FILE_MODE)
                    except OSError as exc:
                        raise _error(
                            EXIT_RUNTIME_ERROR,
                            f"measurement file permissions cannot be set: {path.name}",
                            "check artifacts_root permissions",
                        ) from exc
                continue
            try:
                temporary = _write_temp(path, content)
            except OSError as exc:
                raise _error(
                    EXIT_RUNTIME_ERROR,
                    f"measurement file cannot be staged: {path.name}",
                    "check artifacts_root permissions and free space",
                ) from exc
            staged.append(_StagedFile(path=path, temporary=temporary, previous=previous))
        return directory, staged
    except BaseException:
        _cleanup_staged(staged)
        raise


def _cleanup_staged(staged: Sequence[_StagedFile]) -> None:
    for item in staged:
        if item.temporary is not None:
            try:
                item.temporary.unlink()
            except FileNotFoundError:
                pass


def _persist(
    connection: sqlite3.Connection,
    history: Mapping[tuple[str, str], Measurement],
    staged: Sequence[_StagedFile],
) -> None:
    owns_transaction = not connection.in_transaction
    savepoint = "twill_measure"
    try:
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        else:
            connection.execute(f"SAVEPOINT {savepoint}")
        lesson_ids = sorted({lesson_id for lesson_id, _day in history})
        for lesson_id in lesson_ids:
            connection.execute("DELETE FROM measurement WHERE lesson_id = ?", (lesson_id,))
        for item in (history[key] for key in sorted(history)):
            connection.execute(
                "INSERT INTO measurement(lesson_id, detector_id, measured_at, "
                "window_days, sessions, events) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.lesson_id,
                    item.detector_id,
                    item.measured_at,
                    item.window_days,
                    item.sessions,
                    item.events,
                ),
            )
        for item in staged:
            if item.temporary is None:
                continue
            os.replace(item.temporary, item.path)
            os.chmod(item.path, MEASUREMENT_FILE_MODE)
            item.temporary = None
            directory_fd = os.open(
                item.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        if owns_transaction:
            connection.commit()
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        if owns_transaction:
            connection.rollback()
        else:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def measure_lessons(
    connection: sqlite3.Connection,
    artifacts_root: Path,
    *,
    lesson_id: str | None = None,
    now: str | datetime | None = None,
    window_days: int = DEFAULT_MEASUREMENT_WINDOW_DAYS,
    registry: Sequence[twill_detectors.Detector] | None = None,
    repo_root: Path | None = None,
) -> MeasurementReport:
    """Measure every eligible lesson and reconcile its durable mirror."""

    if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 1:
        raise _validation("measurement window_days must be a positive integer")
    current = _clock(now)
    measured_at = _format_timestamp(current)
    window_start = (current - timedelta(days=window_days)).isoformat()
    records = twill_lessons.list_lessons(
        _validate_root(artifacts_root, repo_root), repo_root=repo_root
    )
    if lesson_id is not None:
        if not isinstance(lesson_id, str) or not twill_lessons.LESSON_ID_RE.fullmatch(lesson_id):
            raise _validation("measurement lesson id is invalid")
        selected = tuple(record for record in records if record.id == lesson_id)
        if not selected:
            raise _validation(f"lesson does not exist: {lesson_id}")
        records = selected
    try:
        active_registry = twill_detectors.build_registry(
            *(tuple(registry) if registry is not None else twill_detectors.REGISTRY)
        )
    except (TypeError, ValueError) as exc:
        raise _validation(
            f"measurement detector registry is invalid: {exc}",
            "fix the active detector registry before measuring",
        ) from exc
    database_history = _read_database(connection)
    mirror_history = read_measurements(artifacts_root, repo_root=repo_root)
    history = _merge_history(database_history, mirror_history)
    points: list[Measurement] = []
    skipped: list[str] = []
    for lesson in records:
        if lesson.state not in MEASURABLE_STATES:
            skipped.append(lesson.id)
            continue
        point = _replay_lesson(
            connection,
            lesson,
            registry=active_registry,
            window_start_utc=window_start,
            window_days=window_days,
        )
        points.append(
            Measurement(
                lesson_id=point.lesson_id,
                detector_id=point.detector_id,
                measured_at=measured_at,
                window_days=point.window_days,
                sessions=point.sessions,
                events=point.events,
            )
        )
    for point in points:
        history[(point.lesson_id, point.day)] = point
    _, staged = _stage_mirrors(artifacts_root, history, repo_root=repo_root)
    try:
        _persist(connection, history, staged)
    except BaseException:
        _cleanup_staged(staged)
        raise
    return MeasurementReport(
        window_days=window_days,
        window_start_utc=window_start,
        measured_at=measured_at,
        measurements=tuple(points),
        skipped=tuple(skipped),
    )


def run_measure(
    connection: sqlite3.Connection,
    artifacts_root: Path,
    **kwargs: object,
) -> MeasurementReport:
    return measure_lessons(connection, artifacts_root, **kwargs)


def restore_measurements(
    connection: sqlite3.Connection,
    artifacts_root: Path,
    *,
    repo_root: Path | None = None,
) -> int:
    """Restore mirror rows into the derived database without running detectors."""

    rows = read_measurements(artifacts_root, repo_root=repo_root)
    owns_transaction = not connection.in_transaction
    savepoint = "twill_measure_restore"
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection.execute(f"SAVEPOINT {savepoint}")
    try:
        lesson_ids = sorted({item.lesson_id for item in rows})
        for lesson_id in lesson_ids:
            connection.execute("DELETE FROM measurement WHERE lesson_id = ?", (lesson_id,))
        for item in rows:
            connection.execute(
                "INSERT INTO measurement(lesson_id, detector_id, measured_at, "
                "window_days, sessions, events) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.lesson_id,
                    item.detector_id,
                    item.measured_at,
                    item.window_days,
                    item.sessions,
                    item.events,
                ),
            )
        if owns_transaction:
            connection.commit()
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        if owns_transaction:
            connection.rollback()
        else:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    return len(rows)


__all__ = [
    "DEFAULT_MEASUREMENT_WINDOW_DAYS",
    "MEASURABLE_STATES",
    "MEASUREMENT_DIRNAME",
    "MEASUREMENT_DIR_MODE",
    "MEASUREMENT_FILE_MODE",
    "Measurement",
    "MeasurementError",
    "MeasurementReport",
    "MeasurementValidationError",
    "measure_lessons",
    "measurement_dir",
    "measurement_path",
    "read_measurements",
    "restore_measurements",
    "run_measure",
]
