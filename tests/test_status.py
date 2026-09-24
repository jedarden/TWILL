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

from twill_status import read_status, record_stage, status_path  # noqa: E402


class StatusRecordTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state"

    def test_record_stage_writes_an_envelope_and_updates_in_place(self):
        first = record_stage(
            self.state,
            "ingest",
            0.25,
            {"sessions": 1, "events": 2, "observations": 2},
        )
        first_payload = read_status(self.state)
        self.assertEqual(first_payload["schema_version"], 1)
        self.assertEqual(set(first_payload), {"schema_version", "generated_at", "data", "warnings"})
        self.assertEqual(first_payload["data"]["stages"]["ingest"], first)
        self.assertEqual(status_path(self.state).stat().st_mode & 0o777, 0o600)

        second = record_stage(
            self.state,
            "ingest",
            0.5,
            {"sessions": 1, "events": 0, "observations": 2},
        )
        self.assertGreaterEqual(second["last_success"], first["last_success"])
        self.assertEqual(second["duration"], 0.5)
        self.assertEqual(second["counts"]["events"], 0)
        self.assertEqual(read_status(self.state)["data"]["stages"]["ingest"], second)

    def test_first_failed_attempt_is_recorded_without_a_success_time(self):
        record = record_stage(
            self.state,
            "detect",
            0.2,
            {"clusters": 0},
            succeeded=False,
        )
        self.assertIsNone(record["last_success"])
        self.assertEqual(read_status(self.state)["data"]["stages"]["detect"], record)

    def test_failed_attempt_does_not_replace_last_success(self):
        first = record_stage(self.state, "detect", 0.1, {"clusters": 3})
        record_stage(
            self.state,
            "detect",
            0.2,
            {"clusters": 0},
            succeeded=False,
        )
        self.assertEqual(read_status(self.state)["data"]["stages"]["detect"], first)

    def test_unknown_or_non_numeric_counts_are_rejected(self):
        for counts in ({"secret": 1}, {"clusters": "3"}, {"clusters": True}):
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                record_stage(self.state, "ingest", 0.1, counts)


class StatusCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.home = Path(cls.temporary.name) / "home"
        config = cls.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(f'artifacts_root = "{Path(cls.temporary.name) / "artifacts"}"\n')

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_cli(self, *args, home=None):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(home or self.home)},
            check=False,
            text=True,
            capture_output=True,
        )

    def fixture(self, root):
        source = root / "session.jsonl"
        source.write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "status-session",
                    "timestamp": "2026-09-24T00:00:00Z",
                    "message": {"role": "user", "content": "status fixture"},
                }
            )
            + "\n"
        )
        return source

    def test_ingest_records_stage_and_status_command_returns_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            source = self.fixture(root)
            ingested = self.run_cli(
                "ingest",
                "--file",
                str(source),
                "--settle",
                "0",
                "--limit",
                "1",
                "--state-dir",
                str(state),
                "--json",
            )
            self.assertEqual(ingested.returncode, 0, ingested.stderr)

            status = self.run_cli("status", "--json", "--state-dir", str(state))
            self.assertEqual(status.returncode, 0, status.stderr)
            payload = json.loads(status.stdout)
            self.assertEqual(
                set(payload), {"schema_version", "generated_at", "data", "warnings"}
            )
            record = payload["data"]["stages"]["ingest"]
            self.assertEqual(record["stage"], "ingest")
            self.assertEqual(record["counts"]["sessions"], 1)
            self.assertEqual(record["counts"]["events"], 1)
            self.assertGreaterEqual(record["duration"], 0)
            self.assertTrue(record["last_success"])

    def test_empty_source_is_a_successful_no_work_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sources"
            source.mkdir()
            state = root / "state"
            result = self.run_cli(
                "ingest",
                "--source",
                str(source),
                "--settle",
                "0",
                "--state-dir",
                str(state),
                "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = read_status(state)["data"]["stages"]["ingest"]
            self.assertEqual(record["counts"]["files"], 0)
            self.assertEqual(record["counts"]["events"], 0)
            self.assertTrue(record["last_success"])

    def test_unsettled_source_remains_a_runtime_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            state = root / "state"
            result = self.run_cli(
                "ingest",
                "--source",
                str(root),
                "--settle",
                "2h",
                "--state-dir",
                str(state),
                "--json",
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("no settled", result.stdout)

    def test_idle_ingest_refreshes_last_success_and_keeps_counts_numeric(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            source = self.fixture(root)
            first = self.run_cli(
                "ingest", "--file", str(source), "--settle", "0",
                "--state-dir", str(state), "--json",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_record = read_status(state)["data"]["stages"]["ingest"]
            second = self.run_cli(
                "ingest", "--file", str(source), "--settle", "0",
                "--state-dir", str(state), "--json",
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            second_record = read_status(state)["data"]["stages"]["ingest"]
            self.assertGreater(second_record["last_success"], first_record["last_success"])
            self.assertEqual(second_record["counts"]["events"], 0)
            self.assertTrue(all(isinstance(value, (int, float)) for value in second_record["counts"].values()))

    def test_detect_records_its_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            result = self.run_cli("detect", "--json", "--state-dir", str(state))
            self.assertEqual(result.returncode, 0, result.stderr)
            record = read_status(state)["data"]["stages"]["detect"]
            self.assertEqual(record["counts"]["detectors"], 1)
            self.assertEqual(record["counts"]["clusters"], 0)

    def test_missing_status_is_a_successful_empty_status(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            result = self.run_cli("status", "--json", "--state-dir", str(state))
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["data"]["stages"], {})
            self.assertTrue(payload["warnings"])
            self.assertFalse((state / "status.json").exists())

    def test_invalid_status_schema_is_a_validation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            (state / "status.json").write_text(
                json.dumps(
                    {
                        "schema_version": True,
                        "generated_at": "not-a-time",
                        "data": {"stages": {}},
                        "warnings": [],
                    }
                )
            )
            result = self.run_cli("status", "--json", "--state-dir", str(state))
            self.assertEqual(result.returncode, 4)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], 4)

    def test_malformed_status_is_a_runtime_error_in_json_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            (state / "status.json").write_text("not json")
            result = self.run_cli("status", "--json", "--state-dir", str(state))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], 1)


if __name__ == "__main__":
    unittest.main()
