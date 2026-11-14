"""Broad, source-verified intervention episodes; historical evidence, never authority.

The native readers own role and interactive-tool pairing. This pass retains
ambiguous followups and calibration negatives instead of treating a keyword
match as either proof of human origin or permission to apply an instruction.
All persisted prose is bounded, redacted, and confined to the private root.
"""
from __future__ import annotations

import hashlib
import dataclasses
import json
import re
import time
from collections import Counter
from pathlib import Path

from codex_reader import CodexRolloutLineParser
from twill_learning_intake import MAX_LINE_BYTES, MAX_SOURCE_BYTES, _private_json
from twill_human_steering import _RETRACTION, extract_directive
from twill_lessons import lessons_dir
from twill_reader import ClaudeCodeLineParser
from twill_redactor import redact_text
from twill_source_discovery import DiscoveryQueue, validate_limits

SCHEMA = "twill-intervention-episode/v1"
SCANNER_VERSION = hashlib.sha256(b"".join(
    hashlib.sha256(Path(__file__).with_name(name).read_bytes()).digest()
    for name in ("twill_intervention_mining.py", "twill_redactor.py", "twill_reader.py", "codex_reader.py", "twill_human_steering.py")
)).hexdigest()
CONSUMER = "intervention-episodes"
MAX_PARSE_INTERACTIONS = 100_000
MAX_PARSE_LINES = 2_000_000
MAX_EPISODE_BYTES = 2 * 1024 * 1024
MAX_AUTHORITY_UPDATES = 64
_HUMAN = {"human_request", "human_answer"}
_WORKER = re.compile(r"(?:\A\s*(?:NEEDLE worker (?:prompt|task|assignment)|\[NEEDLE WORKER\])|"
                     r"Message Type:\s*(?:NEW_TASK|MESSAGE)|"
                     r"You are (?:a |an )?(?:NEEDLE |autonomous )?worker\b)", re.I | re.S)
_HARNESS = re.compile(r"\A\s*<(?:task-notification|task-output|subagent_notification|hook_prompt|"
                      r"local-command-caveat|system-reminder)\b", re.I)
_UNCERTAIN = re.compile(r"\A\s*(?:Coordinator:|Correction to (?:the )?brief\b.{0,250}parent session|"
                        r"(?:Message|Task) from (?:the )?(?:parent|coordinator|agent)\b)", re.I | re.S)
_QUOTED = re.compile(r"\A\s*(?:```|>|<(?:user_instructions|system|developer|environment_context)\b|"
                     r"# AGENTS\.md instructions|This session is being continued from a previous conversation|"
                     r"\[?(?:quoted (?:history|transcript)|pasted transcript))", re.I)
_SIGNALS = (
    ("cancellation", r"\b(?:stop|cancel|pause|abort|hold off|never mind|do not continue|don't continue)\b"),
    ("correction", r"\b(?:actually|instead|incorrect|wrong|mistake|you missed|you forgot|that's not|that is not|"
                   r"I meant|I asked|don't ask|do not ask|why did you|you should have|not what I)\b"),
    ("scope_change", r"\b(?:also (?:add|include|handle|support)|change (?:the )?scope|expand|"
                     r"only (?:change|edit|fix|do|touch)|leave .{0,60} alone)\b"),
    ("preference", r"\b(?:I prefer|I'd prefer|I would prefer|going forward|from now on|always|never|"
                   r"please use|keep .{0,40} concise|avoid|rather than)\b"),
    ("approval", r"\b(?:approved|approve|go ahead|yes|proceed|continue|sounds good|do it|ship it)\b"),
    ("redirection", r"\b(?:focus on|switch to|use .{0,50} instead|first .{0,80} then|"
                    r"can you (?:check|look|try)|please (?:check|look|try))\b"),
)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_policy(policy: dict) -> dict:
    """Validate this pass's bounds; the controller validates model budgets."""
    validate_limits(policy)
    config = policy.get("two_pass", {})
    if not isinstance(config, dict) or type(config.get("enabled", False)) is not bool:
        raise ValueError("two_pass enabled must be boolean")
    scopes = config.get("scopes", [])
    if (not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes) or
            len(set(scopes)) != len(scopes) or any(s not in policy.get("scopes", {}) for s in scopes) or
            config.get("enabled") and not scopes):
        raise ValueError("two_pass scopes must be unique enrolled scope names")
    result = dict(config)
    for key, default, low, high in (("max_episode_chars", 32768, 1024, 131072),
                                   ("max_episodes_per_file", 32, 1, 256)):
        value = config.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"two_pass {key} must be an integer in {low}..{high}")
        result[key] = value
    sources = policy.get("intervention_sources", [])
    if not isinstance(sources, list) or any(not isinstance(s, str) or not s for s in sources):
        raise ValueError("intervention_sources must be source glob strings")
    return result


