import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"


class Phase0CliTests(unittest.TestCase):
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

    def test_fixture_traverses_reader_redactor_store_detector_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "session.jsonl"
            state = root / "state"
            token = "ghp_1234567890abcdefghijklmnop"
            source.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": "fixture-session",
                                "timestamp": "2026-09-20T12:00:00Z",
                                "message": {"role": "user", "content": f"command failed with {token}"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "sessionId": "fixture-session",
                                "timestamp": "2026-09-20T12:00:01Z",
                                "message": {"role": "assistant", "content": [{"type": "text", "text": "error: command not found"}]},
                            }
                        ),
                    ]
                )
                + "\n"
            )

            ingest = self.run_cli(
                "ingest",
                "--file",
                str(source),
                "--settle",
                "0",
                "--limit",
                "1",
                "--state-dir",
                str(state),
            )
            self.assertEqual(ingest.returncode, 0, ingest.stderr)
            self.assertIn("observation(s)", ingest.stdout)

            digest = self.run_cli(
                "digest",
                "--stdout",
                "--week",
                "2026-W38",
                "--state-dir",
                str(state),
            )
            self.assertEqual(digest.returncode, 0, digest.stderr)
            self.assertIn("TWILL digest", digest.stdout)
            self.assertIn(
                "observations: 2 total, 2 current, 0 previous",
                digest.stdout,
            )
            self.assertIn(
                " | $ twill digest --week 2026-W38 --stdout",
                digest.stdout,
            )
            self.assertNotIn(token, digest.stdout)

            connection = sqlite3.connect(state / "twill.db")
            try:
                count = connection.execute("SELECT count(*) FROM observation").fetchone()[0]
                persisted = " ".join(
                    row[0] for row in connection.execute("SELECT text FROM transcript_event")
                )
            finally:
                connection.close()
            self.assertGreater(count, 0)
            self.assertNotIn(token, persisted)

    def test_settle_window_skips_recent_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recent.jsonl"
            source.write_text(json.dumps({"type": "message", "content": "hello"}) + "\n")
            result = self.run_cli(
                "ingest",
                "--file",
                str(source),
                "--settle",
                "2h",
                "--state-dir",
                str(root / "state"),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no settled", result.stderr)

    def test_codex_response_item_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rollout.jsonl"
            source.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "codex-session"},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "codex observation"}],
                        },
                    }
                )
                + "\n"
            )
            result = self.run_cli(
                "ingest",
                "--file",
                str(source),
                "--settle",
                "0",
                "--state-dir",
                str(root / "state"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            digest = self.run_cli(
                "digest",
                "--stdout",
                "--week",
                "2026-W38",
                "--state-dir",
                str(root / "state"),
            )
            self.assertEqual(digest.returncode, 0, digest.stderr)
            self.assertIn("week: 2026-W38", digest.stdout)


if __name__ == "__main__":
    unittest.main()
