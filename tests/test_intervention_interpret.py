"""Contextual hypotheses require original native evidence and independent review."""
import copy
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import twill_experiments as experiments
import twill_intervention_interpret as interpretation
import twill_intervention_mining as mining


class InterpretationTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="interpret-test-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.artifacts = self.root / "private"
        self.repo = self.root / "fixture"
        self.repo.mkdir()
        self.target = self.repo / "AGENTS.md"
        self.target.write_text("Use ordinary repository conventions.\n")
        self.source = self.root / "native.jsonl"
        self.human = "Actually, from now on, continue authorized work after answering status questions."
        rows = [("user", "Check the report."), ("assistant", "I will stop here."),
                ("user", self.human), ("assistant", "Continuing now.")]
        self.source.write_text("".join(json.dumps({"type": role, "sessionId": "fixture",
            "cwd": str(self.repo), "message": {"role": role, "content": [{"type": "text", "text": text}]}}) + "\n" for role, text in rows))
        os.utime(self.source, (1, 1))
        self.providers = {"claude": {"model": "fixture-claude"}, "codex": {"model": "fixture-codex"}}
        self.policy = {"artifacts_root": str(self.artifacts), "providers": self.providers,
                       "intervention_sources": [str(self.source)], "intervention_settle_seconds": 60,
                       "two_pass": {"enabled": True, "scopes": ["repository"], "max_calls_per_cycle": 8},
                       "scopes": {"repository": {"launchdirs": [str(self.repo)], "targets": [str(self.target)]}}}
        mining.discover(self.policy, self.artifacts)
        self.path = next(p for p in (self.artifacts / "learning" / "interventions").glob("IE-*.json")
                         if json.loads(p.read_text())["classification"]["signal"] == "candidate")
        self.episode = mining.verify_episode(self.path)
        self.index = self.episode["intervention_index"]
        citation = {"turn_index": self.index, "quote": self.human}
        claim = {"text": "Continue authorized work after status answers.", "citations": [citation]}
        self.author = {"disposition": "candidate_lesson", "what_failed": copy.deepcopy(claim),
                       "human_wanted": copy.deepcopy(claim),
                       "observed_outcome": {"status": "UNKNOWN", "text": "", "citations": []},
                       "scope": {"breadth": "repository", "rationale": copy.deepcopy(claim)},
                       "durable_intent": {"kind": "explicit", "citations": [citation]},
                       "instruction": self.human, "applies_when": [copy.deepcopy(claim)], "exceptions": [],
                       "novelty": {"status": "new", "context_refs": [str(self.target)], "rationale": copy.deepcopy(claim)},
                       "validation_plan": ["Use an independently authored continuation evaluation suite."], "confidence": "high"}
        self.critic = {"verdict": "accept", "disposition": "candidate_lesson",
                       "supported_claims": sorted(interpretation._CLAIMS), "issues": [],
                       "citations": [citation], "confidence": "high"}
        self.metadata = {name: {"model": conf["model"], "version": "fixture-1",
                               "fingerprint": experiments._fingerprint(conf["model"], "fixture-1")}
                         for name, conf in self.providers.items()}
        self.calls = []

    def invoke(self, prompt, *, work_root, provider, config, timeout_seconds, metadata):
        self.calls.append((provider, prompt))
        return {"status": "ok", "provider": provider, "context_verified": True,
                "actual_model": config["model"], "model_fingerprint": metadata["fingerprint"],
                "output": json.dumps(self.author if provider == "claude" else self.critic), "calls": 1}

    def run_interpret(self):
        with patch.object(interpretation, "provider_fingerprints", return_value=self.metadata), \
                patch.object(interpretation, "run_analysis", side_effect=self.invoke):
            return interpretation.interpret(self.policy, self.artifacts, self.root / "work", self.providers)

    def artifact(self):
        return self.artifacts / "learning" / "interpretations" / self.path.name

    def test_unknown_outcome_is_unvalidated_candidate_not_invented_success(self):
        result = self.run_interpret()
        self.assertEqual(result["calls"], 4)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["kind"], "human_directed")
        self.assertEqual(candidate["observed_outcome"]["status"], "UNKNOWN")
        self.assertEqual(candidate["assessment"], "needs_evaluation")
        self.assertEqual(candidate, interpretation.verified_candidate(self.artifact(), self.policy))
        self.assertEqual(len(interpretation.verified_candidates(self.policy, self.artifacts)), 1)
        self.assertNotIn("expect", candidate)
        self.assertEqual(self.run_interpret()["calls"], 0)
        self.assertIn("I will stop here.", self.calls[0][1])
        self.assertIn("Continuing now.", self.calls[0][1])
        self.assertIn("independently", self.calls[1][1].lower())
        self.assertEqual(self.target.read_text(), "Use ordinary repository conventions.\n")

    def test_paraphrase_cannot_claim_explicit_authority(self):
        self.author["instruction"] = "Continue all authorized tasks automatically."
        result = self.run_interpret()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(json.loads(self.path.read_text())["state"], "needs_review")

    def test_paraphrase_is_allowed_only_as_inferred_hypothesis(self):
        self.author["instruction"] = "Continue authorized work after status answers."
        self.author["durable_intent"]["kind"] = "inferred"
        candidate = self.run_interpret()["candidates"][0]
        self.assertEqual(candidate["kind"], "needs_improvement")
        self.assertIsNone(candidate["explicit_authority"])

    def test_assistant_claim_does_not_establish_known_outcome(self):
        index = next(i for i, t in enumerate(self.episode["turns"]) if t["text"] == "Continuing now.")
        self.author["observed_outcome"] = {"status": "KNOWN", "text": "The work finished.",
                                            "citations": [{"turn_index": index, "quote": "Continuing now."}]}
        self.assertEqual(self.run_interpret()["candidates"], [])
        self.assertEqual(json.loads(self.path.read_text())["state"], "pending_retry")

    def test_uncited_claim_extra_field_and_bad_quote_fail_closed(self):
        for change in (lambda a: a.update(invented="yes"),
                       lambda a: a["what_failed"].update(citations=[]),
                       lambda a: a["human_wanted"]["citations"][0].update(quote="fabricated quote")):
            author = copy.deepcopy(self.author)
            change(author)
            with self.assertRaises(ValueError):
                interpretation._validate_author(author, self.episode, interpretation._context(self.policy))

    def test_novelty_can_cite_captured_guide_context_without_human_quotation(self):
        self.author["novelty"]["rationale"] = {"text": "The captured guide does not cover continuation after status answers.",
                                                "citations": []}
        result = self.run_interpret()
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(self.author["novelty"]["context_refs"], [str(self.target)])
        self.assertIsNotNone(interpretation.verified_candidate(self.artifact(), self.policy))
        self.target.write_text("Changed guide coverage.\n")
        self.assertIsNone(interpretation.verified_candidate(self.artifact(), self.policy))

    def test_novelty_context_evidence_rejects_invalid_paths_and_untethered_rationales(self):
        context = interpretation._context(self.policy)
        base = copy.deepcopy(self.author)
        base["novelty"]["rationale"]["citations"] = []
        for refs in ([], [str(self.root / "foreign-guide.md")], ["AGENTS.md"], [None], "not-a-list"):
            author = copy.deepcopy(base)
            author["novelty"]["context_refs"] = refs
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                interpretation._validate_author(author, self.episode, context)
        for change in (
            lambda a: a["novelty"].pop("context_refs"),
            lambda a: a["novelty"]["rationale"].update(text=""),
            lambda a: a["novelty"]["rationale"].update(text=" " * 3),
            lambda a: a["novelty"]["rationale"].update(text="x" * 8193),
            lambda a: a["novelty"]["rationale"].update(citations=[{"turn_index": self.index, "quote": "Fabricated guide quotation."}]),
        ):
            author = copy.deepcopy(base)
            change(author)
            with self.assertRaises(ValueError):
                interpretation._validate_author(author, self.episode, context)

    def test_context_backed_novelty_does_not_excuse_uncited_historical_facts(self):
        base = copy.deepcopy(self.author)
        base["novelty"]["rationale"]["citations"] = []
        context = interpretation._context(self.policy)
        changes = (
            lambda a: a["what_failed"].update(citations=[]),
            lambda a: a["human_wanted"].update(citations=[]),
            lambda a: a["scope"]["rationale"].update(citations=[]),
            lambda a: a["applies_when"][0].update(citations=[]),
            lambda a: a["observed_outcome"].update(text="A historical outcome claim.", citations=[]),
            lambda a: a["durable_intent"].update(citations=[]),
            lambda a: a.update(exceptions=[{"text": "A historical exception.", "citations": []}]),
        )
        for change in changes:
            author = copy.deepcopy(base)
            change(author)
            with self.assertRaises(ValueError):
                interpretation._validate_author(author, self.episode, context)

    def test_critic_must_independently_confirm_every_field(self):
        self.critic["supported_claims"].remove("scope")
        result = self.run_interpret()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["calls"], 4)
        record = json.loads(self.artifact().read_text())
        self.assertIn("independent confirmation", record["error"])
        self.assertEqual(len(record["attempts"]), 1)

    def test_model_error_is_pending_and_cooldown_has_no_calls(self):
        with patch.object(interpretation, "provider_fingerprints", return_value=self.metadata), \
                patch.object(interpretation, "run_analysis", return_value={"status": "error", "error": "timeout"}):
            result = interpretation.interpret(self.policy, self.artifacts, self.root / "work", self.providers)
        self.assertEqual(result["calls"], 3)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(json.loads(self.artifact().read_text())["attempts"]), 1)
        self.assertEqual(json.loads(self.path.read_text())["state"], "pending_retry")
        self.assertEqual(self.run_interpret()["calls"], 0)

    def test_durable_daily_budget_prevents_model_calls(self):
        self.policy["two_pass"]["max_calls_per_day"] = 3
        self.assertEqual(self.run_interpret()["calls"], 0)
        self.assertEqual(self.calls, [])

    def test_code_fix_and_already_covered_have_meaningful_terminal_dispositions(self):
        for disposition in ("code_fix", "already_covered"):
            with self.subTest(disposition=disposition):
                payload = json.loads(self.path.read_text())
                payload["state"] = "pending_interpretation"
                self.path.write_text(json.dumps(payload))
                self.author["disposition"] = self.critic["disposition"] = disposition
                self.author["instruction"] = ""
                result = self.run_interpret()
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["dispositions"], {disposition: 1})
                self.assertEqual(json.loads(self.path.read_text())["state"], "interpreted")

    def test_candidate_revalidation_rejects_context_source_policy_and_model_drift(self):
        self.run_interpret()
        record = json.loads(self.artifact().read_text())
        self.target.write_text("Changed guidance\n")
        self.assertIsNone(interpretation.verified_candidate(self.artifact(), self.policy))
        self.target.write_text("Use ordinary repository conventions.\n")
        altered = copy.deepcopy(record)
        altered["author"]["actual_model"] = "other-model"
        self.artifact().write_text(json.dumps(altered))
        self.assertIsNone(interpretation.verified_candidate(self.artifact(), self.policy))
        self.artifact().write_text(json.dumps(record))
        policy = copy.deepcopy(self.policy)
        policy["scopes"]["repository"]["launchdirs"] = [str(self.root)]
        self.assertIsNone(interpretation.verified_candidate(self.artifact(), policy))
        self.source.write_text(self.source.read_text().replace("Continuing now.", "Done instead."))
        self.assertIsNone(interpretation.verified_candidate(self.artifact(), self.policy))

    def test_global_scope_needs_explicit_global_human_words(self):
        self.policy["scopes"]["global"] = {"global": True, "targets": [str(self.target)]}
        self.policy["two_pass"]["scopes"].append("global")
        self.author["scope"]["breadth"] = "global"
        self.assertEqual(self.run_interpret()["candidates"], [])

    def test_unenrolled_cwd_has_no_global_fallback(self):
        self.policy["scopes"]["repository"]["launchdirs"] = [str(self.root / "different")]
        self.policy["scopes"]["global"] = {"global": True, "targets": [str(self.target)]}
        self.policy["two_pass"]["scopes"].append("global")
        self.assertEqual(self.run_interpret()["candidates"], [])

    def test_tampered_native_evidence_is_reviewable_without_calls(self):
        self.source.write_text(self.source.read_text().replace("I will stop here.", "Modified response."))
        result = self.run_interpret()
        self.assertEqual(result["calls"], 0)
        self.assertEqual(json.loads(self.path.read_text())["state"], "needs_review")

    def test_later_verified_withdrawal_or_replacement_blocks_even_accepted_hypotheses(self):
        base_rows = self.source.read_text()
        for kind, later in (("retraction", "Withdraw the earlier rule; do not apply it."),
                            ("replacement", "From now on, ask before modifying files.")):
            for intent in ("explicit", "inferred"):
                with self.subTest(kind=kind, intent=intent):
                    # More later assistant records than the local following
                    # window prove the veto uses the full native source.
                    rows = [("assistant", "Unrelated later detail " + str(n)) for n in range(8)]
                    rows.append(("user", later))
                    appended = "".join(json.dumps({"type": role, "sessionId": "fixture", "cwd": str(self.repo),
                        "message": {"role": role, "content": [{"type": "text", "text": text}]}}) + "\n" for role, text in rows)
                    self.source.write_text(base_rows + appended)
                    os.utime(self.source, (1, 1))
                    mining.discover(self.policy, self.artifacts)
                    for path in self.path.parent.glob("IE-*.json"):
                        payload = json.loads(path.read_text())
                        payload["state"] = "pending_interpretation" if path == self.path else "negative_sample"
                        payload.pop("retry_after", None)
                        path.write_text(json.dumps(payload))
                    episode = mining.verify_episode(self.path)
                    self.assertEqual(episode["authority_updates"][0]["kind"], kind)
                    self.assertNotIn(later, " ".join(turn["text"] for turn in episode["turns"]))
                    self.assertEqual(interpretation._evidence(episode)["authority_updates"], episode["authority_updates"])
                    self.assertEqual(interpretation._prompt_episode(episode)["authority_updates"], episode["authority_updates"])
                    self.author["durable_intent"]["kind"] = intent
                    result = self.run_interpret()
                    self.assertEqual(result["processed"], 1)
                    self.assertEqual(result["candidates"], [])
                    self.assertEqual(json.loads(self.path.read_text())["state"], "needs_review")
                    self.assertIsNone(interpretation.verified_candidate(self.artifact(), self.policy))
        # A revoked steering rule still permits a meaningful nonlesson finding.
        payload = json.loads(self.path.read_text())
        payload["state"] = "pending_interpretation"
        self.path.write_text(json.dumps(payload))
        self.author["disposition"] = self.critic["disposition"] = "code_fix"
        result = self.run_interpret()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["dispositions"], {"code_fix": 1})
        self.assertEqual(json.loads(self.path.read_text())["state"], "interpreted")

    def controller(self):
        from twill_learning import Controller, POLICY_SCHEMA
        self.policy.update(schema=POLICY_SCHEMA, enabled=True, work_root=str(self.root / "work"))
        return Controller(self.policy)

    def test_controller_discovery_is_model_free_and_replaces_legacy_scanners(self):
        controller = self.controller()
        with patch.object(interpretation, "run_analysis", side_effect=AssertionError("model called")), \
                patch("twill_learning_intake.discover_sources", side_effect=AssertionError("legacy scanner")), \
                patch("twill_human_steering.discover", side_effect=AssertionError("legacy scanner")):
            result = controller.discover()
        self.assertEqual(result["experimented"], 0)
        self.assertIn("interventions", result["input_coverage"])
        self.assertEqual(controller.records(), [])

    def test_controller_imports_unsupported_verbatim_candidate_visible_without_oracle(self):
        controller = self.controller()
        self.run_interpret()
        self.assertEqual(controller.import_interpretations(), 1)
        record = controller.records()[0]
        self.assertEqual(record["kind"], "human_directed")
        self.assertEqual(record["state"], "needs_evaluation")
        self.assertIsNone(record["evaluator"])
        self.assertTrue(controller.source_current(record))
        self.assertEqual(record["source_evidence"]["type"], "interpreted-intervention")
        self.assertEqual(controller.import_interpretations(), 0)
        self.assertEqual(self.target.read_text(), "Use ordinary repository conventions.\n")
        controller.publish()
        manifest = json.loads((controller.store / "manifest.json").read_text())
        self.assertIn("learning/interpretations/" + self.path.name, [f["path"] for f in manifest["files"]])

    def test_controller_create_derives_kind_and_rejects_forged_scope_instruction_evidence(self):
        from twill_learning import LearningError
        controller = self.controller()
        candidate = self.run_interpret()["candidates"][0]
        base = {"instruction": candidate["instruction"], "scope": candidate["scope"],
                "source": {"path": candidate["source"]}}
        for change in ({"kind": "inferred"}, {"scope": "global"}, {"instruction": "Broader invented rule"}):
            with self.assertRaises(LearningError):
                controller.create(base | change)
        record = controller.create(base | {"source_evidence": {"type": "human-steering", "complete": True}})
        self.assertEqual(record["source_evidence"]["type"], "interpreted-intervention")
        self.source.write_text(self.source.read_text().replace("Continuing now.", "Different result."))
        self.assertFalse(controller.source_current(record))

    def test_controller_inferred_candidate_requires_policy_bound_independent_suite(self):
        from twill_learning import LearningError
        controller = self.controller()
        self.author["durable_intent"]["kind"] = "inferred"
        self.author["instruction"] = "Continue authorized work after status answers."
        self.run_interpret()
        controller.import_interpretations()
        record = controller.records()[0]
        self.assertEqual(record["kind"], "inferred")
        with self.assertRaises(LearningError):
            controller.suite(record)
        self.assertEqual(record["state"], "needs_evaluation")

    def medium_inferred(self):
        self.author["durable_intent"]["kind"] = "inferred"
        self.author["instruction"] = "Continue authorized work after status answers."
        self.author["confidence"] = self.critic["confidence"] = "medium"
        self.critic["supported_claims"] = sorted({"what_failed", "human_wanted", "scope", "novelty",
                                                  "instruction", "applies_when", "durable_intent"})

    def test_critic_requires_substantive_claims_not_empty_fields_or_advisory_plans(self):
        self.medium_inferred()
        self.assertEqual(interpretation._validate_critic(self.critic, self.episode, self.author), self.critic)
        for field in self.critic["supported_claims"]:
            critic = copy.deepcopy(self.critic)
            critic["supported_claims"].remove(field)
            with self.subTest(missing=field), self.assertRaises(ValueError):
                interpretation._validate_critic(critic, self.episode, self.author)
        for field in ("exceptions", "observed_outcome"):
            author = copy.deepcopy(self.author)
            claim = copy.deepcopy(author["what_failed"])
            author[field] = [claim] if field == "exceptions" else claim | {"status": "UNKNOWN"}
            with self.subTest(nonempty=field), self.assertRaises(ValueError):
                interpretation._validate_critic(self.critic, self.episode, author)
            critic = copy.deepcopy(self.critic)
            critic["supported_claims"].append(field)
            interpretation._validate_critic(critic, self.episode, author)
        for extra in ("invented_field", self.critic["supported_claims"][0]):
            critic = copy.deepcopy(self.critic)
            critic["supported_claims"].append(extra)
            with self.subTest(invalid_field=extra), self.assertRaises(ValueError):
                interpretation._validate_critic(critic, self.episode, self.author)
        critic = copy.deepcopy(self.critic)
        critic["supported_claims"].append("validation_plan")
        interpretation._validate_critic(critic, self.episode, self.author)

    def test_medium_repository_hypothesis_is_only_an_unvalidated_trial(self):
        from twill_learning import LearningError
        controller = self.controller()
        self.medium_inferred()
        result = self.run_interpret()
        candidate = result["candidates"][0]
        self.assertEqual(candidate["kind"], "needs_improvement")
        self.assertEqual(candidate["assessment"], "needs_evaluation")
        self.assertEqual(candidate["trial_eligibility"], {"confidence": {"author": "medium", "critic": "medium"},
                                                         "requires_measured_improvement": True})
        interpretation_record = json.loads(self.artifact().read_text())
        self.assertEqual(interpretation_record["state"], "interpreted")
        self.assertEqual(result["calls"], 4)
        self.assertEqual(len(interpretation_record["attempts"]), 1)
        self.assertTrue(all(json.loads(attempt[role]["output"])["confidence"] == "medium"
                            for attempt in interpretation_record["attempts"] for role in ("author", "critic")))
        self.assertEqual(controller.import_interpretations(), 1)
        record = controller.records()[0]
        self.assertEqual((record["kind"], record["state"], record["evaluator"]), ("inferred", "needs_evaluation", None))
        self.assertIsNone(record["application"])
        with self.assertRaises(LearningError):
            controller.suite(record)
        with self.assertRaises(LearningError):
            controller.apply(record["id"])
        self.assertEqual(self.target.read_text(), "Use ordinary repository conventions.\n")

    def test_trial_eligibility_never_downgrades_explicit_authority_or_accepts_low_confidence(self):
        self.run_interpret()
        record = json.loads(self.artifact().read_text())
        for author_confidence, critic_confidence, intent, breadth, verdict in (
            ("medium", "medium", "explicit", "repository", "accept"),
            ("low", "high", "inferred", "repository", "accept"),
            ("high", "low", "inferred", "repository", "accept"),
            ("medium", "medium", "inferred", "global", "accept"),
            ("medium", "medium", "inferred", "task", "accept"),
            ("medium", "medium", "inferred", "repository", "reject"),
            ("medium", "medium", "inferred", "repository", "needs_review"),
        ):
            author, critic = copy.deepcopy(self.author), copy.deepcopy(self.critic)
            author["confidence"], critic["confidence"] = author_confidence, critic_confidence
            author["durable_intent"]["kind"], author["scope"]["breadth"] = intent, breadth
            critic["verdict"] = verdict
            changed = copy.deepcopy(record)
            changed["author"]["output"], changed["critic"]["output"] = json.dumps(author), json.dumps(critic)
            with self.subTest(author=author_confidence, critic=critic_confidence, intent=intent, breadth=breadth, verdict=verdict):
                self.assertIsNone(interpretation._candidate(changed, self.artifact(), self.policy, self.episode))

    def evaluate_medium_trial(self, mode):
        """Freeze an independent suite before interpretation; use real grading aggregation."""
        from twill_learning import LearningError, digest
        self.medium_inferred()
        self.policy["scopes"]["repository"]["auto_apply"] = True
        cases = [{"id": ident, "split": split, "guardrail": guard,
                  "prompt": "INDEPENDENT_CASE_ONLY_SENTINEL " + ident, "expect": {"exact": "FROZEN_ORACLE_LABEL"}}
                 for ident, split, guard in (("train", "train", False), ("holdout", "holdout", False), ("guard", "holdout", True))]
        suite_path = self.root / "independent-suite.json"
        suite_path.write_text(json.dumps({"cases": cases}))
        self.policy["bindings"] = [{"scope": "repository", "detector": "contextual-intervention",
                                     "suite": str(suite_path), "suite_sha256": digest(suite_path.read_bytes())}]
        controller = self.controller()
        self.run_interpret()
        controller.import_interpretations()
        record = controller.records()[0]
        self.assertEqual(record["state"], "draft")
        with self.assertRaises(LearningError):
            controller.apply(record["id"])
        for _, prompt in self.calls:
            self.assertNotIn("INDEPENDENT_CASE_ONLY_SENTINEL", prompt)
            self.assertNotIn("FROZEN_ORACLE_LABEL", prompt)
        def evaluate(spec, **kwargs):
            self.assertEqual(spec["cases"], cases)
            trials = []
            for case in cases:
                for repetition in range(controller.repetitions):
                    for arm in ("baseline", "candidate"):
                        status = "pass"
                        if arm == "baseline" and mode != "redundant" and not case["guardrail"]:
                            status = "fail"
                        if arm == "candidate" and case["guardrail"] and mode in {"guard_failure", "inconclusive"}:
                            status = "fail" if mode == "guard_failure" else "error"
                        trials.append({"case_id": case["id"], "split": case["split"], "repetition": repetition,
                                       "arm": arm, "status": status})
            reports = {name: experiments._verdict(trials) | {"actual_model": conf["model"],
                        "model_fingerprint": self.metadata[name]["fingerprint"]} for name, conf in self.providers.items()}
            if mode == "missing_provider":
                reports.pop("codex")
            verdict = "promote" if len(reports) == 2 and all(r["verdict"] == "improved" for r in reports.values()) else "hold"
            return {"candidate_sha": record["instruction_sha"], "verdict": verdict, "providers": reports}
        with patch.object(experiments, "run_experiment", side_effect=evaluate), \
                patch.object(experiments, "provider_fingerprints", return_value=self.metadata):
            evaluated = controller.experiment(record["id"])
            if mode == "improved":
                self.assertEqual(evaluated["state"], "validated")
                self.assertEqual(controller.apply(record["id"])["state"], "active")
            else:
                self.assertIn(evaluated["state"], {"rejected", "unvalidated"})
                with self.assertRaises(LearningError):
                    controller.apply(record["id"])
                self.assertEqual(self.target.read_text(), "Use ordinary repository conventions.\n")
        return evaluated

    def test_medium_trial_applies_only_after_independent_both_provider_improvement(self):
        self.evaluate_medium_trial("improved")

    def test_medium_trial_rejects_already_correct_baselines(self):
        record = self.evaluate_medium_trial("redundant")
        self.assertIn("redundant", record["reason"])

    def test_medium_trial_rejects_guardrail_failure(self):
        self.evaluate_medium_trial("guard_failure")

    def test_medium_trial_does_not_apply_inconclusive_result(self):
        self.evaluate_medium_trial("inconclusive")

    def test_medium_trial_requires_both_provider_results(self):
        self.evaluate_medium_trial("missing_provider")

    def test_redacted_instruction_remains_reviewable_without_candidate_authority(self):
        self.author["durable_intent"]["kind"] = "inferred"
        self.author["instruction"] = "Keep <redacted:secret> for future tasks."
        result = self.run_interpret()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(json.loads(self.path.read_text())["state"], "needs_review")

    def test_controller_cycle_interprets_then_imports_without_unbound_experiment(self):
        controller = self.controller()
        with patch.object(interpretation, "provider_fingerprints", return_value=self.metadata), \
                patch.object(interpretation, "run_analysis", side_effect=self.invoke), \
                patch.object(experiments, "run_experiment", side_effect=AssertionError("no independently bound suite")):
            summary = controller.cycle()
        self.assertEqual(summary["imported"], 1)
        self.assertEqual(summary["interpretation"]["processed"], 1)
        self.assertEqual(summary["experimented"], 0)
        self.assertNotIn("instruction", summary["interpretation"]["candidates"][0])
        self.assertEqual(controller.records()[0]["state"], "needs_evaluation")

    def test_controller_receipt_binds_both_pass_implementations(self):
        from twill_learning import digest
        controller = self.controller()
        self.author["durable_intent"]["kind"] = "inferred"
        self.run_interpret()
        controller.import_interpretations()
        record = controller.records()[0]
        suite = {"cases": [{"id": "train", "split": "train", "prompt": "A", "expect": {"exact": "A"}},
                            {"id": "holdout", "split": "holdout", "guardrail": True, "prompt": "B", "expect": {"exact": "B"}}]}
        result = {"candidate_sha": record["instruction_sha"], "verdict": "hold", "providers": {
            name: {"candidate_passed": True, "holdout_passed": True, "errors": 0, "verdict": "inconclusive"}
            for name in self.providers}}
        with patch.object(controller, "suite", return_value=suite), patch.object(experiments, "run_experiment", return_value=result):
            updated = controller.experiment(record["id"])
        receipt = updated["experiments"][-1]
        self.assertEqual(receipt["miner_sha"], digest(Path(mining.__file__).read_bytes()))
        self.assertEqual(receipt["interpreter_sha"], digest(Path(interpretation.__file__).read_bytes()))
        self.assertEqual(updated["state"], "unvalidated")

    def test_large_exact_context_is_preserved_in_compact_analysis_prompt(self):
        self.target.write_text("A" * 19043)
        second = self.repo / "CLAUDE.md"
        second.write_text("B" * 54150)
        self.policy["scopes"]["repository"]["targets"].append(str(second))
        context = interpretation._context(self.policy)
        prompt = interpretation._author_prompt(self.episode, context)
        self.assertIn("B" * 54150, prompt)
        self.assertIn("A" * 19043, prompt)
        self.assertEqual(prompt.count(self.human), 1)
        self.assertLess(len(prompt.encode()), experiments.MAX_ANALYSIS_PROMPT)

    def test_owned_application_block_does_not_invalidate_its_own_frozen_context(self):
        controller = self.controller()
        self.run_interpret()
        controller.import_interpretations()
        record = controller.records()[0]
        addition = "\n\n" + controller.block(record) + "\n"
        self.target.write_text(self.target.read_text() + addition)
        record["application"] = {"files": [{"path": str(self.target), "addition": addition}]}
        self.assertTrue(controller.source_current(record))
        self.target.write_text("New unrelated guidance\n" + self.target.read_text())
        self.assertFalse(controller.source_current(record))

    def two_lesson_controller(self, *, second_after_first=False):
        """Learn independent native episodes and exercise actual delivery gates."""
        from twill_learning import digest
        second = self.root / "second-native.jsonl"
        second_human = "Actually, from now on, preserve unrelated edits during repository work."
        self.policy["intervention_sources"].append(str(second))
        self.policy["scopes"]["repository"]["auto_apply"] = True
        second_rows = self.source.read_text().replace(self.human, second_human).replace('"fixture"', '"second-fixture"')
        controller = self.controller()
        suite = {"cases": [
            {"id": "train", "split": "train", "prompt": "A", "expect": {"exact": "A"}},
            {"id": "holdout", "split": "holdout", "prompt": "B", "expect": {"exact": "B"}},
            {"id": "guard", "split": "holdout", "guardrail": True, "prompt": "C", "expect": {"exact": "C"}},
        ]}
        def invoke(prompt, **kwargs):
            answer = self.invoke(prompt, **kwargs)
            if second_human in prompt:
                answer["output"] = answer["output"].replace(self.human, second_human)
            return answer
        def start(patcher):
            self.addCleanup(patcher.stop)
            return patcher.start()
        start(patch.object(interpretation, "provider_fingerprints", return_value=self.metadata))
        start(patch.object(interpretation, "run_analysis", side_effect=invoke))
        start(patch.object(controller, "suite", return_value=suite))
        start(patch.object(experiments, "provider_fingerprints", return_value=self.metadata))
        expected = len(suite["cases"]) * controller.repetitions
        def evaluate(spec, **kwargs):
            return {"candidate_sha": digest(spec["instruction"].encode()),
                    "verdict": "promote", "providers": {
                        name: {"candidate_passed": True, "holdout_passed": True, "guardrails_passed": True,
                               "errors": 0, "verdict": "improved", "actual_model": conf["model"],
                               "model_fingerprint": self.metadata[name]["fingerprint"],
                               "counts": {arm: {"pass": expected, "fail": 0, "error": 0}
                                          for arm in ("baseline", "candidate")}}
                        for name, conf in self.providers.items()}}
        evaluator = start(patch.object(experiments, "run_experiment", side_effect=evaluate))
        def learn_second():
            second.write_text(second_rows)
            os.utime(second, (1, 1))
            mining.discover(self.policy, self.artifacts)
        if not second_after_first:
            learn_second()
        interpretation.interpret(self.policy, self.artifacts, self.root / "work", self.providers)
        controller.import_interpretations()
        first = next(r for r in controller.records() if r["instruction"] == self.human)
        self.assertEqual(controller.experiment(first["id"])["state"], "validated")
        first = controller.apply(first["id"])
        if second_after_first:
            learn_second()
            interpretation.interpret(self.policy, self.artifacts, self.root / "work", self.providers)
            controller.import_interpretations()
        second_record = next(r for r in controller.records() if r["instruction"] == second_human)
        self.assertEqual(controller.experiment(second_record["id"])["state"], "validated")
        second_record = controller.apply(second_record["id"])
        return controller, first, second_record, evaluator

    def test_independently_applied_lessons_keep_provenance_and_require_context_retest(self):
        from twill_learning import LearningError
        controller, first, second, evaluator = self.two_lesson_controller()
        self.assertTrue(controller.source_current(first))
        self.assertTrue(controller.source_current(second))
        self.assertIn(controller.block(second), controller.context(first)["codex"])
        self.assertNotIn(controller.block(first), controller.context(first)["codex"])
        with self.assertRaisesRegex(LearningError, "surrounding instructions changed"):
            controller.fresh_evidence(first)
        self.assertEqual(controller.experiment(first["id"], validation=True)["state"], "active")
        current = controller.load(first["id"])
        controller.fresh_evidence(current)
        self.assertIn(controller.block(second), evaluator.call_args.args[0]["context"]["codex"])
        self.assertIsNone(interpretation.verified_candidate(first["source"]["path"], self.policy))
        with self.assertRaises(LearningError):
            controller.create({"instruction": first["instruction"], "scope": "repository", "source": first["source"]})

    def test_later_interpretation_keeps_owned_blocks_already_in_frozen_context(self):
        controller, first, second, _ = self.two_lesson_controller(second_after_first=True)
        captured = json.loads(Path(second["source"]["path"]).read_text())["context"]
        self.assertIn(controller.block(first), captured[0]["text"])
        self.assertTrue(controller.source_current(first))
        self.assertTrue(controller.source_current(second))

    def active_alias_fixture(self, *, preexisting_alias=False):
        self.policy["intervention_sources"] = [str(self.root / "*.jsonl")]
        alias = self.root / "archive-copy.jsonl"
        if preexisting_alias:
            alias.write_bytes(self.source.read_bytes())
            os.utime(alias, (1, 1))
        controller, first, _, _ = self.two_lesson_controller()
        first = controller.experiment(first["id"], validation=True)
        self.assertEqual(first["state"], "active")
        self.assertTrue(controller.source_current(first))
        controller.fresh_evidence(first)
        return controller, first, alias

    def test_active_lesson_survives_exact_byte_alias_discovery_without_evidence_drift(self):
        controller, first, alias = self.active_alias_fixture()
        before = mining.verify_episode(self.path)
        evidence_before = interpretation._evidence(before)
        alias.write_bytes(self.source.read_bytes())
        os.utime(alias, (1, 1))
        alias.with_suffix(".manifest.json").write_text(json.dumps({"source_version": "alias-fixture", "captured_at": "later"}))
        mining.discover(self.policy, self.artifacts)
        after = mining.verify_episode(self.path)
        self.assertEqual(after["source_original"], before["source_original"])
        self.assertEqual(after["source_copies"], before["source_copies"])
        self.assertEqual(interpretation._evidence(after), evidence_before)
        self.assertEqual(len(after["source_aliases"]), 1)
        self.assertTrue(controller.source_current(first))
        controller.fresh_evidence(first)
        self.assertEqual(self.target.read_text().count(controller.block(first)), 1)

    def test_active_lesson_fallback_keeps_canonical_history_but_checks_live_alias_enrollment(self):
        controller, first, alias = self.active_alias_fixture(preexisting_alias=True)
        before = mining.verify_episode(self.path)
        canonical = Path(before["source_original"]["source_path"])
        canonical.unlink()
        after = mining.verify_episode(self.path, enrolled_sources=self.policy["intervention_sources"])
        self.assertEqual(interpretation._evidence(after), interpretation._evidence(before))
        self.assertEqual(after["resolved_sources"], [{"path": str(alias), "sha256": before["source_original"]["file_sha256"]}])
        self.assertTrue(controller.source_current(first))
        controller.fresh_evidence(first)
        self.policy["intervention_sources"] = [str(canonical), str(self.root / "second-native.jsonl")]
        self.assertFalse(controller.source_current(first))
        with self.assertRaises(ValueError):
            mining.verify_episode(self.path, enrolled_sources=self.policy["intervention_sources"])

    def test_alias_cannot_mask_new_full_generation_or_later_withdrawal(self):
        controller, first, alias = self.active_alias_fixture(preexisting_alias=True)
        original_hash = mining.verify_episode(self.path)["source_original"]["file_sha256"]
        with self.source.open("a") as source:
            source.write(json.dumps({"type": "user", "sessionId": "fixture", "cwd": str(self.repo),
                                    "message": {"role": "user", "content": "Withdraw the earlier rule; do not apply it."}}) + "\n")
        os.utime(self.source, (1, 1))
        self.assertFalse(controller.source_current(first))
        mining.discover(self.policy, self.artifacts)
        current = mining.verify_episode(self.path)
        self.assertNotEqual(current["source_original"]["file_sha256"], original_hash)
        self.assertTrue(current["authority_updates"])
        self.assertTrue(alias.is_file())
        self.assertFalse(controller.source_current(first))
        # Replaying the older alias after the new snapshot cannot roll the
        # canonical generation back or hide its withdrawal.
        with patch.object(mining, "SCANNER_VERSION", "alias-replay-generation"):
            mining.discover(self.policy, self.artifacts)
            self.assertTrue(mining.verify_episode(self.path)["authority_updates"])
            self.assertFalse(controller.source_current(first))
        self.source.unlink()
        with self.assertRaises(ValueError):
            mining.verify_episode(self.path)

    def test_modified_fallback_copy_cannot_preserve_active_lesson(self):
        controller, first, alias = self.active_alias_fixture(preexisting_alias=True)
        Path(mining.verify_episode(self.path)["source_original"]["source_path"]).unlink()
        alias.write_bytes(alias.read_bytes() + b"\n")
        self.assertFalse(controller.source_current(first))
        with self.assertRaises(ValueError):
            mining.verify_episode(self.path)

    def test_multi_lesson_provenance_rejects_manual_and_managed_edits(self):
        controller, first, second, _ = self.two_lesson_controller()
        original = self.target.read_text()
        self.target.write_text(original + "Unrelated manual guidance.\n")
        self.assertFalse(controller.source_current(first))
        self.assertFalse(controller.source_current(second))
        self.target.write_text(original.replace(second["instruction"], "Modified managed instruction."))
        self.assertFalse(controller.source_current(first))
        self.assertFalse(controller.source_current(second))
        self.target.write_text(original + "\n" + controller.block(second) + "\n")
        self.assertFalse(controller.source_current(first))
        self.target.write_bytes(original.replace("\n", "\r\n").encode())
        self.assertFalse(controller.source_current(first))

    def test_owned_exclusions_still_compare_raw_hashes_after_redaction(self):
        self.target.write_text("Ordinary guidance.\npassword=" + "synthetic-first" + "\n")
        controller, first, second, _ = self.two_lesson_controller()
        before = interpretation.redact_text(self.target.read_text())
        self.target.write_text(self.target.read_text().replace("synthetic-first", "synthetic-other"))
        self.assertEqual(interpretation.redact_text(self.target.read_text()), before)
        self.assertFalse(controller.source_current(first))
        self.assertFalse(controller.source_current(second))

    def test_author_prompt_recognizes_only_verified_paired_human_answers(self):
        prompt = interpretation._author_prompt(self.episode, interpretation._context(self.policy))
        self.assertIn("Verified native human_answer turns paired to an interactive question", prompt)
        self.assertIn("automated or unpaired tool results never supply human approval", prompt)
        self.assertIn("It grants no current authority", prompt)

    def test_selected_episode_does_not_load_or_mutate_unselected_stale_files(self):
        unselected = self.path.parent / ("IE-" + "f" * 24 + ".json")
        unselected.write_text("not a valid episode object")
        before = {path: path.read_bytes() for path in self.path.parent.glob("IE-*.json") if path != self.path}
        with patch.object(interpretation, "provider_fingerprints", return_value=self.metadata), \
                patch.object(interpretation, "run_analysis", side_effect=self.invoke):
            result = interpretation.interpret(self.policy, self.artifacts, self.root / "work", self.providers,
                                              episode_ids=[self.path.stem])
        self.assertEqual(result["episode_ids"], [self.path.stem])
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["calls"], 4)
        self.assertEqual([candidate["episode_id"] for candidate in result["candidates"]], [self.path.stem])
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        ledger = json.loads((self.artifact().parent / "budget.json").read_text())
        self.assertEqual(ledger["calls"], 4)

    def test_episode_selection_rejects_invalid_duplicate_missing_and_symlink_ids(self):
        redirected_id = "IE-" + "e" * 24
        (self.path.parent / (redirected_id + ".json")).symlink_to(self.path)
        selections = ([], [self.path.stem, self.path.stem], ["../" + self.path.stem],
                      [self.path.stem + ".json"], ["IE-" + "0" * 24], [redirected_id],
                      [None], self.path.stem, ["IE-" + f"{index:024x}" for index in range(33)])
        before = self.path.read_bytes()
        with patch.object(interpretation, "provider_fingerprints", side_effect=AssertionError("model probe")), \
                patch.object(interpretation, "run_analysis", side_effect=AssertionError("model call")):
            for selected in selections:
                with self.subTest(selection=selected), self.assertRaises(ValueError):
                    interpretation.interpret(self.policy, self.artifacts, self.root / "work", self.providers,
                                             episode_ids=selected)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.artifact().parent / "budget.json").exists())

    def test_selected_episode_keeps_state_cooldown_and_budget_gates(self):
        original = json.loads(self.path.read_text())
        with patch.object(interpretation, "provider_fingerprints", side_effect=AssertionError("model probe")), \
                patch.object(interpretation, "run_analysis", side_effect=AssertionError("model call")):
            for state in ("negative_sample", "interpreted", "pending_retry"):
                self.path.write_text(json.dumps(original | {"state": state, "retry_after": 9999999999}))
                before = self.path.read_bytes()
                result = interpretation.interpret(self.policy, self.artifacts, self.root / "work", self.providers,
                                                  episode_ids=[self.path.stem])
                self.assertEqual((result["processed"], result["calls"]), (0, 0))
                self.assertEqual(self.path.read_bytes(), before)
            self.path.write_text(json.dumps(original))
            self.policy["two_pass"]["max_calls_per_day"] = 3
            result = interpretation.interpret(self.policy, self.artifacts, self.root / "work", self.providers,
                                              episode_ids=[self.path.stem])
            self.assertEqual((result["processed"], result["calls"]), (0, 0))

    def test_selected_privacy_hold_never_consumes_calls_or_requeues_evidence(self):
        held = json.loads(self.path.read_text())
        held["privacy_hold"] = True
        context = held["context"]
        for view in [*context["previous"], context["intervention"], *context["following"]]:
            view.update(text="", truncated=True)
        with patch.object(interpretation, "provider_fingerprints", side_effect=AssertionError("model probe")), \
                patch.object(interpretation, "run_analysis", side_effect=AssertionError("model call")):
            for state in ("needs_review", "pending_interpretation", "pending_retry"):
                held["state"] = state
                self.path.write_text(json.dumps(held))
                before = self.path.read_bytes()
                result = interpretation.interpret(self.policy, self.artifacts, self.root / "work", self.providers,
                                                  episode_ids=[self.path.stem])
                self.assertEqual((result["processed"], result["calls"]), (0, 0))
                self.assertEqual(result["candidates"], [])
                self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.artifact().parent / "budget.json").exists())

    def test_privacy_held_source_blocks_existing_accepted_candidate_and_lesson(self):
        controller = self.controller()
        self.run_interpret()
        controller.import_interpretations()
        lesson = controller.records()[0]
        accepted = json.loads(self.artifact().read_text())
        self.assertEqual(json.loads(accepted["critic"]["output"])["verdict"], "accept")
        held = json.loads(self.path.read_text())
        held.update(privacy_hold=True, state="needs_review")
        context = held["context"]
        for view in [*context["previous"], context["intervention"], *context["following"]]:
            view.update(text="", truncated=True)
        self.path.write_text(json.dumps(held))
        verified = mining.verify_episode(self.path)
        self.assertTrue(verified["verified"])
        self.assertIsNone(interpretation._candidate(accepted, self.artifact(), self.policy, verified))
        self.assertIsNone(interpretation.verified_candidate(self.artifact(), self.policy))
        self.assertEqual(interpretation.verified_candidates(self.policy, self.artifacts), [])
        self.assertFalse(controller.source_current(lesson))

    def test_cli_repeated_episode_selection_uses_native_fixture_and_scoped_import(self):
        import twill_learning
        self.controller()
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps(self.policy))
        other = next(path.stem for path in self.path.parent.glob("IE-*.json") if path != self.path)
        output = io.StringIO()
        with patch.object(interpretation, "provider_fingerprints", return_value=self.metadata), \
                patch.object(interpretation, "run_analysis", side_effect=self.invoke), \
                patch.object(interpretation, "verified_candidates", side_effect=AssertionError("unselected import")), \
                contextlib.redirect_stdout(output):
            code = twill_learning.main(["--policy", str(policy_path), "interpret", "--episode", self.path.stem,
                                        "--episode", other, "--limit", "1", "--json"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())["data"]
        self.assertEqual(result["episode_ids"], [self.path.stem, other])
        self.assertEqual(result["calls"], 4)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["candidates"][0]["episode_id"], self.path.stem)
        self.assertNotIn("instruction", result["candidates"][0])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            twill_learning.main(["interpret", "--help"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("--episode", output.getvalue())

    def run_scripted_interpretation(self, authors, critics):
        outputs = {"claude": iter(authors), "codex": iter(critics)}
        prompts = []
        def invoke(prompt, **kwargs):
            provider = kwargs["provider"]
            ledger = json.loads((self.artifact().parent / "budget.json").read_text())
            prompts.append((provider, prompt, ledger["calls"]))
            return {"status": "ok", "provider": provider, "context_verified": True,
                    "actual_model": self.providers[provider]["model"],
                    "model_fingerprint": self.metadata[provider]["fingerprint"],
                    "output": json.dumps(next(outputs[provider])), "calls": 1}
        with patch.object(interpretation, "provider_fingerprints", return_value=self.metadata), \
                patch.object(interpretation, "run_analysis", side_effect=invoke):
            result = interpretation.interpret(self.policy, self.artifacts, self.root / "work", self.providers,
                                              episode_ids=[self.path.stem])
        return result, json.loads(self.artifact().read_text()), prompts

    def rejected_pair(self):
        author = copy.deepcopy(self.author)
        author["durable_intent"]["kind"] = "inferred"
        author["confidence"] = "medium"
        author["what_failed"]["text"] = "Review relied only on automation."
        critic = copy.deepcopy(self.critic)
        critic.update(verdict="reject", supported_claims=[],
                      issues=["UNTRUSTED_REVIEW_SENTINEL: no evidence establishes automated-only review."])
        return author, critic

    def test_one_refinement_can_narrow_grounded_hypothesis_without_claiming_benefit(self):
        first_author, first_critic = self.rejected_pair()
        revised = copy.deepcopy(self.author)
        revised["durable_intent"]["kind"] = "inferred"
        revised["instruction"] = "Continue authorized work after status answers."
        result, record, prompts = self.run_scripted_interpretation([first_author, revised], [first_critic, self.critic])
        self.assertEqual(result["calls"], 6)
        self.assertEqual(result["daily_calls"], 6)
        self.assertEqual([call[2] for call in prompts], [3, 4, 5, 6])
        self.assertEqual(result["candidates"][0]["kind"], "needs_improvement")
        self.assertEqual(result["candidates"][0]["assessment"], "needs_evaluation")
        self.assertEqual(result["candidates"][0]["observed_outcome"]["status"], "UNKNOWN")
        self.assertEqual(len(record["attempts"]), 2)
        self.assertEqual(json.loads(record["attempts"][0]["author"]["output"]), first_author)
        self.assertEqual(json.loads(record["attempts"][0]["critic"]["output"]), first_critic)
        self.assertEqual(record["author"], record["attempts"][1]["author"])
        self.assertEqual(record["critic"], record["attempts"][1]["critic"])
        self.assertIn("UNTRUSTED MODEL FEEDBACK", prompts[2][1])
        self.assertIn("UNTRUSTED_REVIEW_SENTINEL", prompts[2][1])
        self.assertIn(self.human, prompts[2][1])
        self.assertNotIn("UNTRUSTED_REVIEW_SENTINEL", prompts[3][1])
        self.assertNotIn("previous_critic", prompts[3][1])
        self.assertIn(self.human, prompts[3][1])
        self.assertIsNotNone(interpretation.verified_candidate(self.artifact(), self.policy))

    def test_refinement_still_rejected_stays_held_after_exactly_two_pairs(self):
        author, critic = self.rejected_pair()
        result, record, prompts = self.run_scripted_interpretation([author, author], [critic, critic])
        self.assertEqual(result["calls"], 6)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(record["state"], "needs_review")
        self.assertEqual(len(record["attempts"]), 2)
        self.assertEqual(len(prompts), 4)
        self.assertIsNone(interpretation.verified_candidate(self.artifact(), self.policy))

    def test_medium_confidence_alone_can_request_one_refinement(self):
        author = copy.deepcopy(self.author)
        author["confidence"] = "medium"
        result, record, _ = self.run_scripted_interpretation([author, self.author], [self.critic, self.critic])
        self.assertEqual(result["calls"], 6)
        self.assertEqual(len(record["attempts"]), 2)
        self.assertEqual(len(result["candidates"]), 1)

    def test_uncertain_explicit_critic_can_request_one_refinement(self):
        critic = copy.deepcopy(self.critic)
        critic["confidence"] = "medium"
        result, record, _ = self.run_scripted_interpretation([self.author, self.author], [critic, self.critic])
        self.assertEqual(result["calls"], 6)
        self.assertEqual(len(record["attempts"]), 2)
        self.assertEqual(len(result["candidates"]), 1)

    def test_accepted_low_confidence_inferred_proposal_is_held_without_confidence_chasing(self):
        author = copy.deepcopy(self.author)
        author["durable_intent"]["kind"] = "inferred"
        author["confidence"] = "low"
        result, record, _ = self.run_scripted_interpretation([author], [self.critic])
        self.assertEqual(result["calls"], 4)
        self.assertEqual(len(record["attempts"]), 1)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(record["state"], "needs_review")

    def test_refinement_requires_two_remaining_cycle_and_daily_slots(self):
        author, critic = self.rejected_pair()
        for budget in ("max_calls_per_cycle", "max_calls_per_day"):
            with self.subTest(budget=budget):
                self.policy["two_pass"][budget] = 5
                result, record, prompts = self.run_scripted_interpretation([author], [critic])
                self.assertEqual(result["calls"], 4)
                self.assertEqual(len(record["attempts"]), 1)
                self.assertEqual(len(prompts), 2)
                self.assertEqual(result["candidates"], [])
                # Reset synthetic files and the shared ledger for the next
                # independent budget case, never real enrollment state.
                payload = json.loads(self.path.read_text())
                payload["state"] = "pending_interpretation"
                self.path.write_text(json.dumps(payload))
                (self.artifact().parent / "budget.json").unlink()
                self.policy["two_pass"].pop(budget)

    def test_task_only_does_not_trigger_refinement(self):
        author = copy.deepcopy(self.author)
        author.update(disposition="task_only", instruction="")
        critic = copy.deepcopy(self.critic)
        critic.update(disposition="task_only", verdict="reject", supported_claims=[], issues=["Ordinary request."])
        result, record, prompts = self.run_scripted_interpretation([author], [critic])
        self.assertEqual(result["calls"], 4)
        self.assertEqual(len(record["attempts"]), 1)
        self.assertEqual(len(prompts), 2)

    def test_invalid_refinement_preserves_first_pair_without_reusing_its_critic(self):
        author, critic = self.rejected_pair()
        invalid = copy.deepcopy(self.author)
        invalid["human_wanted"]["citations"][0]["quote"] = "Fabricated source quote."
        result, record, prompts = self.run_scripted_interpretation([author, invalid], [critic])
        self.assertEqual(result["calls"], 5)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(record["state"], "pending_retry")
        self.assertEqual(len(record["attempts"]), 2)
        self.assertEqual(record["critic"], {})
        self.assertEqual(json.loads(record["attempts"][0]["critic"]["output"]), critic)
        self.assertEqual(len(prompts), 3)


class NativeAnalysisIsolationTests(unittest.TestCase):
    def test_one_shot_uses_native_isolation_marker_metadata_and_no_test_oracles(self):
        with tempfile.TemporaryDirectory(prefix="analysis-test-") as directory:
            root = Path(directory)
            calls = []
            original = tempfile.TemporaryDirectory
            def temporary(*args, **kwargs):
                if kwargs.get("dir") == "/tmp":
                    kwargs["dir"] = root
                return original(*args, **kwargs)
            def invoke(command, *, cwd, env, prompt, timeout, control):
                calls.append(command)
                provider = command[0]
                self.assertIn("exact context marker from the loaded instruction file", prompt)
                self.assertIn("BEFORE the requested JSON", prompt)
                self.assertIn("answer AFTER this mandatory marker", prompt)
                requested = prompt.split("ANALYSIS REQUEST:\n", 1)[1]
                self.assertTrue(requested == "Analyze historical evidence." or len(requested) == 128 * 1024 + 1)
                self.assertNotIn("PYTHONPATH", env)
                guidance = (cwd / ("CLAUDE.md" if provider == "claude" else "AGENTS.md")).read_text()
                marker = re.search(r"print exactly (TWILL_CONTEXT_[a-f0-9]+) on", guidance)[1]
                self.assertNotIn(marker, prompt)
                self.assertEqual(guidance.count(marker), 1)
                answer = marker + '\n{"result":"UNKNOWN"}'
                row = ({"type": "result", "result": answer, "modelUsage": {"fixture-claude": {}}} if provider == "claude"
                       else {"type": "item.completed", "item": {"type": "agent_message", "text": answer}})
                return {"returncode": 0, "error": None, "stdout": json.dumps(row), "stderr": "", "seconds": 0.01}
            with patch.object(experiments.tempfile, "TemporaryDirectory", side_effect=temporary), \
                    patch.object(experiments, "_run_process", side_effect=invoke):
                for provider in ("claude", "codex"):
                    model = "fixture-" + provider
                    result = experiments.run_analysis("Analyze historical evidence.", work_root=root / "work", provider=provider,
                        config={"model": model}, metadata={"model": model, "version": "fixture-1",
                        "fingerprint": experiments._fingerprint(model, "fixture-1")})
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(result["calls"], 1)
                    self.assertEqual(json.loads(result["output"]), {"result": "UNKNOWN"})
                model = "fixture-codex"
                result = experiments.run_analysis("X" * (128 * 1024 + 1), work_root=root / "work", provider="codex",
                    config={"model": model}, metadata={"model": model, "version": "fixture-1",
                    "fingerprint": experiments._fingerprint(model, "fixture-1")})
                self.assertEqual(result["status"], "ok")
                with self.assertRaises(ValueError):
                    experiments.run_analysis("X" * (experiments.MAX_ANALYSIS_PROMPT + 1), work_root=root / "work",
                                             provider="codex", config={"model": model})
            self.assertIn("--safe-mode", calls[0])
            self.assertEqual(calls[0][calls[0].index("--tools") + 1], "")
            self.assertIn("features.shell_tool=false", calls[1])
            self.assertIn("apps._default.enabled=false", calls[1])
            self.assertIn("--ignore-user-config", calls[1])
            self.assertEqual(list((root / "work").iterdir()), [])
