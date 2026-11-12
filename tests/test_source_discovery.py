"""Fair inventory and durable native scan progress across large enrolled corpora."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from twill_source_discovery import DiscoveryQueue, discovery_coverage, validate_limits
from twill_human_steering import discover, verify
from twill_learning_intake import discover_sources, resolve


class FakeEntries:
    def __init__(self, directory, count):
        self.directory, self.count, self.position = directory, count, 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def __iter__(self):
        return self

    def __next__(self):
        if self.position >= self.count:
            raise StopIteration
        position = self.position
        self.position += 1
        class Entry:
            name = f"session-{position:06}.jsonl"
            path = str(Path(self.directory) / name)
            def is_symlink(self):
                return False
            def is_dir(self, **kwargs):
                return False
        return Entry()


class SourceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.artifacts = self.root / "private"
        self.codex = self.root / ".codex" / "sessions"
        self.claude = self.root / ".claude" / "projects"
        self.codex.mkdir(parents=True)
        self.claude.mkdir(parents=True)
        self.policy = {"intervention_sources": [str(self.codex / "*.jsonl"), str(self.claude / "*.jsonl")],
                       "intervention_settle_seconds": 60}

    def queue(self, policy=None):
        return DiscoveryQueue(self.artifacts, "test", policy or self.policy, 128 * 1024 * 1024)

    def source(self, directory, name, text="{}\n"):
        path = directory / (name + ".jsonl")
        path.write_text(text)
        os.utime(path, (1, 1))
        return path

    def test_more_than_20000_first_provider_entries_cannot_starve_second(self):
        source = self.source(self.claude, "authentic")
        real_scan = os.scandir
        observed = []
        with self.queue() as queue:
            original = queue._observe
            def observe(pattern, path):
                observed.append(path)
                if path == source:
                    original(pattern, path)
            def scan(directory):
                return FakeEntries(str(directory), 22001) if Path(directory) == self.codex else real_scan(directory)
            with patch("twill_source_discovery.os.scandir", side_effect=scan), patch.object(queue, "_observe", side_effect=observe):
                queue.inventory(time.monotonic() + 30)
            self.assertIn(source, observed)
            self.assertEqual(sum(p.parent == self.codex for p in observed), 512)
            selected = list(queue.pending(time.monotonic() + 30))
            self.assertEqual([item[0] for item in selected], [str(source)])
            queue.finish({})
        coverage = discovery_coverage(self.artifacts)["test"]
        self.assertEqual(coverage["providers"]["claude"]["known"], 1)
        self.assertEqual(coverage["last_cycle"]["inventoried"], 513)

    def test_directory_frontier_resumes_without_restarting_whole_glob(self):
        self.policy["discovery_limits"] = {"inventory_per_glob": 8}
        found = []
        for _ in range(3):
            with self.queue() as queue:
                with patch("twill_source_discovery.os.scandir", side_effect=lambda directory: FakeEntries(str(directory), 22001)), \
                     patch.object(queue, "_observe", side_effect=lambda pattern, path: found.append(path.name)):
                    queue.inventory(time.monotonic() + 30)
                queue.finish({})
        # Both providers resume the next eight directory entries each time.
        self.assertEqual(len(found), 48)
        self.assertEqual(found.count("session-000000.jsonl"), 2)
        self.assertEqual(found.count("session-000023.jsonl"), 2)

    def test_completed_fingerprints_beyond_4096_and_legacy_cursors_survive(self):
        entries = {}
        for n in range(4200):
            path = str(self.codex / f"old-{n}.jsonl")
            entries[path] = {"fingerprint": [2, 1, n], "offset": 2, "line": 1, "done": True,
                             "episode": {}}
        partial = str(self.claude / "partial.jsonl")
        entries[partial] = {"fingerprint": [9, 1, 9], "offset": 5, "line": 1, "done": False,
                            "episode": {"stage": 1, "refs": [{"sha256": "a" * 64,
                                                                  "text": "private source prose"}]}}
        with self.queue() as queue:
            queue.import_legacy({"files": entries, "outputs": ["existing.json"]})
            queue.finish({})
        with self.queue() as queue:
            # Idempotent migration never overwrites the authoritative queue.
            queue.import_legacy({"files": {}, "outputs": []})
            rows = queue.db.execute("SELECT done,cursor FROM source WHERE consumer='test'").fetchall()
            self.assertEqual(sum(row["done"] for row in rows), 4200)
            cursor = json.loads(queue.db.execute("SELECT cursor FROM source WHERE path=?", (partial,)).fetchone()[0])
            self.assertEqual(cursor["offset"], 5)
            self.assertEqual(cursor["episode"]["stage"], 1)
            self.assertNotIn("text", cursor["episode"]["refs"][0])
            self.assertEqual(queue.outputs(), ["existing.json"])
            queue.finish({})
        coverage = discovery_coverage(self.artifacts)["test"]
        self.assertEqual(coverage["providers"]["codex"]["completed"], 4200)
        database = self.artifacts / "learning" / ".source-discovery.sqlite3"
        self.assertEqual(database.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b"private source prose", database.read_bytes())

    def test_completed_file_changed_fingerprint_requeues_without_losing_other_completions(self):
        source = self.source(self.codex, "changed")
        for cycle in range(3):
            with self.queue() as queue:
                queue.inventory(time.monotonic() + 30)
                selected = list(queue.pending(time.monotonic() + 30))
                self.assertEqual(len(selected), 1 if cycle in (0, 2) else 0)
                for path, fp, old in selected:
                    queue.save(path, {"fingerprint": fp, "offset": 3, "line": 1, "done": True})
                queue.finish({})
            if cycle == 1:
                source.write_text('{"changed":true}\n')
                os.utime(source, (1, 1))

    def test_processing_cap_is_round_robin_across_providers_and_persists_resume(self):
        self.policy["discovery_limits"] = {"max_files_per_cycle": 8}
        for directory in (self.codex, self.claude):
            for n in range(10):
                self.source(directory, f"native-{n}")
        selected_cycles = []
        for _ in range(2):
            with self.queue() as queue:
                queue.inventory(time.monotonic() + 30)
                selected = list(queue.pending(time.monotonic() + 30))
                self.assertEqual(len(selected), 8)
                self.assertEqual([Path(item[0]).parent for item in selected], [self.codex, self.claude] * 4)
                for path, fp, old in selected:
                    queue.save(path, {"fingerprint": fp, "offset": 3, "line": 1, "done": True})
                selected_cycles.append({item[0] for item in selected})
                queue.finish({})
        self.assertFalse(selected_cycles[0] & selected_cycles[1])

    def test_timeout_rotates_next_provider_and_preserves_partial_cursor(self):
        first = self.source(self.codex, "first")
        second = self.source(self.claude, "second")
        with self.queue() as queue:
            queue.inventory(time.monotonic() + 30)
            pending = queue.pending(time.monotonic() + 30)
            path, fp, old = next(pending)
            self.assertEqual(path, str(first))
            queue.save(path, {"fingerprint": fp, "offset": 2, "line": 1, "done": False})
            pending.close()
            queue.finish({})
        with self.queue() as queue:
            pending = queue.pending(time.monotonic() + 30)
            self.assertEqual(next(pending)[0], str(second))
            pending.close()
            queue.finish({})

    def test_source_replaced_by_symlink_is_rejected_before_open(self):
        source = self.source(self.codex, "replaced")
        with self.queue() as queue:
            queue.inventory(time.monotonic() + 30)
            source.unlink()
            source.symlink_to(self.source(self.claude, "target"))
            self.assertEqual(list(queue.pending(time.monotonic() + 30)), [])
            queue.finish({})

    def test_human_discovery_native_both_providers_with_recursive_globs(self):
        codex = self.codex / "2026" / "11"
        claude = self.claude / "workspace"
        codex.mkdir(parents=True)
        claude.mkdir(parents=True)
        text = "For all future tasks, Preserve public API compatibility."
        records = [(codex, [{"type": "response_item", "payload": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": text}]}}]),
                   (claude, [{"type": "user", "cwd": "/workspace", "message": {"role": "user", "content": text}}])]
        for directory, items in records:
            self.source(directory, "native", "".join(json.dumps(item) + "\n" for item in items))
        policy = dict(self.policy, intervention_sources=[str(self.codex / "**/*.jsonl"), str(self.claude / "**/*.jsonl")],
                      human_steering={"enabled": True, "scopes": ["global"]}, scopes={"global": {"global": True}})
        outputs = discover(policy, self.artifacts)
        self.assertEqual(len(outputs), 2)
        self.assertTrue(all(verify(path)["complete"] for path in outputs))
        self.assertEqual(discover(policy, self.artifacts), outputs)
        coverage = discovery_coverage(self.artifacts)["human-steering"]
        self.assertEqual(coverage["providers"]["codex"]["completed"], 1)
        self.assertEqual(coverage["providers"]["claude"]["completed"], 1)

    def test_workflow_discovery_verifies_both_providers_in_same_cycle(self):
        turns = [("user", "Complete the workflow."),
                 ("assistant", "Implemented and deployed the workflow."),
                 ("user", "What lessons are implemented?"),
                 ("assistant", "The workflow still has a gap: an unassigned draft."),
                 ("user", "Get the full cycle working completely; complete the workflow.")]
        codex = [{"type": "session_meta", "payload": {"cwd": "/workspace"}}] + [
            {"type": "response_item", "payload": {"role": role,
             "phase": "final_answer" if role == "assistant" else None,
             "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}
            for role, text in turns]
        claude = [{"type": role, "cwd": "/workspace", "sessionId": "native-claude",
                   "isSidechain": False, "isMeta": False,
                   "message": {"role": role, "content": [{"type": "text", "text": text}],
                               **({"stop_reason": "end_turn"} if role == "assistant" else {})}}
                  for role, text in turns]
        self.source(self.codex, "native", "".join(json.dumps(item) + "\n" for item in codex))
        self.source(self.claude, "native", "".join(json.dumps(item) + "\n" for item in claude))
        policy = dict(self.policy, scopes={"global": {"global": True}},
                      auto_evaluators=[{"name": "workflow-completion/v1", "scopes": ["global"]}])
        outputs = discover_sources(policy, self.artifacts)
        self.assertEqual(len(outputs), 2)
        evidence = [resolve({"detector": "workflow-completion", "source": {"path": str(path)}}, policy)
                    for path in outputs]
        self.assertTrue(all(item["source_evidence"]["complete"] for item in evidence))
        self.assertEqual({item["source_evidence"]["source_providers"][0] for item in evidence}, {"codex", "claude"})
        self.assertTrue(all(item["source_evidence"]["task_terminal_state"] == "unknown" for item in evidence))
        # Closing the parser at EOF in a later cycle retains the same evidence.
        self.assertEqual(discover_sources(policy, self.artifacts), outputs)

    def test_optional_feed_absent_and_explicit_provider_directories(self):
        self.assertEqual(discovery_coverage(self.artifacts), {})
        policy = dict(self.policy, intervention_sources=[str(self.root / "feed" / provider / "*.jsonl")
                                                        for provider in ("codex", "claude-code")])
        with self.queue(policy) as queue:
            queue.inventory(time.monotonic() + 30)
            self.assertEqual(list(queue.pending(time.monotonic() + 30)), [])
            queue.finish({})
        self.assertEqual(set(discovery_coverage(self.artifacts)["test"]["providers"]), {"codex", "claude"})

    def test_bad_limits_fail_before_inventory(self):
        for limits in ({"unknown": 8}, {"max_files_per_cycle": 7}, {"scan_seconds": True},
                       {"inventory_per_glob": 8193}, {"scan_seconds": 121}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                validate_limits({"discovery_limits": limits})


if __name__ == "__main__":
    unittest.main()
