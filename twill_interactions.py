"""The versioned interaction contract for Phase 9A (plan §7.1.1).

The six normalized event kinds stay the detector-facing contract they always
were (docs/notes/event-contract.md).  This module is the *separate* record the
execution-continuity layers consume: one ``Interaction`` per conversation
element a Claude or Codex transcript actually carries — ordinary human
requests, assistant prose, tool calls and results, interactive questions and
their real answers (including answers delivered inside a tool result),
harness bookkeeping and quoted history.

The contract is versioned (:data:`INTERACTION_SCHEMA`) and additive to the
event contract: nothing here changes what a parser emits as events, and no
detector keys on interactions.  Free text travels unredacted and unbounded
this far — the persistence boundary in ``twill_app`` redacts before it binds
and cuts to 240 decoded characters afterwards, exactly as it does for
excerpts.  A stored row whose text had to be cut carries ``truncated=True``,
because a truncation that hides scope, authority or a disconfirmer is a
material omission (plan §7.1.1), not a cosmetic one.

Terminality is deliberately conservative: only a source-native terminal
record (Codex ``task_complete``, an explicit abort) or the optional
operator-configured history adapter (``twill_history``) may mark a turn
``completed``.  An assistant reply that merely looks final — including a
source record stamped ``phase=final_answer`` — stays ``unknown``.
"""

from __future__ import annotations

import dataclasses

#: Version stamped on every persisted interaction row's contract.
INTERACTION_SCHEMA = "twill-interaction/v1"

#: Deterministic per-session retention cap (plan §7.1.1).  Interactions past
#: the cap are not stored; their count is published beside the retained ones.
MAX_INTERACTIONS_PER_SESSION = 1000

#: Free-text bound applied after redaction at the persistence boundary.
MAX_TEXT_LENGTH = 240

# -- origin vocabulary -------------------------------------------------------

#: A real human request that opens a turn.
ORIGIN_HUMAN_REQUEST = "human_request"
#: A real interactive answer to a pending question — as a follow-up turn or
#: inside a tool result.
ORIGIN_HUMAN_ANSWER = "human_answer"
#: Assistant prose.
ORIGIN_ASSISTANT_TEXT = "assistant_text"
#: A tool invocation issued by the agent.
ORIGIN_TOOL_CALL = "tool_call"
#: A tool result, when it is not an interactive answer.
ORIGIN_TOOL_RESULT = "tool_result"
#: A question the agent asks the human through a question tool.
ORIGIN_QUESTION = "question"
#: Harness bookkeeping: metadata turns, interrupt/abort sentinels, refusal
#: wrappers — written by the harness, not typed by a human.
ORIGIN_HARNESS = "harness"
#: Quoted history: command wrappers, injected context blocks — text present
#: in the transcript but not an original statement of this session.
ORIGIN_QUOTED_HISTORY = "quoted_history"

ORIGINS = (
    ORIGIN_HUMAN_REQUEST,
    ORIGIN_HUMAN_ANSWER,
    ORIGIN_ASSISTANT_TEXT,
    ORIGIN_TOOL_CALL,
    ORIGIN_TOOL_RESULT,
    ORIGIN_QUESTION,
    ORIGIN_HARNESS,
    ORIGIN_QUOTED_HISTORY,
)

# -- actor vocabulary --------------------------------------------------------

ACTOR_USER = "user"
ACTOR_ASSISTANT = "assistant"
ACTOR_TOOL = "tool"
ACTOR_SYSTEM = "system"

ACTORS = (ACTOR_USER, ACTOR_ASSISTANT, ACTOR_TOOL, ACTOR_SYSTEM)

# -- terminal-state vocabulary ------------------------------------------------

#: The record is not a turn-final candidate at all.
TERMINAL_NONE = "none"
#: A source-native terminal record (or the confirmed history adapter) proved
#: the turn completed with this reply.
TERMINAL_COMPLETED = "completed"
#: The turn ended in an explicit interruption or abort.
TERMINAL_INTERRUPTED = "interrupted"
#: The reply looks turn-final but nothing in the source proves completion.
TERMINAL_UNKNOWN = "unknown"

TERMINAL_STATES = (
    TERMINAL_NONE,
    TERMINAL_COMPLETED,
    TERMINAL_INTERRUPTED,
    TERMINAL_UNKNOWN,
)


