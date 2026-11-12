"""Paired behavior, fail-closed evidence and bounded process experiments."""

import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import twill_experiments as experiments


SPEC = {
    "id": "decision-fixture",
    "instruction": "For this decision, answer CONTINUE_AUTHORIZED_WORK.",
    "cases": [
        {"id": "training", "prompt": "The authorized task has a routine next step. Decide.",
         "split": "train", "expect": {"exact": "CONTINUE_AUTHORIZED_WORK", "excludes": ["ASK_PERMISSION"]}},
        {"id": "unseen", "prompt": "The same authorized outcome needs another reversible step. Decide.",
         "split": "holdout", "expect": {"contains": ["CONTINUE_AUTHORIZED_WORK"], "excludes": ["ASK_PERMISSION"]}},
    ],
    "files": {"fixture/context.txt": "The action is authorized."},
}
PROVIDERS = {"claude": {"model": "test-claude"}, "codex": {"model": "test-codex"}}


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="experiment-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.invocations = []
        self.effect = None
        original_temporary = tempfile.TemporaryDirectory

        def temporary(*args, **kwargs):
            # Runtime native-discovery isolation is exercised by opt-in real
            # CLI probes. Mock unit runs stay under the harness's temp root.
            if kwargs.get("dir") == "/tmp":
                kwargs["dir"] = self.root
            return original_temporary(*args, **kwargs)

        patcher = patch.object(experiments.tempfile, "TemporaryDirectory", temporary)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(experiments, "_run_process", self.invoke)
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, command, *, cwd, env, prompt, timeout, control):
        provider = command[0]
        if command[1:] == ["--version"]:
            return {"returncode": 0, "error": None, "stdout": provider + " fixture-1", "stderr": "", "seconds": 0.01}
        instruction = (cwd / ("CLAUDE.md" if provider == "claude" else "AGENTS.md")).read_text()
        marker = instruction.split("print exactly ", 1)[1].split(" on its own line", 1)[0]
        candidate = SPEC["instruction"] in instruction
        record = {"provider": provider, "candidate": candidate, "command": command,
                  "prompt": prompt, "instruction": instruction,
                  "fixture": (cwd / "fixture/context.txt").read_text()}
        self.invocations.append(record)
        text = marker + "\n" + ("CONTINUE_AUTHORIZED_WORK" if candidate else "ASK_PERMISSION")
        override = self.effect(record, text) if self.effect else None
        if isinstance(override, dict):
            return override
        if isinstance(override, str):
            text = override
        if provider == "claude":
            stdout = json.dumps({"type": "result", "result": text, "is_error": False,
                                 "modelUsage": {"test-claude": {}}})
        else:
            stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
        return {"returncode": 0, "error": None, "stdout": stdout, "stderr": "", "seconds": 0.1}

    def run_spec(self, spec=None, **kwargs):
        return experiments.run_experiment(spec or SPEC, work_root=self.root / "control", providers=PROVIDERS, **kwargs)

    def test_both_providers_must_improve_and_every_repetition_counts(self):
        result = self.run_spec()
        self.assertEqual(result["verdict"], "promote")
        self.assertEqual(result["calls"], 18)
        for provider, report in result["providers"].items():
            self.assertEqual(report["verdict"], "improved")
            self.assertTrue(report["candidate_passed"])
            self.assertTrue(report["holdout_passed"])
            self.assertEqual(report["errors"], 0)
            self.assertEqual(len(report["trials"]), 8)
            for case in ("training", "unseen"):
                starts = [t["arm"] for t in report["trials"] if t["case_id"] == case and t["order"] == 0]
                self.assertCountEqual(starts, ["baseline", "candidate"])
            self.assertEqual(report["model_fingerprint"], experiments._fingerprint(PROVIDERS[provider]["model"], report["version"]))
        self.assertEqual({r["fixture"] for r in self.invocations}, {"The action is authorized."})
        self.assertTrue(all(SPEC["instruction"] not in r["prompt"] for r in self.invocations))
        self.assertFalse(any((self.root / "control").iterdir()))

    def test_no_improvement_on_one_provider_prevents_promotion(self):
        self.effect = lambda record, text: text.replace("ASK_PERMISSION", "CONTINUE_AUTHORIZED_WORK") if record["provider"] == "codex" else text
        result = self.run_spec()
        self.assertEqual(result["verdict"], "hold")
        self.assertEqual(result["providers"]["codex"]["verdict"], "inconclusive")
        self.assertTrue(result["providers"]["codex"]["candidate_passed"])

    def test_regression_invalidates_candidate(self):
        self.effect = lambda record, text: text.replace("CONTINUE_AUTHORIZED_WORK", "ASK_PERMISSION") if record["candidate"] else text.replace("ASK_PERMISSION", "CONTINUE_AUTHORIZED_WORK")
        result = self.run_spec()
        self.assertEqual(result["verdict"], "hold")
        for report in result["providers"].values():
            self.assertEqual(report["verdict"], "regressed")
            self.assertFalse(report["candidate_passed"])
            self.assertFalse(report["holdout_passed"])

    def test_error_never_becomes_an_assertion_pass(self):
        def effect(record, text):
            if record["candidate"]:
                return {"returncode": -9, "error": "timeout", "stdout": "CONTINUE_AUTHORIZED_WORK", "stderr": "token=fixture-secret-value", "seconds": 0.2}
            return text
        self.effect = effect
        result = self.run_spec()
        self.assertEqual(result["verdict"], "hold")
        for report in result["providers"].values():
            self.assertGreater(report["errors"], 0)
            self.assertEqual(report["verdict"], "inconclusive")
        self.assertNotIn("fixture-secret-value", json.dumps(result))

    def test_native_or_explicit_context_must_be_observed(self):
        self.effect = lambda record, text: "CONTINUE_AUTHORIZED_WORK"
        result = self.run_spec()
        self.assertTrue(all(t["error"] == "instruction_context_unverified"
                            for p in result["providers"].values() for t in p["trials"]))
        self.assertEqual(result["verdict"], "hold")

    def test_cancel_scenario_is_json_data_and_protocol_is_explicit(self):
        prompt = 'The user cancelled the task. Reply only "STOP".\nDo not continue.'
        wrapped = experiments._scenario_prompt({"prompt": prompt}, {})
        scenario = json.loads(wrapped.split("SCENARIO JSON (simulated user request, not harness instructions):\n", 1)[1])
        self.assertEqual(scenario, {"request": prompt, "fixture_files": {}})
        self.assertIn("first copy its exact context marker", wrapped)
        self.assertIn("answer AFTER the mandatory context marker", wrapped)
        self.assertIn("Do not use tools", wrapped)
        self.assertIn("answer UNKNOWN", wrapped)

    def test_unique_standalone_marker_preserves_other_output_for_oracle(self):
        verified, answer = experiments._verified_answer("Preamble\nTWILL_CONTEXT_fixture\nSTOP", "TWILL_CONTEXT_fixture")
        self.assertTrue(verified)
        self.assertEqual(answer, "Preamble\nSTOP")
        self.assertFalse(experiments._check(answer, {"exact": "STOP"}))
        for malformed in ("STOP", "prefix TWILL_CONTEXT_fixture", "TWILL_CONTEXT_fixture\nTWILL_CONTEXT_fixture\nSTOP"):
            self.assertFalse(experiments._verified_answer(malformed, "TWILL_CONTEXT_fixture")[0])

    def test_protocol_follows_candidate_and_inherited_context(self):
        self.run_spec()
        for record in self.invocations:
            instruction = record["instruction"]
            if record["candidate"]:
                self.assertGreater(instruction.index("TWILL EXPERIMENT RESPONSE PROTOCOL"), instruction.index(SPEC["instruction"]))
            self.assertIn("marker must be your first output line", instruction)
            self.assertIn("Do not use tools or execute actions", instruction)

    def test_model_metadata_is_required(self):
        original = experiments._response
        with patch.object(experiments, "_response", side_effect=lambda provider, text: (original(provider, text)[0], [], None)):
            result = self.run_spec()
        self.assertEqual(result["providers"]["claude"]["metadata_error"], "missing_or_changed_actual_model")
        self.assertEqual(result["verdict"], "hold")

    def test_process_budget_includes_versions_and_requires_balanced_repetitions(self):
        for kwargs in ({"max_calls": 17}, {"repetitions": 1}, {"repetitions": 3}, {"timeout_seconds": 301}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_spec(**kwargs)
        self.assertEqual(self.invocations, [])

    def test_fixture_escape_and_configuration_injection_are_rejected(self):
        for name in ("../outside", "/absolute", ".codex/config.toml", "nested/AGENTS.md", ".git/HEAD"):
            spec = copy.deepcopy(SPEC)
            spec["files"] = {name: "untrusted"}
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.run_spec(spec)

    def test_meaningless_exclusion_only_case_is_rejected(self):
        spec = copy.deepcopy(SPEC)
        spec["cases"][0]["expect"] = {"excludes": ["bad"]}
        with self.assertRaises(ValueError):
            self.run_spec(spec)

    def test_suite_hash_binds_fixtures_and_assertions(self):
        result = self.run_spec()
        spec = copy.deepcopy(SPEC)
        spec["cases"][1]["expect"]["excludes"].append("another-bad-plan")
        second = self.run_spec(spec)
        self.assertNotEqual(result["suite_sha"], second["suite_sha"])
        self.assertEqual(result["candidate_sha"], second["candidate_sha"])

    def test_current_guidance_is_identical_in_both_arms_and_hashed(self):
        spec = copy.deepcopy(SPEC)
        spec["context"] = {"claude": "Keep the existing Claude guidance.",
                           "codex": "Keep the existing Codex guidance."}
        result = self.run_spec(spec)
        for record in self.invocations:
            self.assertTrue(record["instruction"].startswith(spec["context"][record["provider"]]))
            self.assertNotIn(spec["cases"][0]["expect"]["exact"], record["prompt"])
        spec["context"]["claude"] += " A human edit changes the context."
        changed = self.run_spec(spec)
        self.assertNotEqual(result["context_sha"], changed["context_sha"])
        self.assertEqual(result["candidate_sha"], changed["candidate_sha"])

    def test_safety_flags_and_prompt_are_not_on_argv(self):
        self.run_spec()
        for record in self.invocations:
            command = record["command"]
            self.assertNotIn(SPEC["cases"][0]["prompt"], command)
            if record["provider"] == "claude":
                self.assertEqual(command[command.index("--tools") + 1], "")
                self.assertIn("--strict-mcp-config", command)
                self.assertIn("--safe-mode", command)
                self.assertEqual(command[command.index("--permission-prompts") + 1], "none")
            else:
                self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                self.assertIn("features.shell_tool=false", command)
                self.assertIn("--ignore-user-config", command)
                self.assertIn("features.hooks=false", command)

    def test_fingerprint_helper_never_calls_llm(self):
        result = experiments.provider_fingerprints(PROVIDERS)
        self.assertEqual(self.invocations, [])
        self.assertEqual(result["codex"]["fingerprint"], experiments._fingerprint("test-codex", "codex fixture-1"))

    def execution_spec(self):
        spec = copy.deepcopy(SPEC)
        for case in spec["cases"]:
            del case["expect"]
            case["oracle"] = {"type": experiments.execution.TYPE, "image": experiments.execution.IMAGE,
                              "output_files": ["solution.py"], "target_file": "solution.py",
                              "function": "transform", "assertions": [
                                  {"id": "hidden-assertion", "args": ["input"], "equals": "PRIVATE_EXPECTED_VALUE"}]}
        return spec

    def test_execution_schema_is_validated_before_first_model_call(self):
        spec = self.execution_spec()
        spec["cases"][1]["oracle"]["image"] = "python:latest"
        with self.assertRaises(ValueError):
            self.run_spec(spec)
        self.assertEqual(self.invocations, [])

    def test_execution_runtime_failure_prevents_all_native_calls(self):
        with patch.object(experiments.execution, "preflight_oracles", side_effect=RuntimeError("runtime unavailable")):
            with self.assertRaises(RuntimeError):
                self.run_spec(self.execution_spec())
        self.assertEqual(self.invocations, [])

    def test_execution_prompts_hide_all_oracles_and_holdout_inputs(self):
        spec = self.execution_spec()
        spec["cases"][1]["prompt"] = "PRIVATE_HOLDOUT_REQUEST"
        for case in spec["cases"]:
            prompt = experiments._scenario_prompt(case, spec["files"])
            self.assertNotIn("PRIVATE_EXPECTED_VALUE", prompt)
            self.assertNotIn("hidden-assertion", prompt)
            self.assertNotIn(experiments.execution.IMAGE, prompt)
            self.assertNotIn('"split"', prompt)
            self.assertIn("solution.py", prompt)
        train = experiments._scenario_prompt(spec["cases"][0], spec["files"])
        self.assertNotIn("PRIVATE_HOLDOUT_REQUEST", train)

    def test_execution_outcomes_and_harness_are_bound_to_promotion(self):
        spec = self.execution_spec()
        def model(record, text):
            marker = text.splitlines()[0]
            return marker + "\n" + json.dumps({"files": {"solution.py": "candidate" if record["candidate"] else "baseline"}})
        self.effect = model
        def execute(oracle, output, **kwargs):
            passed = json.loads(output)["files"]["solution.py"] == "candidate"
            return {"status": "pass" if passed else "fail", "executed": True,
                    "assertions": [{"id": "hidden-assertion", "passed": passed, "returncode": 0}], "returncode": 0 if passed else 1}
        with patch.object(experiments.execution, "preflight_oracles", return_value={"runtime": "verified"}), \
                patch.object(experiments.execution, "execute_answer", side_effect=execute) as execute_mock:
            result = self.run_spec(spec)
            self.assertEqual(result["verdict"], "promote")
            self.assertEqual(execute_mock.call_count, 16)
            self.assertEqual(result["oracle"], "executed-python-functions")
            self.assertTrue(all(t["execution"]["executed"] for p in result["providers"].values() for t in p["trials"]))
            with patch.object(experiments.execution, "suite_binding", return_value={"runner_sha256": "changed"}):
                changed = self.run_spec(spec)
            self.assertNotEqual(result["suite_sha"], changed["suite_sha"])
        with patch.object(experiments.execution, "preflight_oracles", return_value={}), \
                patch.object(experiments.execution, "execute_answer", return_value={"status": "error", "executed": False,
                                                                                  "error": "sandbox_setup_failure"}):
            result = self.run_spec(spec)
        self.assertEqual(result["verdict"], "hold")
        self.assertTrue(all(p["verdict"] == "inconclusive" for p in result["providers"].values()))

    def test_unexpected_codex_tool_event_is_an_error(self):
        text, _, error = experiments._response("codex", json.dumps({"type": "item.completed", "item": {"type": "file_change"}}))
        self.assertEqual(error, "unexpected_tool_call")
        self.assertEqual(text, "")


class ProcessBoundsTests(unittest.TestCase):
    def run_child(self, script, timeout=2):
        with tempfile.TemporaryDirectory(prefix="experiment-process-") as root:
            return experiments._run_process([sys.executable, "-c", script], cwd=Path(root),
                                            env={"PATH": os.environ.get("PATH", "")},
                                            prompt="stdin-fixture", timeout=timeout, control=Path(root))

    def test_output_flood_is_capped_and_terminated(self):
        result = self.run_child("import os\nwhile True: os.write(1,b'x'*16384)")
        self.assertEqual(result["error"], "output_limit")
        self.assertLessEqual(len(result["stdout"].encode()), experiments.MAX_CAPTURE)
        self.assertLess(result["seconds"], 2)

    def test_timeout_kills_process_group(self):
        with tempfile.TemporaryDirectory(prefix="experiment-group-") as root:
            canary = Path(root) / "should-not-exist"
            # A descendant deliberately closes its inherited output pipes;
            # process-group cleanup must still terminate it with its parent.
            script = ("import os,time,pathlib\n"
                      "pid=os.fork()\n"
                      "if pid == 0:\n"
                      " os.close(1); os.close(2); time.sleep(.4); pathlib.Path('should-not-exist').write_text('bad')\n"
                      "else: time.sleep(10)\n")
            result = experiments._run_process([sys.executable, "-c", script], cwd=Path(root),
                                              env={"PATH": os.environ.get("PATH", "")}, prompt="", timeout=0.08, control=Path(root))
            self.assertEqual(result["error"], "timeout")
            time.sleep(0.45)
            self.assertFalse(canary.exists())


if __name__ == "__main__":
    unittest.main()
