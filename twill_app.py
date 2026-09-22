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
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

import twill_schema
import twill_cursor
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
    CliError,
    UsageError,
    emit_error,
    emit_success,
)
from twill_lock import StateLock
from twill_redactor import Redactor, redact as _redact


MAX_EXCERPT_LENGTH = 240
MUTATING_VERBS = frozenset({"ingest"})


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
    cwd TEXT,
    UNIQUE(session_key, source_line, event_index)
);
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


def parse_scan(
    path: Path, scan: twill_cursor.LineScan, fallback_session_id: str
) -> tuple[SessionData, int]:
    """Parse one scanned region into normalized, still-unredacted events.

    Returns the session data and the number of complete, non-blank lines in
    the region that did not yield a JSON object — one half of the cursor's
    ``parse_errors`` input; the torn tail :class:`LineScan` already carries
    is the other.  Parsing is stateless per line, so a resumed region's
    events compose with the base parse's (the pairing state the detector
    reader keeps lives in ``twill_reader`` and is not on this path).
    """

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
    return SessionData(path, session_id, _source_kind(path), tuple(events)), invalid_lines


def read_session(path: Path) -> SessionData:
    """Parse one whole JSONL session into normalized, still-unredacted events."""

    session, _ = parse_scan(path, twill_cursor.scan_lines(path, 0), path.stem)
    return session


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

    def close(self) -> None:
        self.connection.close()

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
                        self.redactor.redact_text(event.kind),
                        self.redactor.redact_excerpt(event.text),
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
        conn.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, excerpt, cwd) "
            "SELECT ?, e.ts_utc, e.ts_local, 'session_activity', e.text, e.cwd "
            "FROM transcript_event AS e "
            "WHERE e.session_key = ? AND trim(e.text) <> ''",
            (session_id, session_key),
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


def ingest_command(args: argparse.Namespace) -> int:
    # Loaded before anything is read or written so a wrong config fails fast
    # (plan §3: bad config is a startup error, never a convenient fallback).
    config = load_config()
    settle = args.settle if args.settle is not None else config.settle_window
    try:
        files = settled_files(
            _source_patterns(args.source, config),
            settle,
            Path(args.file) if args.file else None,
        )
    except (OSError, ValueError) as exc:
        raise CliError(EXIT_RUNTIME_ERROR, str(exc), "check the transcript path and try again") from exc
    store = Store(_state_dir(args.state_dir), content_fences=config.content_fences)
    try:
        # EC-05: on every enumerated run, flag upstream files that vanished.
        # The sweep runs before the no-settled-files error so a fully cleaned
        # transcript tree still records its missing paths (evidence survives).
        if args.file is None:
            store.mark_missing_paths()
        if not files:
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
    if args.json:
        emit_success(result, json_mode=True)
    else:
        print(
            f"ingested {len(processed)} session(s); "
            f"stored {total_events} event(s); detector emitted {total_observations} observation(s)"
        )
    return EXIT_SUCCESS


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
