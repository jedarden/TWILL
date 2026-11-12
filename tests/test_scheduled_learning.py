"""Controlled tests for the opt-in timer proof, with no native calls or units."""

import copy
import importlib.util
import json
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from twill_learning import digest


SPEC = importlib.util.spec_from_file_location(
    "scheduled_learning_proof", Path(__file__).resolve().parents[1] / "scripts/verify-scheduled-learning.py")
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


class ScheduledProofTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        with mock.patch.object(proof.shutil, "which", side_effect=lambda name: f"/native/bin/{name}"):
            self.controller, self.policy, self.source, self.targets = proof.fixture(
                self.root, {"claude": "pinned-claude", "codex": "pinned-codex"})
        self.promote = True
        self.calls = []
        adapter = types.ModuleType("twill_experiments")
        adapter.provider_fingerprints = lambda providers: {
            name: {"fingerprint": "fixture-version"} for name in providers}

        def experiment(spec, **kwargs):
            self.calls.append(copy.deepcopy(spec))
            return {"candidate_sha": digest(spec["instruction"].encode()), "verdict": "promote",
                    "providers": {name: {"candidate_passed": self.promote,
                                         "holdout_passed": self.promote, "errors": 0,
                                         "requested_model": self.controller.providers[name]["model"],
                                         "actual_model": self.controller.providers[name]["model"],
                                         "meaningful_improvements": 2,
                                         "model_fingerprint": "fixture-version"}
                                  for name in ("claude", "codex")}}

        adapter.run_experiment = experiment
        patch = mock.patch.dict("sys.modules", {"twill_experiments": adapter})
        patch.start()
        self.addCleanup(patch.stop)

    def test_fixture_is_legacy_draft_and_scope_stays_isolated(self):
        self.assertEqual(proof.load_lesson(self.source).state, "draft")
        self.assertEqual(self.controller.records(), [])
        self.assertEqual(self.controller.providers["codex"]["model"], "pinned-codex")
        self.assertEqual({name: provider["executable"] for name, provider in self.controller.providers.items()},
                         {"claude": "/native/bin/claude", "codex": "/native/bin/codex"})
        self.assertEqual(self.controller.interval, 60)
        for path in self.targets:
            self.assertTrue(path.is_relative_to(self.root))
        self.assertEqual(self.controller.policy["bindings"][0]["suite_sha256"],
                         digest((self.root / "synthetic-independent-suite.json").read_bytes()))

    def test_cycle_only_full_proof_checks_removal_and_terminal_noop(self):
        # Unit clock acceleration; the live harness always uses the policy's 60s interval.
        self.controller.interval = 0
        phases = []

        def run(phase):
            phases.append(phase)
            self.controller.cycle()

        timer = types.SimpleNamespace(root=self.root, deadline=time.monotonic() + 10, run=run)
        result = proof.proof(self.controller, timer, self.source, self.targets)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(phases, ["import-experiment-apply", "scheduled-revalidation",
                                  "automatic-source-withdrawal", "idempotent-no-reapply"])
        self.assertIn("human_edits_preserved", result["checks"])
        self.assertEqual(proof.one_record(self.controller)["state"], "invalidated")
        self.assertEqual(proof.load_lesson(self.source).state, "accepted")

    def test_fresh_failed_revalidation_withdraws_automatically_in_cycle(self):
        initial = self.controller.cycle()
        self.assertEqual(initial["imported"], 1)
        self.assertEqual(proof.one_record(self.controller)["state"], "active")
        self.promote = False
        self.controller.interval = 0
        result = self.controller.cycle()
        record = proof.one_record(self.controller)
        self.assertEqual(result["experimented"], 1)
        self.assertEqual(record["state"], "invalidated")
        self.assertEqual(record["reason"], "candidate failed independent revalidation")
        events = [json.loads(path.read_text())["event"]["event"] for path in
                  (self.controller.store / "history" / record["id"]).glob("*.json")]
        self.assertIn("validation_failed", events)
        self.assertIn("remove_intent", events)
        for target in self.targets:
            self.assertEqual(target.read_text(), "Preserve human instructions.\n")

    def test_timer_arms_timer_and_requires_new_successful_execution(self):
        timer = proof.Timer(self.root, self.policy, time.monotonic() + 10)
        timer.last_start = 10
        finished = {"ExecMainStartTimestampMonotonic": "20", "ExecMainExitTimestampMonotonic": "30",
                    "ActiveState": "active", "SubState": "exited", "Result": "success", "ExecMainStatus": "0",
                    "TriggeredBy": timer.timer}
        with mock.patch.object(timer, "command") as command, mock.patch.object(
                timer, "properties", side_effect=[finished, finished, {"LastTriggerUSecMonotonic": "19",
                                                                      "Triggers": timer.service}]):
            result = timer.run("fixture")
        self.assertEqual(command.call_args_list, [mock.call("stop", timer.service, check=False),
                                                  mock.call("restart", timer.timer)])
        self.assertEqual(result["previous_start"], 10)
        self.assertTrue((self.root / "timer-evidence/fixture.json").is_file())

    def test_timer_failure_is_not_reported_as_success(self):
        timer = proof.Timer(self.root, self.policy, time.monotonic() + 10)
        failed = {"ExecMainStartTimestampMonotonic": "20", "ExecMainExitTimestampMonotonic": "30",
                  "ActiveState": "failed", "Result": "exit-code", "ExecMainStatus": "1",
                  "TriggeredBy": timer.timer}
        with mock.patch.object(timer, "command"), mock.patch.object(
                timer, "properties", side_effect=[failed, failed, {"LastTriggerUSecMonotonic": "19",
                                                                  "Triggers": timer.service}]):
            with self.assertRaisesRegex(proof.ProofError, "scheduled fixture failed"):
                timer.run("fixture")

    def test_unloaded_waiting_service_does_not_query_raw_timestamps(self):
        timer = proof.Timer(self.root, self.policy, time.monotonic() + 10)
        formatted = types.SimpleNamespace(stdout="ActiveState=inactive\nSubState=dead\n")
        with mock.patch.object(timer, "command", return_value=formatted), mock.patch.object(timer, "bus") as bus:
            self.assertEqual(timer.properties(timer.service, raw=False)["ActiveState"], "inactive")
        bus.assert_not_called()

    def test_formatted_systemctl_timestamps_use_raw_dbus_uint64(self):
        timer = proof.Timer(self.root, self.policy, time.monotonic() + 10)
        formatted = ("ActiveState=inactive\nExecMainStartTimestampMonotonic=1month 1w 6d 6h 40min 43.778272s\n"
                     "ExecMainExitTimestampMonotonic=1month 1w 6d 6h 40min 44s\n")
        raw_start, raw_exit = 3776918565850, 3776919565850
        with mock.patch.object(timer, "command", return_value=types.SimpleNamespace(stdout=formatted)), \
                mock.patch.object(timer, "bus", side_effect=[
                    {"type": "o", "data": ["/org/freedesktop/systemd1/unit/fixture"]},
                    {"type": "t", "data": raw_start}, {"type": "t", "data": raw_exit}]) as bus:
            result = timer.properties(timer.service)
        self.assertEqual(result["ExecMainStartTimestampMonotonic"], raw_start)
        self.assertEqual(result["ExecMainExitTimestampMonotonic"], raw_exit)
        self.assertIs(type(result["ExecMainStartTimestampMonotonic"]), int)
        self.assertEqual(bus.call_count, 3)  # The object path is cached for both properties.

    def test_dbus_timestamp_requires_unsigned_integer_type(self):
        timer = proof.Timer(self.root, self.policy, time.monotonic() + 10)
        timer.object_paths[timer.timer] = "/org/freedesktop/systemd1/unit/fixture"
        with mock.patch.object(timer, "bus", return_value={"type": "s", "data": "123"}):
            with self.assertRaisesRegex(proof.ProofError, "raw uint64"):
                timer.monotonic_property(timer.timer, "LastTriggerUSecMonotonic")

    def test_cleanup_refuses_foreign_changed_unit(self):
        timer = proof.Timer(self.root, self.policy, time.monotonic() + 10)
        path = self.root / timer.service
        path.write_text("Unrelated replacement\n")
        timer.installed = [path]
        with mock.patch.object(timer, "command"):
            with self.assertRaisesRegex(proof.ProofError, "owned unit changed"):
                timer.cleanup()
        self.assertEqual(path.read_text(), "Unrelated replacement\n")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze unavailable")
    def test_generated_units_pass_real_systemd_verification_without_install(self):
        timer = proof.Timer(self.root, self.policy, time.monotonic() + 10)
        timer.verify()
        evidence = json.loads((self.root / "unit-verification.json").read_text())
        self.assertEqual(evidence["returncode"], 0, evidence["stderr"])
        self.assertIn(f"WorkingDirectory={proof.REPO}\n", timer.contents[timer.service])
        self.assertEqual(timer.installed, [])
        self.assertFalse((timer.unit_dir / timer.service).exists())


if __name__ == "__main__":
    unittest.main()
