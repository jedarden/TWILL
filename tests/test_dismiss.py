"""Tests for permanent, auditable cluster dismissal."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_review  # noqa: E402
import twill_schema  # noqa: E402
import twill_digest  # noqa: E402
from twill_detectors import MISSING_BINARY  # noqa: E402
from twill_contract import EXIT_VALIDATION_FAILURE, ValidationError  # noqa: E402


class DismissClusterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.connection = twill_schema.connect(self.state_dir)
        self.addCleanup(self.connection.close)
        self.connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score, covered_by, state) "
            "VALUES ('D-01', 'command-not-found:sqlite3', 30, 3, 7, ?, ?, 1.0, NULL, 'open')",
            ("2026-09-01T00:00:00+00:00", "2026-09-24T00:00:00+00:00"),
        )
        self.connection.commit()

    def test_dismiss_records_reason_and_timestamp(self):
        result = twill_review.dismiss_cluster(
            self.connection,
            "D-01:command-not-found:sqlite3",
            "not actionable for this environment",
            dismissed_at="2026-09-27T20:00:00+00:00",
        )

        self.assertEqual(result.state, "dismissed")
        self.assertEqual(result.reason, "not actionable for this environment")
        self.assertEqual(result.dismissed_at, "2026-09-27T20:00:00+00:00")
        self.assertEqual(
            self.connection.execute(
                "SELECT state, dismiss_reason, dismissed_at FROM cluster"
            ).fetchone(),
            (
                "dismissed",
                "not actionable for this environment",
                "2026-09-27T20:00:00+00:00",
            ),
        )

    def test_dismiss_is_idempotent_and_preserves_first_audit_record(self):
        first = twill_review.dismiss_cluster(
            self.connection,
            "D-01:command-not-found:sqlite3",
            "first operator decision",
            dismissed_at="2026-09-27T20:00:00+00:00",
        )
        second = twill_review.dismiss_cluster(
            self.connection,
            "D-01:command-not-found:sqlite3",
            "a later explanation must not rewrite history",
            dismissed_at="2026-09-28T20:00:00+00:00",
        )

        self.assertEqual(second, first)

    def test_reason_is_redacted_and_validated_before_mutation(self):
        token = "ghp_" + "1234567890abcdefghijklmnop"
        result = twill_review.dismiss_cluster(
            self.connection,
            "D-01:command-not-found:sqlite3",
            f"known token {token}",
        )
        self.assertNotIn(token, result.reason)
        self.assertIn("<redacted:github-token>", result.reason)

        connection = twill_schema.connect(Path(self.temporary.name) / "other-state")
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score) VALUES ('D-01', 'plain', 30, 1, 1, 'x', 'y', 1.0)"
        )
        connection.commit()
        with self.assertRaises(ValidationError) as raised:
            twill_review.dismiss_cluster(connection, "D-01:plain", " ")
        self.assertEqual(raised.exception.code, EXIT_VALIDATION_FAILURE)
        self.assertEqual(
            connection.execute("SELECT state FROM cluster WHERE key = 'plain'").fetchone()[0],
            "open",
        )

    def test_invalid_and_missing_cluster_ids_fail_without_writing(self):
        for cluster_id in ("D-01", "D-01:", "not-a-detector:key"):
            with self.subTest(cluster_id=cluster_id):
                with self.assertRaises(ValidationError):
                    twill_review.dismiss_cluster(self.connection, cluster_id, "reason")
        with self.assertRaises(ValidationError):
            twill_review.dismiss_cluster(self.connection, "D-01:missing", "reason")
        self.assertEqual(
            self.connection.execute("SELECT state FROM cluster").fetchone()[0],
            "open",
        )

    def test_dismissed_cluster_is_suppressed_from_digest_replay(self):
        for index in range(2):
            session_id = f"digest-session-{index}"
            timestamp = f"2026-09-16T12:0{index}:00+00:00"
            self.connection.execute(
                "INSERT INTO observation(session_id, ts_utc, ts_local, kind, program, "
                "signature, sig_hash, excerpt) VALUES (?, ?, ?, 'run_failed', ?, ?, ?, ?)",
                (
                    session_id,
                    timestamp,
                    timestamp,
                    "sqlite3",
                    "sqlite3: command not found",
                    "digest-hash",
                    "sqlite3: command not found",
                ),
            )
        self.connection.commit()
        twill_review.dismiss_cluster(
            self.connection,
            "D-01:command-not-found:sqlite3",
            "not actionable",
            dismissed_at="2026-09-27T20:00:00+00:00",
        )

        report = twill_digest.build_digest(
            self.state_dir,
            (2026, 38),
            registry=(MISSING_BINARY,),
        )

        self.assertEqual(report.findings, ())
        self.assertEqual(report.detectors[0].current_clusters, 0)


class DismissCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.home = Path(cls.temporary.name) / "home"
        config = cls.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f'artifacts_root = "{Path(cls.temporary.name) / "artifacts"}"\n'
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "twill"), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home)},
            check=False,
            text=True,
            capture_output=True,
        )

    def seed_cluster(self, state_dir: Path):
        connection = twill_schema.connect(state_dir)
        try:
            connection.execute(
                "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
                "first_seen, last_seen, score) VALUES ('D-01', 'cli', 30, 2, 2, 'x', 'y', 1.0)"
            )
            connection.commit()
        finally:
            connection.close()

    def test_cli_writes_json_and_honors_state_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            self.seed_cluster(state_dir)
            from twill_lock import StateLock

            with StateLock(state_dir):
                locked = self.run_cli(
                    "dismiss",
                    "D-01:cli",
                    "--reason",
                    "already handled",
                    "--json",
                    "--state-dir",
                    str(state_dir),
                )
            self.assertEqual(locked.returncode, 3, locked.stderr)

            dismissed = self.run_cli(
                "dismiss",
                "D-01:cli",
                "--reason",
                "already handled",
                "--json",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(dismissed.returncode, 0, dismissed.stderr)
            payload = json.loads(dismissed.stdout)
            cluster = payload["data"]["cluster"]
            self.assertEqual(cluster["state"], "dismissed")
            self.assertEqual(cluster["reason"], "already handled")

            connection = sqlite3.connect(state_dir / "twill.db")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT state, dismiss_reason FROM cluster WHERE key = 'cli'"
                    ).fetchone(),
                    ("dismissed", "already handled"),
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
