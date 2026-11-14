"""Optional bounded S3-to-private-native-JSONL producer.

The core never imports this module. Only LIST and GET are used remotely. A
completed inventory sweep is required before a settled artifact can be emitted;
"complete" describes the observed archived generation, never session termination.
All source identity and provenance stay in private state and sidecars.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
from urllib.parse import urlsplit
import zlib


_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_AGENT_NAMES = {"claude": "claude-code", "claude-code": "claude-code", "claude_code": "claude-code",
                "codex": "codex", "codex-cli": "codex", "codex_cli": "codex"}
_AGENTS = {"claude-code", "codex"}
_REPOSITORY = Path(__file__).resolve().parent
_MANIFEST_LIMIT = 256 * 1024


class FeedError(Exception):
    """An intentionally payload-free failure category."""


class BudgetExceeded(FeedError):
    pass


class InvalidPolicy(FeedError):
    pass


class InvalidReference(FeedError):
    pass


class IntegrityFailure(FeedError):
    pass


class UnknownCompleteness(FeedError):
    pass


class OversizedArtifact(UnknownCompleteness):
    pass


class UnsupportedArtifact(UnknownCompleteness):
    pass


class RemoteReadFailure(FeedError):
    pass


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise InvalidReference()
    return value


def _contained(path, root):
    return path == root or root in path.parents


def _private_path(value):
    if not isinstance(value, (str, Path)):
        raise InvalidPolicy()
    path = Path(value)
    if not path.is_absolute() or path != path.resolve():
        raise InvalidPolicy()
    # These paths are rejected by name; their contents are never inspected.
    forbidden = (_REPOSITORY, Path.home() / "agent-transcript-archive")
    if any(_contained(path, root) or _contained(root, path) for root in forbidden):
        raise InvalidPolicy()
    return path


def _prefix(value):
    if (not isinstance(value, str) or not value.endswith("/")
            or value.startswith("/") or "\\" in value
            or any(part in ("", ".", "..") for part in value[:-1].split("/"))):
        raise InvalidPolicy()
    return value


def _policy(value):
    if not isinstance(value, dict) or value.get("schema_version") != "twill-archive-feed/v1":
        raise InvalidPolicy()
    if type(value.get("enabled")) is not bool:
        raise InvalidPolicy()
    if not value["enabled"]:
        return dict(value)
    result = dict(value)
    for field in ("feed_root", "state_root", "credentials_file"):
        result[field] = _private_path(value.get(field))
    if (_contained(result["feed_root"], result["state_root"])
            or _contained(result["state_root"], result["feed_root"])):
        raise InvalidPolicy()
    for field in ("refs_prefix", "blobs_prefix"):
        result[field] = _prefix(value.get(field))
    endpoint = value.get("endpoint_url")
    if not isinstance(endpoint, str):
        raise InvalidPolicy()
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise InvalidPolicy()
    if not isinstance(value.get("bucket"), str) or not value["bucket"].strip():
        raise InvalidPolicy()
    agents = value.get("include_agents")
    if not isinstance(agents, list) or not agents or any(a not in _AGENT_NAMES for a in agents):
        raise InvalidPolicy()
    result["include_agents"] = sorted({_AGENT_NAMES[a] for a in agents})
    cache_cap = value.get("max_cache_bytes", 4 * 1024 ** 3)
    if type(cache_cap) is not int or cache_cap <= 0:
        raise InvalidPolicy()
    result["max_cache_bytes"] = cache_cap
    retention = value.get("retention", {"enabled": False})
    if not isinstance(retention, dict) or type(retention.get("enabled")) is not bool:
        raise InvalidPolicy()
    result["retention"] = dict(retention)
    if retention["enabled"]:
        root = _private_path(retention.get("learning_root"))
        if any(_contained(root, result[k]) or _contained(result[k], root)
               for k in ("feed_root", "state_root")):
            raise InvalidPolicy()
        result["retention"]["learning_root"] = root
        for key, default in (("max_files", 128), ("max_bytes", 256 * 1024 ** 2), ("max_seconds", 10)):
            number = retention.get(key, default)
            if type(number) is not int or number <= 0:
                raise InvalidPolicy()
            result["retention"][key] = number
    for field in ("max_refs", "max_pages", "max_sessions", "max_bytes", "max_seconds"):
        if type(value.get(field)) is not int or value[field] <= 0:
            raise InvalidPolicy()
    if type(value.get("settle_seconds")) is not int or value["settle_seconds"] < 0:
        raise InvalidPolicy()
    seeds = value.get("seed_refs", [])
    if not isinstance(seeds, list) or len(seeds) > 256:
        raise InvalidPolicy()
    try:
        groups = sorted({"/".join(_key_parts(key, result["refs_prefix"])[:4]) for key in seeds})
    except InvalidReference:
        raise InvalidPolicy() from None
    if any(_AGENT_NAMES.get(group.split("/")[1]) not in result["include_agents"] for group in groups):
        raise InvalidPolicy()
    result["seed_groups"] = groups
    return result


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise InvalidPolicy()


def _private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > _MANIFEST_LIMIT):
            raise InvalidPolicy()
        return json.loads(stream.read(_MANIFEST_LIMIT + 1))


class _Budget:
    def __init__(self, policy, report):
        self.deadline = time.monotonic() + policy["max_seconds"]
        self.remaining = policy["max_bytes"]
        self.report = report

    def check(self, size=0):
        if time.monotonic() >= self.deadline or size > self.remaining:
            raise BudgetExceeded()

    def consume(self, size):
        self.check(size)
        self.remaining -= size
        self.report["bytes"] += size


def _get(client, policy, key, budget, limit):
    budget.check()
    try:
        response = client.get_object(Bucket=policy["bucket"], Key=key)
    except Exception:
        raise RemoteReadFailure() from None
    body = response.get("Body")
    if body is None:
        raise RemoteReadFailure()
    try:
        length = response.get("ContentLength")
        if length is not None and (type(length) is not int or length < 0 or length > limit):
            raise IntegrityFailure()
        if length is not None:
            budget.check(length)
        data = bytearray()
        while True:
            budget.check()
            if length is not None and len(data) == length:
                break
            budget.check(1)
            chunk = body.read(min(64 * 1024, limit - len(data) + 1, budget.remaining))
            if not chunk:
                break
            budget.consume(len(chunk))
            data.extend(chunk)
            if len(data) > limit:
                raise IntegrityFailure()
        if length is not None and len(data) != length:
            raise IntegrityFailure()
        return bytes(data)
    except FeedError:
        raise
    except Exception:
        raise RemoteReadFailure() from None
    finally:
        body.close()


def _key_parts(key, prefix):
    if not isinstance(key, str) or not key.startswith(prefix):
        raise InvalidReference()
    parts = key[len(prefix):].split("/")
    if (len(parts) != 5 or not parts[-1].endswith(".json")
            or any(p in (".", "..") or not _SEGMENT.fullmatch(p) for p in parts)):
        raise InvalidReference()
    return parts


def _timestamp(value):
    if not isinstance(value, str):
        raise InvalidReference()
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.timestamp()
    except (ValueError, OverflowError):
        raise InvalidReference() from None


def _manifest(key, raw, policy):
    parts = _key_parts(key, policy["refs_prefix"])
    try:
        m = json.loads(raw)
        if not isinstance(m, dict) or type(m.get("schema_version")) is not int or m["schema_version"] != 1:
            raise InvalidReference()
        host, agent, session, artifact, _ = parts
        if m.get("host") != host or m.get("source") != agent or m.get("session_id") != session:
            raise InvalidReference()
        source_key = m.get("source_key")
        if not isinstance(source_key, str) or not source_key:
            raise InvalidReference()
        if hashlib.sha256(source_key.encode()).hexdigest()[:24] != artifact:
            raise InvalidReference()
        ref_key = m.get("reference_object_key")
        if not isinstance(ref_key, str) or not (key == ref_key or key.endswith("/" + ref_key)):
            raise InvalidReference()
        if m.get("raw_tier") is not True or m.get("sanitized") is not False:
            raise InvalidReference()
        if m.get("role") not in ("session", "rollout", "legacy_session_snapshot"):
            raise UnsupportedArtifact()
        _timestamp(m.get("captured_at"))
        start, end = (_integer(m["range"][x]) for x in ("start", "end"))
        if end <= start:
            raise InvalidReference()
        blob = m["blob"]
        if not all(isinstance(blob.get(k), str) and _SHA.fullmatch(blob[k])
                   for k in ("raw_sha256", "stored_sha256")):
            raise InvalidReference()
        if blob.get("content_encoding") != "gzip" or _integer(blob["raw_bytes"]) != end - start:
            raise InvalidReference()
        _integer(blob["stored_bytes"], 1)
        digest = blob["raw_sha256"]
        expected = f"{policy['blobs_prefix']}{digest[:2]}/{digest}.gz"
        object_key = blob.get("object_key")
        if not isinstance(object_key, str) or not (expected == object_key or expected.endswith("/" + object_key)):
            raise InvalidReference()
        cursor = m["cursor"]
        if cursor["kind"] == "file":
            for name in ("generation", "device", "inode"):
                _integer(cursor[name])
            if _integer(cursor["ack_offset"]) != end:
                raise InvalidReference()
        elif cursor["kind"] == "snapshot":
            if start != 0 or not isinstance(cursor.get("source_revision"), str) or not cursor["source_revision"]:
                raise InvalidReference()
        else:
            raise UnknownCompleteness()
        if any(m.get(flag) is True for flag in ("partial", "is_partial", "truncated")):
            raise UnknownCompleteness()
        if any(c.get("complete") is False or c.get("partial") is True or c.get("is_partial") is True
               for c in (m, m["range"], cursor)):
            raise UnknownCompleteness()
        return m
    except FeedError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise InvalidReference() from None


def _decode_blob(client, policy, manifest, budget):
    blob = manifest["blob"]
    budget.check(blob["stored_bytes"] + blob["raw_bytes"])
    digest = blob["raw_sha256"]
    key = f"{policy['blobs_prefix']}{digest[:2]}/{digest}.gz"
    stored = _get(client, policy, key, budget, blob["stored_bytes"])
    if len(stored) != blob["stored_bytes"] or hashlib.sha256(stored).hexdigest() != blob["stored_sha256"]:
        raise IntegrityFailure()
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        # max_length prevents an untrusted compressed stream from allocating
        # past either the manifest's declared size or this cycle's budget.
        raw = decoder.decompress(stored, min(blob["raw_bytes"], budget.remaining) + 1)
        budget.consume(len(raw))
        if (len(raw) != blob["raw_bytes"] or not decoder.eof or decoder.unused_data
                or decoder.unconsumed_tail or hashlib.sha256(raw).hexdigest() != digest):
            raise IntegrityFailure()
        return raw
    except zlib.error:
        raise IntegrityFailure() from None


def _selection(rows, policy, now):
    if rows and all(m and m.get("_error") == "UnsupportedArtifact" for _, m in rows):
        raise UnsupportedArtifact()
    if any(m and m.get("_error") == "RemoteReadFailure" for _, m in rows):
        raise RemoteReadFailure()
    if not rows or any(m is None or "_error" in m for _, m in rows):
        raise UnknownCompleteness()
    kinds = {m["cursor"]["kind"] for _, m in rows}
    if len(kinds) != 1:
        raise UnknownCompleteness()
    if kinds == {"file"}:
        latest_generation = max(m["cursor"]["generation"] for _, m in rows)
        candidates = [(key, m) for key, m in rows if m["cursor"]["generation"] == latest_generation]
        if len({(m["cursor"]["device"], m["cursor"]["inode"]) for _, m in candidates}) != 1:
            raise UnknownCompleteness()
        newest = max(candidates, key=lambda r: (r[1]["range"]["end"], _timestamp(r[1]["captured_at"])))[1]
    else:
        newest = max(rows, key=lambda r: _timestamp(r[1]["captured_at"]))[1]
        latest_time = _timestamp(newest["captured_at"])
        if len({(m["cursor"]["source_revision"], m["blob"]["raw_sha256"])
                for _, m in rows if _timestamp(m["captured_at"]) == latest_time}) != 1:
            raise UnknownCompleteness()
    if now - _timestamp(newest["captured_at"]) < policy["settle_seconds"]:
        raise UnknownCompleteness()
    cursor = newest["cursor"]
    if cursor["kind"] == "snapshot":
        selected = [(key, m) for key, m in rows if m["cursor"] == cursor]
        if len({m["blob"]["raw_sha256"] for _, m in selected}) != 1:
            raise UnknownCompleteness()
        return [selected[-1]]
    identity = tuple(cursor[x] for x in ("generation", "device", "inode"))
    selected = [(key, m) for key, m in rows if m["cursor"]["kind"] == "file"
                and tuple(m["cursor"][x] for x in ("generation", "device", "inode")) == identity]
    # Repeated immutable refs may describe an identical interval. Conflicting
    # duplicates and overlaps are never silently resolved.
    unique = {}
    for key, m in selected:
        interval = (m["range"]["start"], m["range"]["end"])
        if interval in unique and unique[interval][1]["blob"]["raw_sha256"] != m["blob"]["raw_sha256"]:
            raise UnknownCompleteness()
        unique[interval] = (key, m)
    selected = [unique[k] for k in sorted(unique)]
    offset = 0
    for _, m in selected:
        if m["range"]["start"] != offset:
            raise UnknownCompleteness()
        offset = m["range"]["end"]
    if not selected or offset != max(m["cursor"]["ack_offset"] for _, m in selected):
        raise UnknownCompleteness()
    # Every selected chunk must also be settled (capture clocks may differ).
    if any(now - _timestamp(m["captured_at"]) < policy["settle_seconds"] for _, m in selected):
        raise UnknownCompleteness()
    return selected


def _native(raw, agent, budget):
    if not raw or not raw.endswith(b"\n"):
        raise UnknownCompleteness()
    try:
        records = []
        for line in raw.splitlines():
            budget.check()
            if line.strip():
                records.append(json.loads(line))
    except (ValueError, UnicodeError):
        raise UnknownCompleteness() from None
    if not records or any(not isinstance(r, dict) for r in records):
        raise UnknownCompleteness()
    if agent == "codex":
        if not any(r.get("type") == "session_meta" and isinstance(r.get("payload"), dict) for r in records):
            raise UnknownCompleteness()
    elif not any(r.get("type") in ("user", "assistant") and isinstance(r.get("message"), dict) for r in records):
        raise UnknownCompleteness()


def _atomic(path, data):
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _current(policy, group):
    agent = _AGENT_NAMES[group.split("/")[1]]
    directory = policy["feed_root"] / "current" / agent
    _private_directory(policy["feed_root"] / "current")
    _private_directory(directory)
    return directory / (hashlib.sha256(group.encode()).hexdigest() + ".jsonl")


def _withdraw(connection, policy, group, report):
    target = _current(policy, group)
    if target.exists() or target.is_symlink():
        target.unlink()
        report["withdrawn"] += 1
    sidecar = target.with_suffix(".manifest.json")
    if sidecar.exists() or sidecar.is_symlink():
        sidecar.unlink()
    connection.execute("DELETE FROM current_groups WHERE group_id=?", (group,))
    connection.commit()


def _emit(connection, client, policy, rows, group, budget, report):
    selected = _selection(rows, policy, time.time())
    version = _source_version(policy, rows)
    if _already_consumed(connection, policy, group, version):
        report["consumed_unchanged"] += 1
        return
    manifest = selected[-1][1]
    agent = _AGENT_NAMES[manifest["source"]]
    declared_bytes = sum(m["blob"]["raw_bytes"] + m["blob"]["stored_bytes"] for _, m in selected)
    # A complete artifact must fit a fresh cycle. Holding its cursor could
    # otherwise starve every later group forever. A merely spent run budget
    # still defers this group until the next cycle.
    if declared_bytes > policy["max_bytes"]:
        raise OversizedArtifact()
    budget.check(declared_bytes)
    raw = b"".join(_decode_blob(client, policy, m, budget) for _, m in selected)
    _native(raw, agent, budget)
    digest = hashlib.sha256(raw).hexdigest()
    _private_directory(policy["feed_root"] / "objects")
    directory = policy["feed_root"] / "objects" / agent
    _private_directory(directory)
    target = directory / f"{digest}.jsonl"
    sidecar = {
        "schema_version": "twill-archive-native/v1", "raw_sha256": digest,
        "raw_bytes": len(raw), "provider": agent, "host": manifest["host"],
        "session_id": manifest["session_id"], "source_key": manifest["source_key"],
        "reference_keys": [key for key, _ in selected],
        "file_identity": manifest["cursor"], "captured_at": manifest["captured_at"],
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "complete_observed_generation": True,
        "completeness_basis": "immutable_snapshot" if manifest["cursor"]["kind"] == "snapshot"
        else "settled_contiguous_archived_offsets",
        "observed_end": len(raw), "session_ended": "unknown",
        "source_version": version,
    }
    sidecar_bytes = (json.dumps(sidecar, sort_keys=True) + "\n").encode()
    cached = connection.execute("SELECT bytes FROM cache WHERE agent=? AND digest=?", (agent, digest)).fetchone()
    used = connection.execute("SELECT coalesce(sum(bytes),0) FROM cache").fetchone()[0]
    used += connection.execute("SELECT coalesce(sum(metadata_bytes),0) FROM current_groups").fetchone()[0]
    previous = connection.execute("SELECT metadata_bytes FROM current_groups WHERE group_id=?", (group,)).fetchone()
    required = (0 if cached else len(raw)) + len(sidecar_bytes) - (previous[0] if previous else 0)
    if used + required > policy["max_cache_bytes"]:
        _reap(connection, policy, report)
        used = _cache_bytes(connection)
        cached = connection.execute("SELECT bytes FROM cache WHERE agent=? AND digest=?", (agent, digest)).fetchone()
        previous = connection.execute("SELECT metadata_bytes FROM current_groups WHERE group_id=?", (group,)).fetchone()
        required = (0 if cached else len(raw)) + len(sidecar_bytes) - (previous[0] if previous else 0)
    if used + required > policy["max_cache_bytes"]:
        report["cache_limit"] = True
        raise BudgetExceeded()
    if target.is_symlink():
        raise InvalidPolicy()
    if target.exists():
        if (target.stat().st_mode & 0o777 != 0o600 or target.stat().st_size != len(raw)
                or hashlib.sha256(target.read_bytes()).hexdigest() != digest):
            raise IntegrityFailure()
    else:
        _atomic(target, raw)
    connection.execute("INSERT OR IGNORE INTO cache VALUES (?,?,?)", (agent, digest, len(raw)))
    connection.execute("INSERT OR REPLACE INTO current_groups VALUES (?,?)", (group, len(sidecar_bytes)))
    connection.commit()
    current = _current(policy, group)
    if current.is_symlink():
        raise InvalidPolicy()
    if (current.exists() and current.stat().st_size == len(raw)
            and hashlib.sha256(current.read_bytes()).hexdigest() == digest):
        if stat.S_IMODE(current.stat().st_mode) != 0o600:
            raise InvalidPolicy()
        report["unchanged"] += 1
    else:
        # Only our immutable private object is linked. Atomic replacement of
        # the current directory entry preserves old bytes for existing readers.
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=current.parent)
        os.close(fd)
        try:
            os.unlink(temporary)
            os.link(target, temporary, follow_symlinks=False)
            os.replace(temporary, current)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        stamp = max(_timestamp(m["captured_at"]) for _, m in selected)
        os.utime(current, (stamp, stamp))
        report["emitted"] += 1
    _atomic(current.with_suffix(".manifest.json"), sidecar_bytes)


def _source_version(policy, rows):
    namespace = {name: str(policy[name]) for name in
                 ("endpoint_url", "bucket", "refs_prefix", "blobs_prefix")}
    return hashlib.sha256(json.dumps([namespace, sorted(rows)], sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _cache_bytes(connection):
    return (connection.execute("SELECT coalesce(sum(bytes),0) FROM cache").fetchone()[0]
            + connection.execute("SELECT coalesce(sum(metadata_bytes),0) FROM current_groups").fetchone()[0])


def _retention_state(policy):
    from twill_learning_retention import consumed_sources, pinned_sources
    root = policy["retention"]["learning_root"]
    return root, consumed_sources(root), pinned_sources(root)


def _already_consumed(connection, policy, group, version):
    if not policy["retention"]["enabled"]:
        return False
    row = connection.execute("SELECT path,receipt FROM consumed WHERE group_id=? AND version=?",
                             (group, version)).fetchone()
    if not row:
        return False
    try:
        from twill_learning_retention import verified_receipt
        root, receipts, pins = _retention_state(policy)
        receipt = receipts.get(row[0])
        return (row[0] not in pins and receipt == json.loads(row[1])
                and verified_receipt(root, Path(row[0]), receipt))
    except Exception:
        return False


def _owned_regular(path):
    if path != path.resolve():
        raise InvalidPolicy()
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise InvalidPolicy()
    return info


def _raw_digest(path, deadline):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            if time.monotonic() >= deadline:
                raise BudgetExceeded()
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _reap(connection, policy, report):
    """Lock producer first, then the same learning store as the controller."""
    if not policy["retention"]["enabled"]:
        return
    from twill_lock import StateLock
    from twill_contract import LockHeldError
    directory = policy["retention"]["learning_root"] / "learning"
    try:
        _private_directory(directory)
        if (directory / "lock").is_symlink():
            raise InvalidPolicy()
        with StateLock(directory):
            _reap_locked(connection, policy, report)
    except LockHeldError:
        report["retention_busy"] = True
    except Exception:
        report["retention_unresolved"] = True


def _reap_locked(connection, policy, report):
    """Reclaim only producer-owned, exactly consumed and unpinned local bytes."""
    retention = policy["retention"]
    if not retention["enabled"]:
        return
    if "_retention_budget" not in policy:
        policy["_retention_budget"] = [retention["max_files"], retention["max_bytes"],
                                       time.monotonic() + retention["max_seconds"]]
    allowance = policy["_retention_budget"]
    if allowance[0] <= 0 or time.monotonic() >= allowance[2]:
        return
    try:
        from twill_learning_retention import verified_receipt
        # The caller holds the controller's StateLock for this entire pass.
        # Learning owners and scan receipts therefore form one stable snapshot;
        # rereading every episode per unlink would make reclamation quadratic.
        root, receipts, pins = _retention_state(policy)
        cursor = connection.execute("SELECT value FROM progress WHERE name='reap_after'").fetchone()
        after = cursor[0] if cursor else ""
        groups = connection.execute("SELECT group_id FROM current_groups WHERE group_id>? ORDER BY group_id LIMIT ?",
                                    (after, allowance[0])).fetchall()
        if not groups:
            _set(connection, "reap_after", "")
            connection.commit()
            groups = connection.execute("SELECT group_id FROM current_groups ORDER BY group_id LIMIT ?",
                                        (allowance[0],)).fetchall()
        for (group,) in groups:
            if allowance[0] <= 0 or time.monotonic() >= allowance[2]:
                break
            allowance[0] -= 1
            _set(connection, "reap_after", group)
            connection.commit()
            path = _current(policy, group)
            receipt = receipts.get(str(path))
            if str(path) in pins or not receipt or not verified_receipt(root, path, receipt):
                continue
            info = _owned_regular(path)
            if [info.st_size, info.st_mtime_ns, info.st_ino] != receipt["fingerprint"]:
                continue
            sidecar_path = path.with_suffix(".manifest.json")
            metadata = _private_read(sidecar_path)
            if (metadata.get("schema_version") != "twill-archive-native/v1"
                    or metadata.get("complete_observed_generation") is not True
                    or metadata.get("raw_bytes") != info.st_size
                    or metadata.get("provider") != path.parent.name
                    or metadata.get("raw_sha256") != receipt.get("sha256")
                    or metadata.get("source_version") != receipt.get("source_version")):
                continue
            keys = metadata.get("reference_keys")
            if (not isinstance(keys, list) or not keys
                    or any("/".join(_key_parts(key, policy["refs_prefix"])[:4]) != group for key in keys)):
                continue
            rows = [(key, json.loads(m) if m else None) for key, m in connection.execute(
                "SELECT key,manifest FROM refs WHERE group_id=? ORDER BY key", (group,))]
            selected = _selection(rows, policy, time.time())
            version = _source_version(policy, rows)
            if (keys != [key for key, _ in selected]
                    or metadata.get("source_key") != selected[-1][1]["source_key"]
                    or metadata.get("file_identity") != selected[-1][1]["cursor"]
                    or (metadata.get("source_version") is not None and metadata["source_version"] != version)):
                continue
            digest = metadata["raw_sha256"]
            if not isinstance(digest, str) or not _SHA.fullmatch(digest):
                continue
            obj = policy["feed_root"] / "objects" / path.parent.name / (digest + ".jsonl")
            obj_info = _owned_regular(obj)
            if str(obj) in pins or (info.st_dev, info.st_ino) != (obj_info.st_dev, obj_info.st_ino):
                continue
            if info.st_size + sidecar_path.stat().st_size > allowance[1]:
                continue
            if _raw_digest(path, allowance[2]) != digest:
                continue
            # Receipt and complete archive retrieval metadata survive outside
            # all enrolled raw globs before either hardlink is removed.
            durable = {"schema": "twill-archive-consumed/v1", "version": version,
                       "path": str(path), "receipt": receipt, "metadata": metadata,
                       "namespace": {k: str(policy[k]) for k in
                                     ("endpoint_url", "bucket", "refs_prefix", "blobs_prefix")},
                       "references": rows}
            directory = policy["state_root"] / "consumed"
            _private_directory(directory)
            _atomic(directory / (hashlib.sha256((group + version).encode()).hexdigest() + ".json"),
                    (json.dumps(durable, sort_keys=True) + "\n").encode())
            connection.execute("INSERT OR REPLACE INTO consumed VALUES (?,?,?,?,?,?,?)",
                               (group, version, path.parent.name, digest, str(path),
                                json.dumps(receipt, sort_keys=True), json.dumps(metadata, sort_keys=True)))
            connection.commit()
            # Recheck exact inode and metadata just before unlinking. A reader
            # changing the source or ownership state cannot grant deletion.
            check = path.lstat()
            if ((check.st_dev, check.st_ino, check.st_size, check.st_mtime_ns, check.st_mode) !=
                    (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)
                    or _private_read(sidecar_path) != metadata):
                continue
            metadata_bytes = sidecar_path.stat().st_size
            path.unlink()
            sidecar_path.unlink()
            connection.execute("DELETE FROM current_groups WHERE group_id=?", (group,))
            connection.commit()
            allowance[1] -= metadata_bytes
            report["reaped_current"] += 1
            report["reclaimed_bytes"] += metadata_bytes
        # An immutable inode remains accounted until every current link and
        # every explicit source owner is gone. Never prune unproven history.
        for agent, digest, size in connection.execute(
                "SELECT DISTINCT c.agent,c.digest,c.bytes FROM cache c JOIN consumed x "
                "ON c.agent=x.agent AND c.digest=x.digest LIMIT ?", (allowance[0],)).fetchall():
            if allowance[0] <= 0 or time.monotonic() >= allowance[2]:
                break
            allowance[0] -= 1
            if agent not in _AGENTS or not _SHA.fullmatch(digest):
                raise InvalidPolicy()
            if size > allowance[1]:
                continue
            proofs = connection.execute("SELECT path,receipt FROM consumed WHERE agent=? AND digest=?",
                                        (agent, digest)).fetchall()
            if not proofs or any(receipts.get(path) != json.loads(proof)
                                 or not verified_receipt(root, Path(path), json.loads(proof))
                                 for path, proof in proofs):
                continue
            obj = policy["feed_root"] / "objects" / agent / (digest + ".jsonl")
            if str(obj) in pins or any(path in pins for (path,) in connection.execute(
                    "SELECT path FROM consumed WHERE agent=? AND digest=?", (agent, digest))):
                continue
            if not obj.exists():
                continue
            info = _owned_regular(obj)
            if info.st_nlink != 1 or info.st_size != size or _raw_digest(obj, allowance[2]) != digest:
                continue
            obj.unlink()
            connection.execute("DELETE FROM cache WHERE agent=? AND digest=?", (agent, digest))
            connection.commit()
            report["reaped_objects"] += 1
            report["reclaimed_bytes"] += size
            allowance[1] -= size
    except BudgetExceeded:
        report["retention_bounded"] = True
    except Exception:
        # This includes malformed owners, missing adapters and unavailable
        # parser state. No ownership guess is a deletion authorization.
        report["retention_unresolved"] = True


def reap(policy):
    """Bounded manual local-only recovery; never contacts archive storage."""
    report = {"reaped_current": 0, "reaped_objects": 0, "reclaimed_bytes": 0,
              "retention_unresolved": False, "retention_bounded": False, "retention_busy": False}
    connection = None
    lock = None
    try:
        policy = _policy(policy)
        if not policy["enabled"] or not policy["retention"]["enabled"]:
            return report
        _private_directory(policy["feed_root"])
        _private_directory(policy["state_root"])
        lock = os.open(policy["state_root"] / "cycle.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        connection = _database(policy["state_root"] / "progress.sqlite3")
        budget = _Budget(policy, {"bytes": 0})
        _cache_inventory(connection, policy, budget)
        _current_inventory(connection, policy, budget)
        _reap(connection, policy, report)
        report["cache_bytes"] = _cache_bytes(connection)
    except Exception:
        report["retention_unresolved"] = True
    finally:
        if connection is not None:
            connection.close()
        if lock is not None:
            os.close(lock)
    return report


def _database(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    os.close(fd)
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()):
        raise InvalidPolicy()
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS progress (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS refs (
            key TEXT PRIMARY KEY, group_id TEXT NOT NULL, epoch INTEGER NOT NULL,
            manifest TEXT);
        CREATE INDEX IF NOT EXISTS refs_group ON refs(epoch, group_id);
        CREATE TABLE IF NOT EXISTS cache (
            agent TEXT NOT NULL, digest TEXT NOT NULL, bytes INTEGER NOT NULL,
            PRIMARY KEY(agent,digest));
        CREATE TABLE IF NOT EXISTS current_groups (
            group_id TEXT PRIMARY KEY, metadata_bytes INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS consumed (
            group_id TEXT NOT NULL, version TEXT NOT NULL, agent TEXT NOT NULL,
            digest TEXT NOT NULL, path TEXT NOT NULL, receipt TEXT NOT NULL,
            metadata TEXT NOT NULL, PRIMARY KEY(group_id,version));
    """)
    if "metadata_bytes" not in {row[1] for row in connection.execute("PRAGMA table_info(current_groups)")}:
        connection.execute("ALTER TABLE current_groups ADD COLUMN metadata_bytes INTEGER NOT NULL DEFAULT 0")
        connection.execute("DELETE FROM progress WHERE name='current_indexed'")
        connection.commit()
    if "provider" not in {row[1] for row in connection.execute("PRAGMA table_info(refs)")}:
        connection.execute("ALTER TABLE refs ADD COLUMN provider TEXT NOT NULL DEFAULT ''")
        # Upgrade existing inventories in one pass, without remote reads or
        # resetting their durable continuation/cursor state.
        connection.execute("""
            UPDATE refs SET provider = CASE
              WHEN substr(group_id,instr(group_id,'/')+1) GLOB 'claude/*'
                OR substr(group_id,instr(group_id,'/')+1) GLOB 'claude-code/*'
                OR substr(group_id,instr(group_id,'/')+1) GLOB 'claude_code/*' THEN 'claude-code'
              WHEN substr(group_id,instr(group_id,'/')+1) GLOB 'codex/*'
                OR substr(group_id,instr(group_id,'/')+1) GLOB 'codex-cli/*'
                OR substr(group_id,instr(group_id,'/')+1) GLOB 'codex_cli/*' THEN 'codex'
              ELSE '' END
        """)
        connection.commit()
    connection.execute("CREATE INDEX IF NOT EXISTS refs_provider ON refs(epoch,provider,group_id)")
    connection.commit()
    return connection


