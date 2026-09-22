import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "parser" / "claude"
sys.path.insert(0, str(ROOT))

from twill_reader import (  # noqa: E402
    ALL_EVENT_KINDS,
    KIND_FILE_READ,
    KIND_INTERRUPT,
    KIND_RUN,
    KIND_TOOL_ERROR,
    KIND_TOOL_REJECTED,
    KIND_USER_TURN_AFTER_CORRECTION,
    ClaudeCodeLineParser,
    iter_events,
)


def fixture_events(name):
    return list(iter_events(FIXTURES / name))


def kinds(events):
    return [event.kind for event in events]


class ParserManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((FIXTURES / "manifest.json").read_text())

    def test_every_declared_fixture_file_exists(self):
        for case_name, case in self.manifest["cases"].items():
            self.assertTrue((FIXTURES / case["file"]).is_file(), case_name)

    def test_every_normalized_event_kind_has_a_fixture(self):
        covered = set()
        for case in self.manifest["cases"].values():
            covered.update(case["event_kinds"])
        self.assertEqual(covered, set(ALL_EVENT_KINDS))

    def test_declared_kinds_are_a_subset_of_the_vocabulary(self):
        for case_name, case in self.manifest["cases"].items():
            self.assertTrue(set(case["event_kinds"]) <= set(ALL_EVENT_KINDS), case_name)


class RunEventTests(unittest.TestCase):
    def test_failed_run_carries_command_exit_and_error_excerpt(self):
        events = fixture_events("run-failed.jsonl")
        self.assertEqual(kinds(events), [KIND_RUN])
        run = events[0]
        self.assertEqual(run.command, "pytest -q")
        self.assertEqual(run.tool_name, "Bash")
        self.assertEqual(run.exit_code, 2)
        self.assertEqual(
            run.error_excerpt,
            "ERROR tests/test_reader.py - FileNotFound: fixtures/missing.json",
        )
        self.assertEqual(run.text, "pytest -q")
        self.assertEqual(run.source_line, 1)

    def test_successful_run_has_no_exit_code_because_none_was_recorded(self):
        events = fixture_events("run-succeeded.jsonl")
        self.assertEqual(kinds(events), [KIND_RUN])
        run = events[0]
        self.assertEqual(run.command, "git status --short")
        self.assertIsNone(run.exit_code)
        self.assertIsNone(run.error_excerpt)

    def test_unresolved_run_is_flushed_at_end_of_file_with_unknown_exit(self):
        events = fixture_events("unresolved-run.jsonl")
        self.assertEqual(kinds(events), [KIND_RUN])
        run = events[0]
        self.assertEqual(run.command, "make build")
        self.assertIsNone(run.exit_code)
        self.assertIsNone(run.error_excerpt)

    def test_incomplete_final_line_is_skipped_but_complete_lines_survive(self):
        events = fixture_events("truncated-final-line.jsonl")
        self.assertEqual(kinds(events), [KIND_RUN])
        self.assertEqual(events[0].exit_code, 1)
        self.assertEqual(events[0].error_excerpt, "2 failed, 1 passed")


class ToolErrorEventTests(unittest.TestCase):
    def test_failing_non_bash_tool_result_is_a_tool_error(self):
        events = fixture_events("tool-error-edit.jsonl")
        self.assertEqual(kinds(events), [KIND_TOOL_ERROR])
        event = events[0]
        self.assertEqual(event.tool_name, "Edit")
        self.assertEqual(
            event.text,
            "<tool_use_error>String to replace not found in file.</tool_use_error>",
        )
        self.assertIsNone(event.command)


class ToolRejectionTests(unittest.TestCase):
    def test_interrupt_during_tool_use_rejects_and_the_next_user_turn_corrects(self):
        events = fixture_events("tool-rejected.jsonl")
        self.assertEqual(kinds(events), [KIND_TOOL_REJECTED, KIND_USER_TURN_AFTER_CORRECTION])
        self.assertEqual(events[0].tool_name, "Write")
        self.assertEqual(
            events[0].text, "[Request interrupted by user for tool use]"
        )
        self.assertEqual(events[1].text, "stop, write it to the scratch dir instead")

    def test_permission_denial_sentinels_reject_and_a_rejected_bash_is_not_a_run(self):
        events = fixture_events("tool-rejected-permission.jsonl")
        self.assertEqual(
            kinds(events),
            [KIND_TOOL_REJECTED, KIND_TOOL_REJECTED, KIND_USER_TURN_AFTER_CORRECTION],
        )
        self.assertEqual(events[0].tool_name, "Bash")
        self.assertEqual(events[1].tool_name, "WebFetch")
        self.assertEqual(events[2].text, "use the read-only endpoint instead")
        self.assertEqual([event.command for event in events], [None, None, None])

    def test_rejection_sentinel_in_a_user_text_turn_is_a_rejection(self):
        parser = ClaudeCodeLineParser()
        parser.parse_line(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "s",
                    "timestamp": "2026-09-22T09:00:00Z",
                    "message": {
                        "content": "[Request interrupted by user for tool use]"
                    },
                }
            ),
            source_line=1,
        )
        events = parser.parse_line("{}", 2)  # unrelated record keeps state intact
        self.assertEqual(events, [])
        self.assertTrue(parser._awaiting_correction)


