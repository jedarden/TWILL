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
