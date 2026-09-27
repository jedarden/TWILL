import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_detectors  # noqa: E402
import twill_schema  # noqa: E402
import twill_trend  # noqa: E402


NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


def seed_observation(
    connection,
    session_id,
    timestamp,
    *,
    program="sqlite3",
    kind="run_failed",
    signature="sqlite3: command not found",
    sig_hash="hash-1",
):
    connection.execute(
        "INSERT INTO observation(session_id, ts_utc, ts_local, kind, program, "
        "signature, sig_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            timestamp.isoformat(),
            timestamp.isoformat(),
            kind,
            program,
            signature,
            sig_hash,
        ),
    )


class WeeklyClusterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.connection = twill_schema.connect(Path(temporary.name) / "state")
        self.addCleanup(self.connection.close)

    def test_weekly_rows_group_distinct_sessions_and_events_per_iso_week(self):
        for session_id, timestamp in (
            ("a", datetime(2026, 9, 14, 10, tzinfo=timezone.utc)),
            ("a", datetime(2026, 9, 15, 10, tzinfo=timezone.utc)),
            ("b", datetime(2026, 9, 15, 11, tzinfo=timezone.utc)),
            ("c", datetime(2026, 9, 21, 10, tzinfo=timezone.utc)),
            ("d", datetime(2026, 9, 22, 10, tzinfo=timezone.utc)),
        ):
            seed_observation(self.connection, session_id, timestamp)
        self.connection.commit()

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.MISSING_BINARY,),
            now=NOW,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id, key, week, sessions, events, est_waste_usd "
                "FROM cluster_week ORDER BY week"
            ).fetchall(),
            [
                (
                    "D-01",
                    "command-not-found:sqlite3",
                    "2026-W38",
                    2,
                    3,
                    None,
                ),
                (
                    "D-01",
                    "command-not-found:sqlite3",
                    "2026-W39",
                    2,
                    2,
                    None,
                ),
            ],
        )

    def test_detector_identity_and_existing_waste_are_preserved(self):
        for session_id, day in (("a", 14), ("b", 15), ("c", 21), ("d", 22)):
            seed_observation(
                self.connection,
                session_id,
                datetime(2026, 9, day, 10, tzinfo=timezone.utc),
                program=None,
                kind="tool_error",
                signature="shared failure",
                sig_hash="shared-hash",
            )
        self.connection.commit()
        first = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.RECURRING_ERROR_SIGNATURE,),
            now=NOW,
        )
        self.assertEqual(first.exit_code, 0)
        self.connection.execute(
            "UPDATE cluster_week SET est_waste_usd = 1.25 "
            "WHERE detector_id = 'D-02' AND week = '2026-W38'"
        )
        self.connection.commit()

        second = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.RECURRING_ERROR_SIGNATURE,),
            now=NOW,
        )

        self.assertEqual(second.exit_code, 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id, key, week, sessions, events, est_waste_usd "
                "FROM cluster_week ORDER BY week"
            ).fetchall(),
            [
                ("D-02", "shared failure", "2026-W38", 2, 2, 1.25),
                ("D-02", "shared failure", "2026-W39", 2, 2, None),
            ],
        )

    def test_weekly_failure_rolls_back_cluster_and_series_together(self):
        seed_observation(
            self.connection,
            "a",
            datetime(2026, 9, 23, 10, tzinfo=timezone.utc),
        )
        seed_observation(
            self.connection,
            "b",
            datetime(2026, 9, 23, 11, tzinfo=timezone.utc),
        )
        self.connection.commit()
        detector = twill_detectors.Detector(
            "D-98",
            1,
            "weekly contract failure fixture",
            """
            SELECT 'weekly-failure' AS key, 1 AS sessions, 1 AS events,
                   min(ts_utc) AS first_seen, max(ts_utc) AS last_seen
            FROM observation
            WHERE ts_utc >= :window_start_utc
            """,
            weekly_hits_sql="SELECT 'weekly-failure', 'not-an-iso-week', 1, 1",
        )

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(detector,),
            now=NOW,
        )

        self.assertEqual(report.exit_code, 1)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM cluster").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM cluster_week").fetchone()[0],
            0,
        )

    def test_fallback_replay_does_not_carry_counts_across_weeks(self):
        detector = twill_detectors.Detector(
            "D-97",
            1,
            "weekly fallback fixture",
            """
            SELECT program AS key, count(DISTINCT session_id) AS sessions,
                   count(*) AS events, min(ts_utc) AS first_seen,
                   max(ts_utc) AS last_seen
            FROM observation
            WHERE ts_utc >= :window_start_utc
            GROUP BY program
            """,
        )
        for session_id, day in (("a", 14), ("a", 15), ("b", 21), ("b", 22)):
            seed_observation(
                self.connection,
                session_id,
                datetime(2026, 9, day, 10, tzinfo=timezone.utc),
            )
        self.connection.commit()

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(detector,),
            now=NOW,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT week, sessions, events FROM cluster_week ORDER BY week"
            ).fetchall(),
            [("2026-W38", 1, 2), ("2026-W39", 1, 2)],
        )

    def test_refresh_entry_point_accepts_a_registry(self):
        seed_observation(
            self.connection,
            "a",
            datetime(2026, 9, 23, 10, tzinfo=timezone.utc),
        )
        seed_observation(
            self.connection,
            "b",
            datetime(2026, 9, 23, 11, tzinfo=timezone.utc),
        )
        self.connection.commit()

        total = twill_trend.refresh_cluster_weeks(
            self.connection,
            registry=(twill_detectors.MISSING_BINARY,),
            now=NOW,
        )

        self.assertEqual(total, 1)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM cluster_week").fetchone()[0],
            1,
        )

    def test_aggregation_can_join_an_existing_transaction(self):
        seed_observation(
            self.connection,
            "a",
            datetime(2026, 9, 23, 10, tzinfo=timezone.utc),
        )
        seed_observation(
            self.connection,
            "b",
            datetime(2026, 9, 23, 11, tzinfo=timezone.utc),
        )

        rows = twill_trend.aggregate_detector_weeks(
            self.connection,
            twill_detectors.MISSING_BINARY,
            now=NOW,
            manage_transaction=False,
        )

        self.assertEqual(rows, 1)
        self.assertTrue(self.connection.in_transaction)
        self.connection.commit()


class EwmaTrendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.connection = twill_schema.connect(Path(temporary.name) / "state")
        self.addCleanup(self.connection.close)

    def add_week(self, detector, key, week, sessions, events):
        self.connection.execute(
            "INSERT INTO cluster_week(detector_id, key, week, sessions, events) "
            "VALUES (?, ?, ?, ?, ?)",
            (detector, key, week, sessions, events),
        )

    def test_acceleration_uses_the_signature_own_ewma_band(self):
        # The second key is large but flat.  It must not lend its volume to the
        # first key's baseline or appear as a change point itself.
        for index in range(6):
            week = f"2026-W{30 + index:02d}"
            self.add_week("D-02", "rising", week, 1, 1)
            self.add_week("D-02", "flat", week, 10, 10)
        self.add_week("D-02", "rising", "2026-W36", 4, 4)
        self.add_week("D-02", "flat", "2026-W36", 10, 10)
        self.connection.commit()

        report = twill_trend.build_trend_report(
            self.connection,
            detector="D-02",
            weeks=7,
        )

        self.assertEqual([finding.key for finding in report.findings], ["rising"])
        finding = report.findings[0]
        self.assertEqual(finding.status, twill_trend.TREND_ACCELERATING)
        self.assertEqual(finding.signal_metric, "events")
        self.assertEqual(finding.current_events, 4)
        self.assertEqual(finding.events_ewma, 1.0)
        self.assertEqual(finding.events_band, 1.0)
        self.assertTrue(report.history_sufficient)

    def test_new_key_is_reported_after_six_calendar_weeks(self):
        for index in range(6):
            week = f"2026-W{30 + index:02d}"
            self.add_week("D-02", "existing", week, 1, 1)
        self.add_week("D-02", "new-key", "2026-W35", 2, 2)
        self.connection.commit()

        report = twill_trend.build_trend_report(
            self.connection,
            detector="D-02",
            weeks=6,
        )

        self.assertEqual(
            [(finding.key, finding.status) for finding in report.findings],
            [("new-key", twill_trend.TREND_NEW)],
        )

    def test_insufficient_history_is_explicit_and_new_only_filters_it(self):
        self.add_week("D-02", "thin", "2026-W38", 2, 2)
        self.connection.commit()

        report = twill_trend.build_trend_report(
            self.connection,
            detector="D-02",
            weeks=12,
        )
        self.assertEqual(report.findings[0].status, twill_trend.TREND_INSUFFICIENT_HISTORY)
        self.assertFalse(report.history_sufficient)
        self.assertIn("at least 6 weeks", report.warnings[0])

        filtered = twill_trend.build_trend_report(
            self.connection,
            detector="D-02",
            weeks=12,
            new_only=True,
        )
        self.assertEqual(filtered.findings, ())
        self.assertEqual(filtered.warnings, report.warnings)

    def test_report_is_json_serializable(self):
        self.add_week("D-02", "thin", "2026-W38", 2, 2)
        self.connection.commit()

        report = twill_trend.build_trend_report(self.connection)
        import json

        self.assertEqual(json.loads(json.dumps(report.as_dict())), report.as_dict())


class TrendCommandTests(unittest.TestCase):
    def test_cli_exposes_detector_weeks_new_only_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            connection = twill_schema.connect(state)
            for index in range(6):
                week = f"2026-W{30 + index:02d}"
                connection.execute(
                    "INSERT INTO cluster_week(detector_id, key, week, sessions, events) "
                    "VALUES ('D-02', 'existing', ?, 1, 1)",
                    (week,),
                )
            connection.execute(
                "INSERT INTO cluster_week(detector_id, key, week, sessions, events) "
                "VALUES ('D-02', 'new-key', '2026-W35', 2, 2)"
            )
            connection.commit()
            connection.close()

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "twill"),
                    "trend",
                    "--detector",
                    "D-02",
                    "--weeks",
                    "6",
                    "--new-only",
                    "--json",
                    "--state-dir",
                    str(state),
                ],
                cwd=ROOT,
                env={**os.environ, "HOME": str(root / "home")},
                check=False,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(
            [(row["key"], row["status"]) for row in payload["data"]["findings"]],
            [("new-key", twill_trend.TREND_NEW)],
        )


if __name__ == "__main__":
    unittest.main()