def _directory(root: Path, *, create=False) -> Path:
    root = Path(root).expanduser()
    if create:
        lessons_dir(root)
    directory = root / "learning" / "interventions"
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.absolute() != directory.resolve():
        raise ValueError("intervention episodes require canonical private paths")
    return directory


def _read_json(path: Path, *, limit=MAX_EPISODE_BYTES) -> dict:
    if path.is_symlink() or path.absolute() != path.resolve() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("intervention record must be a bounded canonical regular file")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("intervention record must be an object")
    return value


def _fingerprint(path: Path) -> list[int]:
    info = path.stat(follow_symlinks=False)
    if path.is_symlink() or not path.is_file() or path.absolute() != path.resolve() or info.st_size > MAX_SOURCE_BYTES:
        raise ValueError("native source must be a bounded canonical regular file")
    return [info.st_size, info.st_mtime_ns, info.st_ino]


def _source_hash(path: Path, deadline: float) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if time.monotonic() >= deadline:
                raise TimeoutError("source hash scan time budget exhausted")
            hasher.update(chunk)
    return hasher.hexdigest()


def _parse(path: Path, deadline: float):
    """Replay reader state; no prose or private parser internals enter cursors."""
    before = _fingerprint(path)
    original_hash = _source_hash(path, deadline)
    parser = None
    hashes, events, flags, meta_lines = {}, [], [], set()
    hasher = hashlib.sha256()
    offset, line_number, complete = 0, 0, False
    with path.open("rb") as stream:
        while time.monotonic() < deadline:
            raw = stream.readline(MAX_LINE_BYTES + 1)
            if not raw:
                complete = True
                break
            if len(raw) > MAX_LINE_BYTES:
                flags.append("line_limit")
                break
            if not raw.endswith(b"\n"):
                flags.append("incomplete_native_record")
                break
            line_number += 1
            if line_number > MAX_PARSE_LINES:
                flags.append("parser_line_limit")
                break
            hasher.update(raw)
            offset = stream.tell()
            hashes[line_number] = hashlib.sha256(raw).hexdigest()
            text = raw.decode("utf-8", errors="strict")
            try:
                record = json.loads(text)
            except ValueError:
                flags.append("malformed_native_record")
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get("payload", {})
            if record.get("isMeta") is True or isinstance(payload, dict) and payload.get("isMeta") is True:
                meta_lines.add(line_number)
            if parser is None:
                if record.get("type") in {"session_meta", "turn_context", "response_item", "event_msg"}:
                    parser = CodexRolloutLineParser()
                elif record.get("type") in {"user", "assistant", "summary", "system", "attachment"}:
                    parser = ClaudeCodeLineParser()
                else:
                    continue
            events.extend(parser.parse_line(text, line_number))
            if len(parser._interactions) >= MAX_PARSE_INTERACTIONS:
                flags.append("parser_interaction_limit")
                break
        else:
            flags.append("scan_time")
    if before != _fingerprint(path):
        raise ValueError("native source changed during parse")
    if complete and hasher.hexdigest() != original_hash:
        raise ValueError("native source bytes changed during parse")
    if complete and parser:
        events.extend(parser.finish())
    interactions = tuple(dataclasses.replace(i, origin="harness", actor="system")
                         if i.source_line in meta_lines and i.origin in _HUMAN else i
                         for i in parser.interactions) if parser else ()
    provider = "codex" if isinstance(parser, CodexRolloutLineParser) else "claude" if parser else "unknown"
    # A prefix hash must never masquerade as a whole-source hash.
    return interactions, events, hashes, provider, hasher.hexdigest() if complete else None, offset, complete, flags


