"""The v1 corpus schema, WAL journal and mode-600 state directory (plan §7.1, §7.2).

Writer connections to ``~/.local/state/twill/twill.db`` are opened through
:func:`connect`, which prepares the state directory, turns on WAL, pins the
database file to mode 600, and ensures the v1 tables exist.  Read verbs use
:func:`connect_read_only`, which opens an existing database with SQLite URI
``mode=ro`` and never runs the DDL.  The DDL below is the plan's §7.1 schema;
the only deviation is ``IF NOT EXISTS`` so repeated writer opens are idempotent.

The versioned migration runner (plan §8.4) ships for later-phase schema
additions: append a :class:`Migration` to :data:`MIGRATIONS`.  Every accepted
statement is additive — ``CREATE ... IF NOT EXISTS`` or ``ALTER TABLE ... ADD
COLUMN``, enforced at registration — one transaction applies a whole run and
stamps ``meta.schema_version``, and a database stamped above this release's
newest migration is opened untouched (the stamp is never lowered, unknown
columns are kept), which is what lets a rolled-back release still read a newer
database.  The v1 tables are not migration consumers: they come from
``V1_SCHEMA`` directly, and the columns added after the v1 DDL shipped —
``cursor.path_missing`` (EC-05) and ``rule_doc.stale`` (EC-11) — remain inline
baseline convergence via :func:`_apply_additive_columns`, idempotently, so a
database created before they exist converges on the same shape a fresh one
gets.  The interim Phase 0 working tables (``session``, ``transcript_event``)
stay in ``twill_app`` — they are pipeline scaffolding, not corpus schema.
"""

from __future__ import annotations

import dataclasses
import os
import re
import sqlite3
from datetime import datetime, timezone
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

CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

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
    be an opaque ``no such column`` instead of the rebuild instruction.  The
    expected columns only have to be a *prefix*: additive migrations append,
    so extra trailing columns are a newer schema this release can still read
    (plan §8.4), not a rejectable shape.
    """

    columns = tuple(
        row[1] for row in connection.execute("PRAGMA table_info(observation)")
    )
    prefix = columns[: len(EXPECTED_OBSERVATION_COLUMNS)]
    if columns and prefix != EXPECTED_OBSERVATION_COLUMNS:
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


# --- versioned migrations (plan §8.4) -------------------------------------

SCHEMA_VERSION_KEY = "schema_version"
# The shape ``V1_SCHEMA`` + ``_apply_additive_columns`` produce.  An unstamped
# database is assumed to be at the baseline: every release that ships the
# runner stamps on first writer open, so an unstamped database predates it.
BASELINE_VERSION = 1

# One statement per entry, additive only.  The two shapes the runner accepts:
# ``IF NOT EXISTS`` keeps a CREATE idempotent, and ALTER entries are routed
# through the same add-if-missing check ``_apply_additive_columns`` uses so a
# re-run against an already-migrated database is a no-op.  Triggers are not
# accepted because their ``BEGIN ... END`` body needs embedded semicolons,
# which the one-statement rule exists to forbid.
_CREATE_IF_NOT_EXISTS = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?(?:VIRTUAL\s+)?(?:TABLE|INDEX|VIEW)\s+IF\s+NOT\s+EXISTS",
    re.IGNORECASE,
)
_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+"
    r"(?P<column>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<definition>.+)$",
    re.IGNORECASE | re.DOTALL,
)


@dataclasses.dataclass(frozen=True)
class Migration:
    """One additive schema step applied after the v1 baseline (plan §8.4).

    ``version`` must continue :data:`BASELINE_VERSION` contiguously and every
    statement must be additive-only DDL — the registry is validated at import,
    so an illegal migration kills the process at startup instead of
    half-applying at the next writer open.
    """

    version: int
    name: str
    statements: tuple[str, ...]


def _validated(migrations: tuple[Migration, ...]) -> tuple[Migration, ...]:
    """Check the registry is contiguous and additive-only before anything runs."""

    expected_version = BASELINE_VERSION
    names: set[str] = set()
    for migration in migrations:
        expected_version += 1
        if migration.version != expected_version:
            raise ValueError(
                "migration versions must be contiguous integers continuing "
                f"{BASELINE_VERSION}: got {migration.version} ({migration.name}) "
                f"where {expected_version} was expected"
            )
        if not migration.name:
            raise ValueError(f"migration {migration.version} has an empty name")
        if migration.name in names:
            raise ValueError(
                f"migration {migration.version} reuses the name {migration.name!r}"
            )
        names.add(migration.name)
        for statement in migration.statements:
            _validate_statement(migration, statement)
    return migrations


def _validate_statement(migration: Migration, statement: str) -> None:
    """Reject any statement that is not additive-only DDL (plan §8.4)."""

    text = statement.strip().rstrip(";").strip()
    if ";" in text:
        raise ValueError(
            f"migration {migration.version} ({migration.name}) must carry one "
            f"statement per entry: {statement!r}"
        )
    if not (_CREATE_IF_NOT_EXISTS.match(text) or _ADD_COLUMN.match(text)):
        raise ValueError(
            f"migration {migration.version} ({migration.name}) is not additive-only "
            "(plan §8.4: no schema change may need undoing) — only "
            "'CREATE ... IF NOT EXISTS' and 'ALTER TABLE ... ADD COLUMN' are "
            f"allowed: {statement!r}"
        )


# Later-phase schema additions append here; the v1 tables are baseline, not
# migration consumers.  Version 2 is the detector registry's run record
# (plan §8.1 EC-12, §8.2): one row per (detector, version) that has committed
# clusters, stamping the semantics hash that refuses a same-version semantics
# change, plus the last run's status for the per-detector failure report.
MIGRATIONS: tuple[Migration, ...] = _validated(
    (
        Migration(
            2,
            "detector_run",
            (
                "CREATE TABLE IF NOT EXISTS detector_run("
                "detector_id TEXT NOT NULL, version INTEGER NOT NULL, "
                "full_id TEXT NOT NULL, semantics_sha TEXT NOT NULL, "
                "first_run_at TEXT NOT NULL, last_run_at TEXT NOT NULL, "
                "last_status TEXT NOT NULL, last_error TEXT, "
                "clusters INTEGER NOT NULL, window_days INTEGER NOT NULL, "
                "PRIMARY KEY(detector_id, version))",
            ),
        ),
        Migration(
            3,
            "cluster_waste_attribution",
            (
                "ALTER TABLE detector_run ADD COLUMN attribution_sha TEXT",
                "CREATE TABLE IF NOT EXISTS cluster_session("
                "detector_id TEXT NOT NULL, key TEXT NOT NULL, "
                "session_id TEXT NOT NULL, "
                "PRIMARY KEY(detector_id, key, session_id))",
                "CREATE INDEX IF NOT EXISTS cluster_session_by_session "
                "ON cluster_session(session_id, detector_id, key)",
            ),
        ),
        Migration(
            4,
            "detector_backtest_semantics",
            ("ALTER TABLE detector_run ADD COLUMN backtest_sha TEXT",),
        ),
    )
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_schema_version(connection: sqlite3.Connection, db_path: Path) -> int:
    """Return the stamped schema version, 0 for a database that predates the runner."""

    row = connection.execute(
        "SELECT value FROM meta WHERE key = ?", (SCHEMA_VERSION_KEY,)
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        raise CliError(
            EXIT_RUNTIME_ERROR,
            f"{db_path} holds a non-integer {SCHEMA_VERSION_KEY}: {row[0]!r}",
            "the state database is derived and disposable (plan §7.2): "
            f"remove {db_path} and let the next run rebuild it",
        ) from None


def _stamp_baseline(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?)",
        (SCHEMA_VERSION_KEY, str(BASELINE_VERSION), _utc_now()),
    )


def _stamp(connection: sqlite3.Connection, version: int) -> None:
    connection.execute(
        "UPDATE meta SET value = ?, updated_at = ? WHERE key = ?",
        (str(version), _utc_now(), SCHEMA_VERSION_KEY),
    )


def _apply_statement(connection: sqlite3.Connection, statement: str) -> None:
    """Apply one migration statement; column adds are add-if-missing."""

    text = statement.strip().rstrip(";").strip()
    add_column = _ADD_COLUMN.match(text)
    if add_column is None:
        connection.execute(text)
        return
    table = add_column.group("table")
    column = add_column.group("column")
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {add_column.group('definition')}"
        )


def run_migrations(connection: sqlite3.Connection, db_path: Path) -> int:
    """Bring a DDL-prepared database up to this release's schema version.

    ``V1_SCHEMA`` must already have run (``connect`` does that), so the ``meta``
    table exists.  An unstamped database is stamped at the baseline first;
    then every registered migration newer than the stamp applies, all inside
    one ``BEGIN IMMEDIATE`` transaction whose commit also records the new
    version — a statement that fails rolls the whole run back to the previous
    shape and version, and the next open retries from there.

    A database stamped above this release's newest migration — a rolled-back
    release facing a newer database — is returned untouched: no statement
    runs, and the stamp is never lowered (plan §8.4).  Unknown columns and
    tables appended by additive migrations are exactly what older code can
    still read, because every query names its columns.  Returns the version
    now in effect.
    """

    current = _read_schema_version(connection, db_path)
    newest = MIGRATIONS[-1].version if MIGRATIONS else BASELINE_VERSION
    if current > newest:
        return current
    connection.execute("BEGIN IMMEDIATE")
    try:
        if current < BASELINE_VERSION:
            _stamp_baseline(connection)
            current = BASELINE_VERSION
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            for statement in migration.statements:
                _apply_statement(connection, statement)
            _stamp(connection, migration.version)
            current = migration.version
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return current


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
        run_migrations(connection, db_path)
    except BaseException:
        connection.close()
        raise
    return connection
