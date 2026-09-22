#!/usr/bin/env python3
"""Phase 0 implementation of the TWILL transcript-to-digest pipeline.

The first version deliberately keeps the pipeline small and deterministic.  It
does not retain raw transcript lines, and it has no cursor or artifact writer:
reader -> redactor -> SQLite -> one activity detector -> digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from twill_contract import (
    EXIT_RUNTIME_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    CliError,
    UsageError,
    emit_error,
    emit_success,
)


DEFAULT_SETTLE_SECONDS = 2 * 60 * 60
DEFAULT_SOURCE_ROOTS = (
    Path.home() / ".claude" / "projects",
    Path.home() / ".codex" / "sessions",
)
MAX_EXCERPT_LENGTH = 240


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
    cwd TEXT,
    UNIQUE(session_key, source_line, event_index)
);

CREATE TABLE IF NOT EXISTS observation (
    obs_id INTEGER PRIMARY KEY,
    session_key TEXT NOT NULL REFERENCES session(session_key),
    session_id TEXT NOT NULL,
    event_id INTEGER NOT NULL REFERENCES transcript_event(event_id),
    detector_id TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    ts_local TEXT NOT NULL,
    kind TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    UNIQUE(detector_id, event_id)
);

CREATE INDEX IF NOT EXISTS observation_session ON observation(session_id);
CREATE INDEX IF NOT EXISTS observation_detector ON observation(detector_id, ts_utc);
"""


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
class SessionData:
    path: Path
    session_id: str
    source_kind: str
    events: tuple[TranscriptEvent, ...]


_REDACTION_PATTERNS = (
    (
        re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_\-]{12,}"),
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
        re.compile(
            r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|token)\s*[:=]\s*)(['\"]?)[^\s,'\"]+"
        ),
        r"\1\2<redacted:secret>",
    ),
)


def redact(text: str | None) -> str:
    """Redact credential-shaped values and cap persisted excerpts."""

    if not text:
        return ""
    redacted = str(text)
    for pattern, replacement in _REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted.strip()[:MAX_EXCERPT_LENGTH]


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