def _classification(interaction, previous: list) -> dict:
    text = interaction.text.strip()
    flags = []
    if interaction.sidechain:
        flags.append("sidechain")
    if interaction.origin not in _HUMAN:
        flags.append(interaction.origin)
    if _WORKER.search(text):
        flags.append("worker_prompt")
    if _QUOTED.search(text):
        flags.append("quoted_or_injected")
    if _HARNESS.search(text):
        flags.append("harness_wrapper")
    if flags:
        return {"signal": "negative", "category": "excluded_origin", "rank": 0, "flags": flags}
    if _UNCERTAIN.search(text):
        return {"signal": "unknown", "category": "ambiguous", "rank": 20, "flags": ["uncertain_origin"]}
    if interaction.origin == "human_answer":
        return {"signal": "candidate", "category": "interactive_answer", "rank": 95, "flags": []}
    # No assistant behavior precedes a task start, even if its wording says "fix".
    if not previous:
        return {"signal": "negative", "category": "task_start", "rank": 10, "flags": ["missing_previous_behavior"]}
    for category, pattern in _SIGNALS:
        if re.search(pattern, text, re.I | re.S):
            return {"signal": "candidate", "category": category, "rank": 90, "flags": []}
    if "?" in text or re.search(r"\b(?:why|what|how|where|when|can you|could you)\b", text, re.I):
        return {"signal": "candidate", "category": "question", "rank": 80, "flags": []}
    if re.match(r"(?:new (?:task|request)|next (?:task|project)|build |create |implement |write )", text, re.I):
        return {"signal": "negative", "category": "ordinary_request", "rank": 15, "flags": ["possible_new_task"]}
    return {"signal": "unknown", "category": "ambiguous", "rank": 40, "flags": ["uncertain_intervention"]}


def _ref(interaction, path: Path, hashes: dict, file_hash: str) -> dict:
    return {"source_path": str(path), "source_line": interaction.source_line,
            "sha256": hashes[interaction.source_line], "file_sha256": file_hash,
            "role": interaction.actor, "origin": interaction.origin, "locator": interaction.locator,
            "event_index": interaction.event_index, "ordinal": interaction.ordinal,
            "turn_id": interaction.turn_id, "message_id": interaction.message_id,
            "spans": [[0, len(interaction.text)]]}


def _view(interaction, path, hashes, file_hash, limit) -> dict:
    text = redact_text(interaction.text)
    return dict(_ref(interaction, path, hashes, file_hash), text=text[:limit],
                truncated=len(text) > limit or interaction.truncated,
                call_ordinal=interaction.call_ordinal, question_ordinal=interaction.question_ordinal)


def _authority_updates(interactions, index, path, hashes, file_hash) -> list[dict]:
    """Later native directions only veto old authority; they never create it.

    Retain the first bounded set of locators, without later prose. One update
    already suffices to hold every proposed lesson for a fresh human review.
    """
    updates = []
    questions = {i.ordinal for i in interactions if i.origin == "question" and not i.sidechain}
    for later in interactions[index + 1:]:
        text = later.text.strip()
        if (later.sidechain or later.actor != "user" or later.origin not in _HUMAN or
                later.session_id != interactions[index].session_id or
                any(pattern.search(text) for pattern in (_WORKER, _HARNESS, _QUOTED, _UNCERTAIN)) or
                "```" in text or re.search(r"(?m)^\s*(?:>|[\"'])", text)):
            continue
        if later.origin == "human_answer" and later.question_ordinal not in questions:
            continue
        if _RETRACTION.search(text):
            kind = "retraction"
        elif extract_directive(text) is not None or (
                "\n" in text and re.search(r"\b(?:for (?:all )?future (?:tasks|requests)|"
                                              r"going forward|from now on|always|never)\b", text, re.I)):
            kind = "replacement"
        else:
            continue
        updates.append(dict(_ref(later, path, hashes, file_hash), kind=kind))
        if len(updates) == MAX_AUTHORITY_UPDATES:
            break
    return updates


