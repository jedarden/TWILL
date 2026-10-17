import json
import os
import sqlite3
import sys
import tempfile
import unittest
import argparse
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_denials  # noqa: E402
import twill_detectors  # noqa: E402
import twill_doctor  # noqa: E402
import twill_reader  # noqa: E402
import twill_schema  # noqa: E402
from twill_config import TwillConfig  # noqa: E402
from twill_contract import EXIT_SUCCESS  # noqa: E402


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


class DenialEvidenceTests(unittest.TestCase):
    """D-04's evidence path: wholesale materialization from the rolling log."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name) / "state"
        self.active = Path(temporary.name) / twill_denials.LOG_NAME
        self.connection = twill_schema.connect(self.state_dir)
        self.addCleanup(self.connection.close)

    def write_lines(self, *lines):
        with self.active.open("wb") as handle:
            for line in lines:
                handle.write(line)

    def denial_rows(self):
        return self.connection.execute(
            "SELECT rule_id, tool, ts_utc, session_id FROM hook_denial "
            "ORDER BY ts_utc"
        ).fetchall()

    def test_materialization_replaces_the_table_with_the_window_read(self):
        first = dict(
            rule_id="mutating-kubectl",
            tool="Bash",
            ts="2026-10-15T12:00:00Z",
            session_id="s1",
        )
        self.write_lines((json.dumps(record(**first)) + "\n").encode())
        self.assertEqual(
            twill_denials.materialize_denials(self.connection, self.active), 1
        )

        # Rotation dropped the first record and the hook wrote two more:
        # the table mirrors this read exactly, never a blend of the two.
        second = (
            record(
                rule_id="mutating-kubectl",
                session_id="s2",
                ts="2026-10-15T13:00:00Z",
            ),
            record(
                rule_id="latest-image-tag",
                tool="Edit",
                ts="2026-10-15T14:00:00Z",
                session_id="s3",
            ),
        )
        self.write_lines(
            *[(json.dumps(item) + "\n").encode() for item in second]
        )
        self.assertEqual(
            twill_denials.materialize_denials(self.connection, self.active), 2
        )
        self.assertEqual(
            self.denial_rows(),
            [
                ("mutating-kubectl", "Bash", "2026-10-15T13:00:00+00:00", "s2"),
                ("latest-image-tag", "Edit", "2026-10-15T14:00:00+00:00", "s3"),
            ],
        )

    def test_repeated_denials_are_distinct_events_not_deduplicated(self):
        same = json.dumps(
            record(rule_id="mutating-kubectl", session_id="s1")
        ).encode()
        self.write_lines(same + b"\n", same + b"\n")
        self.assertEqual(
            twill_denials.materialize_denials(self.connection, self.active), 2
        )
        self.assertEqual(len(self.denial_rows()), 2)

    def test_stored_fields_are_re_redacted_and_rule_id_is_bounded(self):
        self.write_lines(
            (
                json.dumps(
                    record(
                        rule_id="leak: ghp_" + "x" * 36,
                        session_id="s1",
                    )
                )
                + "\n"
            ).encode(),
            (
                json.dumps(record(rule_id="r" * 400, session_id="s2")) + "\n"
            ).encode(),
        )
        twill_denials.materialize_denials(self.connection, self.active)
        rows = self.denial_rows()
        self.assertEqual(rows[0][0], "leak: <redacted:github-token>")
        self.assertEqual(len(rows[1][0]), twill_denials.MAX_RULE_ID_LENGTH)

    def test_absent_log_raises_file_not_found_and_keeps_prior_evidence(self):
        self.write_lines((json.dumps(record()) + "\n").encode())
        twill_denials.materialize_denials(self.connection, self.active)
        self.active.unlink()
        with self.assertRaises(FileNotFoundError):
            twill_denials.materialize_denials(self.connection, self.active)
        # The prior window survives: a failed refresh replaces nothing.
        self.assertEqual(len(self.denial_rows()), 1)

    def test_unreadable_log_propagates_for_the_detect_skip(self):
        self.active.parent.mkdir(parents=True, exist_ok=True)
        target = self.active.parent / "not-a-file"
        target.mkdir()
        with self.assertRaises(OSError):
            twill_denials.materialize_denials(self.connection, target)

    def test_availability_distinguishes_absence_from_an_empty_window(self):
        self.assertFalse(twill_denials.denial_log_available(self.active))
        self.assertTrue(self.active.parent.exists())
        self.write_lines(b"")  # exists, but holds nothing valid
        self.assertTrue(twill_denials.denial_log_available(self.active))
        self.assertEqual(twill_denials.denial_evidence_rows(self.active), [])


class DetectCommandHookDenialTests(unittest.TestCase):
    """The detect verb's denial-log wiring end to end."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.state_dir = Path(self._temporary.name) / "state"
        self.log_dir = Path(self._temporary.name) / "log"
        self.log_dir.mkdir()
        self.active = self.log_dir / twill_denials.LOG_NAME
        config = TwillConfig(artifacts_root=Path(self._temporary.name) / "artifacts")
        self._config_patch = patch.object(twill_app, "load_config")
        load_config = self._config_patch.start()
        load_config.return_value = config
        self.addCleanup(self._config_patch.stop)
        # The catalog side of detect is D-10's contract; hold it absent so
        # these tests exercise only the denial-log wiring.
        self._catalog_patch = patch.object(
            twill_app.twill_catalog,
            "load_and_record",
            return_value=(twill_app.twill_catalog.CatalogRead("absent"), None),
        )
        self._catalog_patch.start()
        self.addCleanup(self._catalog_patch.stop)
        self._registry_patch = patch.object(
            twill_detectors, "REGISTRY", (twill_detectors.HOOK_DENIAL,)
        )
        self._registry_patch.start()
        self.addCleanup(self._registry_patch.stop)

    def write_log(self, *values):
        with self.active.open("wb") as handle:
            for value in values:
                handle.write((json.dumps(value) + "\n").encode())

    def detect(self, **overrides):
        values = {
            "window": None,
            "detector": None,
            "state_dir": str(self.state_dir),
            "json": True,
        }
        values.update(overrides)
        captured = StringIO()
        with patch.dict(
            os.environ, {"ORG_RULE_GUARD_STATE_DIR": str(self.log_dir)}
        ):
            with redirect_stdout(captured):
                code = twill_app.detect_command(argparse.Namespace(**values))
        envelope = json.loads(captured.getvalue()) if values["json"] else captured.getvalue()
        return code, envelope

    def test_absent_log_skips_d04_without_touching_its_clusters(self):
        code, envelope = self.detect()
        self.assertEqual(code, EXIT_SUCCESS)
        payload = envelope["data"]
        outcome = payload["detectors"][0]
        self.assertEqual(outcome["detector_id"], "D-04")
        self.assertEqual(outcome["status"], "skipped")
        self.assertIn("absent", outcome["reason"])
        self.assertEqual(payload["denial_log"], {})
        self.assertTrue(any("D-04" in item for item in envelope["warnings"]))
        connection = sqlite3.connect(self.state_dir / "twill.db")
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM cluster WHERE detector_id = 'D-04'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_present_log_materializes_and_clusters_the_denials(self):
        self.write_log(
            record(
                rule_id="mutating-kubectl",
                session_id="s1",
                ts="2026-10-15T12:00:00Z",
            ),
            record(
                rule_id="mutating-kubectl",
                session_id="s2",
                ts="2026-10-15T13:00:00Z",
            ),
            record(
                rule_id="one-session-only",
                session_id="s3",
                ts="2026-10-15T14:00:00Z",
            ),
        )
        code, envelope = self.detect()
        self.assertEqual(code, EXIT_SUCCESS)
        payload = envelope["data"]
        outcome = payload["detectors"][0]
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["clusters"], 1)
        self.assertEqual(payload["denial_log"], {"evidence": 3})
        connection = sqlite3.connect(self.state_dir / "twill.db")
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT key, sessions, events FROM cluster "
                    "WHERE detector_id = 'D-04'"
                ).fetchall(),
                [("hook-denial:mutating-kubectl", 2, 2)],
            )
        finally:
            connection.close()

    def test_unreadable_log_skips_d04_with_the_read_failure(self):
        self.active.mkdir()
        code, envelope = self.detect()
        self.assertEqual(code, EXIT_SUCCESS)
        outcome = envelope["data"]["detectors"][0]
        self.assertEqual(outcome["status"], "skipped")
        self.assertIn("could not be read", outcome["reason"])

    def test_a_detector_subset_that_excludes_d04_never_reads_the_log(self):
        # A --detector selection without D-04 has no reason to touch the
        # denial log; the log directory stays unread and D-04 is unreported.
        self._registry_patch.stop()
        replacement = patch.object(
            twill_detectors, "REGISTRY", (twill_detectors.MISSING_BINARY,)
        )
        # setUp's cleanup holds the original patch's stop; this replacement
        # needs its own, or the narrowed registry leaks past this test.
        self.addCleanup(replacement.stop)
        replacement.start()
        # A directory in place of the log would fail any read; the run must
        # not attempt one.
        self.active.mkdir()
        code, envelope = self.detect()
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(envelope["data"]["denial_log"], {})
        outcomes = {
            entry["detector_id"]: entry
            for entry in envelope["data"]["detectors"]
        }
        self.assertEqual(outcomes["D-01"]["status"], "ok")
        self.assertNotIn("D-04", outcomes)


if __name__ == "__main__":
    unittest.main()
