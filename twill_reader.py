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
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator

from twill_interactions import (
    ACTOR_ASSISTANT,
    ACTOR_SYSTEM,
    ACTOR_TOOL,
    ACTOR_USER,
    ORIGIN_ASSISTANT_TEXT,
    ORIGIN_HARNESS,
    ORIGIN_HUMAN_ANSWER,
    ORIGIN_HUMAN_REQUEST,
    ORIGIN_QUESTION,
    ORIGIN_QUOTED_HISTORY,
    ORIGIN_TOOL_CALL,
    ORIGIN_TOOL_RESULT,
    TERMINAL_INTERRUPTED,
    TERMINAL_UNKNOWN,
    Interaction,
    reterminalize,
)


RUN_TOOL_NAME = "Bash"
READ_TOOL_NAME = "Read"

# Tools whose call is a question to the human and whose result is that
# human's real interactive answer (plan §9A).  The wording is fixed per
# client version, like the rejection sentinels.
QUESTION_TOOL_NAMES = frozenset({"AskUserQuestion", "ask_user_question", "ask_user"})

# User-text prefixes that mark quoted history rather than an original
# statement of this session: local-command wrappers and the harness caveat
# that introduces pasted history.
_QUOTED_HISTORY_PREFIXES = (
    "<user_instructions>",
    "<environment_context>",
    "<turn_context>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "Caveat:",
)

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
class ReaderRegistration:
    """One optional input reader and the probe that says whether it is live.

    Optional inputs are owned outside TWILL, so their readers cannot assume
    that a path or producer exists.  Registering the probe separately from
    the parser lets ``doctor`` report that absence without making ingest
    depend on an external gate being answered first.
    """

    name: str
    liveness: Callable[[], bool]

    @property
    def predicate(self) -> Callable[[], bool]:
        """Descriptive alias for callers that refer to a liveness predicate."""

        return self.liveness

    @property
    def is_live(self) -> Callable[[], bool]:
        """Compatibility alias for readers that expose an ``is_live`` probe."""

        return self.liveness


# The concrete optional readers land in separate beads once their external
# formats and owners are settled.  Keeping the registry empty is deliberate:
# doctor can still run now, and each later reader adds its own registration.
READER_REGISTRY: tuple[ReaderRegistration, ...] = ()


def build_reader_registry(
    *readers: ReaderRegistration,
) -> tuple[ReaderRegistration, ...]:
    """Validate and freeze an optional-reader registry.

    Names are the stable keys rendered by doctor.  A duplicate is rejected at
    construction time so two readers cannot report different liveness for the
    same optional input.
    """

    seen: set[str] = set()
    for reader in readers:
        if not isinstance(reader, ReaderRegistration):
            raise TypeError("reader registry entries must be ReaderRegistration values")
        if not reader.name.strip():
            raise ValueError("reader registration name must not be empty")
        if not callable(reader.liveness):
            raise TypeError(
                f"reader {reader.name!r} liveness predicate must be callable"
            )
        if reader.name in seen:
            raise ValueError(f"reader {reader.name!r} is registered more than once")
        seen.add(reader.name)
    return tuple(readers)


def register_optional_input_reader(
    name: str,
    liveness: Callable[[], bool],
) -> ReaderRegistration:
    """Register one optional-input reader and return its registration.

    Reader modules call this once at import time.  The returned value is also
    useful for tests and for code that prefers an explicit registry override.
    """

    global READER_REGISTRY
    reader = ReaderRegistration(name, liveness)
    READER_REGISTRY = build_reader_registry(*READER_REGISTRY, reader)
    return reader


# Short names keep the registry vocabulary consistent with the detector
# registry while retaining the explicit public function above.
Reader = ReaderRegistration
OptionalInputReader = ReaderRegistration
register_reader = register_optional_input_reader


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
class MessageUsage:
    """One deduplicated Claude provider-message usage record."""

    source_line: int
    event_index: int = 0
    timestamp: str | None = None
    session_id: str | None = None
    message_id: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None

    @property
    def provider_message_id(self) -> str | None:
        return self.message_id

    @property
    def costUSD(self) -> float | None:
        return self.cost_usd


TokenUsage = MessageUsage
UsageRecord = MessageUsage


