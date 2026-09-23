import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "transcripts" / "codex"
sys.path.insert(0, str(ROOT))

from codex_reader import (  # noqa: E402
    KIND_FILE_READ,
    KIND_INTERRUPT,
    KIND_RUN,
    KIND_TOOL_ERROR,
    KIND_TOOL_REJECTED,
    KIND_USER_TURN_AFTER_CORRECTION,
    CodexRolloutLineParser,
    CodexRolloutReader,
    iter_events,
    parse_rollout,
)


def record(record_type, payload, *, timestamp="2026-09-22T13:00:00Z"):
    return json.dumps({"type": record_type, "timestamp": timestamp, "payload": payload})


class CodexReaderTests(unittest.TestCase):
    def test_user_response_item_is_not_duplicated_by_event_message(self):
        parser = CodexRolloutLineParser()
        self.assertEqual(
            parser.parse_line(
                record(
                    "response_item",
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "run the tests"}],
                    },
                ),
                1,
            ),
            [],
        )
        self.assertEqual(
            parser.parse_line(
                record(
                    "event_msg",
                    {
                        "type": "user_message",
                        "message": {"type": "user_message", "text": "run the tests"},
                    },
                ),
                2,
            ),
            [],
        )

    def test_exec_call_and_output_emit_claude_shaped_run(self):
        parser = CodexRolloutLineParser()
        parser.parse_line(
            record(
                "session_meta",
                {"session_id": "codex-session", "cwd": "/workspace/demo"},
            ),
            1,
        )
        parser.parse_line(
            record(
                "response_item",
                {
                    "type": "custom_tool_call",
                    "id": "call-1",
                    "call_id": "call-1",
                    "name": "exec",
                    "input": "pytest -q",
                },
            ),
            2,
        )
        events = parser.parse_line(
            record(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-1",
                    "output": [
                        {
                            "type": "text",
                            "text": "Process exited with code 2\n2 failed, 1 passed",
                        }
                    ],
                },
            ),
            3,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, KIND_RUN)
        self.assertEqual(events[0].command, "pytest -q")
        self.assertEqual(events[0].text, "pytest -q")
        self.assertEqual(events[0].exit_code, 2)
        self.assertEqual(events[0].error_excerpt, "2 failed, 1 passed")
        self.assertEqual(events[0].source_line, 2)
        self.assertEqual(events[0].session_id, "codex-session")
        self.assertEqual(events[0].cwd, "/workspace/demo")

    def test_read_rejection_and_non_run_failure_have_distinct_event_kinds(self):
        parser = CodexRolloutLineParser()
        file_read = parser.parse_line(
            record(
                "response_item",
                {
                    "type": "custom_tool_call",
                    "id": "read-1",
                    "call_id": "read-1",
                    "name": "read_file",
                    "input": json.dumps({"file_path": "README.md"}),
                },
            ),
            1,
        )
        self.assertEqual(file_read[0].kind, KIND_FILE_READ)
        self.assertEqual(file_read[0].file_path, "README.md")

        tool_error = parser.parse_line(
            record(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "read-1",
                    "output": [{"type": "text", "text": "<tool_use_error>not found</tool_use_error>"}],
                },
            ),
            2,
        )
        self.assertEqual(tool_error[0].kind, KIND_TOOL_ERROR)
        self.assertEqual(tool_error[0].tool_name, "read_file")

        parser.parse_line(
            record(
                "response_item",
                {
                    "type": "custom_tool_call",
                    "id": "exec-1",
                    "call_id": "exec-1",
                    "name": "exec",
                    "input": "rm -rf scratch",
                },
            ),
            3,
        )
        rejected = parser.parse_line(
            record(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "exec-1",
                    "output": "The user doesn't want to proceed with this tool use.",
                },
            ),
            4,
        )
        self.assertEqual(rejected[0].kind, KIND_TOOL_REJECTED)
        self.assertEqual(rejected[0].tool_name, "exec")

    def test_turn_abort_then_response_item_prompt_is_correction(self):
        parser = CodexRolloutLineParser()
        interrupt = parser.parse_line(
            record("event_msg", {"type": "turn_aborted", "reason": "interrupted"}),
            1,
        )
        correction = parser.parse_line(
            record(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "use a dry run"}],
                },
            ),
            2,
        )
        self.assertEqual([event.kind for event in interrupt], [KIND_INTERRUPT])
        self.assertEqual([event.kind for event in correction], [KIND_USER_TURN_AFTER_CORRECTION])
        self.assertEqual(interrupt[0].text, "[Request interrupted by user]")
        self.assertEqual(correction[0].text, "use a dry run")

    def test_assistant_response_clears_correction_state(self):
        parser = CodexRolloutLineParser()
        parser.parse_line(record("event_msg", {"type": "turn_aborted"}), 1)
        parser.parse_line(
            record(
                "response_item",
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Stopping."}],
                },
            ),
            2,
        )
        self.assertEqual(
            parser.parse_line(
                record(
                    "response_item",
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "continue"}],
                    },
                ),
                3,
            ),
            [],
        )

    def test_cumulative_token_snapshots_emit_only_positive_deltas(self):
        parser = CodexRolloutLineParser()
        snapshots = [
            {"input_tokens": 10, "output_tokens": 4, "cached_input_tokens": 2, "total_tokens": 14},
            {"input_tokens": 10, "output_tokens": 4, "cached_input_tokens": 2, "total_tokens": 14},
            {"input_tokens": 15, "output_tokens": 7, "cached_input_tokens": 3, "total_tokens": 22},
        ]
        for line_number, snapshot in enumerate(snapshots, start=1):
            parser.parse_line(
                record(
                    "event_msg",
                    {"type": "token_count", "info": {"total_token_usage": snapshot}},
                ),
                line_number,
            )

        self.assertEqual(len(parser.usage), 2)
        self.assertEqual(parser.usage[0].input_tokens, 10)
        self.assertEqual(parser.usage[0].cache_read_tokens, 2)
        self.assertEqual(parser.usage[1].input_tokens, 5)
        self.assertEqual(parser.usage[1].output_tokens, 3)
        self.assertEqual(parser.usage[1].cache_read_tokens, 1)
        self.assertEqual(parser.usage[1].total_tokens, 8)

    def test_file_iteration_skips_truncated_line_and_flushes_unresolved_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        record(
                            "response_item",
                            {
                                "type": "custom_tool_call",
                                "id": "call-1",
                                "call_id": "call-1",
                                "name": "exec",
                                "input": "make build",
                            },
                        ),
                        '{"type":"response_item","payload":{"type":"message"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            events = list(iter_events(path))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, KIND_RUN)
            self.assertIsNone(events[0].exit_code)

    def test_parse_rollout_returns_events_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                record(
                    "event_msg",
                    {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 3}},
                    },
                )
                + "\n",
                encoding="utf-8",
            )
            result = parse_rollout(path)
            self.assertEqual(result.events, ())
            self.assertEqual(result.usage[0].total_tokens, 3)
            self.assertEqual(result.next_offset, path.stat().st_size)
            self.assertEqual(result.bytes_consumed, path.stat().st_size)

    def test_appended_fixture_resume_matches_whole_file_event_stream(self):
        base = FIXTURE_ROOT / "appended-between-runs" / "base.jsonl"
        append = FIXTURE_ROOT / "appended-between-runs" / "append.jsonl"
        base_bytes = base.read_bytes()
        append_bytes = append.read_bytes()
        split = append_bytes.index(b"\n") // 2

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes(base_bytes)
            reader = CodexRolloutReader()
            first = parse_rollout(path, reader=reader)
            self.assertEqual(first.next_offset, len(base_bytes))
            self.assertEqual(first.bytes_consumed, len(base_bytes))

            with path.open("ab") as handle:
                handle.write(append_bytes[:split])
            torn = parse_rollout(path, first.next_offset, reader=reader)
            self.assertEqual(torn.next_offset, first.next_offset)
            self.assertEqual(torn.bytes_consumed, 0)

            with path.open("ab") as handle:
                handle.write(append_bytes[split:])
            resumed = parse_rollout(path, torn.next_offset, reader=reader)
            final_events = resumed.events + tuple(reader.finish())
            whole = parse_rollout(path)

            self.assertEqual(resumed.next_offset, path.stat().st_size)
            self.assertEqual(resumed.bytes_consumed, len(append_bytes))
            self.assertEqual(resumed.session_id, whole.session_id)
            self.assertEqual(final_events, whole.events)
            self.assertEqual(first.usage + resumed.usage, whole.usage)

    def test_resume_preserves_pending_call_and_usage_state(self):
        lines = [
            record(
                "session_meta",
                {"session_id": "codex-resume-001", "cwd": "/workspace/demo"},
            ),
            record(
                "response_item",
                {
                    "type": "custom_tool_call",
                    "call_id": "run-1",
                    "name": "exec",
                    "input": "pytest -q",
                },
            ),
            record(
                "event_msg",
                {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 10,
                            "output_tokens": 4,
                            "total_tokens": 14,
                        }
                    },
                },
            ),
            record(
                "event_msg",
                {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 15,
                            "output_tokens": 7,
                            "total_tokens": 22,
                        }
                    },
                },
            ),
            record(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "run-1",
                    "output": "Process exited with code 0",
                },
            ),
            record("event_msg", {"type": "turn_aborted"}),
            record(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "resume safely"}],
                },
            ),
        ]
        encoded = [line.encode("utf-8") + b"\n" for line in lines]
        base_size = sum(len(line) for line in encoded[:3])
        partial_size = len(encoded[3]) // 2

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes(b"".join(encoded[:3]) + encoded[3][:partial_size])
            reader = CodexRolloutReader()
            first = parse_rollout(path, reader=reader)

            self.assertEqual(first.next_offset, base_size)
            self.assertEqual(first.bytes_consumed, base_size)
            self.assertEqual(first.events, ())
            self.assertEqual(first.usage[0].input_tokens, 10)
            self.assertEqual(first.usage[0].output_tokens, 4)
            self.assertEqual(first.usage[0].total_tokens, 14)

            with path.open("ab") as handle:
                handle.write(encoded[3][partial_size:])
            second = parse_rollout(path, first.next_offset, reader=reader)
            self.assertEqual(second.next_offset, base_size + len(encoded[3]))
            self.assertEqual(second.usage[0].input_tokens, 5)
            self.assertEqual(second.usage[0].output_tokens, 3)
            self.assertEqual(second.usage[0].total_tokens, 8)

            with path.open("ab") as handle:
                handle.write(b"".join(encoded[4:6]))
            third = parse_rollout(path, second.next_offset, reader=reader)
            self.assertEqual(
                [event.kind for event in third.events],
                [KIND_RUN, KIND_INTERRUPT],
            )
            self.assertEqual(third.events[0].source_line, 2)
            self.assertEqual(third.events[0].exit_code, 0)
            self.assertEqual(third.events[1].source_line, 6)

            with path.open("ab") as handle:
                handle.write(encoded[6])
            fourth = parse_rollout(path, third.next_offset, reader=reader)
            self.assertEqual(
                [event.kind for event in fourth.events],
                [KIND_USER_TURN_AFTER_CORRECTION],
            )
            self.assertEqual(fourth.events[0].source_line, 7)
            self.assertEqual(fourth.next_offset, path.stat().st_size)
            self.assertEqual(reader.finish(), [])

            resumed_events = (
                first.events
                + second.events
                + third.events
                + fourth.events
            )
            resumed_usage = first.usage + second.usage + third.usage + fourth.usage
            whole = parse_rollout(path)
            self.assertEqual(resumed_events, whole.events)
            self.assertEqual(resumed_usage, whole.usage)
            self.assertEqual(reader.session_id, whole.session_id)


if __name__ == "__main__":
    unittest.main()
