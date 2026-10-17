import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_app import read_session  # noqa: E402
from twill_receipts import (  # noqa: E402
    MAX_CWD,
    READER_NAME,
    SCHEMA,
    iter_receipts,
    parse_receipt,
    read_receipt,
    read_session_receipt,
    receipt_directory,
    register_reader,
)


FIXTURE = ROOT / "tests" / "fixtures" / "receipts" / "friction-v1.json"


class ReceiptReaderTests(unittest.TestCase):
    def test_fixture_round_trips_through_the_public_reader(self):
        receipt = read_receipt(FIXTURE)
        self.assertEqual(receipt.schema, SCHEMA)
        self.assertEqual(receipt.session_id, "fixture-session")
        self.assertEqual(receipt.rules_consulted, ("AGENTS.md", "skills/testing"))
        self.assertEqual(receipt.denials[0]["rule_id"], "latest-image-tag")
        self.assertEqual(receipt.unresolved_errors[0]["kind"], "tool_error")

    def test_reader_skips_malformed_and_secret_bearing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.json").write_text("not json\n")
            unsafe = json.loads(FIXTURE.read_text())
            unsafe["cwd"] = "ghp_" + "A" * 40
            (root / "unsafe.json").write_text(json.dumps(unsafe))
            (root / "good.json").write_text(FIXTURE.read_text())
            self.assertEqual(
                [item.session_id for item in iter_receipts(root)], ["fixture-session"]
            )

    def test_reader_rejects_duplicate_or_unknown_shape(self):
        with self.assertRaises(ValueError):
            parse_receipt({"schema": SCHEMA})
        with self.assertRaises(ValueError):
            parse_receipt(json.loads(FIXTURE.read_text()) | {"extra": True})

    def test_reader_enforces_producer_bounds_and_skips_symlinks(self):
        value = json.loads(FIXTURE.read_text())
        with self.assertRaises(ValueError):
            parse_receipt(value | {"cwd": "x" * (MAX_CWD + 1)})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text(FIXTURE.read_text())
            (root / "link.json").symlink_to(target)
            self.assertEqual(
                [receipt.session_id for receipt in iter_receipts(root)],
                ["fixture-session"],
            )

    def test_default_directory_and_doctor_registration_follow_optional_input(self):
        with tempfile.TemporaryDirectory() as directory:
            state_home = Path(directory) / "state"
            receipt_dir = state_home / "twill" / "friction-receipts"
            receipt_dir.mkdir(parents=True)
            receipt_path = receipt_dir / "fixture-session.json"
            receipt_path.write_text(FIXTURE.read_text())
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
                self.assertEqual(receipt_directory(), receipt_dir)
                registration = register_reader()
                self.assertEqual(registration.name, READER_NAME)
                self.assertTrue(registration.liveness())

    def test_session_parser_uses_matching_receipt_and_falls_back_when_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "fixture-session",
                        "timestamp": "2026-09-28T19:00:00Z",
                        "message": {"content": "finish the task"},
                    }
                )
                + "\n"
            )
            receipt_dir = root / "receipts"
            receipt_dir.mkdir()
            (receipt_dir / "fixture-session.json").write_text(FIXTURE.read_text())

            session = read_session(transcript, receipt_dir)
            self.assertIsNotNone(session.receipt)
            assert session.receipt is not None
            self.assertEqual(
                session.receipt.rules_consulted, ("AGENTS.md", "skills/testing")
            )
            self.assertEqual(session.receipt.denials[0]["tool"], "Write")
            self.assertEqual(
                session.receipt.unresolved_errors[0]["signature"], "tests failed"
            )
            self.assertFalse(session.receipt.ended_mid_task)

            transcript_only = read_session(transcript, root / "missing-receipts")
            self.assertIsNone(transcript_only.receipt)
            self.assertEqual(transcript_only.events, session.events)

    def test_session_parser_ignores_mismatched_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt_dir = root / "receipts"
            receipt_dir.mkdir()
            value = json.loads(FIXTURE.read_text())
            value["session_id"] = "another-session"
            (receipt_dir / "fixture-session.json").write_text(json.dumps(value))
            self.assertIsNone(read_session_receipt("fixture-session", receipt_dir))


if __name__ == "__main__":
    unittest.main()
