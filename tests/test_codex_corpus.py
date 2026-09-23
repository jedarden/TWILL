"""Executable conformance check: the Codex parser against the fixture corpus.

Every codex case listed in ``tests/fixtures/transcripts/manifest.json`` is run
through :func:`codex_reader.parse_rollout` and its output is checked
field-for-field against ``docs/notes/event-contract.md``.  The field tables
below are that document in executable form: one generic shape check driven by
them covers every case and every event kind, so the parsers' source-agnostic
guarantee is enforced against the shared corpus rather than asserted in prose.

The corpus fixtures are deliberately prose-shaped (no tool calls, no outputs,
no token snapshots), so zero detector events is their contract-correct outcome
-- "everything else in a transcript yields no event".  The provenance oracle
below proves that emptiness is earned rather than accidental, and
:class:`CodexContractCheckBitesTests` runs the same generic check over
tool-bearing output so the conformance machinery is seen to hold on events the
corpus itself cannot produce.
"""

import json
import re
import sys
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "transcripts"
sys.path.insert(0, str(ROOT))

from codex_reader import (  # noqa: E402
    ALL_EVENT_KINDS,
    KIND_FILE_READ,
    KIND_INTERRUPT,
    KIND_RUN,
    NormalizedEvent,
    TokenUsage,
    parse_rollout,
)
from twill_redactor import redact  # noqa: E402


MANIFEST = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The event contract, in executable form (docs/notes/event-contract.md).
# --------------------------------------------------------------------------

# ``NormalizedEvent``: field name -> the types the contract allows.
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

# ``TokenUsage``: field name -> the types the contract allows.
USAGE_FIELD_TYPES = {
    "source_line": (int,),
    "event_index": (int,),
    "timestamp": (str, type(None)),
    "session_id": (str, type(None)),
    "input_tokens": (int,),
    "output_tokens": (int,),
    "cache_read_tokens": (int,),
    "cache_write_tokens": (int,),
    "reasoning_output_tokens": (int,),
    "total_tokens": (int,),
    "model_context_window": (int, type(None)),
}

USAGE_COUNTER_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

# Fields the contract reserves for one kind alone; every other kind carries
# ``None`` in them.
KIND_EXCLUSIVE_FIELDS = {
    KIND_RUN: frozenset({"command", "exit_code", "error_excerpt"}),
    KIND_FILE_READ: frozenset({"file_path"}),
}
EXCLUSIVE_FIELD_NAMES = frozenset().union(*KIND_EXCLUSIVE_FIELDS.values())

# The contract names exactly two kinds on which no tool was involved.
TOOL_LESS_KINDS = frozenset({"interrupt", "user_turn_after_correction"})


def assert_result_conforms(test, result, line_count):
    """Check one parse result field-for-field against the event contract.

    One generic shape check, driven by the tables above -- never by
    fixture-specific literals -- so it binds every corpus case and every event
    kind alike.
    """

    test.assertEqual(
        {field.name for field in fields(NormalizedEvent)},
        set(EVENT_FIELD_TYPES),
        "NormalizedEvent has drifted from the contract field table",
    )
    test.assertEqual(
        {field.name for field in fields(TokenUsage)},
        set(USAGE_FIELD_TYPES),
        "TokenUsage has drifted from the contract field table",
    )

    indices_per_line: dict[int, list[int]] = {}
    for event in result.events:
        test.assertIsInstance(event, NormalizedEvent)
        # The kind set is closed: exactly the contract's six kinds.
        test.assertIn(event.kind, ALL_EVENT_KINDS)
        for field_name, allowed in EVENT_FIELD_TYPES.items():
            test.assertIsInstance(
                getattr(event, field_name), allowed, f"{event.kind}.{field_name}"
            )
        _assert_bounded_int(test, event.source_line, 1, line_count, "source_line")
        _assert_bounded_int(test, event.event_index, 0, None, "event_index")

        for field_name in EXCLUSIVE_FIELD_NAMES - KIND_EXCLUSIVE_FIELDS.get(
            event.kind, frozenset()
        ):
            test.assertIsNone(getattr(event, field_name), f"{event.kind}.{field_name}")
        if event.kind in TOOL_LESS_KINDS:
            test.assertIsNone(event.tool_name, event.kind)
        if event.kind == KIND_RUN and event.command is not None:
            # ``run`` mirrors its command into the digest content.
            test.assertEqual(event.text, event.command)
        if event.exit_code in (None, 0):
            # The excerpt follows the stated exit status: none on success and
            # none on an unknown exit, which is never an implicit success.
            test.assertIsNone(event.error_excerpt, event.kind)
        elif event.error_excerpt is not None:
            # The parser never truncates (the cut belongs to persistence), so
            # the only parser-side bound is empty-or-content.
            test.assertTrue(event.error_excerpt.strip(), "error_excerpt")

        indices_per_line.setdefault(event.source_line, []).append(event.event_index)

    # ``event_index`` is the event's 0-based position among its line's events.
    for indices in indices_per_line.values():
        test.assertEqual(indices, list(range(len(indices))))

    for row in result.usage:
        test.assertIsInstance(row, TokenUsage)
        for field_name, allowed in USAGE_FIELD_TYPES.items():
            test.assertIsInstance(getattr(row, field_name), allowed, f"usage.{field_name}")
        _assert_bounded_int(test, row.source_line, 1, line_count, "usage.source_line")
        test.assertEqual(row.event_index, 0)  # one snapshot yields at most one row
        for field_name in USAGE_COUNTER_FIELDS:
            # Deltas are positive accounting data, never negative or boolean.
            _assert_bounded_int(test, getattr(row, field_name), 0, None, f"usage.{field_name}")