@dataclass(frozen=True)
class _RecordContext:
    """Per-record fields copied onto every event the record produces."""

    timestamp: str | None
    session_id: str | None
    cwd: str | None
    sidechain: bool
    thread_id: str | None = None
    channel: str | None = None
    record_id: str | None = None
    parent_id: str | None = None


@dataclass(frozen=True)
class _PendingToolUse:
    """A tool_use block waiting for its tool_result on a later line."""

    tool_use_id: str
    name: str
    command: str | None
    file_path: str | None
    source_line: int
    context: _RecordContext
    # Interaction graph links (plan §7.1.1): the tool_call/question
    # interaction emitted at the invocation, so its result can link back to
    # its actual call rather than its position in a wrapper record.
    interaction_ordinal: int | None = None
    is_question: bool = False


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
        self._usage_by_key: dict[str, MessageUsage] = {}
        self._usage_order: list[str] = []
        self._model: str | None = None
        self._model_weights: dict[str, tuple[float, int]] = {}
        self._cumulative_cost_usd: float | None = None
        self._cumulative_duration_ms: int | None = None
        self._saw_cost_state = False
        self._cost_state_counters: dict[str, int] = {}
        self._first_timestamp: str | None = None
        self._last_timestamp: str | None = None
        # Interaction graph state (plan §9A).  Turns are counted separately
        # for main and sidechain threads so delegated turns stay
        # distinguishable; terminality of the trailing assistant reply is
        # resolved per thread as the next boundary arrives.
        self._interactions: list[Interaction] = []
        self._main_turn = 0
        self._sidechain_turn = 0
        self._seen_assistant_message_ids: set[str] = set()
        self._pending_question: dict[bool, int | None] = {False: None, True: None}
        self._last_assistant: dict[bool, int | None] = {False: None, True: None}
        self._question_by_tool_use_id: dict[str, int] = {}
        self._record_ordinal: dict[str, int] = {}

    @property
    def usage(self) -> tuple[MessageUsage, ...]:
        """Usage records deduplicated by provider message id."""

        return tuple(self._usage_by_key[key] for key in self._usage_order)

    @property
    def usage_deltas(self) -> tuple[MessageUsage, ...]:
        return self.usage

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def total_cost_usd(self) -> float | None:
        return self._cumulative_cost_usd

    @property
    def cost_usd(self) -> float | None:
        return self._cumulative_cost_usd

    @property
    def cumulative_cost_usd(self) -> float | None:
        return self._cumulative_cost_usd

    @property
    def wall_seconds(self) -> int | None:
        if self._cumulative_duration_ms is None:
            return None
        return max(0, round(self._cumulative_duration_ms / 1000))

    @property
    def duration_seconds(self) -> int | None:
        return self.wall_seconds

    @property
    def has_cost_state(self) -> bool:
        return self._saw_cost_state

    @property
    def cost_state_counters(self) -> dict[str, int]:
        return dict(self._cost_state_counters)

    @property
    def first_timestamp(self) -> str | None:
        return self._first_timestamp

    @property
    def last_timestamp(self) -> str | None:
        return self._last_timestamp

    @property
    def interactions(self) -> tuple[Interaction, ...]:
        """The interaction-contract records derived so far (plan §7.1.1)."""

        return tuple(self._interactions)

    # -- interaction plumbing ------------------------------------------------

    def _current_turn(self, sidechain: bool) -> int:
        return self._sidechain_turn if sidechain else self._main_turn

    def _emit_interaction(
        self,
        *,
        origin: str,
        actor: str,
        source_line: int,
        context: _RecordContext,
        index: int = 0,
        text: str = "",
        message_id: str | None = None,
        locator: str | None = None,
        terminal_state: str = "none",
        turn_id: int | None = None,
        call_ordinal: int | None = None,
        question_ordinal: int | None = None,
        channel: str | None = None,
    ) -> int:
        """Append one interaction and return its ordinal."""

        ordinal = len(self._interactions)
        parent_ordinal = (
            self._record_ordinal.get(context.parent_id)
            if context.parent_id else None
        )
        self._interactions.append(
            Interaction(
                origin=origin,
                actor=actor,
                source_line=source_line,
                event_index=index,
                ordinal=ordinal,
                timestamp=context.timestamp,
                session_id=context.session_id,
                thread_id=context.thread_id,
                cwd=context.cwd,
                sidechain=context.sidechain,
                turn_id=(
                    turn_id
                    if turn_id is not None
                    else self._current_turn(context.sidechain) or None
                ),
                message_id=message_id,
                parent_ordinal=parent_ordinal,
                call_ordinal=call_ordinal,
                question_ordinal=question_ordinal,
                locator=locator,
                terminal_state=terminal_state,
                text=text,
                channel=channel or context.channel,
            )
        )
        if context.record_id:
            self._record_ordinal.setdefault(context.record_id, ordinal)
        return ordinal

    def _open_turn(self, sidechain: bool) -> int:
        """Start a new turn on one thread, closing the previous trailing reply."""

        if sidechain:
            self._sidechain_turn += 1
        else:
            self._main_turn += 1
        self._close_trailing(sidechain, TERMINAL_UNKNOWN)
        return self._current_turn(sidechain)

    def _close_trailing(self, sidechain: bool, terminal_state: str) -> None:
        index = self._last_assistant.get(sidechain)
        if index is not None:
            reterminalize(self._interactions, index, terminal_state)
            self._last_assistant[sidechain] = None

    def _interrupt_turn(self, sidechain: bool) -> None:
        self._awaiting_correction = True
        self._close_trailing(sidechain, TERMINAL_INTERRUPTED)
        self._pending_question[sidechain] = None

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
        self._note_timestamp(context.timestamp)
        self._consume_usage_record(record, source_line, context)
        if record.get("type") in {"cost-state", "cost_state"}:
            self._consume_cost_state(record)

        record_type = record.get("type")
        if record_type == "assistant":
            return self._parse_assistant(record, source_line, context)
        if record_type == "user":
            return self._parse_user(record, source_line, context)
        return []

    def finish(self) -> list[NormalizedEvent]:
        """Flush unresolved runs, and close each thread's trailing reply.

        Claude Code JSONL carries no source-native terminal record, so the
        trailing assistant reply of an unterminated thread can only ever be
        ``unknown`` — never ``completed`` (plan §9A: a reply that merely
        looks final does not prove completion).
        """

        events = [
            self._run_event(pending, exit_code=None, error_excerpt=None, index=index)
            for index, pending in enumerate(
                pending
                for pending in self._pending.values()
                if pending.name == RUN_TOOL_NAME and pending.command is not None
            )
        ]
        self._pending.clear()
        self._close_trailing(False, TERMINAL_UNKNOWN)
        self._close_trailing(True, TERMINAL_UNKNOWN)
        return events

    # -- record plumbing -----------------------------------------------------

    def _absorb_record_identity(self, record: dict[str, object]) -> None:
        session_id = _first_string(record.get("sessionId"), record.get("session_id"))
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.cwd = cwd

    def _record_context(self, record: dict[str, object]) -> _RecordContext:
        timestamp = record.get("timestamp")
        if not isinstance(timestamp, str):
            message = record.get("message")
            timestamp = message.get("timestamp") if isinstance(message, dict) else None
        return _RecordContext(
            timestamp=timestamp if isinstance(timestamp, str) else None,
            session_id=self.session_id,
            cwd=self.cwd,
            sidechain=record.get("isSidechain") is True,
            thread_id=_first_string(
                record.get("agentId"), record.get("threadId"),
                record.get("thread_id"), self.session_id,
            ),
            channel=_first_string(
                record.get("channel"),
                record.get("message", {}).get("channel")
                if isinstance(record.get("message"), dict) else None,
            ),
            record_id=_first_string(record.get("uuid")),
            parent_id=_first_string(record.get("parentUuid"), record.get("parent_uuid")),
        )

    # -- assistant records ---------------------------------------------------

    def _parse_assistant(
        self,
        record: dict[str, object],
        source_line: int,
        context: _RecordContext,
    ) -> list[NormalizedEvent]:
        message = record.get("message")
        message_id = (
            _first_string(
                message.get("id"),  # type: ignore[union-attr]
                record.get("uuid"),
            )
            if isinstance(message, dict)
            else None
        )
        # One provider response can arrive as repeated records (streaming
        # duplicates carry the same message.id); the interaction graph keeps
        # the first occurrence only.
        duplicate = bool(message_id) and message_id in self._seen_assistant_message_ids
        if message_id:
            self._seen_assistant_message_ids.add(message_id)

        events: list[NormalizedEvent] = []
        for block_index, block in enumerate(_content_blocks(record)):
            if block.get("type") == "text" and not duplicate:
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    ordinal = self._emit_interaction(
                        origin=ORIGIN_ASSISTANT_TEXT,
                        actor=ACTOR_ASSISTANT,
                        source_line=source_line,
                        context=context,
                        index=block_index,
                        text=text,
                        message_id=message_id,
                        locator=f"assistant/text[{block_index}]",
                    )
                    # The latest assistant reply of the thread is the
                    # turn-final candidate; without a source-native terminal
                    # record it stays unknown (plan §9A).
                    self._last_assistant[context.sidechain] = ordinal
            if block.get("type") != "tool_use":
                continue
            pending = _register_tool_use(block, source_line, context)
            if not duplicate:
                ordinal = self._emit_call_interaction(pending, block, source_line, context, block_index)
                pending = replace(pending, interaction_ordinal=ordinal)
                if pending.tool_use_id:
                    if pending.is_question:
                        self._question_by_tool_use_id[pending.tool_use_id] = ordinal
                        self._pending_question[context.sidechain] = ordinal
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

    def _emit_call_interaction(
        self,
        pending: _PendingToolUse,
        block: dict[str, object],
        source_line: int,
        context: _RecordContext,
        block_index: int,
    ) -> int:
        """Emit the question or tool_call interaction for one tool_use block."""

        if pending.name in QUESTION_TOOL_NAMES:
            return self._emit_interaction(
                origin=ORIGIN_QUESTION,
                actor=ACTOR_ASSISTANT,
                source_line=source_line,
                context=context,
                index=block_index,
                text=_question_text(block),
                locator=f"assistant/tool_use[{block_index}]",
            )
        return self._emit_interaction(
            origin=ORIGIN_TOOL_CALL,
            actor=ACTOR_ASSISTANT,
            source_line=source_line,
            context=context,
            index=block_index,
            text=pending.command or pending.file_path or "",
            locator=f"assistant/tool_use[{block_index}]",
            call_ordinal=None,
        )

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
            self._emit_user_text_interaction(
                content, source_line, context, index=0, is_meta=is_meta
            )
            return [event] if event is not None else []

        events: list[NormalizedEvent] = []
        for block_index, block in enumerate(content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                self._emit_result_interaction(
                    block, source_line, context, index=block_index
                )
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
                if isinstance(text, str):
                    self._emit_user_text_interaction(
                        text,
                        source_line,
                        context,
                        index=block_index,
                        is_meta=is_meta,
                    )
            else:
                event = None
            if event is not None:
                events.append(event)
        return events

    # -- interaction emission for user records -------------------------------

    def _emit_user_text_interaction(
        self,
        text: str,
        source_line: int,
        context: _RecordContext,
        *,
        index: int,
        is_meta: bool,
    ) -> None:
        """Classify one user text turn into its interaction origin.

        The classification mirrors the event contract's ordering rules but
        keeps the conversation-level distinctions the events deliberately
        drop: harness bookkeeping (``isMeta``), quoted history wrappers, the
        interrupt sentinel, interactive answers to a pending question, and
        real human requests that open turns.
        """

        stripped = text.strip()
        if not stripped:
            return
        if is_meta:
            self._emit_interaction(
                origin=ORIGIN_HARNESS,
                actor=ACTOR_SYSTEM,
                source_line=source_line,
                context=context,
                index=index,
                text=text,
                locator=f"user/text[{index}]",
            )
            return
        if stripped == _INTERRUPT_SENTINEL:
            self._emit_interaction(
                origin=ORIGIN_HARNESS,
                actor=ACTOR_USER,
                source_line=source_line,
                context=context,
                index=index,
                text=text,
                locator=f"user/text[{index}]",
                terminal_state=TERMINAL_INTERRUPTED,
            )
            self._interrupt_turn(context.sidechain)
            return
        if _matches_rejection(stripped):
            self._emit_interaction(
                origin=ORIGIN_HARNESS,
                actor=ACTOR_USER,
                source_line=source_line,
                context=context,
                index=index,
                text=text,
                locator=f"user/text[{index}]",
            )
            return
        if stripped.startswith(_QUOTED_HISTORY_PREFIXES):
            self._emit_interaction(
                origin=ORIGIN_QUOTED_HISTORY,
                actor=ACTOR_SYSTEM,
                source_line=source_line,
                context=context,
                index=index,
                text=text,
                locator=f"user/text[{index}]",
            )
            return
        pending_question = self._pending_question.get(context.sidechain)
        if pending_question is not None:
            # An interactive answer resolves its actual question, not the
            # wrapper position it happens to arrive at.
            self._pending_question[context.sidechain] = None
            self._emit_interaction(
                origin=ORIGIN_HUMAN_ANSWER,
                actor=ACTOR_USER,
                source_line=source_line,
                context=context,
                index=index,
                text=text,
                locator=f"user/text[{index}]",
                question_ordinal=pending_question,
            )
            return
        turn_id = self._open_turn(context.sidechain)
        self._emit_interaction(
            origin=ORIGIN_HUMAN_REQUEST,
            actor=ACTOR_USER,
            source_line=source_line,
            context=context,
            index=index,
            text=text,
            locator=f"user/text[{index}]",
            turn_id=turn_id,
        )

    def _emit_result_interaction(
        self,
        block: dict[str, object],
        source_line: int,
        context: _RecordContext,
        *,
        index: int,
    ) -> None:
        """Emit the answer or tool_result interaction for one tool_result block."""

        content_text = _tool_result_text(block.get("content"))
        tool_use_id = block.get("tool_use_id")
        question_ordinal = (
            self._question_by_tool_use_id.pop(tool_use_id, None)
            if isinstance(tool_use_id, str)
            else None
        )
        pending = (
            self._pending.get(tool_use_id) if isinstance(tool_use_id, str) else None
        )
        if question_ordinal is not None:
            sidechain = context.sidechain
            if self._pending_question.get(sidechain) == question_ordinal:
                self._pending_question[sidechain] = None
            self._emit_interaction(
                origin=ORIGIN_HUMAN_ANSWER,
                actor=ACTOR_USER,
                source_line=source_line,
                context=context,
                index=index,
                text=content_text,
                locator=f"user/tool_result[{index}]",
                question_ordinal=question_ordinal,
            )
            return
        self._emit_interaction(
            origin=ORIGIN_TOOL_RESULT,
            actor=ACTOR_TOOL,
            source_line=source_line,
            context=context,
            index=index,
            text=content_text,
            locator=f"user/tool_result[{index}]",
            channel=pending.name if pending else None,
            call_ordinal=pending.interaction_ordinal if pending else None,
        )

    def _note_timestamp(self, timestamp: str | None) -> None:
        if not timestamp:
            return
        if self._first_timestamp is None:
            self._first_timestamp = timestamp
        self._last_timestamp = timestamp

    def _consume_usage_record(
        self,
        record: dict[str, object],
        source_line: int,
        context: _RecordContext,
    ) -> None:
        message = record.get("message")
        if not isinstance(message, dict):
            message = record
        usage = message.get("usage")
        if not isinstance(usage, dict):
            usage = record.get("usage")
        if not isinstance(usage, dict):
            return

        counters = _usage_counters(usage)
        cost = _first_nonnegative_float(
            usage, ("cost_usd", "costUSD", "cost")
        )
        if cost is None:
            cost = _first_nonnegative_float(message, ("cost_usd", "costUSD", "cost"))
        cumulative_cost = _first_nonnegative_float(
            usage,             ("total_cost_usd", "totalCostUSD", "total_cost", "totalCost")

        )
        if cumulative_cost is None:
            cumulative_cost = _first_nonnegative_float(
                message,             ("total_cost_usd", "totalCostUSD", "total_cost", "totalCost")

            )
        if cumulative_cost is not None:
            self._cumulative_cost_usd = max(
                self._cumulative_cost_usd or 0.0, cumulative_cost
            )
        if not counters and cost is None and cumulative_cost is None:
            return

        message_id = _first_string(
            message.get("id"),
            message.get("message_id"),
            message.get("uuid"),
            record.get("message_id"),
            record.get("provider_message_id"),
            record.get("requestId"),
            record.get("uuid"),
            record.get("id"),
        )
        key = message_id or f"line:{source_line}"
        model = _first_string(message.get("model"), record.get("model"))
        candidate = MessageUsage(
            source_line=source_line,
            event_index=0,
            timestamp=context.timestamp,
            session_id=context.session_id,
            message_id=message_id,
            model=model,
            cost_usd=cost,
            **counters,
        )
        existing = self._usage_by_key.get(key)
        if existing is None:
            self._usage_by_key[key] = candidate
            self._usage_order.append(key)
        else:
            self._usage_by_key[key] = _merge_message_usage(existing, candidate)
        if model:
            self._select_model(model, candidate)

    def _consume_cost_state(self, record: dict[str, object]) -> None:
        self._saw_cost_state = True
        cost = _first_nonnegative_float(
            record,
            (
                "totalCostUSD",
                "total_cost_usd",
                "totalCost",
                "total_cost",
                "costUSD",
                "cost_usd",
            ),
        )
        if cost is not None:
            self._cumulative_cost_usd = max(
                self._cumulative_cost_usd or 0.0, cost
            )

        duration = _first_nonnegative_float(
            record,
            ("totalDuration", "total_duration_ms", "duration_ms"),
        )
        if duration is not None:
            duration_ms = max(0, round(duration))
            self._cumulative_duration_ms = max(
                self._cumulative_duration_ms or 0, duration_ms
            )

        model_usage = record.get("modelUsage") or record.get("model_usage")
        if not isinstance(model_usage, dict):
            return
        for model_name, raw_usage in model_usage.items():
            if not isinstance(model_name, str) or not model_name:
                continue
            if not isinstance(raw_usage, dict):
                continue
            model_cost = _first_nonnegative_float(
                raw_usage, ("costUSD", "cost_usd", "cost")
            )
            if model_cost is not None:
                self._cumulative_cost_usd = max(
                    self._cumulative_cost_usd or 0.0, model_cost
                )
            model_input = dict(raw_usage)
            for source_key, target_key in (
                ("inputTokens", "input_tokens"),
                ("outputTokens", "output_tokens"),
                ("cacheReadInputTokens", "cache_read_tokens"),
                ("cacheCreationInputTokens", "cache_write_tokens"),
            ):
                if source_key in model_input:
                    model_input[target_key] = model_input[source_key]
            model_counters = _usage_counters(model_input)
            for key, value in model_counters.items():
                self._cost_state_counters[key] = max(
                    self._cost_state_counters.get(key, 0), value
                )
            model_total = sum(
                model_counters.get(key, 0)
                for key in ("input_tokens", "output_tokens", "reasoning_output_tokens")
            )
            weight = (model_cost or 0.0, model_total)
            previous = self._model_weights.get(model_name)
            if previous is None or weight > previous:
                self._model_weights[model_name] = weight
            self._select_model(model_name, None, weight=weight)

    def _select_model(
        self,
        model: str,
        usage: MessageUsage | None = None,
        *,
        weight: tuple[float, int] | None = None,
    ) -> None:
        if weight is None:
            weight = _usage_weight(usage) if usage is not None else (0.0, 0)
        previous = self._model_weights.get(model)
        if previous is None or weight > previous:
            self._model_weights[model] = weight
        if self._model is None:
            self._model = model
            return
        current_weight = self._model_weights.get(self._model, (0.0, 0))
        if weight > current_weight:
            self._model = model

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
        is_question=name in QUESTION_TOOL_NAMES if isinstance(name, str) else False,
    )


