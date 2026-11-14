"""Adverse delivery/recovery cases for the opt-in learning controller."""

import copy
import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from twill_learning import Controller, LearningError, POLICY_SCHEMA, digest, main


class LearningCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.targets = [self.root / "repo" / name for name in ("AGENTS.md", "CLAUDE.md")]
        self.targets[0].parent.mkdir()
        for path in self.targets:
            path.write_text("Human instructions.\n")
        self.source = self.root / "evidence.json"
        self.source.write_text(json.dumps({"request": "Create two files", "correction": "Finish both"}))
        self.suite = self.root / "suite.json"
        self.suite.write_text(json.dumps({"cases": [
            {"id": "train", "split": "train", "prompt": "Build both files", "expect": {"contains": ["finish"]}},
            {"id": "holdout", "split": "holdout", "prompt": "Finish two tasks", "expect": {"contains": ["finish"]}},
            {"id": "stop", "split": "holdout", "guardrail": True, "prompt": "Stop", "expect": {"contains": ["stop"]}},
        ]}))
        self.policy = {
            "schema": POLICY_SCHEMA, "enabled": True,
            "artifacts_root": str(self.root / "private"), "work_root": str(self.root / "work"),
            "providers": {name: {"model": "fixed-model"} for name in ("claude", "codex")},
            "scopes": {"repo": {"targets": [str(p) for p in self.targets], "auto_apply": True}},
            "bindings": [{"scope": "repo", "detector": "D-06", "suite": str(self.suite),
                          "suite_sha256": digest(self.suite.read_bytes())}],
        }
        self.controller = Controller(self.policy)
        self.record = self.controller.create({"instruction": "Finish every authorized task before stopping.",
                                             "scope": "repo", "source": {"path": str(self.source), "detector": "D-06"}})
        self.id = self.record["id"]
        self.result = {"candidate_sha": self.record["instruction_sha"], "verdict": "promote",
                       "providers": {name: {"candidate_passed": True, "holdout_passed": True, "errors": 0,
                                            "model_fingerprint": "fixed"}
                                     for name in ("claude", "codex")}}
        self.adapter = types.ModuleType("twill_experiments")
        self.adapter.run_experiment = mock.Mock(side_effect=lambda *a, **k: copy.deepcopy(self.result))
        self.adapter.provider_fingerprints = lambda providers: {name: {"fingerprint": "fixed"} for name in providers}
        self.modules = mock.patch.dict("sys.modules", {"twill_experiments": self.adapter})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def active(self):
        self.controller.experiment(self.id)
        return self.controller.apply(self.id)

    def rejected(self):
        result = copy.deepcopy(self.result)
        result["verdict"] = "hold"
        for provider in result["providers"].values():
            provider.update(candidate_passed=False, holdout_passed=False, verdict="regressed",
                            counts={"baseline": {"pass": 0, "fail": 6, "error": 0},
                                    "candidate": {"pass": 0, "fail": 6, "error": 0}})
        self.adapter.run_experiment.side_effect = lambda *a, **k: copy.deepcopy(result)
        record = self.controller.experiment(self.id)
        self.assertEqual(record["state"], "rejected")
        self.adapter.run_experiment.side_effect = lambda *a, **k: copy.deepcopy(self.result)
        return record

    def revised_suite(self):
        suite = json.loads(self.suite.read_text())
        suite["cases"].append({"id": "fresh-holdout", "split": "holdout",
                               "prompt": "Complete the remaining authorized review",
                               "expect": {"contains": ["finish"]}})
        self.suite = self.root / "revised-suite.json"
        self.suite.write_text(json.dumps(suite))
        self.policy["bindings"] = [{"scope": "repo", "lesson_id": self.id,
                                    "suite": str(self.suite), "suite_sha256": digest(self.suite.read_bytes())}]
        return suite

    def test_reconsider_creates_fresh_draft_and_preserves_terminal_history(self):
        parent = self.rejected()
        protected = [self.controller.path(self.id), self.suite,
                     *list((self.controller.store / "history" / self.id).glob("*.json")),
                     self.controller.store / parent["experiments"][-1]["path"]]
        before = {path: path.read_bytes() for path in protected}
        revised = self.revised_suite()
        self.adapter.run_experiment.reset_mock()
        child = self.controller.reconsider(self.id, "  Independently revised holdout coverage.  ")
        expected = "LC-" + digest({"reevaluation_of": self.id, "source_sha": parent["source"]["sha256"],
                                   "instruction_sha": parent["instruction_sha"], "suite_sha": digest(revised)})[:16]
        self.assertEqual(child["id"], expected)
        self.assertEqual(child["state"], "draft")
        self.assertEqual(child["evaluation_parent"], self.id)
        self.assertEqual(child["evaluation_suite_sha"], digest(revised))
        self.assertEqual(child["reconsideration_reason"], "Independently revised holdout coverage.")
        for field in ("instruction", "instruction_sha", "source", "scope", "kind", "evaluator", "source_evidence"):
            self.assertEqual(child[field], parent[field])
        self.assertEqual(child["experiments"], [])
        self.assertIsNone(child["application"])
        self.assertNotIn("assessment", child)
        self.assertNotIn("validated_policy_sha", child)
        self.adapter.run_experiment.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        self.assertEqual(self.controller.suite(child), revised)
        with self.assertRaises(LearningError):
            self.controller.apply(child["id"])

        # Neither an old receipt nor one provider's new result can admit the child.
        incomplete = copy.deepcopy(self.result)
        incomplete["providers"].pop("claude")
        self.adapter.run_experiment.side_effect = lambda *a, **k: incomplete
        self.assertEqual(self.controller.experiment(child["id"])["state"], "unvalidated")
        with self.assertRaises(LearningError):
            self.controller.apply(child["id"])
        self.adapter.run_experiment.side_effect = lambda *a, **k: copy.deepcopy(self.result)
        evaluated = self.controller.experiment(child["id"])
        self.assertEqual(evaluated["state"], "validated")
        self.assertEqual(len(evaluated["experiments"]), 2)
        self.assertEqual(evaluated["experiments"][-1]["suite_sha"], digest(revised))
        self.assertEqual(set(self.adapter.run_experiment.call_args.kwargs["providers"]), {"claude", "codex"})
        self.assertEqual(self.controller.apply(child["id"])["state"], "active")
        self.assertEqual({path: path.read_bytes() for path in protected}, before)

    def test_reconsider_refuses_unchanged_or_unapproved_or_ambiguous_oracles(self):
        self.rejected()
        with self.assertRaisesRegex(LearningError, "revised independent suite"):
            self.controller.reconsider(self.id, "Review the oracle")
        # A new serialized file hash alone does not make a revised oracle.
        unchanged = json.loads(self.suite.read_text())
        self.suite = self.root / "reformatted-suite.json"
        self.suite.write_text(json.dumps(unchanged, indent=2))
        self.policy["bindings"][0]["suite"] = str(self.suite)
        self.policy["bindings"][0]["suite_sha256"] = digest(self.suite.read_bytes())
        with self.assertRaisesRegex(LearningError, "revised independent suite"):
            self.controller.reconsider(self.id, "Formatting changed")
        self.revised_suite()
        self.policy["bindings"][0]["suite_sha256"] = "0" * 64
        with self.assertRaisesRegex(LearningError, "update policy explicitly"):
            self.controller.reconsider(self.id, "Unapproved suite drift")
        self.policy["bindings"][0]["suite_sha256"] = digest(self.suite.read_bytes())
        self.policy["bindings"].append(copy.deepcopy(self.policy["bindings"][0]))
        with self.assertRaisesRegex(LearningError, "exactly one"):
            self.controller.reconsider(self.id, "Ambiguous binding")
        self.assertEqual(len(self.controller.records()), 1)

    def test_reconsider_refuses_nonterminal_applied_untested_and_changed_sources(self):
        parent = self.rejected()
        self.revised_suite()
        for state in ("draft", "validated", "active", "invalidated", "superseded", "conflict"):
            self.controller.save({**parent, "state": state}, "synthetic-state")
            with self.assertRaisesRegex(LearningError, "rejected, unapplied"):
                self.controller.reconsider(self.id, "Revised independent suite")
        self.controller.save({**parent, "application": {"files": []}}, "synthetic-exposure")
        with self.assertRaisesRegex(LearningError, "rejected, unapplied"):
            self.controller.reconsider(self.id, "Revised independent suite")
        self.controller.save({**parent, "experiments": []}, "synthetic-no-evidence")
        with self.assertRaisesRegex(LearningError, "prior experiment suite evidence"):
            self.controller.reconsider(self.id, "Revised independent suite")
        self.controller.save(parent, "restore-synthetic-parent")
        self.source.write_text("Changed source")
        with self.assertRaisesRegex(LearningError, "no longer current"):
            self.controller.reconsider(self.id, "Revised independent suite")
        self.assertEqual(len(self.controller.records()), 1)

    def test_reconsider_is_idempotent_without_resetting_terminal_child(self):
        self.rejected()
        self.revised_suite()
        child = self.controller.reconsider(self.id, "Revised independent suite")
        failed = copy.deepcopy(self.result)
        failed["verdict"] = "hold"
        for provider in failed["providers"].values():
            provider.update(candidate_passed=False, holdout_passed=False,
                            counts={"baseline": {"pass": 0, "fail": 8, "error": 0},
                                    "candidate": {"pass": 0, "fail": 8, "error": 0}})
        self.adapter.run_experiment.side_effect = lambda *a, **k: failed
        terminal = self.controller.experiment(child["id"])
        self.assertEqual(terminal["state"], "rejected")
        before = self.controller.path(child["id"]).read_bytes()
        again = self.controller.reconsider(self.id, "A repeated operator request")
        self.assertEqual((again["id"], again["state"], again["revision"]),
                         (child["id"], "rejected", terminal["revision"]))
        self.assertEqual(self.controller.path(child["id"]).read_bytes(), before)

    def test_reconsider_child_stays_bound_to_its_exact_suite_and_parent(self):
        self.rejected()
        self.revised_suite()
        child = self.controller.reconsider(self.id, "Revised independent suite")
        forged = copy.deepcopy(child)
        forged["source"]["detector"] = "D-99"
        with self.assertRaisesRegex(LearningError, "exact parent"):
            self.controller.suite(forged)
        suite = json.loads(self.suite.read_text())
        suite["cases"][-1]["prompt"] = "A later distinct holdout"
        self.suite.write_text(json.dumps(suite))
        self.policy["bindings"][0]["suite_sha256"] = digest(self.suite.read_bytes())
        with self.assertRaisesRegex(LearningError, "reconsideration suite changed"):
            self.controller.experiment(child["id"])
        self.assertEqual(self.controller.load(child["id"])["experiments"], [])

    def test_reconsider_cli_requires_precise_nonblank_safe_reason(self):
        self.rejected()
        self.revised_suite()
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps(self.policy))
        args = ["--policy", str(policy_path), "reconsider", self.id]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(args)
        self.assertEqual(raised.exception.code, 2)
        for reason, message in [(" ", "1..1000"), ("x" * 1001, "1..1000"),
                                ("credential sk-" + "a" * 30, "credential-shaped")]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(main(args + ["--reason", reason]), 4)
            self.assertIn(message, output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(Controller, "publish", return_value={"published": False}):
            self.assertEqual(main(args + ["--reason", "Reviewed independent oracle correction"]), 0)
        self.assertEqual(json.loads(output.getvalue())["data"]["state"], "draft")

    def test_full_cycle_requires_both_agents_and_removes_only_owned_text(self):
        self.assertEqual(self.active()["state"], "active")
        for path in self.targets:
            self.assertIn(self.record["instruction"], path.read_text())
            path.write_text(path.read_text() + "Later human instruction.\n")
        result = self.controller.experiment(self.id, validation=True)
        self.assertEqual(result["reason"], "revalidated for both agents")
        result = self.controller.invalidate(self.id, "Counterexample discovered")
        self.assertEqual(result["state"], "invalidated")
        for path in self.targets:
            self.assertNotIn(self.record["instruction"], path.read_text())
            self.assertIn("Human instructions.", path.read_text())
            self.assertIn("Later human instruction.", path.read_text())
        self.assertTrue(list((self.controller.store / "history" / self.id).glob("*.json")))
        self.assertEqual(self.controller.invalidate(self.id, "Repeated")["state"], "invalidated")
        with self.assertRaises(LearningError):
            self.controller.apply(self.id)

    def test_single_provider_and_errors_never_promote(self):
        for bad in ("missing", "error", "failure", "candidate_hash"):
            with self.subTest(bad=bad):
                result = copy.deepcopy(self.result)
                if bad == "missing":
                    result["providers"].pop("claude")
                elif bad == "error":
                    result["providers"]["claude"]["errors"] = 1
                elif bad == "failure":
                    result["providers"]["claude"]["holdout_passed"] = False
                else:
                    result["candidate_sha"] = "wrong"
                self.adapter.run_experiment.side_effect = lambda *a, **k: result
                self.assertEqual(self.controller.experiment(self.id)["state"], "unvalidated")
                with self.assertRaises(LearningError):
                    self.controller.apply(self.id)
        for path in self.targets:
            self.assertEqual(path.read_text(), "Human instructions.\n")

    def test_source_drift_prevents_apply_and_removes_active_exposure(self):
        self.controller.experiment(self.id)
        self.source.write_text("changed evidence")
        with self.assertRaises(LearningError):
            self.controller.apply(self.id)
        self.source.write_text(json.dumps({"request": "Create two files", "correction": "Finish both"}))
        self.active()
        self.source.write_text("changed again")
        self.controller.cycle()
        self.assertEqual(self.controller.load(self.id)["state"], "invalidated")
        self.assertEqual(self.targets[0].read_text(), "Human instructions.\n")

    def test_policy_and_oracle_changes_cannot_reuse_passing_evidence(self):
        self.controller.experiment(self.id)
        self.policy["providers"]["codex"]["model"] = "different-model"
        with self.assertRaises(LearningError):
            self.controller.apply(self.id)
        self.policy["providers"]["codex"]["model"] = "fixed-model"
        self.suite.write_text("{}")
        with self.assertRaises(LearningError):
            self.controller.apply(self.id)

    def test_failed_revalidation_invalidates_but_provider_error_suspends(self):
        self.active()
        self.result["providers"]["codex"]["errors"] = 1
        result = self.controller.experiment(self.id, validation=True)
        self.assertEqual(result["state"], "unvalidated")
        self.assertIsNone(result["application"])
        self.assertEqual(self.targets[0].read_text(), "Human instructions.\n")
        self.result["providers"]["codex"]["errors"] = 0
        self.active()
        self.result["providers"]["codex"]["candidate_passed"] = False
        result = self.controller.experiment(self.id, validation=True)
        self.assertEqual(result["state"], "invalidated")

    def test_interrupted_two_target_application_recovers_idempotently(self):
        self.controller.experiment(self.id)
        from twill_learning import atomic
        def crash(path, content, mode=0o600):
            if path == self.targets[1]:
                raise OSError("power failure")
            atomic(path, content, mode)
        with mock.patch("twill_learning.atomic", side_effect=crash):
            with self.assertRaises(OSError):
                self.controller.apply(self.id)
        self.assertEqual(self.controller.load(self.id)["state"], "applying")
        self.assertIn(self.record["instruction"], self.targets[0].read_text())
        self.assertEqual(self.targets[1].read_text(), "Human instructions.\n")
        self.assertEqual(self.controller.recover(self.id)["state"], "active")
        self.controller.apply(self.id)
        self.assertEqual(self.targets[0].read_text().count(self.record["instruction"]), 1)

    def test_interrupted_two_target_removal_recovers_without_reapplying(self):
        from twill_learning import atomic

        prefixes = [b"Human instructions for Claude.\n\n", b"Human instructions for Codex.\n\n"]
        suffixes = ["Later human guidance: café.\n".encode(), b"Later human guidance, no final newline."]
        for path, prefix in zip(self.targets, prefixes):
            path.write_bytes(prefix)
        self.active()
        for path, suffix in zip(self.targets, suffixes):
            path.write_bytes(path.read_bytes() + suffix)
        expected = [prefix + suffix for prefix, suffix in zip(prefixes, suffixes)]
        block = self.controller.block(self.controller.load(self.id)).encode()

        def crash(path, content, mode=0o600):
            if path == self.targets[1]:
                raise OSError("power failure during removal")
            atomic(path, content, mode)

        with mock.patch("twill_learning.atomic", side_effect=crash):
            with self.assertRaisesRegex(OSError, "power failure during removal"):
                self.controller.invalidate(self.id, "Synthetic counterexample")
        self.assertEqual(self.controller.load(self.id)["state"], "removing")
        self.assertEqual(self.targets[0].read_bytes(), expected[0])
        self.assertIn(block, self.targets[1].read_bytes())

        # A new process has only the durable removal intent, not in-memory progress.
        restarted = Controller(copy.deepcopy(self.policy))
        self.assertEqual(restarted.recover(self.id)["state"], "invalidated")
        self.assertEqual(restarted.load(self.id)["state"], "invalidated")
        self.assertEqual(restarted.recover(self.id)["state"], "invalidated")
        restarted.cycle()
        with self.assertRaises(LearningError):
            restarted.apply(self.id)
        self.assertEqual(restarted.load(self.id)["state"], "invalidated")
        for path, content in zip(self.targets, expected):
            self.assertEqual(path.read_bytes(), content)
            self.assertNotIn(block, path.read_bytes())

    def test_modified_or_duplicated_managed_block_stops_all_removal(self):
        self.active()
        self.targets[1].write_text(self.targets[1].read_text().replace("authorized task", "other task"))
        result = self.controller.invalidate(self.id, "Remove")
        self.assertEqual(result["state"], "conflict")
        self.assertIn(self.record["instruction"], self.targets[0].read_text())

    def test_missing_final_newline_still_removes_the_entire_owned_block(self):
        self.active()
        for path in self.targets:
            path.write_text(path.read_text().rstrip("\n"))
        self.assertEqual(self.controller.invalidate(self.id, "Remove")["state"], "invalidated")
        for path in self.targets:
            self.assertNotIn(self.record["instruction"], path.read_text())

    def test_recovery_rechecks_revoked_admission_before_second_write(self):
        self.controller.experiment(self.id)
        from twill_learning import atomic
        def crash(path, content, mode=0o600):
            if path == self.targets[1]:
                raise OSError("power failure")
            atomic(path, content, mode)
        with mock.patch("twill_learning.atomic", side_effect=crash):
            with self.assertRaises(OSError):
                self.controller.apply(self.id)
        self.policy["scopes"]["repo"]["auto_apply"] = False
        self.assertEqual(self.controller.recover(self.id)["state"], "unvalidated")
        for path in self.targets:
            self.assertEqual(path.read_text(), "Human instructions.\n")

    def test_binding_without_detector_does_not_authorize_other_lessons(self):
        self.policy["bindings"][0].pop("detector")
        self.policy["bindings"][0]["lesson_id"] = "LC-0000000000000000"
        record = self.controller.load(self.id)
        record["source"]["detector"] = None
        self.controller.save(record, "fixture")
        with self.assertRaises(LearningError):
            self.controller.suite(record)

    def test_context_drift_requires_fresh_passing_experiment_before_reapplication(self):
        self.active()
        self.targets[0].write_text(self.targets[0].read_text() + "New surrounding rule.\n")
        result = self.controller.cycle()
        self.assertEqual(result["results"][0]["state"], "active")
        self.assertEqual(len(self.controller.load(self.id)["experiments"]), 2)
        self.assertIn("New surrounding rule.", self.targets[0].read_text())

    def test_active_suite_drift_suspends_instead_of_leaving_stale_rules(self):
        self.active()
        self.suite.write_text("{}")
        self.controller.cycle()
        self.assertEqual(self.controller.load(self.id)["state"], "unvalidated")
        self.assertNotIn(self.record["instruction"], self.targets[0].read_text())

    def test_exact_original_bytes_restored_without_surrounding_edits(self):
        self.targets[0].write_text("No trailing newline")
        self.targets[1].unlink()
        self.active()
        self.controller.invalidate(self.id, "Rehearsal complete")
        self.assertEqual(self.targets[0].read_text(), "No trailing newline")
        self.assertFalse(self.targets[1].exists())

    def test_unapproved_scope_suite_and_symlinks_are_rejected(self):
        self.policy["bindings"] = []
        with self.assertRaises(LearningError):
            self.controller.experiment(self.id)
        self.policy["scopes"]["repo"]["auto_apply"] = False
        self.controller.save({**self.controller.load(self.id), "state": "validated",
                              "validated_policy_sha": digest(self.policy)}, "fixture")
        with self.assertRaises(LearningError):
            self.controller.apply(self.id)
        self.targets[0].unlink()
        self.targets[0].symlink_to(self.source)
        with self.assertRaises(LearningError):
            Controller(self.policy)

    def test_lesson_is_immutable_and_secret_values_cannot_enter_it(self):
        fields = self.controller.load(self.id)
        fields["instruction"] = "Changed without a new version"
        self.controller.path(self.id).write_text(json.dumps(fields))
        with self.assertRaises(LearningError):
            self.controller.load(self.id)
        with self.assertRaises(LearningError):
            self.controller.create({"instruction": "Use sk-" + "a" * 20,
                                    "scope": "repo", "source": {"path": str(self.source)}})

    def test_target_edit_during_trial_cannot_bless_a_new_context(self):
        def intervening_edit(*args, **kwargs):
            self.targets[0].write_text("Changed while experiment ran.\n")
            return copy.deepcopy(self.result)
        self.adapter.run_experiment.side_effect = intervening_edit
        result = self.controller.experiment(self.id)
        self.assertEqual(result["state"], "unvalidated")
        with self.assertRaises(LearningError):
            self.controller.apply(self.id)

    def test_provider_context_manifests_capture_global_and_nested_rules(self):
        extra = self.root / "global.md"
        extra.write_text("Global guidance.")
        self.policy["scopes"]["repo"]["context_paths"] = {
            "claude": [str(extra), str(self.targets[1])],
            "codex": [str(extra), str(self.targets[0])],
        }
        self.active()
        context = self.controller.context(self.controller.load(self.id))
        self.assertIn("Global guidance.", context["claude"])
        self.assertNotIn(self.record["instruction"], context["codex"])

    def test_cycle_cannot_multiply_the_experiment_budget(self):
        self.policy["limits"] = {"max_lessons_per_cycle": 2}
        with self.assertRaises(LearningError):
            Controller(self.policy)


if __name__ == "__main__":
    unittest.main()
