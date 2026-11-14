"""Synthetic native steering shapes through readers, private mining and CLI."""

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from codex_reader import CodexRolloutLineParser
from twill_reader import ClaudeCodeLineParser
import twill_intervention_mining as mining


ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime.now(timezone.utc).isoformat()


def claude(role, text):
    return {"type": role, "sessionId": "queued-fixture", "cwd": "/workspace/demo",
            "timestamp": STAMP, "message": {"role": role, "content": text}}


def queued(text="Actually, keep the report readable.", source="source-1", delivery="delivery-1"):
    return {"type": "attachment", "sessionId": "queued-fixture", "cwd": "/workspace/demo",
            "uuid": "attachment-1", "timestamp": STAMP,
            "attachment": {"type": "queued_command", "prompt": text,
                           "source_uuid": source, "delivery_id": delivery,
                           "commandMode": "prompt", "origin": {"kind": "human"},
                           "humanTurn": True},
            "renderedRole": "system",
            "rendered": [{"content": "Rendered reminder is a copy, not a request."}]}


def codex(text):
    return {"type": "response_item", "timestamp": STAMP,
            "payload": {"type": "message", "role": "user", "session_id": "goal-fixture",
                        "cwd": "/workspace/demo", "content": [{"type": "input_text", "text": text}]}}


def parse(parser, records):
    events = []
    for n, record in enumerate(records, 1):
        events.extend(parser.parse_line(json.dumps(record), n))
    events.extend(parser.finish())
    return events


GOAL = ('<codex_internal_context source="goal">\nContinue toward the active goal. '
        'Actually use the current worktree instead.\n</codex_internal_context>')


class NativeSteeringReaderTests(unittest.TestCase):
    def test_queued_delivery_preserves_identity_and_deduplicates_native_copies(self):
        original = queued()
        repeat = copy.deepcopy(original)
        repeat["uuid"] = "another-attachment-copy"
        same_source = queued(delivery="another-delivery")
        same_delivery = queued(source="another-source")
        records = [claude("user", "Build the report"),
                   claude("user", "[Request interrupted by user]"),
                   {"type": "queue-operation", "operation": "enqueue", "content": original["attachment"]["prompt"]},
                   {"type": "queue-operation", "operation": "remove", "reason": "absorbed_mid_turn",
                    "content": original["attachment"]["prompt"]},
                   original, repeat, same_source, same_delivery]
        parser = ClaudeCodeLineParser()
        events = parse(parser, records)
        self.assertEqual([(e.kind, e.source_line) for e in events],
                         [("interrupt", 2), ("user_turn_after_correction", 5)])
        humans = [i for i in parser.interactions if i.origin == "human_request"]
        self.assertEqual(len(humans), 2)
        self.assertEqual((humans[-1].source_line, humans[-1].message_id, humans[-1].locator),
                         (5, "source-1", "attachment/queued_command[delivery-1]"))
        parser.parse_line(json.dumps(queued(source="yet-another-source", delivery="another-delivery")), 9)
        self.assertEqual(sum(i.origin == "human_request" for i in parser.interactions), 2)
        # Equal prose with a different pair of native IDs is another real message.
        parser.parse_line(json.dumps(queued(source="source-2", delivery="delivery-2")), 10)
        self.assertEqual(sum(i.origin == "human_request" for i in parser.interactions), 3)

    def test_queued_requires_actual_human_origin_flag_and_native_identity(self):
        invalid = []
        for field, value in [("humanTurn", False), ("humanTurn", "true"),
                             ("origin", {"kind": "agent"}), ("origin", None),
                             ("source_uuid", ""), ("delivery_id", None),
                             ("commandMode", ["prompt"]), ("type", "other")]:
            row = queued()
            row["attachment"][field] = value
            invalid.append(row)
        missing_flag = queued()
        del missing_flag["attachment"]["humanTurn"]
        invalid.append(missing_flag)
        meta = queued()
        meta["isMeta"] = True
        invalid.append(meta)
        parser = ClaudeCodeLineParser()
        self.assertEqual(parse(parser, invalid), [])
        self.assertEqual(parser.interactions, ())

    def test_goal_wrapper_is_harness_and_preserves_pending_human_correction(self):
        parser = CodexRolloutLineParser()
        human = "Actually, continue working on the goal I requested."
        events = parse(parser, [codex("Build the report"),
                                codex("[Request interrupted by user]"),
                                codex(GOAL), codex(human)])
        self.assertEqual([(e.kind, e.source_line) for e in events],
                         [("interrupt", 2), ("user_turn_after_correction", 4)])
        goal = next(i for i in parser.interactions if i.text == GOAL)
        self.assertEqual((goal.origin, goal.actor), ("harness", "system"))
        self.assertEqual(parser.interactions[-1].origin, "human_request")
        self.assertEqual(parser.interactions[-1].text, human)
        prose = "I prefer concise goal updates; <codex_internal_context source=\"goal\"> is a log wrapper."
        parser.parse_line(json.dumps(codex(prose)), 5)
        self.assertEqual(parser.interactions[-1].origin, "human_request")

    def test_goal_wrapper_cannot_answer_a_pending_interactive_question(self):
        question = {"type": "response_item", "payload": {
            "type": "function_call", "name": "request_user_input", "call_id": "ask-1",
            "arguments": json.dumps({"questions": [{"question": "Which audience?"}]})}}
        parser = CodexRolloutLineParser()
        parse(parser, [codex("Build the report"), question, codex(GOAL), codex("Customers")])
        pending = next(i for i in parser.interactions if i.origin == "question")
        answer = parser.interactions[-1]
        self.assertEqual((answer.origin, answer.text, answer.question_ordinal),
                         ("human_answer", "Customers", pending.ordinal))


class NativeSteeringPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.jsonl"
        self.home = self.root / "home"
        config = self.home / ".config/twill/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(f'artifacts_root = "{self.root / "private"}"\n'
                          'retention = "3650d"\nsettle_window = "0"\n')
        self.state = self.root / "state"

    def write(self, records):
        self.source.write_text("".join(json.dumps(row) + "\n" for row in records))
        os.utime(self.source, (1, 1))

    def ingest(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "twill"), "ingest", "--file", str(self.source),
             "--settle", "0", "--state-dir", str(self.state), "--json"],
            cwd=ROOT, env={**os.environ, "HOME": str(self.home)},
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with closing(sqlite3.connect(self.state / "twill.db")) as connection:
            events = connection.execute(
                "SELECT kind, source_line, text FROM transcript_event ORDER BY source_line"
            ).fetchall()
            observations = connection.execute(
                "SELECT kind, excerpt FROM observation ORDER BY obs_id"
            ).fetchall()
            interactions = connection.execute(
                "SELECT origin, actor, source_line, message_id, locator, text FROM interaction ORDER BY ordinal"
            ).fetchall()
        return events, observations, interactions

    def discover(self):
        private = self.root / "learning-artifacts"
        policy = {"scopes": {"fixture": {"global": True}},
                  "two_pass": {"enabled": True, "scopes": ["fixture"]},
                  "intervention_sources": [str(self.source)], "intervention_settle_seconds": 0}
        mining.discover(policy, private)
        return [(path, json.loads(path.read_text()))
                for path in (private / "learning/interventions").glob("IE-*.json")]

    def test_queued_native_shape_reaches_ingested_observation_and_verified_mining(self):
        human = "Actually, use the public summary rather than a local file."
        attachment = queued(human)
        self.write([claude("user", "Build the report"),
                    claude("assistant", [{"type": "text", "text": "I will use a local file."}]),
                    claude("user", "[Request interrupted by user]"),
                    {"type": "queue-operation", "operation": "enqueue", "content": human},
                    attachment, copy.deepcopy(attachment),
                    claude("assistant", [{"type": "text", "text": "Using the public summary."}])])
        events, observations, interactions = self.ingest()
        self.assertEqual(observations.count(("user_turn_after_correction", human)), 1)
        delivered = [i for i in interactions if i[-1] == human]
        self.assertEqual(delivered, [("human_request", "user", 5, "source-1",
                                      "attachment/queued_command[delivery-1]", human)])
        episodes = self.discover()
        steering = [(p, e) for p, e in episodes if e["context"]["intervention"]["text"] == human]
        self.assertEqual(len(steering), 1)
        path, episode = steering[0]
        verified = mining.verify_episode(path)
        self.assertTrue(verified["verified"])
        self.assertEqual(episode["source_original"]["source_line"], 5)
        self.assertEqual(episode["source_original"]["message_id"], "source-1")
        # Native provenance remains part of the exact source hash guard.
        altered = self.source.read_text().replace('"humanTurn": true', '"humanTurn": false')
        self.source.write_text(altered)
        with self.assertRaises(ValueError):
            mining.verify_episode(path)

    def test_mining_can_identify_a_source_starting_with_queued_attachment(self):
        self.write([{"type": "queue-operation", "operation": "enqueue", "content": "copy"}, queued()])
        episodes = self.discover()
        self.assertEqual(len(episodes), 1)
        path, episode = episodes[0]
        self.assertEqual((episode["provider"], episode["source_original"]["source_line"]), ("claude", 2))
        self.assertTrue(mining.verify_episode(path)["verified"])

    def test_goal_native_shape_is_negative_without_stealing_real_human_observation(self):
        human = "Actually, keep working toward my goal."
        assistant = codex("I will stop here.")
        assistant["payload"]["role"] = "assistant"
        assistant["payload"]["content"][0]["type"] = "output_text"
        self.write([codex("Build the report"), assistant, codex("[Request interrupted by user]"),
                    codex(GOAL), codex(human)])
        events, observations, interactions = self.ingest()
        self.assertIn(("response_item", 5, human), events)
        self.assertNotIn(("user_turn_after_correction", GOAL), observations)
        self.assertIn(("user_turn_after_correction", human), observations)
        self.assertEqual(next(i[:2] for i in interactions if i[-1] == GOAL), ("harness", "system"))
        episodes = self.discover()
        wrapper_path, wrapper = next((p, e) for p, e in episodes
                                     if e["context"]["intervention"]["text"] == GOAL)
        self.assertEqual(wrapper["classification"]["signal"], "negative")
        self.assertIn("harness", wrapper["classification"]["flags"])
        self.assertTrue(mining.verify_episode(wrapper_path)["verified"])
        self.assertTrue(any(e["classification"]["category"] == "correction"
                            and e["context"]["intervention"]["text"] == human for _, e in episodes))


if __name__ == "__main__":
    unittest.main()
