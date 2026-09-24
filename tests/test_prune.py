import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

import twill_detectors  # noqa: E402
import twill_prune  # noqa: E402
import twill_schema  # noqa: E402


NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


def detector(hits=True):
    sql = """
        SELECT program AS key,
               count(DISTINCT session_id) AS sessions,
               count(*) AS events,
               min(ts_utc) AS first_seen,
               max(ts_utc) AS last_seen
        FROM observation
        WHERE ts_utc >= :window_start_utc
          AND program IS NOT NULL
        GROUP BY program
        ORDER BY key
    """
    hits_sql = None
    if hits:
        hits_sql = """
            SELECT program AS key, session_id
            FROM observation
            WHERE ts_utc >= :window_start_utc
              AND program IS NOT NULL
            GROUP BY program, session_id
            ORDER BY key, session_id
        """
    return twill_detectors.Detector(
        "D-01",
        1,
        "retention fixture",
        sql,
        hits_sql,
    )


def seed_observation(
    connection,
    session_id,
    observed_at,
    program="alpha",
    kind="run_failed",
):
    connection.execute(
        "INSERT INTO observation(session_id, ts_utc, ts_local, kind, program, "
        "signature, sig_hash, excerpt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            observed_at.isoformat(),
            observed_at.isoformat(),
            kind,
            program,
            f"{program} failed",
            f"hash-{program}-{session_id}",
            "fixture",
        ),
    )


class PruneTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.connection = twill_schema.connect(self.state)
        self.addCleanup(self.connection.close)

    def test_old_observations_are_removed_at_the_retention_boundary(self):
        boundary = NOW - timedelta(days=180)
        seed_observation(
            self.connection,
            "old-micro",
            boundary - timedelta(microseconds=1),
            "old",
        )
        seed_observation(self.connection, "old", NOW - timedelta(days=181), "old")
        seed_observation(self.connection, "boundary", boundary, "boundary")
        seed_observation(self.connection, "recent", NOW - timedelta(days=1), "recent")
        self.connection.commit()

        report = twill_prune.prune_observations(
            self.connection,
            retention_seconds=180 * 86400,
            now=NOW,
            registry=(detector(),),
        )

        self.assertTrue(report.committed)
        self.assertEqual(report.pruned_observations, 2)
        self.assertEqual(report.remaining_observations, 2)
        self.assertEqual(
            [row[0] for row in self.connection.execute(
                "SELECT session_id FROM observation ORDER BY session_id"
            )],
            ["boundary", "recent"],
        )
        self.assertEqual(report.cutoff_utc, boundary.isoformat())

    def test_cluster_refresh_uses_the_previous_analysis_window(self):
        fixture = detector()
        seed_observation(self.connection, "s1", NOW - timedelta(days=1), "alpha")
        seed_observation(self.connection, "s2", NOW - timedelta(days=1), "alpha")
        self.connection.commit()
        twill_detectors.run_detectors(
            self.connection,
            window_days=7,
            registry=(fixture,),
            now=NOW,
        )
        seed_observation(self.connection, "s3", NOW - timedelta(days=20), "old")
        self.connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score) VALUES "
            "('D-01', 'old', 7, 1, 1, ?, ?, 0)",
            (
                (NOW - timedelta(days=20)).isoformat(),
                (NOW - timedelta(days=20)).isoformat(),
            ),
        )
        self.connection.commit()

        report = twill_prune.prune_observations(
            self.connection,
            retention_days=30,
            now=NOW,
            registry=(fixture,),
        )

        self.assertEqual(report.analysis_window_days, {"D-01": 7})
        self.assertEqual(
            self.connection.execute(
                "SELECT sessions, events FROM cluster WHERE key = 'alpha'"
            ).fetchone(),
            (2, 2),
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM cluster WHERE key = 'old'"
            ).fetchone()
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT session_id FROM cluster_session WHERE key = 'alpha' ORDER BY session_id"
            ).fetchall(),
            [("s1",), ("s2",)],
        )

    def test_reviewed_clusters_and_durable_rows_survive(self):
        seed_observation(self.connection, "old", NOW - timedelta(days=181), "old")
        self.connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score, covered_by, state) VALUES "
            "('D-01', 'old', 30, 1, 1, ?, ?, 4.5, '/rule.md', 'dismissed')",
            (
                (NOW - timedelta(days=181)).isoformat(),
                (NOW - timedelta(days=181)).isoformat(),
            ),
        )
        self.connection.execute(
            "INSERT INTO measurement(lesson_id, detector_id, measured_at, window_days, "
            "sessions, events) VALUES "
            "('L-00000001', 'D-01@1', ?, 7, 3, 4)",
            ((NOW - timedelta(days=300)).isoformat(),),
        )
        self.connection.execute(
            "INSERT INTO cluster_week(detector_id, key, week, sessions, events) "
            "VALUES ('D-01', 'old', '2025-W01', 1, 1)"
        )
        self.connection.execute(
            "INSERT INTO cursor(path, session_id, source, identity_sha, size, mtime_ns, "
            "first_seen, last_indexed_at) VALUES "
            "('/fixture.jsonl', 'old', 'claude', 'sha', 1, 1, ?, ?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
        self.connection.commit()

        report = twill_prune.prune_observations(
            self.connection,
            retention_days=180,
            now=NOW,
            registry=(detector(hits=False),),
        )

        self.assertEqual(report.pruned_observations, 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT state, covered_by, score FROM cluster WHERE key = 'old'"
            ).fetchone(),
            ("dismissed", "/rule.md", 4.5),
        )
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM measurement").fetchone()[0], 1
        )
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM cluster_week").fetchone()[0], 1
        )
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM cursor").fetchone()[0], 1
        )

    def test_failed_detector_does_not_block_retention(self):
        seed_observation(self.connection, "old", NOW - timedelta(days=181), "old")
        self.connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score) VALUES "
            "('D-01', 'old', 30, 1, 1, ?, ?, 0)",
            (
                (NOW - timedelta(days=181)).isoformat(),
                (NOW - timedelta(days=181)).isoformat(),
            ),
        )
        self.connection.commit()
        broken = twill_detectors.Detector(
            "D-01",
            1,
            "broken retention detector",
            "SELECT missing_column AS key, 1 AS sessions, 1 AS events, "
            "'x' AS first_seen, 'x' AS last_seen FROM observation",
        )

        report = twill_prune.prune_observations(
            self.connection,
            retention_days=180,
            now=NOW,
            registry=(broken,),
        )

        self.assertTrue(report.committed)
        self.assertFalse(report.detector_refresh_committed)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM observation").fetchone()[0], 0
        )
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM cluster").fetchone()[0], 1
        )

    def test_malformed_timestamps_are_not_deleted(self):
        self.connection.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind) "
            "VALUES ('bad', 'not-a-timestamp', 'not-a-timestamp', 'run_failed')"
        )
        self.connection.commit()

        report = twill_prune.prune_observations(
            self.connection,
            retention_days=180,
            now=NOW,
            registry=(),
        )

        self.assertEqual(report.pruned_observations, 0)
        self.assertEqual(report.remaining_observations, 1)

    def test_zero_and_negative_retention_are_refused(self):
        for value in (0, -1, True, float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                twill_prune.prune_observations(
                    self.connection,
                    retention_seconds=value,
                    now=NOW,
                    registry=(),
                )


class PruneCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        config = self.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        self.artifacts = self.root / "artifacts"
        config.write_text(f'artifacts_root = "{self.artifacts}"\n')
        self.state = self.root / "state"

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home)},
            check=False,
            text=True,
            capture_output=True,
        )

    def test_cli_prunes_and_records_a_stage(self):
        connection = twill_schema.connect(self.state)
        self.addCleanup(connection.close)
        seed_observation(connection, "old", NOW - timedelta(days=2), "old")
        connection.commit()
        connection.close()

        result = self.run_cli(
            "prune",
            "--older-than",
            "1d",
            "--state-dir",
            str(self.state),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["data"]["pruned_observations"], 1)
        self.assertEqual(payload["data"]["remaining_observations"], 0)
        status = json.loads((self.state / "status.json").read_text())
        self.assertEqual(status["data"]["stages"]["prune"]["counts"]["pruned_observations"], 1)

    def test_ingest_does_not_resurrect_pruned_observations(self):
        config = self.home / ".config" / "twill" / "config.toml"
        config.write_text(
            f'retention = "1d"\nsettle_window = "0"\n'
            f'artifacts_root = "{self.artifacts}"\n'
        )
        source = self.root / "session.jsonl"
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=2)
        recent = now - timedelta(hours=1)
        source.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "sessionId": "retention-session",
                            "timestamp": old.isoformat(),
                            "message": {"content": "old event"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "user",
                            "sessionId": "retention-session",
                            "timestamp": recent.isoformat(),
                            "message": {"content": "recent event"},
                        }
                    ),
                ]
            )
            + "\n"
        )
        first = self.run_cli(
            "ingest",
            "--file",
            str(source),
            "--state-dir",
            str(self.state),
            "--json",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        with sqlite3.connect(self.state / "twill.db") as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM observation").fetchone()[0],
                1,
            )
        with source.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "retention-session",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "message": {"content": "new event"},
                    }
                )
                + "\n"
            )
        second = self.run_cli(
            "ingest",
            "--file",
            str(source),
            "--state-dir",
            str(self.state),
            "--json",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        with sqlite3.connect(self.state / "twill.db") as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM observation").fetchone()[0],
                2,
            )

    def test_cli_refuses_a_zero_retention_as_usage_error(self):
        result = self.run_cli(
            "prune",
            "--older-than",
            "0",
            "--state-dir",
            str(self.state),
            "--json",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], 2)
        self.assertFalse((self.state / "twill.db").exists())


if __name__ == "__main__":
    unittest.main()
