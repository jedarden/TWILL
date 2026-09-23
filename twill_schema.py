"""The v1 corpus schema, WAL journal and mode-600 state directory (plan §7.1, §7.2).

Writer connections to ``~/.local/state/twill/twill.db`` are opened through
:func:`connect`, which prepares the state directory, turns on WAL, pins the
database file to mode 600, and ensures the v1 tables exist.  Read verbs use
:func:`connect_read_only`, which opens an existing database with SQLite URI
``mode=ro`` and never runs the DDL.  The DDL below is the plan's §7.1 schema;
the only deviation is ``IF NOT EXISTS`` so repeated writer opens are idempotent.

Two pieces of §7.1 deliberately live elsewhere: ``meta`` is added by its own
bead, and the versioned migration runner (plan §8.4) is a separate bead.  The
columns added after the v1 DDL shipped — ``cursor.path_missing`` (EC-05) and
``rule_doc.stale`` (EC-11) — are applied inline by
:func:`_apply_additive_columns`, idempotently, so a database created before
they exist converges on the same shape a fresh one gets.  The interim Phase 0
working tables (``session``, ``transcript_event``) stay in ``twill_app`` —
they are pipeline scaffolding, not corpus schema.
"""

from __future__ import annotations

import os
import sqlite3
from urllib.parse import quote
from pathlib import Path

from twill_contract import EXIT_RUNTIME_ERROR, CliError


DB_FILENAME = "twill.db"
DIR_MODE = 0o700
DB_MODE = 0o600

# The one table a pre-v1 state database can already hold under the same name:
# the Phase 0 walking skeleton stored a different ``observation`` shape there.
# Everything else in the v1 schema is new, so its ``IF NOT EXISTS`` is safe.
EXPECTED_OBSERVATION_COLUMNS = (
    "obs_id",
    "session_id",
    "ts_utc",
    "ts_local",
    "kind",
    "program",
    "command",
    "signature",
    "sig_hash",
    "tool",
    "path",
    "rule",
    "excerpt",
    "launch_dir",
    "cwd",
    "host",
)

# Columns added after the v1 DDL shipped, applied to pre-existing tables by
# _apply_additive_columns.  A fresh database gets them from the DDL itself.
ADDITIVE_COLUMNS = (
    # EC-05: a vanished upstream transcript is flagged, never avenged — its
    # observations survive and `doctor` reports the spike.
    ("cursor", "path_missing", "INTEGER NOT NULL DEFAULT 0"),
    # EC-11 (§8.1): a vanished rule file is flagged stale, never deleted —
    # its row and FTS text survive so coverage degrades visibly, not
    # silently.
    ("rule_doc", "stale", "INTEGER NOT NULL DEFAULT 0"),
)


V1_SCHEMA = """
-- identity + resume position for every transcript file ever seen
CREATE TABLE IF NOT EXISTS cursor(
  path TEXT PRIMARY KEY, session_id TEXT NOT NULL, source TEXT NOT NULL,   -- claude|codex
  identity_sha TEXT NOT NULL,        -- sha256 of first 4 KiB, detects rewrite-in-place
  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  last_offset INTEGER NOT NULL DEFAULT 0,
  parse_errors INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL, last_indexed_at TEXT NOT NULL,
  path_missing INTEGER NOT NULL DEFAULT 0);   -- EC-05: vanished upstream, evidence kept

-- the atom of evidence; text fields are POST-redaction and <=240 chars
CREATE TABLE IF NOT EXISTS observation(
  obs_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
  ts_utc TEXT NOT NULL, ts_local TEXT NOT NULL,        -- EC-15: both, from the v1 DDL onward
  kind TEXT NOT NULL,                -- run_failed|tool_error|tool_rejected|interrupt|file_read|hook_denial
  program TEXT, command TEXT, signature TEXT, sig_hash TEXT,
  tool TEXT, path TEXT, rule TEXT, excerpt TEXT,
  launch_dir TEXT, cwd TEXT, host TEXT NOT NULL DEFAULT 'codinghome');
CREATE INDEX IF NOT EXISTS obs_sig ON observation(sig_hash, ts_utc);
CREATE INDEX IF NOT EXISTS obs_kind_ts ON observation(kind, ts_utc);
CREATE INDEX IF NOT EXISTS obs_session ON observation(session_id);

-- detector output, refreshed per run; (detector_id, key) is the cluster identity
CREATE TABLE IF NOT EXISTS cluster(
  detector_id TEXT NOT NULL, key TEXT NOT NULL, window_days INTEGER NOT NULL,
  sessions INTEGER NOT NULL, events INTEGER NOT NULL,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  score REAL NOT NULL, covered_by TEXT,        -- rule file path, or NULL
  state TEXT NOT NULL DEFAULT 'open',          -- open|drafted|escalation|dismissed
  PRIMARY KEY(detector_id, key));

-- the rule corpus TWILL checks coverage against (read-only inputs, hashed for staleness)
CREATE TABLE IF NOT EXISTS rule_doc(
  path TEXT PRIMARY KEY, layer TEXT NOT NULL,  -- memory|claude_md|agents_md|skill|hook
  sha TEXT NOT NULL, indexed_at TEXT NOT NULL, last_read_by_agent TEXT,
  stale INTEGER NOT NULL DEFAULT 0);   -- EC-11 (§8.1): path vanished, content kept for hash matching
CREATE VIRTUAL TABLE IF NOT EXISTS rule_fts USING fts5(text, path UNINDEXED, tokenize='porter unicode61');

-- per-session token/cost usage, extracted during ingest; feeds waste attribution
CREATE TABLE IF NOT EXISTS session_usage(
  session_id TEXT PRIMARY KEY, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cost_usd REAL, wall_seconds INTEGER, messages INTEGER);

-- weekly rate per signature; the series change-point detection runs over
CREATE TABLE IF NOT EXISTS cluster_week(
  detector_id TEXT NOT NULL, key TEXT NOT NULL, week TEXT NOT NULL,   -- ISO yyyy-Www
  sessions INTEGER NOT NULL, events INTEGER NOT NULL,
  est_waste_usd REAL,
  PRIMARY KEY(detector_id, key, week));

-- per-run record-type histogram per source; a shifted distribution is the drift alarm
CREATE TABLE IF NOT EXISTS parse_shape(
  run_at TEXT NOT NULL, source TEXT NOT NULL, record_type TEXT NOT NULL,
  n INTEGER NOT NULL, PRIMARY KEY(run_at, source, record_type));

-- one row per (lesson, measurement day); mirrored to measurements/<lesson>.jsonl for durability
-- detector_id carries the version (`D-01@2`, per EC-12), which is how §8.3's
-- "a measurement always records the detector version it ran" is satisfied
CREATE TABLE IF NOT EXISTS measurement(
  lesson_id TEXT NOT NULL, detector_id TEXT NOT NULL, measured_at TEXT NOT NULL,
  window_days INTEGER NOT NULL, sessions INTEGER NOT NULL, events INTEGER NOT NULL,
  PRIMARY KEY(lesson_id, measured_at));
"""


