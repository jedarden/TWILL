"""Read the optional org-rule-guard denial log (plan §6.5).

The hook owns the log and its rotation.  This module only reads complete,
valid records from the backup followed by the active file.  It does not infer
denials from transcript text, which can contain quoted rule descriptions.
Callers must redact fields again before persisting them as observations.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator


READER_NAME = "org_rule_guard_denials"
LOG_NAME = "denials.jsonl"
_FIELDS = frozenset({"ts", "rule_id", "tool", "cwd", "session_id", "fragment"})
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


@dataclass(frozen=True)
class Denial:
    ts: str
    rule_id: str
    tool: str
    cwd: str
    session_id: str
    fragment: str


def denial_log_path() -> Path:
    """Resolve the hook's state location, including its test/worker override."""

    override = os.environ.get("ORG_RULE_GUARD_STATE_DIR")
    if override:
        directory = Path(override).expanduser()
    else:
        state_home = os.environ.get("XDG_STATE_HOME")
        directory = (
            Path(state_home).expanduser() if state_home else Path.home() / ".local/state"
        ) / "org-rule-guard"
    return directory / LOG_NAME


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate denial field")
        result[key] = value
    return result


def parse_denial(value: object) -> Denial:
    """Validate one record against the hook's six-field JSONL contract."""

    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("denial fields do not match the hook contract")
    if not all(isinstance(value[field], str) for field in _FIELDS):
        raise ValueError("denial fields must be strings")
    ts = value["ts"]
    if not _UTC_SECOND.fullmatch(ts):
        raise ValueError("denial timestamp must be UTC to the second")
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid denial timestamp") from exc
    if not value["rule_id"] or not value["tool"]:
        raise ValueError("denial rule and tool must be nonempty")
    if any(len(value[field]) > 512 for field in ("tool", "cwd", "session_id")):
        raise ValueError("denial metadata exceeds the hook bound")
    if len(value["fragment"]) > 80:
        raise ValueError("denial fragment exceeds the hook bound")
    return Denial(**value)


def iter_denials(log_path: Path | None = None) -> Iterator[Denial]:
    """Yield valid complete lines oldest first; tolerate absent and torn logs.

    The producer can rotate between opening the backup and active file.  A
    disappeared file is therefore ordinary absence.  Other read errors are
    propagated so doctor can distinguish an unreadable input from an absent
    one when it evaluates :func:`denials_live`.
    """

    active = Path(log_path) if log_path is not None else denial_log_path()
    backup = active.with_name(active.name + ".1")
    for path in (backup, active):
        try:
            with path.open("rb") as handle:
                for raw in handle:
                    if not raw.endswith(b"\n"):
                        continue
                    try:
                        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
                        yield parse_denial(value)
                    except (UnicodeDecodeError, ValueError, TypeError):
                        continue
        except FileNotFoundError:
            continue


def denials_live(log_path: Path | None = None) -> bool:
    """Whether the rolling window contains at least one valid denial."""

    return next(iter_denials(log_path), None) is not None


def register_reader() -> object:
    """Build a doctor reader registration without changing global state."""

    from twill_reader import ReaderRegistration

    return ReaderRegistration(READER_NAME, denials_live)