def _question_text(block: dict[str, object]) -> str:
    """Recover the question a question-tool invocation asks the human."""

    tool_input = block.get("input")
    if isinstance(tool_input, str):
        return tool_input
    if not isinstance(tool_input, dict):
        return ""
    questions = tool_input.get("questions")
    if isinstance(questions, list):
        parts = []
        for item in questions:
            if isinstance(item, dict):
                question = item.get("question")
                if isinstance(question, str) and question.strip():
                    parts.append(question.strip())
        if parts:
            return "\n".join(parts)
    for key in ("question", "query", "prompt", "message"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


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


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _first_nonnegative_int(value: dict[str, object], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            return candidate
    return None


def _first_nonnegative_float(
    value: dict[str, object], keys: tuple[str, ...]
) -> float | None:
    for key in keys:
        candidate = value.get(key)
        if (
            isinstance(candidate, (int, float))
            and not isinstance(candidate, bool)
            and candidate >= 0
        ):
            return float(candidate)
    return None


def _usage_counters(usage: dict[str, object]) -> dict[str, int]:
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "outputTokens", "completion_tokens"),
        "cache_read_tokens": (
            "cache_read_tokens",
            "cache_read_input_tokens",
            "cacheReadInputTokens",
            "cached_input_tokens",
        ),
        "cache_write_tokens": (
            "cache_write_tokens",
            "cache_creation_input_tokens",
            "cacheCreationInputTokens",
            "cache_creation_tokens",
        ),
        "reasoning_output_tokens": (
            "reasoning_output_tokens",
            "reasoning_tokens",
        ),
        "total_tokens": ("total_tokens", "totalTokens"),
    }
    counters: dict[str, int] = {}
    for normalized, keys in aliases.items():
        value = _first_nonnegative_int(usage, keys)
        if value is not None:
            counters[normalized] = value
    details = usage.get("output_tokens_details")
    if "reasoning_output_tokens" not in counters and isinstance(details, dict):
        value = _first_nonnegative_int(
            details, ("thinking_tokens", "reasoning_tokens")
        )
        if value is not None:
            counters["reasoning_output_tokens"] = value
    cache_creation = usage.get("cache_creation")
    if "cache_write_tokens" not in counters and isinstance(cache_creation, dict):
        values = [
            _first_nonnegative_int(cache_creation, (key,))
            for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        ]
        present = [value for value in values if value is not None]
        if present:
            counters["cache_write_tokens"] = sum(present)
    if "total_tokens" not in counters and counters:
        counters["total_tokens"] = sum(
            counters.get(key, 0)
            for key in ("input_tokens", "output_tokens", "reasoning_output_tokens")
        )
    return counters


