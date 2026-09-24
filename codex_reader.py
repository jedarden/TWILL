#!/usr/bin/env python3
"""Parse Codex rollout JSONL into source-independent observations.

Codex rollouts are not shaped like Claude Code transcripts.  User prompts are
``response_item`` records with ``role=user``; shell calls are usually
``custom_tool_call`` records named ``exec``; and token counts arrive in
``event_msg`` records as cumulative snapshots.  This module translates those
records to the same six event kinds and fields used by the Claude reader.

The parser deliberately consumes complete lines only.  A caller may feed it a
file while Codex is still appending to it, then call :meth:`finish` to flush a
run whose result has not arrived yet.  Token snapshots are exposed separately
as :class:`TokenUsage` rows because they are accounting data, not detector
events; each row contains the delta since the previous cumulative snapshot.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator


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

_INTERRUPT_SENTINEL = "[Request interrupted by user]"

# Codex has used both the Claude-compatible wording and shorter approval
# wording in tool output.  These are intentionally bounded sentinels rather
# than a broad ``denied`` match: a command's ordinary stderr may say
# "permission denied" and should remain a failed run.
_REJECTION_SENTINELS = (
    "[Request interrupted by user for tool use]",
    "The user doesn't want to proceed with this tool use",
    "The user doesn't want to take this action",
    "tool call was rejected",
    "tool call rejected",
    "command was rejected",
    "approval denied",
    "user denied",
    "rejected by user",
)

_EXIT_CODE_PATTERNS = (
    re.compile(r"(?im)\bProcess exited with code\s*[:=]?\s*(-?\d+)"),
    re.compile(r"(?im)\bCommand exited with code\s*[:=]?\s*(-?\d+)"),
    re.compile(r"(?im)\bExit code\s*[:=]?\s*(-?\d+)"),
    re.compile(r"(?im)\bexit code\s*[:=]?\s*(-?\d+)"),
    re.compile(r"(?im)\breturned non[- ]zero exit status\s+(-?\d+)"),
)

_EXEC_NAMES = frozenset({"exec", "exec_command", "shell", "bash", "run"})
_READ_NAMES = frozenset({"read", "read_file", "readfile", "cat"})
_CALL_TYPES = frozenset({"custom_tool_call", "function_call"})
_OUTPUT_TYPES = frozenset({"custom_tool_call_output", "function_call_output"})
_READ_CHUNK_BYTES = 1 << 20
_REJECTION_EVENT_TYPES = frozenset(
    {"tool_call_rejected", "tool_rejected", "approval_rejected"}
)


@dataclass(frozen=True)
class NormalizedEvent:
    """One detector-facing event.

    The field names and semantics intentionally mirror the Claude parser.  In
    particular, ``exit_code`` is ``None`` if the rollout does not state an
    exit status, not an implicit success value.
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
class TokenUsage:
    """A positive delta derived from one cumulative Codex token snapshot."""

    source_line: int
    event_index: int
    timestamp: str | None = None
    session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    model_context_window: int | None = None