class InterruptAndCorrectionTests(unittest.TestCase):
    def test_interrupt_then_correction_then_a_later_turn_that_is_not_a_correction(self):
        events = fixture_events("interrupt-then-correction.jsonl")
        self.assertEqual(
            kinds(events), [KIND_INTERRUPT, KIND_USER_TURN_AFTER_CORRECTION]
        )
        self.assertEqual(events[0].text, "[Request interrupted by user]")
        self.assertEqual(events[1].text, "don't deploy, run the dry-run first")

    def test_correction_window_closes_when_the_agent_completes_an_action(self):
        events = fixture_events("interrupt-assistant-resumes.jsonl")
        self.assertEqual(kinds(events), [KIND_INTERRUPT, KIND_RUN])
        self.assertEqual(events[1].command, "git status --short")

    def test_interrupt_sentinel_matches_exactly_not_as_a_substring(self):
        events = fixture_events("plain-turns.jsonl")
        self.assertEqual(events, [])

    def test_meta_user_turn_does_not_consume_the_correction_window(self):
        parser = ClaudeCodeLineParser()
        parser.parse_line(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "s",
                    "message": {"content": "[Request interrupted by user]"},
                }
            ),
            source_line=1,
        )
        parser.parse_line(
            json.dumps(
                {
                    "type": "user",
                    "isMeta": True,
                    "sessionId": "s",
                    "message": {"content": "<system-reminder>context</system-reminder>"},
                }
            ),
            source_line=2,
        )
        events = parser.parse_line(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "s",
                    "message": {"content": "run the dry-run instead"},
                }
            ),
            source_line=3,
        )
        self.assertEqual(kinds(events), [KIND_USER_TURN_AFTER_CORRECTION])
        self.assertEqual(events[0].text, "run the dry-run instead")


class FileReadEventTests(unittest.TestCase):
    def test_file_read_is_emitted_at_the_call_and_run_at_its_result(self):
        events = fixture_events("file-read.jsonl")
        self.assertEqual(kinds(events), [KIND_FILE_READ, KIND_RUN])
        file_read, run = events
        self.assertEqual(file_read.file_path, "/home/coding/TWILL/README.md")
        self.assertEqual(file_read.tool_name, "Read")
        self.assertEqual(file_read.text, "/home/coding/TWILL/README.md")
        self.assertEqual(run.command, "wc -l README.md")
        # The run resolves on line 2 even though it was invoked on line 1.
        self.assertEqual(run.source_line, 1)
        self.assertEqual(file_read.source_line, 1)


class LineParserUnitTests(unittest.TestCase):
    SESSION = {
        "sessionId": "unit-session",
        "cwd": "/home/coding/TWILL",
        "timestamp": "2026-09-22T09:00:00.000Z",
    }

    def parse(self, parser, record, source_line):
        return parser.parse_line(json.dumps({"type": "user", **self.SESSION, **record}),
                                 source_line)

    def test_incomplete_and_non_record_lines_produce_nothing(self):
        parser = ClaudeCodeLineParser()
        self.assertEqual(parser.parse_line('{"type":"user","mess', 1), [])
        self.assertEqual(parser.parse_line("[1, 2]", 2), [])
        self.assertEqual(parser.parse_line("   ", 3), [])
        # A broken line must not stop later complete lines from parsing.
        events = parser.parse_line(
            json.dumps(
                {
                    "type": "user",
                    **self.SESSION,
                    "message": {"content": "[Request interrupted by user]"},
                }
            ),
            4,
        )
        self.assertEqual(kinds(events), [KIND_INTERRUPT])

    def test_successful_tool_result_of_a_non_bash_tool_is_not_an_event(self):
        parser = ClaudeCodeLineParser()
        self.parse(
            parser,
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_9",
                            "name": "WebFetch",
                            "input": {"url": "https://example.invalid"},
                        }
                    ]
                },
            },
            1,
        )
        events = self.parse(
            parser,
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_9",
                            "content": [{"type": "text", "text": "<output>ok</output>"}],
                        }
                    ]
                }
            },
            2,
        )
        self.assertEqual(events, [])
        self.assertEqual(parser._pending, {})

    def test_events_carry_record_identity_fields(self):
        parser = ClaudeCodeLineParser()
        events = self.parse(
            parser,
            {
                "isSidechain": True,
                "message": {"content": "[Request interrupted by user]"},
            },
            7,
        )
        event = events[0]
        self.assertEqual(event.session_id, "unit-session")
        self.assertEqual(event.cwd, "/home/coding/TWILL")
        self.assertEqual(event.timestamp, "2026-09-22T09:00:00.000Z")
        self.assertTrue(event.sidechain)

    def test_finish_is_idempotent(self):
        parser = ClaudeCodeLineParser()
        self.parse(
            parser,
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Bash",
                            "input": {"command": "make build"},
                        }
                    ]
                },
            },
            1,
        )
        first = parser.finish()
        self.assertEqual(kinds(first), [KIND_RUN])
        self.assertEqual(parser.finish(), [])


if __name__ == "__main__":
    unittest.main()
