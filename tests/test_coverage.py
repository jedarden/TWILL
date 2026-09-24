"""Contract tests for cluster coverage and new-lesson suppression."""

import argparse
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_ranker  # noqa: E402
import twill_schema  # noqa: E402
from twill_config import TwillConfig  # noqa: E402


class CoverageTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.state_dir = self.root / "state"
        self.rules_dir = self.root / "rules"
        self.rules_dir.mkdir()
        self.rule_path = self.rules_dir / "AGENTS.md"
        self.rule_path.write_text("command-not-found:sqlite3\n")
        self.pattern = f"memory:{self.rules_dir}/*.md"
        self.connection = twill_schema.connect(self.state_dir)
        self.addCleanup(self.connection.close)

    def add_cluster(
        self,
        key: str,
        *,
        covered_by: str | None = None,
        state: str = "open",
        sessions: int = 2,
        detector_id: str = "D-01",
    ) -> None:
        self.connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score, covered_by, state) "
            "VALUES (?, ?, 30, ?, ?, 'first', 'last', 0.0, ?, ?)",
            (detector_id, key, sessions, sessions, covered_by, state),
        )
        self.connection.commit()

    def cluster(self, key: str) -> tuple:
        return self.connection.execute(
            "SELECT covered_by, state FROM cluster WHERE key = ?", (key,)
        ).fetchone()

    def test_matching_rule_marks_covered_and_uncovered_stays_open(self):
        self.add_cluster("command-not-found:sqlite3")
        self.add_cluster("command-not-found:other")

        report = twill_ranker.run_rank(self.connection, [self.pattern])

        self.assertEqual(report.ranking.coverage.covered, 1)
        self.assertEqual(report.ranking.coverage.uncovered, 1)
        self.assertEqual(
            [row.key for row in report.ranking.clusters],
            ["command-not-found:other"],
        )
        self.assertEqual(
            self.cluster("command-not-found:sqlite3"),
            (str(self.rule_path.resolve()), "open"),
        )
        self.assertEqual(
            self.cluster("command-not-found:other"),
            (None, "open"),
        )

    def test_one_unrelated_rule_token_does_not_cover_a_cluster(self):
        self.rule_path.write_text("Install sqlite3 when the command is unavailable.\n")
        self.add_cluster("command-not-found:sqlite3")

        report = twill_ranker.run_rank(self.connection, [self.pattern])

        self.assertEqual(report.ranking.coverage.covered, 0)
        self.assertEqual(len(report.ranking.clusters), 1)

    def test_punctuation_in_a_key_cannot_become_fts_syntax(self):
        self.rule_path.write_text("force-push sqlite3 unmatched are covered here.\n")
        self.add_cluster('force-push:sqlite3 "unmatched"')

        report = twill_ranker.run_rank(self.connection, [self.pattern])

        self.assertEqual(report.ranking.coverage.covered, 1)

    def test_covered_open_cluster_is_separate_escalation_output(self):
        self.add_cluster("command-not-found:sqlite3")

        report = twill_ranker.run_rank(self.connection, [self.pattern])

        self.assertEqual(report.ranking.clusters, ())
        self.assertEqual(len(report.ranking.escalations), 1)
        self.assertEqual(report.ranking.escalations[0].state, "open")
        self.assertEqual(self.cluster("command-not-found:sqlite3")[1], "open")

    def test_stale_only_coverage_is_visible_as_degraded(self):
        self.add_cluster("command-not-found:sqlite3")
        twill_ranker.run_rank(self.connection, [self.pattern])
        self.rule_path.unlink()

        report = twill_ranker.run_rank(self.connection, [])

        self.assertEqual(len(report.ranking.degraded_clusters), 1)
        self.assertEqual(report.ranking.escalations, ())
        self.assertTrue(report.ranking.covered_clusters[0].rule_stale)

    def test_move_wins_over_a_lexically_earlier_live_copy(self):
        self.add_cluster("command-not-found:sqlite3")
        twill_ranker.run_rank(self.connection, [self.pattern])
        moved = self.rules_dir / "z-moved.md"
        self.rule_path.rename(moved)
        competing = self.rules_dir / "a-competing.md"
        competing.write_text("command-not-found:sqlite3 is documented here.\n")

        report = twill_ranker.run_rank(self.connection, [self.pattern])

        self.assertEqual(
            self.cluster("command-not-found:sqlite3"),
            (str(moved.resolve()), "open"),
        )
        self.assertIn(str(competing.resolve()), report.index.indexed)

    def test_move_wins_when_the_old_path_is_reused(self):
        self.add_cluster("command-not-found:sqlite3")
        twill_ranker.run_rank(self.connection, [self.pattern])
        moved = self.rules_dir / "z-moved.md"
        self.rule_path.rename(moved)
        self.rule_path.write_text("unrelated maintenance note.\n")
        competing = self.rules_dir / "a-competing.md"
        competing.write_text("command-not-found:sqlite3 is documented here.\n")

        twill_ranker.run_rank(self.connection, [self.pattern])

        self.assertEqual(
            self.cluster("command-not-found:sqlite3"),
            (str(moved.resolve()), "open"),
        )

    def test_no_match_clears_an_obsolete_marker(self):
        self.add_cluster("command-not-found:other", covered_by="/old/AGENTS.md")

        report = twill_ranker.run_rank(self.connection, [])

        self.assertEqual(report.ranking.coverage.changed, 1)
        self.assertEqual(self.cluster("command-not-found:other"), (None, "open"))

    def test_moved_rule_prefers_the_live_path(self):
        self.add_cluster("command-not-found:sqlite3")
        twill_ranker.run_rank(self.connection, [self.pattern])
        moved = self.rules_dir / "MOVED.md"
        self.rule_path.rename(moved)

        report = twill_ranker.run_rank(self.connection, [self.pattern])

        self.assertEqual(report.index.moves, ((str(self.rule_path.resolve()), (str(moved.resolve()),)),))
        self.assertEqual(self.cluster("command-not-found:sqlite3"), (str(moved.resolve()), "open"))

    def test_review_state_is_preserved_and_not_a_new_candidate(self):
        self.add_cluster("command-not-found:sqlite3", state="drafted")

        report = twill_ranker.run_rank(self.connection, [self.pattern])

        self.assertEqual(report.ranking.clusters, ())
        self.assertEqual(self.cluster("command-not-found:sqlite3")[1], "drafted")

    def test_zero_session_rule_document_finding_is_not_a_new_candidate(self):
        self.add_cluster(
            "unread-rule-doc:/rules/old.md",
            detector_id="D-09",
            sessions=0,
        )

        report = twill_ranker.run_rank(self.connection, [self.pattern])

        self.assertEqual(report.ranking.clusters, ())
        self.assertEqual(
            self.connection.execute(
                "SELECT state, covered_by FROM cluster WHERE detector_id = 'D-09'"
            ).fetchone(),
            ("open", None),
        )

    def test_refresh_is_idempotent(self):
        self.add_cluster("command-not-found:sqlite3")

        first = twill_ranker.run_rank(self.connection, [self.pattern])
        second = twill_ranker.run_rank(self.connection, [self.pattern])

        self.assertEqual(first.ranking.coverage.changed, 1)
        self.assertEqual(second.ranking.coverage.changed, 0)

    def test_index_and_coverage_roll_back_together(self):
        self.add_cluster("command-not-found:sqlite3")

        with mock.patch.object(
            twill_ranker,
            "rank_clusters",
            side_effect=sqlite3.OperationalError("injected coverage failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                twill_ranker.run_rank(self.connection, [self.pattern])

        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM rule_doc").fetchone()[0],
            0,
        )
        self.assertEqual(self.cluster("command-not-found:sqlite3"), (None, "open"))

    def test_rank_command_returns_json_and_updates_coverage(self):
        self.add_cluster("command-not-found:sqlite3")
        self.add_cluster("command-not-found:other")
        self.connection.close()
        config = TwillConfig(
            artifacts_root=self.root / "artifacts",
            rule_globs=(self.pattern,),
            top_k=1,
        )
        args = argparse.Namespace(
            top=None,
            state_dir=str(self.state_dir),
            json=True,
        )
        output = io.StringIO()
        with mock.patch.object(twill_app, "load_config", return_value=config):
            with redirect_stdout(output):
                code = twill_app.rank_command(args)

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())["data"]
        self.assertEqual(payload["top_k"], 1)
        self.assertEqual([row["key"] for row in payload["clusters"]], ["command-not-found:other"])
        self.assertEqual(
            payload["covered_clusters"][0]["covered_by"],
            str(self.rule_path.resolve()),
        )
        self.assertEqual(len(payload["escalations"]), 1)
        self.assertEqual(
            payload["coverage"], {"total": 2, "covered": 1, "uncovered": 1}
        )
        for lane in ("clusters", "covered_clusters", "escalations"):
            for row in payload[lane]:
                self.assertIn("estimated_tokens", row)
                self.assertIn("estimated_waste_usd", row)

    def test_rank_command_labels_every_human_waste_figure_as_estimated(self):
        self.add_cluster("command-not-found:sqlite3")
        self.connection.execute(
            "INSERT INTO cluster_session(detector_id, key, session_id) "
            "VALUES ('D-01', 'command-not-found:sqlite3', 's1')"
        )
        self.connection.execute(
            "INSERT INTO session_usage(session_id, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd) VALUES ('s1', 10, 20, 30, 0.125)"
        )
        self.connection.commit()
        self.connection.close()
        config = TwillConfig(
            artifacts_root=self.root / "artifacts",
            rule_globs=(),
            top_k=1,
        )
        args = argparse.Namespace(
            top=None,
            state_dir=str(self.state_dir),
            json=False,
        )
        output = io.StringIO()
        with mock.patch.object(twill_app, "load_config", return_value=config):
            with redirect_stdout(output):
                code = twill_app.rank_command(args)

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("estimated tokens: 60.00", rendered)
        self.assertIn("estimated waste: 0.125000 USD", rendered)


if __name__ == "__main__":
    unittest.main()
