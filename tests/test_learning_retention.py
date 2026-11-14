"""Synthetic producer-owned hardlinks and private consumption proofs."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_archive_feed import ArchiveFeedTests, CLAUDE, CODEX, jsonl
import twill_archive_feed as feed
import twill_learning_retention as retention
import twill_intervention_mining as mining


class ConsumedCacheTests(ArchiveFeedTests):
    # Archive tests remain in their original suite; inherit only fixture helpers.
    __unittest_skip__ = False

    def setUp(self):
        super().setUp()
        self.learning = Path(self.temp.name) / "private"
        self.policy["retention"] = {"enabled": True, "learning_root": str(self.learning),
                                    "max_files": 128, "max_bytes": 2_000_000, "max_seconds": 10}

    def private_json(self, path, value):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def consume(self, path, **changes):
        info = path.stat()
        fp = [info.st_size, info.st_mtime_ns, info.st_ino]
        sidecar = json.loads(path.with_suffix(".manifest.json").read_text())
        receipt = {"fingerprint": fp, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                   "complete": True, "compact_durable": True, "offset": info.st_size,
                   "line": 1, "episode_ids": [], "error": None,
                   "scanner_version": mining.SCANNER_VERSION,
                   "source_version": sidecar.get("source_version")}
        receipt.update(changes)
        receipts = retention.consumed_sources(self.learning)
        receipts[str(path)] = receipt
        self.private_json(self.learning / "learning/.intervention-consumption.json",
                          {"schema": "twill-intervention-consumption/v1", "sources": receipts})
        db = self.learning / "learning/.source-discovery.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS source (consumer,path,fingerprint,cursor,done,error)")
            connection.execute("DELETE FROM source WHERE path=?", (str(path),))
            connection.execute("INSERT INTO source VALUES (?,?,?,?,?,?)", ("intervention-episodes", str(path),
                json.dumps(fp), json.dumps({"fingerprint": fp, "offset": info.st_size, "done": True}), 1, 0))
        db.chmod(0o600)
        return receipt

    def emit(self):
        self.add_ref()
        self.assertEqual(self.run_cycle()["emitted"], 1)
        return self.current()[0]

    def test_consumed_zero_episode_removes_both_links_retains_retrieval_and_no_loop(self):
        path = self.emit()
        self.consume(path)
        used = self.run_cycle()["cache_bytes"]
        obj = next(Path(self.policy["feed_root"]).glob("objects/**/*.jsonl"))
        self.assertEqual(obj.stat().st_nlink, 2)
        result = feed.reap(self.policy)
        self.assertEqual(result["reaped_current"], 1, result)
        self.assertEqual(result["reaped_objects"], 1, result)
        self.assertEqual(result["reclaimed_bytes"], used)
        self.assertEqual(result["cache_bytes"], 0)
        self.assertFalse(path.exists())
        self.assertFalse(obj.exists())
        retained = list((Path(self.policy["state_root"]) / "consumed").glob("*.json"))
        self.assertEqual(len(retained), 1)
        proof = json.loads(retained[0].read_text())
        self.assertTrue(proof["references"])
        self.assertTrue(proof["namespace"])
        self.client.calls.clear()
        for _ in range(2):
            result = self.run_cycle()
            self.assertEqual(result["consumed_unchanged"], 1)
            self.assertEqual(result["emitted"], 0)
        self.assertFalse(any(op == "GET" for op, args in self.client.calls))
        self.assertEqual(feed.reap(self.policy)["reclaimed_bytes"], 0)

    def test_live_record_and_source_owners_pin(self):
        path = self.emit()
        for state in ("draft", "validated", "active", "proposed"):
            with self.subTest(state=state):
                self.consume(path)
                owner = self.learning / "learning/records/LC-synthetic.json"
                self.private_json(owner, {"state": state, "source_evidence": {"raw_sources": [{"path": str(path)}]}})
                self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)
                self.assertTrue(path.exists())
                owner.unlink()
        self.private_json(self.learning / "learning/sources/pending.json", {"evidence": {"path": str(path)}})
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)

    def test_pending_episode_pins_and_missing_compact_episode_fails_closed(self):
        path = self.emit()
        ident = "IE-" + "a" * 24
        self.consume(path, episode_ids=[ident])
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)
        self.private_json(self.learning / "learning/interventions" / (ident + ".json"),
                          {"schema": "twill-intervention-episode/v1", "id": ident, "state": "pending_interpretation",
                           "classification": {"signal": "candidate"}, "context": {},
                           "source_original": {"source_path": str(path)}, "source_copies": []})
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)
        self.assertTrue(path.exists())

    def test_bad_missing_partial_or_stale_proof_preserves_every_link(self):
        path = self.emit()
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)
        for changes in ({"complete": False}, {"compact_durable": False}, {"error": "parse_error"},
                        {"offset": 0}, {"sha256": "0" * 64}, {"source_version": "stale"}):
            self.consume(path, **changes)
            self.assertEqual(feed.reap(self.policy)["reaped_current"], 0, changes)
        self.consume(path)
        db = self.learning / "learning/.source-discovery.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("UPDATE source SET done=0")
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)
        self.assertTrue(path.exists())

    def test_old_or_missing_scanner_receipt_cannot_evict_before_rescan(self):
        path = self.emit()
        for version in (None, "previous-scanner"):
            receipt = self.consume(path, scanner_version=version)
            self.assertFalse(retention.verified_receipt(self.learning, path, receipt))
            self.assertFalse(retention.consumed_sources(self.learning)[str(path)]["complete"])
            self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)
            self.assertTrue(path.exists())
        receipt = self.consume(path)
        with patch.object(mining, "SCANNER_VERSION", "upgraded-scanner"):
            self.assertFalse(retention.verified_receipt(self.learning, path, receipt))
            self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 1)

    def test_same_fingerprint_hash_mismatch_cannot_delete(self):
        path = self.emit()
        self.consume(path)
        info = path.stat()
        raw = path.read_bytes()
        path.write_bytes(b"x" + raw[1:])
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)
        self.assertTrue(path.exists())

    def test_new_archive_version_reexports_stable_current(self):
        path = self.emit()
        self.consume(path)
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 1)
        addition = jsonl({"type": "assistant", "message": {"role": "assistant", "content": "Next."}})
        self.add_ref(addition, start=len(CLAUDE), captured_at="2020-01-01T00:01:00Z")
        self.assertEqual(self.run_cycle()["emitted"], 1)
        self.assertEqual(path.read_bytes(), CLAUDE + addition)
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)

    def test_revoked_receipt_or_new_pin_reexports(self):
        path = self.emit()
        self.consume(path)
        feed.reap(self.policy)
        self.private_json(self.learning / "learning/records/pending.json", {"state": "active", "path": str(path)})
        self.assertEqual(self.run_cycle()["emitted"], 1)
        self.assertTrue(path.exists())

    def test_cache_capacity_recovery_admits_later_artifact(self):
        path = self.emit()
        self.consume(path)
        used = self.run_cycle()["cache_bytes"]
        self.add_ref(CODEX, "codex")
        result = self.run_cycle(max_cache_bytes=used + 100)
        self.assertEqual(result["reaped_current"], 1, result)
        self.assertEqual(result["reaped_objects"], 1, result)
        self.assertEqual(result["emitted"], 1, result)
        self.assertFalse(result["cache_limit"])
        self.assertEqual([p.read_bytes() for p in self.current()], [CODEX])

    def test_another_hardlink_keeps_object_and_accounting(self):
        path = self.emit()
        self.consume(path)
        outside = Path(self.temp.name) / "external.jsonl"
        os.link(path, outside)
        result = feed.reap(self.policy)
        self.assertEqual(result["reaped_current"], 1)
        self.assertEqual(result["reaped_objects"], 0)
        self.assertEqual(result["cache_bytes"], len(CLAUDE))
        self.assertEqual(outside.read_bytes(), CLAUDE)
        outside.unlink()
        self.assertEqual(feed.reap(self.policy)["reaped_objects"], 1)

    def test_symlinks_unknown_ownership_and_nonproducer_paths_preserved(self):
        path = self.emit()
        self.consume(path)
        original = Path(self.temp.name) / "core-local.jsonl"
        original.write_bytes(CLAUDE)
        original.chmod(0o600)
        path.unlink()
        path.symlink_to(original)
        result = feed.reap(self.policy)
        self.assertTrue(result["retention_unresolved"])
        self.assertEqual(original.read_bytes(), CLAUDE)
        self.assertTrue(path.is_symlink())

    def test_failed_receipt_persistence_does_not_delete(self):
        path = self.emit()
        self.consume(path)
        with patch.object(feed, "_atomic", side_effect=OSError()):
            self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)
        self.assertTrue(path.exists())

    def test_manual_cli_uses_no_sdk_or_credentials(self):
        for entry in (Path(feed.__file__), Path(feed.__file__).parent / "scripts/export-archive-transcripts.py"):
            path = self.emit()
            self.consume(path)
            policy_path = Path(self.temp.name) / "policy.json"
            self.private_json(policy_path, self.policy)
            result = subprocess.run([sys.executable, str(entry), "--policy", str(policy_path), "--reap"],
                                    cwd=Path(feed.__file__).parent, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["reaped_current"], 1)
            self.assertFalse(Path(self.policy["credentials_file"]).exists())
            # Remove the receipt to permit this fixture's second re-export.
            (self.learning / "learning/.intervention-consumption.json").unlink()

    def test_locked_reaper_reads_complete_ownership_tree_once(self):
        self.add_ref()
        self.add_ref(CODEX, "codex")
        self.assertEqual(self.run_cycle()["emitted"], 2)
        for path in self.current():
            self.consume(path)
        with patch.object(feed, "_retention_state", wraps=feed._retention_state) as snapshot:
            result = feed.reap(self.policy)
        self.assertEqual(result["reaped_current"], 2, result)
        self.assertEqual(result["reaped_objects"], 2, result)
        self.assertEqual(snapshot.call_count, 1)

    def test_learning_controller_lock_skips_reaping(self):
        from twill_lock import StateLock
        path = self.emit()
        self.consume(path)
        with StateLock(self.learning / "learning"):
            result = feed.reap(self.policy)
        self.assertTrue(result["retention_busy"])
        self.assertFalse(result["retention_unresolved"])
        self.assertTrue(path.exists())
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 1)

    def test_manual_reap_bounds_and_remote_removal_do_not_resurrect(self):
        path = self.emit()
        self.consume(path)
        self.policy["retention"]["max_bytes"] = 1
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 0)
        self.policy["retention"]["max_bytes"] = 2_000_000
        self.assertEqual(feed.reap(self.policy)["reaped_current"], 1)
        refs = [key for key in self.client.objects if key.startswith(self.policy["refs_prefix"])]
        for key in refs:
            del self.client.objects[key]
        result = self.run_cycle()
        self.assertEqual(result["withdrawn"], 0)
        self.assertEqual(result["emitted"], 0)
        self.assertEqual(result["current_artifacts"], 0)


# Do not run the inherited archive suite twice from this module.
for _name in list(vars(ArchiveFeedTests)):
    if _name.startswith("test_") and _name not in ConsumedCacheTests.__dict__:
        setattr(ConsumedCacheTests, _name, None)
del ArchiveFeedTests

if __name__ == "__main__":
    unittest.main()
