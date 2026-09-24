"""Contract tests for the v1 corpus schema, WAL and state-directory modes.

The schema is the most-depended-on artifact in the bead graph, so these tests
pin it exactly: table and column shape, index columns, WAL journal mode, and
the mode 600/700 permissions of plan §7.2.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_schema  # noqa: E402
from twill_contract import EXIT_RUNTIME_ERROR, CliError  # noqa: E402


V1_TABLES = (
    "cluster",
    "cluster_week",
    "cursor",
    "measurement",
    "meta",
    "observation",
    "parse_shape",
    "rule_doc",
    "rule_fts",
    "session_usage",
)

# (name, type, notnull, default, pk) per table, in declaration order.
COLUMN_CONTRACT = {
    "cursor": [
        ("path", "TEXT", 0, None, 1),
        ("session_id", "TEXT", 1, None, 0),
        ("source", "TEXT", 1, None, 0),
        ("identity_sha", "TEXT", 1, None, 0),
        ("size", "INTEGER", 1, None, 0),
        ("mtime_ns", "INTEGER", 1, None, 0),
        ("last_offset", "INTEGER", 1, "0", 0),
        ("parse_errors", "INTEGER", 1, "0", 0),
        ("first_seen", "TEXT", 1, None, 0),
        ("last_indexed_at", "TEXT", 1, None, 0),
        ("path_missing", "INTEGER", 1, "0", 0),
    ],
    "observation": [
        ("obs_id", "INTEGER", 0, None, 1),
        ("session_id", "TEXT", 1, None, 0),
        ("ts_utc", "TEXT", 1, None, 0),
        ("ts_local", "TEXT", 1, None, 0),
        ("kind", "TEXT", 1, None, 0),
        ("program", "TEXT", 0, None, 0),
        ("command", "TEXT", 0, None, 0),
        ("signature", "TEXT", 0, None, 0),
        ("sig_hash", "TEXT", 0, None, 0),
        ("tool", "TEXT", 0, None, 0),
        ("path", "TEXT", 0, None, 0),
        ("rule", "TEXT", 0, None, 0),
        ("excerpt", "TEXT", 0, None, 0),
        ("launch_dir", "TEXT", 0, None, 0),
        ("cwd", "TEXT", 0, None, 0),
        ("host", "TEXT", 1, "'codinghome'", 0),
    ],
    "cluster": [
        ("detector_id", "TEXT", 1, None, 1),
        ("key", "TEXT", 1, None, 2),
        ("window_days", "INTEGER", 1, None, 0),
        ("sessions", "INTEGER", 1, None, 0),
        ("events", "INTEGER", 1, None, 0),
        ("first_seen", "TEXT", 1, None, 0),
        ("last_seen", "TEXT", 1, None, 0),
        ("score", "REAL", 1, None, 0),
        ("covered_by", "TEXT", 0, None, 0),
        ("state", "TEXT", 1, "'open'", 0),
    ],
    "rule_doc": [
        ("path", "TEXT", 0, None, 1),
        ("layer", "TEXT", 1, None, 0),
        ("sha", "TEXT", 1, None, 0),
        ("indexed_at", "TEXT", 1, None, 0),
        ("last_read_by_agent", "TEXT", 0, None, 0),
        ("stale", "INTEGER", 1, "0", 0),
    ],
    "session_usage": [
        ("session_id", "TEXT", 0, None, 1),
        ("model", "TEXT", 0, None, 0),
        ("input_tokens", "INTEGER", 0, None, 0),
        ("output_tokens", "INTEGER", 0, None, 0),
        ("cache_read_tokens", "INTEGER", 0, None, 0),
        ("cost_usd", "REAL", 0, None, 0),
        ("wall_seconds", "INTEGER", 0, None, 0),
        ("messages", "INTEGER", 0, None, 0),
    ],
    "cluster_week": [
        ("detector_id", "TEXT", 1, None, 1),
        ("key", "TEXT", 1, None, 2),
        ("week", "TEXT", 1, None, 3),
        ("sessions", "INTEGER", 1, None, 0),
        ("events", "INTEGER", 1, None, 0),
        ("est_waste_usd", "REAL", 0, None, 0),
    ],
    "parse_shape": [
        ("run_at", "TEXT", 1, None, 1),
        ("source", "TEXT", 1, None, 2),
        ("record_type", "TEXT", 1, None, 3),
        ("n", "INTEGER", 1, None, 0),
    ],
    "meta": [
        ("key", "TEXT", 0, None, 1),
        ("value", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
    ],
    "measurement": [
        ("lesson_id", "TEXT", 1, None, 1),
        ("detector_id", "TEXT", 1, None, 0),
        ("measured_at", "TEXT", 1, None, 2),
        ("window_days", "INTEGER", 1, None, 0),
        ("sessions", "INTEGER", 1, None, 0),
        ("events", "INTEGER", 1, None, 0),
    ],
}

INDEX_CONTRACT = {
    "obs_sig": ("observation", ("sig_hash", "ts_utc")),
    "obs_kind_ts": ("observation", ("kind", "ts_utc")),
    "obs_session": ("observation", ("session_id",)),
}


class SchemaContractTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.state_dir = Path(self._temporary.name) / "state"
        self.connection = twill_schema.connect(self.state_dir)
        self.addCleanup(self.connection.close)

    def table_columns(self, table):
        return [
            (row[1], row[2], row[3], row[4], row[5])
            for row in self.connection.execute(f"PRAGMA table_info({table})")
        ]

    def test_every_v1_table_exists(self):
        names = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        for table in V1_TABLES:
            self.assertIn(table, names)

    def test_cluster_session_relation_is_available_for_attribution(self):
        columns = self.table_columns("cluster_session")
        self.assertEqual(
            columns,
            [
                ("detector_id", "TEXT", 1, None, 1),
                ("key", "TEXT", 1, None, 2),
                ("session_id", "TEXT", 1, None, 3),
            ],
        )
        observed = tuple(
            row[2]
            for row in self.connection.execute(
                "PRAGMA index_info(cluster_session_by_session)"
            )
        )
        self.assertEqual(observed, ("session_id", "detector_id", "key"))

    def test_rule_fts_is_fts5_with_unindexed_path(self):
        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'rule_fts'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("USING fts5", row[0])
        self.assertIn("UNINDEXED", row[0])

    def test_column_contract(self):
        for table, expected in COLUMN_CONTRACT.items():
            self.assertEqual(self.table_columns(table), expected, table)

    def test_additive_column_migrates_a_pre_existing_cursor_table(self):
        # A database created before cursor.path_missing shipped keeps its
        # data and gains the column on the next writer open (plan §8.4:
        # additive only; a fresh database gets it from the DDL directly).
        self.connection.execute(
            "INSERT INTO cursor(path, session_id, source, identity_sha, size, mtime_ns, "
            "last_offset, first_seen, last_indexed_at) "
            "VALUES ('/t/old.jsonl', 's1', 'claude', 'sha', 10, 1, 4, 'x', 'y')"
        )
        self.connection.commit()
        self.connection.close()
        legacy = sqlite3.connect(self.state_dir / "twill.db")
        legacy.execute("CREATE TABLE cursor_backup AS SELECT * FROM cursor")
        legacy.execute("DROP TABLE cursor")
        # The pre-path_missing shape, column for column.
        legacy.execute(
            "CREATE TABLE cursor(path TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
            "source TEXT NOT NULL, identity_sha TEXT NOT NULL, size INTEGER NOT NULL, "
            "mtime_ns INTEGER NOT NULL, last_offset INTEGER NOT NULL DEFAULT 0, "
            "parse_errors INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL, "
            "last_indexed_at TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO cursor SELECT path, session_id, source, identity_sha, size, "
            "mtime_ns, last_offset, parse_errors, first_seen, last_indexed_at FROM cursor_backup"
        )
        legacy.commit()
        legacy.close()

        migrated = twill_schema.connect(self.state_dir)
        self.addCleanup(migrated.close)
        columns = [row[1] for row in migrated.execute("PRAGMA table_info(cursor)")]
        self.assertEqual(columns[-1], "path_missing")
        row = migrated.execute(
            "SELECT path, last_offset, path_missing FROM cursor"
        ).fetchone()
        self.assertEqual(row, ("/t/old.jsonl", 4, 0))
        # The migrated shape matches a fresh one exactly.
        fresh = twill_schema.connect(self.state_dir.parent / "fresh")
        self.addCleanup(fresh.close)
        fresh_columns = [row[1] for row in fresh.execute("PRAGMA table_info(cursor)")]
        self.assertEqual(columns, fresh_columns)

    def test_additive_column_migrates_a_pre_existing_rule_doc_table(self):
        # Same mechanism as cursor.path_missing, one table over: a database
        # created before rule_doc.stale shipped (EC-11) converges on the
        # fresh shape at the next writer open.
        self.connection.execute(
            "INSERT INTO rule_doc(path, layer, sha, indexed_at) "
            "VALUES ('/h/CLAUDE.md', 'claude_md', 'sha', 't')"
        )
        self.connection.commit()
        self.connection.close()
        legacy = sqlite3.connect(self.state_dir / "twill.db")
        legacy.execute("CREATE TABLE rule_doc_backup AS SELECT * FROM rule_doc")
        legacy.execute("DROP TABLE rule_doc")
        legacy.execute(
            "CREATE TABLE rule_doc(path TEXT PRIMARY KEY, layer TEXT NOT NULL, "
            "sha TEXT NOT NULL, indexed_at TEXT NOT NULL, last_read_by_agent TEXT)"
        )
        legacy.execute(
            "INSERT INTO rule_doc SELECT path, layer, sha, indexed_at, "
            "last_read_by_agent FROM rule_doc_backup"
        )
        legacy.execute("DROP TABLE rule_doc_backup")
        legacy.commit()
        legacy.close()

        migrated = twill_schema.connect(self.state_dir)
        self.addCleanup(migrated.close)
        row = migrated.execute(
            "SELECT path, layer, sha, stale FROM rule_doc"
        ).fetchone()
        self.assertEqual(row, ("/h/CLAUDE.md", "claude_md", "sha", 0))
        fresh = twill_schema.connect(self.state_dir.parent / "fresh-rule-doc")
        self.addCleanup(fresh.close)
        self.assertEqual(
            [r[1] for r in migrated.execute("PRAGMA table_info(rule_doc)")],
            [r[1] for r in fresh.execute("PRAGMA table_info(rule_doc)")],
        )

    def test_meta_enforces_key_value_state_contract(self):
        # The migration runner already stamped schema_version at writer open,
        # so the seeds below avoid it and the first conflict below proves the
        # key is unique — the runner's stamp cannot be shadowed by a second row.
        self.connection.executemany(
            "INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?)",
            (
                ("trailing_medians", "{}", "2026-09-23T00:00:00+00:00"),
                ("digest_cursor", "2026-09-19", "2026-09-23T00:00:00+00:00"),
            ),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?)",
                ("schema_version", "2", "2026-09-24T00:00:00+00:00"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO meta(key, updated_at) VALUES (?, ?)",
                ("icg_catalog_version", "2026-09-24T00:00:00+00:00"),
            )
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM meta").fetchone()[0], 3
        )

    def test_index_contract(self):
        indexes = {
            row[0]: row[1]
            for row in self.connection.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'"
            )
        }
        for name, (table, columns) in INDEX_CONTRACT.items():
            self.assertEqual(indexes.get(name), table, name)
            observed = tuple(
                row[2] for row in self.connection.execute(f"PRAGMA index_info({name})")
            )
            self.assertEqual(observed, columns, name)

    def test_documented_defaults_apply(self):
        self.connection.execute(
            "INSERT INTO cursor(path, session_id, source, identity_sha, size, mtime_ns, "
            "first_seen, last_indexed_at) VALUES ('/t/a.jsonl', 's1', 'claude', 'sha', 1, 1, 'x', 'y')"
        )
        self.connection.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind) "
            "VALUES ('s1', 'tsu', 'tsl', 'run_failed')"
        )
        self.connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score) VALUES ('D-01@1', 'k', 7, 1, 1, 'x', 'y', 1.0)"
        )
        row = self.connection.execute(
            "SELECT last_offset, parse_errors FROM cursor"
        ).fetchone()
        self.assertEqual(row, (0, 0))
        host = self.connection.execute(
            "SELECT host FROM observation"
        ).fetchone()[0]
        self.assertEqual(host, "codinghome")
        state = self.connection.execute("SELECT state FROM cluster").fetchone()[0]
        self.assertEqual(state, "open")

    def test_rule_fts_matches_rule_text(self):
        self.connection.execute(
            "INSERT INTO rule_fts(text, path) VALUES "
            "('Never force-push to the Forgejo origin.', 'AGENTS.md')"
        )
        # The phrase must be quoted: FTS5 reads a bare hyphen as query syntax.
        hits = self.connection.execute(
            "SELECT path FROM rule_fts WHERE rule_fts MATCH '\"force-push\"'"
        ).fetchall()
        self.assertEqual([row[0] for row in hits], ["AGENTS.md"])
        misses = self.connection.execute(
            "SELECT path FROM rule_fts WHERE rule_fts MATCH '\"never-employed\"'"
        ).fetchall()
        self.assertEqual(misses, [])


class MigrationRunnerTests(unittest.TestCase):
    """Plan §8.4: additive-only, version-stamped, downgrade-tolerant migrations.

    The shipped registry carries the additive tables later phases add on top
    of the v1 baseline (currently version 3); the apply path beyond it is
    exercised by registering the kind of migrations those phases will add.
    """

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.state_dir = Path(self._temporary.name) / "state"

    def _connect(self, migrations=tuple()):
        with mock.patch.object(twill_schema, "MIGRATIONS", migrations):
            return twill_schema.connect(self.state_dir)

    def stamped_version(self, connection):
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def test_shipped_registry_applies_and_stamps_its_newest_version(self):
        # The v1 tables ship unmigrated; version 2 adds detector_run (the
        # registry's run record, plan §8.1 EC-12 / §8.2).  _connect patches
        # MIGRATIONS, so the shipped registry is passed explicitly.
        versions = [migration.version for migration in twill_schema.MIGRATIONS]
        self.assertEqual(versions, list(range(2, 2 + len(versions))))
        connection = self._connect(twill_schema.MIGRATIONS)
        self.addCleanup(connection.close)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("detector_run", tables)
        self.assertIn("cluster_session", tables)
        self.assertIn(
            "attribution_sha",
            [row[1] for row in connection.execute("PRAGMA table_info(detector_run)")],
        )
        newest = str(versions[-1])
        self.assertEqual(self.stamped_version(connection), newest)
        # Reopening neither duplicates nor bumps the stamp.
        connection.close()
        reopened = self._connect(twill_schema.MIGRATIONS)
        self.addCleanup(reopened.close)
        rows = reopened.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchall()
        self.assertEqual(rows, [(newest,)])

    def test_registered_migrations_apply_in_order_and_stamp_the_version(self):
        registry = (
            twill_schema.Migration(
                2,
                "phase4_table",
                ("CREATE TABLE IF NOT EXISTS phase4(id INTEGER PRIMARY KEY, note TEXT)",),
            ),
            twill_schema.Migration(
                3,
                "phase4_observation_note",
                ("ALTER TABLE observation ADD COLUMN phase4_note TEXT",),
            ),
        )
        connection = self._connect(registry)
        self.addCleanup(connection.close)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("phase4", tables)
        # An appended column trails the v1 shape; the prefix is untouched.
        columns = [row[1] for row in connection.execute("PRAGMA table_info(observation)")]
        self.assertEqual(
            columns[: len(twill_schema.EXPECTED_OBSERVATION_COLUMNS)],
            list(twill_schema.EXPECTED_OBSERVATION_COLUMNS),
        )
        self.assertEqual(columns[-1], "phase4_note")
        self.assertEqual(self.stamped_version(connection), "3")
        # Reopening with the same registry applies nothing twice.
        connection.close()
        reopened = self._connect(registry)
        self.addCleanup(reopened.close)
        self.assertEqual(self.stamped_version(reopened), "3")
        reopened.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, phase4_note) "
            "VALUES ('s1', 't', 't', 'run_failed', 'kept')"
        )
        self.assertEqual(
            reopened.execute("SELECT phase4_note FROM observation").fetchone()[0],
            "kept",
        )

    def test_a_failed_migration_rolls_back_the_whole_run(self):
        registry = (
            twill_schema.Migration(
                2,
                "phase4_table",
                ("CREATE TABLE IF NOT EXISTS phase4(id INTEGER PRIMARY KEY)",),
            ),
            # Validates as additive, but names a table that does not exist.
            twill_schema.Migration(
                3, "phase4_bad", ("ALTER TABLE no_such_table ADD COLUMN c TEXT",)
            ),
        )
        with self.assertRaises(sqlite3.OperationalError):
            self._connect(registry)
        # The run was one transaction: the version stamp and migration 2's
        # table are both absent, so the next open retries from the baseline
        # instead of straddling a half-applied shape.
        raw = sqlite3.connect(twill_schema.state_db_path(self.state_dir))
        self.addCleanup(raw.close)
        self.assertEqual(
            raw.execute(
                "SELECT count(*) FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0],
            0,
        )
        tables = {
            row[0]
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertNotIn("phase4", tables)

    def test_rolled_back_release_reads_a_newer_database(self):
        # §8.4's tolerance: this release (empty registry, the shipped shape)
        # opens a database a newer release migrated to version 3 — no error,
        # no downgrade, unknown columns kept, stamp left where it was.
        newer = (
            twill_schema.Migration(
                2,
                "phase4_table",
                ("CREATE TABLE IF NOT EXISTS phase4(id INTEGER PRIMARY KEY, note TEXT)",),
            ),
            twill_schema.Migration(
                3,
                "phase4_observation_note",
                ("ALTER TABLE observation ADD COLUMN phase4_note TEXT",),
            ),
        )
        writer = self._connect(newer)
        writer.execute(
            "INSERT INTO phase4(id, note) VALUES (1, 'from the future release')"
        )
        writer.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, phase4_note) "
            "VALUES ('s1', 't', 't', 'run_failed', 'newer shape')"
        )
        writer.commit()
        writer.close()

        older = twill_schema.connect(self.state_dir)
        self.addCleanup(older.close)
        self.assertEqual(self.stamped_version(older), "3")
        columns = [row[1] for row in older.execute("PRAGMA table_info(observation)")]
        self.assertEqual(columns[-1], "phase4_note")
        self.assertEqual(
            older.execute("SELECT note FROM phase4").fetchone()[0],
            "from the future release",
        )
        # The older release can still write with named columns.
        older.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind) "
            "VALUES ('s2', 't', 't', 'tool_error')"
        )
        older.commit()
        self.assertEqual(
            older.execute("SELECT count(*) FROM observation").fetchone()[0], 2
        )
        # And the read-only verb opens the newer shape too.
        reader = twill_schema.connect_read_only(self.state_dir)
        self.addCleanup(reader.close)
        self.assertEqual(
            reader.execute("SELECT count(*) FROM observation").fetchone()[0], 2
        )

    def test_read_only_open_never_stamps_or_migrates(self):
        writer = self._connect()
        writer.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind) "
            "VALUES ('s1', 't', 't', 'run_failed')"
        )
        writer.commit()
        writer.close()
        # Simulate a database that predates the runner: no stamp at all.
        raw = sqlite3.connect(twill_schema.state_db_path(self.state_dir))
        raw.execute("DELETE FROM meta WHERE key = 'schema_version'")
        raw.commit()
        raw.close()

        reader = twill_schema.connect_read_only(self.state_dir)
        self.addCleanup(reader.close)
        self.assertEqual(reader.execute("SELECT count(*) FROM observation").fetchone()[0], 1)
        raw = sqlite3.connect(twill_schema.state_db_path(self.state_dir))
        self.addCleanup(raw.close)
        self.assertEqual(
            raw.execute(
                "SELECT count(*) FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0],
            0,
        )

    def test_registry_rejects_non_additive_statements(self):
        bad = (
            "DROP TABLE observation",
            "ALTER TABLE cursor DROP COLUMN path_missing",
            "ALTER TABLE cursor RENAME COLUMN path TO transcript_path",
            "CREATE TABLE phase4(id INTEGER PRIMARY KEY)",
            "CREATE INDEX phase4_kind ON observation(kind)",
            "DELETE FROM observation",
            "UPDATE meta SET value = '99'",
            "CREATE TABLE a(x); CREATE INDEX b ON a(x)",
        )
        for statement in bad:
            with self.subTest(statement=statement):
                with self.assertRaises(ValueError):
                    twill_schema._validated(
                        (twill_schema.Migration(2, "bad", (statement,)),)
                    )
        ok = (
            "CREATE TABLE IF NOT EXISTS phase4(id INTEGER PRIMARY KEY)",
            "CREATE UNIQUE INDEX IF NOT EXISTS phase4_id ON phase4(id)",
            "CREATE VIRTUAL TABLE IF NOT EXISTS phase4_fts USING fts5(text)",
            "CREATE VIEW IF NOT EXISTS phase4_open AS SELECT * FROM phase4",
            "ALTER TABLE phase4 ADD COLUMN note TEXT NOT NULL DEFAULT ''",
            "  ALTER TABLE phase4 ADD COLUMN trailing TEXT ;  ",  # padding + semicolon
        )
        twill_schema._validated((twill_schema.Migration(2, "ok", ok),))

    def test_registry_rejects_non_contiguous_or_renamed_versions(self):
        def create(name):
            return (f"CREATE TABLE IF NOT EXISTS {name}(x)",)
        cases = (
            (twill_schema.Migration(3, "skipped_two", create("t")),),
            (
                twill_schema.Migration(2, "first", create("t")),
                twill_schema.Migration(4, "gap", create("u")),
            ),
            (
                twill_schema.Migration(2, "same", create("t")),
                twill_schema.Migration(3, "same", create("u")),
            ),
            (twill_schema.Migration(2, "", create("t")),),
        )
        for registry in cases:
            with self.subTest(registry=registry):
                with self.assertRaises(ValueError):
                    twill_schema._validated(registry)


class StateStoreModeTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.state_dir = self.root / "state"

    def test_fresh_database_is_wal_at_mode_600(self):
        connection = twill_schema.connect(self.state_dir)
        self.addCleanup(connection.close)
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)
        db = twill_schema.state_db_path(self.state_dir)
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)
        # A write materialises the WAL sidecars; they must be private too.
        connection.execute(
            "INSERT INTO parse_shape(run_at, source, record_type, n) VALUES ('r', 'claude', 'user', 1)"
        )
        connection.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db) + suffix)
            if sidecar.exists():
                self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600, suffix)

    def test_existing_loose_permissions_are_tightened(self):
        self.state_dir.mkdir(mode=0o755)
        loose = sqlite3.connect(twill_schema.state_db_path(self.state_dir))
        loose.close()
        os.chmod(twill_schema.state_db_path(self.state_dir), 0o644)
        connection = twill_schema.connect(self.state_dir)
        self.addCleanup(connection.close)
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            twill_schema.state_db_path(self.state_dir).stat().st_mode & 0o777, 0o600
        )

    def test_read_only_connection_uses_wal_without_permitting_writes(self):
        writer = twill_schema.connect(self.state_dir)
        writer.execute(
            "INSERT INTO parse_shape(run_at, source, record_type, n) VALUES ('r', 'claude', 'user', 1)"
        )
        writer.commit()
        writer.close()

        reader = twill_schema.connect_read_only(self.state_dir)
        self.addCleanup(reader.close)
        self.assertEqual(reader.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(reader.execute("PRAGMA query_only").fetchone()[0], 1)
        self.assertEqual(reader.execute("SELECT n FROM parse_shape").fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            reader.execute(
                "INSERT INTO parse_shape(run_at, source, record_type, n) VALUES ('x', 'x', 'x', 1)"
            )

    def test_reconnect_is_idempotent_and_keeps_data(self):
        first = twill_schema.connect(self.state_dir)
        first.execute(
            "INSERT INTO session_usage(session_id, model, input_tokens) VALUES ('s1', 'm', 10)"
        )
        first.commit()
        first.close()
        second = twill_schema.connect(self.state_dir)
        self.addCleanup(second.close)
        row = second.execute(
            "SELECT model, input_tokens FROM session_usage WHERE session_id = 's1'"
        ).fetchone()
        self.assertEqual(row, ("m", 10))

    def test_pre_v1_observation_shape_is_rejected_with_rebuild_hint(self):
        self.state_dir.mkdir()
        legacy = sqlite3.connect(twill_schema.state_db_path(self.state_dir))
        legacy.execute(
            "CREATE TABLE observation ("
            "obs_id INTEGER PRIMARY KEY, session_key TEXT NOT NULL, session_id TEXT NOT NULL, "
            "event_id INTEGER NOT NULL, detector_id TEXT NOT NULL, ts_utc TEXT NOT NULL, "
            "ts_local TEXT NOT NULL, kind TEXT NOT NULL, excerpt TEXT NOT NULL, "
            "UNIQUE(detector_id, event_id))"
        )
        legacy.commit()
        legacy.close()
        with self.assertRaises(CliError) as caught:
            twill_schema.connect(self.state_dir)
        self.assertEqual(caught.exception.code, EXIT_RUNTIME_ERROR)
        self.assertIn("derived and disposable", caught.exception.hint)


class StoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _write_fixture(self) -> Path:
        source = self.root / "session.jsonl"
        source.write_text(
            "\n".join(
                [
                    '{"type":"user","sessionId":"fx-1","timestamp":"2026-09-20T12:00:00Z",'
                    '"cwd":"/home/coding/TWILL","message":{"role":"user","content":"boom: exit 1"}}',
                    '{"type":"assistant","sessionId":"fx-1","timestamp":"2026-09-20T12:00:01Z",'
                    '"message":{"role":"assistant","content":[{"type":"text","text":"retrying"}]}}',
                ]
            )
            + "\n"
        )
        return source

    def test_store_persists_observations_in_the_v1_shape(self):
        store = twill_app.Store(self.root / "state")
        self.addCleanup(store.close)
        session = twill_app.read_session(self._write_fixture())
        events, observations = store.ingest(session)
        self.assertEqual(events, 2)
        self.assertEqual(observations, 2)

        rows = store.connection.execute(
            "SELECT session_id, kind, host, cwd, ts_utc, ts_local FROM observation ORDER BY obs_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row[0], "fx-1")
            self.assertEqual(row[1], "session_activity")
            self.assertEqual(row[2], "codinghome")
        self.assertEqual(rows[0][4], "2026-09-20T12:00:00+00:00")
        self.assertEqual(rows[1][4], "2026-09-20T12:00:01+00:00")
        self.assertEqual(rows[0][3], "/home/coding/TWILL")
        # The redactor maps an absent cwd to "" before persistence.
        self.assertEqual(rows[1][3], "")
        # ts_local renders the same instant in the host's local zone (EC-15).
        local = datetime.fromisoformat(rows[0][5])
        self.assertEqual(
            local.astimezone(timezone.utc).isoformat(), "2026-09-20T12:00:00+00:00"
        )

        total, digest = store.digest_rows(limit=1)
        self.assertEqual(total, 2)
        self.assertEqual(digest[0]["detector_id"], "D-00@1")

    def test_rereading_the_same_session_replaces_its_observations(self):
        store = twill_app.Store(self.root / "state")
        self.addCleanup(store.close)
        session = twill_app.read_session(self._write_fixture())
        store.ingest(session)
        store.ingest(session)
        count = store.connection.execute(
            "SELECT count(*) FROM observation WHERE session_id = 'fx-1'"
        ).fetchone()[0]
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
