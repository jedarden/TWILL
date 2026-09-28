import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_receipts import SCHEMA, iter_receipts, parse_receipt, read_receipt  # noqa: E402


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
            self.assertEqual([item.session_id for item in iter_receipts(root)], ["fixture-session"])

    def test_reader_rejects_duplicate_or_unknown_shape(self):
        with self.assertRaises(ValueError):
            parse_receipt({"schema": SCHEMA})
        with self.assertRaises(ValueError):
            parse_receipt(json.loads(FIXTURE.read_text()) | {"extra": True})


if __name__ == "__main__":
    unittest.main()
