"""Phase 9A interaction contract exercised through real CLI ingestion."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
sys.path.insert(0, str(ROOT))

from twill_history import HistorySnapshot  # noqa: E402


def claude(kind, content, *, sid="claude-9a", message_id=None, sidechain=False, meta=False):
    record = {
        "type": kind,
        "sessionId": sid,
        "timestamp": NOW.isoformat(),
        "isSidechain": sidechain,
        "isMeta": meta,
        "message": {"role": kind, "content": content},
    }
    if message_id:
        record["message"]["id"] = message_id
    return record


def codex(kind, payload, *, sid="codex-9a"):
    return {
        "type": kind,
        "timestamp": NOW.isoformat(),
        "payload": {**payload, "session_id": sid},
    }


class InteractionCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        config = self.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        self.artifacts = self.root / "artifacts"
        config.write_text(
            f'artifacts_root = "{self.artifacts}"\nretention = "3650d"\n'
            'settle_window = "0"\n', encoding="utf-8"
        )
        self.state = self.root / "state"
        self.source = self.root / "source.jsonl"

    def write(self, records):
        self.source.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def cli(self, verb="ingest", *, history=None):
        args = [sys.executable, str(CLI), verb]
        if verb == "ingest":
            args += ["--file", str(self.source), "--settle", "0"]
        if verb == "doctor":
            args += ["--rescan-redaction"]
        if verb == "rebuild":
            args[2] = "doctor"
            args += ["--rebuild", "--settle", "0", "--source", str(self.source)]
        args += ["--state-dir", str(self.state), "--json"]
        environment = {**os.environ, "HOME": str(self.home)}
        if history is not None:
            environment["TWILL_HISTORY_DB"] = str(history)
        else:
            environment.pop("TWILL_HISTORY_DB", None)
        result = subprocess.run(
            args, cwd=ROOT, env=environment, capture_output=True, text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)["data"]

    def rows(self):
        with sqlite3.connect(self.state / "twill.db") as connection:
            return connection.execute(
                "SELECT ordinal, origin, actor, turn_id, message_id, call_ordinal, "
                "question_ordinal, sidechain, terminal_state, truncated, text "
                "FROM interaction ORDER BY ordinal"
            ).fetchall()

    def test_claude_mixed_answers_sidechains_redaction_and_dedup(self):
        token = "ghp_" + "1234567890abcdefghijklmnop"
        self.write([
            claude("user", "Make the release notes"),
            claude("assistant", [
                {"type": "text", "text": "I will check."},
                {"type": "tool_use", "id": "ask-1", "name": "AskUserQuestion",
                 "input": {"questions": [{"question": "Which audience?"}]}},
            ], message_id="a-1"),
            claude("assistant", [{"type": "text", "text": "I will check."}], message_id="a-1"),
            claude("user", [{"type": "tool_result", "tool_use_id": "ask-1",
                            "content": "Customers"}]),
            claude("assistant", [{"type": "tool_use", "id": "run-1",
                                  "name": "Bash", "input": {"command": "true"}}],
                   message_id="a-tool"),
            claude("user", [{"type": "tool_result", "tool_use_id": "run-1",
                            "content": "Exit code 0"}]),
            claude("user", "<user_instructions>quoted old instruction</user_instructions>"),
            claude("user", "harness note", meta=True),
            claude("assistant", [{"type": "text", "text": "Done. " + "x" * 300 + " " + token}],
                   message_id="a-final"),
            claude("user", "Delegated check", sidechain=True),
            claude("assistant", [{"type": "text", "text": "Worker summary"}],
                   message_id="worker-final", sidechain=True),
        ])
        self.cli()
        rows = self.rows()
        origins = [row[1] for row in rows]
        self.assertEqual(origins.count("assistant_text"), 3)
        question = next(row for row in rows if row[1] == "question")
        answer = next(row for row in rows if row[1] == "human_answer")
        self.assertEqual(answer[6], question[0])
        self.assertEqual(answer[2], "user")
        call = next(row for row in rows if row[1] == "tool_call")
        result = next(row for row in rows if row[1] == "tool_result")
        self.assertEqual(result[5], call[0])
        self.assertIn("quoted_history", origins)
        self.assertIn("harness", origins)
        final = next(row for row in rows if row[4] == "a-final")
        self.assertEqual(final[8], "unknown")
        self.assertEqual(final[9], 1)
        self.assertLessEqual(len(final[10]), 240)
        self.assertNotIn(token, final[10])
        worker = next(row for row in rows if row[4] == "worker-final")
        self.assertEqual((worker[7], worker[8]), (1, "unknown"))
        with sqlite3.connect(self.state / "twill.db") as connection:
            summary = connection.execute(
                "SELECT retained, truncated_fields FROM interaction_summary"
            ).fetchone()
        self.assertEqual(summary, (len(rows), 1))
        self.cli("doctor")
        self.assertEqual(rows, self.rows())

    def test_codex_native_completion_abort_and_async_user_prompt(self):
        self.write([
            codex("response_item", {"type": "message", "role": "user",
                                     "content": [{"type": "input_text", "text": "Build A"}]}),
            codex("response_item", {"type": "message", "role": "assistant",
                                     "id": "a1", "phase": "final_answer",
                                     "content": [{"type": "output_text", "text": "Working"}]}),
            codex("response_item", {"type": "message", "role": "user",
                                     "content": [{"type": "input_text", "text": "Status?"}]}),
            codex("response_item", {"type": "custom_tool_call", "name": "functions.request_user_input",
                                     "call_id": "q1", "input": "Select audience"}),
            codex("response_item", {"type": "custom_tool_call_output", "call_id": "q1",
                                     "output": "Customers"}),
            codex("response_item", {"type": "message", "role": "assistant",
                                     "id": "a2", "phase": "final_answer",
                                     "content": [{"type": "output_text", "text": "Done"}]}),
            codex("event_msg", {"type": "task_complete"}),
            codex("response_item", {"type": "message", "role": "user",
                                     "content": [{"type": "input_text", "text": "Build B"}]}),
            codex("response_item", {"type": "message", "role": "assistant",
                                     "id": "a3", "content": [{"type": "output_text", "text": "Partial"}]}),
            codex("event_msg", {"type": "turn_aborted"}),
        ])
        self.cli()
        rows = self.rows()
        terminal = {row[4]: row[8] for row in rows if row[4]}
        self.assertEqual(terminal, {"a1": "unknown", "a2": "completed", "a3": "interrupted"})
        question = next(row for row in rows if row[1] == "question")
        answer = next(row for row in rows if row[1] == "human_answer")
        self.assertEqual(answer[6], question[0])
        self.assertEqual(sum(row[1] == "human_request" for row in rows), 3)

    def test_parent_link_uses_source_uuid_not_adjacent_sidechain_record(self):
        first = claude("user", "Main request")
        first["uuid"] = "main-1"
        middle = claude("user", "Delegated request", sidechain=True)
        middle["uuid"] = "side-1"
        last = claude("assistant", [{"type": "text", "text": "Main reply"}],
                      message_id="main-reply")
        last["parentUuid"] = "main-1"
        self.write([first, middle, last])
        self.cli()
        with sqlite3.connect(self.state / "twill.db") as connection:
            parent = connection.execute(
                "SELECT parent_ordinal FROM interaction WHERE message_id='main-reply'"
            ).fetchone()[0]
        self.assertEqual(parent, 0)

    def test_history_adapter_exact_final_item_schema_and_read_only(self):
        self.write([
            codex("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "Finish report"}]}),
            codex("response_item", {"type": "message", "role": "assistant",
                                    "id": "a-final", "phase": "final_answer",
                                    "content": [{"type": "output_text", "text": "Done"}]}),
        ])
        history = self.root / "history.db"
        with sqlite3.connect(history) as connection:
            connection.execute(
                "CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, "
                "status TEXT, completed_at INTEGER, final_agent_item_id TEXT)"
            )
            connection.execute(
                "CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT)"
            )
            connection.execute(
                "INSERT INTO thread_turns VALUES "
                "('codex-9a', 'turn-1', 'completed', 1, 'a-final')"
            )
            connection.execute(
                "INSERT INTO thread_items VALUES ('codex-9a', 'turn-1', 'a-final')"
            )
        before = hashlib.sha256(history.read_bytes()).digest()
        os.chmod(history, 0o444)
        self.cli(history=history)
        self.assertEqual(self.rows()[-1][8], "completed")
        self.assertEqual(hashlib.sha256(history.read_bytes()).digest(), before)
        os.chmod(history, 0o644)
        with sqlite3.connect(history) as connection:
            connection.execute("DROP TABLE thread_items")
        with self.source.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(codex("event_msg", {"type": "progress"})) + "\n")
        self.cli(history=history)
        self.assertEqual(self.rows()[-1][8], "unknown")
        self.cli()
        self.assertEqual(self.rows()[-1][8], "unknown")

    def test_cap_append_rewrite_rebuild_and_idempotency(self):
        records = [claude("user", f"step {index}") for index in range(1002)]
        self.write(records)
        self.cli()
        with sqlite3.connect(self.state / "twill.db") as connection:
            summary = connection.execute(
                "SELECT retained, omitted_over_cap, omitted_first_ordinal, "
                "omitted_last_ordinal FROM interaction_summary"
            ).fetchone()
            first_rows = connection.execute(
                "SELECT ordinal, origin, text FROM interaction ORDER BY ordinal"
            ).fetchall()
        self.assertEqual(summary, (1000, 2, 1000, 1001))
        self.cli()
        self.assertEqual(len(self.rows()), 1000)
        with sqlite3.connect(self.state / "twill.db") as connection:
            self.assertEqual(first_rows, connection.execute(
                "SELECT ordinal, origin, text FROM interaction ORDER BY ordinal"
            ).fetchall())
        with self.source.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(claude("user", "step 1002")) + "\n")
        self.cli()
        with sqlite3.connect(self.state / "twill.db") as connection:
            self.assertEqual(connection.execute(
                "SELECT omitted_over_cap FROM interaction_summary"
            ).fetchone()[0], 3)
        self.write([claude("user", "replacement")])
        self.cli()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][-1], "replacement")
        self.cli("rebuild")
        self.assertEqual(self.rows()[0][-1], "replacement")

    def test_retention_prune_and_rescan_redaction(self):
        self.write([
            claude("user", "old", sid="retention-9a"),
            claude("user", "new", sid="retention-9a"),
        ])
        self.cli()
        with sqlite3.connect(self.state / "twill.db") as connection:
            connection.execute(
                "UPDATE interaction SET ts_utc = ?, text = ?, truncated=1, "
                "truncated_fields=1, terminal_state='unknown', history_confirmed=1 "
                "WHERE ordinal = 0",
                ((NOW - timedelta(days=4000)).isoformat(),
                 "ghp_1234567890abcdefghijklmnop"),
            )
            connection.execute(
                "UPDATE interaction SET channel = ? WHERE ordinal = 1",
                ("ghp_1234567890abcdefghijklmnop",),
            )
        self.cli("doctor")
        self.assertNotIn("ghp_", str(self.rows()))
        with sqlite3.connect(self.state / "twill.db") as connection:
            self.assertEqual(connection.execute(
                "SELECT channel FROM interaction WHERE ordinal=1"
            ).fetchone()[0], "<redacted:github-token>")
        self.cli("prune")
        self.assertEqual(len(self.rows()), 1)
        with sqlite3.connect(self.state / "twill.db") as connection:
            self.assertEqual(connection.execute(
                "SELECT retained, truncated_fields, terminal_unknown, "
                "history_confirmed FROM interaction_summary"
            ).fetchone(), (1, 0, 0, 0))

    def test_history_snapshot_does_not_see_later_provider_write(self):
        history = self.root / "history.db"
        with sqlite3.connect(history) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, "
                "status TEXT, completed_at INTEGER, final_agent_item_id TEXT)"
            )
            connection.execute(
                "CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT)"
            )
        with HistorySnapshot.open(history) as snapshot:
            with sqlite3.connect(history) as writer:
                writer.execute(
                    "INSERT INTO thread_turns VALUES ('s', 't', 'completed', 1, 'a')"
                )
                writer.execute("INSERT INTO thread_items VALUES ('s', 't', 'a')")
            self.assertEqual(snapshot.completed_message_ids("s"), frozenset())
        with HistorySnapshot.open(history) as later:
            self.assertEqual(later.completed_message_ids("s"), frozenset({"a"}))


if __name__ == "__main__":
    unittest.main()
