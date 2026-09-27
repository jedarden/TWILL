"""Tests for the read-only, on-demand pre-flight brief."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

import twill_brief  # noqa: E402
import twill_schema  # noqa: E402
from twill_lessons import list_lessons  # noqa: E402


LESSON = """---
id: L-1234abcd
summary: known setup friction
state: accepted
detector: D-02@1
key: missing setup
evidence: {sessions: 1, events: 2, first_seen: 2026-09-20, session_ids: [session-lesson]}
routing: {recommended: retrieval_only, reason: current evidence supports retrieval only, applied: null, applied_at: null, bead: null}
backtest: {window_days: 30, sessions: 1, first_seen: 2026-09-20, weeks_present: 1}
---
Use the documented setup path.
"""


DRAFT = LESSON.replace("L-1234abcd", "L-1234abce").replace(
    "state: accepted", "state: draft"
)


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.artifacts = self.root / "artifacts"
        lessons = self.artifacts / "lessons"
        lessons.mkdir(parents=True)
        (lessons / "L-1234abcd.md").write_text(LESSON)
        (lessons / "L-1234abce.md").write_text(DRAFT)
        self.connection = twill_schema.connect(self.state)
        self.addCleanup(self.connection.close)

        observation_rows = [
            (
                "session-lesson",
                "2026-09-20T12:00:00+00:00",
                "lesson evidence",
                "/workspace/repo",
                "/launch/project",
            ),
            (
                "session-open",
                "2026-09-27T12:00:00+00:00",
                "open evidence",
                "/workspace/repo",
                "/launch/project",
            ),
        ]
        self.connection.executemany(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, excerpt, cwd, launch_dir) "
            "VALUES (?, ?, ?, 'run_failed', ?, ?, ?)",
            (
                (session, ts, ts, excerpt, cwd, launch)
                for session, ts, excerpt, cwd, launch in observation_rows
            ),
        )
        self.connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, first_seen, last_seen, score, covered_by, state) "
            "VALUES ('D-01', 'open-key', 30, 1, 1, '2026-09-27T12:00:00+00:00', '2026-09-27T12:00:00+00:00', 5.0, NULL, 'open')"
        )
        self.connection.execute(
            "INSERT INTO cluster_session(detector_id, key, session_id) VALUES ('D-01', 'open-key', 'session-open')"
        )
        self.connection.commit()

    def test_brief_matches_repo_and_launch_dir_without_writing(self):
        records = list_lessons(self.artifacts, state="accepted")
        read_only = twill_schema.connect_read_only(self.state)
        self.addCleanup(read_only.close)
        before = twill_schema.state_db_path(self.state).stat().st_mtime_ns
        report = twill_brief.build_brief(
            read_only,
            "/workspace/repo",
            records,
            top_k=10,
        )
        after = twill_schema.state_db_path(self.state).stat().st_mtime_ns

        self.assertEqual(before, after)
        self.assertEqual(
            [item.lesson_id for item in report.accepted_lessons],
            ["L-1234abcd"],
        )
        self.assertEqual([item.key for item in report.open_clusters], ["open-key"])
        self.assertEqual(report.matched_by, ("cwd",))
        self.assertEqual(report.open_clusters[0].matched_sessions, 1)

        launch_report = twill_brief.build_brief(
            read_only,
            "/launch/project",
            records,
            top_k=10,
        )
        self.assertEqual(launch_report.matched_by, ("launch_dir",))

    def test_cli_human_output_is_plain_text_and_does_not_create_status(self):
        home = self.root / "home"
        config = home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(f'artifacts_root = "{self.artifacts}"\n')
        result = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "brief",
                "/launch/project",
                "--state-dir",
                str(self.state),
            ],
            cwd=ROOT,
            env={**os.environ, "HOME": str(home)},
            check=False,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TWILL brief: /launch/project", result.stdout)
        self.assertIn("L-1234abcd", result.stdout)
        self.assertIn("open-key", result.stdout)
        self.assertFalse((self.state / "status.json").exists())

    def test_json_output_is_serializable(self):
        read_only = twill_schema.connect_read_only(self.state)
        self.addCleanup(read_only.close)
        report = twill_brief.build_brief(
            read_only,
            "/workspace/repo",
            list_lessons(self.artifacts, state="accepted"),
        )
        payload = report.as_dict()
        self.assertEqual(json.loads(json.dumps(payload)), payload)


if __name__ == "__main__":
    unittest.main()
