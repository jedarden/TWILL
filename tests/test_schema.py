"""Contract tests for the v1 corpus schema, WAL and state-directory modes.

The schema is the most-depended-on artifact in the bead graph, so these tests
pin it exactly: table and column shape, index columns, WAL journal mode, and
the mode 600/700 permissions of plan §7.2.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import twill_app
import twill_schema
from twill_contract import EXIT_RUNTIME_ERROR, CliError


V1_TABLES = (
    "cluster",
    "cluster_week",
    "cursor",
    "measurement",
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

    def test_meta_is_not_part_of_this_schema(self):
        # meta(key, value, updated_at) has its own bead; the v1 schema bead
        # enumerates the other nine tables and must not absorb it.
        names = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertNotIn("meta", names)

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
