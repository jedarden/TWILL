"""Settle-window eligibility gate tests (plan §4 "settled session", §8.1 EC-01).

A transcript file is eligible for ingest only when its age -- now minus
mtime -- is at least the settle window (default 2h, configurable).  These
tests pin the gate's exact contract: the boundary is inclusive, a young file
is skipped whole (never parsed partially, whatever else the run ingests),
an explicitly named file crosses the same gate, and a future mtime is never
eligible -- a negative age is less than any window, including zero.

The two boundary tests keep a 0.5 s margin on either side of the edge rather
than aiming at exact equality: the gate snapshots ``now`` internally, so a
test that backdates by precisely the window would race that snapshot.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

from twill_app import settled_files  # noqa: E402


def claude_line(session_id: str, text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-09-22T12:00:00Z",
            "cwd": "/workspace/demo",
            "message": {"role": "user", "content": text},
        }
    )


class SettleGateUnitTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def write(self, name: str, age_seconds: float) -> Path:
        path = self.root / name
        path.write_text(claude_line("session-" + name.removesuffix(".jsonl"), name) + "\n")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_file_aged_past_the_window_is_eligible(self):
        old = self.write("old.jsonl", 2 * 3600 + 0.5)
        self.assertEqual(settled_files([self.root], 2 * 3600), [old.resolve()])

    def test_file_inside_the_window_is_skipped(self):
        self.write("young.jsonl", 2 * 3600 - 0.5)
        self.assertEqual(settled_files([self.root], 2 * 3600), [])

    def test_skip_is_whole_file_in_a_mixed_enumeration(self):
        old = self.write("old.jsonl", 3 * 3600)
        self.write("young.jsonl", 60)
        self.assertEqual(settled_files([self.root], 2 * 3600), [old.resolve()])

    def test_explicit_file_crosses_the_same_gate(self):
        young = self.write("young.jsonl", 60)
        self.assertEqual(settled_files([self.root], 2 * 3600, explicit_file=young), [])
        old = self.write("old.jsonl", 3 * 3600)
        self.assertEqual(
            settled_files([self.root], 2 * 3600, explicit_file=old), [old.resolve()]
        )

    def test_future_mtime_is_not_eligible_even_at_a_zero_window(self):
        self.write("ahead.jsonl", -3600)
        self.assertEqual(settled_files([self.root], 0), [])


class SettleGateCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ingest loads config at startup; artifacts_root has no default.
        cls._config_home = tempfile.TemporaryDirectory()
        home = Path(cls._config_home.name)
        config_dir = home / ".config" / "twill"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            f'artifacts_root = "{home / "artifacts"}"\n'
        )

    @classmethod
    def tearDownClass(cls):
        cls._config_home.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(Path(self._config_home.name))},
            check=False,
            text=True,
            capture_output=True,
        )

    def test_young_file_is_skipped_whole_while_a_settled_one_ingests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            settled = root / "settled.jsonl"
            fresh = root / "fresh.jsonl"
            settled.write_text(claude_line("cli-settled", "settled turn") + "\n")
            fresh.write_text(claude_line("cli-fresh", "fresh turn") + "\n")
            stamp = time.time() - 3 * 3600
            os.utime(settled, (stamp, stamp))

            result = self.run_cli(
                "ingest",
                "--source",
                str(root),
                "--settle",
                "2h",
                "--limit",
                "5",
                "--state-dir",
                str(state),
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            connection = sqlite3.connect(state / "twill.db")
            try:
                cursor_paths = [
                    row[0]
                    for row in connection.execute("SELECT path FROM cursor").fetchall()
                ]
                stored_events = " ".join(
                    row[0] for row in connection.execute("SELECT text FROM transcript_event")
                )
                observed_sessions = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT session_id FROM observation"
                    ).fetchall()
                }
            finally:
                connection.close()

            # Skipped whole: the young file never reached the store in any
            # form -- no cursor row, no event, no observation.
            self.assertEqual(cursor_paths, [str(settled)])
            self.assertNotIn("cli-fresh", stored_events)
            self.assertNotIn("fresh turn", stored_events)
            self.assertEqual(observed_sessions, {"cli-settled"})


if __name__ == "__main__":
    unittest.main()
