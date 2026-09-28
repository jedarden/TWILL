"""Tests for deterministic lesson routing recommendations."""

import sys
import shlex
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_contract import ValidationError  # noqa: E402
from twill_router import (  # noqa: E402
    DIRECT_CHANGE_LAYERS,
    NO_DIRECT_CHANGE_LAYERS,
    ROUTING_LAYER_ORDER,
    bead_create_command,
    has_direct_change,
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
        self.assertEqual(DIRECT_CHANGE_LAYERS, expected[:-1])
        self.assertEqual(NO_DIRECT_CHANGE_LAYERS, ("retrieval_only",))
        self.assertTrue(has_direct_change("environment"))
        self.assertFalse(has_direct_change("retrieval_only"))

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

    def test_bead_create_command_contains_owner_repo_payload_without_executing(self):
        command = bead_create_command(
            title="Repair the recurring command failure",
            body="Install the missing command before retrying.\nKeep the fix in the owner repo.",
            detector="D-01",
        )

        self.assertEqual(
            shlex.split(command),
            [
                "bead",
                "create",
                "--title",
                "Repair the recurring command failure",
                "--description",
                "Install the missing command before retrying.\nKeep the fix in the owner repo.",
                "--label",
                "detector:D-01",
            ],
        )
        self.assertNotIn("subprocess", command)


if __name__ == "__main__":
    unittest.main()
