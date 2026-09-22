"""Stable machine-facing output and exit-code contract for the TWILL CLI."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, TextIO


SCHEMA_VERSION = 1

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_LOCK_HELD = 3
EXIT_VALIDATION_FAILURE = 4

ERROR_CODES = frozenset(
    {
        EXIT_RUNTIME_ERROR,
        EXIT_USAGE_ERROR,
        EXIT_LOCK_HELD,
        EXIT_VALIDATION_FAILURE,
    }
)


_CREDENTIAL_PATTERNS = (
    (
        re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_-]{12,}"),
        "<redacted:github-token>",
    ),
    (
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "<redacted:aws-access-key>",
    ),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        "Bearer <redacted:bearer-token>",
    ),
    (
        re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}"),
        "<redacted:api-key>",
    ),
    (
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
        "<redacted:slack-token>",
    ),
    (
        re.compile(
            r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|token)\s*[:=]\s*)(['\"]?)[^\s,'\"]+"
        ),
        r"\1\2<redacted:secret>",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z ]+ PRIVATE KEY-----.*?-----END [A-Z ]+ PRIVATE KEY-----",
            re.DOTALL,
        ),
        "<redacted:private-key>",
    ),
)


class CliError(Exception):
    """An expected CLI failure that maps directly to a documented exit code."""

    def __init__(self, code: int, message: str, hint: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"invalid CLI error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


class UsageError(CliError):
    def __init__(
        self,
        message: str,
        hint: str = "run 'twill <verb> --help' for usage",
    ) -> None:
        super().__init__(EXIT_USAGE_ERROR, message, hint)


class LockHeldError(CliError):
    def __init__(
        self,
        pid: int,
        since: str,
        hint: str = "wait for the active TWILL run to finish",
    ) -> None:
        super().__init__(EXIT_LOCK_HELD, f"lock held by pid {pid} since {since}", hint)


class ValidationError(CliError):
    def __init__(
        self,
        message: str,
        hint: str = "run 'twill doctor' to inspect the failing validation",
    ) -> None:
        super().__init__(EXIT_VALIDATION_FAILURE, message, hint)


def _safe_text(value: object) -> str:
    """Make operator-facing text safe without exposing credential-shaped values."""

    text = str(value).replace("\x00", "").strip()
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def generated_at() -> str:
    """Return the contract timestamp format: an RFC 3339 UTC instant."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def success_envelope(data: Any, warnings: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    """Build the one-object envelope emitted by successful ``--json`` reads."""

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at(),
        "data": data,
        "warnings": [_safe_text(warning) for warning in warnings],
    }


def error_envelope(
    code: int, message: str, hint: str = ""
) -> dict[str, dict[str, object]]:
    """Build the one-object envelope emitted by failed ``--json`` commands."""

    if code not in ERROR_CODES:
        raise ValueError(f"invalid CLI error code: {code}")
    return {
        "error": {
            "code": code,
            "message": _safe_text(message),
            "hint": _safe_text(hint),
        }
    }


def emit_success(
    data: Any,
    *,
    json_mode: bool,
    warnings: list[str] | tuple[str, ...] = (),
    stdout: TextIO | None = None,
) -> None:
    """Write a success envelope in JSON mode, or human-readable warnings otherwise."""

    output = stdout or sys.stdout
    if json_mode:
        print(json.dumps(success_envelope(data, warnings), sort_keys=True), file=output)
    for warning in warnings:
        if not json_mode:
            print(f"warning: {_safe_text(warning)}", file=sys.stderr)


def emit_error(
    code: int,
    message: str,
    hint: str = "",
    *,
    json_mode: bool,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    """Write exactly one machine envelope or a human-readable safe error."""

    output = stdout or sys.stdout
    diagnostics = stderr or sys.stderr
    if json_mode:
        print(json.dumps(error_envelope(code, message, hint), sort_keys=True), file=output)
        return
    print(f"twill: error: {_safe_text(message)}", file=diagnostics)
    if hint:
        print(f"hint: {_safe_text(hint)}", file=diagnostics)
