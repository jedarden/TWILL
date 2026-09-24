"""File-backed lesson lifecycle operations."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from twill_config import ConfigError
from twill_contract import ValidationError
from twill_redactor import redact_text
from twill_router import ROUTING_LAYER_ORDER


LESSON_DIRNAME = "lessons"
LESSON_DIR_MODE = 0o700
LESSON_FILE_MODE = 0o600
LESSON_ID_RE = re.compile(r"^L-[0-9a-f]{8}$")
LESSON_STATES = frozenset({"draft", "accepted", "resolved", "escalated", "retired"})
STATE_FILTERS = frozenset(LESSON_STATES | {"applied"})
ROUTING_LAYERS = frozenset(ROUTING_LAYER_ORDER)
TERMINAL_STATES = frozenset({"resolved", "escalated", "retired"})
VALID_STATES = frozenset(
    LESSON_STATES | {f"applied:{layer}" for layer in ROUTING_LAYERS}
)
MAX_FIELD_LENGTH = 240
_REQUIRED_TOP_LEVEL = frozenset(
    {"id", "summary", "state", "detector", "key", "evidence", "routing", "backtest"}
)
_REQUIRED_EVIDENCE = frozenset({"sessions", "events", "first_seen", "session_ids"})
_REQUIRED_ROUTING = frozenset({"recommended", "applied", "applied_at", "bead"})
_REQUIRED_BACKTEST = frozenset(
    {"window_days", "sessions", "first_seen", "weeks_present"}
)
_SAFE_BARE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]*$")
_SAFE_FIELD_RE = re.compile(r"^[^\x00-\x1f\x7f]+$")


class _InlineValueError(ValueError):
    pass


class _InlineParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.position = 0

    def parse(self) -> object:
        value = self._value()
        self._space()
        if self.position != len(self.text):
            raise _InlineValueError("trailing inline value")
        return value

    def _space(self) -> None:
        while self.position < len(self.text) and self.text[self.position].isspace():
            self.position += 1

    def _value(self) -> object:
        self._space()
        if self.position >= len(self.text):
            raise _InlineValueError("missing value")
        character = self.text[self.position]
        if character == "{":
            return self._mapping()
        if character == "[":
            return self._sequence()
        if character == '"':
            return self._quoted()
        start = self.position
        while self.position < len(self.text) and self.text[self.position] not in ",}]":
            self.position += 1
        token = self.text[start : self.position].strip()
        if not token:
            raise _InlineValueError("empty value")
        if token == "null":
            return None
        if token == "true":
            return True
        if token == "false":
            return False
        if re.fullmatch(r"[-+]?\d+", token):
            try:
                return int(token)
            except ValueError:
                raise _InlineValueError("invalid integer") from None
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?", token):
            try:
                return float(token)
            except ValueError:
                raise _InlineValueError("invalid number") from None
        if not _SAFE_FIELD_RE.fullmatch(token):
            raise _InlineValueError("invalid scalar")
        return token

    def _quoted(self) -> str:
        try:
            value, end = json.JSONDecoder().raw_decode(self.text, self.position)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _InlineValueError("invalid quoted scalar") from exc
        if not isinstance(value, str):
            raise _InlineValueError("quoted scalar must be text")
        self.position = end
        return value

    def _key(self) -> str:
        self._space()
        if self.position >= len(self.text):
            raise _InlineValueError("missing mapping key")
        if self.text[self.position] == '"':
            return self._quoted()
        start = self.position
        while self.position < len(self.text) and self.text[self.position] not in ":,}]":
            self.position += 1
        key = self.text[start : self.position].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise _InlineValueError("invalid mapping key")
        return key

    def _mapping(self) -> dict[str, object]:
        self.position += 1
        result: dict[str, object] = {}
        self._space()
        if self.position < len(self.text) and self.text[self.position] == "}":
            self.position += 1
            return result
        while True:
            key = self._key()
            if key in result:
                raise _InlineValueError("duplicate mapping key")
            self._space()
            if self.position >= len(self.text) or self.text[self.position] != ":":
                raise _InlineValueError("mapping key has no value")
            self.position += 1
            result[key] = self._value()
            self._space()
            if self.position >= len(self.text):
                raise _InlineValueError("unterminated mapping")
            character = self.text[self.position]
            self.position += 1
            if character == "}":
                return result
            if character != ",":
                raise _InlineValueError("mapping separator is not a comma")
            self._space()
            if self.position < len(self.text) and self.text[self.position] == "}":
                raise _InlineValueError("trailing mapping comma")

    def _sequence(self) -> list[object]:
        self.position += 1
        result: list[object] = []
        self._space()
        if self.position < len(self.text) and self.text[self.position] == "]":
            self.position += 1
            return result
        while True:
            result.append(self._value())
            self._space()
            if self.position >= len(self.text):
                raise _InlineValueError("unterminated sequence")
            character = self.text[self.position]
            self.position += 1
            if character == "]":
                return result
            if character != ",":
                raise _InlineValueError("sequence separator is not a comma")
            self._space()
            if self.position < len(self.text) and self.text[self.position] == "]":
                raise _InlineValueError("trailing sequence comma")


def _error(message: str, hint: str = "") -> ValidationError:
    return ValidationError(message, hint)


def _parse_value(value: str, field: str) -> object:
    try:
        return _InlineParser(value.strip()).parse()
    except _InlineValueError as exc:
        raise _error(f"lesson frontmatter field {field!r} is invalid: {exc}") from None


def _redacted(value: object, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise _error(f"lesson frontmatter field {field!r} must not be null")
        return None
    if isinstance(value, bool):
        raise _error(f"lesson frontmatter field {field!r} must be text")
    text = str(value)
    safe = redact_text(text)
    if safe != text:
        raise _error(f"lesson frontmatter field {field!r} contains redacted content")
    if required and not safe:
        raise _error(f"lesson frontmatter field {field!r} must not be empty")
    if len(safe) > MAX_FIELD_LENGTH or not _SAFE_FIELD_RE.fullmatch(safe):
        raise _error(
            f"lesson frontmatter field {field!r} is not a bounded single-line value"
        )
    return safe


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error(
            f"lesson frontmatter field {field!r} must be a non-negative integer"
        )
    return value


def _positive_int(value: object, field: str) -> int:
    result = _nonnegative_int(value, field)
    if result < 1:
        raise _error(f"lesson frontmatter field {field!r} must be a positive integer")
    return result


def _timestamp(value: object, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise _error(f"lesson frontmatter field {field!r} must be a timestamp")
        return None
    if not isinstance(value, str) or not value.strip():
        raise _error(f"lesson frontmatter field {field!r} must be a timestamp or null")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise _error(
            f"lesson frontmatter field {field!r} must be an ISO-8601 timestamp"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _date(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _error(f"lesson frontmatter field {field!r} must be a date or null")
    text = value.strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise _error(
            f"lesson frontmatter field {field!r} must be an ISO date"
        ) from None
    return text


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _error(f"lesson frontmatter field {field!r} must be a mapping")
    return dict(value)


def _validate_state(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _error("lesson state must be a non-empty string")
    if value in LESSON_STATES:
        return value
    prefix = "applied:"
    if value.startswith(prefix):
        layer = value[len(prefix) :]
        if layer not in ROUTING_LAYERS:
            raise _error(f"unknown routing layer in lesson state: {layer!r}")
        return value
    raise _error(
        "unknown lesson state",
        "use draft, accepted, applied:<layer>, resolved, escalated, or retired",
    )


def state_category(state: str) -> str:
    """Return the filterable category for a lesson state."""

    if isinstance(state, str) and state in STATE_FILTERS:
        return state
    return _validate_state(state).partition(":")[0]


def _validate_layer(value: object) -> str:
    if not isinstance(value, str) or value not in ROUTING_LAYERS:
        choices = ", ".join(sorted(ROUTING_LAYERS))
        raise _error(f"routing layer must be one of: {choices}")
    return value


def _validate_evidence(value: object) -> dict[str, object]:
    evidence = _mapping(value, "evidence")
    if not _REQUIRED_EVIDENCE.issubset(evidence):
        missing = ", ".join(sorted(_REQUIRED_EVIDENCE - set(evidence)))
        raise _error(f"lesson evidence is missing required field(s): {missing}")
    sessions = _positive_int(evidence["sessions"], "evidence.sessions")
    events = _nonnegative_int(evidence["events"], "evidence.events")
    first_seen = _date(evidence["first_seen"], "evidence.first_seen")
    raw_ids = evidence["session_ids"]
    if not isinstance(raw_ids, list) or not raw_ids:
        raise _error("lesson evidence.session_ids must contain at least one session id")
    session_ids: list[str] = []
    for index, raw_id in enumerate(raw_ids):
        session_id = _redacted(raw_id, f"evidence.session_ids[{index}]", required=True)
        assert session_id is not None
        if session_id not in session_ids:
            session_ids.append(session_id)
    return {
        "sessions": sessions,
        "events": events,
        "first_seen": first_seen,
        "session_ids": session_ids,
    }


def _validate_routing(value: object) -> dict[str, object]:
    routing = _mapping(value, "routing")
    if not _REQUIRED_ROUTING.issubset(routing):
        missing = ", ".join(sorted(_REQUIRED_ROUTING - set(routing)))
        raise _error(f"lesson routing is missing required field(s): {missing}")
    recommended = routing["recommended"]
    reason = _redacted(routing.get("reason"), "routing.reason")
    applied = routing["applied"]
    if recommended is not None:
        recommended = _validate_layer(recommended)
    if applied is not None:
        applied = _validate_layer(applied)
    if (recommended is None) != (reason is None):
        raise _error(
            "routing.reason must accompany a recommendation",
            "record why the selected layer is the strongest justified intervention",
        )
    applied_at = _timestamp(routing["applied_at"], "routing.applied_at")
    bead = _redacted(routing["bead"], "routing.bead")
    if applied is not None and (applied_at is None or bead is None):
        raise _error(
            "an applied lesson must record routing.applied_at and routing.bead"
        )
    return {
        "recommended": recommended,
        "reason": reason,
        "applied": applied,
        "applied_at": applied_at,
        "bead": bead,
    }


def _validate_backtest(value: object) -> dict[str, object]:
    backtest = _mapping(value, "backtest")
    if not _REQUIRED_BACKTEST.issubset(backtest):
        missing = ", ".join(sorted(_REQUIRED_BACKTEST - set(backtest)))
        raise _error(
            f"lesson backtest is missing required field(s): {missing}",
            "a lesson cannot be reviewed before its detector backtest is populated",
        )
    return {
        "window_days": _positive_int(backtest["window_days"], "backtest.window_days"),
        "sessions": _nonnegative_int(backtest["sessions"], "backtest.sessions"),
        "first_seen": _date(backtest["first_seen"], "backtest.first_seen"),
        "weeks_present": _nonnegative_int(
            backtest["weeks_present"], "backtest.weeks_present"
        ),
    }


def _validate_guard(value: object) -> dict[str, object]:
    if value is None:
        return {"layer": None, "artifact": None, "installed": False}
    guard = _mapping(value, "guard")
    layer = guard.get("layer")
    if layer is not None:
        layer = _validate_layer(layer)
    artifact = _redacted(guard.get("artifact"), "guard.artifact")
    installed = guard.get("installed", False)
    if not isinstance(installed, bool):
        raise _error("lesson frontmatter field 'guard.installed' must be boolean")
    return {"layer": layer, "artifact": artifact, "installed": installed}


def _validate_body(body: str) -> str:
    for line_number, line in enumerate(body.splitlines(), start=1):
        safe = redact_text(line)[:MAX_FIELD_LENGTH]
        if safe != line:
            raise _error(f"lesson body line {line_number} contains redacted content")
        if len(safe) > MAX_FIELD_LENGTH:
            raise _error(f"lesson body line {line_number} exceeds 240 characters")
    return body


def _validate_record_fields(
    fields: Mapping[str, object], path: Path | None = None
) -> dict[str, object]:
    missing = _REQUIRED_TOP_LEVEL - set(fields)
    if missing:
        names = ", ".join(sorted(missing))
        raise _error(f"lesson frontmatter is missing required field(s): {names}")
    lesson_id = _redacted(fields["id"], "id", required=True)
    assert lesson_id is not None
    if not LESSON_ID_RE.fullmatch(lesson_id):
        raise _error(
            "lesson id must match L- followed by eight lowercase hexadecimal digits"
        )
    if path is not None and path.stem != lesson_id:
        raise _error("lesson id does not match its filename")
    summary = _redacted(fields["summary"], "summary", required=True)
    assert summary is not None
    state = _validate_state(fields["state"])
    detector = _redacted(fields["detector"], "detector", required=True)
    assert detector is not None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]*", detector):
        raise _error("lesson detector must be an identifier")
    key = _redacted(fields["key"], "key", required=True)
    assert key is not None
    evidence = _validate_evidence(fields["evidence"])
    routing = _validate_routing(fields["routing"])
    if state.startswith("applied:"):
        layer = state.split(":", 1)[1]
        if routing["applied"] != layer:
            raise _error("lesson state and routing.applied must name the same layer")
    elif state in TERMINAL_STATES:
        if routing["applied"] is None:
            raise _error("a terminal lesson must retain its applied routing layer")
    elif routing["applied"] is not None:
        raise _error("only an applied lesson may carry routing.applied")
    backtest = _validate_backtest(fields["backtest"])
    return {
        "id": lesson_id,
        "summary": summary,
        "state": state,
        "detector": detector,
        "key": key,
        "evidence": evidence,
        "routing": routing,
        "backtest": backtest,
        "guard": _validate_guard(fields.get("guard")),
    }


@dataclass(frozen=True)
class LessonRecord:
    """A validated lesson file and its operator-owned body."""

    id: str
    summary: str
    state: str
    detector: str
    key: str
    evidence: dict[str, object]
    routing: dict[str, object]
    backtest: dict[str, object]
    guard: dict[str, object]
    body: str
    path: Path
    _opening: str
    _frontmatter_lines: tuple[str, ...]
    _closing: str

    @property
    def lesson_id(self) -> str:
        return self.id

    @property
    def layer(self) -> str | None:
        if self.state.startswith("applied:"):
            return self.state.split(":", 1)[1]
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "summary": self.summary,
            "state": self.state,
            "detector": self.detector,
            "key": self.key,
            "evidence": dict(self.evidence),
            "routing": dict(self.routing),
            "backtest": dict(self.backtest),
            "guard": dict(self.guard),
        }


def _validate_id(lesson_id: str) -> str:
    if not isinstance(lesson_id, str) or not LESSON_ID_RE.fullmatch(lesson_id):
        raise _error(
            "lesson id must match L- followed by eight lowercase hexadecimal digits"
        )
    return lesson_id


def _validate_root(artifacts_root: Path, repo_root: Path | None = None) -> Path:
    root = Path(artifacts_root).expanduser().resolve()
    repository = (
        Path(__file__).resolve().parent if repo_root is None else Path(repo_root)
    ).resolve()
    if root == repository or repository in root.parents:
        raise ConfigError(
            f"{root} resolves inside the TWILL repository tree ({repository})",
            "this repository is public; lessons belong under artifacts_root outside it",
        )
    return root


def lessons_dir(artifacts_root: Path, *, repo_root: Path | None = None) -> Path:
    """Return the validated external lessons directory without creating it."""

    root = _validate_root(artifacts_root, repo_root)
    return root / LESSON_DIRNAME


def _validate_lesson_path(path: Path, repo_root: Path | None = None) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or candidate.parent.is_symlink():
        raise _error("lesson path may not traverse a symbolic link")
    resolved = candidate.resolve()
    repository = (
        Path(__file__).resolve().parent
        if repo_root is None
        else Path(repo_root).resolve()
    )
    if resolved == repository or repository in resolved.parents:
        raise ConfigError(
            f"{resolved} resolves inside the TWILL repository tree ({repository})",
            "this repository is public; lessons belong under artifacts_root outside it",
        )
    return candidate


def lesson_path(
    artifacts_root: Path,
    lesson_id: str,
    *,
    repo_root: Path | None = None,
) -> Path:
    """Return the safe path for one lesson identifier."""

    return (
        lessons_dir(artifacts_root, repo_root=repo_root)
        / f"{_validate_id(lesson_id)}.md"
    )


def _read_document(path: Path) -> tuple[dict[str, object], str, str, str, str]:
    if path.is_symlink() or not path.is_file():
        raise _error(f"lesson path is not a regular file: {path.name}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _error(f"lesson file {path.name} cannot be read") from exc
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise _error(f"lesson file {path.name} has no frontmatter opening")
    closing_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip("\r\n") == "---"
        ),
        None,
    )
    if closing_index is None:
        raise _error(f"lesson file {path.name} has no frontmatter closing marker")
    fields: dict[str, object] = {}
    for line_number, line in enumerate(lines[1:closing_index], start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace() or ":" not in line:
            raise _error(f"lesson frontmatter line {line_number} is not a field")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise _error(f"lesson frontmatter line {line_number} has an invalid key")
        if key in fields:
            raise _error(f"lesson frontmatter contains duplicate field {key!r}")
        fields[key] = _parse_value(raw_value.strip(), key)
    return (
        fields,
        lines[0],
        "".join(lines[1:closing_index]),
        lines[closing_index],
        "".join(lines[closing_index + 1 :]),
    )


def _parse_record(path: Path) -> LessonRecord:
    fields, opening, frontmatter, closing, body = _read_document(path)
    validated = _validate_record_fields(fields, path)
    body = _validate_body(body)
    return LessonRecord(
        **validated,
        body=body,
        path=path,
        _opening=opening,
        _frontmatter_lines=tuple(frontmatter.splitlines(keepends=True)),
        _closing=closing,
    )


def load_lesson(
    artifacts_root_or_path: Path,
    lesson_id: str | None = None,
    *,
    repo_root: Path | None = None,
) -> LessonRecord:
    """Load and validate a lesson from an external artifact root or file path."""

    if lesson_id is None:
        path = Path(artifacts_root_or_path).expanduser()
        if path.is_dir() and path.name == LESSON_DIRNAME:
            raise _error("a lesson id is required when loading a lessons directory")
        if not path.is_absolute():
            path = path.resolve()
    else:
        path = lesson_path(artifacts_root_or_path, lesson_id, repo_root=repo_root)
    path = _validate_lesson_path(path, repo_root)
    if path.is_symlink() or not path.is_file():
        raise _error(f"lesson does not exist: {path.name}")
    return _parse_record(path)


def _render_scalar(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        implicit_scalar = value.lower() in {"null", "true", "false"} or re.fullmatch(
            r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", value
        )
        if _SAFE_BARE_RE.fullmatch(value) and implicit_scalar is None:
            return value
        return json.dumps(value, ensure_ascii=False)
    raise _error("lesson frontmatter contains an unsupported value")


def _render_mapping(value: Mapping[str, object]) -> str:
    return (
        "{"
        + ", ".join(f"{key}: {_render_scalar(item)}" for key, item in value.items())
        + "}"
    )


def _replace_fields(record: LessonRecord, updates: Mapping[str, object]) -> str:
    lines = list(record._frontmatter_lines)
    for key, value in updates.items():
        matches = [
            index
            for index, line in enumerate(lines)
            if line.split(":", 1)[0].strip() == key
        ]
        if len(matches) != 1:
            raise _error(f"lesson frontmatter field {key!r} cannot be updated safely")
        index = matches[0]
        ending = (
            "\r\n"
            if lines[index].endswith("\r\n")
            else "\n"
            if lines[index].endswith("\n")
            else ""
        )
        if key == "state":
            rendered = str(value)
        else:
            rendered = (
                _render_mapping(value)
                if isinstance(value, Mapping)
                else _render_scalar(value)
            )
        lines[index] = f"{key}: {rendered}{ending}"
    return record._opening + "".join(lines) + record._closing + record.body


def _write_atomic(path: Path, text: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise _error(f"lesson path is not a regular file: {path.name}")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise _error(f"lesson directory is not a regular directory: {parent.name}")
    os.chmod(parent, LESSON_DIR_MODE)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent, prefix=".lesson-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, LESSON_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, LESSON_FILE_MODE)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def save_lesson(record: LessonRecord) -> Path:
    """Validate and atomically persist a lesson record."""

    _validate_record_fields(
        {
            "id": record.id,
            "summary": record.summary,
            "state": record.state,
            "detector": record.detector,
            "key": record.key,
            "evidence": record.evidence,
            "routing": record.routing,
            "backtest": record.backtest,
            "guard": record.guard,
        },
        record.path,
    )
    _validate_body(record.body)
    existing = load_lesson(record.path)
    if record.as_dict() != existing.as_dict() or record.body != existing.body:
        raise _error(
            "saving a modified lesson is not an explicit lifecycle transition",
            "use the named operator transition command",
        )
    _write_atomic(record.path, _replace_fields(record, {}))
    return record.path


def _operator_token(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(f"{field} must be a non-empty string")
    safe = redact_text(value)
    if not safe or len(safe) > MAX_FIELD_LENGTH or not _SAFE_FIELD_RE.fullmatch(safe):
        raise _error(f"{field} must be a bounded single-line string")
    return safe


def _utc_now(value: datetime | str | None = None) -> str:
    if value is None:
        current = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        current = value
    else:
        try:
            current = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise _error("applied_at must be an ISO-8601 timestamp") from None
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _transition_target(state: str, layer: str | None) -> str:
    if state == "applied":
        if layer is None:
            raise _error("an applied transition requires a routing layer")
        return f"applied:{_validate_layer(layer)}"
    validated = _validate_state(state)
    if not validated.startswith("applied:"):
        if layer is not None:
            raise _error("a routing layer may only accompany an applied state")
        return validated
    applied_layer = validated.split(":", 1)[1]
    if layer is not None and _validate_layer(layer) != applied_layer:
        raise _error("state layer and --layer do not agree")
    return f"applied:{_validate_layer(applied_layer)}"


def _transition(
    artifacts_root_or_path: Path,
    lesson_id: str | None,
    target_state: str,
    *,
    layer: str | None = None,
    bead: object = None,
    applied_at: datetime | str | None = None,
    operator: bool = False,
    repo_root: Path | None = None,
) -> LessonRecord:
    if not isinstance(operator, bool):
        raise _error("operator must be a boolean")
    target = _transition_target(target_state, layer)
    if lesson_id is None:
        path = Path(artifacts_root_or_path).expanduser()
        if not path.is_file():
            raise _error("a lesson file path is required when no lesson id is supplied")
    else:
        path = lesson_path(artifacts_root_or_path, lesson_id, repo_root=repo_root)
    record = load_lesson(path, repo_root=repo_root)
    current = record.state
    if current == target:
        if target.startswith("applied:"):
            if layer is not None and layer != record.layer:
                raise _error("an applied lesson cannot change layers")
            if (
                bead is not None
                and _operator_token(bead, "bead") != record.routing["bead"]
            ):
                raise _error("an applied lesson cannot change its bead")
        return record
    legal = {
        "draft": {"accepted"},
        "accepted": {target} if target.startswith("applied:") else set(),
        **{f"applied:{item}": TERMINAL_STATES for item in ROUTING_LAYERS},
    }
    if target not in legal.get(current, set()):
        raise _error(
            f"illegal lesson transition: {current} -> {target}",
            "use the explicit operator command for the next lifecycle step",
        )
    if not operator:
        raise _error(
            f"refusing lesson transition {current} -> {target} without an explicit operator command",
            "invoke the named operator command; automatic state advancement is forbidden",
        )
    updates: dict[str, object] = {"state": target}
    if target.startswith("applied:"):
        applied_layer = target.split(":", 1)[1]
        safe_bead = _operator_token(bead, "bead")
        routing = dict(record.routing)
        routing.update(
            {
                "applied": applied_layer,
                "applied_at": _utc_now(applied_at),
                "bead": safe_bead,
            }
        )
        updates["routing"] = routing
    _validate_routing(updates.get("routing", record.routing))
    _write_atomic(path, _replace_fields(record, updates))
    return load_lesson(path, repo_root=repo_root)


def transition_lesson(
    artifacts_root_or_path: Path,
    lesson_id: str | None,
    target_state: str,
    *,
    layer: str | None = None,
    bead: object = None,
    applied_at: datetime | str | None = None,
    operator: bool = False,
    repo_root: Path | None = None,
) -> LessonRecord:
    """Apply one validated lifecycle transition.

    The generic operation refuses to move a lesson unless ``operator`` is
    true. Named lifecycle commands set that flag after the operator has chosen
    the command explicitly.
    """

    return _transition(
        artifacts_root_or_path,
        lesson_id,
        target_state,
        layer=layer,
        bead=bead,
        applied_at=applied_at,
        operator=operator,
        repo_root=repo_root,
    )


def accept_lesson(
    artifacts_root_or_path: Path,
    lesson_id: str | None = None,
    *,
    operator: bool = True,
    repo_root: Path | None = None,
) -> LessonRecord:
    """Accept a drafted lesson through the explicit operator operation."""

    return transition_lesson(
        artifacts_root_or_path,
        lesson_id,
        "accepted",
        operator=operator,
        repo_root=repo_root,
    )


def apply_lesson(
    artifacts_root_or_path: Path,
    lesson_id: str | None = None,
    *,
    layer: str,
    bead: object,
    applied_at: datetime | str | None = None,
    operator: bool = True,
    repo_root: Path | None = None,
) -> LessonRecord:
    """Record an operator-applied routing layer and its owning bead."""

    return transition_lesson(
        artifacts_root_or_path,
        lesson_id,
        f"applied:{layer}",
        layer=layer,
        bead=bead,
        applied_at=applied_at,
        operator=operator,
        repo_root=repo_root,
    )


def resolve_lesson(
    artifacts_root_or_path: Path,
    lesson_id: str | None = None,
    *,
    operator: bool = True,
    repo_root: Path | None = None,
) -> LessonRecord:
    return transition_lesson(
        artifacts_root_or_path,
        lesson_id,
        "resolved",
        operator=operator,
        repo_root=repo_root,
    )


def escalate_lesson(
    artifacts_root_or_path: Path,
    lesson_id: str | None = None,
    *,
    operator: bool = True,
    repo_root: Path | None = None,
) -> LessonRecord:
    return transition_lesson(
        artifacts_root_or_path,
        lesson_id,
        "escalated",
        operator=operator,
        repo_root=repo_root,
    )


def retire_lesson(
    artifacts_root_or_path: Path,
    lesson_id: str | None = None,
    *,
    operator: bool = True,
    repo_root: Path | None = None,
) -> LessonRecord:
    return transition_lesson(
        artifacts_root_or_path,
        lesson_id,
        "retired",
        operator=operator,
        repo_root=repo_root,
    )


def list_lessons(
    artifacts_root: Path,
    *,
    state: str | None = None,
    repo_root: Path | None = None,
) -> tuple[LessonRecord, ...]:
    """List valid lessons, optionally filtering by state category."""

    directory = lessons_dir(artifacts_root, repo_root=repo_root)
    if state is not None:
        category = state_category(state)
    else:
        category = None
    if not directory.exists():
        if directory.is_symlink():
            raise _error("lessons path may not be a symbolic link")
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise _error("lessons path is not a regular directory")
    records: list[LessonRecord] = []
    for path in sorted(directory.glob("L-*.md")):
        record = load_lesson(path, repo_root=repo_root)
        if category is None or state_category(record.state) == category:
            records.append(record)
    return tuple(records)


__all__ = [
    "LESSON_DIRNAME",
    "LESSON_DIR_MODE",
    "LESSON_FILE_MODE",
    "LESSON_ID_RE",
    "LESSON_STATES",
    "LessonRecord",
    "ROUTING_LAYER_ORDER",
    "ROUTING_LAYERS",
    "STATE_FILTERS",
    "TERMINAL_STATES",
    "VALID_STATES",
    "accept_lesson",
    "apply_lesson",
    "escalate_lesson",
    "lesson_path",
    "lessons_dir",
    "list_lessons",
    "load_lesson",
    "retire_lesson",
    "save_lesson",
    "state_category",
    "transition_lesson",
    "resolve_lesson",
]