def _episode(interactions, index, events, path, hashes, provider, file_hash, config) -> dict:
    human = interactions[index]
    behavior = [i for i in interactions[:index] if not i.sidechain and
                i.origin in {"assistant_text", "tool_call", "tool_result", "question"}][-6:]
    # Classification still depends only on preceding assistant/tool behavior:
    # retained task history cannot turn a fresh request into a correction.
    classification = _classification(human, behavior)
    earlier_humans = [i for i in interactions[:index]
                      if not i.sidechain and i.actor == "user" and i.origin in _HUMAN
                      and i.session_id == human.session_id
                      and not any(pattern.search(i.text.strip())
                                  for pattern in (_WORKER, _HARNESS, _QUOTED, _UNCERTAIN))][-8:]
    previous = behavior + earlier_humans
    # Keep actual question provenance even if tools between it and its answer fill the window.
    if human.question_ordinal is not None:
        question = next((i for i in interactions if i.ordinal == human.question_ordinal), None)
        if question is not None and question not in previous:
            previous.append(question)
    previous = sorted({i.ordinal: i for i in previous}.values(), key=lambda i: i.ordinal)
    following = []
    for item in interactions[index + 1:]:
        if not item.sidechain and item.origin in _HUMAN:
            break
        if not item.sidechain and item.origin in {"assistant_text", "tool_call", "tool_result", "question"}:
            following.append(item)
            if len(following) == 6:
                break
    native_identity = [provider, human.session_id or str(path), human.turn_id,
                       human.ordinal, human.event_index, human.locator, _digest(human.text)]
    ident = "IE-" + _digest(native_identity)[:24]
    maximum = config["max_episode_chars"]
    human_limit = maximum // 2
    context_limit = max(1, (maximum - human_limit) // max(1, len(previous) + len(following)))
    if classification["signal"] == "negative":
        # Calibration needs a short recognizable excerpt plus native locators,
        # not copies of whole task briefs and their neighboring tool output.
        human_limit = min(human_limit, 240)
        context_limit = min(context_limit, 240)
    context = {"previous": [_view(i, path, hashes, file_hash, context_limit) for i in previous],
               "intervention": _view(human, path, hashes, file_hash, human_limit),
               "following": [_view(i, path, hashes, file_hash, context_limit) for i in following]}
    flags = list(classification["flags"])
    if not following:
        flags.append("missing_following_behavior")
    if not human.session_id:
        flags.append("unknown_native_session")
    if any(v["truncated"] for v in [*context["previous"], context["intervention"], *context["following"]]):
        flags.append("context_truncated")
    end = following[-1].source_line if following else human.source_line
    statuses = [{"source_line": e.source_line, "exit_code": e.exit_code} for e in events
                if human.source_line < e.source_line <= end and e.exit_code is not None]
    original = _ref(human, path, hashes, file_hash)
    return {"schema": SCHEMA, "id": ident, "scanner_version": SCANNER_VERSION,
            "state": "negative_sample" if classification["signal"] == "negative" else "pending_interpretation",
            "classification": classification, "session_id": human.session_id, "provider": provider,
            "cwd": human.cwd, "authority": "historical_evidence_only", "source_original": original,
            "source_copies": [original], "context": context, "flags": flags,
            "authority_updates": _authority_updates(interactions, index, path, hashes, file_hash),
            "outcome": {"status": "unknown", "executor_status": statuses}}


def consumed_sources(root: Path) -> dict:
    """Exact fully/partially processed generations, including files with no episodes."""
    path = Path(root) / "learning" / ".intervention-consumption.json"
    if not path.exists():
        return {}
    sources = _read_json(path, limit=MAX_SOURCE_BYTES).get("sources", {})
    for receipt in sources.values():
        if receipt.get("scanner_version") != SCANNER_VERSION:
            receipt.update(complete=False, compact_durable=False)
    return sources


def discover(policy: dict, artifacts_root: Path) -> dict:
    config = validate_policy(policy)
    summary = {"enabled": config.get("enabled", False), "discovered": 0, "updated": 0,
               "deduplicated": 0, "errors": 0, "coverage": {}}
    if not summary["enabled"]:
        return summary
    root = Path(artifacts_root).expanduser()
    directory = _directory(root, create=True)
    receipts = consumed_sources(root)
    with DiscoveryQueue(root, CONSUMER, policy, MAX_SOURCE_BYTES) as queue:
        version_path = root / "learning" / ".intervention-miner-version.json"
        version = _read_json(version_path).get("scanner_version") if version_path.exists() else None
        if version != SCANNER_VERSION:
            queue.db.execute("UPDATE source SET done=0,cursor='{}',error=0 WHERE consumer=?", (CONSUMER,))
            queue.db.commit()
            _private_json(version_path, {"schema": "twill-intervention-miner-version/v1", "scanner_version": SCANNER_VERSION})
        deadline = time.monotonic() + queue.limits["scan_seconds"]
        queue.inventory(deadline)
        # A sidecar provenance generation may change without changing native
        # bytes. Fairly rotate a bounded metadata audit through completed rows.
        rows = queue.db.execute("SELECT path,fingerprint FROM source WHERE consumer=? AND done=1 ORDER BY attempted,path LIMIT ?",
                                (CONSUMER, queue.limits["max_files_per_cycle"])).fetchall()
        for row in rows:
            if time.monotonic() >= deadline:
                break
            queue.db.execute("UPDATE consumer SET counter=counter+1 WHERE name=?", (CONSUMER,))
            queue.db.execute("UPDATE source SET attempted=(SELECT counter FROM consumer WHERE name=?) WHERE consumer=? AND path=?",
                             (CONSUMER, CONSUMER, row["path"]))
            native = Path(row["path"])
            sidecar = native.with_suffix(".manifest.json")
            if not native.is_file() or not sidecar.exists():
                continue
            try:
                version = _read_json(sidecar).get("source_version")
            except (OSError, ValueError):
                summary["errors"] += 1
                continue
            if receipts.get(row["path"], {}).get("source_version") != version:
                queue.db.execute("UPDATE source SET done=0,cursor='{}',error=0 WHERE consumer=? AND path=?",
                                 (CONSUMER, row["path"]))
        for value, fingerprint, cursor in queue.pending(deadline):
            path = Path(value)
            try:
                interactions, events, hashes, provider, file_hash, offset, complete, flags = _parse(path, deadline)
                if complete and hashes and provider == "unknown":
                    flags.append("unsupported_native_format")
                if not complete:
                    # Replay is bounded, and incomplete evidence never receives a consumption receipt.
                    queue.save(value, {"fingerprint": fingerprint, "offset": 0,
                                       "line": cursor.get("line", 0), "done": False})
                    receipts[value] = {"fingerprint": fingerprint, "sha256": None, "complete": False,
                                       "offset": offset, "error": flags[0] if flags else "incomplete_scan",
                                       "compact_durable": False, "episode_ids": []}
                    queue.budget(flags[0] if flags else "incomplete_scan")
                    continue
                if _fingerprint(path) != fingerprint:
                    raise ValueError("native source changed before episode publication")
                sidecar = path.with_suffix(".manifest.json")
                provenance = _read_json(sidecar) if sidecar.exists() else {}
                processed = set(cursor.get("pending", []))
                old_ids = receipts.get(value, {}).get("episode_ids", [])
                existing_ordinals = set()
                for old_id in old_ids:
                    old_path = directory / (old_id + ".json")
                    if old_path.is_file():
                        old_episode = _read_json(old_path)
                        for ref in [old_episode["source_original"], *old_episode.get("source_copies", [])]:
                            if ref.get("source_path") == value:
                                existing_ordinals.add(ref["ordinal"])
                candidates = [(n, i) for n, i in enumerate(interactions) if i.actor == "user" or
                              i.origin in {"quoted_history", "harness"}]
                candidate_indices = {n for n, _ in candidates}
                ranked, negative_counts = [], Counter()
                saw_behavior = False
                for n, item in enumerate(interactions):
                    if not item.sidechain and item.origin in {"assistant_text", "tool_call", "tool_result", "question"}:
                        saw_behavior = True
                    if n not in candidate_indices:
                        continue
                    label = _classification(item, [True] if saw_behavior else [])
                    if label["signal"] == "negative":
                        reason = ":".join([label["category"], *label["flags"]])
                        negative_counts[reason] += 1
                        if negative_counts[reason] > 2 and n not in existing_ordinals:
                            continue
                    if n not in processed:
                        ranked.append((n, item, label["rank"]))
                remaining = sorted(ranked, key=lambda row: (-row[2], row[0]))
                selected = remaining[:config["max_episodes_per_file"]]
                ids = list(old_ids)
                for index, interaction, _rank in selected:
                    episode = _episode(interactions, index, events, path, hashes, provider, file_hash, config)
                    if provenance:
                        episode["source_provenance"] = {key: provenance[key] for key in
                            ("schema_version", "source_version", "raw_sha256", "reference_keys", "host",
                             "source_key", "file_identity", "captured_at", "completeness_basis") if key in provenance}
                    if flags:
                        episode["flags"] = sorted(set(episode["flags"] + flags))
                    target = directory / (episode["id"] + ".json")
                    if target.exists():
                        old = _read_json(target)
                        old_evidence = _digest({key: old.get(key) for key in
                            ("source_original", "source_copies", "context", "outcome", "source_provenance", "classification", "authority_updates")})
                        copies = old.get("source_copies", [old["source_original"]])
                        aliases = old.get("source_aliases", [])
                        incoming = episode["source_original"]
                        canonical = old["source_original"]
                        same_generation = incoming["file_sha256"] == canonical["file_sha256"]
                        known_hashes = {ref["file_sha256"] for ref in [canonical, *copies, *aliases]}
                        older_alias = (not same_generation and incoming["source_path"] != canonical["source_path"]
                                       and incoming["file_sha256"] in known_hashes)
                        if incoming != canonical and incoming not in aliases:
                            aliases.append(incoming)
                        old["source_aliases"] = aliases
                        if same_generation:
                            # Byte-identical aliases are resolution metadata,
                            # not a new historical source or interpretation.
                            # Still refresh scanner-derived views, rebasing
                            # them onto the existing canonical locator.
                            episode["source_original"] = dict(incoming, source_path=canonical["source_path"])
                            for ref in [*episode["context"]["previous"], episode["context"]["intervention"],
                                        *episode["context"]["following"], *episode["authority_updates"]]:
                                ref["source_path"] = canonical["source_path"]
                        elif not older_alias and incoming not in copies:
                            copies.append(incoming)
                        # Keep lifecycle state and interpretation decisions when copies/append snapshots arrive.
                        if not older_alias:
                            old.update(source_copies=copies, source_original=episode["source_original"],
                                       context=episode["context"], flags=episode["flags"], outcome=episode["outcome"],
                                       classification=episode["classification"], scanner_version=SCANNER_VERSION,
                                       authority_updates=episode["authority_updates"])
                            if episode["classification"]["signal"] == "negative":
                                old["state"] = "negative_sample"
                            elif old.get("state") == "negative_sample":
                                old["state"] = "pending_interpretation"
                            if provenance and not same_generation:
                                old["source_provenance"] = episode["source_provenance"]
                        new_evidence = _digest({key: old.get(key) for key in
                            ("source_original", "source_copies", "context", "outcome", "source_provenance", "classification", "authority_updates")})
                        if old_evidence != new_evidence and old.get("state") in {"interpreted", "needs_review", "pending_retry"}:
                            old.update(state="pending_interpretation", interpretation_stale=True)
                            old.pop("retry_after", None)
                            old.pop("interpretation_disposition", None)
                        episode = old
                        summary["deduplicated"] += 1
                        summary["updated"] += 1
                    else:
                        summary["discovered"] += 1
                    if episode.get("privacy_hold"):
                        # An operator quarantine survives new native snapshots
                        # and scanner generations. Keep the native references,
                        # but never repopulate prose or queue lesson inference.
                        episode.update(privacy_hold=True, state="needs_review")
                        context = episode["context"]
                        for view in [*context["previous"], context["intervention"], *context["following"]]:
                            view.update(text="", truncated=True)
                        episode["flags"] = sorted(set(episode["flags"] + ["context_truncated"]))
                    _private_json(target, episode)
                    queue.add_output(target.name)
                    if episode["id"] not in ids:
                        ids.append(episode["id"])
                    processed.add(index)
                done = len(remaining) <= len(selected) and not flags
                position = max(processed, default=-1) + 1
                if _fingerprint(path) != fingerprint or _source_hash(path, deadline) != file_hash:
                    raise ValueError("native source changed during episode publication")
                queue.save(value, {"fingerprint": fingerprint, "offset": offset,
                                   "line": position, "pending": sorted(processed), "done": done}, error=bool(flags))
                version = provenance.get("source_version")
                receipts[value] = {"fingerprint": fingerprint, "sha256": file_hash, "complete": done,
                                   "offset": offset, "line": position, "episode_ids": ids,
                                   "error": flags[0] if flags else None, "compact_durable": done, "source_version": version,
                                   "negative_reasons": dict(negative_counts), "scanner_version": SCANNER_VERSION}
                if not done:
                    queue.budget("episode_limit")
            except (OSError, ValueError, UnicodeError):
                summary["errors"] += 1
                queue.save(value, {"fingerprint": fingerprint, "offset": 0, "line": 0, "done": False}, error=True)
                receipts.pop(value, None)
        state = queue.finish({})
        summary["coverage"] = state["coverage"]
    _private_json(root / "learning" / ".intervention-consumption.json",
                  {"schema": "twill-intervention-consumption/v1", "sources": receipts})
    summary["metrics"] = metrics(root)
    return summary


def verify_episode(path: Path, *, enrolled_sources: list[str] | None = None) -> dict:
    """Reparse native evidence and verify original roles, hashes, spans and text.

    Replayed graph links prove interactive answers came from their actual tool
    question. No historical source statement is upgraded to current authority.
    """
    episode = _read_json(Path(path))
    if episode.get("schema") != SCHEMA or not re.fullmatch(r"IE-[0-9a-f]{24}", episode.get("id", "")):
        raise ValueError("invalid intervention episode schema")
    if episode.get("scanner_version") != SCANNER_VERSION:
        raise ValueError("intervention episode requires current miner rescan")
    context = episode["context"]
    views = [*context["previous"], context["intervention"], *context["following"]]
    if episode.get("privacy_hold") and any(ref.get("text") != "" or ref.get("truncated") is not True for ref in views):
        raise ValueError("privacy-held evidence must have reference-only context")
    parsed = {}
    if episode["source_original"] != {k: v for k, v in context["intervention"].items()
                                      if k not in {"text", "truncated", "call_ordinal", "question_ordinal"}}:
        raise ValueError("intervention original locator mismatch")
    original_path = episode["source_original"]["source_path"]
    updates = episode.get("authority_updates")
    if (not isinstance(updates, list) or len(updates) > MAX_AUTHORITY_UPDATES or
            any(not isinstance(ref, dict) or ref.get("source_path") != original_path or
                ref.get("file_sha256") != episode["source_original"]["file_sha256"] for ref in updates)):
        raise ValueError("invalid intervention authority updates")
    def update_identity(ref):
        return {key: value for key, value in ref.items() if key not in {"source_path", "file_sha256"}}
    allowed_paths = {r["source_path"] for r in [episode["source_original"], *episode.get("source_copies", [])]}
    if any(ref["source_path"] not in allowed_paths for ref in views):
        raise ValueError("intervention context has unrelated source")
    from twill_learning import enrolled_source
    if Path(original_path).exists() and (enrolled_sources is None or enrolled_source(original_path, enrolled_sources)):
        # A live canonical source moving to a different generation is drift,
        # not a missing file that an older archived copy may substitute for.
        parsed[original_path] = _parse(Path(original_path), time.monotonic() + 120)
        if not parsed[original_path][6] or parsed[original_path][4] != episode["source_original"]["file_sha256"]:
            raise ValueError("canonical intervention native generation changed")
    # Resolve live storage separately from canonical historical locators.
    # The complete current generation, native records and authority snapshot
    # must match before any copy may stand in for the original.
    selected = None
    for copy in [episode["source_original"], *episode.get("source_copies", []), *episode.get("source_aliases", [])][:64]:
        # An older append snapshot can share every local context record while
        # omitting a later withdrawal. Only copies of the current full captured
        # generation may substitute for the original authority snapshot.
        if copy.get("file_sha256") != episode["source_original"]["file_sha256"]:
            continue
        value = copy["source_path"]
        if enrolled_sources is not None:
            if not enrolled_source(value, enrolled_sources):
                continue
        try:
            if value not in parsed:
                parsed[value] = _parse(Path(value), time.monotonic() + 120)
            items, _, hashes, _, file_hash, _, complete, _ = parsed[value]
            if not complete or file_hash != copy["file_sha256"]:
                continue
            by_ordinal = {item.ordinal: item for item in items}
            actual_updates = _authority_updates(items, context["intervention"]["ordinal"], Path(value), hashes, file_hash)
            if [update_identity(ref) for ref in updates] != [update_identity(ref) for ref in actual_updates]:
                continue
            if all(ref["ordinal"] in by_ordinal and
                   hashes.get(by_ordinal[ref["ordinal"]].source_line) == ref["sha256"] for ref in views):
                selected = (value, file_hash, by_ordinal)
                break
        except (OSError, ValueError, UnicodeError):
            continue
    if selected is None:
        raise ValueError("intervention native source hash mismatch")
    selected_path, selected_hash, by_ordinal = selected
    interactions, _, hashes, _, file_hash, _, complete, _ = parsed[selected_path]
    for ref in views:
        if not complete or ref["file_sha256"] != file_hash or hashes.get(ref["source_line"]) != ref["sha256"]:
            raise ValueError("intervention native source hash mismatch")
        native = next((i for i in interactions if i.ordinal == ref["ordinal"]), None)
        if native is None or any(ref[key] != actual for key, actual in (
                ("role", native.actor), ("origin", native.origin), ("locator", native.locator),
                ("event_index", native.event_index), ("turn_id", native.turn_id),
                ("message_id", native.message_id), ("source_line", native.source_line), ("spans", [[0, len(native.text)]]))):
            raise ValueError("intervention native role or span mismatch")
        safe = redact_text(native.text)
        if ref["text"] != safe[:len(ref["text"])] or (not ref["truncated"] and ref["text"] != safe):
            raise ValueError("intervention excerpt does not match original")
        if ref.get("question_ordinal") != native.question_ordinal or ref.get("call_ordinal") != native.call_ordinal:
            raise ValueError("intervention tool pairing mismatch")
    intervention = context["intervention"]
    native_human = by_ordinal[intervention["ordinal"]]
    native_provider = parsed[selected_path][3]
    native_identity = [native_provider, native_human.session_id or original_path, native_human.turn_id,
                       native_human.ordinal, native_human.event_index, native_human.locator, _digest(native_human.text)]
    if (episode["id"] != "IE-" + _digest(native_identity)[:24] or
            episode.get("session_id") != native_human.session_id or episode.get("cwd") != native_human.cwd or
            episode.get("provider") != native_provider or episode.get("authority") != "historical_evidence_only"):
        raise ValueError("intervention identity or captured scope mismatch")
    natives = parsed[selected_path][0]
    previous_native = [i for i in natives[:native_human.ordinal] if not i.sidechain and
                       i.origin in {"assistant_text", "tool_call", "tool_result", "question"}][-6:]
    if episode["classification"] != _classification(native_human, previous_native):
        raise ValueError("intervention classification does not match native source")
    if any(ref["ordinal"] >= native_human.ordinal for ref in context["previous"]) or any(
            ref["ordinal"] <= native_human.ordinal for ref in context["following"]):
        raise ValueError("intervention context order mismatch")
    original = {k: v for k, v in intervention.items() if k not in {"text", "truncated", "call_ordinal", "question_ordinal"}}
    if original != episode["source_original"]:
        raise ValueError("intervention original locator mismatch")
    episode["verified"] = True
    episode["resolved_sources"] = [{"path": selected_path, "sha256": selected_hash}]
    episode["turns"] = [dict(ref, index=index) for index, ref in enumerate(views)]
    episode["intervention_index"] = len(context["previous"])
    return episode


def metrics(root: Path) -> dict:
    stages, signals, categories, reasons, dispositions = Counter(), Counter(), Counter(), Counter(), Counter()
    invalid = 0
    directory = _directory(root)
    for path in directory.glob("IE-*.json"):
        try:
            episode = _read_json(path)
            stages[episode.get("state", "unknown")] += 1
            signals[episode["classification"]["signal"]] += 1
            categories[episode["classification"]["category"]] += 1
            reasons.update(episode.get("flags", []))
            if episode.get("interpretation_disposition"):
                dispositions[episode["interpretation_disposition"]] += 1
        except (OSError, ValueError, KeyError, TypeError):
            invalid += 1
    receipts = consumed_sources(root)
    negatives = Counter()
    for receipt in receipts.values():
        negatives.update(receipt.get("negative_reasons", {}))
    return {"episodes": sum(stages.values()), "stages": dict(stages), "signals": dict(signals),
            "interpretation_dispositions": dict(dispositions),
            "categories": dict(categories), "reasons": dict(reasons), "invalid": invalid,
            "consumed_sources": sum(bool(r.get("complete")) for r in receipts.values()),
            "incomplete_sources": sum(not r.get("complete") for r in receipts.values()),
            "sampled_negative_reasons": dict(negatives),
            "scan_reasons": dict(Counter(r["error"] for r in receipts.values() if r.get("error")))}


def pinned_sources(root: Path) -> set[str]:
    """Conservative pins cover unresolved episodes and all candidate evidence."""
    paths = set()
    for path in _directory(root).glob("IE-*.json"):
        episode = _read_json(path)
        terminal_nonlesson = (episode.get("state") == "interpreted" and
                              episode.get("interpretation_disposition") in
                              {"task_only", "non_intervention", "already_covered", "code_fix"})
        if episode.get("privacy_hold") or (episode.get("state") not in {"archived", "rejected", "negative_sample"} and not terminal_nonlesson):
            for ref in [episode["source_original"], *episode.get("source_copies", []), *episode.get("source_aliases", [])]:
                value = ref.get("source_path")
                if isinstance(value, str) and Path(value).is_absolute() and str(Path(value).resolve()) == value:
                    paths.add(value)
            context = episode.get("context", {})
            for ref in [*context.get("previous", []), context.get("intervention", {}), *context.get("following", [])]:
                value = ref.get("source_path")
                if isinstance(value, str) and Path(value).is_absolute() and str(Path(value).resolve()) == value:
                    paths.add(value)
    return paths
