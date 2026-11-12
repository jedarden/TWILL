"""Explicit durable human steering, verified against enrolled native sources.

An extraction is only a locator and a snapshot hash. Neither candidate prose
nor extracted scope metadata supplies authority or an evaluation oracle.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from twill_source_discovery import DiscoveryQueue

from twill_learning_intake import (MAX_LINE_BYTES, MAX_SOURCE_BYTES,
                                   _interactive_boundary, _native_workflow_turn,
                                   _private_json, validate_policy as _validate_intake)

SCHEMA = "twill-human-steering/v1"
MAX_RECORDS = 2_000_000
_PREFIX = re.compile(
    r"\A(?:For all future tasks[,：:]\s*|Remember for all future tasks:\s*|"
    r"For future tasks in this repository[,：:]\s*|"
    r"Remember for future tasks in this repository:\s*)(?P<rule>.+)\Z", re.I)
_LOCAL_PREFIX = re.compile(
    r"\A(?:(?:Always|Never)\s+|Going forward,\s*|From now on,?\s+|Remember:\s*)(?P<rule>.+)\Z", re.I)
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_RETRACTION = re.compile(
    r"\b(?:withdraw|revoke|retract|rescind|forget|disregard)\b|"
    r"\b(?:ignore|replace|correct|change|override|amend|supersede)\b.{0,100}"
    r"\b(?:rule|instruction|directive|guidance)\b|"
    r"\b(?:rule|instruction|directive|guidance)\b.{0,100}"
    r"\b(?:withdrawn|revoked|retracted|replaced|incorrect|cancelled|canceled)\b|"
    r"\b(?:no longer|stop|do not|don't)\s+(?:follow|use|apply|remember|enforce)\b", re.I | re.S)


def extract_directive(text: str) -> dict | None:
    """Recognize standalone durable directions; natural forms are repository scoped.

    Deliberately abstain on quotations, wrappers, pasted multi-line material,
    and task-local requests. Credential-bearing directives are not exported.
    """
    if not isinstance(text, str) or not 1 <= len(text) <= 8192:
        return None
    stripped = text.strip()
    if (len(stripped) > 4096 or any(c in stripped for c in ("\n", "\r", "\x00", "`", "<", ">")) or
            re.search(r"(?:\b(?:system|developer|assistant|user)\s*:|"
                      r"\b(?:quoted?|example|pasted?|worker prompt|role spoof|transcript)\b)", stripped, re.I)):
        return None
    match = _PREFIX.fullmatch(stripped) or _LOCAL_PREFIX.fullmatch(stripped)
    if match is None or not match["rule"].strip() or match["rule"].lstrip().startswith(('"', "'", "“")):
        return None
    from twill_redactor import redact_text
    if redact_text(text) != stripped:
        return None
    prefix = stripped[:match.start("rule")].lower()
    breadth = "global" if "all future tasks" in prefix else "repository"
    return {"instruction": stripped, "breadth": breadth}


def classify(instruction: str) -> str | None:
    """Route only an exact, independently covered continuation rule body."""
    directive = extract_directive(instruction)
    if directive is None:
        return None
    match = _PREFIX.fullmatch(instruction.strip())
    if match is None:
        return None
    rule = match["rule"].strip().lower()
    known = {
        "continue authorized work after answering status questions; finish the authorized outcome, "
        "honor explicit cancellation, and ask for approval when required.",
        "after answering a status question, continue the authorized task until complete unless "
        "i stop it or required approval is missing.",
    }
    return "authorized-continuation/v1" if rule in known else None


def validate_policy(policy: dict) -> None:
    """Require explicit, exact scope enrollment before reading transcript globs."""
    _validate_intake(policy)
    entry = policy.get("human_steering", {})
    if not isinstance(entry, dict) or set(entry) - {"enabled", "scopes"}:
        raise ValueError("human_steering accepts only enabled and exact scopes")
    if "enabled" in entry and type(entry["enabled"]) is not bool:
        raise ValueError("human_steering enabled must be boolean")
    scopes = entry.get("scopes", [])
    if (not isinstance(scopes, list) or any(not isinstance(name, str) for name in scopes) or
            len(scopes) != len(set(scopes)) or
            any(name not in policy.get("scopes", {}) for name in scopes) or
            entry.get("enabled") is True and not scopes):
        raise ValueError("human_steering scopes must be unique enrolled scope names")
    for name in scopes:
        config = policy["scopes"][name]
        if not isinstance(config, dict):
            raise ValueError("human_steering scope must be a mapping")
        dirs = config.get("launchdirs", [])
        if (not isinstance(dirs, list) or any(not _canonical_string(p) for p in dirs) or
                config.get("global") is not True and not dirs):
            raise ValueError("human_steering scopes need canonical launchdirs or global:true")


def _canonical_string(value: object) -> bool:
    return (isinstance(value, str) and value.startswith("/") and
            str(Path(value).resolve()) == value)


def resolve_scope(evidence: dict, policy: dict) -> str | None:
    """Resolve explicit breadth, with no repository-to-global fallback."""
    validate_policy(policy)
    entry = policy.get("human_steering", {})
    if entry.get("enabled") is not True or not evidence.get("complete"):
        return None
    names = entry["scopes"]
    if evidence.get("breadth") == "global":
        matches = [name for name in names if policy["scopes"][name].get("global") is True]
        return matches[0] if len(matches) == 1 else None
    dirs = evidence.get("launchdirs", [])
    if evidence.get("breadth") != "repository" or not dirs or any(not _canonical_string(d) for d in dirs):
        return None
    matches = []
    for name in names:
        config = policy["scopes"][name]
        if config.get("global") is True:
            continue
        roots = config.get("launchdirs", [])
        depths = []
        for directory in dirs:
            covering = [len(root) for root in roots if directory == root or directory.startswith(root.rstrip("/") + "/")]
            if not covering:
                break
            depths.append(max(covering))
        else:
            matches.append((min(depths), name))
    if not matches:
        return None
    best = max(depth for depth, _ in matches)
    winners = [name for depth, name in matches if depth == best]
    return winners[0] if len(winners) == 1 else None


def _regular(path: Path, limit: int) -> bool:
    return (path.is_absolute() and path.absolute() == path.resolve() and
            not path.is_symlink() and path.is_file() and path.stat().st_size <= limit)


def _fingerprint(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_ino]


def _full_hash(path: Path, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("source scan budget exhausted")
            digest.update(data)
    return digest.hexdigest()


def _valid_cursor(cursor: object) -> bool:
    if not isinstance(cursor, dict):
        return False
    fingerprint = cursor.get("fingerprint")
    return (isinstance(fingerprint, list) and len(fingerprint) == 3 and
            all(type(value) is int and value >= 0 for value in fingerprint) and
            type(cursor.get("offset")) is int and 0 <= cursor["offset"] <= MAX_SOURCE_BYTES and
            type(cursor.get("line")) is int and 0 <= cursor["line"] <= MAX_RECORDS and
            isinstance(cursor.get("tools"), dict) and len(cursor["tools"]) <= 128 and
            all(isinstance(k, str) and isinstance(v, str) for k, v in cursor["tools"].items()) and
            type(cursor.get("pending")) is bool and type(cursor.get("done")) is bool)


def _human_turn(raw: dict, number: int) -> dict | None:
    """Use native role parsing, but never synthesize a message from fragments."""
    if raw.get("role") not in (None, "user") or raw.get("channel") in {"analysis", "commentary"}:
        return None
    if raw.get("type") == "response_item":
        message = raw.get("payload")
        if not isinstance(message, dict) or message.get("type") not in (None, "message"):
            return None
        allowed = {"input_text", "text"}
    elif raw.get("type") == "user":
        message = raw.get("message")
        allowed = {"text"}
    else:
        return None
    if not isinstance(message, dict) or message.get("role", "user") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str) and raw.get("type") == "user":
        text = content
    elif (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) and
          content[0].get("type") in allowed and isinstance(content[0].get("text"), str)):
        text = content[0]["text"]
    else:
        return None
    turn = _native_workflow_turn(raw, number)
    if turn is None or turn["role"] != "user":
        return None
    return dict(turn, text=text)


def verify(source_path: str | Path, instruction: str | None = None) -> dict:
    """Re-read the whole snapshot, including subsequent human withdrawals."""
    result = {"type": "human-steering", "complete": False, "instruction": None,
              "breadth": None, "launchdirs": [], "references": [], "raw_sources": []}
    try:
        path = Path(source_path)
        if not _regular(path, MAX_LINE_BYTES):
            return result
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            return result
        refs, sources = payload.get("references"), payload.get("raw_sources")
        if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict):
            return result
        ref = refs[0]
        raw_path, number, expected = ref.get("source_path"), ref.get("source_line"), ref.get("sha256")
        if (not _canonical_string(raw_path) or type(number) is not int or not 1 <= number <= MAX_RECORDS or
                not isinstance(expected, str) or not _HEX.fullmatch(expected) or ref.get("role") != "user" or
                not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict) or
                sources[0].get("path") != raw_path or not isinstance(sources[0].get("sha256"), str) or
                not _HEX.fullmatch(sources[0]["sha256"])):
            return result
        raw_file = Path(raw_path)
        if not _regular(raw_file, MAX_SOURCE_BYTES):
            return result
        before = _fingerprint(raw_file)
        if _full_hash(raw_file) != sources[0]["sha256"]:
            return result
        tools, pending, cwd, found, directive = {}, False, None, None, None
        with raw_file.open("rb") as stream:
            for line_number in range(1, MAX_RECORDS + 2):
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if (line_number > MAX_RECORDS or len(line) > MAX_LINE_BYTES or
                        not line.endswith(b"\n")):
                    return result
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    return result
                if line_number == 1 and raw.get("type") == "session_meta":
                    meta = raw.get("payload")
                    if isinstance(meta, dict) and _canonical_string(meta.get("cwd")):
                        cwd = meta["cwd"]
                if _interactive_boundary(raw, tools):
                    pending = True
                    if line_number == number:
                        return result
                    continue
                turn = _native_workflow_turn(raw, line_number)
                if turn and turn["role"] == "user" and pending:
                    pending = False
                    if line_number == number:
                        return result
                    continue
                if line_number == number:
                    if hashlib.sha256(line).hexdigest() != expected:
                        return result
                    found = _human_turn(raw, number)
                    directive = extract_directive(found["text"]) if found else None
                    if directive is None or instruction is not None and directive["instruction"] != instruction:
                        return result
                elif line_number > number and directive is not None:
                    later = _human_turn(raw, line_number)
                    if later is None:
                        continue
                    text = later["text"].strip()
                    replacement = extract_directive(text)
                    # Later native human authority can retract or supersede
                    # a rule. A new snapshot must not resurrect its old line.
                    if ((replacement is not None and replacement["breadth"] == directive["breadth"]) or
                            not text.startswith((">", "\"", "'", "<", "```")) and _RETRACTION.search(text)):
                        return result
        if found is None or before != _fingerprint(raw_file):
            return result
        if directive is None:
            return result
        captured = found["cwd"] if found["provider"] == "claude" else cwd
        result.update(directive, complete=True,
                      launchdirs=[captured] if _canonical_string(captured) else [],
                      references=[{"source_path_sha256": hashlib.sha256(raw_path.encode()).hexdigest(),
                                   "source_line": number, "sha256": expected, "role": "user"}],
                      raw_sources=[{"path": raw_path, "sha256": sources[0]["sha256"]}])
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        pass
    return result


def discover(policy: dict, artifacts_root: Path) -> list[Path]:
    """Discover settled explicit directives with bounded resumable native scans."""
    validate_policy(policy)
    if policy.get("human_steering", {}).get("enabled") is not True or not policy.get("intervention_sources"):
        return []
    from twill_lessons import lessons_dir
    root = Path(artifacts_root).expanduser().resolve()
    lessons_dir(root)
    directory = root / "learning" / "sources"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.absolute() != directory.resolve():
        raise ValueError("steering discovery destination cannot traverse symlinks")
    checkpoint = directory / ".human-steering-discovery.json"
    state = {"schema": "twill-human-steering-discovery/v1", "files": {}, "outputs": []}
    if checkpoint.exists():
        if not _regular(checkpoint, MAX_LINE_BYTES):
            raise ValueError("steering discovery state must be a bounded regular file")
        state = json.loads(checkpoint.read_text())
        if (not isinstance(state, dict) or state.get("schema") != "twill-human-steering-discovery/v1" or
                not isinstance(state.get("files"), dict) or not isinstance(state.get("outputs"), list) or
                len(state["files"]) > 4096 or len(state["outputs"]) > 256 or
                any(not _canonical_string(k) or not _valid_cursor(v) for k, v in state["files"].items()) or
                any(not isinstance(n, str) or not re.fullmatch(r"auto-human-steering-[0-9a-f]{24}\.json", n)
                    for n in state["outputs"])):
            raise ValueError("invalid steering discovery state")
    with DiscoveryQueue(root, "human-steering", policy, MAX_SOURCE_BYTES) as queue:
        queue.import_legacy(state)
        deadline = time.monotonic() + queue.limits["scan_seconds"]
        queue.inventory(deadline)
        for value, fingerprint, previous in queue.pending(deadline):
            path = Path(value)
            current = previous if previous.get("fingerprint") == fingerprint else {
                "fingerprint": fingerprint, "offset": 0, "line": 0, "tools": {}, "pending": False, "done": False}
            found = []
            try:
                snapshot = _full_hash(path, deadline)
                with path.open("rb") as stream:
                    stream.seek(current["offset"])
                    for _ in range(50000):
                        if time.monotonic() >= deadline:
                            break
                        line = stream.readline(MAX_LINE_BYTES + 1)
                        if not line:
                            current["done"] = True
                            break
                        if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n") or current["line"] >= MAX_RECORDS:
                            raise ValueError("unbounded native record")
                        current.update(offset=stream.tell(), line=current["line"] + 1)
                        raw = json.loads(line)
                        if not isinstance(raw, dict):
                            raise ValueError("native record must be a mapping")
                        if _interactive_boundary(raw, current["tools"]):
                            current["pending"] = True
                            continue
                        turn = _native_workflow_turn(raw, current["line"])
                        if turn and turn["role"] == "user" and current["pending"]:
                            current["pending"] = False
                            continue
                        human = _human_turn(raw, current["line"])
                        if human is None or extract_directive(human["text"]) is None:
                            continue
                        found.append({"source_path": value, "source_line": current["line"],
                                      "sha256": hashlib.sha256(line).hexdigest(), "role": "user"})
                        if len(found) >= 16:
                            break
                if fingerprint != _fingerprint(path):
                    continue
                queue.save(value, current)
                for ref in found:
                    if time.monotonic() >= deadline:
                        # Do not advance past an unpublished directive when the
                        # cooperative budget expires during this source scan.
                        queue.save(value, {"fingerprint": fingerprint, "offset": 0, "line": 0,
                                           "tools": {}, "pending": False, "done": False})
                        break
                    payload = {"schema": SCHEMA, "references": [ref], "raw_sources": [{"path": value, "sha256": snapshot}]}
                    ident = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
                    target = directory / f"auto-human-steering-{ident}.json"
                    _private_json(target, payload)
                    evidence = verify(target)
                    if resolve_scope(evidence, policy) is not None and target.name not in state["outputs"]:
                        queue.add_output(target.name)
            except TimeoutError:
                queue.budget("source_time")
                break
            except (OSError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
                current["done"] = True
                queue.save(value, current, error=True)
        state = queue.finish(state)
    _private_json(checkpoint, state)
    return [directory / name for name in state["outputs"] if (directory / name).is_file()]
