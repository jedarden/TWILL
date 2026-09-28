#!/usr/bin/env python3
"""Read the optional ``twill-friction-receipt/v1`` SessionEnd input."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCHEMA = "twill-friction-receipt/v1"
_SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{30,}"),
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
    if len(value) > 64:
        raise ValueError(f"{field} is too large")
    return tuple(value)


def _records(value: object, field: str, keys: set[str]) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list) or len(value) > 128:
        raise ValueError(f"{field} must be a bounded object list")
    records: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != keys or not all(
            isinstance(part, str) for part in item.values()
        ):
            raise ValueError(f"invalid {field} record")
        records.append(dict(item))
    return tuple(records)


def parse_receipt(value: object) -> FrictionReceipt:
    if not isinstance(value, dict):
        raise ValueError("receipt must be an object")
    expected = {
        "schema", "session_id", "ended_at", "reason", "cwd", "rules_consulted",
        "denials", "unresolved_errors", "ended_mid_task",
    }
    if set(value) != expected:
        raise ValueError("receipt fields do not match v1")
    if value["schema"] != SCHEMA:
        raise ValueError("unsupported receipt schema")
    scalar_fields = ("session_id", "ended_at", "reason", "cwd")
    if not all(isinstance(value[field], str) for field in scalar_fields):
        raise ValueError("receipt scalar field is not a string")
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


def iter_receipts(directory: Path) -> Iterator[FrictionReceipt]:
    """Yield valid receipts in deterministic filename order; skip bad files."""

    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            yield read_receipt(path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue


def receipts_live(directory: Path) -> bool:
    """Whether the optional input has at least one valid receipt."""

    return next(iter(iter_receipts(directory)), None) is not None
