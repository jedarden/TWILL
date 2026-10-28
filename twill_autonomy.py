"""Deterministic, conservative Phase 9C autonomy candidate assessments.

This is a derived projection. Phrase shapes only shortlist an episode; source
linked authority, terminality and disconfirmers decide whether it can support
registry membership. An unchanged replay retains the same assessment bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass

from twill_episodes import action_key


ANALYZER_VERSION = "autonomy-analyzer/v1"
DETECTOR_VERSION = 1
FAMILIES = ("D-12", "D-13", "D-14", "D-15", "D-16")
MAX_REFERENCES = 24

# Deliberately narrow. A natural-language resemblance is a candidate, never
# evidence of permission, completion, or ability under matching conditions.
_OFFER = re.compile(
    r"\b(?:I can|would you like me to|shall I)\s+"
    r"(?P<action>(?:implement|build|fix|write|create|update|deploy|test|review|prepare|finish|publish|remove|delete|migrate)\s+[^?.;]+?)"
    r"\s*(?:after (?:another|your|new) approval|if you (?:want|approve)|\?)",
    re.I,
)
_APPROVAL_OFFER = re.compile(r"\b(?:after (?:another|your|new) approval|approve .* again|if you approve)\b", re.I)
_DEFECT = re.compile(r"\b(?:found|acknowledge|identified|confirmed) (?:a |the )?(?:defect|bug|failure)\b", re.I)
_ABANDON = re.compile(r"\b(?:I am abandoning|I will abandon|I have stopped working on)\s+(?P<action>[^.;]+)", re.I)
_BLOCK = re.compile(r"\bI cannot\s+(?P<action>(?:implement|build|fix|write|create|update|deploy|test|review|prepare|finish|publish|remove|delete|migrate)\s+[^.;]+?)\s+under current access\b", re.I)
_CAPABLE = re.compile(r"\bcapability check:\s*(?P<action>[^.;]+?)\s+available under current access\b", re.I)
_SUCCESS = re.compile(r"\b(?:exit code|process exited with code)\s*:?\s*0\b", re.I)
_COMPLETED = re.compile(r"\b(?:completed|finished|I (?:built|fixed|wrote|created|updated|deployed|tested|reviewed|published)|already (?:built|fixed|wrote|created|updated|deployed|tested|reviewed|published))\b", re.I)
_CHANGED = re.compile(r"\b(?:new credentials|changed access|new approval|different permissions|after approval)\b", re.I)
_EXTERNAL = re.compile(r"\b(?:purchase|payment|physical intervention|onsite access|external approval)\b", re.I)


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                   separators=(",", ":")).encode()).hexdigest()


def _cluster_key(family: str, action: str) -> str:
    verb, resource = action.split(":", 1)
    words = [word for word in re.findall(r"[a-z0-9]+", resource.casefold())
             if word not in {"a", "an", "the", "my", "our", "this", "that"}]
    resource_class = words[0] if words else "unspecified"
    return f"autonomy:{family}:{verb}:{resource_class}"


def _ref(kind: str, ordinal: int | None, line: int | None,
         related: int | None = None) -> dict[str, object]:
    ref = {"kind": kind, "ordinal": ordinal, "source_line": line}
    if related is not None:
        ref["related_ordinal"] = related
    return ref


@dataclass(frozen=True)
class Candidate:
    family: str
    action: str
    source_ordinal: int
    source_line: int
    text: str


def _shortlist(rows: list[dict], pending: set[str]) -> list[Candidate]:
    found: list[Candidate] = []
    for row in rows:
        if row["sidechain"] or row["origin"] != "assistant_text":
            continue
        text = row["text"] or ""
        offer = _OFFER.search(text)
        offered = action_key(offer["action"]) if offer else None
        if offered and offered in pending:
            found.append(Candidate("D-12", offered, row["ordinal"], row["source_line"], text))
            if _APPROVAL_OFFER.search(text):
                found.append(Candidate("D-13", offered, row["ordinal"], row["source_line"], text))
            if _DEFECT.search(text) and _impeded_action(text) == offered:
                found.append(Candidate("D-14", offered, row["ordinal"], row["source_line"], text))
        abandon = _ABANDON.search(text)
        abandoned = action_key(abandon["action"]) if abandon else None
        if abandoned and abandoned in pending:
            found.append(Candidate("D-15", abandoned, row["ordinal"], row["source_line"], text))
        block = _BLOCK.search(text)
        blocked = action_key(block["action"]) if block else None
        if blocked and blocked in pending:
            found.append(Candidate("D-16", blocked, row["ordinal"], row["source_line"], text))
    # One stable candidate per episode/family; the earliest source-linked
    # occurrence wins. Further repetitions remain in the evidence hash.
    return list({candidate.family: candidate for candidate in reversed(found)}.values())


def _impeded_action(text: str) -> str | None:
    match = re.search(r"\b(?:blocks|prevents|impedes)\s+([^.;]+)", text, re.I)
    return action_key(match[1]) if match else None


def _assess(episode: dict, candidate: Candidate, assertions: list[dict],
            rows: list[dict]) -> tuple[str, str, list[dict], list[dict], list[str]]:
    by_kind: dict[str, list[dict]] = {}
    for assertion in assertions:
        by_kind.setdefault(assertion["kind"], []).append(assertion)
    supporting = [_ref("candidate", candidate.source_ordinal, candidate.source_line)]
    disconfirming: list[dict] = []
    missing: list[str] = []

    def evidence(kind: str, *, strength: str = "known") -> bool:
        matches = [a for a in by_kind.get(kind, []) if a["strength"] == strength]
        for a in matches:
            supporting.append(_ref(kind, a["source_ordinal"], a["source_line"],
                                   a["related_ordinal"]))
        return bool(matches)

    def adverse(kind: str) -> bool:
        matches = by_kind.get(kind, [])
        for a in matches:
            disconfirming.append(_ref(kind, a["source_ordinal"], a["source_line"]))
        return bool(matches)

    pending = any(a["value"] == candidate.action and a["strength"] == "known"
                  for a in by_kind.get("pending_action", []))
    if not pending:
        missing.append("captured pending action")
    else:
        evidence("pending_action")
    if episode["authority_state"] != "authorized" or not any(
            a["value"] == candidate.action and a["strength"] == "known"
            for a in by_kind.get("authorization", []) + by_kind.get("explicit_approval", [])):
        missing.append("captured applicable authority")
    else:
        evidence("authorization")
        evidence("explicit_approval")
    if episode["context_state"] != "known":
        missing.append("complete scope and interaction context")
    for kind in ("question_answer", "same_outcome_resume", "status_exchange"):
        evidence(kind)
    if episode["advice_only"]:
        adverse("advice_only")
    for kind in ("permission_hold", "approval_withheld", "external_wait",
                 "external_wait_evidence",
                 "revocation", "observed_completion", "scope_change"):
        adverse(kind)
    if by_kind.get("scope_restriction"):
        missing.append("restriction applicability")
        supporting.extend(_ref("scope_restriction", a["source_ordinal"], a["source_line"])
                          for a in by_kind["scope_restriction"])
    if episode["lifecycle"] in ("cancelled", "completed", "superseded"):
        missing.append("still-active objective")
    disconfirming.extend(
        _ref("completion_or_check", row["ordinal"], row["source_line"])
        for row in rows if row["origin"] in {"human_request", "tool_result", "assistant_text"}
        and _COMPLETED.search(row["text"] or ""))
    disconfirming.extend(
        _ref("changed_conditions", row["ordinal"], row["source_line"])
        for row in rows if _CHANGED.search(row["text"] or ""))
    external_rows = [row for row in rows
                     if row["origin"] in {"human_request", "tool_result"}
                     and _EXTERNAL.search(row["text"] or "")]
    if external_rows:
        missing.append("external purchase, access or physical approval")
        disconfirming.extend(_ref("external_approval", row["ordinal"], row["source_line"])
                              for row in external_rows)

    if candidate.family in ("D-12", "D-13", "D-14"):
        terminal = next((row for row in rows if row["ordinal"] == candidate.source_ordinal
                         and row["terminal_state"] == "completed"), None)
        if terminal is None:
            missing.append("source-native completed terminal reply")
        else:
            supporting.append(_ref("terminal_reply", terminal["ordinal"], terminal["source_line"]))
    if candidate.family == "D-13" and not any(
            a["kind"] == "explicit_approval" and a["value"] == candidate.action
            and a["strength"] == "known" and a["source_ordinal"] is not None
            and a["source_ordinal"] < candidate.source_ordinal for a in assertions):
        missing.append("settled approval for same action")
    if candidate.family == "D-14" and not (_DEFECT.search(candidate.text)
                                           and _impeded_action(candidate.text) == candidate.action):
        missing.append("acknowledged outcome-blocking defect")
    if candidate.family == "D-15":
        if not any(a["kind"] == "status_exchange" and a["strength"] == "known"
                   and a["source_ordinal"] is not None
                   and a["source_ordinal"] < candidate.source_ordinal for a in assertions):
            missing.append("captured status exchange")
        if not _APPROVAL_OFFER.search(candidate.text):
            missing.append("affirmative abandonment and repeated approval")
    if candidate.family == "D-16":
        if _OFFER.search(candidate.text):
            missing.append("blocker claim includes offered continuation")
        checks = [row for row in rows if row["origin"] == "tool_result"
                  and (match := _CAPABLE.search(row["text"] or ""))
                  and _SUCCESS.search(row["text"] or "")
                  and action_key(match["action"]) == candidate.action]
        if not checks:
            missing.append("recorded matching capability check")
        else:
            supporting.extend(_ref("capability_check", row["ordinal"], row["source_line"])
                              for row in checks)

    if disconfirming:
        return "dismissed", "recorded stop reason or contradictory context", supporting, disconfirming, missing
    if missing:
        return "needs_review", "insufficient evidence for automatic support", supporting, disconfirming, missing
    return "supported", "captured authority, unmet action and family evidence", supporting, disconfirming, missing


def project(connection: sqlite3.Connection) -> int:
    """Replace deterministic assessments from current episodes in one transaction.

    Called before registry refresh, independently of supported cluster output.
    A projection exception rolls back rather than allowing stale membership.
    """
    connection.execute("SAVEPOINT twill_autonomy_projection")
    try:
        assessments: list[tuple] = []
        episodes = [dict(zip(("episode_id", "session_key", "input_sha", "start_ordinal",
                             "end_ordinal", "action_key", "authority_state", "context_state",
                             "lifecycle", "advice_only"), row)) for row in connection.execute(
            "SELECT episode_id, session_key, input_sha, start_ordinal, end_ordinal, "
            "action_key, authority_state, context_state, lifecycle, advice_only "
            "FROM execution_episode ORDER BY session_key, start_ordinal")]
        for episode in episodes:
            if not episode["action_key"]:
                continue
            pending = set(episode["action_key"].split(" | "))
            assertions = [dict(zip(("kind", "value", "strength", "source_ordinal",
                                    "source_line", "related_ordinal"), row))
                          for row in connection.execute(
                              "SELECT kind, value, strength, source_ordinal, source_line, related_ordinal "
                              "FROM episode_assertion WHERE episode_id=? ORDER BY assertion_id",
                              (episode["episode_id"],))]
            rows = [dict(zip(("ordinal", "source_line", "origin", "text", "terminal_state",
                              "sidechain", "truncated", "ts_utc"), row))
                    for row in connection.execute(
                        "SELECT ordinal, source_line, origin, text, terminal_state, "
                        "sidechain, truncated, ts_utc FROM interaction "
                        "WHERE session_key=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
                        (episode["session_key"], episode["start_ordinal"], episode["end_ordinal"]))]
            for candidate in _shortlist(rows, pending):
                disposition, reason, support, adverse, missing = _assess(
                    episode, candidate, assertions, rows)
                refs = support + adverse
                if len(refs) > MAX_REFERENCES:
                    disposition = "needs_review"
                    reason = "candidate evidence cap exceeded"
                    missing.append(f"{len(refs) - MAX_REFERENCES} omitted evidence references")
                    support = support[:MAX_REFERENCES]
                    adverse = adverse[:max(0, MAX_REFERENCES - len(support))]
                candidate_id = _sha([episode["episode_id"], candidate.family])[:32]
                evidence_sha = _sha([assertions, rows])
                assessments.append((candidate_id, episode["episode_id"], episode["session_key"],
                                    candidate.family, DETECTOR_VERSION, ANALYZER_VERSION,
                                    episode["input_sha"], evidence_sha,
                                    _cluster_key(candidate.family, candidate.action),
                                    disposition, reason, json.dumps(support, sort_keys=True),
                                    json.dumps(adverse, sort_keys=True), json.dumps(missing),
                                    "captured" if disposition == "supported" else "incomplete",
                                    rows[-1]["ts_utc"] if rows else ""))
        connection.execute("DELETE FROM autonomy_assessment")
        connection.executemany(
            "INSERT INTO autonomy_assessment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", assessments)
        connection.execute("RELEASE SAVEPOINT twill_autonomy_projection")
        return len(assessments)
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT twill_autonomy_projection")
        connection.execute("RELEASE SAVEPOINT twill_autonomy_projection")
        raise
