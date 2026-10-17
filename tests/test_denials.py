import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_denials  # noqa: E402
import twill_doctor  # noqa: E402
import twill_reader  # noqa: E402


def record(**changes):
    value = {
        "ts": "2026-09-05T12:41:07Z",
        "rule_id": "mutating-kubectl",
        "tool": "Bash",
        "cwd": "/work/project",
        "session_id": "session-one",
        "fragment": "kubectl delete pod example",
    }
    value.update(changes)
    return value


class DenialReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.active = Path(temporary.name) / twill_denials.LOG_NAME
        self.backup = self.active.with_name(self.active.name + ".1")

    def write_lines(self, path, *lines):
        with path.open("wb") as handle:
            for line in lines:
                handle.write(line)

    def test_reads_rotated_then_active_records_without_losing_identity(self):
        old = record(session_id="old-session")
        new = record(ts="2026-09-05T12:42:00Z", rule_id="latest-image-tag", tool="Edit")
        self.write_lines(self.backup, (json.dumps(old) + "\n").encode())
        self.write_lines(self.active, (json.dumps(new) + "\n").encode())

        self.assertEqual(
            [
                (item.ts, item.rule_id, item.tool, item.session_id)
                for item in twill_denials.iter_denials(self.active)
            ],
            [
                (old["ts"], old["rule_id"], old["tool"], old["session_id"]),
                (new["ts"], new["rule_id"], new["tool"], new["session_id"]),
            ],
        )
        self.assertTrue(twill_denials.denials_live(self.active))

    def test_skips_bad_lines_and_a_torn_tail_independently(self):
        good = (json.dumps(record(session_id="good")) + "\n").encode()
        duplicate = (
            json.dumps(record())[:-1] + ', "ts": "2026-09-05T12:41:07Z"}\n'
        ).encode()
        incomplete = json.dumps(record(session_id="torn")).encode()
        self.write_lines(
            self.active,
            b"not json\n",
            b"\xff\n",
            duplicate,
            (json.dumps(record(extra=True)) + "\n").encode(),
            (json.dumps(record(ts="2026-13-05T12:41:07Z")) + "\n").encode(),
            good,
            incomplete,
        )

        self.assertEqual([item.session_id for item in twill_denials.iter_denials(self.active)], ["good"])

    def test_absence_and_only_invalid_data_are_not_live(self):
        self.assertEqual(list(twill_denials.iter_denials(self.active)), [])
        self.assertFalse(twill_denials.denials_live(self.active))
        self.write_lines(self.backup, b"broken\n")
        self.write_lines(self.active, json.dumps(record()).encode())
        self.assertFalse(twill_denials.denials_live(self.active))

    def test_parser_requires_the_hook_shape_and_bounds(self):
        for value in (
            record(rule_id=""),
            record(tool=1),
            record(fragment="x" * 81),
            record(session_id="x" * 513),
            record(ts="2026-09-05T12:41:07+00:00"),
            record(ts="2026-02-30T12:41:07Z"),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                twill_denials.parse_denial(value)
        self.assertEqual(twill_denials.parse_denial(record(session_id="")).session_id, "")

    def test_path_follows_the_producers_environment_and_probe_registers(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": "/state", "ORG_RULE_GUARD_STATE_DIR": ""}):
            self.assertEqual(
                twill_denials.denial_log_path(),
                Path("/state/org-rule-guard/denials.jsonl"),
            )
        with patch.dict(os.environ, {"ORG_RULE_GUARD_STATE_DIR": str(self.active.parent)}):
            self.assertEqual(twill_denials.denial_log_path(), self.active)
            registration = twill_denials.register_reader()
            self.assertEqual(registration.name, twill_denials.READER_NAME)
            self.assertFalse(registration.liveness())
            self.write_lines(self.active, (json.dumps(record()) + "\n").encode())
            self.assertTrue(registration.liveness())
            self.assertEqual(twill_reader.build_reader_registry(registration), (registration,))

    def test_unreadable_input_is_a_probe_error_for_doctor(self):
        self.active.mkdir()
        with patch.dict(os.environ, {"ORG_RULE_GUARD_STATE_DIR": str(self.active.parent)}):
            check = twill_doctor._check_optional_inputs((twill_denials.register_reader(),))
        self.assertEqual(check.status, twill_doctor.DEGRADED)
        self.assertEqual(check.details["probe_errors"], [twill_denials.READER_NAME])


if __name__ == "__main__":
    unittest.main()