def _set(connection, name, value):
    connection.execute("INSERT OR REPLACE INTO progress VALUES (?,?)", (name, str(value)))


def _cache_inventory(connection, policy, budget):
    if connection.execute("SELECT 1 FROM progress WHERE name='cache_indexed'").fetchone():
        return
    for agent in sorted(_AGENTS):
        directory = policy["feed_root"] / "objects" / agent
        if not directory.exists():
            continue
        _private_directory(directory)
        for path in directory.iterdir():
            budget.check()
            if path.suffix != ".jsonl" or not _SHA.fullmatch(path.stem) or path.is_symlink():
                raise InvalidPolicy()
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise InvalidPolicy()
            connection.execute("INSERT OR IGNORE INTO cache VALUES (?,?,?)", (agent, path.stem, info.st_size))
            connection.commit()
    _set(connection, "cache_indexed", "1")
    connection.commit()


def _current_inventory(connection, policy, budget):
    """Recover ownership once for current files created before this table."""
    if connection.execute("SELECT 1 FROM progress WHERE name='current_indexed'").fetchone():
        return
    for agent in sorted(_AGENTS):
        directory = policy["feed_root"] / "current" / agent
        if not directory.exists():
            continue
        _private_directory(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                budget.check()
                if not entry.name.endswith(".manifest.json"):
                    continue
                metadata = _private_read(Path(entry.path))
                keys = metadata.get("reference_keys") if isinstance(metadata, dict) else None
                if not isinstance(keys, list) or not keys:
                    raise InvalidPolicy()
                groups = {"/".join(_key_parts(key, policy["refs_prefix"])[:4]) for key in keys}
                if len(groups) != 1:
                    raise InvalidPolicy()
                group = groups.pop()
                expected = hashlib.sha256(group.encode()).hexdigest() + ".manifest.json"
                if entry.name != expected or _AGENT_NAMES.get(group.split("/")[1]) != agent:
                    raise InvalidPolicy()
                connection.execute("INSERT OR REPLACE INTO current_groups VALUES (?,?)", (group, entry.stat().st_size))
                connection.commit()
    _set(connection, "current_indexed", "1")
    connection.commit()


def _count_unknown(policy, report, group):
    counted = policy.setdefault("_unknown_counted", set())
    if group not in counted:
        report["unknown"] += 1
        counted.add(group)


def _scan(connection, client, policy, budget, report):
    progress = dict(connection.execute("SELECT name,value FROM progress"))
    epoch = int(progress.get("epoch", "1"))
    token = progress.get("token", "")
    while report["pages"] < policy["max_pages"] and report["refs"] < policy["max_refs"]:
        budget.check()
        args = {"Bucket": policy["bucket"], "Prefix": policy.get("_scan_prefix", policy["refs_prefix"]),
                "MaxKeys": min(1000, policy["max_refs"] - report["refs"])}
        if token:
            args["ContinuationToken"] = token
        try:
            response = client.list_objects_v2(**args)
        except Exception:
            raise RemoteReadFailure() from None
        report["pages"] += 1
        items = response.get("Contents", [])
        if not isinstance(items, list) or len(items) > args["MaxKeys"]:
            raise RemoteReadFailure()
        # The page token advances only after the entire page is durable. A
        # byte/time interruption replays the page without skipping a reference.
        for item in items:
            budget.check()
            key = item.get("Key")
            report["refs"] += 1
            if not isinstance(key, str) or not key.startswith(args["Prefix"]):
                report["errors"]["InvalidListingPrefix"] += 1
                _set(connection, "scan_failed", "1")
                continue
            try:
                parts = _key_parts(key, policy["refs_prefix"])
            except InvalidReference:
                report["errors"]["InvalidReference"] += 1
                _set(connection, "scan_failed", "1")
                continue
            if _AGENT_NAMES.get(parts[1]) not in policy["include_agents"]:
                report["excluded"] += 1
                continue
            group = "/".join(parts[:4])
            existing = connection.execute("SELECT epoch,manifest FROM refs WHERE key=?", (key,)).fetchone()
            if existing and existing[0] == epoch:
                continue
            # LIST is the inventory oracle. Immutable validated manifests can
            # be reused; missing/error manifests are fetched only for selected
            # groups after the whole listing is complete.
            manifest = existing[1] if existing else None
            if manifest and "_error" in json.loads(manifest):
                manifest = None
            connection.execute("INSERT OR REPLACE INTO refs (key,group_id,epoch,manifest,provider) VALUES (?,?,?,?,?)",
                               (key, group, epoch, manifest, _AGENT_NAMES[parts[1]]))
        if response.get("IsTruncated") is True:
            next_token = response.get("NextContinuationToken")
            if not isinstance(next_token, str) or not next_token or next_token == token:
                raise RemoteReadFailure()
            token = next_token
            _set(connection, "token", token)
            connection.commit()
        else:
            _set(connection, "phase", "prune")
            _set(connection, "after", "")
            _set(connection, "token", "")
            if policy.get("_scan_group"):
                connection.execute("DELETE FROM refs WHERE group_id=? AND epoch<>?", (policy["_scan_group"], epoch))
            else:
                connection.execute("DELETE FROM refs WHERE epoch<>?", (epoch,))
            connection.commit()
            return True
    return False


def _materialize(connection, client, policy, rows, group, budget, report):
    result = []
    for key, manifest in rows:
        budget.check()
        if manifest is None:
            if report["manifest_gets"] >= policy["max_refs"]:
                raise BudgetExceeded()
            report["manifest_gets"] += 1
            try:
                manifest = _manifest(key, _get(client, policy, key, budget, _MANIFEST_LIMIT), policy)
            except BudgetExceeded:
                raise
            except UnsupportedArtifact:
                report["excluded"] += 1
                policy.setdefault("_excluded_counted", set()).add(group)
                manifest = {"_error": "UnsupportedArtifact"}
            except UnknownCompleteness:
                _count_unknown(policy, report, group)
                manifest = {"_error": "UnknownCompleteness"}
            except FeedError as error:
                report["errors"][type(error).__name__] += 1
                _set(connection, "scan_failed", "1")
                manifest = {"_error": type(error).__name__}
            connection.execute("UPDATE refs SET manifest=? WHERE key=?", (json.dumps(manifest), key))
            connection.commit()
        result.append((key, manifest))
    return result


def _fair_group(connection, policy, epoch, progress):
    providers = policy["include_agents"]
    if progress.get("fair_epoch") != str(epoch):
        # A legacy lexical cursor proves every preceding group was attempted,
        # including groups from other providers. Seed each provider from it.
        for provider in providers:
            for name, value in ((f"provider_after:{provider}", progress.get("after", "")),
                                (f"provider_done:{provider}", "0")):
                _set(connection, name, value)
                progress[name] = value
        _set(connection, "fair_epoch", epoch)
        _set(connection, "provider_next", "0")
        progress["fair_epoch"] = str(epoch)
        progress["provider_next"] = "0"
        connection.commit()
    first = int(progress.get("provider_next", "0")) % len(providers)
    for offset in range(len(providers)):
        index = (first + offset) % len(providers)
        provider = providers[index]
        if progress.get(f"provider_done:{provider}") == "1":
            continue
        after = progress.get(f"provider_after:{provider}", "")
        row = connection.execute(
            "SELECT group_id FROM refs WHERE epoch=? AND provider=? AND group_id>? ORDER BY group_id LIMIT 1",
            (epoch, provider, after)).fetchone()
        if not row:
            _set(connection, f"provider_done:{provider}", "1")
            progress[f"provider_done:{provider}"] = "1"
            continue
        # Persist rotation before attempting IO so a remaining-run or cache
        # limit cannot make one provider monopolize every future invocation.
        next_index = str((index + 1) % len(providers))
        _set(connection, "provider_next", next_index)
        progress["provider_next"] = next_index
        connection.commit()
        return row, provider
    connection.commit()
    return None, None


def _process(connection, client, policy, budget, report):
    progress = dict(connection.execute("SELECT name,value FROM progress"))
    epoch = int(progress.get("epoch", "1"))
    after = progress.get("after", "")
    while report["sessions"] < policy["max_sessions"]:
        budget.check()
        provider = None
        if policy.get("_scan_group"):
            group = connection.execute(
                "SELECT group_id FROM refs WHERE epoch=? AND group_id>? AND group_id=? ORDER BY group_id LIMIT 1",
                (epoch, after, policy["_scan_group"])).fetchone()
        else:
            group, provider = _fair_group(connection, policy, epoch, progress)
        if not group:
            _set(connection, "phase", "reconcile")
            connection.commit()
            return
        rows = [(key, json.loads(m) if m else None) for key, m in connection.execute(
            "SELECT key,manifest FROM refs WHERE epoch=? AND group_id=? ORDER BY key LIMIT ?",
            (epoch, group[0], policy["max_refs"] + 1))]
        try:
            if len(rows) > policy["max_refs"]:
                raise UnknownCompleteness()
            rows = _materialize(connection, client, policy, rows, group[0], budget, report)
            _emit(connection, client, policy, rows, group[0], budget, report)
        except BudgetExceeded:
            raise
        except OversizedArtifact:
            _count_unknown(policy, report, group[0])
            report["oversized"] += 1
            _withdraw(connection, policy, group[0], report)
        except UnsupportedArtifact:
            if group[0] not in policy.get("_excluded_counted", set()):
                report["excluded"] += 1
            _withdraw(connection, policy, group[0], report)
        except UnknownCompleteness:
            _count_unknown(policy, report, group[0])
            _withdraw(connection, policy, group[0], report)
        except RemoteReadFailure:
            # A failed manifest GET was counted during inventory. Its unknown
            # group must preserve an existing feed without counting it twice.
            if (not any(m and m.get("_error") == "RemoteReadFailure" for _, m in rows)
                    or not report["errors"]["RemoteReadFailure"]):
                report["errors"]["RemoteReadFailure"] += 1
            report["unknown"] += 1
        except FeedError as error:
            report["errors"][type(error).__name__] += 1
        report["sessions"] += 1
        if provider is not None:
            _set(connection, f"provider_after:{provider}", group[0])
            progress[f"provider_after:{provider}"] = group[0]
        else:
            after = group[0]
            _set(connection, "after", after)
        connection.commit()


def _prune_missing(connection, policy, budget, report, epoch):
    scope = policy.get("_scan_group", "")
    while report["sessions"] < policy["max_sessions"]:
        budget.check()
        missing = connection.execute(
            "SELECT o.group_id FROM current_groups o WHERE (?='' OR o.group_id=?) "
            "AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.group_id=o.group_id AND r.epoch=?) "
            "ORDER BY o.group_id LIMIT 1", (scope, scope, epoch)).fetchone()
        if not missing:
            return True
        _withdraw(connection, policy, missing[0], report)
        report["sessions"] += 1
        report["reconciled"] += 1
    return False


def _reconcile(connection, policy, budget, report):
    progress = dict(connection.execute("SELECT name,value FROM progress"))
    epoch = int(progress.get("epoch", "1"))
    if progress.get("scan_failed", "0") == "1":
        report["inventory_failed"] = True
        if not report["errors"]:
            report["errors"]["IncompleteInventory"] += 1
    else:
        if not _prune_missing(connection, policy, budget, report, epoch):
            return
        report["coverage_complete"] = True
    _set(connection, "phase", "scan")
    _set(connection, "epoch", epoch + 1)
    _set(connection, "after", "")
    _set(connection, "scan_failed", "0")
    connection.commit()


def run(policy, client=None):
    """Run one bounded cycle; return counters without source identities.

    ``client`` is an injected S3-compatible LIST/GET client. The optional CLI
    constructs it. Setup/auth/IO failures yield unknown coverage
    and typed counters, and never cause an existing feed to be removed.
    """
    report = {"schema_version": "twill-archive-feed-report/v1", "enabled": False,
              "pages": 0, "refs": 0, "manifest_gets": 0, "sessions": 0, "bytes": 0, "emitted": 0,
              "unchanged": 0, "excluded": 0, "unknown": 0,
              "withdrawn": 0, "consumed_unchanged": 0,
              "reaped_current": 0, "reaped_objects": 0, "reclaimed_bytes": 0,
              "retention_unresolved": False, "retention_bounded": False, "retention_busy": False, "cache_bytes": 0,
              "cache_limit": False,
              "oversized": 0,
              "reconciled": 0, "inventory_failed": False,
              "inventory_refs": 0, "inventory_artifacts": 0, "current_artifacts": 0,
              "scope": "all_refs", "scopes_total": 1, "scopes_completed": 0,
              "coverage_complete": False, "budget_exhausted": False, "errors": Counter()}
    connection = None
    lock = None
    seed_index = None
    status_root = None
    try:
        policy = _policy(policy)
        report["enabled"] = policy["enabled"]
        if not policy["enabled"]:
            return report
        budget = _Budget(policy, report)
        _private_directory(policy["feed_root"])
        _private_directory(policy["state_root"])
        status_root = policy["state_root"]
        lock = os.open(policy["state_root"] / "cycle.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FeedError() from None
        if client is None:
            raise FeedError()
        connection = _database(policy["state_root"] / "progress.sqlite3")
        # Tokens and inventory must never be resumed against a different source.
        identity = hashlib.sha256(json.dumps({key: str(policy[key]) for key in (
            "endpoint_url", "bucket", "refs_prefix", "blobs_prefix", "include_agents", "feed_root", "seed_groups")},
            sort_keys=True).encode()).hexdigest()
        old = connection.execute("SELECT value FROM progress WHERE name='identity'").fetchone()
        if old and old[0] != identity:
            raise InvalidPolicy()
        _set(connection, "identity", identity)
        connection.commit()
        _cache_inventory(connection, policy, budget)
        _current_inventory(connection, policy, budget)
        if policy["seed_groups"]:
            report["scope"] = "seeded_artifacts"
            report["scopes_total"] = len(policy["seed_groups"])
            position = connection.execute("SELECT value FROM progress WHERE name='seed_next'").fetchone()
            seed_index = int(position[0]) % len(policy["seed_groups"]) if position else 0
            seed_state = connection.execute("SELECT value FROM progress WHERE name=?", (f"seed_state:{seed_index}",)).fetchone()
            state = json.loads(seed_state[0]) if seed_state else {}
            for name, default in (("epoch", "1"), ("token", ""), ("phase", "scan"), ("after", ""),
                                  ("scan_failed", "0"), ("pruned_epoch", "")):
                _set(connection, name, state.get(name, default))
            group = policy["seed_groups"][seed_index]
            policy["_scan_group"] = group
            policy["_scan_prefix"] = policy["refs_prefix"] + group + "/"
            connection.commit()
        phase = connection.execute("SELECT value FROM progress WHERE name='phase'").fetchone()
        if not phase or phase[0] == "scan":
            if not _scan(connection, client, policy, budget, report):
                report["budget_exhausted"] = True
                return report
        progress = dict(connection.execute("SELECT name,value FROM progress"))
        if progress.get("scan_failed", "0") == "1":
            # A poisoned or failed inventory cannot establish a complete group
            # or a deletion. Finish it without either exposure or withdrawal.
            _reconcile(connection, policy, budget, report)
            return report
        if progress.get("phase") in ("prune", "process") and progress.get("pruned_epoch") != progress.get("epoch", "1"):
            _set(connection, "phase", "prune")
            connection.commit()
            epoch = int(progress.get("epoch", "1"))
            if not _prune_missing(connection, policy, budget, report, epoch):
                report["budget_exhausted"] = True
                return report
            _set(connection, "pruned_epoch", epoch)
            _set(connection, "phase", "process")
            connection.commit()
        phase = connection.execute("SELECT value FROM progress WHERE name='phase'").fetchone()
        if phase and phase[0] == "process":
            _process(connection, client, policy, budget, report)
        phase = connection.execute("SELECT value FROM progress WHERE name='phase'").fetchone()
        if phase and phase[0] == "reconcile":
            _reconcile(connection, policy, budget, report)
        if report["coverage_complete"]:
            report["scopes_completed"] = 1
        if not report["coverage_complete"] and not report["inventory_failed"]:
            report["budget_exhausted"] = True
    except BudgetExceeded:
        report["budget_exhausted"] = True
    except FeedError as error:
        report["errors"][type(error).__name__] += 1
    except Exception:
        report["errors"]["SetupFailure"] += 1
    finally:
        if connection is not None:
            # An interrupted LIST page has no durable continuation token;
            # replay the whole page rather than publish a partial transaction.
            connection.rollback()
            if seed_index is not None:
                state = dict(connection.execute("SELECT name,value FROM progress WHERE name IN ('epoch','token','phase','after','scan_failed','pruned_epoch')"))
                _set(connection, f"seed_state:{seed_index}", json.dumps(state))
                _set(connection, "seed_next", (seed_index + 1) % len(policy["seed_groups"]))
                connection.commit()
            try:
                report["inventory_refs"] = connection.execute("SELECT count(*) FROM refs").fetchone()[0]
                report["inventory_artifacts"] = connection.execute("SELECT count(DISTINCT group_id) FROM refs").fetchone()[0]
                report["current_artifacts"] = connection.execute("SELECT count(*) FROM current_groups").fetchone()[0]
                report["cache_bytes"] = _cache_bytes(connection)
            except sqlite3.Error:
                pass
            connection.close()
        if lock is not None:
            os.close(lock)
        report["at"] = datetime.now(timezone.utc).isoformat()
        report["status"] = ("attention" if report["errors"] else
                            "disabled" if not report["enabled"] else
                            "in_progress" if report["budget_exhausted"] else
                            "partial" if report["unknown"] or report["scope"] == "seeded_artifacts" else "healthy")
        report["coverage"] = ("unknown" if report["errors"] or report["unknown"] else
                              "in_progress" if report["budget_exhausted"] else
                              "seeded_only" if report["scope"] == "seeded_artifacts" else
                              "complete" if report["coverage_complete"] else "unknown")
        if not report["errors"] and report["enabled"]:
            report["last_success"] = report["at"]
        if status_root is not None:
            try:
                _atomic(status_root / "status.json", (json.dumps(report, sort_keys=True) + "\n").encode())
            except Exception:
                report["errors"]["StatusWriteFailure"] += 1
                report["status"] = "attention"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--reap", action="store_true", help="reclaim verified consumed local cache without network access")
    args = parser.parse_args(argv)
    try:
        policy = _private_read(args.policy)
    except Exception:
        policy = None
    report = reap(policy) if args.reap else run(policy)
    print(json.dumps(report, sort_keys=True))
    return 1 if report.get("errors") or report.get("retention_unresolved") else 0


if __name__ == "__main__":
    raise SystemExit(main())
