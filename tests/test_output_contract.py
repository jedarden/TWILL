import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))


class OutputContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ingest loads config at startup and artifacts_root is the one key
        # with no default (plan §13.1): every CLI run in this suite needs a
        # config that sets it to a directory outside the repository tree.
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

    def test_success_envelope_has_contract_fields_and_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            result = self.run_cli("digest", "--json", "--state-dir", str(state))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        envelope = json.loads(result.stdout)
        self.assertEqual(set(envelope), {"schema_version", "generated_at", "data", "warnings"})
        self.assertEqual(envelope["schema_version"], 1)
        self.assertIsInstance(envelope["warnings"], list)
        datetime.fromisoformat(envelope["generated_at"].replace("Z", "+00:00"))
        self.assertEqual(envelope["data"]["observations"], 0)

    def test_success_and_error_helpers_write_their_respective_streams(self):
        from twill_contract import (
            EXIT_LOCK_HELD,
            EXIT_RUNTIME_ERROR,
            EXIT_USAGE_ERROR,
            EXIT_VALIDATION_FAILURE,
            emit_error,
            emit_success,
            error_envelope,
        )

        self.assertEqual(
            [EXIT_RUNTIME_ERROR, EXIT_USAGE_ERROR, EXIT_LOCK_HELD, EXIT_VALIDATION_FAILURE],
            [1, 2, 3, 4],
        )
        self.assertEqual(error_envelope(EXIT_LOCK_HELD, "busy", "wait")["error"]["code"], 3)

        stdout = io.StringIO()
        stderr = io.StringIO()
        emit_success({"ok": True}, json_mode=True, stdout=stdout)
        emit_error(
            4,
            "bad token=ghp_1234567890abcdefghijklmnop",
            "fix it",
            json_mode=True,
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(stderr.getvalue(), "")
        lines = stdout.getvalue().splitlines()
        self.assertEqual(json.loads(lines[0])["data"], {"ok": True})
        error = json.loads(lines[1])["error"]
        self.assertEqual(error["code"], 4)
        self.assertNotIn("ghp_1234567890abcdefghijklmnop", lines[1])

    def test_usage_error_is_json_and_exit_two(self):
        result = self.run_cli("digest", "--json", "--not-a-real-option")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {
                "error": {
                    "code": 2,
                    "hint": "run 'twill <verb> --help' for usage",
                    "message": "unrecognized arguments: --not-a-real-option",
                }
            },
        )

    def test_runtime_error_is_json_and_redacts_path_credentials(self):
        token = "ghp_1234567890abcdefghijklmnop"
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / token
            result = self.run_cli(
                "ingest",
                "--json",
                "--file",
                str(missing),
                "--settle",
                "0",
                "--state-dir",
                str(Path(directory) / "state"),
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope["error"]["code"], 1)
        self.assertNotIn(token, result.stdout)
        self.assertIn("<redacted:github-token>", result.stdout)

    def test_human_runtime_error_remains_on_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(
                "ingest",
                "--file",
                str(Path(directory) / "missing.jsonl"),
                "--settle",
                "0",
                "--state-dir",
                str(Path(directory) / "state"),
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("twill: error:", result.stderr)
        self.assertIn("does not exist", result.stderr)


if __name__ == "__main__":
    unittest.main()