def _usage_weight(usage: MessageUsage | None) -> tuple[float, int]:
    if usage is None:
        return 0.0, 0
    total = usage.total_tokens or (
        usage.input_tokens
        + usage.output_tokens
        + usage.reasoning_output_tokens
    )
    return usage.cost_usd or 0.0, total


def _merge_message_usage(
    existing: MessageUsage, candidate: MessageUsage
) -> MessageUsage:
    return MessageUsage(
        source_line=existing.source_line,
        event_index=existing.event_index,
        timestamp=existing.timestamp or candidate.timestamp,
        session_id=existing.session_id or candidate.session_id,
        message_id=existing.message_id or candidate.message_id,
        model=(candidate.model if _usage_weight(candidate) > _usage_weight(existing) else existing.model)
        or candidate.model
        or existing.model,
        input_tokens=max(existing.input_tokens, candidate.input_tokens),
        output_tokens=max(existing.output_tokens, candidate.output_tokens),
        cache_read_tokens=max(existing.cache_read_tokens, candidate.cache_read_tokens),
        cache_write_tokens=max(existing.cache_write_tokens, candidate.cache_write_tokens),
        reasoning_output_tokens=max(
            existing.reasoning_output_tokens, candidate.reasoning_output_tokens
        ),
        total_tokens=max(existing.total_tokens, candidate.total_tokens),
        cost_usd=(
            max(existing.cost_usd or 0.0, candidate.cost_usd or 0.0)
            if existing.cost_usd is not None or candidate.cost_usd is not None
            else None
        ),
    )


def iter_usage(path: Path) -> Iterator[MessageUsage]:
    """Yield deduplicated Claude usage records from one transcript."""

    parser = ClaudeCodeLineParser()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for source_line, line in enumerate(handle, start=1):
            if not line.endswith("\n"):
                break
            parser.parse_line(line, source_line)
    yield from parser.usage


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
