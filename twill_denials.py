"""Read the optional org-rule-guard denial log (plan §6.5).

The hook owns the log and its rotation.  This module only reads complete,
valid records from the backup followed by the active file.  It does not infer
denials from transcript text, which can contain quoted rule descriptions.
Callers must redact fields again before persisting them as observations.

:func:`materialize_denials` is the D-04 side of that contract: it refreshes
the ``hook_denial`` evidence table (schema migration 8) from one wholesale
read, replacing the previous window so the table never blends two reads.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from twill_redactor import redact_text


READER_NAME = "org_rule_guard_denials"
LOG_NAME = "denials.jsonl"
EVIDENCE_TABLE = "hook_denial"
# The hook bounds tool/cwd/session_id at 512 and the fragment at 80, but the
# contract pins no bound for rule_id — bound it here so a stored row can
# never exceed the corpus' key budget it feeds ('hook-denial:' + rule_id is
# truncated to 240 by the detector runner either way).
MAX_RULE_ID_LENGTH = 240
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


def denial_log_available(log_path: Path | None = None) -> bool:
    """Whether either file of the rolling window exists at all.

    Absence is the one state :func:`iter_denials` cannot distinguish from an
    empty window, and the difference decides D-04's skip: a log that does
    not exist says nothing about denials, while an existing-but-empty window
    is a live input honestly reporting zero of them.
    """

    active = Path(log_path) if log_path is not None else denial_log_path()
    return active.exists() or active.with_name(active.name + ".1").exists()


def denial_evidence_rows(log_path: Path | None = None) -> list[tuple[str, str, str, str]]:
    """Read the window into evidence rows: (rule_id, tool, ts_utc, session_id).

    Fields are re-redacted on the way out — the hook redacts its fragment,
    but the reader's contract is that callers re-assert redaction before
    anything persists (plan §8.3).  The hook's ``...Z`` timestamp is
    normalized to the corpus ``ts_utc`` shape so D-04's window predicates
    compare like every other detector's.
    """

    rows: list[tuple[str, str, str, str]] = []
    for denial in iter_denials(log_path):
        normalized_ts = datetime.fromisoformat(denial.ts.replace("Z", "+00:00"))
        rows.append(
            (
                redact_text(denial.rule_id)[:MAX_RULE_ID_LENGTH],
                redact_text(denial.tool)[:512],
                normalized_ts.isoformat(),
                redact_text(denial.session_id)[:512],
            )
        )
    return rows


def materialize_denials(
    connection: sqlite3.Connection, log_path: Path | None = None
) -> int:
    """Refresh D-04's evidence table from the rolling denial log.

    The whole table is replaced in one transaction, so it always mirrors
    exactly the window this call read — rotation truncates it the same way
    the log was truncated, and no record is ever counted twice.  A log that
    does not exist raises ``FileNotFoundError`` (the caller skips D-04: an
    absent input says nothing about denials); other read errors propagate
    for the same reason.
    """

    if not denial_log_available(log_path):
        raise FileNotFoundError(f"org-rule-guard denial log not found: {log_path}")
    rows = denial_evidence_rows(log_path)
    with connection:
        connection.execute(f"DELETE FROM {EVIDENCE_TABLE}")
        connection.executemany(
            f"INSERT INTO {EVIDENCE_TABLE}(rule_id, tool, ts_utc, session_id) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def register_reader() -> object:
    """Build a doctor reader registration without changing global state."""

    from twill_reader import ReaderRegistration

    return ReaderRegistration(READER_NAME, denials_live)
