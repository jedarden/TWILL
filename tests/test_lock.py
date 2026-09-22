import fcntl
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
from twill_lock import StateLock, _proc_lock_owner  # noqa: E402


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

    def test_proc_lock_owner_fallback_recovers_holding_pid(self):
        # /proc/locks prints the device as hex (%02x:%02x, fs/locks.c); a
        # decimal rendering matches nothing on devices with major > 9 and
        # would turn the EC-10 fallback into an opaque runtime error.
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "lock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                owner = _proc_lock_owner(lock_path)
            finally:
                os.close(fd)
            self.assertIsNotNone(owner, "own flock not recovered from /proc/locks")
            self.assertEqual(owner.pid, os.getpid())
            self.assertTrue(owner.since)


if __name__ == "__main__":
    unittest.main()
