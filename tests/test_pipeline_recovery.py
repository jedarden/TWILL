"""Forward recovery of the interaction schema deployed before v13."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import twill_app  # noqa: E402
import twill_doctor  # noqa: E402
import twill_schema  # noqa: E402

# Historical DDL is intentional: replaying today's migration 9 would hide
# the deployed shape drift that prevented real transcript ingestion.
HISTORICAL_INTERACTION_DDL = """
CREATE TABLE interaction(
 interaction_id INTEGER PRIMARY KEY, session_key TEXT NOT NULL REFERENCES session(session_key),
 session_id TEXT NOT NULL, source TEXT NOT NULL, source_path TEXT NOT NULL,
 source_identity TEXT, source_offset INTEGER, source_line INTEGER NOT NULL,
 event_index INTEGER NOT NULL DEFAULT 0, locator TEXT, ordinal INTEGER NOT NULL,
 turn_id INTEGER, message_id TEXT, parent_ordinal INTEGER, call_ordinal INTEGER,
 question_ordinal INTEGER, ts_utc TEXT NOT NULL, ts_local TEXT NOT NULL,
 actor TEXT NOT NULL, channel TEXT, origin TEXT NOT NULL,
 sidechain INTEGER NOT NULL DEFAULT 0, terminal_state TEXT NOT NULL DEFAULT 'none',
 truncated INTEGER NOT NULL DEFAULT 0, text TEXT, UNIQUE(session_key, ordinal));
CREATE TABLE interaction_summary(
 session_key TEXT PRIMARY KEY REFERENCES session(session_key), source TEXT NOT NULL,
 retained INTEGER NOT NULL, omitted_over_cap INTEGER NOT NULL,
 truncated_fields INTEGER NOT NULL, terminal_unknown INTEGER NOT NULL,
 history_confirmed INTEGER NOT NULL, history_adapter TEXT, ingested_at TEXT NOT NULL);
"""


class PipelineRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"

    def historical(self, version=12):
        registry = tuple(m for m in twill_schema.MIGRATIONS if m.version <= version)
        with mock.patch.object(twill_schema, "MIGRATIONS", registry):
            conn = twill_schema.connect(self.state)
        conn.executescript(twill_app.SCHEMA)
        conn.executescript("DROP TABLE interaction_summary; DROP TABLE interaction;" +
                           HISTORICAL_INTERACTION_DDL)
        conn.execute("INSERT INTO session VALUES ('legacy-key', 'kept', '/legacy.jsonl', 'claude', 'old')")
        conn.execute(
            "INSERT INTO interaction(session_key, session_id, source, source_path, "
            "source_line, ordinal, ts_utc, ts_local, actor, origin, text) "
            "VALUES ('legacy-key', 'kept', 'claude', '/legacy.jsonl', 1, 0, "
            "'2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z', 'user', "
            "'human_request', 'preserved conversation evidence')"
        )
        conn.execute(
            "INSERT INTO interaction_summary VALUES ('legacy-key', 'claude', 1, 0, 0, 0, 0, NULL, 'old')"
        )
        conn.execute("INSERT INTO observation(session_id, ts_utc, ts_local, kind, excerpt) "
                     "VALUES ('kept', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z', "
                     "'run_failed', 'preserved historical evidence')")
        conn.commit()
        return conn

    def test_each_deployed_stamp_converges_and_preserves_observations(self):
        for version in range(9, 13):
            with self.subTest(version=version):
                self.state = self.root / str(version)
                self.historical(version).close()
                conn = twill_schema.connect(self.state)
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone(),
                                 (str(twill_schema.MIGRATIONS[-1].version),))
                self.assertEqual(conn.execute("SELECT excerpt FROM observation").fetchone(),
                                 ('preserved historical evidence',))
                self.assertEqual(conn.execute(
                    "SELECT text, thread_id, contract_version, history_confirmed "
                    "FROM interaction WHERE session_key='legacy-key'"
                ).fetchone(), ('preserved conversation evidence', None, 'twill-interaction/v1', 0))
                self.assertEqual(conn.execute(
                    "SELECT retained, omitted_first_ordinal, omitted_last_ordinal "
                    "FROM interaction_summary WHERE session_key='legacy-key'"
                ).fetchone(), (1, None, None))
                self.assertEqual(twill_doctor._check_database_schema(conn, None).status,
                                 twill_doctor.HEALTHY)
                before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
                conn.close()
                conn = twill_schema.connect(self.state)
                self.assertEqual(before, conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall())
                conn.close()

    def test_doctor_detects_missing_columns_even_with_current_stamp(self):
        conn = self.historical()
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                     (str(twill_schema.MIGRATIONS[-1].version),))
        conn.commit()
        report = twill_doctor._check_database_schema(conn, None)
        self.assertEqual(report.status, twill_doctor.BROKEN)
        self.assertIn('thread_id', report.message)
        conn.close()

    def test_real_transcript_ingests_and_detects_after_historical_schema_upgrade(self):
        self.historical().close()
        home = self.root / 'home'
        config = home / '.config/twill/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text(f'artifacts_root = "{self.root / "artifacts"}"\nsettle_window = "0"\n')
        source = self.root / 'session.jsonl'
        now = datetime.now(timezone.utc).isoformat()
        records = [
            {'type': 'user', 'sessionId': 'recovered', 'timestamp': now,
             'message': {'role': 'user', 'content': 'Check the build'}},
            {'type': 'assistant', 'sessionId': 'recovered', 'timestamp': now,
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 'c1', 'name': 'Bash',
                  'input': {'command': 'missing-recovery-command'}}]}},
            {'type': 'user', 'sessionId': 'recovered', 'timestamp': now,
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 'c1', 'is_error': True,
                  'content': 'Exit code 127\nmissing-recovery-command: command not found'}]}},
        ]
        source.write_text(''.join(json.dumps(record) + '\n' for record in records))
        env = {**os.environ, 'HOME': str(home)}
        env.pop('TWILL_HISTORY_DB', None)
        for verb, extra in [('ingest', ['--file', str(source), '--settle', '0']), ('detect', [])]:
            run = subprocess.run([sys.executable, str(ROOT / 'twill'), verb,
                                  '--state-dir', str(self.state), '--json', *extra],
                                 cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        with sqlite3.connect(self.state / 'twill.db') as conn:
            self.assertGreater(conn.execute("SELECT count(*) FROM interaction WHERE session_id='recovered'").fetchone()[0], 0)
            self.assertGreater(conn.execute("SELECT count(*) FROM observation WHERE session_id='recovered' AND kind='run_failed'").fetchone()[0], 0)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone(), ('ok',))


if __name__ == '__main__':
    unittest.main()
