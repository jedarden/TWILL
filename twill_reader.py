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
