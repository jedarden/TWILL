"""Synthetic native bytes exercise the optional read-only producer boundary."""
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import twill_archive_feed as feed


def jsonl(*rows):
    return b"".join((json.dumps(row, separators=(",", ":")) + "\n").encode() for row in rows)


CLAUDE = jsonl({"type": "user", "uuid": "synthetic-user", "sessionId": "sample-session",
                "timestamp": "2020-01-01T00:00:00Z",
                "message": {"role": "user", "content": "Synthetic example."}},
               {"type": "assistant", "uuid": "synthetic-assistant", "sessionId": "sample-session",
                "timestamp": "2020-01-01T00:00:01Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Example reply."}]}})
CODEX = jsonl({"type": "session_meta", "timestamp": "2020-01-01T00:00:00Z",
               "payload": {"id": "sample-session", "cwd": "/synthetic/project"}},
              {"type": "response_item", "timestamp": "2020-01-01T00:00:01Z",
               "payload": {"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": "Synthetic example."}]}})


class S3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail = set()
        self.page_size = 1000
        self.before_get = None

    def list_objects_v2(self, **kwargs):
        self.calls.append(("LIST", dict(kwargs)))
        keys = sorted(k for k in self.objects if k.startswith(kwargs["Prefix"]))
        start = int(kwargs.get("ContinuationToken", "0"))
        end = min(len(keys), start + min(kwargs["MaxKeys"], self.page_size))
        result = {"Contents": [{"Key": key} for key in keys[start:end]], "IsTruncated": end < len(keys)}
        if result["IsTruncated"]:
            result["NextContinuationToken"] = str(end)
        return result

    def get_object(self, **kwargs):
        self.calls.append(("GET", dict(kwargs)))
        if self.before_get:
            self.before_get()
        if kwargs["Key"] in self.fail:
            raise OSError("Do not propagate remote text or identity")
        raw = self.objects[kwargs["Key"]]
        return {"Body": io.BytesIO(raw), "ContentLength": len(raw)}


class ArchiveFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="twill-archive-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.policy = {"schema_version": "twill-archive-feed/v1", "enabled": True,
                       "endpoint_url": "https://object-storage.invalid", "bucket": "synthetic-bucket",
                       "refs_prefix": "outer/v1/raw/refs/", "blobs_prefix": "outer/v1/raw/blobs/sha256/",
                       "credentials_file": str(root / "credentials.json"),
                       "feed_root": str(root / "feed"), "state_root": str(root / "state"),
                       "include_agents": ["claude-code", "codex"], "max_refs": 100,
                       "max_pages": 10, "max_sessions": 10, "max_bytes": 2_000_000,
                       "max_seconds": 30, "settle_seconds": 60}
        self.client = S3()

    def add_ref(self, raw=CLAUDE, agent="claude-code", start=0, generation=0,
                source_key="synthetic/file.jsonl", session="sample-session", snapshot=False,
                captured_at="2020-01-01T00:00:00Z", inode=7):
        digest = hashlib.sha256(raw).hexdigest()
        artifact = hashlib.sha256(source_key.encode()).hexdigest()[:24]
        end = start + len(raw)
        name = f"g{generation}-{start}-{end}-{digest}.json"
        relative_ref = f"v1/raw/refs/synthetic-host/{agent}/{session}/{artifact}/{name}"
        key = "outer/" + relative_ref
        relative_blob = f"v1/raw/blobs/sha256/{digest[:2]}/{digest}.gz"
        stored = gzip.compress(raw, mtime=0)
        self.client.objects["outer/" + relative_blob] = stored
        manifest = {"schema_version": 1, "source": agent, "host": "synthetic-host",
                    "session_id": session, "source_key": source_key,
                    "role": "legacy_session_snapshot" if snapshot else "session",
                    "captured_at": captured_at, "raw_tier": True, "sanitized": False,
                    "reference_object_key": relative_ref,
                    "range": {"start": start, "end": end},
                    "cursor": {"kind": "snapshot", "source_revision": digest} if snapshot else
                              {"kind": "file", "device": 3, "inode": inode,
                               "generation": generation, "ack_offset": end},
                    "blob": {"raw_sha256": digest, "stored_sha256": hashlib.sha256(stored).hexdigest(),
                             "raw_bytes": len(raw), "stored_bytes": len(stored),
                             "object_key": relative_blob, "content_encoding": "gzip"}}
        self.client.objects[key] = json.dumps(manifest).encode()
        return key, manifest

    def run_cycle(self, **changes):
        self.policy.update(changes)
        return feed.run(self.policy, client=self.client)

    def current(self):
        return sorted(Path(self.policy["feed_root"]).glob("current/**/*.jsonl"))

    def test_original_native_bytes_private_provenance_and_idempotence(self):
        claude, _ = self.add_ref()
        codex, _ = self.add_ref(CODEX, "codex", snapshot=True)
        result = self.run_cycle()
        self.assertEqual(result["errors"], {})
        self.assertEqual(result["emitted"], 2)
        self.assertEqual(result["inventory_refs"], 2)
        self.assertEqual(result["inventory_artifacts"], 2)
        self.assertEqual(result["current_artifacts"], 2)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual({p.read_bytes() for p in self.current()}, {CLAUDE, CODEX})
        identities = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in self.current()}
        for path in self.current():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_mtime, datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
            metadata = json.loads(path.with_suffix(".manifest.json").read_bytes())
            self.assertTrue(metadata["complete_observed_generation"])
            self.assertEqual(metadata["session_ended"], "unknown")
            self.assertEqual(metadata["raw_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertTrue(set(metadata["reference_keys"]) <= {claude, codex})
        second = self.run_cycle()
        self.assertEqual(second["unchanged"], 2)
        self.assertEqual(identities, {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in self.current()})
        self.assertTrue(all(operation in ("GET", "LIST") for operation, _ in self.client.calls))
        status = json.loads((Path(self.policy["state_root"]) / "status.json").read_bytes())
        self.assertEqual(status["status"], "healthy")
        self.assertNotIn("synthetic-host", json.dumps(status))
        self.assertNotIn(claude, json.dumps(result))

    def test_pagination_resumes_without_exposing_a_partial_inventory(self):
        self.add_ref()
        self.add_ref(CODEX, "codex")
        self.client.page_size = 1
        first = self.run_cycle(max_pages=1)
        self.assertTrue(first["budget_exhausted"])
        self.assertEqual(self.current(), [])
        second = self.run_cycle(max_pages=1)
        self.assertEqual(second["emitted"], 2)
        listing = [args for operation, args in self.client.calls if operation == "LIST"]
        self.assertNotIn("ContinuationToken", listing[0])
        self.assertEqual(listing[1]["ContinuationToken"], "1")

    def test_contiguous_chunks_and_atomic_stable_path_update(self):
        split = CLAUDE.index(b"\n") + 1
        self.add_ref(CLAUDE[:split])
        self.add_ref(CLAUDE[split:], start=split)
        result = self.run_cycle()
        self.assertEqual(result["emitted"], 1)
        path = self.current()[0]
        self.assertEqual(path.read_bytes(), CLAUDE)
        old_inode = path.stat().st_ino
        addition = jsonl({"type": "assistant", "message": {"role": "assistant", "content": "Another example."}})
        self.add_ref(addition, start=len(CLAUDE), captured_at="2020-01-01T00:01:00Z")
        result = self.run_cycle()
        self.assertEqual(result["emitted"], 1)
        self.assertEqual(self.current(), [path])
        self.assertEqual(path.read_bytes(), CLAUDE + addition)
        self.assertNotEqual(path.stat().st_ino, old_inode)
        self.assertEqual(len(list(Path(self.policy["feed_root"]).glob("objects/**/*.jsonl"))), 2)

    def test_later_chunk_gap_overlap_or_new_generation_is_never_a_session(self):
        for kind in ("later", "gap", "overlap", "new_generation"):
            with self.subTest(kind=kind):
                self.client = S3()
                self.policy["state_root"] = str(Path(self.temp.name) / ("state-" + kind))
                self.policy["feed_root"] = str(Path(self.temp.name) / ("feed-" + kind))
                if kind != "later":
                    self.add_ref()
                start = len(CLAUDE) + 1 if kind in ("gap", "later") else 3
                self.add_ref(CLAUDE, start=start, generation=1 if kind == "new_generation" else 0)
                result = self.run_cycle()
                self.assertEqual(result["unknown"], 1)
                self.assertEqual(self.current(), [])

    def test_new_incomplete_or_fresh_generation_withdraws_current(self):
        self.add_ref()
        self.assertEqual(self.run_cycle()["emitted"], 1)
        self.add_ref(CLAUDE, start=3, generation=1)
        result = self.run_cycle()
        self.assertEqual(result["withdrawn"], 1)
        self.assertEqual(self.current(), [])
        self.add_ref(CLAUDE, generation=2, captured_at=datetime.now(timezone.utc).isoformat())
        result = self.run_cycle()
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(self.current(), [])

    def test_cross_generation_never_joins_old_prefix(self):
        first = CLAUDE.index(b"\n") + 1
        self.add_ref(CLAUDE[:first], generation=0)
        self.add_ref(CLAUDE[first:], start=first, generation=1)
        self.assertEqual(self.run_cycle()["unknown"], 1)
        self.assertEqual(self.current(), [])

    def test_explicit_nested_partial_is_unknown(self):
        key, manifest = self.add_ref()
        manifest["cursor"]["is_partial"] = True
        self.client.objects[key] = json.dumps(manifest).encode()
        result = self.run_cycle()
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(self.current(), [])

    def test_integrity_failures_do_not_publish_and_isolate_other_artifacts(self):
        for kind in ("raw_hash", "stored_hash", "gzip", "raw_size", "stored_size", "escaped_blob", "host"):
            with self.subTest(kind=kind):
                self.client = S3()
                self.policy["state_root"] = str(Path(self.temp.name) / ("state-" + kind))
                self.policy["feed_root"] = str(Path(self.temp.name) / ("feed-" + kind))
                key, m = self.add_ref()
                self.add_ref(CODEX, "codex", source_key="synthetic/other.jsonl")
                blob_key = "outer/" + m["blob"]["object_key"]
                if kind == "raw_hash":
                    self.client.objects[blob_key] = gzip.compress(b"X" * len(CLAUDE), mtime=0)
                    m["blob"]["stored_bytes"] = len(self.client.objects[blob_key])
                    m["blob"]["stored_sha256"] = hashlib.sha256(self.client.objects[blob_key]).hexdigest()
                elif kind == "stored_hash":
                    m["blob"]["stored_sha256"] = "0" * 64
                elif kind == "gzip":
                    self.client.objects[blob_key] = b"not a gzip stream"
                    m["blob"]["stored_bytes"] = len(self.client.objects[blob_key])
                    m["blob"]["stored_sha256"] = hashlib.sha256(self.client.objects[blob_key]).hexdigest()
                elif kind == "raw_size":
                    m["blob"]["raw_bytes"] += 1
                elif kind == "stored_size":
                    m["blob"]["stored_bytes"] += 1
                elif kind == "escaped_blob":
                    m["blob"]["object_key"] = "../outside.gz"
                else:
                    m["host"] = "different-host"
                self.client.objects[key] = json.dumps(m).encode()
                result = self.run_cycle()
                self.assertEqual(result["emitted"], 1)
                self.assertTrue(result["errors"])
                self.assertEqual([p.read_bytes() for p in self.current()], [CODEX])

    def test_remote_failure_never_erases_prior_feed_or_leaks_error(self):
        key, m = self.add_ref()
        self.run_cycle()
        path = self.current()[0]
        key, m = self.add_ref(generation=1)
        for failed_key in (key, "outer/" + m["blob"]["object_key"]):
            self.client.fail = {failed_key}
            result = self.run_cycle()
            self.assertEqual(result["errors"].get("RemoteReadFailure"), 1)
            self.assertEqual(path.read_bytes(), CLAUDE)
            self.assertNotIn("Do not propagate", json.dumps(result))
            self.assertEqual(result["status"], "attention")

    def test_global_completed_inventory_withdraws_remote_removal_and_keeps_history(self):
        key, _ = self.add_ref()
        self.assertEqual(self.run_cycle()["emitted"], 1)
        path = self.current()[0]
        del self.client.objects[key]
        result = self.run_cycle()
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["withdrawn"], 1)
        self.assertEqual(result["reconciled"], 1)
        self.assertEqual(result["current_artifacts"], 0)
        self.assertFalse(path.exists())
        self.assertEqual(len(list(Path(self.policy["feed_root"]).glob("objects/**/*.jsonl"))), 1)

    def test_seeded_completed_inventory_withdraws_only_its_removed_artifact(self):
        key, _ = self.add_ref()
        codex, _ = self.add_ref(CODEX, "codex")
        self.run_cycle(seed_refs=[key, codex])
        self.run_cycle()
        self.assertEqual(len(self.current()), 2)
        del self.client.objects[key]
        result = self.run_cycle()
        self.assertEqual(result["withdrawn"], 1)
        self.assertEqual([p.read_bytes() for p in self.current()], [CODEX])

    def test_removal_reconciliation_is_bounded_and_resumable(self):
        key, _ = self.add_ref()
        codex, _ = self.add_ref(CODEX, "codex")
        self.run_cycle()
        del self.client.objects[key]
        del self.client.objects[codex]
        first = self.run_cycle(max_sessions=1)
        self.assertEqual(first["withdrawn"], 1)
        self.assertEqual(first["sessions"], 1)
        self.assertTrue(first["budget_exhausted"])
        self.assertEqual(len(self.current()), 1)
        second = self.run_cycle()
        self.assertEqual(second["withdrawn"], 1)
        self.assertEqual(second["sessions"], 1)
        self.assertEqual(self.current(), [])
        self.assertTrue(self.run_cycle()["coverage_complete"])

    def test_existing_private_sidecar_recovers_owned_current_for_removal(self):
        key, _ = self.add_ref()
        self.run_cycle()
        database = Path(self.policy["state_root"]) / "progress.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM current_groups")
            connection.execute("DELETE FROM progress WHERE name='current_indexed'")
        del self.client.objects[key]
        result = self.run_cycle()
        self.assertEqual(result["withdrawn"], 1)
        self.assertEqual(self.current(), [])

    def test_partial_and_failed_global_inventory_preserve_missing_current(self):
        key, _ = self.add_ref()
        self.run_cycle()
        path = self.current()[0]
        del self.client.objects[key]
        self.add_ref(CODEX, "codex", source_key="synthetic/first.jsonl")
        self.add_ref(CODEX, "codex", source_key="synthetic/second.jsonl")
        self.client.page_size = 1
        partial = self.run_cycle(max_pages=1)
        self.assertFalse(partial["coverage_complete"])
        self.assertEqual(partial["withdrawn"], 0)
        self.assertTrue(path.exists())
        with patch.object(self.client, "list_objects_v2", side_effect=OSError("not public")):
            failed = self.run_cycle()
        self.assertEqual(failed["errors"].get("RemoteReadFailure"), 1)
        self.assertTrue(path.exists())
        completed = self.run_cycle(max_pages=10)
        self.assertEqual(completed["withdrawn"], 1)
        self.assertFalse(path.exists())

    def test_seed_listing_rejects_out_of_scope_key_before_any_get(self):
        key, _ = self.add_ref()
        outside, _ = self.add_ref(CODEX, "codex")
        self.run_cycle(seed_refs=[key])
        path = self.current()[0]
        self.client.calls.clear()
        response = {"Contents": [{"Key": outside}], "IsTruncated": False}
        with patch.object(self.client, "list_objects_v2", return_value=response):
            result = self.run_cycle()
        self.assertEqual(result["errors"].get("InvalidListingPrefix"), 1)
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["withdrawn"], 0)
        self.assertTrue(path.exists())
        self.assertFalse(any(op == "GET" for op, _ in self.client.calls))

    def test_completed_inventory_prunes_removed_current_before_cache_hold(self):
        removed, _ = self.add_ref()
        self.assertEqual(self.run_cycle()["emitted"], 1)
        old_current = self.current()[0]
        del self.client.objects[removed]
        self.add_ref(CODEX, "codex")
        result = self.run_cycle(max_cache_bytes=len(CLAUDE))
        self.assertTrue(result["cache_limit"])
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(result["withdrawn"], 1)
        self.assertEqual(result["reconciled"], 1)
        self.assertEqual(result["emitted"], 0)
        self.assertFalse(old_current.exists())
        self.assertEqual(self.current(), [])

    def test_poisoned_inventory_preserves_existing_and_emits_no_new_current(self):
        self.add_ref()
        self.run_cycle()
        previous = self.current()[0]
        valid, _ = self.add_ref(CODEX, "codex")
        outside = "outside/valid-looking.json"
        response = {"Contents": [{"Key": valid}, {"Key": outside}], "IsTruncated": False}
        self.client.calls.clear()
        with patch.object(self.client, "list_objects_v2", return_value=response):
            result = self.run_cycle()
        self.assertTrue(result["inventory_failed"])
        self.assertEqual(result["errors"].get("InvalidListingPrefix"), 1)
        self.assertEqual(result["emitted"], 0)
        self.assertEqual(result["withdrawn"], 0)
        self.assertEqual(self.current(), [previous])
        self.assertFalse(any(op == "GET" for op, _ in self.client.calls))
        self.assertEqual(self.run_cycle()["emitted"], 1)

    def test_mixed_corpus_excludes_roles_and_preserves_partial_as_unknown(self):
        self.add_ref()
        sidecar, m = self.add_ref(CLAUDE, source_key="synthetic/sidecar.jsonl")
        m["role"] = "metadata_sidecar"
        self.client.objects[sidecar] = json.dumps(m).encode()
        partial, m = self.add_ref(CODEX, "codex")
        m["cursor"]["is_partial"] = True
        self.client.objects[partial] = json.dumps(m).encode()
        result = self.run_cycle()
        self.assertEqual(result["emitted"], 1)
        self.assertEqual(result["errors"], {})
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["excluded"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([p.read_bytes() for p in self.current()], [CLAUDE])

    def test_inventory_lists_only_and_materializes_selected_groups_after_completion(self):
        for number in range(12):
            self.add_ref(CODEX, "codex", source_key=f"synthetic/file-{number}.jsonl")
        self.client.page_size = 4
        first = self.run_cycle(max_pages=1, max_sessions=1)
        self.assertTrue(first["budget_exhausted"])
        self.assertEqual(first["refs"], 4)
        self.assertEqual(first["inventory_refs"], 4)
        self.assertEqual(first["inventory_artifacts"], 4)
        self.assertEqual(first["manifest_gets"], 0)
        self.assertFalse(any(op == "GET" for op, _ in self.client.calls))
        self.run_cycle()
        third = self.run_cycle()
        self.assertEqual(third["emitted"], 1)
        self.assertEqual(third["manifest_gets"], 1)
        self.assertEqual(third["sessions"], 1)
        self.assertEqual(sum(op == "GET" for op, _ in self.client.calls), 2)

    def test_global_processing_is_durably_fair_between_providers(self):
        for number in range(5):
            self.add_ref(source_key=f"synthetic/claude-{number}.jsonl",
                         agent="claude" if number % 2 else "claude-code")
        self.add_ref(CODEX, "codex")
        first = self.run_cycle(max_sessions=2)
        self.assertEqual(first["emitted"], 2)
        self.assertEqual({path.parent.name for path in self.current()}, {"claude-code", "codex"})
        self.assertTrue(first["budget_exhausted"])
        for _ in range(2):
            result = self.run_cycle()
            self.assertEqual(result["emitted"], 2)
            self.assertEqual(result["unchanged"], 0)
        self.assertEqual(len(self.current()), 6)
        final = self.run_cycle()
        self.assertTrue(final["coverage_complete"])
        self.assertEqual(final["emitted"], 0)
        refs_read = [args["Key"] for op, args in self.client.calls
                     if op == "GET" and args["Key"].startswith(self.policy["refs_prefix"])]
        self.assertEqual(len(refs_read), 6)
        self.assertEqual(len(set(refs_read)), 6)

    def test_existing_global_cursor_and_inventory_migrate_without_replaying_groups(self):
        self.add_ref(source_key="synthetic/first-claude.jsonl")
        self.add_ref(source_key="synthetic/second-claude.jsonl")
        self.add_ref(CODEX, "codex")
        self.assertEqual(self.run_cycle(max_sessions=1)["emitted"], 1)
        database = Path(self.policy["state_root"]) / "progress.sqlite3"
        with sqlite3.connect(database) as connection:
            after = connection.execute("SELECT value FROM progress WHERE name='provider_after:claude-code'").fetchone()[0]
            connection.execute("UPDATE progress SET value=? WHERE name='after'", (after,))
            connection.execute("DELETE FROM progress WHERE name LIKE 'provider_%' OR name='fair_epoch'")
            connection.execute("DROP INDEX refs_provider")
            connection.execute("ALTER TABLE refs DROP COLUMN provider")
        result = self.run_cycle(max_sessions=2)
        self.assertEqual(result["emitted"], 2)
        self.assertEqual(result["unchanged"], 0)
        self.assertEqual(len(self.current()), 3)
        self.assertEqual({path.parent.name for path in self.current()}, {"claude-code", "codex"})

    def test_byte_cap_and_time_cap_resume_without_partial_feed(self):
        key, m = self.add_ref()
        first = self.run_cycle(max_bytes=len(self.client.objects[key]) + 1)
        self.assertTrue(first["budget_exhausted"])
        self.assertLessEqual(first["bytes"], self.policy["max_bytes"])
        self.assertEqual(self.current(), [])
        self.assertEqual(self.run_cycle(max_bytes=2_000_000)["emitted"], 1)
        self.client.before_get = lambda: clock.__setitem__(0, 100)
        clock = [0]
        with patch.object(feed.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.run_cycle(max_seconds=1)
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(len(self.current()), 1)

    def test_gzip_inflate_is_capped_before_declared_size_can_be_exceeded(self):
        key, m = self.add_ref()
        bomb = gzip.compress(b"X" * 200_000, mtime=0)
        self.client.objects["outer/" + m["blob"]["object_key"]] = bomb
        m["blob"]["stored_bytes"] = len(bomb)
        m["blob"]["stored_sha256"] = hashlib.sha256(bomb).hexdigest()
        self.client.objects[key] = json.dumps(m).encode()
        result = self.run_cycle(max_bytes=10_000)
        self.assertEqual(result["errors"].get("IntegrityFailure"), 1)
        self.assertLess(result["bytes"], 10_000)
        self.assertEqual(self.current(), [])

    def test_seeded_artifact_prefixes_rotate_and_never_bypass_other_chunks(self):
        key, _ = self.add_ref()
        self.add_ref(CLAUDE, start=len(CLAUDE) + 1)
        codex, _ = self.add_ref(CODEX, "codex")
        first = self.run_cycle(seed_refs=[key, codex], max_pages=1)
        self.assertEqual(first["scope"], "seeded_artifacts")
        self.assertEqual(first["unknown"], 1)
        self.assertEqual(self.current(), [])
        second = self.run_cycle()
        self.assertEqual(second["emitted"], 1)
        self.assertEqual(self.current()[0].read_bytes(), CODEX)
        prefixes = [args["Prefix"] for operation, args in self.client.calls if operation == "LIST"]
        self.assertEqual(len(set(prefixes)), 2)
        self.assertTrue(all(prefix.endswith("/") and len(prefix.split("/")) > 7 for prefix in prefixes))

    def test_provider_alias_and_bounded_hardlinked_cache(self):
        self.add_ref(agent="claude")
        result = self.run_cycle()
        self.assertEqual(result["emitted"], 1)
        path = self.current()[0]
        self.assertEqual(path.parent.name, "claude-code")
        cached = next(Path(self.policy["feed_root"]).glob("objects/**/*.jsonl"))
        self.assertEqual(path.stat().st_ino, cached.stat().st_ino)
        used = cached.stat().st_size + path.with_suffix(".manifest.json").stat().st_size
        self.add_ref(CODEX, "codex")
        result = self.run_cycle(max_cache_bytes=used)
        self.assertTrue(result["budget_exhausted"])
        self.assertTrue(result["cache_limit"])
        self.assertEqual(len(self.current()), 1)
        self.assertEqual(path.read_bytes(), CLAUDE)
        self.assertEqual(sum(p.stat().st_size for p in Path(self.policy["feed_root"]).glob("objects/**/*.jsonl")), len(CLAUDE))
        result = self.run_cycle(max_cache_bytes=used + len(CODEX) + 4000)
        self.assertEqual(result["emitted"], 1)

    def test_current_metadata_counts_toward_cache_cap(self):
        self.add_ref()
        result = self.run_cycle(max_cache_bytes=len(CLAUDE))
        self.assertTrue(result["cache_limit"])
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(self.current(), [])

    def test_tied_snapshot_revisions_are_unknown(self):
        self.add_ref(CODEX, "codex", snapshot=True)
        other = CODEX + jsonl({"type": "event_msg", "payload": {"type": "task_complete"}})
        self.add_ref(other, "codex", snapshot=True)
        result = self.run_cycle()
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(self.current(), [])

    def test_seed_rotation_is_fair_while_a_different_prefix_is_paginating(self):
        key, _ = self.add_ref()
        self.add_ref(CLAUDE, start=len(CLAUDE))
        codex, _ = self.add_ref(CODEX, "codex")
        self.client.page_size = 1
        first = self.run_cycle(seed_refs=[key, codex], max_pages=1)
        self.assertTrue(first["budget_exhausted"])
        self.assertEqual(self.current(), [])
        self.assertEqual(self.run_cycle()["emitted"], 1)
        third = self.run_cycle()
        self.assertEqual(third["emitted"], 1)
        self.assertEqual(len(self.current()), 2)

    def test_oversized_new_generation_withdraws_and_advances_to_other_provider(self):
        self.add_ref()
        self.assertEqual(self.run_cycle()["emitted"], 1)
        previous = self.current()[0]
        oversized = jsonl({"type": "user", "message": {"role": "user", "content": "X" * 12000}})
        _, large_manifest = self.add_ref(oversized, generation=1)
        self.add_ref(CODEX, "codex")
        self.client.calls.clear()
        result = self.run_cycle(max_bytes=6000)
        self.assertEqual(result["oversized"], 1)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["withdrawn"], 1)
        self.assertEqual(result["emitted"], 1)
        self.assertEqual(result["sessions"], 2)
        self.assertFalse(result["budget_exhausted"])
        self.assertLessEqual(result["bytes"], 6000)
        self.assertFalse(previous.exists())
        self.assertEqual([p.read_bytes() for p in self.current()], [CODEX])
        large_key = "outer/" + large_manifest["blob"]["object_key"]
        self.assertFalse(any(op == "GET" and args["Key"] == large_key for op, args in self.client.calls))

    def test_disabled_setup_auth_bad_keys_and_path_boundaries_are_unknown(self):
        with patch.object(Path, "mkdir", side_effect=AssertionError("disabled must not write")):
            self.assertEqual(feed.run({"schema_version": "twill-archive-feed/v1", "enabled": False})["status"], "disabled")
        result = feed.run(self.policy)
        self.assertTrue(result["errors"])
        self.assertFalse(result["coverage_complete"])
        bad_key = self.policy["refs_prefix"] + "synthetic-host/claude-code/sample/../x.json"
        self.client.objects[bad_key] = b"{}"
        self.assertEqual(self.run_cycle()["errors"].get("InvalidReference"), 1)
        self.assertEqual(self.current(), [])
        for path in (str(Path(__file__).resolve().parents[1] / "feed"), str(Path(self.temp.name) / "feed/../feed")):
            result = self.run_cycle(feed_root=path)
            self.assertEqual(result["errors"].get("InvalidPolicy"), 1)


if __name__ == "__main__":
    unittest.main()
