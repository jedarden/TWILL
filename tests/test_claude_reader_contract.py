"""Contract and shared-corpus regression tests for the Claude reader."""

import json
import sys
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "transcripts"
MANIFEST = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))
CLAUDE_CASES = MANIFEST["sources"]["claude"]["cases"]

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
    NormalizedEvent,
    iter_events,
)


TIMESTAMP = "2026-09-28T09:00:00.000Z"
SESSION = "claude-contract-001"
CWD = "/workspace/demo"


def claude_line(
    record_type, content, *, session_id=SESSION, cwd=CWD, include_identity=True
):
    record = {"type": record_type, "timestamp": TIMESTAMP}
    if include_identity:
        record.update({"sessionId": session_id, "cwd": cwd})
    record["message"] = {
        "role": "assistant" if record_type == "assistant" else "user",
        "content": content,
    }
    return json.dumps(record)


def tool_use(name, tool_use_id, tool_input, **kwargs):
    return claude_line(
        "assistant",
        [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}],
        **kwargs,
    )


def tool_result(tool_use_id, content, *, is_error=False, **kwargs):
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return claude_line("user", [block], **kwargs)


def user_text(text, **kwargs):
    return claude_line("user", text, **kwargs)


def parse_lines(lines, *, flush=True):
    parser = ClaudeCodeLineParser()
    events = []
    for source_line, line in enumerate(lines, start=1):
        events.extend(parser.parse_line(line, source_line))
    if flush:
        events.extend(parser.finish())
    return events


def parse_file(path):
    parser = ClaudeCodeLineParser()
    events = []
    with path.open("r", encoding="utf-8") as handle:
        for source_line, line in enumerate(handle, start=1):
            events.extend(parser.parse_line(line, source_line))
    events.extend(parser.finish())
    return parser, events


EVENT_FIELD_TYPES = {
    "kind": (str,),
    "source_line": (int,),
    "event_index": (int,),
    "timestamp": (str, type(None)),
    "session_id": (str, type(None)),
    "cwd": (str, type(None)),
    "sidechain": (bool,),
    "text": (str,),
    "tool_name": (str, type(None)),
    "command": (str, type(None)),
    "exit_code": (int, type(None)),
    "error_excerpt": (str, type(None)),
    "file_path": (str, type(None)),
}

KIND_EXCLUSIVE_FIELDS = {
    KIND_RUN: frozenset({"command", "exit_code", "error_excerpt"}),
    KIND_FILE_READ: frozenset({"file_path"}),
}
EXCLUSIVE_FIELDS = frozenset().union(*KIND_EXCLUSIVE_FIELDS.values())


def assert_event_contract(test, event, line_count):
    test.assertIsInstance(event, NormalizedEvent)
    test.assertIn(event.kind, ALL_EVENT_KINDS)
    test.assertEqual({field.name for field in fields(event)}, set(EVENT_FIELD_TYPES))
    for field_name, allowed in EVENT_FIELD_TYPES.items():
        test.assertIsInstance(
            getattr(event, field_name), allowed, f"{event.kind}.{field_name}"
        )
    test.assertGreaterEqual(event.source_line, 1)
    test.assertLessEqual(event.source_line, line_count)
    test.assertGreaterEqual(event.event_index, 0)
    for field_name in EXCLUSIVE_FIELDS - KIND_EXCLUSIVE_FIELDS.get(
        event.kind, frozenset()
    ):
        test.assertIsNone(getattr(event, field_name), f"{event.kind}.{field_name}")
    if event.kind in {
        KIND_INTERRUPT,
        KIND_USER_TURN_AFTER_CORRECTION,
    }:
        test.assertIsNone(event.tool_name, event.kind)
    if event.kind == KIND_RUN:
        test.assertEqual(event.text, event.command)