def validate_vocabulary() -> None:
    """Fail at import when a vocabulary constant drifts out of its tuple."""

    for name, value in (
        ("ORIGIN_HUMAN_REQUEST", ORIGIN_HUMAN_REQUEST),
        ("ORIGIN_HUMAN_ANSWER", ORIGIN_HUMAN_ANSWER),
        ("ORIGIN_ASSISTANT_TEXT", ORIGIN_ASSISTANT_TEXT),
        ("ORIGIN_TOOL_CALL", ORIGIN_TOOL_CALL),
        ("ORIGIN_TOOL_RESULT", ORIGIN_TOOL_RESULT),
        ("ORIGIN_QUESTION", ORIGIN_QUESTION),
        ("ORIGIN_HARNESS", ORIGIN_HARNESS),
        ("ORIGIN_QUOTED_HISTORY", ORIGIN_QUOTED_HISTORY),
    ):
        if value not in ORIGINS:
            raise ValueError(f"{name}={value!r} is not a member of ORIGINS")
    for name, value in (
        ("TERMINAL_NONE", TERMINAL_NONE),
        ("TERMINAL_COMPLETED", TERMINAL_COMPLETED),
        ("TERMINAL_INTERRUPTED", TERMINAL_INTERRUPTED),
        ("TERMINAL_UNKNOWN", TERMINAL_UNKNOWN),
    ):
        if value not in TERMINAL_STATES:
            raise ValueError(f"{name}={value!r} is not a member of TERMINAL_STATES")
    for name, value in (
        ("ACTOR_USER", ACTOR_USER),
        ("ACTOR_ASSISTANT", ACTOR_ASSISTANT),
        ("ACTOR_TOOL", ACTOR_TOOL),
        ("ACTOR_SYSTEM", ACTOR_SYSTEM),
    ):
        if value not in ACTORS:
            raise ValueError(f"{name}={value!r} is not a member of ACTORS")


validate_vocabulary()


@dataclasses.dataclass(frozen=True)
class Interaction:
    """One source-linked conversation element (plan §7.1.1).

    ``ordinal`` is the 0-based position in the session's interaction
    sequence, assigned by the parser; links between interactions
    (``parent_ordinal``, ``call_ordinal``, ``question_ordinal``) reference
    these ordinals, so the same source bytes always derive the same linked
    graph regardless of row ids.  ``text`` is still unredacted here — the
    persistence boundary owns redaction and the 240-character bound.
    """

    origin: str
    actor: str
    source_line: int
    event_index: int = 0
    ordinal: int = 0
    timestamp: str | None = None
    session_id: str | None = None
    thread_id: str | None = None
    cwd: str | None = None
    sidechain: bool = False
    channel: str | None = None
    turn_id: int | None = None
    message_id: str | None = None
    parent_ordinal: int | None = None
    call_ordinal: int | None = None
    question_ordinal: int | None = None
    locator: str | None = None
    terminal_state: str = TERMINAL_NONE
    truncated: bool = False
    history_confirmed: bool = False
    text: str = ""

    def __post_init__(self) -> None:
        if self.origin not in ORIGINS:
            raise ValueError(f"unknown interaction origin: {self.origin!r}")
        if self.actor not in ACTORS:
            raise ValueError(f"unknown interaction actor: {self.actor!r}")
        if self.terminal_state not in TERMINAL_STATES:
            raise ValueError(
                f"unknown interaction terminal state: {self.terminal_state!r}"
            )
        if self.source_line < 1:
            raise ValueError("interaction source_line must be 1-based")


def reterminalize(
    interactions: tuple[Interaction, ...],
    index: int,
    terminal_state: str,
) -> None:
    """Upgrade one interaction's terminal state in place in a mutable list.

    Only the transitions the contract allows are applied: an explicit state
    (completed/interrupted) is never downgraded, and ``none`` — the
    not-turn-final marker — is only replaced by a state, never by ``none``.
    """

    if not 0 <= index < len(interactions):
        return
    current = interactions[index]
    if current.terminal_state not in (TERMINAL_NONE, TERMINAL_UNKNOWN):
        return
    if terminal_state == TERMINAL_NONE:
        return
    interactions[index] = dataclasses.replace(current, terminal_state=terminal_state)


def apply_completed_history(
    interactions: tuple[Interaction, ...],
    completed_keys: frozenset[tuple[str, str]],
) -> tuple[Interaction, ...]:
    """Mark unknown-terminal assistant replies confirmed by the history adapter.

    ``completed_keys`` holds ``(session_id, message_id)`` pairs the adapter
    proved completed.  Only ``unknown`` rows move — a source-native
    ``interrupted`` is never overridden, and rows without a provider message
    id can never match.  Returns the (possibly unchanged) tuple and leaves
    the count of confirmed rows to :func:`count_confirmed`.
    """

    if not completed_keys:
        return interactions
    changed: list[Interaction] = []
    for interaction in interactions:
        key = (interaction.session_id or "", interaction.message_id or "")
        if (
            interaction.terminal_state == TERMINAL_UNKNOWN
            and interaction.message_id
            and key in completed_keys
        ):
            interaction = dataclasses.replace(
                interaction, terminal_state=TERMINAL_COMPLETED,
                history_confirmed=True,
            )
        changed.append(interaction)
    return tuple(changed)


def count_confirmed(
    interactions: tuple[Interaction, ...],
    completed_keys: frozenset[tuple[str, str]],
) -> int:
    """Count unknown→completed upgrades :func:`apply_completed_history` would make."""

    return sum(
        1
        for interaction in interactions
        if interaction.terminal_state == TERMINAL_UNKNOWN
        and interaction.message_id
        and (interaction.session_id or "", interaction.message_id) in completed_keys
    )


def sort_key(interaction: Interaction) -> tuple[int, int, int]:
    """Source order of one interaction: line, then block, then ordinal."""

    return (interaction.source_line, interaction.event_index, interaction.ordinal)
