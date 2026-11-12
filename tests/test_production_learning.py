"""Production dispositions, fairness and operational health beyond process exit."""

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import test_learning_cycle as fixture
from twill_learning import LearningError, canonical, main
from twill_learning_health import report


class ProductionLearningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.LearningCycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.controller = self.fixture.controller
        self.id = self.fixture.id

    def complete_counts(self, *, redundant=False, failure=False):
        result = copy.deepcopy(self.fixture.result)
        result["verdict"] = "hold"
        for provider in result["providers"].values():
            provider["counts"] = {"baseline": {"pass": 6 if redundant else 2, "fail": 0 if redundant else 4, "error": 0},
                                  "candidate": {"pass": 0 if failure else 6, "fail": 6 if failure else 0, "error": 0}}
            provider["candidate_passed"] = not failure
            provider["holdout_passed"] = not failure
            provider["verdict"] = "regressed" if failure else "inconclusive"
        self.fixture.adapter.run_experiment.side_effect = lambda *a, **k: copy.deepcopy(result)

    def test_redundancy_is_terminal_disposition_without_forced_application(self):
        self.complete_counts(redundant=True)
        record = self.controller.experiment(self.id)
        self.assertEqual(record["state"], "rejected")
        self.assertIn("already pass", record["reason"])
        self.controller.cycle()
        self.assertEqual(len(self.controller.load(self.id)["experiments"]), 1)
        with self.assertRaises(LearningError):
            self.controller.apply(self.id)

    def test_complete_candidate_failure_rejects_but_runtime_unknown_does_not(self):
        self.complete_counts(failure=True)
        self.assertEqual(self.controller.experiment(self.id)["state"], "rejected")
        self.assertEqual(self.fixture.targets[0].read_text(), "Human instructions.\n")

    def test_drift_with_provider_failure_removes_exposure_and_reports_attention(self):
        self.fixture.active()
        self.fixture.targets[0].write_text(self.fixture.targets[0].read_text() + "New constraint.\n")
        self.fixture.result["providers"]["codex"]["errors"] = 1
        cycle = self.controller.cycle()
        self.assertEqual(self.controller.load(self.id)["state"], "unvalidated")
        self.assertNotIn(self.fixture.record["instruction"], self.fixture.targets[0].read_text())
        self.assertEqual(cycle["health"]["status"], "needs_attention")

    def test_blocked_cycle_and_doctor_return_nonzero(self):
        self.fixture.policy["bindings"] = []
        policy_path = self.fixture.root / "policy.json"
        policy_path.write_bytes(canonical(self.fixture.policy))
        with mock.patch("twill_learning.Controller.publish", return_value={"published": False}):
            with mock.patch("sys.stdout"):
                self.assertEqual(main(["--policy", str(policy_path), "cycle"]), 4)
                self.assertEqual(main(["--policy", str(policy_path), "doctor"]), 4)

    def test_unavailable_execution_runtime_is_not_counted_as_an_experiment(self):
        self.fixture.adapter.run_experiment.side_effect = RuntimeError("trusted sandbox unavailable")
        result = self.controller.cycle()
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["experimented"], 0)
        self.assertEqual(result["health"]["status"], "needs_attention")
        self.assertEqual(self.controller.load(self.id)["state"], "unvalidated")
        self.assertFalse(self.controller.load(self.id)["experiments"])

    def test_incomplete_provider_metadata_is_attention_not_healthy_no_improvement(self):
        self.fixture.result["providers"].pop("codex")
        result = self.controller.cycle()
        self.assertEqual(result["health"]["status"], "needs_attention")
        self.assertTrue(self.controller.load(self.id)["assessment"]["unknown"])

    def test_execution_grader_drift_prevents_instruction_delivery(self):
        import twill_execution_oracle
        from twill_learning import digest
        suite = json.loads(self.fixture.suite.read_text())
        suite["cases"][0].pop("expect")
        suite["cases"][0]["oracle"] = {"type": "python-functions/v1", "image": twill_execution_oracle.IMAGE,
                                        "output_files": ["solution.py"], "target_file": "solution.py",
                                        "function": "transform", "assertions": [{"id": "behavior", "args": [1], "equals": 1}]}
        self.fixture.suite.write_text(json.dumps(suite))
        self.fixture.policy["bindings"][0]["suite_sha256"] = digest(self.fixture.suite.read_bytes())
        record = self.controller.experiment(self.id)
        self.assertEqual(record["state"], "validated")
        self.assertIn("execution_sha", record["experiments"][-1])
        record["experiments"][-1]["execution_sha"] = "changed-grader"
        self.controller.save(record, "fixture-grader-change")
        with self.assertRaisesRegex(LearningError, "grader changed"):
            self.controller.apply(self.id)


class HealthTests(unittest.TestCase):
    def test_all_blocked_is_not_healthy_even_when_process_succeeded(self):
        now = datetime.now(timezone.utc)
        record = {"id": "LC-example", "state": "needs_evaluation", "reason": "missing suite",
                  "created_at": now.isoformat(), "experiments": []}
        health = report([record], {"at": now.isoformat()}, now=now)
        self.assertEqual(health["status"], "needs_attention")
        self.assertIn("all_candidates_blocked", {r["kind"] for r in health["findings"]})

    def test_stale_timer_inputs_and_pending_age_are_independent(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=2)).isoformat()
        health = report([{"id": "LC-old", "state": "draft", "created_at": old}],
                        {"at": old}, now=now, pipeline={"stages": {}})
        kinds = {row["kind"] for row in health["findings"]}
        self.assertTrue({"stale_cycle", "stale_pending", "stale_input_stage"} <= kinds)

    def test_evidence_rejection_counts_as_progress_without_adoption(self):
        now = datetime.now(timezone.utc)
        health = report([{"id": "LC-rejected", "state": "rejected", "experiments": []}],
                        {"at": now.isoformat()}, now=now)
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["terminal"], 1)
        self.assertEqual(health["active"], 0)


if __name__ == "__main__":
    unittest.main()
