"""Contextual hypotheses require original native evidence and independent review."""
import copy
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

    def test_critic_must_independently_confirm_every_field(self):
        self.critic["supported_claims"].remove("scope")
        self.assertEqual(self.run_interpret()["candidates"], [])
        self.assertIn("independent confirmation", json.loads(self.artifact().read_text())["error"])

    def test_model_error_is_pending_and_cooldown_has_no_calls(self):
        with patch.object(interpretation, "provider_fingerprints", return_value=self.metadata), \
                patch.object(interpretation, "run_analysis", return_value={"status": "error", "error": "timeout"}):
            result = interpretation.interpret(self.policy, self.artifacts, self.root / "work", self.providers)
        self.assertEqual(result["calls"], 3)
        self.assertEqual(result["candidates"], [])
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
        addition = "\n" + controller.block(record) + "\n"
        self.target.write_text(self.target.read_text() + addition)
        record["application"] = {"files": [{"path": str(self.target), "addition": addition}]}
        self.assertTrue(controller.source_current(record))
        self.target.write_text("New unrelated guidance\n" + self.target.read_text())
        self.assertFalse(controller.source_current(record))


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
