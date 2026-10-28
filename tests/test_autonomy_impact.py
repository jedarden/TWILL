"""Phase 9E raw-source coverage, private impact, and replay scenarios."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_artifacts import read_autonomy_manifest  # noqa: E402
from twill_autonomy_impact import summarize  # noqa: E402
import twill_autonomy  # noqa: E402


NOW = datetime.now(timezone.utc).replace(microsecond=0)
STAMP = (NOW - timedelta(days=3)).isoformat()


def event(kind, payload, session):
    return {"type": kind, "timestamp": STAMP,
            "payload": {**payload, "session_id": session}}


def user(text, session):
    return event("response_item", {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": text}]}, session)


def reply(text, session, terminal=True):
    result = [event("response_item", {"type": "message", "role": "assistant",
                     "id": f"reply-{session}", "phase": "final_answer",
                     "content": [{"type": "output_text", "text": text}]}, session)]
    if terminal:
        result.append(event("event_msg", {"type": "task_complete"}, session))
    return result


class AutonomyImpactCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.artifacts = self.root / "private-artifacts"
        self.artifacts.mkdir()
        self.state = self.root / "state"
        config = self.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(f'artifacts_root = "{self.artifacts}"\n'
                          'retention = "3650d"\nsettle_window = "0"\n')
        remote = self.root / "origin.git"
        subprocess.run(("git", "init", "--bare", "-q", str(remote)), check=True)
        (self.artifacts / "README.md").write_text("Synthetic private test data\n")
        for args in (("init", "-q", "-b", "main"), ("add", "README.md"),
                     ("-c", "user.name=test", "-c", "user.email=test@example.test",
                      "commit", "-qm", "initial"),
                     ("remote", "add", "origin", str(remote)),
                     ("push", "-q", "-u", "origin", "HEAD:main")):
            subprocess.run(("git", *args), cwd=self.artifacts, check=True,
                           capture_output=True)

    def cli(self, command, *args):
        completed = subprocess.run(
            (sys.executable, str(ROOT / "twill"), command, *args,
             "--state-dir", str(self.state), "--json"), cwd=ROOT,
            env={**os.environ, "HOME": str(self.home)}, text=True,
            capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)["data"]

    def ingest(self, name, rows):
        path = self.root / f"{name}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.cli("ingest", "--file", str(path), "--settle", "0")
        return path

    def test_scenarios_five_to_seven_and_observational_measurement(self):
        # One explicit answer inside a tool result triggers two families for
        # one execution opportunity. The status question retains the objective.
        overlap = [user("Build report", "overlap"),
                   event("response_item", {"type": "custom_tool_call",
                         "name": "functions.request_user_input", "call_id": "approve-1",
                         "input": "May I build report?"}, "overlap"),
                   event("response_item", {"type": "custom_tool_call_output",
                         "call_id": "approve-1", "output": "Yes"}, "overlap"),
                   user("Status?", "overlap"), *reply(
                       "I can build report after another approval", "overlap")]
        self.ingest("overlap", overlap)
        self.ingest("same", [user("Build report", "same"),
                             *reply("I can build report after another approval", "same")])
        self.ingest("advice", [user("How should I build report?", "advice"),
                               *reply("I can build report after another approval", "advice")])
        self.ingest("hold", [user("Build report", "hold"),
                             user("Do not proceed until approval", "hold"),
                             *reply("I can build report after another approval", "hold")])
        self.ingest("unknown", [user("Build report", "unknown"),
                                *reply("I can build report after another approval", "unknown", False)])
        self.cli("detect")
        inspected = self.cli("autonomy")["candidates"]
        self.assertTrue({"D-12", "D-13"} <= {item["family"] for item in inspected
                                                  if item["disposition"] == "supported"})
        self.assertFalse(any(item["disposition"] == "supported" for item in inspected
                             if item["source_path"].endswith(("advice.jsonl", "hold.jsonl",
                                                             "unknown.jsonl"))))
        supported = next(item for item in inspected if item["family"] == "D-12"
                         and item["source_path"].endswith("overlap.jsonl"))
        self.cli("autonomy", "review", supported["candidate_id"],
                 "--disposition", "supported", "--reason", "Captured authority")
        self.cli("rank")
        self.assertIn("pending_action", self.cli("explain", "--dry-run")["prompt"])
        intervention_at = (NOW - timedelta(days=2)).isoformat()
        self.cli("autonomy", "intervention", supported["candidate_id"],
                 "--at", intervention_at, "--reason", "Synthetic dated change")
        impact = self.cli("measure")["autonomy_impact"]
        self.assertEqual(impact["schema"], "twill-autonomy-impact/v1")
        self.assertEqual(impact["supported_interventions"], 2)
        self.assertEqual(len(impact["interventions"]), 1)
        self.assertEqual(impact["interventions"][0]["comparison"],
                         "observational; no causal attribution")
        self.assertFalse(impact["interventions"][0]["after_complete"])
        self.assertFalse(impact["interventions"][0]["comparable"])
        self.assertIsNone(impact["telemetry"]["exchange_cost_usd"])
        self.assertIsNone(impact["telemetry"]["exchange_tokens"])
        self.assertGreaterEqual(impact["telemetry"]["exchange_elapsed_seconds"]["observed"], 1)
        d12 = next(group for group in impact["groups"] if group["family"] == "D-12")
        self.assertGreaterEqual(d12["eligible_opportunities"], 2)
        self.assertGreaterEqual(d12["terminal_unknown"], 1)
        self.assertGreaterEqual(d12["unknown_unassessed"], 1)
        self.assertGreaterEqual(d12["explicit_approval_exchanges"], 1)
        self.assertIsNone(d12["supported_rate"])
        self.assertEqual(d12["adjudicated_precision"], 1.0)
        self.assertEqual(impact["source_coverage"][0]["source"], "codex")
        self.assertEqual(impact["source_coverage"][0]["sessions"], 5)
        path = self.artifacts / "autonomy" / "impact" / f"{NOW.date()}.json"
        before = path.read_bytes()
        with sqlite3.connect(self.state / "twill.db") as db:
            empty = summarize(db, self.artifacts, "2000-01-01T00:00:00+00:00",
                              "2000-01-08T00:00:00+00:00")
        self.assertEqual(empty["groups"], [])
        self.assertEqual(empty["supported_interventions"], 0)
        self.cli("detect")
        self.cli("measure")
        replay = json.loads(path.read_bytes())
        original = json.loads(before)
        replay.pop("measured_at")
        original.pop("measured_at")
        replay.pop("window_start")
        original.pop("window_start")
        replay.pop("window_end")
        original.pop("window_end")
        self.assertEqual(replay, original)
        read_autonomy_manifest(self.artifacts)
        self.assertTrue((self.artifacts / "autonomy" / "interventions.jsonl").is_file())
        week = (NOW - timedelta(days=3)).strftime("%G-W%V")
        self.assertIn("autonomy_impact", self.cli("digest", "--week", week))
        self.assertTrue(self.cli("trend")["autonomy_weeks"])

    def test_reversal_and_version_drift_remove_supported_membership(self):
        source = self.ingest("drift", [user("Build report", "drift"),
                            *reply("I can build report after another approval", "drift")])
        self.cli("detect")
        candidate = next(item for item in self.cli("autonomy")["candidates"]
                         if item["family"] == "D-12")
        self.cli("autonomy", "review", candidate["candidate_id"],
                 "--disposition", "supported", "--reason", "Synthetic support")
        self.assertEqual(self.cli("measure")["autonomy_impact"]["supported_interventions"], 1)
        with sqlite3.connect(self.state / "twill.db") as db:
            with mock.patch.object(twill_autonomy, "DETECTOR_VERSION", 3):
                versioned = summarize(db, self.artifacts,
                                      (NOW - timedelta(days=7)).isoformat(), NOW.isoformat())
        self.assertEqual(versioned["supported_interventions"], 0)
        self.assertTrue(any(not group["current_version"] for group in versioned["groups"]))
        self.cli("autonomy", "review", candidate["candidate_id"],
                 "--disposition", "dismissed", "--reason", "Synthetic reversal")
        reversed_impact = self.cli("measure")["autonomy_impact"]
        self.assertEqual(reversed_impact["supported_interventions"], 0)
        self.assertEqual(next(group for group in reversed_impact["groups"]
                              if group["family"] == "D-12")["adjudicated_precision"], 0.0)
        with source.open("a") as handle:
            handle.write(json.dumps(user("Status?", "drift")) + "\n")
        self.cli("ingest", "--file", str(source), "--settle", "0")
        with sqlite3.connect(self.state / "twill.db") as db:
            invalidated = summarize(db, self.artifacts,
                                    (NOW - timedelta(days=7)).isoformat(), NOW.isoformat())
        self.assertEqual(invalidated["supported_interventions"], 0)
        self.cli("detect")
        changed = self.cli("measure")["autonomy_impact"]
        self.assertEqual(changed["supported_interventions"], 0)
        self.assertTrue(any(group["unknown_unassessed"] for group in changed["groups"]))
