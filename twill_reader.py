#!/usr/bin/env python3
"""Claude Code transcript parsing into normalized events (plan §6.2 step 4).

The reader consumes one transcript line at a time and yields the six normalized
event kinds the detector layer keys on: ``run`` (command, exit, error excerpt),
``tool_error``, ``tool_rejected``, ``interrupt``, ``file_read`` and
``user_turn_after_correction``.  Anything else in a transcript -- assistant
prose, successful non-run tool results, ordinary user turns -- is deliberately
not an event.

Parsing rules pinned against the local Claude Code corpus:

- A ``Bash`` tool_use block paired with its ``tool_result`` is one ``run``.
  Failed results carry the exit code in a leading ``Exit code N`` line, so the
  error excerpt is what follows that line.  A run whose result never arrives
  (session ended, or the final line was incomplete) still becomes a ``run`` at
  ``finish()`` with an unknown exit.
- ``tool_error`` covers a failing result of every tool except ``Bash`` -- a
  failed run is already reported by its own ``run`` event and must not be
  double-counted.
- A rejected call is reported as ``tool_rejected`` instead of as a ``run``: the
  command never executed, so it must not feed run-failure detectors.
- ``tool_rejected`` matches the rejection sentinels Claude Code writes into the
  tool result.  The wording varies between client versions, so the sentinels
  live in one tuple and nowhere else.
- ``interrupt`` is the exact user-turn sentinel emitted when a turn is cut off.
  Substring matching would false-positive on prose like "I was interrupted by
  a meeting", so the sentinel is compared exactly.
- A real user turn is a ``user_turn_after_correction`` when it is the first
  user text after an interrupt or a rejection and the agent has not completed
  an action in between (a resolved run, a tool error, a file read): once the
  agent acts again, the user's next turn is a new instruction, not the
  correction.

Excerpts are NOT truncated here.  Plan §6.2 step 5 orders redaction before the
240-character cut, and truncating first could sever a credential into a prefix
the redactor no longer recognizes; the cut belongs to persistence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


RUN_TOOL_NAME = "Bash"
READ_TOOL_NAME = "Read"

KIND_RUN = "run"
KIND_TOOL_ERROR = "tool_error"
KIND_TOOL_REJECTED = "tool_rejected"
KIND_INTERRUPT = "interrupt"
KIND_FILE_READ = "file_read"
KIND_USER_TURN_AFTER_CORRECTION = "user_turn_after_correction"

ALL_EVENT_KINDS = (
    KIND_RUN,
    KIND_TOOL_ERROR,
    KIND_TOOL_REJECTED,
    KIND_INTERRUPT,
    KIND_FILE_READ,
    KIND_USER_TURN_AFTER_CORRECTION,
)

# Claude Code prefixes a failed command's output with its exit status; the
# marker is its own line and the output follows it.
_EXIT_CODE_MARKER = re.compile(r"^Exit code (\d+)\s*$", re.MULTILINE)

# Compared exactly against a user text turn.
_INTERRUPT_SENTINEL = "[Request interrupted by user]"

# Substring-matched against tool-result content and user text turns.  Extend
# here when a client version changes its wording; no other place may grow a
# rejection pattern.
_REJECTION_SENTINELS = (
    "[Request interrupted by user for tool use]",
    "The user doesn't want to proceed with this tool use",
    "The user doesn't want to take this action",
)


@dataclass(frozen=True)
class NormalizedEvent:
    """One normalized transcript event.

    ``text`` is the digest excerpt; the remaining fields are the queryable
    columns a detector groups on.  ``exit_code`` is ``None`` whenever the
    transcript does not state one -- Claude Code only records it for failures,
    so ``None`` means "not in the transcript", never "zero".
    """

    kind: str
    source_line: int
    event_index: int
    timestamp: str | None = None
    session_id: str | None = None
    cwd: str | None = None
    sidechain: bool = False
    text: str = ""
    tool_name: str | None = None
    command: str | None = None
    exit_code: int | None = None
    error_excerpt: str | None = None
    file_path: str | None = None


@dataclass(frozen=True)
class _RecordContext:
    """Per-record fields copied onto every event the record produces."""

    timestamp: str | None
    session_id: str | None
    cwd: str | None
    sidechain: bool


@dataclass(frozen=True)
class _PendingToolUse:
    """A tool_use block waiting for its tool_result on a later line."""

    tool_use_id: str
    name: str
    command: str | None
    file_path: str | None
    source_line: int
    context: _RecordContext


class ClaudeCodeLineParser:
    """Parses complete Claude Code transcript lines one at a time.

    State is bounded by the tool calls whose result has not arrived yet, so the
    parser can run over a file that is still being appended to.
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.cwd: str | None = None
        self._pending: dict[str, _PendingToolUse] = {}
        self._awaiting_correction = False

    def parse_line(self, line: str, source_line: int) -> list[NormalizedEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            # An incomplete (typically final) line: complete lines only.
            return []
        if not isinstance(record, dict):
            return []

        self._absorb_record_identity(record)
        context = self._record_context(record)

        record_type = record.get("type")
        if record_type == "assistant":
            return self._parse_assistant(record, source_line, context)
        if record_type == "user":
            return self._parse_user(record, source_line, context)
        return []

    def finish(self) -> list[NormalizedEvent]:
        """Flush runs whose result never arrived (unknown exit)."""

        events = [
            self._run_event(pending, exit_code=None, error_excerpt=None, index=index)
            for index, pending in enumerate(
                pending
                for pending in self._pending.values()
                if pending.name == RUN_TOOL_NAME and pending.command is not None
            )
        ]
        self._pending.clear()
        return events

    # -- record plumbing -----------------------------------------------------

    def _absorb_record_identity(self, record: dict[str, object]) -> None:
        session_id = record.get("sessionId")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.cwd = cwd

    def _record_context(self, record: dict[str, object]) -> _RecordContext:
        timestamp = record.get("timestamp")
        return _RecordContext(
            timestamp=timestamp if isinstance(timestamp, str) else None,
            session_id=self.session_id,
            cwd=self.cwd,
            sidechain=record.get("isSidechain") is True,
        )

    # -- assistant records ---------------------------------------------------

    def _parse_assistant(
        self,
        record: dict[str, object],
        source_line: int,
        context: _RecordContext,
    ) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        for block in _content_blocks(record):
            if block.get("type") != "tool_use":
                continue
            pending = _register_tool_use(block, source_line, context)
            if pending.tool_use_id:
                self._pending[pending.tool_use_id] = pending
            elif pending.name == RUN_TOOL_NAME and pending.command is not None:
                # No id to pair a result with: the exit can never arrive.
                events.append(
                    self._run_event(pending, exit_code=None, error_excerpt=None, index=0)
                )
            if pending.name == READ_TOOL_NAME:
                events.append(self._file_read_event(pending, index=len(events)))
        if events:
            self._awaiting_correction = False
        return events

    # -- user records --------------------------------------------------------

    def _parse_user(
        self,
        record: dict[str, object],
        source_line: int,
        context: _RecordContext,
    ) -> list[NormalizedEvent]:
        is_meta = record.get("isMeta") is True
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None

        if isinstance(content, str):
            event = self._classify_user_text(
                content, source_line, index=0, context=context, is_meta=is_meta
            )
            return [event] if event is not None else []

        events: list[NormalizedEvent] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                event = self._consume_tool_result(block, source_line, context)
            elif block.get("type") == "text":
                text = block.get("text")
                event = (
                    self._classify_user_text(
                        text,
                        source_line,
                        index=len(events),
                        context=context,
                        is_meta=is_meta,
                    )
                    if isinstance(text, str)
                    else None
                )
            else:
                event = None
            if event is not None:
                events.append(event)
        return events

    def _consume_tool_result(
        self,
        block: dict[str, object],
        source_line: int,
        context: _RecordContext,
    ) -> NormalizedEvent | None:
        content_text = _tool_result_text(block.get("content"))
        is_error = block.get("is_error") is True
        tool_use_id = block.get("tool_use_id")
        pending = (
            self._pending.pop(tool_use_id, None) if isinstance(tool_use_id, str) else None
        )

        if _matches_rejection(content_text):
            self._awaiting_correction = True
            return self._event(
                KIND_TOOL_REJECTED,
                source_line,
                index=0,
                context=context,
                text=content_text,
                tool_name=pending.name if pending else None,
            )

        if pending is not None and pending.name == RUN_TOOL_NAME:
            exit_code, error_excerpt = _split_exit_code(content_text)
            if exit_code is None and is_error:
                error_excerpt = content_text or None
            self._awaiting_correction = False
            return self._run_event(
                pending, exit_code=exit_code, error_excerpt=error_excerpt, index=0
            )

        if is_error:
            self._awaiting_correction = False
            return self._event(
                KIND_TOOL_ERROR,
                source_line,
                index=0,
                context=context,
                text=content_text,
                tool_name=pending.name if pending else None,
            )
        return None

    def _classify_user_text(
        self,
        text: str,
        source_line: int,
        index: int,
        context: _RecordContext,
        is_meta: bool,
    ) -> NormalizedEvent | None:
        stripped = text.strip()
        if stripped == _INTERRUPT_SENTINEL:
            self._awaiting_correction = True
            return self._event(
                KIND_INTERRUPT, source_line, index=index, context=context, text=stripped
            )
        if _matches_rejection(stripped):
            self._awaiting_correction = True
            return self._event(
                KIND_TOOL_REJECTED,
                source_line,
                index=index,
                context=context,
                text=stripped,
            )
        if stripped and self._awaiting_correction and not is_meta:
            self._awaiting_correction = False
            return self._event(
                KIND_USER_TURN_AFTER_CORRECTION,
                source_line,
                index=index,
                context=context,
                text=text,
            )
        return None

    # -- event builders ------------------------------------------------------

    @staticmethod
    def _event(
        kind: str,
        source_line: int,
        index: int,
        context: _RecordContext,
        **fields: object,
    ) -> NormalizedEvent:
        return NormalizedEvent(
            kind=kind,
            source_line=source_line,
            event_index=index,
            timestamp=context.timestamp,
            session_id=context.session_id,
            cwd=context.cwd,
            sidechain=context.sidechain,
            **fields,  # type: ignore[arg-type]
        )

    @staticmethod
    def _run_event(
        pending: _PendingToolUse,
        exit_code: int | None,
        error_excerpt: str | None,
        index: int,
    ) -> NormalizedEvent:
        # The run is timestamped at its invocation line, not at its result.
        return NormalizedEvent(
            kind=KIND_RUN,
            source_line=pending.source_line,
            event_index=index,
            timestamp=pending.context.timestamp,
            session_id=pending.context.session_id,
            cwd=pending.context.cwd,
            sidechain=pending.context.sidechain,
            text=pending.command or "",
            tool_name=pending.name,
            command=pending.command,
            exit_code=exit_code,
            error_excerpt=error_excerpt,
        )

    @staticmethod
    def _file_read_event(pending: _PendingToolUse, index: int) -> NormalizedEvent:
        return NormalizedEvent(
            kind=KIND_FILE_READ,
            source_line=pending.source_line,
            event_index=index,
            timestamp=pending.context.timestamp,
            session_id=pending.context.session_id,
            cwd=pending.context.cwd,
            sidechain=pending.context.sidechain,
            text=pending.file_path or "",
            tool_name=pending.name,
            file_path=pending.file_path,
        )


def _register_tool_use(
    block: dict[str, object],
    source_line: int,
    context: _RecordContext,
) -> _PendingToolUse:
    name = block.get("name")
    tool_input = block.get("input")
    input_dict = tool_input if isinstance(tool_input, dict) else {}
    command = input_dict.get("command")
    file_path = input_dict.get("file_path")
    tool_use_id = block.get("id")
    return _PendingToolUse(
        tool_use_id=tool_use_id if isinstance(tool_use_id, str) else "",
        name=name if isinstance(name, str) else "",
        command=command if isinstance(command, str) else None,
        file_path=file_path if isinstance(file_path, str) else None,
        source_line=source_line,
        context=context,
    )


def _content_blocks(record: dict[str, object]) -> list[dict[str, object]]:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _tool_result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _split_exit_code(content_text: str) -> tuple[int | None, str | None]:
    """Split a failed run's result into its exit code and error excerpt."""

    match = _EXIT_CODE_MARKER.match(content_text)
    if match is None:
        return None, None
    excerpt = content_text[match.end() :].lstrip("\n").strip()
    return int(match.group(1)), (excerpt or None)


def _matches_rejection(content_text: str) -> bool:
    return any(sentinel in content_text for sentinel in _REJECTION_SENTINELS)


def iter_events(path: Path) -> Iterator[NormalizedEvent]:
    """Yield normalized events from one Claude Code JSONL transcript.

    The file is read strictly one line at a time; only lines that parse as
    complete JSON records produce events.
    """

    parser = ClaudeCodeLineParser()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for source_line, line in enumerate(handle, start=1):
            yield from parser.parse_line(line, source_line)
    yield from parser.finish()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("module is library-only; run pytest")