@dataclass(frozen=True)
class ParseResult:
    """Normalized output and byte progress from one rollout parse span."""

    session_id: str | None
    events: tuple[NormalizedEvent, ...]
    usage: tuple[TokenUsage, ...]
    next_offset: int = field(default=0, compare=False)
    bytes_consumed: int = field(default=0, compare=False)
    usage_max: tuple[TokenUsage, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class _PendingCall:
    name: str
    kind: str
    command: str | None
    source_line: int
    timestamp: str | None
    session_id: str | None
    cwd: str | None
    sidechain: bool


class CodexRolloutLineParser:
    """Parse complete Codex rollout records one line at a time."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.cwd: str | None = None
        self._pending: dict[str, _PendingCall] = {}
        self._pending_order: list[str] = []
        self._awaiting_correction = False
        self._last_usage: dict[str, int] = {}
        self._usage: list[TokenUsage] = []
        self._max_usage: dict[str, int] = {}
        self._max_usage_context: dict[str, object] = {}
        self._usage_keys: set[str] = set()
        self._message_ids: set[str] = set()
        self._max_cost_usd: float | None = None
        self._model: str | None = None
        self._first_timestamp: str | None = None
        self._last_timestamp: str | None = None

    @property
    def usage(self) -> tuple[TokenUsage, ...]:
        """Usage deltas observed so far."""

        return tuple(self._usage)

    # ``usage_deltas`` is a descriptive alias useful to callers that also
    # retain raw snapshots elsewhere.
    @property
    def usage_deltas(self) -> tuple[TokenUsage, ...]:
        return self.usage

    @property
    def usage_max(self) -> tuple[TokenUsage, ...]:
        """One row containing the component-wise maximum cumulative snapshot."""

        if not self._max_usage:
            return ()
        context = self._max_usage_context
        window = context.get("model_context_window")
        return (
            TokenUsage(
                source_line=int(context.get("source_line", 1)),
                event_index=0,
                timestamp=context.get("timestamp"),
                session_id=context.get("session_id"),
                input_tokens=self._max_usage.get("input_tokens", 0),
                output_tokens=self._max_usage.get("output_tokens", 0),
                cache_read_tokens=self._max_usage.get("cache_read_tokens", 0),
                cache_write_tokens=self._max_usage.get("cache_write_tokens", 0),
                reasoning_output_tokens=self._max_usage.get(
                    "reasoning_output_tokens", 0
                ),
                total_tokens=self._max_usage.get("total_tokens", 0),
                model_context_window=window if isinstance(window, int) else None,
            ),
        )

    @property
    def cumulative_usage(self) -> tuple[TokenUsage, ...]:
        return self.usage_max

    @property
    def max_usage(self) -> tuple[TokenUsage, ...]:
        return self.usage_max

    @property
    def message_count(self) -> int:
        return len(self._message_ids or self._usage_keys)

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def cost_usd(self) -> float | None:
        return self._max_cost_usd

    @property
    def total_cost_usd(self) -> float | None:
        return self._max_cost_usd

    @property
    def first_timestamp(self) -> str | None:
        return self._first_timestamp

    @property
    def last_timestamp(self) -> str | None:
        return self._last_timestamp

    def parse_line(self, line: str, source_line: int) -> list[NormalizedEvent]:
        """Parse one complete JSONL line, returning zero or more events."""

        stripped = line.strip()
        if not stripped:
            return []
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            # A producer commonly leaves an incomplete final line while the
            # rollout is being written.  Complete earlier lines remain useful.
            return []
        if not isinstance(record, dict):
            return []

        payload = record.get("payload")
        payload_dict = payload if isinstance(payload, dict) else {}
        self._update_context(record, payload_dict)
        context = self._context(record, payload_dict)
        record_type = record.get("type")

        if record_type == "response_item":
            return self._parse_response_item(payload_dict, source_line, context)
        if record_type == "event_msg":
            return self._parse_event_message(payload_dict, source_line, context)
        if record_type == "token_usage_record":
            self._consume_cost(payload_dict)
            self._consume_usage_snapshot(
                payload_dict.get("thread_token_usage")
                or payload_dict.get("usage"),
                payload_dict.get("model_context_window"),
                source_line,
                context,
                _usage_key(payload_dict, source_line),
            )
        return []

    def finish(self) -> list[NormalizedEvent]:
        """Flush unresolved shell calls with an unknown exit status."""

        events: list[NormalizedEvent] = []
        for call_id in self._pending_order:
            pending = self._pending[call_id]
            if pending.kind == KIND_RUN and pending.command is not None:
                events.append(self._run_event(pending, None, None, len(events)))
        self._pending.clear()
        self._pending_order.clear()
        return events

    def _update_context(self, record: dict[str, object], payload: dict[str, object]) -> None:
        session_id = _first_string(
            record.get("sessionId"),
            record.get("session_id"),
            payload.get("session_id"),
        )
        if session_id:
            self.session_id = session_id

        cwd = _first_string(record.get("cwd"), payload.get("cwd"))
        if cwd:
            self.cwd = cwd

        model = _first_string(record.get("model"), payload.get("model"))
        if model:
            self._model = model

        timestamp = _first_string(record.get("timestamp"), payload.get("timestamp"))
        if timestamp:
            if self._first_timestamp is None:
                self._first_timestamp = timestamp
            self._last_timestamp = timestamp

    def _context(
        self, record: dict[str, object], payload: dict[str, object]
    ) -> dict[str, object]:
        timestamp = _first_string(record.get("timestamp"), payload.get("timestamp"))
        sidechain = (
            record.get("isSidechain") is True
            or record.get("sidechain") is True
            or payload.get("is_sidechain") is True
            or payload.get("sidechain") is True
        )
        return {
            "timestamp": timestamp,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "sidechain": sidechain,
        }

    def _parse_response_item(
        self,
        payload: dict[str, object],
        source_line: int,
        context: dict[str, object],
    ) -> list[NormalizedEvent]:
        payload_type = payload.get("type")
        if payload_type == "message" and payload.get("role") == "assistant":
            message_id = _first_string(
                payload.get("id"), payload.get("message_id"), payload.get("response_id")
            )
            self._message_ids.add(message_id or f"line:{source_line}")
        if payload_type in _CALL_TYPES:
            return self._register_call(payload, source_line, context)
        if payload_type in _OUTPUT_TYPES:
            return self._consume_call_output(payload, source_line, context)

        role = payload.get("role")
        if role == "user":
            return self._consume_user_prompt(_content_texts(payload.get("content")), source_line, context)
        if role == "assistant":
            # A response item is an assistant event even when it is prose.
            # This is what makes "assistant spoke, then user spoke" distinct
            # from an actual correction after an interruption.
            self._awaiting_correction = False
        return []

    def _parse_event_message(
        self,
        payload: dict[str, object],
        source_line: int,
        context: dict[str, object],
    ) -> list[NormalizedEvent]:
        payload_type = payload.get("type")
        if payload_type == "token_count":
            info = payload.get("info")
            info_dict = info if isinstance(info, dict) else {}
            total = info_dict.get("total_token_usage")
            self._consume_cost(info_dict)
            self._consume_cost(payload)
            self._consume_usage_snapshot(
                total,
                info_dict.get("model_context_window"),
                source_line,
                context,
                _usage_key(payload, source_line),
            )
            return []

        if payload_type == "turn_aborted":
            self._awaiting_correction = True
            return [self._interrupt_event(source_line, context)]

        if payload_type in _REJECTION_EVENT_TYPES:
            text = _text_from_value(
                payload.get("message")
                or payload.get("reason")
                or payload.get("text")
                or payload.get("output")
            )
            return [
                NormalizedEvent(
                    kind=KIND_TOOL_REJECTED,
                    source_line=source_line,
                    event_index=0,
                    text=text,
                    tool_name=payload.get("name")
                    if isinstance(payload.get("name"), str)
                    else None,
                    **context,
                )
            ]

        # Current Codex writes the prompt as response_item/role=user.  Older
        # event_msg/user_message records are deliberately not treated as a
        # second prompt, but an exact interrupt sentinel is still meaningful.
        if payload_type == "user_message":
            text = _text_from_value(payload.get("message"))
            if text.strip() == _INTERRUPT_SENTINEL:
                self._awaiting_correction = True
                return [self._interrupt_event(source_line, context)]
        return []

    def _register_call(
        self,
        payload: dict[str, object],
        source_line: int,
        context: dict[str, object],
    ) -> list[NormalizedEvent]:
        name = payload.get("name") if isinstance(payload.get("name"), str) else ""
        raw_input = payload.get("input")
        if raw_input is None:
            raw_input = payload.get("arguments")
        kind, command = _call_kind_and_command(name, raw_input)
        call = _PendingCall(
            name=name,
            kind=kind,
            command=command,
            source_line=source_line,
            timestamp=context["timestamp"],  # type: ignore[arg-type]
            session_id=context["session_id"],  # type: ignore[arg-type]
            cwd=context["cwd"],  # type: ignore[arg-type]
            sidechain=context["sidechain"] is True,
        )
        call_id = _first_string(payload.get("call_id"), payload.get("id"))
        events: list[NormalizedEvent] = []
        if kind == KIND_FILE_READ:
            file_path = command or ""
            events.append(
                NormalizedEvent(
                    kind=KIND_FILE_READ,
                    source_line=source_line,
                    event_index=0,
                    text=file_path,
                    tool_name=name,
                    file_path=file_path or None,
                    **context,
                )
            )

        if call_id:
            self._pending[call_id] = call
            self._pending_order.append(call_id)
        elif kind == KIND_RUN and command is not None:
            # Without a call id no later output can be paired, so preserve the
            # same unknown-exit behavior as the Claude parser immediately.
            events.append(self._run_event(call, None, None, len(events)))
        self._awaiting_correction = False
        return events

    def _consume_call_output(
        self,
        payload: dict[str, object],
        source_line: int,
        context: dict[str, object],
    ) -> list[NormalizedEvent]:
        text = _text_from_value(payload.get("output"))
        call_id = _first_string(payload.get("call_id"), payload.get("id"))
        pending = self._remove_pending(call_id)

        if _matches_rejection(text):
            return [
                NormalizedEvent(
                    kind=KIND_TOOL_REJECTED,
                    source_line=source_line,
                    event_index=0,
                    text=text,
                    tool_name=pending.name if pending else None,
                    **context,
                )
            ]

        if pending is not None and pending.kind == KIND_RUN:
            exit_code, error_excerpt = _split_exit_code(text)
            if exit_code is None and _looks_like_error(text):
                error_excerpt = text or None
            return [
                self._run_event(pending, exit_code, error_excerpt, 0)
            ]

        if _looks_like_error(text):
            return [
                NormalizedEvent(
                    kind=KIND_TOOL_ERROR,
                    source_line=source_line,
                    event_index=0,
                    text=text,
                    tool_name=pending.name if pending else None,
                    **context,
                )
            ]
        return []

    def _consume_user_prompt(
        self,
        texts: list[str],
        source_line: int,
        context: dict[str, object],
    ) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        for text in texts:
            stripped = text.strip()
            if not stripped:
                continue
            if stripped == _INTERRUPT_SENTINEL:
                self._awaiting_correction = True
                events.append(self._interrupt_event(source_line, context, len(events)))
                continue
            if self._awaiting_correction:
                self._awaiting_correction = False
                events.append(
                    NormalizedEvent(
                        kind=KIND_USER_TURN_AFTER_CORRECTION,
                        source_line=source_line,
                        event_index=len(events),
                        text=text,
                        **context,
                    )
                )
                # One response_item is one prompt.  Ignore any additional
                # content blocks rather than creating duplicate corrections.
                break
        return events

    def _consume_cost(self, value: dict[str, object]) -> None:
        cost = _first_nonnegative_float(
            value,
            (
                "cost_usd",
                "costUSD",
                "cost",
                "total_cost_usd",
                "totalCostUSD",
                "total_cost",
                "totalCost",
            ),
        )
        if cost is not None:
            self._max_cost_usd = max(self._max_cost_usd or 0.0, cost)

    def _consume_usage_snapshot(
        self,
        snapshot: object,
        model_context_window: object,
        source_line: int,
        context: dict[str, object],
        usage_key: str,
    ) -> None:
        if not isinstance(snapshot, dict):
            return
        self._consume_cost(snapshot)
        current = _usage_counters(snapshot)
        if not current:
            return

        improved = any(
            value > self._max_usage.get(key, 0) for key, value in current.items()
        )
        for key, value in current.items():
            self._max_usage[key] = max(self._max_usage.get(key, 0), value)
        if improved or not self._max_usage_context:
            self._max_usage_context = {
                "source_line": source_line,
                "timestamp": context.get("timestamp"),
                "session_id": context.get("session_id"),
                "model_context_window": model_context_window,
            }
        self._usage_keys.add(usage_key)

        deltas: dict[str, int] = {}
        for key, value in current.items():
            previous = self._last_usage.get(key)
            deltas[key] = value if previous is None or value < previous else value - previous
        self._last_usage.update(current)
        if not any(deltas.values()):
            return

        window = model_context_window
        if not isinstance(window, int):
            window = None
        self._usage.append(
            TokenUsage(
                source_line=source_line,
                event_index=0,
                timestamp=context["timestamp"],  # type: ignore[arg-type]
                session_id=context["session_id"],  # type: ignore[arg-type]
                input_tokens=deltas.get("input_tokens", 0),
                output_tokens=deltas.get("output_tokens", 0),
                cache_read_tokens=deltas.get("cache_read_tokens", 0),
                cache_write_tokens=deltas.get("cache_write_tokens", 0),
                reasoning_output_tokens=deltas.get("reasoning_output_tokens", 0),
                total_tokens=deltas.get("total_tokens", 0),
                model_context_window=window,
            )
        )

    def _remove_pending(self, call_id: str | None) -> _PendingCall | None:
        if call_id is None:
            return None
        pending = self._pending.pop(call_id, None)
        if pending is not None:
            self._pending_order.remove(call_id)
        return pending

    @staticmethod
    def _run_event(
        pending: _PendingCall,
        exit_code: int | None,
        error_excerpt: str | None,
        index: int,
    ) -> NormalizedEvent:
        return NormalizedEvent(
            kind=KIND_RUN,
            source_line=pending.source_line,
            event_index=index,
            timestamp=pending.timestamp,
            session_id=pending.session_id,
            cwd=pending.cwd,
            sidechain=pending.sidechain,
            text=pending.command or "",
            tool_name=pending.name,
            command=pending.command,
            exit_code=exit_code,
            error_excerpt=error_excerpt,
        )

    @staticmethod
    def _interrupt_event(
        source_line: int,
        context: dict[str, object],
        index: int = 0,
    ) -> NormalizedEvent:
        return NormalizedEvent(
            kind=KIND_INTERRUPT,
            source_line=source_line,
            event_index=index,
            text=_INTERRUPT_SENTINEL,
            **context,
        )


# Short alias for callers that use the Claude parser's ``*LineParser`` naming
# convention without including the rollout format in the class name.
CodexLineParser = CodexRolloutLineParser


class CodexRolloutReader:
    """Retain parser state while reading successive rollout spans."""

    def __init__(self) -> None:
        self._line_parser = CodexRolloutLineParser()
        self._next_offset = 0
        self._next_source_line = 1

    @property
    def session_id(self) -> str | None:
        return self._line_parser.session_id

    @property
    def usage_max(self) -> tuple[TokenUsage, ...]:
        return self._line_parser.usage_max

    @property
    def message_count(self) -> int:
        return self._line_parser.message_count

    @property
    def model(self) -> str | None:
        return self._line_parser.model

    @property
    def cost_usd(self) -> float | None:
        return self._line_parser.cost_usd

    @property
    def first_timestamp(self) -> str | None:
        return self._line_parser.first_timestamp

    @property
    def last_timestamp(self) -> str | None:
        return self._line_parser.last_timestamp

    def parse(
        self,
        path: Path,
        last_offset: int = 0,
        *,
        end_offset: int | None = None,
    ) -> ParseResult:
        """Parse complete lines from ``last_offset`` without finalizing state."""

        if last_offset < 0:
            raise ValueError(f"parse start offset must be nonnegative: {last_offset}")
        if end_offset is not None and end_offset < last_offset:
            raise ValueError("parse end offset must not precede its start offset")

        events: list[NormalizedEvent] = []
        usage_offset = len(self._line_parser._usage)
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            limit = size if end_offset is None else min(size, end_offset)
            if last_offset > limit:
                raise ValueError(
                    f"parse start {last_offset} is past end of file {path} ({limit})"
                )
            if last_offset == self._next_offset:
                source_line = self._next_source_line
            else:
                source_line = _count_newlines(handle, last_offset) + 1
            handle.seek(last_offset)
            bytes_consumed = 0
            for raw_line in handle:
                if not raw_line.endswith(b"\n"):
                    break
                if end_offset is not None and last_offset + bytes_consumed + len(raw_line) > end_offset:
                    break
                bytes_consumed += len(raw_line)
                events.extend(
                    self._line_parser.parse_line(
                        raw_line.decode("utf-8", errors="replace"), source_line
                    )
                )
                source_line += 1

        next_offset = last_offset + bytes_consumed
        self._next_offset = next_offset
        self._next_source_line = source_line
        return ParseResult(
            session_id=self._line_parser.session_id,
            events=tuple(events),
            usage=tuple(self._line_parser._usage[usage_offset:]),
            next_offset=next_offset,
            bytes_consumed=bytes_consumed,
            usage_max=self._line_parser.usage_max,
        )

    def finish(self) -> list[NormalizedEvent]:
        """Finalize unresolved calls at the end of the rollout."""

        return self._line_parser.finish()


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _content_texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    texts: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"input_text", "output_text", "text"}:
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    return texts


def _text_from_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            text
            for item in value
            for text in [_text_from_value(item)]
            if text
        )
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        for key in ("content", "output", "message"):
            if key in value:
                text = _text_from_value(value[key])
                if text:
                    return text
    return ""


def _call_kind_and_command(name: str, raw_input: object) -> tuple[str, str | None]:
    name_lower = name.casefold()
    parsed = raw_input
    if isinstance(raw_input, str):
        try:
            parsed_json = json.loads(raw_input)
        except (TypeError, json.JSONDecodeError):
            parsed_json = None
        if isinstance(parsed_json, dict):
            parsed = parsed_json

    if name_lower in _EXEC_NAMES:
        if isinstance(parsed, dict):
            command = _first_string(parsed.get("cmd"), parsed.get("command"))
        elif isinstance(parsed, str):
            command = parsed
        else:
            command = None
        return KIND_RUN, command

    if name_lower in _READ_NAMES:
        if isinstance(parsed, dict):
            path = _first_string(
                parsed.get("file_path"), parsed.get("path"), parsed.get("file")
            )
        elif isinstance(parsed, str):
            path = parsed
        else:
            path = None
        return KIND_FILE_READ, path

    return "tool", None


def _split_exit_code(text: str) -> tuple[int | None, str | None]:
    for pattern in _EXIT_CODE_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        exit_code = int(match.group(1))
        if exit_code == 0:
            return exit_code, None
        excerpt = text[match.end() :].lstrip(" \t\r\n:-").strip()
        return exit_code, (excerpt or text.strip() or None)
    return None, None


def _matches_rejection(text: str) -> bool:
    folded = text.casefold()
    return any(sentinel.casefold() in folded for sentinel in _REJECTION_SENTINELS)


def _looks_like_error(text: str) -> bool:
    if not text:
        return False
    if _matches_rejection(text):
        return False
    folded = text.casefold()
    return any(
        marker in folded
        for marker in (
            "<tool_use_error>",
            "tool call failed",
            "tool execution failed",
            "command failed",
            "permission denied",
            "error:",
            "error ",
            "\nerror:",
            "\nerror ",
        )
    )


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


def _usage_key(payload: dict[str, object], source_line: int) -> str:
    value = _first_string(
        payload.get("response_id"),
        payload.get("responseId"),
        payload.get("turn_id"),
        payload.get("turnId"),
        payload.get("id"),
    )
    return value or f"line:{source_line}"


def _usage_counters(snapshot: dict[str, object]) -> dict[str, int]:
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens"),
        "output_tokens": ("output_tokens", "outputTokens"),
        "cache_read_tokens": (
            "cache_read_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
        ),
        "cache_write_tokens": (
            "cache_write_tokens",
            "cache_write_input_tokens",
            "cache_creation_input_tokens",
        ),
        "reasoning_output_tokens": ("reasoning_output_tokens", "reasoningTokens"),
        "total_tokens": ("total_tokens", "totalTokens"),
    }
    counters: dict[str, int] = {}
    for normalized, keys in aliases.items():
        for key in keys:
            value = snapshot.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counters[normalized] = value
                break
    return counters


def _count_newlines(handle: BinaryIO, limit: int) -> int:
    count = 0
    remaining = limit
    while remaining > 0:
        chunk = handle.read(min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        count += chunk.count(b"\n")
        remaining -= len(chunk)
    return count


def parse_rollout(
    path: Path,
    last_offset: int = 0,
    *,
    reader: CodexRolloutReader | None = None,
) -> ParseResult:
    """Parse one rollout span, finalizing it unless a retained reader is given."""

    retained_reader = reader is not None
    rollout_reader = reader or CodexRolloutReader()
    result = rollout_reader.parse(path, last_offset)
    if retained_reader:
        return result
    return ParseResult(
        session_id=result.session_id,
        events=result.events + tuple(rollout_reader.finish()),
        usage=result.usage,
        next_offset=result.next_offset,
        bytes_consumed=result.bytes_consumed,
        usage_max=result.usage_max,
    )


def iter_events(path: Path) -> Iterator[NormalizedEvent]:
    """Yield normalized detector events from a Codex rollout."""

    yield from parse_rollout(path).events


def iter_usage(path: Path) -> Iterator[TokenUsage]:
    """Yield positive token deltas from cumulative rollout snapshots."""

    yield from parse_rollout(path).usage


def iter_max_usage(path: Path) -> Iterator[TokenUsage]:
    """Yield the component-wise maximum cumulative snapshot in a rollout."""

    reader = CodexRolloutReader()
    reader.parse(path)
    yield from reader.usage_max


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("module is library-only; import codex_reader")