class ClaudeReaderContractTests(unittest.TestCase):
    def test_table_driven_cases_cover_all_normalized_event_kinds(self):
        cases = [
            (
                "run",
                [
                    tool_use("Bash", "run-1", {"command": "pytest -q"}),
                    tool_result(
                        "run-1",
                        "Exit code 2\n2 failed, 1 passed",
                        is_error=True,
                    ),
                ],
                [{"kind": KIND_RUN, "command": "pytest -q", "exit_code": 2}],
            ),
            (
                "tool_error",
                [
                    tool_use("Edit", "edit-1", {"file_path": "src/app.py"}),
                    tool_result(
                        "edit-1",
                        "<tool_use_error>not found</tool_use_error>",
                        is_error=True,
                    ),
                ],
                [{"kind": KIND_TOOL_ERROR, "tool_name": "Edit"}],
            ),
            (
                "tool_rejected",
                [
                    tool_use("Bash", "deny-1", {"command": "rm -rf scratch"}),
                    tool_result(
                        "deny-1",
                        "The user doesn't want to proceed with this tool use.",
                        is_error=True,
                    ),
                ],
                [{"kind": KIND_TOOL_REJECTED, "tool_name": "Bash"}],
            ),
            (
                "interrupt",
                [user_text("[Request interrupted by user]")],
                [{"kind": KIND_INTERRUPT, "text": "[Request interrupted by user]"}],
            ),
            (
                "file_read",
                [tool_use("Read", "read-1", {"file_path": "README.md"})],
                [{"kind": KIND_FILE_READ, "file_path": "README.md"}],
            ),
            (
                "user_turn_after_correction",
                [
                    user_text("[Request interrupted by user]"),
                    user_text("use the dry run instead"),
                ],
                [
                    {"kind": KIND_INTERRUPT},
                    {
                        "kind": KIND_USER_TURN_AFTER_CORRECTION,
                        "text": "use the dry run instead",
                    },
                ],
            ),
        ]

        observed = set()
        for name, lines, expected_events in cases:
            with self.subTest(case=name):
                events = parse_lines(lines)
                self.assertEqual(len(events), len(expected_events))
                for event, expected in zip(events, expected_events):
                    assert_event_contract(self, event, len(lines))
                    observed.add(event.kind)
                    for field_name, expected_value in expected.items():
                        self.assertEqual(getattr(event, field_name), expected_value)
        self.assertEqual(observed, set(ALL_EVENT_KINDS))

    def test_session_and_cwd_are_sticky_until_a_new_value_is_stated(self):
        lines = [
            tool_use("Read", "read-1", {"file_path": "one.md"}, session_id="s1", cwd="/one"),
            tool_result("read-1", "contents", include_identity=False),
            user_text("[Request interrupted by user]", include_identity=False),
            tool_use("Read", "read-2", {"file_path": "two.md"}, session_id="s2", cwd="/two"),
            tool_result("read-2", "contents", include_identity=False),
            user_text(
                "[Request interrupted by user for tool use]", include_identity=False
            ),
        ]

        events = parse_lines(lines)

        self.assertEqual(
            [(event.kind, event.session_id, event.cwd) for event in events],
            [
                (KIND_FILE_READ, "s1", "/one"),
                (KIND_INTERRUPT, "s1", "/one"),
                (KIND_FILE_READ, "s2", "/two"),
                (KIND_TOOL_REJECTED, "s2", "/two"),
            ],
        )

    def test_incomplete_result_leaves_a_pending_run_to_finish(self):
        incomplete_result = (
            '{"type":"user","message":{"content":[{"type":"tool_result"'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "incomplete.jsonl"
            path.write_text(
                tool_use("Bash", "run-1", {"command": "make build"})
                + "\n"
                + incomplete_result
                + "\n",
                encoding="utf-8",
            )
            events = list(iter_events(path))

        self.assertEqual([event.kind for event in events], [KIND_RUN])
        self.assertEqual(events[0].source_line, 1)
        self.assertEqual(events[0].command, "make build")
        self.assertIsNone(events[0].exit_code)
        self.assertIsNone(events[0].error_excerpt)

    def test_rejection_sentinels_are_table_driven_and_never_become_runs(self):
        sentinels = [
            "[Request interrupted by user for tool use]",
            "The user doesn't want to proceed with this tool use",
            "The user doesn't want to take this action",
        ]
        for sentinel in sentinels:
            with self.subTest(sentinel=sentinel):
                events = parse_lines(
                    [
                        tool_use("Bash", "run-1", {"command": "dangerous-command"}),
                        tool_result("run-1", sentinel, is_error=True),
                    ]
                )
                self.assertEqual([event.kind for event in events], [KIND_TOOL_REJECTED])
                self.assertIsNone(events[0].command)
                self.assertEqual(events[0].tool_name, "Bash")

    def test_interrupt_classification_requires_the_exact_sentinel(self):
        cases = [
            ("[Request interrupted by user]", [KIND_INTERRUPT]),
            ("[Request interrupted by user] while checking", []),
            ("I was interrupted by a meeting", []),
        ]
        for text, expected_kinds in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    [event.kind for event in parse_lines([user_text(text)])],
                    expected_kinds,
                )

    def test_correction_ordering_stops_after_the_agent_acts(self):
        cases = [
            (
                "interrupt then correction",
                [user_text("[Request interrupted by user]"), user_text("use dry run")],
                [KIND_INTERRUPT, KIND_USER_TURN_AFTER_CORRECTION],
            ),
            (
                "rejection then correction",
                [
                    tool_use("Bash", "deny-1", {"command": "deploy"}),
                    tool_result("deny-1", "The user doesn't want to take this action", is_error=True),
                    user_text("run the dry run instead"),
                ],
                [KIND_TOOL_REJECTED, KIND_USER_TURN_AFTER_CORRECTION],
            ),
            (
                "interrupt then completed run",
                [
                    user_text("[Request interrupted by user]"),
                    tool_use("Bash", "run-1", {"command": "git status"}),
                    tool_result("run-1", "(Bash completed with no output)"),
                    user_text("continue with the next step"),
                ],
                [KIND_INTERRUPT, KIND_RUN],
            ),
            (
                "rejection then tool error",
                [
                    tool_use("Bash", "deny-1", {"command": "deploy"}),
                    tool_result("deny-1", "The user doesn't want to take this action", is_error=True),
                    tool_use("Edit", "edit-1", {"file_path": "README.md"}),
                    tool_result("edit-1", "edit failed", is_error=True),
                    user_text("try another approach"),
                ],
                [KIND_TOOL_REJECTED, KIND_TOOL_ERROR],
            ),
            (
                "interrupt then file read",
                [
                    user_text("[Request interrupted by user]"),
                    tool_use("Read", "read-1", {"file_path": "README.md"}),
                    user_text("what did you find?"),
                ],
                [KIND_INTERRUPT, KIND_FILE_READ],
            ),
        ]
        for name, lines, expected_kinds in cases:
            with self.subTest(case=name):
                self.assertEqual(
                    [event.kind for event in parse_lines(lines)], expected_kinds
                )


