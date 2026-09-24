#!/usr/bin/env python3
"""Phase 0 implementation of the TWILL transcript-to-digest pipeline.

The first version deliberately keeps the pipeline small and deterministic.  It
does not retain raw transcript lines and has no artifact writer:
reader -> cursor -> redactor -> SQLite -> one activity detector -> digest.
Ingest is cursor-bookkept (plan §6.2 steps 3-6, §8.1 EC-02..EC-05): appended
files resume at ``last_offset``, rewritten or shrunk files reparse from zero,
a torn final line is left for the next run, and a vanished file is flagged
without losing its observations.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Iterator, Sequence

import twill_detectors
import twill_lessons
import twill_measure
import twill_ranker
import twill_schema
import twill_cursor
import twill_doctor
from codex_reader import CodexRolloutLineParser, CodexRolloutReader, TokenUsage
from twill_reader import ClaudeCodeLineParser, MessageUsage
from twill_config import (
    ConfigError,
    TwillConfig,
    load_config,
    parse_duration,
)
from twill_contract import (
    EXIT_RUNTIME_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    EXIT_VALIDATION_FAILURE,
    CliError,
    UsageError,
    emit_error,
    emit_success,
)
from twill_lock import StateLock
from twill_redactor import Redactor, redact as _redact, redact_text
from twill_status import read_status, record_stage


MAX_EXCERPT_LENGTH = 240
SIGNATURE_INPUT_LIMIT = 400
SIGNATURE_HASH_LENGTH = 12
MUTATING_VERBS = frozenset({"ingest", "detect", "rank", "accept", "apply", "measure"})

_SIGNATURE_SUBSTITUTIONS = (
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "<uuid>",
    ),
    (re.compile(r"\b(?:[0-9a-f]{40}|[0-9a-f]{64})\b", re.IGNORECASE), "<sha>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"\b[0-9a-f]{7,64}\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"/(?:home|tmp|var|Users)/[^\s:'\"]+", re.IGNORECASE), "<path>"),
    (
        re.compile(r"(?<![A-Za-z0-9_/])/(?:[A-Za-z0-9._@+-]+/)*[A-Za-z0-9._@+-]+"),
        "<path>",
    ),
    (
        re.compile(
            r"(?P<prefix>^|[\s(\"'=,:])"
            r"(?:(?:\./|\.\./)[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*"
            r"|(?:[A-Za-z0-9._@+-]+/)+[A-Za-z0-9._@+-]*[._0-9-][A-Za-z0-9._@+-]*)"
        ),
        lambda match: f"{match.group('prefix')}<path>",
    ),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def signature(text: object | None) -> str:
    """Normalize a redacted error text without retaining volatile values."""

    normalized = redact_text(text).strip()[:SIGNATURE_INPUT_LIMIT]
    for pattern, replacement in _SIGNATURE_SUBSTITUTIONS:
        normalized = pattern.sub(replacement, normalized)
    return normalized.strip()


def h12(text: str) -> str:
    """Return the compact SHA-256 fingerprint used for error signatures."""

    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[
        :SIGNATURE_HASH_LENGTH
    ]


def normalize_error_signature(text: object | None) -> str:
    """Descriptive alias for :func:`signature`."""

    return signature(text)


def hash_error_signature(text: object | None) -> str:
    """Normalize and hash one error text."""

    return h12(signature(text))


# Plan §14: `twill detect [--window 30d] ...` — the default analysis window.
DEFAULT_DETECT_WINDOW_DAYS = 30
SECONDS_PER_DAY = 86400


# Interim Phase 0 working tables.  The v1 corpus schema (cursor, observation,
# cluster, ...) lives in twill_schema and is applied to every connection this
# Store opens; observation in particular is now the v1 shape.
SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source_path TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript_event (
    event_id INTEGER PRIMARY KEY,
    session_key TEXT NOT NULL REFERENCES session(session_key),
    source_line INTEGER NOT NULL,
    event_index INTEGER NOT NULL,
    ts_utc TEXT NOT NULL,
    ts_local TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    signature TEXT,
    sig_hash TEXT,
    cwd TEXT,
    UNIQUE(session_key, source_line, event_index)
);
"""


def _ensure_transcript_event_signature_columns(
    connection: sqlite3.Connection,
) -> None:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(transcript_event)")
    }
    for column in ("signature", "sig_hash"):
        if column not in columns:
            connection.execute(f"ALTER TABLE transcript_event ADD COLUMN {column} TEXT")


def _backfill_transcript_event_signatures(
    connection: sqlite3.Connection,
) -> None:
    with connection:
        rows = connection.execute(
            "SELECT event_id, text FROM transcript_event "
            "WHERE signature IS NULL OR sig_hash IS NULL"
        ).fetchall()
        for event_id, text in rows:
            normalized = signature(text)
            connection.execute(
                "UPDATE transcript_event SET signature = ?, sig_hash = ? WHERE event_id = ?",
                (normalized, h12(normalized), event_id),
            )
        connection.execute(
            """
            UPDATE observation
               SET signature = (
                       SELECT e.signature
                         FROM transcript_event AS e
                         JOIN session AS s ON s.session_key = e.session_key
                        WHERE s.session_id = observation.session_id
                          AND e.ts_utc = observation.ts_utc
                          AND e.ts_local = observation.ts_local
                          AND e.text = observation.excerpt
                          AND e.cwd = observation.cwd
                        LIMIT 1
                   ),
                   sig_hash = (
                       SELECT e.sig_hash
                         FROM transcript_event AS e
                         JOIN session AS s ON s.session_key = e.session_key
                        WHERE s.session_id = observation.session_id
                          AND e.ts_utc = observation.ts_utc
                          AND e.ts_local = observation.ts_local
                          AND e.text = observation.excerpt
                          AND e.cwd = observation.cwd
                        LIMIT 1
                   )
             WHERE kind = 'session_activity'
               AND (signature IS NULL OR sig_hash IS NULL)
            """
        )


@dataclass(frozen=True)
class TranscriptEvent:
    session_id: str
    timestamp: str
    kind: str
    text: str
    source_line: int
    event_index: int
    cwd: str | None = None


@dataclass(frozen=True)
class SessionUsage:
    session_id: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None
    wall_seconds: int | None = None
    messages: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": self.cost_usd,
            "wall_seconds": self.wall_seconds,
            "messages": self.messages,
        }


@dataclass(frozen=True)
class SessionData:
    path: Path
    session_id: str
    source_kind: str
    events: tuple[TranscriptEvent, ...]
    usage: SessionUsage | None = None
    usage_rows: tuple[MessageUsage, ...] = ()

    @property
    def usage_records(self) -> tuple[MessageUsage, ...]:
        return self.usage_rows

    @property
    def message_usage(self) -> tuple[MessageUsage, ...]:
        return self.usage_rows

    @property
    def session_usage(self) -> SessionUsage | None:
        return self.usage


