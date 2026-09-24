"""Tests for deterministic lesson routing recommendations."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_contract import ValidationError  # noqa: E402
from twill_router import (  # noqa: E402
    ROUTING_LAYER_ORDER,
    rank_routing_layers,
    recommend_routing,
)


class RoutingTests(unittest.TestCase):
    def test_layers_are_ranked_strongest_to_weakest(self):
        expected = (
            "environment",
            "hook",
            "wrapper",
            "skill",
            "agents_md",
            "memory",
            "retrieval_only",
        )

        self.assertEqual(ROUTING_LAYER_ORDER, expected)
        self.assertEqual(rank_routing_layers(reversed(expected)), expected)

    def test_missing_binary_recommends_the_environment_fix(self):
        recommendation = recommend_routing(
            detector="D-01@1",
            key="command-not-found:sqlite3",
            sessions=1096,
        )

        self.assertEqual(recommendation.recommended, "environment")
        self.assertIn("across 1096 sessions", recommendation.reason)
        self.assertIn("installing or repairing", recommendation.reason)

    def test_unclassified_recurrence_falls_back_to_retrieval_only(self):
        recommendation = recommend_routing(
            detector="D-02",
            key="tool rejected: invalid input",
            sessions=4,
        )

        self.assertEqual(recommendation.recommended, "retrieval_only")
        self.assertIn("proves no stronger prevention point", recommendation.reason)

    def test_invalid_routing_inputs_fail_closed(self):
        with self.assertRaises(ValidationError):
            rank_routing_layers(("environment", "unknown"))
        with self.assertRaises(ValidationError):
            recommend_routing(
                detector="D-01",
                key="command-not-found:sqlite3",
                sessions=0,
            )


if __name__ == "__main__":
    unittest.main()
