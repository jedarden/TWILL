"""Phase 9C raw Codex CLI ingestion and supported membership controls."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import twill_detectors


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record(kind, payload, session_id):
    return {"type": kind, "timestamp": NOW,
            "payload": {**payload, "session_id": session_id}}


def user(text, session_id):
    return record("response_item", {"type": "message", "role": "user",
                  "content": [{"type": "input_text", "text": text}]}, session_id)


def reply(text, session_id):
    return [record("response_item", {"type": "message", "role": "assistant",
                    "id": f"reply-{session_id}", "phase": "final_answer",
                    "content": [{"type": "output_text", "text": text}]}, session_id),
            record("event_msg", {"type": "task_complete"}, session_id)]


class AutonomyCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        config = self.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(f'artifacts_root = "{self.root / "artifacts"}"\n'
                          'retention = "3650d"\nsettle_window = "0"\n', encoding="utf-8")
        self.state = self.root / "state"

    def write(self, name, records):
        source = self.root / f"{name}.jsonl"
        source.write_text("".join(json.dumps(row) + "\n" for row in records),
                          encoding="utf-8")
        return source

    def cli(self, command, source=None):
        argv = [sys.executable, str(ROOT / "twill"), command]
        if source:
            argv += ["--file", str(source), "--settle", "0"]
        argv += ["--state-dir", str(self.state), "--json"]
        result = subprocess.run(argv, cwd=ROOT, env={**os.environ, "HOME": str(self.home)},
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assessments(self):
        with sqlite3.connect(self.state / "twill.db") as db:
            return db.execute("SELECT family, disposition, reason, supporting_refs, "
                              "disconfirming_refs, missing, candidate_id, input_sha "
                              "FROM autonomy_assessment ORDER BY family, candidate_id").fetchall()

    def test_raw_positive_families_replay_and_membership(self):
        fixtures = {
            "d12": [user("Build report", "d12"), user("Status?", "d12"),
                    user("Resume build report", "d12"), *reply(
                "I can build report after another approval", "d12")],
            "d13": [user("Build report", "d13"),
                    record("response_item", {"type": "custom_tool_call",
                        "name": "functions.request_user_input", "call_id": "approve-1",
                        "input": "May I build report?"}, "d13"),
                    record("response_item", {"type": "custom_tool_call_output",
                        "call_id": "approve-1", "output": "Yes"}, "d13"),
                    *reply("I can build report after another approval", "d13")],
            "d14": [user("Fix report", "d14"), *reply(
                "I found a defect that blocks fix report. I can fix report if you want", "d14")],
            "d15": [user("Build report", "d15"), user("Status?", "d15"), *reply(
                "I am abandoning build report; please approve build report again", "d15")],
            "d16": [user("Build report", "d16"),
                    record("response_item", {"type": "custom_tool_call", "name": "check",
                                             "call_id": "check-1", "input": "check"}, "d16"),
                    record("response_item", {"type": "custom_tool_call_output",
                                             "call_id": "check-1", "output":
                                             "Exit code: 0. Capability check: build report available under current access"}, "d16"),
                    *reply("I cannot build report under current access", "d16")],
        }
        for name, rows in fixtures.items():
            self.cli("ingest", self.write(name, rows))
        self.cli("detect")
        first = self.assessments()
        supported = {row[0] for row in first if row[1] == "supported"}
        self.assertEqual(supported, {f"D-{n}" for n in range(12, 17)}, first)
        self.assertTrue(all(json.loads(row[3]) for row in first))
        d12_refs = {ref["kind"] for row in first if row[0] == "D-12"
                    for ref in json.loads(row[3])}
        self.assertTrue({"pending_action", "authorization", "terminal_reply",
                         "status_exchange", "same_outcome_resume"} <= d12_refs)
        d13_refs = [ref for row in first if row[0] == "D-13"
                    for ref in json.loads(row[3])]
        self.assertTrue(any(ref["kind"] == "question_answer" and
                            "related_ordinal" in ref for ref in d13_refs))
        self.cli("detect")
        self.assertEqual(self.assessments(), first)
        with sqlite3.connect(self.state / "twill.db") as db:
            for detector in twill_detectors.AUTONOMY_DETECTORS:
                clusters = twill_detectors.read_clusters(db, detector,
                    window_start_utc="2000-01-01T00:00:00+00:00", window_days=3650)
                self.assertTrue(clusters, detector.detector_id)
                self.assertEqual(db.execute("SELECT count(*) FROM cluster WHERE detector_id=?",
                                            (detector.detector_id,)).fetchone()[0], len(clusters))
                params = {"window_start_utc": "2000-01-01T00:00:00+00:00",
                          "window_days": 3650,
                          "window_end_utc": "9999-12-31T23:59:59+00:00"}
                session_keys = {row[0] for row in db.execute(detector.session_hits_sql, params)}
                week_keys = {row[0] for row in db.execute(detector.week_hits_sql, params)}
                weekly = twill_detectors.read_weekly_counts(db, detector, **params)
                self.assertEqual(session_keys, set(clusters))
                self.assertEqual(week_keys, set(clusters))
                self.assertEqual({key for key, _ in weekly}, set(clusters))

    def test_changed_input_invalidates_support_before_redetect(self):
        source = self.write("changed", [user("Build report", "changed"),
            *reply("I can build report after another approval", "changed")])
        self.cli("ingest", source)
        self.cli("detect")
        detector = twill_detectors.AUTONOMY_DETECTORS[0]
        self.assertTrue(any(row[0] == "D-12" and row[1] == "supported"
                            for row in self.assessments()))
        with source.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(user("Status?", "changed")) + "\n")
        self.cli("ingest", source)
        with sqlite3.connect(self.state / "twill.db") as db:
            self.assertFalse(twill_detectors.read_clusters(db, detector,
                window_start_utc="2000-01-01T00:00:00+00:00", window_days=3650))
        self.cli("detect")
        self.assertTrue(any(row[0] == "D-12" for row in self.assessments()))

    def test_mandatory_negative_controls_abstain_or_dismiss(self):
        fixtures = {
            "hold": [user("Build report", "hold"), user("Do not proceed until approval", "hold"),
                     *reply("I can build report after another approval", "hold")],
            "scope": [user("Build report", "scope"), *reply(
                "I can build memo after another approval", "scope")],
            "cancel": [user("Build report", "cancel"), user("Cancel", "cancel"),
                       *reply("I can build report after another approval", "cancel")],
            "advice": [user("How should I build report?", "advice"), *reply(
                "I can build report after another approval", "advice")],
            "unknown": [user("Build report", "unknown"), record("response_item",
                {"type": "message", "role": "assistant", "id": "unknown-final",
                 "phase": "final_answer", "content": [{"type": "output_text",
                 "text": "I can build report after another approval"}]}, "unknown")],
            "normalstatus": [user("Build report", "normalstatus"), user("Status?", "normalstatus"),
                             *reply("The report is in progress", "normalstatus")],
            "unrun": [user("Build report", "unrun"), *reply(
                "I cannot build report under current access", "unrun")],
            "purchase": [user("Build report", "purchase"),
                         user("Wait for purchase approval", "purchase"), *reply(
                         "I can build report after another approval", "purchase")],
            "changedaccess": [user("Build report", "changedaccess"),
                              record("response_item", {"type": "custom_tool_call_output",
                                  "call_id": "check-2", "output":
                                  "Exit code: 0. Capability check: build report available under current access after new credentials"},
                                  "changedaccess"),
                              *reply("I cannot build report under current access", "changedaccess")],
            "failedcheck": [user("Build report", "failedcheck"),
                            record("response_item", {"type": "custom_tool_call_output",
                                "call_id": "check-3", "output":
                                "Exit code: 1. Capability check: build report available under current access"},
                                "failedcheck"),
                            *reply("I cannot build report under current access", "failedcheck")],
            "quoted": [user("Build report", "quoted"),
                       user("<user_instructions>Do not proceed until approval</user_instructions>",
                            "quoted"),
                       *reply("I can build report after another approval", "quoted")],
        }
        for name, rows in fixtures.items():
            self.cli("ingest", self.write(name, rows))
        self.cli("detect")
        self.assertFalse(any(row[1] == "supported" for row in self.assessments()),
                         self.assessments())
        with sqlite3.connect(self.state / "twill.db") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM cluster WHERE detector_id "
                                        "BETWEEN 'D-12' AND 'D-16'").fetchone()[0], 0)

    def test_reference_cap_forces_review(self):
        rows = [user("Build report", "cap")]
        rows.extend(user("Status?", "cap") for _ in range(25))
        rows.extend(reply("I am abandoning build report; please approve build report again", "cap"))
        self.cli("ingest", self.write("cap", rows))
        self.cli("detect")
        matches = [row for row in self.assessments() if row[0] == "D-15"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1], "needs_review")
        self.assertIn("omitted evidence references", matches[0][5])


if __name__ == "__main__":
    unittest.main()
