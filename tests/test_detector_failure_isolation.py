"""Fixture-driven detector isolation and digest visibility checks."""

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "detectors" / "failure_isolation.json"
sys.path.insert(0, str(ROOT))

import twill_detectors  # noqa: E402
import twill_digest  # noqa: E402
import twill_schema  # noqa: E402


def detector_from_fixture(definition: dict[str, object]) -> twill_detectors.Detector:
    return twill_detectors.Detector(
        str(definition["detector_id"]),
        int(definition["version"]),
        str(definition["description"]),
        str(definition["sql"]),
    )


class DetectorFailureIsolationFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state"
        self.connection = twill_schema.connect(self.state)
        self.addCleanup(self.connection.close)
        self.connection.executemany(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, "
            "program, command, signature, sig_hash, excerpt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["session_id"],
                    row["ts_utc"],
                    row["ts_utc"],
                    row["kind"],
                    row.get("program"),
                    row.get("command"),
                    row.get("signature"),
                    row.get("sig_hash"),
                    row.get("excerpt"),
                )
                for row in self.fixture["observations"]
            ],
        )
        self.connection.commit()

    def detector(self, name: str) -> twill_detectors.Detector:
        return detector_from_fixture(self.fixture["detectors"][name])

    def run_detectors(self, registry):
        return twill_detectors.run_detectors(
            self.connection,
            window_days=self.fixture["window_days"],
            registry=registry,
            now=self.fixture["now"],
        )

    def test_healthy_commits_failures_isolate_and_exit_codes_are_distinct(self):
        healthy = self.detector("healthy")
        sql_error = self.detector("sql_error")
        contract_baseline = self.detector("contract_baseline")
        contract_refusing = self.detector("contract_refusing")

        baseline = self.run_detectors((healthy, contract_baseline))
        self.assertEqual(baseline.exit_code, 0)
        self.assertEqual(
            [(outcome.full_id, outcome.status) for outcome in baseline.outcomes],
            [("D-90@1", "ok"), ("D-92@1", "ok")],
        )
        contract_before = self.connection.execute(
            "SELECT detector_id, key, sessions, events, first_seen, last_seen "
            "FROM cluster WHERE detector_id = 'D-92'"
        ).fetchall()

        runtime = self.run_detectors((healthy, sql_error))
        self.assertEqual(runtime.exit_code, 1)
        self.assertEqual(
            [(outcome.full_id, outcome.status) for outcome in runtime.outcomes],
            [("D-90@1", "ok"), ("D-91@1", "error")],
        )
        self.assertIn("no such column", runtime.outcomes[1].error)

        validation = self.run_detectors((healthy, contract_refusing))
        self.assertEqual(validation.exit_code, 4)
        self.assertEqual(
            [(outcome.full_id, outcome.status) for outcome in validation.outcomes],
            [("D-90@1", "ok"), ("D-92@1", "refused")],
        )
        self.assertIn("bump the version", validation.outcomes[1].error)

        expected_healthy = self.fixture["expected"]["healthy"]
        healthy_rows = self.connection.execute(
            "SELECT key, sessions, events FROM cluster "
            "WHERE detector_id = 'D-90' ORDER BY key"
        ).fetchall()
        self.assertEqual(
            healthy_rows,
            [
                (key, values["sessions"], values["events"])
                for key, values in expected_healthy["rows"].items()
            ],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT key, sessions, events FROM cluster "
                "WHERE detector_id = 'D-91'"
            ).fetchall(),
            [],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id, key, sessions, events, first_seen, last_seen "
                "FROM cluster WHERE detector_id = 'D-92'"
            ).fetchall(),
            contract_before,
        )

        run_statuses = self.connection.execute(
            "SELECT detector_id, last_status, last_error FROM detector_run "
            "ORDER BY detector_id"
        ).fetchall()
        self.assertEqual(
            [(detector_id, status) for detector_id, status, _ in run_statuses],
            [("D-90", "ok"), ("D-92", "refused")],
        )
        self.assertIsNone(run_statuses[0][2])
        self.assertIn("bump the version", run_statuses[1][2])
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM detector_run WHERE detector_id = 'D-91'"
            ).fetchone()
        )

    def test_digest_reports_runtime_and_refused_detectors_as_skipped(self):
        healthy = self.detector("healthy")
        sql_error = self.detector("sql_error")
        contract_baseline = self.detector("contract_baseline")
        contract_refusing = self.detector("contract_refusing")

        self.run_detectors((healthy, contract_baseline))
        report = twill_digest.build_digest(
            self.state,
            twill_digest.parse_week(self.fixture["week"]),
            registry=(healthy, sql_error, contract_refusing),
        )

        summaries = {summary.full_id: summary for summary in report.detectors}
        self.assertEqual(summaries["D-90@1"].current_status, "ok")
        self.assertEqual(summaries["D-91@1"].current_status, "error")
        self.assertEqual(summaries["D-92@1"].current_status, "refused")
        self.assertIn("no such column", summaries["D-91@1"].current_error)
        self.assertIn("bump the version", summaries["D-92@1"].current_error)
        self.assertFalse(report.clean)

        data = twill_digest.render_data(report)
        data_by_detector = {row["detector"]: row for row in data["detectors"]}
        self.assertEqual(data_by_detector["D-90@1"]["current_status"], "ok")
        self.assertEqual(data_by_detector["D-91@1"]["current_status"], "error")
        self.assertEqual(
            data_by_detector["D-92@1"]["current_status"], "refused"
        )

        text = twill_digest.render_text(report)
        self.assertIn("detector skipped: D-91@1 2026-W38 (error)", text)
        self.assertIn("detector skipped: D-92@1 2026-W38 (refused)", text)
        self.assertIn("detector failures: D-91@1, D-92@1", text)


if __name__ == "__main__":
    unittest.main()
