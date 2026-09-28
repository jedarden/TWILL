"""Independent contract tests for detector auxiliary queries.

The cluster query is deliberately not used by the direct session-hit and
week-hit tests below.  Those tests exercise the documented auxiliary output
contracts on their own, so a detector whose primary query happens to pass
cannot hide a broken attribution or backtest query.
"""

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_detectors  # noqa: E402
import twill_schema  # noqa: E402
from twill_contract import EXIT_RUNTIME_ERROR, EXIT_SUCCESS, EXIT_VALIDATION_FAILURE  # noqa: E402


NOW = "2026-09-24T12:00:00+00:00"
WINDOW_START = "2026-08-25T12:00:00+00:00"


PRIMARY_CLUSTER_SQL = """
    SELECT 'finding' AS key,
           count(DISTINCT session_id) AS sessions,
           count(*) AS events,
           min(ts_utc) AS first_seen,
           max(ts_utc) AS last_seen
    FROM observation
    WHERE ts_utc >= :window_start_utc
"""

SESSION_HITS_SQL = """
    SELECT 'finding' AS key, session_id
    FROM observation
    WHERE ts_utc >= :window_start_utc
    GROUP BY session_id
    ORDER BY session_id
"""


def make_detector(
    detector_id="D-90",
    version=1,
    *,
    cluster_sql=PRIMARY_CLUSTER_SQL,
    session_hits_sql=None,
    week_hits_sql=None,
):
    return twill_detectors.Detector(
        detector_id,
        version,
        "auxiliary-query contract fixture",
        cluster_sql,
        session_hits_sql=session_hits_sql,
        week_hits_sql=week_hits_sql,
    )


class AuxiliaryQueryContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.connection = twill_schema.connect(Path(self.temporary.name) / "state")
        self.addCleanup(self.connection.close)

    def seed_observations(self):
        rows = [
            ("s1", "2026-09-01T10:00:00+00:00"),
            ("s1", "2026-09-02T10:00:00+00:00"),
            ("s1", "2026-09-03T10:00:00+00:00"),
            ("s2", "2026-09-09T10:00:00+00:00"),
        ]
        self.connection.executemany(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, "
            "program) VALUES (?, ?, ?, 'run_failed', 'fixture')",
            [(session_id, timestamp, timestamp) for session_id, timestamp in rows],
        )
        self.connection.commit()

    def test_session_hits_are_validated_without_running_primary_query(self):
        self.seed_observations()
        detector = make_detector(session_hits_sql=SESSION_HITS_SQL)
        cursor = self.connection.execute(
            detector.session_hits_sql,
            {"window_start_utc": WINDOW_START, "window_days": 30},
        )

        hits = twill_detectors._collect_session_hits(
            detector,
            cursor,
            {"finding": (2, 4, "first", "last")},
        )

        self.assertEqual(hits, (("finding", "s1"), ("finding", "s2")))

    def test_session_hit_output_contract_deduplicates_rows(self):
        self.seed_observations()
        expected = {"finding": (2, 4, "first", "last")}

        missing_session = make_detector(
            session_hits_sql="SELECT 'finding' AS key FROM observation"
        )
        with self.assertRaisesRegex(
            twill_detectors.DetectorContractError, "session_id"
        ):
            twill_detectors._collect_session_hits(
                missing_session,
                self.connection.execute(missing_session.session_hits_sql),
                expected,
            )

        duplicate_session = make_detector(
            session_hits_sql=(
                "SELECT 'finding' AS key, session_id FROM observation "
                "WHERE session_id = 's1'"
            )
        )
        self.assertEqual(
            twill_detectors._collect_session_hits(
                duplicate_session,
                self.connection.execute(duplicate_session.session_hits_sql),
                {"finding": (1, 3, "first", "last")},
            ),
            (("finding", "s1"),),
        )

    def test_week_hits_count_distinct_iso_weeks_without_running_primary_query(self):
        self.seed_observations()
        week_hits_sql = """
            SELECT 'finding' AS key,
                   CASE WHEN ts_utc < '2026-09-07T00:00:00+00:00'
                        THEN '2026-W36' ELSE '2026-W37' END AS week
            FROM observation
            WHERE ts_utc >= :window_start_utc
        """
        detector = make_detector(week_hits_sql=week_hits_sql)

        self.assertEqual(
            twill_detectors.read_cluster_weeks(
                self.connection,
                detector,
                "finding",
                window_start_utc=WINDOW_START,
                window_days=30,
            ),
            2,
        )

    def test_week_hit_output_contract_rejects_missing_and_invalid_weeks(self):
        for sql in (
            "SELECT 'finding' AS key",
            "SELECT 'finding' AS key, 'not-an-iso-week' AS week",
            "SELECT 'finding' AS key, '2025-W53' AS week",
        ):
            detector = make_detector(week_hits_sql=sql)
            with self.subTest(sql=sql), self.assertRaises(
                twill_detectors.DetectorContractError
            ):
                twill_detectors.read_cluster_weeks(
                    self.connection,
                    detector,
                    "finding",
                    window_start_utc=WINDOW_START,
                    window_days=30,
                )

    def test_attribution_rows_and_hash_are_persisted(self):
        self.seed_observations()
        detector = make_detector(session_hits_sql=SESSION_HITS_SQL)

        report = twill_detectors.run_detectors(
            self.connection,
            registry=(detector,),
            window_days=30,
            now=NOW,
        )

        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id, key, session_id FROM cluster_session"
            ).fetchall(),
            [("D-90", "finding", "s1"), ("D-90", "finding", "s2")],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT full_id, attribution_sha FROM detector_run "
                "WHERE detector_id = 'D-90' AND version = 1"
            ).fetchone(),
            ("D-90@1", detector.attribution_sha),
        )

    def test_auxiliary_hashes_ignore_whitespace_only_edits(self):
        session_sql = SESSION_HITS_SQL
        week_sql = "SELECT 'finding' AS key, '2026-W36' AS week"
        reflowed_session = "  " + " \n ".join(session_sql.split()) + "  "
        reflowed_week = "\n" + "\n".join(week_sql.split()) + "\n"

        original = make_detector(
            session_hits_sql=session_sql,
            week_hits_sql=week_sql,
        )
        reflowed = make_detector(
            session_hits_sql=reflowed_session,
            week_hits_sql=reflowed_week,
        )

        self.assertEqual(original.attribution_sha, reflowed.attribution_sha)
        self.assertEqual(original.backtest_sha, reflowed.backtest_sha)

    def test_each_query_hash_is_stamped_and_version_bump_redefines_all_of_them(self):
        self.seed_observations()
        week_sql = "SELECT 'finding' AS key, '2026-W36' AS week"
        first = make_detector(
            session_hits_sql=SESSION_HITS_SQL,
            week_hits_sql=week_sql,
        )
        self.assertEqual(
            twill_detectors.run_detectors(
                self.connection, registry=(first,), window_days=30, now=NOW
            ).exit_code,
            EXIT_SUCCESS,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT semantics_sha, attribution_sha, backtest_sha "
                "FROM detector_run WHERE detector_id = 'D-90' AND version = 1"
            ).fetchone(),
            (first.semantics_sha, first.attribution_sha, first.backtest_sha),
        )

        second = make_detector(
            version=2,
            cluster_sql=PRIMARY_CLUSTER_SQL + "\n",
            session_hits_sql=SESSION_HITS_SQL.replace(
                "ORDER BY session_id", "ORDER BY session_id DESC"
            ),
            week_hits_sql="SELECT 'finding' AS key, '2026-W37' AS week",
        )
        self.assertEqual(
            twill_detectors.run_detectors(
                self.connection, registry=(second,), window_days=30, now=NOW
            ).exit_code,
            EXIT_SUCCESS,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT version, semantics_sha, attribution_sha, backtest_sha "
                "FROM detector_run WHERE detector_id = 'D-90' ORDER BY version"
            ).fetchall(),
            [
                (1, first.semantics_sha, first.attribution_sha, first.backtest_sha),
                (2, second.semantics_sha, second.attribution_sha, second.backtest_sha),
            ],
        )

    def test_attribution_hash_drift_is_refused_without_replacing_persisted_hits(self):
        self.seed_observations()
        first = make_detector(session_hits_sql=SESSION_HITS_SQL)
        self.assertEqual(
            twill_detectors.run_detectors(
                self.connection, registry=(first,), window_days=30, now=NOW
            ).exit_code,
            EXIT_SUCCESS,
        )
        clusters_before = self.connection.execute(
            "SELECT detector_id, key, sessions, events, first_seen, last_seen "
            "FROM cluster ORDER BY detector_id, key"
        ).fetchall()

        changed_sql = SESSION_HITS_SQL.replace(
            "SELECT 'finding' AS key, session_id",
            "SELECT 'finding' AS key, session_id || '-changed' AS session_id",
        )
        changed = make_detector(session_hits_sql=changed_sql)
        report = twill_detectors.run_detectors(
            self.connection, registry=(changed,), window_days=30, now=NOW
        )

        self.assertEqual(report.exit_code, EXIT_VALIDATION_FAILURE)
        self.assertEqual(report.outcomes[0].status, "refused")
        self.assertIn("waste-attribution semantics changed", report.outcomes[0].error)
        self.assertEqual(
            self.connection.execute(
                "SELECT key, session_id FROM cluster_session "
                "ORDER BY session_id"
            ).fetchall(),
            [("finding", "s1"), ("finding", "s2")],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id, key, sessions, events, first_seen, last_seen "
                "FROM cluster ORDER BY detector_id, key"
            ).fetchall(),
            clusters_before,
        )

    def test_shared_semantics_validation_rejects_session_hit_drift(self):
        self.seed_observations()
        first = make_detector(session_hits_sql=SESSION_HITS_SQL)
        self.assertEqual(
            twill_detectors.run_detectors(
                self.connection, registry=(first,), window_days=30, now=NOW
            ).exit_code,
            EXIT_SUCCESS,
        )
        changed = make_detector(
            session_hits_sql=SESSION_HITS_SQL.replace(
                "ORDER BY session_id", "ORDER BY session_id DESC"
            )
        )

        with self.assertRaisesRegex(
            twill_detectors.DetectorContractError,
            "waste-attribution semantics changed",
        ):
            twill_detectors.validate_detector_semantics(self.connection, changed)

    def test_removing_session_hits_is_refused_without_replacing_clusters(self):
        self.seed_observations()
        first = make_detector(session_hits_sql=SESSION_HITS_SQL)
        self.assertEqual(
            twill_detectors.run_detectors(
                self.connection, registry=(first,), window_days=30, now=NOW
            ).exit_code,
            EXIT_SUCCESS,
        )
        clusters_before = self.connection.execute(
            "SELECT detector_id, key, sessions, events, first_seen, last_seen "
            "FROM cluster ORDER BY detector_id, key"
        ).fetchall()

        report = twill_detectors.run_detectors(
            self.connection,
            registry=(make_detector(),),
            window_days=30,
            now=NOW,
        )

        self.assertEqual(report.exit_code, EXIT_VALIDATION_FAILURE)
        self.assertEqual(report.outcomes[0].status, "refused")
        self.assertIn("removed waste-attribution semantics", report.outcomes[0].error)
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id, key, sessions, events, first_seen, last_seen "
                "FROM cluster ORDER BY detector_id, key"
            ).fetchall(),
            clusters_before,
        )

    def test_version_bump_allows_new_attribution_hash_and_replaces_hits(self):
        self.seed_observations()
        first = make_detector(session_hits_sql=SESSION_HITS_SQL)
        twill_detectors.run_detectors(
            self.connection, registry=(first,), window_days=30, now=NOW
        )
        changed_sql = SESSION_HITS_SQL.replace(
            "SELECT 'finding' AS key, session_id",
            "SELECT 'finding' AS key, session_id || '-changed' AS session_id",
        )
        second = make_detector(
            version=2,
            session_hits_sql=changed_sql,
        )

        report = twill_detectors.run_detectors(
            self.connection, registry=(second,), window_days=30, now=NOW
        )

        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        self.assertEqual(
            self.connection.execute(
                "SELECT key, session_id FROM cluster_session "
                "ORDER BY session_id"
            ).fetchall(),
            [("finding", "s1-changed"), ("finding", "s2-changed")],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT full_id, attribution_sha FROM detector_run "
                "WHERE detector_id = 'D-90' ORDER BY version"
            ).fetchall(),
            [
                ("D-90@1", first.attribution_sha),
                ("D-90@2", second.attribution_sha),
            ],
        )

    def test_week_hash_drift_is_refused_under_the_same_version(self):
        self.seed_observations()
        first = make_detector(
            week_hits_sql="SELECT 'finding' AS key, '2026-W36' AS week"
        )
        twill_detectors.run_detectors(
            self.connection, registry=(first,), window_days=30, now=NOW
        )
        changed = make_detector(
            week_hits_sql="SELECT 'finding' AS key, '2026-W37' AS week"
        )

        report = twill_detectors.run_detectors(
            self.connection, registry=(changed,), window_days=30, now=NOW
        )

        self.assertEqual(report.exit_code, EXIT_VALIDATION_FAILURE)
        self.assertEqual(report.outcomes[0].status, "refused")
        self.assertIn("backtest semantics changed", report.outcomes[0].error)

    def test_auxiliary_failures_are_isolated_from_healthy_detectors(self):
        self.seed_observations()
        healthy = make_detector(
            detector_id="D-90", session_hits_sql=SESSION_HITS_SQL
        )
        missing_column = make_detector(
            detector_id="D-91",
            session_hits_sql="SELECT 'finding' AS key FROM observation",
        )
        sql_error = make_detector(
            detector_id="D-92",
            session_hits_sql=(
                "SELECT 'finding' AS key, session_id FROM missing_auxiliary_table"
            ),
        )

        report = twill_detectors.run_detectors(
            self.connection,
            registry=(healthy, missing_column, sql_error),
            window_days=30,
            now=NOW,
        )

        self.assertEqual(report.exit_code, EXIT_RUNTIME_ERROR)
        self.assertEqual(
            [(outcome.full_id, outcome.status) for outcome in report.outcomes],
            [("D-90@1", "ok"), ("D-91@1", "error"), ("D-92@1", "error")],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id, key FROM cluster ORDER BY detector_id"
            ).fetchall(),
            [("D-90", "finding")],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id, key, session_id FROM cluster_session "
                "ORDER BY detector_id"
            ).fetchall(),
            [("D-90", "finding", "s1"), ("D-90", "finding", "s2")],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id FROM detector_run ORDER BY detector_id"
            ).fetchall(),
            [("D-90",)],
        )


if __name__ == "__main__":
    unittest.main()