class ClaudeSharedCorpusTests(unittest.TestCase):
    def test_every_claude_manifest_file_is_present_and_parseable(self):
        for case_name, case in CLAUDE_CASES.items():
            for relative_path in case["files"]:
                path = FIXTURE_ROOT / relative_path
                with self.subTest(case=case_name, file=relative_path):
                    self.assertTrue(path.is_file())
                    parser, _events = parse_file(path)
                    self.assertTrue(parser.session_id)
                    self.assertTrue(parser.cwd)

    def test_shared_claude_corpus_has_no_tool_provenance_for_normalized_events(self):
        # These shared fixtures intentionally contain ordinary turns and prose;
        # parser-specific tool/correction fixtures cover event production.
        for case_name, case in CLAUDE_CASES.items():
            for relative_path in case["files"]:
                path = FIXTURE_ROOT / relative_path
                with self.subTest(case=case_name, file=relative_path):
                    self.assertEqual(list(iter_events(path)), [])

    def test_truncated_corpus_snapshot_equals_its_complete_prefix(self):
        relative_path = CLAUDE_CASES["truncated-final-line"]["files"][0]
        path = FIXTURE_ROOT / relative_path
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertGreater(len(lines), 1)
        try:
            json.loads(lines[-1])
        except json.JSONDecodeError:
            pass
        else:
            self.fail("truncated fixture final line unexpectedly became valid JSON")

        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "prefix.jsonl"
            prefix.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            self.assertEqual(list(iter_events(path)), list(iter_events(prefix)))

    def test_append_and_rewrite_snapshots_keep_their_sticky_identity(self):
        append_paths = [
            FIXTURE_ROOT / relative
            for relative in CLAUDE_CASES["appended-between-runs"]["files"]
        ]
        append_states = [parse_file(path)[0] for path in append_paths]
        self.assertEqual(append_states[0].session_id, append_states[1].session_id)
        self.assertEqual(append_states[0].cwd, append_states[1].cwd)

        rewrite_paths = [
            FIXTURE_ROOT / relative
            for relative in CLAUDE_CASES["rewritten-in-place"]["files"]
        ]
        rewrite_states = [parse_file(path)[0] for path in rewrite_paths]
        self.assertEqual(rewrite_states[0].session_id, rewrite_states[1].session_id)
        self.assertEqual(rewrite_states[0].cwd, rewrite_states[1].cwd)


if __name__ == "__main__":
    unittest.main()
