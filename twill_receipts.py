#!/usr/bin/env python3
"""Read the optional ``twill-friction-receipt/v1`` SessionEnd input."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCHEMA = "twill-friction-receipt/v1"
READER_NAME = "friction_receipts"
RECEIPTS_DIR_NAME = "friction-receipts"
MAX_SESSION_ID = 128
MAX_REASON = 128
MAX_TIMESTAMP = 32
MAX_CWD = 160
MAX_DENIAL_TIMESTAMP = 32
MAX_DENIAL_FIELD = 128
MAX_ERROR_SIGNATURE = 240
MAX_ITEMS = 64
MAX_DENIALS = 128
_SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(
        r"-{5}BEGIN (?:[A-Z]+ )?PRIVATE KEY-{5}.*?-{5}END "
        r"(?:[A-Z]+ )?PRIVATE KEY-{5}",
        re.DOTALL,
    ),
)


@dataclass(frozen=True)
class FrictionReceipt:
    schema: str
    session_id: str
    ended_at: str
    reason: str
    cwd: str
    rules_consulted: tuple[str, ...]
    denials: tuple[dict[str, str], ...]
    unresolved_errors: tuple[dict[str, str], ...]
    ended_mid_task: bool


def _has_secret(value: object) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_PATTERNS)
    if isinstance(value, dict):
        return any(_has_secret(key) or _has_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_secret(item) for item in value)
    return False


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate receipt key")
        result[key] = value
    return result


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a string list")
    if len(value) > MAX_ITEMS:
        raise ValueError(f"{field} is too large")
    if any(len(item) > MAX_REASON for item in value):
        raise ValueError(f"{field} item is too large")
    return tuple(value)


def _records(value: object, field: str, keys: set[str]) -> tuple[dict[str, str], ...]:
    maximum = MAX_DENIALS if field == "denials" else MAX_ITEMS
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} must be a bounded object list")
    records: list[dict[str, str]] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != keys
            or not all(isinstance(part, str) for part in item.values())
        ):
            raise ValueError(f"invalid {field} record")
        if field == "denials":
            bounds = {
                "ts": MAX_DENIAL_TIMESTAMP,
                "rule_id": MAX_DENIAL_FIELD,
                "tool": MAX_DENIAL_FIELD,
            }
        else:
            bounds = {"kind": MAX_REASON, "signature": MAX_ERROR_SIGNATURE}
        if any(len(item[key]) > bound for key, bound in bounds.items()):
            raise ValueError(f"{field} record is too large")
        records.append(dict(item))
    return tuple(records)


def receipt_directory() -> Path:
    """Resolve the producer's default receipt directory without creating it."""

    state_home = os.environ.get("XDG_STATE_HOME")
    root = (
        Path(state_home).expanduser()
        if state_home
        else Path.home() / ".local" / "state"
    )
    return root / "twill" / RECEIPTS_DIR_NAME


def receipt_path(session_id: str, directory: Path | None = None) -> Path:
    """Return the per-session receipt path, rejecting unsafe session ids."""

    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > MAX_SESSION_ID
    ):
        raise ValueError("invalid receipt session id")
    if Path(session_id).name != session_id or session_id in {".", ".."}:
        raise ValueError("receipt session id must be a single path component")
    return (directory or receipt_directory()) / f"{session_id}.json"


def parse_receipt(value: object) -> FrictionReceipt:
    if not isinstance(value, dict):
        raise ValueError("receipt must be an object")
    expected = {
        "schema",
        "session_id",
        "ended_at",
        "reason",
        "cwd",
        "rules_consulted",
        "denials",
        "unresolved_errors",
        "ended_mid_task",
    }
    if set(value) != expected:
        raise ValueError("receipt fields do not match v1")
    if value["schema"] != SCHEMA:
        raise ValueError("unsupported receipt schema")
    scalar_fields = ("session_id", "ended_at", "reason", "cwd")
    if not all(isinstance(value[field], str) for field in scalar_fields):
        raise ValueError("receipt scalar field is not a string")
    scalar_bounds = {
        "session_id": MAX_SESSION_ID,
        "ended_at": MAX_TIMESTAMP,
        "reason": MAX_REASON,
        "cwd": MAX_CWD,
    }
    if any(len(value[field]) > bound for field, bound in scalar_bounds.items()):
        raise ValueError("receipt scalar field is too large")
    if not isinstance(value["ended_mid_task"], bool):
        raise ValueError("ended_mid_task must be boolean")
    if _has_secret(value):
        raise ValueError("receipt contains an unredacted secret")
    return FrictionReceipt(
        schema=SCHEMA,
        session_id=value["session_id"],
        ended_at=value["ended_at"],
        reason=value["reason"],
        cwd=value["cwd"],
        rules_consulted=_strings(value["rules_consulted"], "rules_consulted"),
        denials=_records(value["denials"], "denials", {"ts", "rule_id", "tool"}),
        unresolved_errors=_records(
            value["unresolved_errors"], "unresolved_errors", {"kind", "signature"}
        ),
        ended_mid_task=value["ended_mid_task"],
    )


def read_receipt(path: Path) -> FrictionReceipt:
    """Read one receipt, rejecting malformed or unsafe records."""

    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=_strict_object)
    return parse_receipt(value)


def read_session_receipt(
    session_id: str, directory: Path | None = None
) -> FrictionReceipt | None:
    """Read the valid receipt belonging to one transcript session.

    A missing, malformed, unsafe, or mismatched receipt is treated as absent;
    the transcript remains a complete input on its own.
    """

    try:
        path = receipt_path(session_id, directory)
        if path.is_symlink() or not path.is_file():
            return None
        receipt = read_receipt(path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return receipt if receipt.session_id == session_id else None


def iter_receipts(directory: Path) -> Iterator[FrictionReceipt]:
    """Yield valid receipts in deterministic filename order; skip bad files."""

    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            yield read_receipt(path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue


def receipts_live(directory: Path | None = None) -> bool:
    """Whether the optional input has at least one valid receipt."""

    return next(iter(iter_receipts(directory or receipt_directory())), None) is not None


def register_reader() -> object:
    """Build the doctor registration without mutating the reader registry."""

    from twill_reader import ReaderRegistration

    return ReaderRegistration(READER_NAME, receipts_live)
