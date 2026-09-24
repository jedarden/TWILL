"""Contract tests for composite cluster ranking and suppression."""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_ranker  # noqa: E402
import twill_schema  # noqa: E402

NOW = "2026-09-24T00:00:00+00:00"
OLDER = "2026-09-09T00:00:00+00:00"


class RankerTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.state_dir = self.root / "state"
        self.connection = twill_schema.connect(self.state_dir)
        self.addCleanup(self.connection.close)

    def add_cluster(
        self,
        key: str,
        *,
        sessions: int = 2,
        events: int = 2,
        last_seen: str = NOW,
        state: str = "open",
        detector_id: str = "D-01",
    ) -> None:
        self.connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score, covered_by, state) "
            "VALUES (?, ?, 30, ?, ?, ?, ?, 0.0, NULL, ?)",
            (
                detector_id,
                key,
                sessions,
                events,
                "2026-09-01T00:00:00+00:00",
                last_seen,
                state,
            ),
        )
        self.connection.commit()

    def rank(self, patterns=(), **kwargs):
        return twill_ranker.run_rank(
            self.connection,
            patterns,
            as_of=NOW,
            **kwargs,
        )

    def score(self, key: str) -> float:
        return self.connection.execute(
            "SELECT score FROM cluster WHERE key = ?", (key,)
        ).fetchone()[0]

    def add_hit(self, key: str, session_id: str, detector_id: str = "D-01") -> None:
        self.connection.execute(
            "INSERT INTO cluster_session(detector_id, key, session_id) "
            "VALUES (?, ?, ?)",
            (detector_id, key, session_id),
        )
        self.connection.commit()

    def add_usage(
        self,
        session_id: str,
        *,
        input_tokens: int | None = 0,
        output_tokens: int | None = 0,
        cache_read_tokens: int | None = 0,
        cost_usd: float | None = 0.0,
    ) -> None:
        self.connection.execute(
            "INSERT INTO session_usage(session_id, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd) VALUES (?, ?, ?, ?, ?)",
            (session_id, input_tokens, output_tokens, cache_read_tokens, cost_usd),
        )
        self.connection.commit()

    def test_session_usage_is_split_equally_across_all_hit_clusters(self):
        self.add_cluster("missing", sessions=2, events=3)
        self.add_cluster("recurring", sessions=2, events=4, detector_id="D-02")
        self.add_cluster("other", sessions=2, events=2)
        self.add_hit("missing", "s1")
        self.add_hit("recurring", "s1", "D-02")
        self.add_hit("other", "s1")
        self.add_usage(
            "s1",
            input_tokens=300,
            output_tokens=600,
            cache_read_tokens=900,
            cost_usd=1.5,
        )

        report = self.rank()

        for cluster in report.ranking.all_clusters:
            self.assertIsNotNone(cluster.estimated_waste)
            self.assertAlmostEqual(cluster.estimated_waste.input_tokens, 100.0)
            self.assertAlmostEqual(cluster.estimated_waste.output_tokens, 200.0)
            self.assertAlmostEqual(cluster.estimated_waste.cache_read_tokens, 300.0)
            self.assertAlmostEqual(cluster.estimated_waste.tokens, 600.0)
            self.assertAlmostEqual(cluster.estimated_waste.waste_usd, 0.5)
        rendered = report.ranking.all_clusters[0].as_dict()
        self.assertEqual(rendered["estimated_tokens"], 600.0)
        self.assertEqual(rendered["estimated_waste_usd"], 0.5)
        self.assertEqual(
            rendered["waste_attribution_method"],
            "equal_split_across_distinct_cluster_hits",
        )

    def test_missing_usage_components_remain_unavailable_not_zero(self):
        self.add_cluster("known")
        self.add_cluster("unknown")
        self.add_hit("known", "s1")
        self.add_hit("unknown", "s1")
        self.add_usage(
            "s1",
            input_tokens=100,
            output_tokens=None,
            cache_read_tokens=20,
            cost_usd=None,
        )

        estimates = twill_ranker.attribute_waste(self.connection)

        for estimate in estimates.values():
            self.assertEqual(estimate.input_tokens, 50.0)
            self.assertIsNone(estimate.output_tokens)
            self.assertEqual(estimate.cache_read_tokens, 10.0)
            self.assertIsNone(estimate.tokens)
            self.assertIsNone(estimate.waste_usd)

    def test_cost_is_unavailable_when_any_contributing_session_is_unknown(self):
        self.add_cluster("known", sessions=2)
        self.add_cluster("other", sessions=2, detector_id="D-02")
        self.add_hit("known", "s1")
        self.add_hit("known", "s2")
        self.add_hit("other", "s1", "D-02")
        self.add_usage("s1", input_tokens=10, cost_usd=0.0)
        self.add_usage("s2", input_tokens=20, cost_usd=None)

        estimates = twill_ranker.attribute_waste(self.connection)

        self.assertIsNone(estimates[("D-01", "known")].waste_usd)
        self.assertEqual(estimates[("D-02", "other")].waste_usd, 0.0)

    def test_unattributed_cluster_exposes_explicitly_unavailable_estimates(self):
        self.add_cluster("old")

        rendered = self.rank().ranking.all_clusters[0].as_dict()

        self.assertIsNone(rendered["estimated_tokens"])
        self.assertIsNone(rendered["estimated_waste_usd"])

    def test_score_combines_sessions_events_and_recency(self):
        reference = datetime(2026, 9, 24, tzinfo=timezone.utc)
        base = twill_ranker.score_cluster(2, 2, NOW, 30, as_of=NOW)

        self.assertGreater(
            twill_ranker.score_cluster(10, 2, NOW, 30, as_of=NOW), base
        )
        self.assertGreater(
            twill_ranker.score_cluster(2, 20, NOW, 30, as_of=NOW), base
        )
        newer = twill_ranker.score_cluster(
            2,
            2,
            (reference - timedelta(days=1)).isoformat(),
            30,
            as_of=NOW,
        )
        older = twill_ranker.score_cluster(2, 2, OLDER, 30, as_of=NOW)
        self.assertGreater(newer, older)

    def test_rank_persists_scores_and_orders_by_score(self):
        self.add_cluster("small", sessions=2, events=2, last_seen=OLDER)
        self.add_cluster("large", sessions=10, events=20, last_seen=NOW)

        first = self.rank()
        second = self.rank()

        self.assertEqual(
            [row.key for row in first.ranking.clusters],
            ["large", "small"],
        )
        self.assertEqual(
            [row.key for row in second.ranking.clusters],
            ["large", "small"],
        )
        self.assertGreater(self.score("small"), 0.0)
        self.assertEqual(
            first.ranking.clusters[0].score,
            self.score("large"),
        )
        self.assertEqual(
            [row.score for row in first.ranking.all_clusters],
            [row.score for row in second.ranking.all_clusters],
        )

    def test_top_k_is_applied_after_coverage_filtering(self):
        rules = self.root / "rules"
        rules.mkdir()
        rule = rules / "AGENTS.md"
        rule.write_text("covered-key\n")
        self.add_cluster("covered-key", sessions=100, events=100)
        self.add_cluster("uncovered", sessions=2, events=2)

        report = self.rank((f"memory:{rules}/*.md",), top_k=1)

        self.assertEqual([row.key for row in report.ranking.clusters], ["uncovered"])
        self.assertEqual(
            [row.key for row in report.ranking.covered_clusters],
            ["covered-key"],
        )
        self.assertEqual(report.ranking.coverage.covered, 1)

    def test_dismissed_state_remains_permanently_out_of_candidates(self):
        self.add_cluster("dismissed", sessions=100, events=100, state="dismissed")
        self.add_cluster("open", sessions=2, events=2)

        first = self.rank()
        second = self.rank()

        self.assertEqual([row.key for row in first.ranking.clusters], ["open"])
        self.assertEqual([row.key for row in second.ranking.clusters], ["open"])
        self.assertIn(
            "dismissed",
            [row.key for row in second.ranking.suppressed_clusters],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM cluster WHERE key = 'dismissed'"
            ).fetchone()[0],
            "dismissed",
        )

    def test_score_and_coverage_rollback_together(self):
        rules = self.root / "rules"
        rules.mkdir()
        (rules / "AGENTS.md").write_text("covered-key\n")
        self.add_cluster("covered-key")

        with mock.patch.object(
            twill_ranker,
            "_score_updates",
            side_effect=sqlite3.OperationalError("injected score failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.rank((f"memory:{rules}/*.md",))

        self.assertEqual(
            self.connection.execute(
                "SELECT covered_by FROM cluster WHERE key = 'covered-key'"
            ).fetchone()[0],
            None,
        )

    def test_invalid_top_k_is_rejected_by_the_library_boundary(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                twill_ranker.rank_clusters(self.connection, value, as_of=NOW)


if __name__ == "__main__":
    unittest.main()
