"""The accepted-lesson payload is a documented, drift-checked contract."""

from dataclasses import fields, make_dataclass
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_lessons import LessonRecord  # noqa: E402


EXPECTED_PAYLOAD_FIELDS = (
    "id",
    "summary",
    "state",
    "detector",
    "key",
    "evidence",
    "routing",
    "backtest",
    "guard",
    "body",
)
NOTE = ROOT / "docs" / "notes" / "lesson-payload-contract.md"


def public_payload_fields(record_type):
    """Return the dataclass fields that cross the lesson payload boundary."""

    return tuple(
        field.name
        for field in fields(record_type)
        if field.name != "path" and not field.name.startswith("_")
    )


def assert_payload_shape(record_type):
    actual = public_payload_fields(record_type)
    if actual != EXPECTED_PAYLOAD_FIELDS:
        raise AssertionError(
            f"LessonRecord payload drifted: expected {EXPECTED_PAYLOAD_FIELDS}, got {actual}"
        )


class LessonPayloadContractTests(unittest.TestCase):
    def test_current_lesson_record_shape_and_note_are_pinned(self):
        assert_payload_shape(LessonRecord)
        note = NOTE.read_text(encoding="utf-8")
        for field_name in EXPECTED_PAYLOAD_FIELDS:
            with self.subTest(field=field_name):
                self.assertIn(f"`{field_name}`", note)

    def test_synthetic_field_addition_and_removal_fail_the_shape_check(self):
        added = make_dataclass(
            "LessonRecordWithAddedField",
            [(name, object) for name in EXPECTED_PAYLOAD_FIELDS] + [("new_field", object)],
        )
        removed = make_dataclass(
            "LessonRecordWithRemovedField",
            [(name, object) for name in EXPECTED_PAYLOAD_FIELDS[:-1]],
        )

        for record_type in (added, removed):
            with self.subTest(record_type=record_type.__name__):
                with self.assertRaises(AssertionError):
                    assert_payload_shape(record_type)


if __name__ == "__main__":
    unittest.main()