def redact(text: object | None, content_fences: Sequence[str] = ()) -> str:
    """Compatibility export for the bounded persistence excerpt redactor."""

    return _redact(text, content_fences)


def _text_values(value: object) -> Iterator[str]:
    """Yield text from the small set of transcript shapes used by both clients."""

    if isinstance(value, str):
        if value.strip():
            yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _text_values(item)
        return
    if not isinstance(value, dict):
        return

    # Content blocks and generic fixture records use these names.  Restricting
    # traversal to them avoids accidentally persisting metadata or whole tool
    # input objects as observations.
    for key in ("text", "message", "content", "output", "stderr", "stdout"):
        if key in value:
            yield from _text_values(value[key])


def _event_texts(record: dict[str, object]) -> Iterator[str]:
    record_type = record.get("type")

    # Claude Code JSONL: user/assistant records hold content below message.
    if record_type in {"user", "assistant"} and "message" in record:
        message = record["message"]
        if isinstance(message, dict):
            yield from _text_values(message.get("content"))
        else:
            yield from _text_values(message)
        return

    # Codex rollout JSONL: response_item content is a list of text blocks.
    if record_type == "response_item":
        payload = record.get("payload")
        if isinstance(payload, dict):
            yield from _text_values(payload.get("content"))
        return

    # Codex event messages and small test fixtures.
    if record_type == "event_msg":
        payload = record.get("payload")
        if isinstance(payload, dict):
            yield from _text_values(payload.get("message"))
        return

    for key in ("text", "message", "content"):
        if key in record:
            yield from _text_values(record[key])