def read_session(path: Path) -> SessionData:
    """Parse one JSONL session into normalized, still-unredacted events."""

    fallback_id = path.stem
    events: list[TranscriptEvent] = []
    session_id = fallback_id
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for source_line, line in enumerate(handle, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A partial final line is normal while a producer is closing a
                # file.  Settled files should not have one, but it is safer to
                # skip it than to lose all earlier evidence.
                continue
            if not isinstance(record, dict):
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
    return SessionData(path, session_id, _source_kind(path), tuple(events))


def _parse_duration(value: str) -> float:
    if value.isdigit():
        return float(value)
    match = re.fullmatch(r"(?i)([0-9]+(?:\.[0-9]+)?)([smhd])", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("duration must be seconds or a value such as 2h, 30m, or 0")
    number = float(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
    return number * multiplier


def _state_dir(value: str | None) -> Path:
    configured = value or os.environ.get("TWILL_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "twill"


def _source_roots(values: Sequence[str] | None) -> tuple[Path, ...]:
    if values:
        return tuple(Path(value).expanduser() for value in values)
    configured = os.environ.get("TWILL_SOURCE_ROOTS")
    if configured:
        return tuple(Path(value).expanduser() for value in configured.split(os.pathsep) if value)
    return DEFAULT_SOURCE_ROOTS


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


class Store:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.db_path = state_dir / "twill.db"
        state_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def ingest(self, session: SessionData) -> tuple[int, int]:
        key = _session_key(session.path)
        now = datetime.now(timezone.utc).isoformat()
        conn = self.connection
        with conn:
            conn.execute("DELETE FROM observation WHERE session_key = ?", (key,))
            conn.execute("DELETE FROM transcript_event WHERE session_key = ?", (key,))
            conn.execute(
                "INSERT INTO session(session_key, session_id, source_path, source_kind, ingested_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_key) DO UPDATE SET session_id=excluded.session_id, "
                "source_path=excluded.source_path, source_kind=excluded.source_kind, ingested_at=excluded.ingested_at",
                (key, session.session_id, str(session.path), session.source_kind, now),
            )
            for event in session.events:
                ts_utc, ts_local = _timestamp_pair(event.timestamp)
                # The boundary is before the first database bind.  There is no
                # unredacted transcript text in the state DB.
                conn.execute(
                    "INSERT INTO transcript_event(session_key, source_line, event_index, ts_utc, ts_local, kind, text, cwd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        event.source_line,
                        event.event_index,
                        ts_utc,
                        ts_local,
                        event.kind,
                        redact(event.text),
                        redact(event.cwd),
                    ),
                )
            observation_count = self._run_detector(conn, key, session.session_id)
        return len(session.events), observation_count

    @staticmethod
    def _run_detector(conn: sqlite3.Connection, session_key: str, session_id: str) -> int:
        """D-00@1: a minimal detector proving stored events become observations."""

        conn.execute(
            "INSERT INTO observation(session_key, session_id, event_id, detector_id, ts_utc, ts_local, kind, excerpt) "
            "SELECT e.session_key, ?, e.event_id, 'D-00@1', e.ts_utc, e.ts_local, 'session_activity', e.text "
            "FROM transcript_event AS e "
            "WHERE e.session_key = ? AND trim(e.text) <> ''",
            (session_id, session_key),
        )
        row = conn.execute(
            "SELECT count(*) FROM observation WHERE session_key = ? AND detector_id = 'D-00@1'",
            (session_key,),
        ).fetchone()
        return int(row[0]) if row else 0

    def digest_rows(self, limit: int = 20) -> tuple[int, list[sqlite3.Row]]:
        self.connection.row_factory = sqlite3.Row
        total = self.connection.execute("SELECT count(*) FROM observation").fetchone()[0]
        rows = self.connection.execute(
            "SELECT obs_id, session_id, detector_id, kind, excerpt, ts_utc "
            "FROM observation ORDER BY obs_id LIMIT ?",
            (limit,),
        ).fetchall()
        return int(total), rows


def settled_files(
    roots: Sequence[Path], settle_seconds: float, explicit_file: Path | None = None
) -> list[Path]:
    now = datetime.now(timezone.utc).timestamp()
    if explicit_file is not None:
        path = explicit_file.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"transcript file does not exist: {path}")
        candidates = [path]
    else:
        candidates = []
        seen: set[Path] = set()
        for root in roots:
            root = root.expanduser()
            if root.is_file():
                paths = (root,)
            elif root.is_dir():
                paths = root.rglob("*.jsonl")
            else:
                continue
            for path in paths:
                path = path.resolve()
                if path not in seen and path.is_file():
                    seen.add(path)
                    candidates.append(path)
    cutoff = now - settle_seconds
    return sorted(
        (path for path in candidates if path.stat().st_mtime <= cutoff),
        key=lambda path: (-path.stat().st_mtime, str(path)),
    )


def ingest_command(args: argparse.Namespace) -> int:
    try:
        files = settled_files(
            _source_roots(args.source),
            args.settle,
            Path(args.file) if args.file else None,
        )
    except (OSError, ValueError) as exc:
        raise CliError(EXIT_RUNTIME_ERROR, str(exc), "check the transcript path and try again") from exc
    if not files:
        raise CliError(
            EXIT_RUNTIME_ERROR,
            "no settled JSONL sessions found",
            "wait for the transcript settle window or use --settle 0 for a controlled fixture",
        )

    store = Store(_state_dir(args.state_dir))
    try:
        processed = []
        for path in files[: args.limit]:
            session = read_session(path)
            events, observations = store.ingest(session)
            processed.append(
                {
                    "path": str(path),
                    "session_id": session.session_id,
                    "events": events,
                    "observations": observations,
                }
            )
    finally:
        store.close()

    total_events = sum(item["events"] for item in processed)
    total_observations = sum(item["observations"] for item in processed)
    result = {
        "sessions": len(processed),
        "events": total_events,
        "observations": total_observations,
    }
    if args.json:
        emit_success(result, json_mode=True)
    else:
        print(
            f"ingested {len(processed)} session(s); "
            f"stored {total_events} event(s); detector emitted {total_observations} observation(s)"
        )
    return EXIT_SUCCESS


def digest_command(args: argparse.Namespace) -> int:
    store = Store(_state_dir(args.state_dir))
    try:
        total, rows = store.digest_rows()
    finally:
        store.close()
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
    ingest.add_argument("--settle", type=_parse_duration, default=DEFAULT_SETTLE_SECONDS)
    ingest.add_argument("--state-dir")
    ingest.add_argument("--json", action="store_true")
    ingest.set_defaults(handler=ingest_command)

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
        return int(args.handler(args))
    except CliError as exc:
        emit_error(exc.code, exc.message, exc.hint, json_mode=json_mode)
        return exc.code
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
