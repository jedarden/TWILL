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

import twill_schema  # noqa: E402
from twill_lock import StateLock  # noqa: E402


class StateLockTests(unittest.TestCase):
    def test_contending_mutating_verb_reports_pid_and_since(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with StateLock(state) as lock:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(CLI),
                        "ingest",
                        "--json",
                        "--file",
                        str(Path(directory) / "does-not-matter.jsonl"),
                        "--settle",
                        "0",
                        "--state-dir",
                        str(state),
                    ],
                    cwd=ROOT,
                    check=False,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 3, result.stderr)
                error = json.loads(result.stdout)["error"]
                self.assertEqual(error["code"], 3)
                self.assertEqual(error["message"].split()[0:3], ["lock", "held", "by"])
                self.assertIn(f"pid {os.getpid()}", error["message"])
                self.assertIn(" since ", error["message"])
                self.assertEqual(result.stderr, "")
                self.assertIsNotNone(lock.owner)

    def test_read_verb_does_not_take_state_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            connection = twill_schema.connect(state)
            connection.close()
            with StateLock(state):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(CLI),
                        "digest",
                        "--json",
                        "--state-dir",
                        str(state),
                    ],
                    cwd=ROOT,
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["data"]["observations"], 0)

    def test_read_verb_does_not_create_missing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "missing-state"
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "digest",
                    "--json",
                    "--state-dir",
                    str(state),
                ],
                cwd=ROOT,
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