def state_db_path(state_dir: Path) -> Path:
    return state_dir / DB_FILENAME


def prepare_state_dir(state_dir: Path) -> Path:
    """Create the state directory if needed and hold it at mode 700.

    The database file inside is pinned to mode 600 by :func:`connect`; the
    directory itself must not let other local users enumerate or swap it.
    """

    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, DIR_MODE)
    return state_dir


def _pin_db_modes(db_path: Path) -> None:
    """Pin the database and its WAL sidecars to mode 600.

    SQLite derives sidecar permissions from the database file, but the first
    WAL frame can be written before any pin, so every open re-asserts it.
    """

    os.chmod(db_path, DB_MODE)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            os.chmod(sidecar, DB_MODE)


def _verify_observation_shape(connection: sqlite3.Connection, db_path: Path) -> None:
    """Reject a pre-v1 ``observation`` table before the v1 DDL references it.

    Checked before ``executescript`` because ``obs_sig`` indexes a column the
    legacy Phase 0 shape does not have — without this guard the failure would
    be an opaque ``no such column`` instead of the rebuild instruction.
    """

    columns = tuple(
        row[1] for row in connection.execute("PRAGMA table_info(observation)")
    )
    if columns and columns != EXPECTED_OBSERVATION_COLUMNS:
        raise CliError(
            EXIT_RUNTIME_ERROR,
            f"{db_path} holds a pre-v1 observation table that does not match the v1 schema",
            "the state database is derived and disposable (plan §7.2): "
            f"remove {db_path} and let the next run rebuild it",
        )


def connect_read_only(state_dir: Path) -> sqlite3.Connection:
    """Open an existing state database read-only without creating any files."""

    db_path = state_db_path(state_dir)
    if not db_path.is_file():
        raise CliError(
            EXIT_RUNTIME_ERROR,
            f"state database does not exist: {db_path}",
            "run a mutating verb such as 'twill ingest' first",
        )
    # URI mode=ro prevents SQLite from creating or modifying the database;
    # WAL lets this connection read a consistent snapshot alongside a writer.
    uri = f"file:{quote(str(db_path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        _verify_observation_shape(connection, db_path)
    except BaseException:
        connection.close()
        raise
    return connection


def _apply_additive_columns(connection: sqlite3.Connection) -> None:
    """Add columns introduced after the v1 DDL shipped (plan §8.4: additive only).

    ``CREATE TABLE IF NOT EXISTS`` cannot extend a table that already exists,
    so each additive column is checked against ``table_info`` and appended
    with ``ALTER TABLE`` when missing.  Appending keeps fresh and migrated
    databases in the same column order; a downgrade simply keeps the unknown
    column, which is exactly §8.4's tolerance.
    """

    for table, column, definition in ADDITIVE_COLUMNS:
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def connect(state_dir: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the state database as a writer, or explicitly read-only."""

    if read_only:
        return connect_read_only(state_dir)

    db_path = state_db_path(prepare_state_dir(state_dir))
    # Create the file at mode 600 before SQLite ever touches it.  os.open's
    # mode is umask-masked, but 600 has no group/other bits to mask away.
    file_descriptor = os.open(db_path, os.O_CREAT | os.O_RDWR, DB_MODE)
    os.close(file_descriptor)
    connection = sqlite3.connect(db_path)
    try:
        # WAL keeps read verbs (digest, doctor) off the single writer's lock
        # (plan §6.2); it is persistent, so this only has to succeed once.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        _pin_db_modes(db_path)
        _verify_observation_shape(connection, db_path)
        connection.executescript(V1_SCHEMA)
        _apply_additive_columns(connection)
    except BaseException:
        connection.close()
        raise
    return connection
