import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_detectors  # noqa: E402
import twill_measure  # noqa: E402
import twill_schema  # noqa: E402
from twill_contract import EXIT_VALIDATION_FAILURE  # noqa: E402


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts = self.root / "artifacts"
        self.state = self.root / "state"
        self.artifacts.mkdir()

    def lesson(self, lesson_id, *, state="accepted", key="command-not-found:sqlite3"):
        directory = self.artifacts / "lessons"
        directory.mkdir(exist_ok=True)
        if state.startswith("applied:") or state in {"resolved", "escalated", "retired"}:
            layer = state.split(":", 1)[1] if ":" in state else "environment"
            routing = (
                f"routing: {{recommended: {layer}, applied: {layer}, "
                "applied_at: 2026-09-20T00:00:00Z, bead: twill-measure}"
            )
        else:
            routing = "routing: {recommended: null, applied: null, applied_at: null, bead: null}"
        text = "\n".join(
            [
                "---",
                f"id: {lesson_id}",
                'summary: "A command fails repeatedly. Install it before retrying."',
                f"state: {state}",
                "detector: D-01",
                f'key: "{key}"',
                'evidence: {sessions: 2, events: 2, first_seen: 2026-09-20, session_ids: ["s1", "s2"]}',
                routing,
                "backtest: {window_days: 180, sessions: 2, first_seen: 2026-09-20, weeks_present: 1}",
                "guard: {layer: null, artifact: null, installed: false}",
                "---",
                "",
            ]
        )
        path = directory / f"{lesson_id}.md"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def seed(self, connection, *, count=2, timestamp="2026-09-23T00:00:00+00:00"):
        for index in range(count):
            connection.execute(
                "INSERT INTO observation(session_id, ts_utc, ts_local, kind, program, "
                "signature, sig_hash) VALUES (?, ?, ?, 'run_failed', 'sqlite3', "
                "'command not found', ?)",
                (f"s{index + 1}", timestamp, timestamp, f"hash{index + 1}"),
            )
        connection.commit()

    def test_measure_records_versioned_counts_in_db_and_external_mirror(self):
        self.lesson("L-00000001")
        connection = twill_schema.connect(self.state)
        self.addCleanup(connection.close)
        self.seed(connection)

        report = twill_measure.measure_lessons(
            connection,
            self.artifacts,
            now="2026-09-24T12:00:00Z",
        )

        self.assertEqual(report.window_days, 7)
        self.assertEqual(len(report.measurements), 1)
        self.assertEqual(report.measurements[0].detector_id, "D-01@1")
        self.assertEqual(report.measurements[0].sessions, 2)
        self.assertEqual(report.measurements[0].events, 2)
        rows = connection.execute(
            "SELECT detector_id, window_days, sessions, events FROM measurement"
        ).fetchall()
        self.assertEqual(rows, [("D-01@1", 7, 2, 2)])
        path = twill_measure.measurement_path(self.artifacts, "L-00000001")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["lesson_id"], "L-00000001")
        self.assertEqual(payload["detector_id"], "D-01@1")

    def test_same_day_rerun_replaces_the_point_and_new_day_appends(self):
        self.lesson("L-00000001", state="applied:environment")
        connection = twill_schema.connect(self.state)
        self.addCleanup(connection.close)
        self.seed(connection)

        twill_measure.measure_lessons(
            connection, self.artifacts, now="2026-09-24T12:00:00Z"
        )
        twill_measure.measure_lessons(
            connection, self.artifacts, now="2026-09-24T18:00:00Z"
        )
        path = twill_measure.measurement_path(self.artifacts, "L-00000001")
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

        twill_measure.measure_lessons(
            connection, self.artifacts, now="2026-09-25T12:00:00Z"
        )
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(
            connection.execute("SELECT count(*) FROM measurement").fetchone()[0], 2
        )

    def test_mirror_restores_derived_rows_after_database_rebuild(self):
        self.lesson("L-00000001")
        connection = twill_schema.connect(self.state)
        self.seed(connection)
        twill_measure.measure_lessons(
            connection, self.artifacts, now="2026-09-24T12:00:00Z"
        )
        connection.close()
        shutil.rmtree(self.state)

        rebuilt = twill_schema.connect(self.state)
        self.addCleanup(rebuilt.close)
        restored = twill_measure.restore_measurements(rebuilt, self.artifacts)
        self.assertEqual(restored, 1)
        self.assertEqual(
            rebuilt.execute(
                "SELECT detector_id, sessions, events FROM measurement"
            ).fetchall(),
            [("D-01@1", 2, 2)],
        )

    def test_draft_is_skipped_and_active_registry_version_is_recorded(self):
        self.lesson("L-00000001", state="draft")
        self.lesson("L-00000002", key="command-not-found:other")
        self.lesson("L-00000003", state="resolved", key="command-not-found:resolved")
        connection = twill_schema.connect(self.state)
        self.addCleanup(connection.close)
        self.seed(connection)
        detector = twill_detectors.Detector(
            "D-01",
            2,
            "versioned measurement fixture",
            twill_detectors.MISSING_BINARY_SQL,
        )
        with mock.patch.object(twill_detectors, "REGISTRY", (detector,)):
            report = twill_measure.measure_lessons(
                connection, self.artifacts, now="2026-09-24T12:00:00Z"
            )
        self.assertEqual(report.skipped, ("L-00000001", "L-00000003"))
        self.assertEqual(report.measurements[0].detector_id, "D-01@2")

    def test_malformed_mirror_fails_before_database_writes(self):
        self.lesson("L-00000001")
        path = twill_measure.measurement_path(self.artifacts, "L-00000001")
        path.parent.mkdir(parents=True)
        path.write_text("not-json\n", encoding="utf-8")
        connection = twill_schema.connect(self.state)
        self.addCleanup(connection.close)
        with self.assertRaises(twill_measure.MeasurementError) as raised:
            twill_measure.measure_lessons(
                connection, self.artifacts, now="2026-09-24T12:00:00Z"
            )
        self.assertEqual(raised.exception.code, EXIT_VALIDATION_FAILURE)
        self.assertEqual(
            connection.execute("SELECT count(*) FROM measurement").fetchone()[0], 0
        )


class MeasurementCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.artifacts = Path(self.temporary.name) / "artifacts"
        config = self.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(f'artifacts_root = "{self.artifacts}"\n', encoding="utf-8")
        self.artifacts.mkdir()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "twill"), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home)},
            check=False,
            text=True,
            capture_output=True,
        )

    def test_measure_cli_emits_json_and_updates_status(self):
        lessons = self.artifacts / "lessons"
        lessons.mkdir()
        lesson = lessons / "L-00000001.md"
        lesson.write_text(
            "\n".join(
                [
                    "---",
                    "id: L-00000001",
                    'summary: "A command fails repeatedly. Install it before retrying."',
                    "state: accepted",
                    "detector: D-01",
                    'key: "command-not-found:sqlite3"',
                    'evidence: {sessions: 2, events: 2, first_seen: 2026-09-20, session_ids: ["s1", "s2"]}',
                    "routing: {recommended: null, applied: null, applied_at: null, bead: null}",
                    "backtest: {window_days: 180, sessions: 2, first_seen: 2026-09-20, weeks_present: 1}",
                    "guard: {layer: null, artifact: null, installed: false}",
                    "---",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        state = Path(self.temporary.name) / "state"
        connection = twill_schema.connect(state)
        try:
            for index in range(2):
                connection.execute(
                    "INSERT INTO observation(session_id, ts_utc, ts_local, kind, program, "
                    "signature, sig_hash) VALUES (?, ?, ?, 'run_failed', 'sqlite3', "
                    "'command not found', ?)",
                    (
                        f"s{index + 1}",
                        "2026-09-23T00:00:00+00:00",
                        "2026-09-23T00:00:00+00:00",
                        f"hash{index + 1}",
                    ),
                )
            connection.commit()
        finally:
            connection.close()

        result = self.run_cli("measure", "--json", "--state-dir", str(state))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["data"]["measurements"][0]["detector_id"], "D-01@1")
        status = json.loads(
            (state / "status.json").read_text(encoding="utf-8")
        )
        self.assertIn("measure", status["data"]["stages"])


if __name__ == "__main__":
    unittest.main()