def _session_id(record: dict[str, object], fallback: str) -> str:
    for key in ("sessionId", "session_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    payload = record.get("payload")
    if isinstance(payload, dict):
        for key in ("session_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return fallback


def _timestamp(record: dict[str, object]) -> str:
    value = record.get("timestamp")
    if isinstance(value, str) and value:
        return value
    payload = record.get("payload")
    if isinstance(payload, dict):
        value = payload.get("timestamp")
        if isinstance(value, str) and value:
            return value
    return datetime.now(timezone.utc).isoformat()


def _source_kind(path: Path) -> str:
    path_text = str(path)
    if "/.claude/" in path_text:
        return "claude"
    if "/.codex/" in path_text:
        return "codex"
    return "jsonl"


def _parse_codex_scan(
    path: Path,
    scan: twill_cursor.LineScan,
    fallback_session_id: str,
    source_kind: str,
) -> tuple[SessionData, int]:
    parser = CodexRolloutLineParser()
    session_id = fallback_session_id

    if scan.start_offset and scan.lines:
        prefix_end = scan.lines[0][0]
        prefix = twill_cursor.scan_lines(path, 0)
        for source_line, line in prefix.lines:
            if source_line >= prefix_end:
                break
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                session_id = _session_id(record, session_id)
                parser.parse_line(line, source_line)

    session_id = parser.session_id or session_id
    normalized_events = []
    invalid_lines = 0
    for source_line, line in scan.lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if not isinstance(record, dict):
            invalid_lines += 1
            continue
        session_id = _session_id(record, session_id)
        normalized_events.extend(parser.parse_line(line, source_line))

    session_id = parser.session_id or session_id
    events = tuple(
        TranscriptEvent(
            session_id=event.session_id or session_id,
            timestamp=event.timestamp or datetime.now(timezone.utc).isoformat(),
            kind=event.kind,
            text=event.text,
            source_line=event.source_line,
            event_index=event.event_index,
            cwd=event.cwd,
        )
        for event in normalized_events
    )
    usage, usage_rows = _extract_usage(
        path, source_kind, session_id, scan.new_offset
    )
    return SessionData(
        path,
        session_id,
        source_kind,
        events,
        usage=usage,
        usage_rows=usage_rows,
    ), invalid_lines


def parse_scan(
    path: Path, scan: twill_cursor.LineScan, fallback_session_id: str
) -> tuple[SessionData, int]:
    """Parse one scanned region into normalized, still-unredacted events.

    Returns the session data and the number of complete, non-blank lines in
    the region that did not yield a JSON object — one half of the cursor's
    ``parse_errors`` input; the torn tail :class:`LineScan` already carries
    is the other.  Codex parsing replays the committed prefix to restore its
    stateful call and correction tracking before consuming the resumed span.
    """

    source_kind = _source_kind(path)
    if source_kind == "codex":
        return _parse_codex_scan(path, scan, fallback_session_id, source_kind)

    events: list[TranscriptEvent] = []
    session_id = fallback_session_id
    invalid_lines = 0
    for source_line, line in scan.lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # A newline-terminated line that is not valid JSON is a torn
            # write the producer flushed but will never repair.  Count it and
            # move past it — only an unterminated tail blocks the cursor.
            invalid_lines += 1
            continue
        if not isinstance(record, dict):
            invalid_lines += 1
            continue
        session_id = _session_id(record, session_id)
        record_type = str(record.get("type") or "session_event")
        cwd = record.get("cwd")
        cwd_text = cwd if isinstance(cwd, str) else None
        for event_index, text in enumerate(_event_texts(record)):
            if not text.strip():
                continue
            events.append(
                TranscriptEvent(
                    session_id=session_id,
                    timestamp=_timestamp(record),
                    kind=record_type,
                    text=text,
                    source_line=source_line,
                    event_index=event_index,
                    cwd=cwd_text,
                )
            )
    usage, usage_rows = _extract_usage(
        path, source_kind, session_id, scan.new_offset
    )
    return SessionData(
        path,
        session_id,
        source_kind,
        tuple(events),
        usage=usage,
        usage_rows=usage_rows,
    ), invalid_lines


def read_session(path: Path) -> SessionData:
    """Parse one whole JSONL session into normalized, still-unredacted events."""

    session, _ = parse_scan(path, twill_cursor.scan_lines(path, 0), path.stem)
    return session


def extract_usage(
    path: Path, source_kind: str | None = None
) -> SessionUsage | None:
    kind = source_kind or _source_kind(path)
    end_offset = twill_cursor.scan_lines(path, 0).new_offset
    usage, _ = _extract_usage(path, kind, path.stem, end_offset)
    return usage


def read_usage(path: Path, source_kind: str | None = None) -> SessionUsage | None:
    return extract_usage(path, source_kind)


def _parse_duration(value: str) -> float:
    try:
        return parse_duration(value)
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(exc.message) from exc


def _state_dir(value: str | None) -> Path:
    configured = value or os.environ.get("TWILL_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "twill"


def _glob_roots(patterns: Sequence[str]) -> tuple[Path, ...]:
    """Return the literal directory prefix of each configured glob.

    This helper remains for callers that need to display or inspect a source
    tree.  Ingest does *not* use it to enumerate files: broadening a glob to
    its prefix loses the operator's matching rules.
    """

    roots: list[Path] = []
    for pattern in patterns:
        literal: list[str] = []
        for part in Path(pattern).expanduser().parts:
            if any(marker in part for marker in "*?["):
                break
            literal.append(part)
        root = Path(*literal)
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _source_roots(values: Sequence[str] | None, config: TwillConfig) -> tuple[Path, ...]:
    """Resolve transcript roots: CLI flag, then env override, then config."""

    if values:
        return tuple(Path(value).expanduser() for value in values)
    configured = os.environ.get("TWILL_SOURCE_ROOTS")
    if configured:
        return tuple(Path(value).expanduser() for value in configured.split(os.pathsep) if value)
    return _glob_roots(config.source_globs)


def _source_patterns(
    values: Sequence[str] | None, config: TwillConfig
) -> tuple[Path | str, ...]:
    """Resolve the inputs that the enumerator should expand.

    ``--source`` and ``TWILL_SOURCE_ROOTS`` are the backwards-compatible root
    interfaces from the walking skeleton.  Configured ``source_globs`` must
    remain patterns all the way to enumeration so a pattern such as
    ``projects/*/*.jsonl`` cannot accidentally become ``projects/**/*.jsonl``.
    """

    if values:
        return _source_roots(values, config)
    configured = os.environ.get("TWILL_SOURCE_ROOTS")
    if configured:
        return _source_roots(None, config)
    return tuple(config.source_globs)


def _session_key(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:32]


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_pair(value: str) -> tuple[str, str]:
    utc = _parse_timestamp(value)
    return utc.isoformat(), utc.astimezone().isoformat()


def _parse_usage_timestamp(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) > 100_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return _parse_usage_timestamp(float(text))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _usage_wall_seconds(first: str | None, last: str | None) -> int | None:
    start = _parse_usage_timestamp(first)
    end = _parse_usage_timestamp(last)
    if start is None or end is None:
        return None
    return max(0, round((end - start).total_seconds()))


def _message_usage_summary(
    session_id: str,
    rows: tuple[MessageUsage, ...],
    *,
    model: str | None = None,
    cost_usd: float | None = None,
    wall_seconds: int | None = None,
) -> SessionUsage:
    input_tokens = sum(row.input_tokens for row in rows)
    output_tokens = sum(row.output_tokens for row in rows)
    cache_read_tokens = sum(row.cache_read_tokens for row in rows)
    if cost_usd is None:
        row_costs = [row.cost_usd for row in rows if row.cost_usd is not None]
        cost_usd = sum(row_costs) if row_costs else None
    if model is None:
        model = next((row.model for row in reversed(rows) if row.model), None)
    return SessionUsage(
        session_id=session_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cost_usd=cost_usd,
        wall_seconds=wall_seconds,
        messages=len(rows),
    )


def _extract_claude_usage(
    path: Path, fallback_session_id: str, end_offset: int | None = None
) -> tuple[SessionUsage | None, tuple[MessageUsage, ...]]:
    parser = ClaudeCodeLineParser()
    consumed = 0
    with path.open("rb") as handle:
        for source_line, raw_line in enumerate(handle, start=1):
            if end_offset is not None and consumed + len(raw_line) > end_offset:
                break
            consumed += len(raw_line)
            parser.parse_line(raw_line.decode("utf-8", errors="replace"), source_line)
    rows = parser.usage
    if not rows and parser.cost_state_counters:
        rows = (
            MessageUsage(
                source_line=1,
                model=parser.model,
                **parser.cost_state_counters,
            ),
        )
    wall_seconds = parser.wall_seconds
    if wall_seconds is None:
        wall_seconds = _usage_wall_seconds(parser.first_timestamp, parser.last_timestamp)
    if not rows and parser.total_cost_usd is None and not parser.has_cost_state:
        return None, ()
    session_id = parser.session_id or fallback_session_id
    summary = _message_usage_summary(
        session_id,
        rows,
        model=parser.model,
        cost_usd=parser.total_cost_usd,
        wall_seconds=wall_seconds,
    )
    return summary, rows


def _codex_usage_row(
    row: TokenUsage, *, cost_usd: float | None = None
) -> MessageUsage:
    return MessageUsage(
        source_line=row.source_line,
        event_index=row.event_index,
        timestamp=row.timestamp,
        session_id=row.session_id,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_read_tokens=row.cache_read_tokens,
        cache_write_tokens=row.cache_write_tokens,
        reasoning_output_tokens=row.reasoning_output_tokens,
        total_tokens=row.total_tokens,
        cost_usd=cost_usd,
    )


def _extract_codex_usage(
    path: Path, fallback_session_id: str, end_offset: int | None = None
) -> tuple[SessionUsage | None, tuple[MessageUsage, ...]]:
    reader = CodexRolloutReader()
    reader.parse(path, end_offset=end_offset)
    max_rows = reader.usage_max
    if not max_rows and reader.cost_usd is None:
        return None, ()
    if max_rows:
        row = max_rows[0]
        usage_rows = (_codex_usage_row(row, cost_usd=reader.cost_usd),)
        input_tokens = row.input_tokens
        output_tokens = row.output_tokens
        cache_read_tokens = row.cache_read_tokens
    else:
        row = None
        usage_rows = ()
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
    summary = SessionUsage(
        session_id=reader.session_id or fallback_session_id,
        model=reader.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cost_usd=reader.cost_usd,
        wall_seconds=_usage_wall_seconds(reader.first_timestamp, reader.last_timestamp),
        messages=max(1, reader.message_count),
    )
    return summary, usage_rows


def _looks_like_codex(path: Path, end_offset: int | None = None) -> bool:
    try:
        consumed = 0
        with path.open("rb") as handle:
            for _ in range(1000):
                raw_line = handle.readline()
                if not raw_line:
                    break
                if end_offset is not None and consumed + len(raw_line) > end_offset:
                    break
                consumed += len(raw_line)
                try:
                    record = json.loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") in {"response_item", "token_usage_record"}:
                    return True
                payload = record.get("payload")
                if (
                    record.get("type") == "event_msg"
                    and isinstance(payload, dict)
                    and payload.get("type") in {"token_count", "turn_context"}
                ):
                    return True
    except OSError:
        return False
    return False


def _extract_usage(
    path: Path,
    source_kind: str,
    fallback_session_id: str,
    end_offset: int | None = None,
) -> tuple[SessionUsage | None, tuple[MessageUsage, ...]]:
    if source_kind == "codex" or (
        source_kind == "jsonl" and _looks_like_codex(path, end_offset)
    ):
        return _extract_codex_usage(path, fallback_session_id, end_offset)
    return _extract_claude_usage(path, fallback_session_id, end_offset)


def _coerce_session_usage(session: SessionData) -> SessionUsage | None:
    if isinstance(session.usage, SessionUsage):
        return session.usage
    if isinstance(session.usage, (tuple, list)):
        rows = tuple(row for row in session.usage if isinstance(row, MessageUsage))
        if rows or session.usage:
            return _message_usage_summary(session.session_id, rows)
    if session.usage_rows:
        return _message_usage_summary(session.session_id, session.usage_rows)
    return None


@dataclass(frozen=True)
class CursorUpdate:
    """The cursor-side half of one ingest span, written with its rows."""

    facts: twill_cursor.FileFacts
    last_offset: int
    parse_errors: int


class Store:
    def __init__(
        self,
        state_dir: Path,
        *,
        read_only: bool = False,
        content_fences: Sequence[str] = (),
    ):
        self.state_dir = state_dir
        self.db_path = twill_schema.state_db_path(state_dir)
        self.redactor = Redactor(content_fences)
        self.connection = twill_schema.connect(state_dir, read_only=read_only)
        if not read_only:
            self.connection.executescript(SCHEMA)
            _ensure_transcript_event_signature_columns(self.connection)
            _backfill_transcript_event_signatures(self.connection)
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def _persist_usage(
        self,
        conn: sqlite3.Connection,
        session: SessionData,
        stored_session_id: str,
        *,
        replace: bool,
        stale_session_ids: Sequence[str],
    ) -> None:
        if replace or stored_session_id not in stale_session_ids:
            for stale_id in dict.fromkeys((*stale_session_ids, stored_session_id)):
                conn.execute("DELETE FROM session_usage WHERE session_id = ?", (stale_id,))
        usage = _coerce_session_usage(session)
        if usage is None:
            return
        model = self.redactor.redact_text(usage.model) if usage.model else None
        input_tokens = max(0, int(usage.input_tokens))
        output_tokens = max(0, int(usage.output_tokens))
        cache_read_tokens = max(0, int(usage.cache_read_tokens))
        wall_seconds = (
            max(0, int(usage.wall_seconds))
            if usage.wall_seconds is not None
            else None
        )
        cost_usd = float(usage.cost_usd) if usage.cost_usd is not None else None
        messages = max(0, int(usage.messages))
        conn.execute(
            "INSERT INTO session_usage(session_id, model, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd, wall_seconds, messages) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET model=excluded.model, "
            "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
            "cache_read_tokens=excluded.cache_read_tokens, cost_usd=excluded.cost_usd, "
            "wall_seconds=excluded.wall_seconds, messages=excluded.messages",
            (
                stored_session_id,
                model,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cost_usd,
                wall_seconds,
                messages,
            ),
        )

    def _persist(
        self,
        session: SessionData,
        *,
        replace: bool,
        stale_session_ids: Sequence[str] = (),
        cursor_update: CursorUpdate | None = None,
    ) -> int:
        """Write one session's derived rows — and its cursor row — in one transaction.

        ``replace`` re-ingests from scratch (the Phase 0 re-read semantic and
        EC-03's reparse): every derived row tied to this file's session_key or
        to a stale ``session_id`` is deleted first, so a rewritten file can
        never leave its previous identity's observations behind.  Without it
        (EC-02 resume) the stored base rows stay and only the new span's
        events are appended; observations are still re-derived, from the full
        stored event set.  A crash either lands both halves or neither
        (plan §6.2 step 6).  Returns the session's observation count.
        """
        key = _session_key(session.path)
        now = datetime.now(timezone.utc).isoformat()
        conn = self.connection
        # Sanitize every transcript-derived string before it can become a
        # SQLite parameter. Excerpts use the bounded form; identifiers and
        # paths use the same redaction pass without an artificial length cap.
        stored_session_id = self.redactor.redact_text(session.session_id)
        stored_source_path = self.redactor.redact_text(str(session.path))
        stored_source_kind = self.redactor.redact_text(session.source_kind)
        with conn:
            if replace:
                for stale_id in dict.fromkeys((*stale_session_ids, stored_session_id)):
                    conn.execute(
                        "DELETE FROM observation WHERE session_id = ?", (stale_id,)
                    )
                conn.execute("DELETE FROM transcript_event WHERE session_key = ?", (key,))
            else:
                # Resume: the base events stay; observations are re-derived
                # from the union below.
                conn.execute(
                    "DELETE FROM observation WHERE session_id = ?", (stored_session_id,)
                )
            conn.execute(
                "INSERT INTO session(session_key, session_id, source_path, source_kind, ingested_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_key) DO UPDATE SET session_id=excluded.session_id, "
                "source_path=excluded.source_path, source_kind=excluded.source_kind, ingested_at=excluded.ingested_at",
                (key, stored_session_id, stored_source_path, stored_source_kind, now),
            )
            self._persist_usage(
                conn,
                session,
                stored_session_id,
                replace=replace,
                stale_session_ids=stale_session_ids,
            )
            for event in session.events:
                ts_utc, ts_local = _timestamp_pair(event.timestamp)
                stored_text = self.redactor.redact_excerpt(event.text)
                normalized = signature(self.redactor.redact_text(event.text))
                # The boundary is before the first database bind.  There is no
                # unredacted transcript text in the state DB.
                conn.execute(
                    "INSERT INTO transcript_event(session_key, source_line, event_index, ts_utc, ts_local, kind, text, signature, sig_hash, cwd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        event.source_line,
                        event.event_index,
                        ts_utc,
                        ts_local,
                        self.redactor.redact_text(event.kind),
                        stored_text,
                        normalized,
                        h12(normalized),
                        self.redactor.redact_excerpt(event.cwd),
                    ),
                )
            observation_count = self._run_detector(conn, key, stored_session_id)
            if cursor_update is not None:
                twill_cursor.upsert_cursor(
                    conn,
                    path=stored_source_path,
                    session_id=stored_session_id,
                    source=stored_source_kind,
                    facts=cursor_update.facts,
                    last_offset=cursor_update.last_offset,
                    parse_errors=cursor_update.parse_errors,
                    now=now,
                )
        return observation_count

    def ingest(self, session: SessionData) -> tuple[int, int]:
        """Persist one whole in-memory session, replacing its derived rows."""

        observation_count = self._persist(session, replace=True)
        return len(session.events), observation_count

    def ingest_path(self, path: Path) -> dict[str, object]:
        """Ingest one transcript file under cursor bookkeeping (§6.2 steps 3-6).

        Returns a per-file summary: the action taken, the new event count and
        the session's observation count after the write.
        """
        stored_path = self.redactor.redact_text(str(path))
        row = twill_cursor.load_cursor(self.connection, stored_path)
        facts = twill_cursor.file_facts(path)
        plan = twill_cursor.plan_ingest(row, path, facts)
        scan = twill_cursor.scan_lines(path, plan.start_offset)

        if plan.action == twill_cursor.ACTION_RESUME and not scan.lines:
            # Nothing new to parse — an unchanged file, or one whose only new
            # bytes are the still-growing tail.  Derived rows are left alone
            # (obs_ids stay put across idle passes), and only the cursor
            # bookkeeping is refreshed; an empty region keeps the stored
            # parse_errors sticky so doctor's consecutive-run signal can fire.
            parse_errors = row.parse_errors if scan.region_empty else 1
            now = datetime.now(timezone.utc).isoformat()
            with self.connection:
                twill_cursor.upsert_cursor(
                    self.connection,
                    path=stored_path,
                    session_id=row.session_id,
                    source=row.source,
                    facts=facts,
                    last_offset=scan.new_offset,
                    parse_errors=parse_errors,
                    now=now,
                )
            observations = self._count_observations(row.session_id)
            return {
                "path": str(path),
                "session_id": row.session_id,
                "action": plan.action,
                "events": 0,
                "observations": observations,
            }

        fallback_id = (
            row.session_id if plan.action == twill_cursor.ACTION_RESUME else path.stem
        )
        session, invalid_lines = parse_scan(path, scan, fallback_id)
        parse_errors = invalid_lines + (1 if scan.pending_tail else 0)
        stale_ids = (row.session_id,) if row is not None else ()
        observations = self._persist(
            session,
            replace=plan.replace_session,
            stale_session_ids=stale_ids,
            cursor_update=CursorUpdate(facts, scan.new_offset, parse_errors),
        )
        return {
            "path": str(path),
            "session_id": session.session_id,
            "action": plan.action,
            "events": len(session.events),
            "observations": observations,
        }

    def mark_missing_paths(self) -> int:
        """Sweep cursor rows for vanished files (EC-05); returns rows changed."""

        with self.connection:
            return twill_cursor.mark_missing(self.connection)

    def _count_observations(self, stored_session_id: str) -> int:
        row = self.connection.execute(
            "SELECT count(*) FROM observation WHERE session_id = ?",
            (stored_session_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _run_detector(conn: sqlite3.Connection, session_key: str, session_id: str) -> int:
        """D-00@1: a minimal detector proving stored events become observations."""

        # The v1 observation table carries no detector_id (cluster and
        # measurement attribute detectors); D-00@1 events are recognisable by
        # their kind until Phase 2 replaces this detector.
        rows = conn.execute(
            "SELECT ts_utc, ts_local, text, signature, cwd "
            "FROM transcript_event WHERE session_key = ? AND trim(text) <> ''",
            (session_key,),
        ).fetchall()
        for ts_utc, ts_local, text, stored_signature, cwd in rows:
            normalized = (
                stored_signature if stored_signature is not None else signature(text)
            )
            sig_hash = h12(normalized)
            conn.execute(
                "INSERT INTO observation(session_id, ts_utc, ts_local, kind, "
                "signature, sig_hash, excerpt, cwd) "
                "VALUES (?, ?, ?, 'session_activity', ?, ?, ?, ?)",
                (session_id, ts_utc, ts_local, normalized, sig_hash, text, cwd),
            )
        row = conn.execute(
            "SELECT count(*) FROM observation WHERE session_id = ? AND kind = 'session_activity'",
            (session_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def digest_rows(self, limit: int = 20) -> tuple[int, list[sqlite3.Row]]:
        self.connection.row_factory = sqlite3.Row
        total = self.connection.execute("SELECT count(*) FROM observation").fetchone()[0]
        rows = self.connection.execute(
            "SELECT obs_id, session_id, 'D-00@1' AS detector_id, kind, excerpt, ts_utc "
            "FROM observation ORDER BY obs_id LIMIT ?",
            (limit,),
        ).fetchall()
        return int(total), rows


def _candidate_paths(pattern: Path | str) -> Iterator[Path]:
    """Yield regular JSONL files matched by one source pattern.

    A directory is accepted for the legacy ``--source`` and environment-root
    interfaces and means ``**/*.jsonl``.  A configured pattern is otherwise
    expanded literally with recursive glob support.  Matching is performed
    before the settle gate and before any transcript bytes are opened.
    """

    expanded = Path(pattern).expanduser()
    if expanded.is_dir():
        yield from expanded.rglob("*.jsonl")
        return
    if expanded.is_file():
        if expanded.suffix == ".jsonl":
            yield expanded
        return
    pattern_text = str(expanded)
    if not glob.has_magic(pattern_text):
        return
    for match in glob.iglob(pattern_text, recursive=True):
        path = Path(match)
        if path.is_file() and path.suffix == ".jsonl":
            yield path


def settled_files(
    roots: Sequence[Path | str], settle_seconds: float, explicit_file: Path | None = None
) -> list[Path]:
    """Expand sources and return transcript files old enough to parse.

    Configured glob patterns are expanded exactly, with duplicate paths
    removed before the settle gate.  Directory inputs retain the walking
    skeleton's ``**/*.jsonl`` compatibility behavior for ``--source`` and
    ``TWILL_SOURCE_ROOTS``.  A file is eligible when ``now - mtime >=
    settle_seconds`` -- the boundary is inclusive, so a file exactly one
    window old may parse.  The gate runs here, at enumeration, before any byte
    of the file is read: a younger file is skipped whole, never parsed
    partially, and leaves no cursor row.  An explicitly named file crosses the
    same gate (``--settle 0`` is the controlled-fixture escape hatch), and a
    future mtime is never eligible -- a negative age is less than any window,
    literally -- so a clock-skewed file waits until the clock reaches its
    mtime.  Newest first, because ``--limit`` takes the freshest settled
    sessions.
    """

    now = datetime.now(timezone.utc).timestamp()
    if explicit_file is not None:
        path = explicit_file.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"transcript file does not exist: {path}")
        candidates = [path]
    else:
        candidates = []
        seen: set[Path] = set()
        for pattern in roots:
            for path in _candidate_paths(pattern):
                path = path.resolve()
                if path not in seen and path.is_file():
                    seen.add(path)
                    candidates.append(path)
    cutoff = now - settle_seconds
    return sorted(
        (path for path in candidates if path.stat().st_mtime <= cutoff),
        key=lambda path: (-path.stat().st_mtime, str(path)),
    )


def _has_candidate_files(roots: Sequence[Path | str]) -> bool:
    return any(
        path.is_file() and path.suffix == ".jsonl"
        for root in roots
        for path in _candidate_paths(root)
    )


def _has_missing_source_root(roots: Sequence[Path | str]) -> bool:
    return any(
        not glob.has_magic(str(Path(root).expanduser()))
        and not Path(root).expanduser().exists()
        for root in roots
    )


def ingest_command(args: argparse.Namespace) -> int:
    # Loaded before anything is read or written so a wrong config fails fast
    # (plan §3: bad config is a startup error, never a convenient fallback).
    config = load_config()
    state_dir = _state_dir(args.state_dir)
    started = perf_counter()
    settle = args.settle if args.settle is not None else config.settle_window
    source_patterns = _source_patterns(args.source, config)
    try:
        files = settled_files(
            source_patterns,
            settle,
            Path(args.file) if args.file else None,
        )
    except (OSError, ValueError) as exc:
        raise CliError(EXIT_RUNTIME_ERROR, str(exc), "check the transcript path and try again") from exc
    store = Store(state_dir, content_fences=config.content_fences)
    try:
        # EC-05: on every enumerated run, flag upstream files that vanished.
        # The sweep runs before the no-settled-files error so a fully cleaned
        # transcript tree still records its missing paths (evidence survives).
        if args.file is None:
            store.mark_missing_paths()
        if not files and (
            args.file is not None
            or (args.source is not None and _has_missing_source_root(source_patterns))
            or _has_candidate_files(source_patterns)
            or store.connection.execute(
                "SELECT count(*) FROM cursor WHERE path_missing = 1"
            ).fetchone()[0]
        ):
            raise CliError(
                EXIT_RUNTIME_ERROR,
                "no settled JSONL sessions found",
                "wait for the transcript settle window or use --settle 0 for a controlled fixture",
            )
        processed = []
        for path in files[: args.limit]:
            processed.append(store.ingest_path(path))
    finally:
        store.close()

    total_events = sum(item["events"] for item in processed)
    total_observations = sum(item["observations"] for item in processed)
    result = {
        "sessions": len(processed),
        "events": total_events,
        "observations": total_observations,
    }
    record_stage(
        state_dir,
        "ingest",
        perf_counter() - started,
        {
            "files": len(processed),
            "sessions": len(processed),
            "events": total_events,
            "observations": total_observations,
        },
    )
    if args.json:
        emit_success(result, json_mode=True)
    elif total_events == 0:
        print(
            f"no work; checked {len(processed)} session(s); "
            f"detector holds {total_observations} observation(s)"
        )
    else:
        print(
            f"ingested {len(processed)} session(s); "
            f"stored {total_events} event(s); detector emitted {total_observations} observation(s)"
        )
    return EXIT_SUCCESS


def _window_days(seconds: float) -> int:
    """Convert a ``--window`` duration to whole days (cluster.window_days)."""

    if seconds <= 0 or seconds % SECONDS_PER_DAY:
        raise UsageError(
            "--window must be a whole number of days, e.g. 30d or 7d",
            "the window labels every cluster row it refreshes, so 12h-style "
            "windows are refused rather than silently rounded",
        )
    return int(seconds // SECONDS_PER_DAY)


def _detect_failure_hint(report: twill_detectors.DetectorRunReport) -> str:
    if report.exit_code == EXIT_VALIDATION_FAILURE:
        return (
            "detector semantics are versioned (EC-12): a refused detector "
            "changed under its recorded version — bump its version in "
            "twill_detectors.REGISTRY to redefine what it means"
        )
    return (
        "the failing detector was skipped and the others' clusters were "
        "committed; re-run 'twill detect --detector <id>' to isolate it"
    )


def detect_command(args: argparse.Namespace) -> int:
    # Same startup gate as ingest (plan §3): a wrong config fails before any
    # detector runs, never mid-run.
    load_config()
    days = (
        DEFAULT_DETECT_WINDOW_DAYS
        if args.window is None
        else _window_days(args.window)
    )
    registry = twill_detectors.REGISTRY
    if args.detector:
        # Fail fast on a typo before the database is opened: a partial run
        # behind a usage error would be needlessly hard to reason about.
        try:
            twill_detectors.select_detectors(registry, args.detector)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc

    state_dir = _state_dir(args.state_dir)
    started = perf_counter()
    connection = twill_schema.connect(state_dir)
    try:
        report = twill_detectors.run_detectors(
            connection, window_days=days, only=args.detector
        )
    finally:
        connection.close()

    if report.exit_code != EXIT_SUCCESS:
        failures = "; ".join(
            f"{outcome.full_id}: {outcome.error}"
            for outcome in report.failed
            if outcome.error
        )
        message = (
            f"{len(report.failed)} of {len(report.outcomes)} detector(s) "
            f"failed ({failures})"
        )
        if len(report.failed) < len(report.outcomes):
            message += "; the rest ran and their clusters were committed"
        raise CliError(
            report.exit_code,
            message,
            _detect_failure_hint(report),
        )

    warnings: list[str] = []
    record_stage(
        state_dir,
        "detect",
        perf_counter() - started,
        {
            "detectors": len(report.outcomes),
            "clusters": sum(outcome.clusters for outcome in report.outcomes),
        },
    )
    if not report.outcomes:
        warnings.append(
            "no detectors registered; 'twill detect' had nothing to run"
        )
    emit_success(
        {
            "window_days": report.window_days,
            "window_start_utc": report.window_start_utc,
            "detectors": [
                {
                    "detector_id": outcome.detector_id,
                    "version": outcome.version,
                    "full_id": outcome.full_id,
                    "status": outcome.status,
                    "clusters": outcome.clusters,
                    "error": outcome.error,
                }
                for outcome in report.outcomes
            ],
        },
        json_mode=args.json,
        warnings=warnings,
    )
    if args.json:
        return EXIT_SUCCESS

    print("TWILL detect")
    print(f"window: {report.window_days} day(s) starting {report.window_start_utc}")
    if not report.outcomes:
        print("no detectors registered; add detectors in twill_detectors.REGISTRY")
        return EXIT_SUCCESS
    for outcome in report.outcomes:
        print(f"{outcome.full_id}: {outcome.clusters} cluster(s)")
    total_clusters = sum(outcome.clusters for outcome in report.outcomes)
    print(f"{len(report.outcomes)} detector(s) ran; {total_clusters} cluster(s)")
    return EXIT_SUCCESS


def rank_command(args: argparse.Namespace) -> int:
    config = load_config()
    top_k = config.top_k if getattr(args, "top", None) is None else args.top
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise UsageError("--top must be a positive integer")
    state_dir = _state_dir(args.state_dir)
    started = perf_counter()
    connection = twill_schema.connect(state_dir)
    try:
        result = twill_ranker.run_rank(
            connection,
            config.rule_globs,
            top_k=top_k,
        )
    finally:
        connection.close()

    coverage = result.ranking.coverage
    warnings: list[str] = []
    if result.index.skipped:
        warnings.append(
            f"skipped {len(result.index.skipped)} rule document(s) during indexing"
        )
    if result.index.still_stale or result.index.vanished:
        warnings.append(
            "the rule corpus contains stale documents; coverage is degraded"
        )
    record_stage(
        state_dir,
        "rank",
        perf_counter() - started,
        {
            "rows": result.index.docs,
            "clusters": coverage.total,
        },
    )
    data = {
        "top_k": top_k,
        "clusters": [cluster.as_dict() for cluster in result.ranking.clusters],
        "covered_clusters": [
            cluster.as_dict() for cluster in result.ranking.covered_clusters
        ],
        "escalations": [
            cluster.as_dict() for cluster in result.ranking.escalations
        ],
        "degraded_clusters": [
            cluster.as_dict() for cluster in result.ranking.degraded_clusters
        ],
        "suppressed_clusters": [
            cluster.as_dict() for cluster in result.ranking.suppressed_clusters
        ],
        "coverage": {
            "total": coverage.total,
            "covered": coverage.covered,
            "uncovered": coverage.uncovered,
        },
        "corpus": {
            "documents": result.index.docs,
            "stale": result.index.still_stale + len(result.index.vanished),
            "skipped": len(result.index.skipped),
        },
    }
    emit_success(data, json_mode=args.json, warnings=warnings)
    if args.json:
        return EXIT_SUCCESS

    print("TWILL rank")
    print(f"top: {top_k}")
    if not result.ranking.clusters:
        print("no new-lesson candidates")
    for cluster in result.ranking.clusters:
        print(
            f"- {cluster.detector_id} {cluster.key}: "
            f"{cluster.sessions} session(s), {cluster.events} event(s)"
        )
        waste = cluster.estimated_waste
        tokens = "unavailable" if waste is None or waste.tokens is None else f"{waste.tokens:,.2f}"
        dollars = "unavailable" if waste is None or waste.waste_usd is None else f"{waste.waste_usd:.6f}"
        print(f"  estimated tokens: {tokens}; estimated waste: {dollars} USD")
    if result.ranking.escalations:
        print("escalations:")
        for cluster in result.ranking.escalations:
            rule = f" ({cluster.covered_by})" if cluster.covered_by else ""
            print(f"- {cluster.detector_id} {cluster.key}{rule}")
    if result.ranking.degraded_clusters:
        print("degraded coverage:")
        for cluster in result.ranking.degraded_clusters:
            print(f"- {cluster.detector_id} {cluster.key} ({cluster.covered_by})")
    print(
        f"{coverage.covered} covered cluster(s); "
        f"{coverage.uncovered} uncovered cluster(s)"
    )
    return EXIT_SUCCESS


def measure_command(args: argparse.Namespace) -> int:
    config = load_config()
    artifacts_root = config.require_artifacts_root()
    state_dir = _state_dir(args.state_dir)
    started = perf_counter()
    connection = twill_schema.connect(state_dir)
    try:
        report = twill_measure.measure_lessons(
            connection,
            artifacts_root,
            lesson_id=getattr(args, "lesson_id", None),
        )
    finally:
        connection.close()
    record_stage(
        state_dir,
        "measure",
        perf_counter() - started,
        {
            "lessons": len(report.measurements),
            "measurements": len(report.measurements),
        },
    )
    emit_success(report.as_dict(), json_mode=args.json)
    if args.json:
        return EXIT_SUCCESS
    print("TWILL measure")
    print(
        f"window: {report.window_days} day(s) starting {report.window_start_utc}"
    )
    if not report.measurements:
        print("no eligible lessons")
    for item in report.measurements:
        print(
            f"- {item.lesson_id} {item.detector_id}: "
            f"{item.sessions} session(s), {item.events} event(s)"
        )
    print(f"{len(report.measurements)} measurement(s) recorded")
    return EXIT_SUCCESS


def _lesson_data(record: twill_lessons.LessonRecord) -> dict[str, object]:
    return {"lesson": record.as_dict()}


def _print_lesson(record: twill_lessons.LessonRecord, action: str) -> None:
    print(
        f"{action} {record.id}: state={record.state} "
        f"detector={record.detector}"
    )
    if record.layer is not None:
        print(f"layer: {record.layer}")
        print(f"bead: {record.routing['bead']}")
        print(f"applied_at: {record.routing['applied_at']}")


def accept_command(args: argparse.Namespace) -> int:
    config = load_config()
    record = twill_lessons.accept_lesson(
        config.require_artifacts_root(),
        args.lesson_id,
        operator=True,
    )
    emit_success(_lesson_data(record), json_mode=args.json)
    if not args.json:
        _print_lesson(record, "accepted")
    return EXIT_SUCCESS


def apply_command(args: argparse.Namespace) -> int:
    config = load_config()
    if not args.bead:
        raise UsageError(
            "--bead is required when recording an applied lesson",
            "record the bead that owns the fix before applying the lesson",
        )
    record = twill_lessons.apply_lesson(
        config.require_artifacts_root(),
        args.lesson_id,
        layer=args.layer,
        bead=args.bead,
        operator=True,
    )
    emit_success(_lesson_data(record), json_mode=args.json)
    if not args.json:
        _print_lesson(record, "applied")
    return EXIT_SUCCESS


def lessons_command(args: argparse.Namespace) -> int:
    config = load_config()
    records = twill_lessons.list_lessons(
        config.require_artifacts_root(),
        state=args.state,
    )
    emit_success(
        {"lessons": [record.as_dict() for record in records]},
        json_mode=args.json,
    )
    if args.json:
        return EXIT_SUCCESS
    if not records:
        print("no lessons")
        return EXIT_SUCCESS
    for record in records:
        print(
            f"{record.id}\t{record.state}\t{record.detector}\t{record.key}"
        )
    return EXIT_SUCCESS


def status_command(args: argparse.Namespace) -> int:
    payload = read_status(_state_dir(args.state_dir))
    if args.json:
        emit_success(
            payload["data"],
            json_mode=True,
            warnings=payload["warnings"],
        )
        return EXIT_SUCCESS

    print("TWILL status")
    stages = payload["data"]["stages"]
    if not stages:
        print("no stage records")
    else:
        print("stage\tlast_success\tduration_seconds\tcounts")
        for name in sorted(stages):
            record = stages[name]
            counts = ", ".join(
                f"{key}={value}" for key, value in record["counts"].items()
            ) or "-"
            last_success = record["last_success"] or "never"
            print(
                f"{name}\t{last_success}\t{record['duration']:.6f}\t{counts}"
            )
    for warning in payload["warnings"]:
        print(f"warning: {redact_text(warning)}", file=sys.stderr)
    return EXIT_SUCCESS


def doctor_command(args: argparse.Namespace) -> int:
    report = twill_doctor.run_doctor(_state_dir(args.state_dir))
    emit_success(
        report.as_dict(),
        json_mode=args.json,
        warnings=report.warnings,
    )
    if args.json:
        return report.exit_code
    print("TWILL doctor")
    print(f"status: {report.status}")
    for check in report.checks:
        print(f"- {check.name}: {check.status} ({redact_text(check.message)})")
    return report.exit_code


def digest_command(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args.state_dir)
    # A read verb must not bootstrap the state directory or database.  An
    # absent derived database is simply an empty first-run digest; an existing
    # database is always opened through SQLite's URI mode=ro path.
    if twill_schema.state_db_path(state_dir).is_file():
        store = Store(state_dir, read_only=True)
        try:
            total, rows = store.digest_rows()
        finally:
            store.close()
    else:
        total, rows = 0, []
    if args.json:
        emit_success(
            {
                "observations": total,
                "detectors": ["D-00@1"],
                "rows": [dict(row) for row in rows],
            },
            json_mode=True,
        )
        return EXIT_SUCCESS

    print("TWILL digest")
    print(f"observations: {total}")
    print("detectors: D-00@1 (session activity)")
    if not rows:
        print("no observations")
        return EXIT_SUCCESS
    for row in rows:
        print(
            f"- observation #{row['obs_id']} [{row['detector_id']}] "
            f"{row['kind']} session={row['session_id']}: {row['excerpt']}"
        )
    return 0


class _ArgumentParser(argparse.ArgumentParser):
    """Raise a contract error instead of letting argparse print unstructured prose."""

    def error(self, message: str) -> None:
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="twill")
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=_ArgumentParser
    )

    ingest = subparsers.add_parser("ingest", help="read settled local JSONL sessions")
    ingest.add_argument("--limit", type=int, default=1)
    ingest.add_argument("--file", help="read one explicit JSONL session")
    ingest.add_argument("--source", action="append", help="transcript root; may be repeated")
    ingest.add_argument(
        "--settle",
        type=_parse_duration,
        default=None,
        help="seconds or a form such as 2h; default comes from config.toml (2h)",
    )
    ingest.add_argument("--state-dir")
    ingest.add_argument("--json", action="store_true")
    ingest.set_defaults(handler=ingest_command)

    detect = subparsers.add_parser(
        "detect", help="run the versioned detector registry over stored observations"
    )
    detect.add_argument(
        "--window",
        type=_parse_duration,
        default=None,
        help="trailing window as whole days, e.g. 30d or 7d (default 30d)",
    )
    detect.add_argument(
        "--detector",
        action="append",
        help="detector id to run, e.g. D-01; may be repeated (default: all)",
    )
    detect.add_argument("--state-dir")
    detect.add_argument("--json", action="store_true")
    detect.set_defaults(handler=detect_command)

    rank = subparsers.add_parser(
        "rank", help="refresh rule coverage and list new-lesson candidates"
    )
    rank.add_argument("--top", type=int, default=None, help="maximum uncovered clusters")
    rank.add_argument("--state-dir")
    rank.add_argument("--json", action="store_true")
    rank.set_defaults(handler=rank_command)

    measure = subparsers.add_parser(
        "measure", help="replay accepted lesson detectors and append measurements"
    )
    measure.add_argument("--lesson", dest="lesson_id", metavar="ID")
    measure.add_argument("--state-dir")
    measure.add_argument("--json", action="store_true")
    measure.set_defaults(handler=measure_command)

    accept = subparsers.add_parser(
        "accept", help="accept a drafted lesson after operator review"
    )
    accept.add_argument("lesson_id", metavar="ID")
    accept.add_argument("--state-dir")
    accept.add_argument("--json", action="store_true")
    accept.set_defaults(handler=accept_command)

    apply = subparsers.add_parser(
        "apply", help="record an applied lesson and its routing metadata"
    )
    apply.add_argument("lesson_id", metavar="ID")
    apply.add_argument("--layer", required=True)
    apply.add_argument("--bead")
    apply.add_argument("--state-dir")
    apply.add_argument("--json", action="store_true")
    apply.set_defaults(handler=apply_command)

    lessons = subparsers.add_parser("lessons", help="list lesson files by state")
    lessons.add_argument(
        "--state",
        choices=("draft", "accepted", "applied", "resolved", "escalated", "retired"),
    )
    lessons.add_argument("--state-dir")
    lessons.add_argument("--json", action="store_true")
    lessons.set_defaults(handler=lessons_command)

    status = subparsers.add_parser("status", help="show stage status records")
    status.add_argument("--state-dir")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=status_command)

    doctor = subparsers.add_parser(
        "doctor", help="check Phase 1 pipeline health"
    )
    doctor.add_argument("--state-dir")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=doctor_command)

    digest = subparsers.add_parser("digest", help="render the stored Phase 0 digest")
    digest.add_argument("--stdout", action="store_true", help="render to stdout")
    digest.add_argument("--state-dir")
    digest.add_argument("--json", action="store_true")
    digest.set_defaults(handler=digest_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in actual_argv
    parser = build_parser()
    try:
        args = parser.parse_args(actual_argv)
        if getattr(args, "limit", 1) < 1:
            raise CliError(EXIT_USAGE_ERROR, "--limit must be at least 1")
        if args.command in MUTATING_VERBS:
            with StateLock(_state_dir(args.state_dir)):
                return int(args.handler(args))
        return int(args.handler(args))
    except CliError as exc:
        emit_error(exc.code, exc.message, exc.hint, json_mode=json_mode)
        return exc.code
    except ConfigError as exc:
        # Runtime error (contract code 1): the config is operator state that
        # turned out to be wrong, not a misuse of a verb's flags.
        emit_error(EXIT_RUNTIME_ERROR, exc.message, exc.hint, json_mode=json_mode)
        return EXIT_RUNTIME_ERROR
    except Exception as exc:  # pragma: no cover - exercised by integration failures
        emit_error(
            EXIT_RUNTIME_ERROR,
            str(exc) or "unexpected runtime error",
            "check the state directory and try again",
            json_mode=json_mode,
        )
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
