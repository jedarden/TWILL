"""Contract tests for the read-only rule earnings and decay report."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_rules  # noqa: E402
import twill_schema  # noqa: E402


NOW = "2026-09-28T00:00:00+00:00"


class RulesReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.connection = twill_schema.connect(Path(self.temporary.name) / "state")
        self.addCleanup(self.connection.close)

    def add_rule(
        self,
        path: str,
        sha: str,
        indexed_at: str,
        *,
        last_read: str | None = None,
        stale: int = 0,
    ) -> None:
        self.connection.execute(
            "INSERT INTO rule_doc(path, layer, sha, indexed_at, last_read_by_agent, stale) "
            "VALUES (?, 'memory', ?, ?, ?, ?)",
            (path, sha, indexed_at, last_read, stale),
        )

    def add_cluster(self, path: str, *, detector: str = "D-01", key: str = "rule-key"):
        self.connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score, covered_by, state) "
            "VALUES (?, ?, 30, 3, 5, ?, ?, 0.0, ?, 'open')",
            (
                detector,
                key,
                "2026-09-01T00:00:00+00:00",
                "2026-09-27T00:00:00+00:00",
                path,
            ),
        )

    def add_week(self, week: str, sessions: int, events: int):
        self.connection.execute(
            "INSERT INTO cluster_week(detector_id, key, week, sessions, events) "
            "VALUES ('D-01', 'rule-key', ?, ?, ?)",
            (week, sessions, events),
        )

    def test_report_inverts_coverage_and_classifies_weekly_earnings(self):
        self.add_rule(
            "/rules/useful.md",
            "sha-useful",
            "2026-08-01T00:00:00+00:00",
            last_read="2026-09-27T00:00:00+00:00",
        )
        self.add_cluster("/rules/useful.md")
        self.add_week("2026-W37", 2, 3)
        self.add_week("2026-W38", 3, 5)
        self.connection.commit()

        report = twill_rules.build_rules_report(
            self.connection,
            unread_days=90,
            now=NOW,
        )

        self.assertEqual(len(report.rules), 1)
        rule = report.rules[0]
        self.assertTrue(rule.covered)
        self.assertFalse(rule.unread)
        cluster = rule.clusters[0]
        self.assertEqual(cluster.recurrence.direction, twill_rules.RECURRENCE_UP)
        self.assertEqual(cluster.recurrence.events_delta, 2)
        self.assertEqual(cluster.recurrence.sessions_delta, 1)
        self.assertEqual(cluster.recurrence.previous_week, "2026-W37")
        self.assertEqual(cluster.recurrence.current_week, "2026-W38")

    def test_reads_are_derived_from_file_read_events_and_shared_hashes(self):
        self.add_rule(
            "/rules/one.md",
            "sha-shared",
            "2026-08-01T00:00:00+00:00",
        )
        self.add_rule(
            "/rules/two.md",
            "sha-shared",
            "2026-08-02T00:00:00+00:00",
            last_read="2026-08-03T00:00:00+00:00",
        )
        self.connection.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, path) "
            "VALUES ('session-a', ?, ?, 'file_read', ?)",
            (
                "2026-09-27T12:00:00+00:00",
                "2026-09-27T12:00:00+00:00",
                "/rules/one.md",
            ),
        )
        self.connection.commit()

        report = twill_rules.build_rules_report(self.connection, now=NOW)

        self.assertEqual(
            [rule.last_read for rule in report.rules],
            ["2026-09-27T12:00:00+00:00", "2026-09-27T12:00:00+00:00"],
        )
        self.assertFalse(any(rule.unread for rule in report.rules))

    def test_candidates_need_old_enough_decay_and_no_covered_cluster(self):
        self.add_rule(
            "/rules/candidate.md",
            "sha-candidate",
            "2026-06-01T00:00:00+00:00",
        )
        self.add_rule(
            "/rules/young.md",
            "sha-young",
            "2026-09-27T00:00:00+00:00",
        )
        self.add_rule(
            "/rules/covered.md",
            "sha-covered",
            "2026-06-01T00:00:00+00:00",
        )
        self.add_cluster("/rules/covered.md", key="covered-key")
        self.add_rule(
            "/rules/stale.md",
            "sha-stale",
            "2026-06-01T00:00:00+00:00",
            stale=1,
        )
        self.connection.commit()

        report = twill_rules.build_rules_report(
            self.connection,
            unread_days=90,
            now=NOW,
        )

        self.assertEqual(report.deletion_candidates[0].path, "/rules/candidate.md")
        self.assertEqual(report.stale_rules[0]["path"], "/rules/stale.md")
        self.assertNotIn("/rules/stale.md", report.deletion_candidates)
        self.assertFalse(
            next(rule for rule in report.rules if rule.path == "/rules/young.md").deletion_candidate
        )
        self.assertFalse(
            next(rule for rule in report.rules if rule.path == "/rules/covered.md").deletion_candidate
        )

    def test_report_serializes_as_one_machine_object(self):
        self.add_rule(
            "/rules/flat.md",
            "sha-flat",
            "2026-06-01T00:00:00+00:00",
        )
        self.connection.commit()

        payload = twill_rules.build_rules_report(
            self.connection,
            now=NOW,
        ).as_dict()

        self.assertEqual(json.loads(json.dumps(payload)), payload)
        self.assertEqual(payload["deletion_candidate_paths"], ["/rules/flat.md"])


class RulesCommandTests(unittest.TestCase):
    def test_cli_exposes_the_report_without_an_artifacts_config(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            connection = twill_schema.connect(state)
            connection.execute(
                "INSERT INTO rule_doc(path, layer, sha, indexed_at, stale) "
                "VALUES ('/rules/old.md', 'memory', 'sha-old', ?, 0)",
                ("2026-01-01T00:00:00+00:00",),
            )
            connection.commit()
            connection.close()
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "twill"),
                    "rules",
                    "--json",
                    "--deletion-candidates",
                    "--state-dir",
                    str(state),
                ],
                cwd=ROOT,
                env={**os.environ, "TWILL_STATE_DIR": str(state)},
                check=False,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["data"]["deletion_candidate_paths"], ["/rules/old.md"])
        self.assertEqual(payload["warnings"], [])

class RetirementTests(unittest.TestCase):
    """Contract tests for the read-only Phase 6 retirement proposal."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.connection = twill_schema.connect(Path(temporary.name) / "state")
        self.addCleanup(self.connection.close)

    def add_rule(
        self,
        path: str,
        *,
        indexed_at: str = "2026-06-01T00:00:00+00:00",
        last_read: str | None = None,
        stale: int = 0,
    ) -> None:
        self.connection.execute(
            "INSERT INTO rule_doc(path, layer, sha, indexed_at, last_read_by_agent, stale) "
            "VALUES (?, 'memory', ?, ?, ?, ?)",
            (path, f"sha-{path}", indexed_at, last_read, stale),
        )

    def add_cluster(self, path: str, *, last_seen: str) -> None:
        self.connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score, covered_by, state) "
            "VALUES ('D-01', ?, 30, 3, 5, ?, ?, 0.0, ?, 'open')",
            (
                f"rule-{path}",
                "2026-06-01T00:00:00+00:00",
                last_seen,
                path,
            ),
        )

    def report(self, *, now: str = NOW):
        self.connection.commit()
        return twill_rules.build_rules_report(self.connection, now=now)

    def test_old_unread_rule_with_zero_occurrences_is_proposed(self):
        self.add_rule("/rules/dormant.md")

        report = self.report()

        self.assertEqual(len(report.retirement_proposals), 1)
        proposal = report.retirement_proposals[0]
        self.assertEqual(proposal.path, "/rules/dormant.md")
        self.assertIsNone(proposal.last_occurrence)
        self.assertEqual(proposal.covered_clusters, 0)
        self.assertEqual(
            proposal.removal_owner,
            "human edit in the owning layer",
        )

    def test_old_last_occurrence_is_proposed_but_recent_one_is_not(self):
        self.add_rule("/rules/old.md")
        self.add_cluster(
            "/rules/old.md",
            last_seen="2026-06-15T00:00:00+00:00",
        )
        report = self.report()
        self.assertEqual(
            report.retirement_proposals[0].last_occurrence,
            "2026-06-15T00:00:00+00:00",
        )

        self.add_rule("/rules/recent.md")
        self.add_cluster(
            "/rules/recent.md",
            last_seen="2026-09-27T00:00:00+00:00",
        )
        self.assertEqual(
            [item.path for item in self.report().retirement_proposals],
            ["/rules/old.md"],
        )

    def test_recent_read_and_young_rules_are_not_proposed(self):
        self.add_rule(
            "/rules/read.md",
            last_read="2026-09-27T00:00:00+00:00",
        )
        self.add_rule(
            "/rules/young.md",
            indexed_at="2026-09-27T00:00:00+00:00",
        )

        self.assertEqual(self.report().retirement_proposals, ())

    def test_malformed_occurrence_evidence_does_not_prove_zero(self):
        self.add_rule("/rules/mystery.md")
        self.add_cluster("/rules/mystery.md", last_seen="not-a-timestamp")

        self.assertEqual(self.report().retirement_proposals, ())

    def test_retirement_is_serialized_and_does_not_mutate_state(self):
        self.add_rule("/rules/dormant.md")
        self.connection.commit()
        before = self.connection.execute(
            "SELECT path, stale FROM rule_doc"
        ).fetchall()

        payload = self.report().as_dict()

        self.assertEqual(payload["retirement_proposal_paths"], ["/rules/dormant.md"])
        self.assertEqual(payload["summary"]["retirement_proposals"], 1)
        self.assertEqual(
            payload["retirement_proposals"][0]["removal_owner"],
            "human edit in the owning layer",
        )
        self.assertEqual(
            self.connection.execute("SELECT path, stale FROM rule_doc").fetchall(),
            before,
        )

    def test_zero_occurrence_threshold_can_be_evaluated_explicitly(self):
        self.add_rule("/rules/fading.md")
        self.add_cluster(
            "/rules/fading.md",
            last_seen="2026-08-19T00:00:00+00:00",
        )

        report = self.report()
        self.assertEqual(report.retirement_proposals, ())
        proposals = twill_rules.evaluate_retirements(report, zero_days=30)
        self.assertEqual([item.path for item in proposals], ["/rules/fading.md"])


if __name__ == "__main__":
    unittest.main()