def _assert_bounded_int(test, value, low, high, label):
    test.assertNotIsInstance(value, bool, label)  # bool is an int subclass
    test.assertGreaterEqual(value, low, label)
    if high is not None:
        test.assertLessEqual(value, high, label)


# --------------------------------------------------------------------------
# Provenance oracle, derived from the contract's Codex rules -- deliberately
# not imported from the parser, so it stays an independent prediction.
# --------------------------------------------------------------------------

_CALL_RECORD_TYPES = frozenset({"custom_tool_call", "function_call"})
_OUTPUT_RECORD_TYPES = frozenset({"custom_tool_call_output", "function_call_output"})
_INTERRUPT_MESSAGE_TYPES = frozenset({"turn_aborted"})
_REJECTION_MESSAGE_TYPES = frozenset(
    {"tool_call_rejected", "tool_rejected", "approval_rejected"}
)


def iter_complete_records(path):
    """Yield ``(line_number, record)`` for every line holding a JSON object."""

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line the parser must also ignore
        if isinstance(record, dict):
            yield line_number, record


def record_emits_events(record):
    """Whether the contract's Codex provenance rules can map this to an event."""

    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    payload_type = payload.get("type")
    if record.get("type") == "response_item":
        if payload_type in _CALL_RECORD_TYPES | _OUTPUT_RECORD_TYPES:
            return True
        # A user prompt only events while a correction is owed (or on the
        # exact interrupt sentinel); anything else is prose.  "Can map" is
        # read conservatively: prompts count as possible events.
        return payload.get("role") == "user"
    if record.get("type") == "event_msg":
        return payload_type in _INTERRUPT_MESSAGE_TYPES | _REJECTION_MESSAGE_TYPES or (
            payload_type == "user_message"
        )
    return False


def record_carries_usage(record):
    """Whether the record can produce a ``TokenUsage`` row."""

    if record.get("type") == "token_usage_record":
        return True
    payload = record.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "token_count"


def last_stated_session_id(path):
    """The contract's sticky identity: the most recent session id the file stated."""

    last = None
    for _number, record in iter_complete_records(path):
        payload = record.get("payload")
        candidates = (
            record.get("sessionId"),
            record.get("session_id"),
            payload.get("session_id") if isinstance(payload, dict) else None,
        )
        for value in candidates:
            if isinstance(value, str) and value:
                last = value
    return last


def _harvest_strings(value, sink):
    if isinstance(value, str):
        sink.add(value)
    elif isinstance(value, list):
        for item in value:
            _harvest_strings(item, sink)
    elif isinstance(value, dict):
        for item in value.values():
            _harvest_strings(item, sink)


def rollout_record(record_type, payload, timestamp="2026-09-23T12:00:00Z"):
    return json.dumps({"type": record_type, "timestamp": timestamp, "payload": payload})


