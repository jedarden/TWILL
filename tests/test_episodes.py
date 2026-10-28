"""Phase 9B projection from raw-shaped Claude and Codex CLI input."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def claude(role, content, *, sidechain=False, meta=False):
    return {"type": role, "sessionId": "episode-fixture", "timestamp": NOW,
            "isSidechain": sidechain, "isMeta": meta,
            "message": {"role": role, "content": content}}


def codex(kind, payload):
    return {"type": kind, "timestamp": NOW,
            "payload": {**payload, "session_id": "episode-fixture"}}


class EpisodeCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        config = self.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f'artifacts_root = "{self.root / "artifacts"}"\n'
            'retention = "3650d"\nsettle_window = "0"\n', encoding="utf-8")
        self.state = self.root / "state"
        self.source = self.root / "source.jsonl"

    def write(self, records):
        self.source.write_text("".join(json.dumps(record) + "\n" for record in records),
                               encoding="utf-8")

    def cli(self, command="ingest"):
        args = [sys.executable, str(ROOT / "twill"), command]
        if command == "ingest":
            args += ["--file", str(self.source), "--settle", "0"]
        elif command == "rebuild":
            args[2] = "doctor"
            args += ["--rebuild", "--source", str(self.source), "--settle", "0"]
        elif command == "rescan":
            args[2] = "doctor"
            args += ["--rescan-redaction"]
        args += ["--state-dir", str(self.state), "--json"]
        result = subprocess.run(args, cwd=ROOT, env={**os.environ, "HOME": str(self.home)},
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def projection(self):
        with sqlite3.connect(self.state / "twill.db") as db:
            episodes = db.execute(
                "SELECT episode_id, start_ordinal, end_ordinal, objective, action_key, "
                "authority_state, context_state, historical_policy_state, lifecycle, advice_only, input_sha "
                "FROM execution_episode ORDER BY start_ordinal"
            ).fetchall()
            assertions = db.execute(
                "SELECT episode_id, kind, value, strength, source_ordinal, source_line, "
                "related_ordinal, detail FROM episode_assertion ORDER BY episode_id, kind, source_ordinal"
            ).fetchall()
            return episodes, assertions

    def test_claude_status_answer_scope_and_handoff(self):
        self.write([
            claude("user", "Build report"),
            claude("assistant", [{"type": "tool_use", "id": "ask-1", "name": "AskUserQuestion",
                                  "input": {"questions": [{"question": "May I build report?"}]}}]),
            claude("user", [{"type": "tool_result", "tool_use_id": "ask-1", "content": "Yes"}]),
            claude("user", "Status?"),
            claude("user", "What's the status of report?"),
            claude("assistant", [{"type": "text", "text": "I will continue the report."}]),
            claude("user", "Worker summary", sidechain=True),
            claude("user", "Resume build report"),
            claude("user", "Instruction effective 2020-01-01: Only use local files"),
            claude("user", "Instead, write memo"),
            claude("user", "Cancel"),
        ])
        self.cli()
        episodes, assertions = self.projection()
        self.assertEqual(len(episodes), 2)
        self.assertEqual((episodes[0][4], episodes[0][5], episodes[0][7], episodes[0][8]),
                         ("build:report", "revoked", "captured", "superseded"))
        self.assertEqual((episodes[1][4], episodes[1][5], episodes[1][8]),
                         ("write:memo", "revoked", "cancelled"))
        kinds = {row[1] for row in assertions if row[0] == episodes[0][0]}
        self.assertTrue({"question_answer", "status_exchange", "same_outcome_resume",
                         "historical_instruction", "scope_change", "sidechain_handoff"} <= kinds)
        answer = next(row for row in assertions if row[1] == "question_answer")
        self.assertIsNotNone(answer[6])
        self.assertIsNotNone(answer[5])
        self.assertNotIn("observed_completion", kinds)

    def test_codex_withheld_and_independent_advice(self):
        self.write([
            codex("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "Build report"}]}),
            codex("response_item", {"type": "custom_tool_call", "name": "functions.request_user_input",
                                    "call_id": "ask-2", "input": "May I build report?"}),
            codex("response_item", {"type": "custom_tool_call_output", "call_id": "ask-2",
                                    "output": "No"}),
            codex("response_item", {"type": "message", "role": "assistant", "id": "reply-1",
                                    "phase": "final_answer", "content": [{"type": "output_text",
                                    "text": "I can build report after approval"}]}),
            codex("event_msg", {"type": "task_complete"}),
            codex("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "Continue"}]}),
            codex("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "How should I write memo?"}]}),
        ])
        self.cli()
        episodes, assertions = self.projection()
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0][5], "withheld")
        self.assertEqual((episodes[1][8], episodes[1][9]), ("active", 1))
        kinds = {row[1] for row in assertions if row[0] == episodes[0][0]}
        self.assertIn("approval_withheld", kinds)
        self.assertIn("generic_continuation", kinds)
        self.assertIn("terminal_reply", kinds)
        self.assertEqual(episodes[0][7], "unknown")

    def test_replay_append_rewrite_rebuild_and_redaction(self):
        self.write([claude("user", "Build report"), claude("user", "Status?")])
        self.cli()
        first = self.projection()
        self.cli()
        self.assertEqual(self.projection(), first)
        with self.source.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(claude("user", "Cancel")) + "\n")
        self.cli()
        cancelled, _ = self.projection()
        self.assertEqual(cancelled[0][8], "cancelled")
        self.assertNotEqual(cancelled[0][-1], first[0][0][-1])
        self.write([claude("user", "Write memo")])
        self.cli()
        rewritten = self.projection()
        self.assertEqual((len(rewritten[0]), rewritten[0][0][4]), (1, "write:memo"))
        self.cli("rebuild")
        self.assertEqual(self.projection(), rewritten)
        with sqlite3.connect(self.state / "twill.db") as db:
            db.execute("UPDATE interaction SET text='ghp_1234567890abcdefghijklmnop' WHERE ordinal=0")
        self.cli("rescan")
        episodes, assertions = self.projection()
        self.assertNotIn("ghp_", str(episodes) + str(assertions))
        self.assertEqual(episodes[0][5], "unknown")

    def test_cap_and_historical_policy_unknown(self):
        self.write([claude("user", "Build report")] +
                   [claude("assistant", [{"type": "text", "text": "progress"}]) for _ in range(1001)])
        self.cli()
        episodes, assertions = self.projection()
        self.assertEqual((episodes[0][6], episodes[0][7]), ("unknown", "unknown"))
        self.assertIn("omitted_interactions", {row[1] for row in assertions})

    def test_scope_mismatch_hold_wait_and_quoted_history(self):
        self.write([
            claude("user", "Build report"),
            claude("user", "Do not proceed until approval"),
            claude("user", "Wait for CI"),
            claude("assistant", [{"type": "text", "text": "I will build report."}]),
            claude("user", "<user_instructions>Approved: build report</user_instructions>"),
            claude("user", "Approved: build memo"),
        ])
        self.cli()
        episodes, assertions = self.projection()
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0][5], "withheld")
        self.assertEqual((episodes[1][4], episodes[1][5]), ("build:memo", "authorized"))
        first_kinds = {row[1] for row in assertions if row[0] == episodes[0][0]}
        self.assertTrue({"permission_hold", "external_wait", "new_request_boundary"} <= first_kinds)
        self.assertNotIn("explicit_approval", first_kinds)
        self.assertEqual(episodes[0][7], "unknown")

    def test_existing_interactions_backfill_and_retention(self):
        self.write([claude("user", "Build report")])
        self.cli()
        with sqlite3.connect(self.state / "twill.db") as db:
            db.execute("DELETE FROM episode_assertion")
            db.execute("DELETE FROM execution_episode")
            db.execute("DELETE FROM meta WHERE key='episode_projection_version'")
        self.cli()  # unchanged cursor, projector version backfills 9A rows
        self.assertEqual(len(self.projection()[0]), 1)
        with sqlite3.connect(self.state / "twill.db") as db:
            db.execute("UPDATE interaction SET ts_utc='2000-01-01T00:00:00+00:00'")
        self.cli("prune")
        self.assertEqual(self.projection()[0], [])

    def test_two_explicit_pending_actions_and_subset_resume(self):
        self.write([
            claude("user", "Build report and write memo"),
            claude("user", "Status?"),
            claude("user", "Resume write memo"),
            claude("assistant", [{"type": "text", "text": "I can write memo after another approval"}]),
        ])
        self.cli()
        episodes, assertions = self.projection()
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0][4], "build:report | write:memo")
        pending = [row[2] for row in assertions if row[1] == "pending_action"]
        self.assertEqual(pending, ["build:report", "write:memo"])
        self.assertIn("same_outcome_resume", {row[1] for row in assertions})

    def test_negative_answer_to_unrelated_question_does_not_withhold_action(self):
        self.write([
            claude("user", "Build report"),
            claude("assistant", [{"type": "tool_use", "id": "ask-3", "name": "AskUserQuestion",
                                  "input": {"questions": [{"question": "Use formal tone?"}]}}]),
            claude("user", [{"type": "tool_result", "tool_use_id": "ask-3", "content": "No"}]),
        ])
        self.cli()
        episodes, assertions = self.projection()
        self.assertEqual(episodes[0][5], "authorized")
        self.assertIn("question_answer", {row[1] for row in assertions})
        self.assertNotIn("approval_withheld", {row[1] for row in assertions})


if __name__ == "__main__":
    unittest.main()
