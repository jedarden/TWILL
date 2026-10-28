"""Conservative, source-linked execution episodes (plan §9B).

This projection uses only the persisted, redacted interaction contract. It is
replaced in the same transaction as that contract. It records what was said and
what remains unknown; it never turns assistant prose or a present-day rule file
into historical permission.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field


EPISODE_SCHEMA = "twill-execution-episode/v1"
PROJECTION_VERSION = "episode-projection/v1"

_STATUS = re.compile(r"^(?:status(?: update)?|progress(?: update)?|how(?:'s| is) (?:it|the work|the progress)|where (?:are we|does (?:it|this) stand))[?.! ]*$", re.I)
_STATUS_TARGET = re.compile(r"^(?:what(?:'s| is) the status of|status of|progress on|any update on) (.+?)[?.! ]*$", re.I)
_CANCEL = re.compile(r"^(?:cancel|stop|abandon)(?: this| that| the work| the task| the outcome)?[.! ]*$", re.I)
_TARGETED_CANCEL = re.compile(r"^(?:cancel|stop|abandon) (.+?)[.! ]*$", re.I)
_DONE = re.compile(r"^(?:this|that|the work|the task|the outcome) is (?:done|complete|finished)[.! ]*$", re.I)
_REPLACE = re.compile(r"^(?:instead|replace (?:this|that|the task) with|switch to|change (?:the )?scope to)[: ,]+(.+)$", re.I)
_RESUME = re.compile(r"^(?:resume|continue) (?:work on |working on |the |this )?(.+?)[.! ]*$", re.I)
_ACTION = re.compile(r"^(?:please )?(implement|build|fix|write|create|update|deploy|test|review|prepare|finish|publish|remove|delete|migrate)\s+(.+?)[.! ]*$", re.I)
_APPROVAL = re.compile(r"^(?:yes[,; ]+)?(?:approved|authorized|go ahead|proceed)(?:[: ,]+)(.+?)[.! ]*$", re.I)
_QUESTION_ACTION = re.compile(r"^(?:may|should|can) (?:i|we|you)\s+(.+?)[? ]*$", re.I)
_ADVICE = re.compile(r"^(?:how (?:do|should|could|would)|what (?:is|are|would)|explain|describe|compare|suggest|advise)\b", re.I)
_RESTRICT = re.compile(r"\b(?:do not|don't|must not|only|without|except|within|limit(?:ed)? to)\b", re.I)
_WAIT = re.compile(r"\b(?:wait for|waiting for|until (?:ci|the build|the test|approval|a response|the provider))\b", re.I)
_DECISION = re.compile(r"^(?:decision|use|choose|select|the answer is)[: ,]+(.+)$", re.I)
_HOLD = re.compile(r"\b(?:proposal.pending|human approval required|permission hold|approval withheld|do not proceed|only after approval|pending approval)\b", re.I)
_EFFECTIVE = re.compile(r"^instruction effective (\d{4}-\d{2}-\d{2})[: ,]+(.+)$", re.I)


def _normalized(text: str) -> str:
    return " ".join(text.casefold().strip().split()).rstrip(".!? ")


def action_key(text: str) -> str | None:
    """Exact verb/object equivalence; semantic paraphrases remain unknown."""
    match = _ACTION.fullmatch(text.strip())
    if not match:
        return None
    return f"{match[1].casefold()}:{_normalized(match[2])}"


def action_keys(text: str) -> tuple[str, ...]:
    """Separate only explicitly repeated verb/object clauses."""
    parts = re.split(r"\s+and\s+", text.strip(), flags=re.I)
    if len(parts) > 1:
        keys = tuple(action_key(part) for part in parts)
        if all(keys):
            return keys  # type: ignore[return-value]
    key = action_key(text)
    return (key,) if key else ()


@dataclass
class Episode:
    start: int
    end: int
    thread_id: str | None
    objective: str
    action: str | None
    authority: str
    context: str = "known"
    historical_policy: str = "unknown"
    lifecycle: str = "active"
    advice_only: bool = False
    assertions: list[tuple[str, str | None, str, int | None, int | None, int | None, str | None]] = field(default_factory=list)

    def add(self, kind: str, value: str | None, strength: str, row: dict | None = None,
            related: int | None = None, detail: str | None = None) -> None:
        self.assertions.append((kind, value, strength,
                                row["ordinal"] if row else None,
                                row["source_line"] if row else None, related, detail))


_COLUMNS = ("ordinal", "source_line", "event_index", "locator", "ts_utc",
            "thread_id", "message_id", "parent_ordinal", "call_ordinal",
            "origin", "sidechain", "text", "truncated", "terminal_state",
            "history_confirmed", "question_ordinal", "source_identity",
            "source_offset", "contract_version")


def _rows(connection: sqlite3.Connection, session_key: str) -> list[dict]:
    return [dict(zip(_COLUMNS, values)) for values in connection.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM interaction WHERE session_key=? ORDER BY ordinal",
        (session_key,),
    )]


def _hash(rows: list[dict], summary: tuple | None) -> str:
    payload = json.dumps([PROJECTION_VERSION, rows, summary], sort_keys=True,
                         ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _new(row: dict, *, objective: str, action: str | None, authority: str,
         advice: bool = False) -> Episode:
    episode = Episode(row["ordinal"], row["ordinal"], row["thread_id"], objective,
                      action, authority, advice_only=advice)
    episode.add("objective", objective, "known", row)
    episode.add("historical_policy", None, "unknown", row,
                detail="no captured effective-time instruction")
    if advice:
        episode.add("advice_only", None, "known", row)
    elif action is None:
        episode.context = "unknown"
        episode.add("action_scope", None, "unknown", row, detail="no exact action key")
    else:
        for pending in action.split(" | "):
            episode.add("pending_action", pending, "known", row)
            episode.add("authorization", pending, "known", row)
    return episode


def _derive(rows: list[dict], summary: tuple | None) -> list[Episode]:
    episodes: list[Episode] = []
    active: Episode | None = None
    questions: dict[int, tuple[Episode, str | None]] = {}
    for row in rows:
        text = (row["text"] or "").strip()
        origin = row["origin"]
        if row["sidechain"]:
            if active:
                active.add("sidechain_handoff", text or None, "inferred", row,
                           detail="delegated content cannot grant main-thread authority")
            continue
        if origin in {"harness", "quoted_history"}:
            if active and _HOLD.search(text):
                active.add("quoted_or_harness_hold", text, "unknown", row)
                active.context = "unknown"
            continue
        if origin == "human_request":
            effective = _EFFECTIVE.fullmatch(text)
            if effective and active:
                active.end = row["ordinal"]
                if effective[1] <= row["ts_utc"][:10]:
                    active.historical_policy = "captured"
                    active.add("historical_instruction", effective[2][:240], "known", row,
                               detail=effective[1])
                else:
                    active.add("historical_instruction", effective[2][:240], "unknown", row,
                               detail="effective time follows source time")
                continue
            if active and not action_keys(text):
                if decision := _DECISION.fullmatch(text):
                    active.end = row["ordinal"]
                    active.add("settled_decision", decision[1][:240], "known", row)
                    continue
                if _HOLD.search(text) or _RESTRICT.match(text) or _WAIT.match(text):
                    active.end = row["ordinal"]
                    if _RESTRICT.search(text):
                        active.add("scope_restriction", text, "known", row)
                    if _HOLD.search(text):
                        active.authority = "withheld"
                        active.add("permission_hold", text, "known", row)
                    if _WAIT.search(text):
                        active.add("external_wait", text, "known", row)
                    continue
            status_target = _STATUS_TARGET.fullmatch(text)
            pending_objects = (
                {_normalized(key.split(":", 1)[1]) for key in active.action.split(" | ")}
                if active and active.action else set()
            )
            if (_STATUS.fullmatch(text) or
                    (status_target and active and _normalized(status_target[1]) in
                     pending_objects | {_normalized(active.objective)})):
                if active:
                    active.end = row["ordinal"]
                    active.add("status_exchange", text, "known", row)
                continue
            if _CANCEL.fullmatch(text):
                if active:
                    active.end = row["ordinal"]
                    active.lifecycle = "cancelled"
                    active.authority = "revoked"
                    active.add("revocation", text, "known", row)
                    active = None
                continue
            targeted_cancel = _TARGETED_CANCEL.fullmatch(text)
            if targeted_cancel and active:
                pending = tuple(active.action.split(" | ")) if active.action else ()
                if (action_key(targeted_cancel[1]) in pending
                        or _normalized(targeted_cancel[1]) == _normalized(active.objective)):
                    active.end = row["ordinal"]
                    active.lifecycle = "cancelled"
                    active.authority = "revoked"
                    active.add("revocation", text, "known", row)
                    active = None
                    continue
            if _DONE.fullmatch(text):
                if active:
                    active.end = row["ordinal"]
                    active.lifecycle = "completed"
                    active.add("observed_completion", text, "known", row,
                               detail="human reported completion")
                    active = None
                continue
            replacement = _REPLACE.fullmatch(text)
            if replacement and active:
                active.end = row["ordinal"]
                active.lifecycle = "superseded"
                active.authority = "revoked"
                active.add("scope_change", text, "known", row)
                text = replacement[1].strip()
                active = None
            resume = _RESUME.fullmatch(text)
            if resume and active:
                proposed = action_key(resume[1]) or action_key(text.removeprefix("Resume ").removeprefix("Continue "))
                pending = tuple(active.action.split(" | ")) if active.action else ()
                if proposed in pending and active.authority == "authorized":
                    active.end = row["ordinal"]
                    active.add("same_outcome_resume", text, "known", row)
                    continue
                if _normalized(resume[1]) == _normalized(active.objective):
                    active.end = row["ordinal"]
                    active.add("same_outcome_resume", text, "known", row)
                    continue
                # Generic continuation or uncertain equivalence cannot add authority.
                if _normalized(resume[1]) in {"", "work", "task", "it", "this"}:
                    active.end = row["ordinal"]
                    active.add("generic_continuation", text, "unknown", row)
                    continue
            if _normalized(text) in {"continue", "resume", "go on"} and active:
                active.end = row["ordinal"]
                active.add("generic_continuation", text, "unknown", row)
                continue
            approval = _APPROVAL.fullmatch(text)
            approved_action = action_key(approval[1]) if approval else None
            pending = tuple(active.action.split(" | ")) if active and active.action else ()
            if active and approved_action and approved_action in pending:
                active.end = row["ordinal"]
                if len(pending) == 1:
                    active.authority = "authorized"
                active.add("explicit_approval", approved_action, "known", row)
                continue
            if active:
                active.lifecycle = "superseded"
                active.end = row["ordinal"]
                active.add("new_request_boundary", text, "known", row)
            candidate = approval[1] if approval else text
            keys = action_keys(candidate)
            action = " | ".join(keys) if keys else None
            advice = bool(_ADVICE.match(text)) and not approval
            authority = "authorized" if action and not advice else "unknown"
            active = _new(row, objective=text[:240], action=action,
                          authority=authority, advice=advice)
            episodes.append(active)
            if approval:
                active.add("explicit_approval", candidate[:240],
                           "known" if action else "unknown", row)
        elif origin == "question" and active:
            active.end = row["ordinal"]
            question_match = _QUESTION_ACTION.fullmatch(text)
            question_action = action_key(question_match[1]) if question_match else None
            questions[row["ordinal"]] = (active, question_action)
            active.add("outstanding_question", text or None, "known", row)
        elif origin == "human_answer":
            link = row["question_ordinal"]
            target = questions.get(link) if link is not None else None
            if target:
                episode, question_action = target
                episode.end = max(episode.end, row["ordinal"])
                episode.add("question_answer", text or None, "known", row, related=link)
                if _normalized(text) in {"yes", "approved", "go ahead", "proceed"} and question_action:
                    pending = tuple(episode.action.split(" | ")) if episode.action else ()
                    if question_action in pending and episode.authority != "revoked":
                        if len(pending) == 1:
                            episode.authority = "authorized"
                        episode.add("explicit_approval", question_action, "known", row, related=link)
                    else:
                        episode.add("approval_scope", question_action, "unknown", row, related=link)
                if (_normalized(text) in {"no", "do not proceed", "not approved"}
                        and question_action
                        and question_action in (episode.action or "").split(" | ")
                        and episode.lifecycle == "active"):
                    episode.authority = "withheld"
                    episode.add("approval_withheld", text, "known", row, related=link)
            elif active:
                active.add("unmatched_answer", text or None, "unknown", row, related=link)
                active.context = "unknown"
        elif active:
            active.end = row["ordinal"]
            if origin == "assistant_text":
                if row["terminal_state"] != "none":
                    active.add("terminal_reply", text or None,
                               "known" if row["terminal_state"] == "completed" else "unknown",
                               row, detail=row["terminal_state"])
                if _WAIT.search(text):
                    active.add("assistant_wait_claim", text or None, "inferred", row)
            elif origin == "tool_result" and _WAIT.search(text):
                active.add("external_wait_evidence", text or None, "inferred", row)
        if active and origin in {"human_request", "human_answer"}:
            if _RESTRICT.search(text):
                active.add("scope_restriction", text, "known", row)
            if _HOLD.search(text):
                active.authority = "withheld"
                active.add("permission_hold", text, "known", row)
            if _WAIT.search(text):
                active.add("external_wait", text, "known", row)
            if decision := _DECISION.fullmatch(text):
                active.add("settled_decision", decision[1][:240], "known", row)
        if active and row["truncated"]:
            active.context = "unknown"
            active.add("material_truncation", None, "unknown", row)
    if summary and (summary[0] or summary[3]):
        for episode in episodes:
            episode.context = "unknown"
            if summary[0]:
                episode.add("omitted_interactions", str(summary[0]), "unknown", detail="session cap")
            if summary[3]:
                episode.add("truncated_interactions", str(summary[3]), "unknown")
    for episode in episodes:
        if not any(item[0] == "terminal_reply" for item in episode.assertions):
            episode.add("terminality", None, "unknown", detail="no source-native completed reply")
    return episodes


def project_session(connection: sqlite3.Connection, session_key: str) -> int:
    """Replace one session projection; caller owns the transaction."""
    rows = _rows(connection, session_key)
    summary = connection.execute(
        "SELECT omitted_over_cap, omitted_first_ordinal, omitted_last_ordinal, truncated_fields "
        "FROM interaction_summary WHERE session_key=?", (session_key,),
    ).fetchone()
    input_sha = _hash(rows, summary)
    connection.execute("DELETE FROM episode_assertion WHERE episode_id IN "
                       "(SELECT episode_id FROM execution_episode WHERE session_key=?)", (session_key,))
    connection.execute("DELETE FROM execution_episode WHERE session_key=?", (session_key,))
    for episode in _derive(rows, summary):
        episode_id = hashlib.sha256(
            f"{session_key}:{episode.start}:{PROJECTION_VERSION}".encode()
        ).hexdigest()[:32]
        connection.execute(
            "INSERT INTO execution_episode VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (episode_id, session_key, EPISODE_SCHEMA, input_sha,
             episode.start, episode.end, episode.thread_id, episode.objective,
             episode.action, episode.authority, episode.context, episode.historical_policy,
             episode.lifecycle, int(episode.advice_only)),
        )
        for index, assertion in enumerate(episode.assertions):
            assertion_id = hashlib.sha256(f"{episode_id}:{index}".encode()).hexdigest()[:32]
            connection.execute(
                "INSERT INTO episode_assertion VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (assertion_id, episode_id, *assertion),
            )
    return len(rows)


def project_all(connection: sqlite3.Connection) -> None:
    """Rebuild after redaction or retention changes, including emptied sessions."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='execution_episode'").fetchone():
        return
    keys = {row[0] for row in connection.execute("SELECT session_key FROM interaction_summary")}
    keys.update(row[0] for row in connection.execute("SELECT DISTINCT session_key FROM execution_episode"))
    for key in sorted(keys):
        project_session(connection, key)


def ensure_current(connection: sqlite3.Connection) -> None:
    """Backfill existing 9A state once per projector version."""
    row = connection.execute("SELECT value FROM meta WHERE key='episode_projection_version'").fetchone()
    if row and row[0] == PROJECTION_VERSION:
        return
    with connection:
        project_all(connection)
        connection.execute(
            "INSERT INTO meta(key, value, updated_at) "
            "VALUES ('episode_projection_version', ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (PROJECTION_VERSION,),
        )
