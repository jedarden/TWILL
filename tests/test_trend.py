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

    def test_detector_identity_is_preserved_and_stale_waste_is_rederived(self):
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
        second = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.RECURRING_ERROR_SIGNATURE,),
            now=NOW,
        )
        self.assertEqual(second.exit_code, 0)
        # A stale value is replaced inside the trailing window and kept
        # outside it: the full run re-derives both weeks, then a narrow
        # 3-day pass touches only W39.
        self.connection.execute(
            "UPDATE cluster_week SET est_waste_usd = 1.25 "
            "WHERE detector_id = 'D-02'"
        )
        self.connection.commit()
        kept = twill_trend.attribute_weekly_waste(
            self.connection,
            registry=(twill_detectors.RECURRING_ERROR_SIGNATURE,),
            now=NOW,
            history_days=3,
        )

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
        self.assertEqual(kept, 1)

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

    def test_through_week_makes_historical_reports_ignore_future_buckets(self):
        for index in range(6):
            self.add_week("D-02", "existing", f"2026-W{33 + index:02d}", 1, 1)
        self.add_week("D-02", "future-key", "2026-W39", 2, 2)
        self.connection.commit()

        report = twill_trend.build_trend_report(
            self.connection,
            detector="D-02",
            weeks=6,
            through_week="2026-W38",
        )

        self.assertEqual(report.latest_week, "2026-W38")
        self.assertNotIn("future-key", {finding.key for finding in report.findings})
        self.assertNotIn("2026-W39", {finding.latest_week for finding in report.findings})


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


class WeeklyWasteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.connection = twill_schema.connect(Path(temporary.name) / "state")
        self.addCleanup(self.connection.close)

    def add_usage(self, session_id, cost_usd):
        self.connection.execute(
            "INSERT INTO session_usage(session_id, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd) VALUES (?, 10, 20, 30, ?)",
            (session_id, cost_usd),
        )
        self.connection.commit()

    def waste_rows(self):
        return self.connection.execute(
            "SELECT week, est_waste_usd FROM cluster_week ORDER BY week"
        ).fetchall()

    def test_cost_is_split_across_the_weeks_and_clusters_a_session_hits(self):
        # Session "a" hits the same cluster in two weeks (with a repeated
        # observation in W38, which must not increase that week's share),
        # "b" only W38, and "c" only W39 without a known cost.
        for session_id, day in (("a", 14), ("a", 15), ("b", 15), ("a", 21), ("c", 22)):
            seed_observation(
                self.connection, session_id, datetime(2026, 9, day, 10, tzinfo=timezone.utc)
            )
        for session_id, cost_usd in (("a", 2.0), ("b", 1.0)):
            self.add_usage(session_id, cost_usd)
        self.connection.execute(
            "INSERT INTO session_usage(session_id) VALUES ('c')"
        )
        self.connection.commit()

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.MISSING_BINARY,),
            now=NOW,
        )

        self.assertEqual(report.exit_code, 0)
        # "a" hit two cells, so each week receives half of its 2.0; "b"'s 1.0
        # lands wholly in W38; W39 stays unavailable because "c" lacks a cost.
        self.assertEqual(
            self.waste_rows(),
            [("2026-W38", 2.0), ("2026-W39", None)],
        )

    def test_cost_is_split_across_detectors_sharing_one_week(self):
        for session_id, day in (("s", 16), ("x", 17)):
            day_dt = datetime(2026, 9, day, 10, tzinfo=timezone.utc)
            seed_observation(self.connection, session_id, day_dt)
            seed_observation(
                self.connection,
                session_id,
                day_dt,
                program=None,
                kind="tool_error",
                signature="shared failure",
                sig_hash="shared-hash",
            )
        self.add_usage("s", 3.0)
        self.add_usage("x", 1.0)

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(
                twill_detectors.MISSING_BINARY,
                twill_detectors.RECURRING_ERROR_SIGNATURE,
            ),
            now=NOW,
        )

        self.assertEqual(report.exit_code, 0)
        # D-02 also clusters the command-not-found failures themselves, so
        # each session hit three distinct cells in W38 (one D-01, two D-02);
        # every cell receives the same (3.0 + 1.0) / 3 share.
        rows = self.connection.execute(
            "SELECT detector_id, key, est_waste_usd FROM cluster_week "
            "ORDER BY detector_id, key"
        ).fetchall()
        self.assertEqual(
            [(detector_id, key) for detector_id, key, _ in rows],
            [
                ("D-01", "command-not-found:sqlite3"),
                ("D-02", "shared failure"),
                ("D-02", "sqlite3: command not found"),
            ],
        )
        for _, _, waste_usd in rows:
            self.assertAlmostEqual(waste_usd, 4.0 / 3.0)

    def test_cost_is_unavailable_when_any_contributing_session_is_unknown(self):
        # s1..s3 hit both cluster cells and carry usage; s4 hits only the
        # sqlite3 cell and has no usage row at all, so that cell is
        # unavailable while the bf cell attributes its three contributors.
        for session_id, day in (("s1", 16), ("s2", 17), ("s3", 18)):
            day_dt = datetime(2026, 9, day, 10, tzinfo=timezone.utc)
            seed_observation(self.connection, session_id, day_dt)
            seed_observation(
                self.connection,
                session_id,
                day_dt,
                program="bf",
                signature="bf: command not found",
                sig_hash="hash-2",
            )
        seed_observation(
            self.connection, "s4", datetime(2026, 9, 19, 10, tzinfo=timezone.utc)
        )
        self.add_usage("s1", 2.0)
        self.add_usage("s2", 1.0)
        self.add_usage("s3", 1.0)

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.MISSING_BINARY,),
            now=NOW,
        )

        self.assertEqual(report.exit_code, 0)
        # Each bf contributor hit two cells, so the bf cell receives
        # (2.0 + 1.0 + 1.0) / 2; the sqlite3 cell stays unavailable because
        # s4's missing usage is unavailable, not zero.
        self.assertEqual(
            self.connection.execute(
                "SELECT key, est_waste_usd FROM cluster_week ORDER BY key"
            ).fetchall(),
            [
                ("command-not-found:bf", 2.0),
                ("command-not-found:sqlite3", None),
            ],
        )

    def test_detector_without_session_hit_sql_is_left_unattributed(self):
        seed_observation(
            self.connection, "a", datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
        )
        seed_observation(
            self.connection, "b", datetime(2026, 9, 23, 11, tzinfo=timezone.utc)
        )
        self.add_usage("a", 1.0)
        detector = twill_detectors.Detector(
            "D-96",
            1,
            "weekly series without session-hit SQL fixture",
            """
            SELECT program AS key, count(DISTINCT session_id) AS sessions,
                   count(*) AS events, min(ts_utc) AS first_seen,
                   max(ts_utc) AS last_seen
            FROM observation
            WHERE ts_utc >= :window_start_utc
            GROUP BY program
            """,
            weekly_hits_sql="""
            SELECT program AS key, strftime('%G-W%V', ts_utc) AS week,
                   count(DISTINCT session_id) AS sessions, count(*) AS events
            FROM observation
            WHERE ts_utc >= :window_start_utc AND ts_utc < :window_end_utc
            GROUP BY program, strftime('%G-W%V', ts_utc)
            """,
        )

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(detector,),
            now=NOW,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT sessions, events, est_waste_usd FROM cluster_week"
            ).fetchall(),
            [(2, 2, None)],
        )

    def test_failed_hit_query_skips_only_that_detector(self):
        seed_observation(
            self.connection, "a", datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
        )
        seed_observation(
            self.connection, "b", datetime(2026, 9, 23, 11, tzinfo=timezone.utc)
        )
        self.add_usage("a", 1.0)
        self.add_usage("b", 1.0)
        broken = twill_detectors.Detector(
            "D-95",
            1,
            "broken session-hit SQL fixture",
            """
            SELECT program AS key, count(DISTINCT session_id) AS sessions,
                   count(*) AS events, min(ts_utc) AS first_seen,
                   max(ts_utc) AS last_seen
            FROM observation
            WHERE ts_utc >= :window_start_utc
            GROUP BY program
            """,
            session_hits_sql="""
            SELECT key, session_id FROM missing_table
            """,
        )

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.MISSING_BINARY, broken),
            only=("D-01",),
            now=NOW,
        )

        # The isolated run attributes D-01 even though the registry carries a
        # detector whose hit query cannot run: it is skipped, not fatal.
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(self.waste_rows(), [("2026-W39", 2.0)])

    def test_narrowed_weekly_refresh_keeps_the_full_attribution_selection(self):
        # --only narrows the cluster refresh, never the waste pass: a
        # session's cost is split across every persisted cell it hit across
        # all detectors, so a narrowed refresh must not shrink the
        # denominator — nor drop the other detectors' rows from the pass.
        for session_id, day in (("a", 16), ("b", 17)):
            day_dt = datetime(2026, 9, day, 10, tzinfo=timezone.utc)
            seed_observation(self.connection, session_id, day_dt)
            seed_observation(
                self.connection,
                session_id,
                day_dt,
                program=None,
                kind="tool_error",
                signature="shared failure",
                sig_hash="shared-hash",
            )
        self.add_usage("a", 3.0)
        self.add_usage("b", 1.0)
        registry = (
            twill_detectors.MISSING_BINARY,
            twill_detectors.RECURRING_ERROR_SIGNATURE,
        )

        full = twill_trend.refresh_cluster_weeks(
            self.connection,
            registry=registry,
            now=NOW,
        )
        # Poison every estimate: only a pass that re-derives all three cells
        # can pass the assertions below.
        self.connection.execute("UPDATE cluster_week SET est_waste_usd = 9.0")
        self.connection.commit()

        narrowed = twill_trend.refresh_cluster_weeks(
            self.connection,
            registry=registry,
            only=("D-01",),
            now=NOW,
        )

        # The cluster refresh narrowed to D-01's single row, yet every cell —
        # D-02's included — re-derives to (3.0 + 1.0) / 3.
        self.assertEqual(full, 3)
        self.assertEqual(narrowed, 1)
        rows = self.connection.execute(
            "SELECT detector_id, key, est_waste_usd FROM cluster_week "
            "ORDER BY detector_id, key"
        ).fetchall()
        self.assertEqual(
            [(detector_id, key) for detector_id, key, _ in rows],
            [
                ("D-01", "command-not-found:sqlite3"),
                ("D-02", "shared failure"),
                ("D-02", "sqlite3: command not found"),
            ],
        )
        for _, _, waste_usd in rows:
            self.assertAlmostEqual(waste_usd, 4.0 / 3.0)

    def test_waste_pass_can_join_an_existing_transaction(self):
        seed_observation(
            self.connection, "a", datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
        )
        seed_observation(
            self.connection, "b", datetime(2026, 9, 23, 11, tzinfo=timezone.utc)
        )
        self.add_usage("a", 1.0)
        self.add_usage("b", 3.0)
        twill_trend.aggregate_detector_weeks(
            self.connection,
            twill_detectors.MISSING_BINARY,
            now=NOW,
        )
        self.connection.commit()

        updated = twill_trend.attribute_weekly_waste(
            self.connection,
            registry=(twill_detectors.MISSING_BINARY,),
            now=NOW,
            manage_transaction=False,
        )

        self.assertEqual(updated, 1)
        self.assertTrue(self.connection.in_transaction)
        self.connection.commit()
        self.assertEqual(self.waste_rows(), [("2026-W39", 4.0)])


if __name__ == "__main__":
    unittest.main()