class CodexCorpusConformanceTests(unittest.TestCase):
    """``parse_rollout`` over every manifest codex case, checked against the contract."""

    @classmethod
    def setUpClass(cls):
        cls.codex_cases = MANIFEST["sources"]["codex"]["cases"]

    def case_paths(self, case):
        return [FIXTURE_ROOT / relative for relative in self.codex_cases[case]["files"]]

    def iter_case_files(self):
        for case in sorted(self.codex_cases):
            for path in self.case_paths(case):
                yield case, path

    def test_every_codex_corpus_case_conforms_to_the_event_contract(self):
        for case, path in self.iter_case_files():
            with self.subTest(case=case, file=str(path.relative_to(FIXTURE_ROOT))):
                result = parse_rollout(path)
                line_count = len(path.read_text(encoding="utf-8").splitlines())
                assert_result_conforms(self, result, line_count)

                # Complete lines only: no event or usage row cites a line that
                # never parsed as a JSON object.
                complete_lines = {number for number, _ in iter_complete_records(path)}
                for event in result.events:
                    self.assertIn(event.source_line, complete_lines, event.kind)
                for row in result.usage:
                    self.assertIn(row.source_line, complete_lines)

                # Sticky identity: the result states exactly the session id the
                # file itself last stated, and never invents one.
                self.assertEqual(result.session_id, last_stated_session_id(path))

                # The provenance oracle: a transcript whose records the
                # contract never maps to events (or usage) must yield none.
                records = [record for _number, record in iter_complete_records(path)]
                if not any(record_emits_events(record) for record in records):
                    self.assertEqual(result.events, (), case)
                if not any(record_carries_usage(record) for record in records):
                    self.assertEqual(result.usage, (), case)

    def test_truncated_final_line_yields_no_event_from_the_partial_line(self):
        for path in self.case_paths("truncated-final-line"):
            with self.subTest(file=str(path.relative_to(FIXTURE_ROOT))):
                lines = path.read_text(encoding="utf-8").splitlines()

                def complete(line):
                    try:
                        return isinstance(json.loads(line), dict)
                    except json.JSONDecodeError:
                        return False

                torn = [
                    number
                    for number, line in enumerate(lines, 1)
                    if not complete(line)
                ]
                # The corpus guarantees the damage is the final line; if that
                # ever stops holding, the scenario needs a new fixture.
                self.assertEqual(torn, [len(lines)], path)

                result = parse_rollout(path)
                for event in result.events:
                    self.assertNotEqual(event.source_line, len(lines), event.kind)
                for row in result.usage:
                    self.assertNotEqual(row.source_line, len(lines))

                # Earlier complete lines remain fully usable on their own:
                # parsing the file equals parsing just its complete prefix.
                with tempfile.TemporaryDirectory() as directory:
                    prefix = Path(directory) / "prefix.jsonl"
                    prefix.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
                    self.assertEqual(parse_rollout(prefix), parse_rollout(path))

    def test_appended_between_runs_keeps_identity_and_leaves_the_base_intact(self):
        base, append = self.case_paths("appended-between-runs")
        base_text = base.read_text(encoding="utf-8")
        append_text = append.read_text(encoding="utf-8")
        base_lines = len(base_text.splitlines())

        with tempfile.TemporaryDirectory() as directory:
            live = Path(directory) / "rollout.jsonl"
            live.write_text(base_text, encoding="utf-8")
            first = parse_rollout(live)

            # The producer appends bytes to the same path between runs.
            live.write_text(base_text + append_text, encoding="utf-8")
            second = parse_rollout(live)
            assert_result_conforms(
                self, second, len((base_text + append_text).splitlines())
            )

            self.assertEqual(first.session_id, last_stated_session_id(base))
            self.assertEqual(second.session_id, first.session_id)
            self.assertEqual(
                parse_rollout(append).session_id, second.session_id,
                "the appended run states the same sticky identity",
            )
            # The append must neither disturb nor duplicate the base region.
            self.assertEqual(
                [event for event in second.events if event.source_line <= base_lines],
                list(first.events),
            )

    def test_rewritten_in_place_parses_each_snapshot_from_its_bytes_alone(self):
        before, after = self.case_paths("rewritten-in-place")
        first = parse_rollout(before)
        second = parse_rollout(after)
        assert_result_conforms(self, second, len(after.read_text(encoding="utf-8").splitlines()))

        # The corpus pairs the snapshots by session identity: the rewrite
        # replaces content, not which session it belongs to.
        self.assertEqual(first.session_id, second.session_id)

        with tempfile.TemporaryDirectory() as directory:
            live = Path(directory) / "rollout.jsonl"
            live.write_text(before.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertEqual(parse_rollout(live), first)
            live.write_text(after.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertEqual(
                parse_rollout(live), second,
                "the replacement parse must follow current bytes alone",
            )

    def test_secret_bearing_output_is_credential_free_after_redaction(self):
        # Redaction before persist is persistence's job (the parser binds text
        # verbatim and never truncates), so the pipeline guarantee is: run the
        # persistence redactor over what the parser emitted and nothing
        # credential-shaped survives.
        credential_patterns = (
            re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_-]{12,}"),
            re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
            re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        )
        for path in self.case_paths("secret-bearing"):
            with self.subTest(file=str(path.relative_to(FIXTURE_ROOT))):
                result = parse_rollout(path)
                emitted = "\n".join(event.text for event in result.events)
                redacted = redact(emitted)
                for pattern in credential_patterns:
                    self.assertIsNone(pattern.search(redacted), path)

    def test_injection_bearing_text_is_transcribed_never_synthesized(self):
        # Transcript text is untrusted data: whatever it instructs, the parser
        # may only bind strings the file itself contains.  The interrupt kind
        # is exempt -- the contract pins its text to the source's sentinel
        # constant rather than to a transcribed string.
        for path in self.case_paths("injection-bearing"):
            with self.subTest(file=str(path.relative_to(FIXTURE_ROOT))):
                source_strings = set()
                for _number, record in iter_complete_records(path):
                    _harvest_strings(record, source_strings)
                for event in parse_rollout(path).events:
                    if event.kind == KIND_INTERRUPT:
                        continue
                    for chunk in event.text.split("\n"):
                        self.assertIn(chunk, source_strings, event.kind)


class CodexContractCheckBitesTests(unittest.TestCase):
    """The generic conformance check holds on tool-bearing output, not just prose."""

    def test_contract_check_accepts_live_output_for_every_kind(self):
        # The corpus fixtures are prose-shaped, so exercise the same shape
        # check over a rollout that produces all six kinds plus a usage row.
        lines = [
            rollout_record(
                "session_meta",
                {"session_id": "codex-contract-001", "cwd": "/workspace/demo"},
            ),
            rollout_record(
                "response_item",
                {
                    "type": "custom_tool_call",
                    "call_id": "run-1",
                    "name": "exec",
                    "input": "pytest -q",
                },
            ),
            rollout_record(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "run-1",
                    "output": "Process exited with code 2\n2 failed, 1 passed",
                },
            ),
            rollout_record(
                "response_item",
                {
                    "type": "custom_tool_call",
                    "call_id": "read-1",
                    "name": "read_file",
                    "input": json.dumps({"file_path": "README.md"}),
                },
            ),
            rollout_record(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "read-1",
                    "output": "<tool_use_error>file not found</tool_use_error>",
                },
            ),
            rollout_record(
                "response_item",
                {
                    "type": "custom_tool_call",
                    "call_id": "deny-1",
                    "name": "exec",
                    "input": "rm -rf scratch",
                },
            ),
            rollout_record(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "deny-1",
                    "output": "The user doesn't want to proceed with this tool use.",
                },
            ),
            rollout_record("event_msg", {"type": "turn_aborted", "reason": "interrupt"}),
            rollout_record(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "[Request interrupted by user]"},
                        {"type": "input_text", "text": "use a dry run"},
                    ],
                },
            ),
            rollout_record(
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
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = parse_rollout(path)

        emitted_kinds = [event.kind for event in result.events]
        for kind in ALL_EVENT_KINDS:
            self.assertIn(kind, emitted_kinds)
        self.assertEqual(len(result.usage), 1)
        assert_result_conforms(self, result, len(lines))


if __name__ == "__main__":
    unittest.main()
