"""Compatibility tests for the private artifacts_root interchange snapshot."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_artifacts import (  # noqa: E402
    ARTIFACT_DIRS,
    CONTRACT_SCHEMA,
    ArtifactContractError,
    manifest_after_write,
    read_manifest,
    write_manifest,
)


class ArtifactContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts = self.root / "private-artifacts"
        for directory in ARTIFACT_DIRS:
            (self.artifacts / directory).mkdir(parents=True)
        files = {
            "lessons/L-0123abcd.md": (
                b"---\n"
                b"id: L-0123abcd\n"
                b"summary: \"A safe lesson.\"\n"
                b"state: draft\n"
                b"detector: D-01\n"
                b"key: \"command-not-found:test\"\n"
                b"evidence: {sessions: 1, events: 1, first_seen: \"2026-09-01\", session_ids: [\"session-1\"]}\n"
                b"routing: {recommended: null, reason: null, applied: null, applied_at: null, bead: null}\n"
                b"backtest: {window_days: 30, sessions: 1, first_seen: \"2026-09-01\", weeks_present: 1}\n"
                b"guard: {layer: null, artifact: null, installed: false}\n"
                b"---\n"
                b"A safe lesson.\n"
            ),
            "digests/2026-W38.txt": (
                b"digest | $ twill digest --week 2026-W38 --stdout --state-dir /tmp/twill-state\n"
            ),
            "measurements/L-0123abcd.jsonl": (
                b"{\"lesson_id\":\"L-0123abcd\",\"detector_id\":\"D-01@1\",\"measured_at\":\"2026-09-20T00:00:00Z\",\"window_days\":7,\"sessions\":1,\"events\":1}\n"
            ),
            "guards/L-0123abcd.hook.json": (
                b"{\"schema\":\"twill-guard/v1\",\"lesson_id\":\"L-0123abcd\",\"detector\":\"D-01\",\"key\":\"command-not-found:test\",\"install\":{\"human_only\":true,\"instruction\":\"Review and install manually.\"}}\n"
            ),
        }
        for relative, content in files.items():
            (self.artifacts / relative).write_bytes(content)

    def test_manifest_describes_a_complete_versioned_snapshot(self):
        path = write_manifest(
            self.artifacts,
            generated_at="2026-09-28T12:00:00Z",
            repo_root=ROOT,
        )

        self.assertEqual(path, self.artifacts / "manifest.json")
        manifest = read_manifest(self.artifacts, repo_root=ROOT)
        self.assertEqual(manifest["schema"], CONTRACT_SCHEMA)
        self.assertEqual(manifest["producer"], "twill")
        self.assertEqual(manifest["generated_at"], "2026-09-28T12:00:00Z")
        self.assertEqual(
            {entry["path"] for entry in manifest["artifacts"]},
            {
                "lessons/L-0123abcd.md",
                "digests/2026-W38.txt",
                "measurements/L-0123abcd.jsonl",
                "guards/L-0123abcd.hook.json",
            },
        )
        for entry in manifest["artifacts"]:
            content = (self.artifacts / entry["path"]).read_bytes()
            self.assertEqual(entry["bytes"], len(content))
            self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())

    def test_additive_metadata_is_compatible(self):
        write_manifest(self.artifacts, generated_at="2026-09-28T12:00:00Z", repo_root=ROOT)
        path = self.artifacts / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["future_metadata"] = {"retention_hint": "consumer-defined"}
        payload["artifacts"][0]["future_field"] = True
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        self.assertEqual(read_manifest(self.artifacts, repo_root=ROOT)["schema"], CONTRACT_SCHEMA)

    def test_unknown_contract_version_is_not_silently_consumed(self):
        write_manifest(self.artifacts, repo_root=ROOT)
        path = self.artifacts / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema"] = "twill-artifacts/v2"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

    def test_stale_inventory_and_hash_mismatch_are_rejected(self):
        write_manifest(self.artifacts, repo_root=ROOT)
        (self.artifacts / "digests/2026-W39.txt").write_text("new\n", encoding="utf-8")
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

        (self.artifacts / "digests/2026-W39.txt").unlink()
        (self.artifacts / "lessons/L-0123abcd.md").write_bytes(b"changed\n")
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

    def test_manifest_remains_external_to_public_tree(self):
        with self.assertRaises(Exception):
            write_manifest(ROOT / ".artifact-contract-root", repo_root=ROOT)

    def test_empty_namespaces_still_publish_required_schema_metadata(self):
        empty = self.root / "empty-artifacts"
        (empty / "lessons").mkdir(parents=True)

        write_manifest(empty, generated_at="2026-09-28T12:00:00Z", repo_root=ROOT)
        manifest = read_manifest(empty, repo_root=ROOT)

        self.assertEqual(manifest["artifacts"], [])
        self.assertEqual(set(manifest["paths"]), set(ARTIFACT_DIRS))
        for namespace in ARTIFACT_DIRS:
            self.assertIn("pattern", manifest["paths"][namespace])
            self.assertIn("schema", manifest["paths"][namespace])

    def test_partial_artifact_write_restores_the_previous_snapshot(self):
        write_manifest(self.artifacts, generated_at="2026-09-28T12:00:00Z", repo_root=ROOT)
        path = self.artifacts / "digests/2026-W38.txt"
        previous_artifact = path.read_bytes()
        previous_manifest = (self.artifacts / "manifest.json").read_bytes()

        with mock.patch(
            "twill_artifacts.write_manifest",
            side_effect=RuntimeError("injected manifest failure"),
        ):
            with self.assertRaises(RuntimeError):
                with manifest_after_write(self.artifacts, (path,), repo_root=ROOT):
                    path.write_bytes(b"partial artifact\n")

        self.assertEqual(path.read_bytes(), previous_artifact)
        self.assertEqual(
            (self.artifacts / "manifest.json").read_bytes(), previous_manifest
        )
        self.assertEqual(
            read_manifest(self.artifacts, repo_root=ROOT)["artifacts"],
            json.loads(previous_manifest)["artifacts"],
        )

    def test_unknown_root_files_and_additive_fields_are_compatible(self):
        write_manifest(self.artifacts, repo_root=ROOT)
        (self.artifacts / "README.md").write_text("private repository notes\n", encoding="utf-8")
        (self.artifacts / "metadata").mkdir()
        path = self.artifacts / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["future_metadata"] = {"retention_hint": "consumer-defined"}
        payload["paths"]["lessons"]["future_pattern"] = "consumer-defined"
        payload["artifacts"][0]["future_field"] = True
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        self.assertEqual(read_manifest(self.artifacts, repo_root=ROOT)["schema"], CONTRACT_SCHEMA)

    def test_inventory_entries_must_be_unique_and_complete(self):
        write_manifest(self.artifacts, repo_root=ROOT)
        path = self.artifacts / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["artifacts"].append(dict(payload["artifacts"][0]))
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

        write_manifest(self.artifacts, repo_root=ROOT)
        path = self.artifacts / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["artifacts"][0]["path"] = "lessons/../lessons/L-0123abcd.md"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

        write_manifest(self.artifacts, repo_root=ROOT)
        path = self.artifacts / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["artifacts"].pop()
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

    def test_symlink_size_and_hash_mismatches_are_rejected(self):
        write_manifest(self.artifacts, repo_root=ROOT)
        target = self.artifacts / "guards/L-0123abcd.hook.json"
        target.unlink()
        os.symlink("../lessons/L-0123abcd.md", target)
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

        target.unlink()
        target.write_text(
            "{\"schema\":\"twill-guard/v1\",\"lesson_id\":\"L-0123abcd\",\"detector\":\"D-01\",\"key\":\"command-not-found:test\",\"install\":{\"human_only\":true,\"instruction\":\"Review and install manually.\"}}\n",
            encoding="utf-8",
        )
        path = self.artifacts / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["artifacts"][-1]["bytes"] += 1
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

    def test_each_artifact_payload_and_reference_is_validated(self):
        lesson = self.artifacts / "lessons/L-0123abcd.md"
        valid_lesson = lesson.read_text(encoding="utf-8")
        write_manifest(self.artifacts, repo_root=ROOT)
        lesson.write_text(valid_lesson + ("x" * 241) + "\n", encoding="utf-8")
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

        lesson.write_text(valid_lesson, encoding="utf-8")
        digest = self.artifacts / "digests/2026-W38.txt"
        digest.write_text("leaked token=secret | $ twill digest --week 2026-W38 --stdout --state-dir /tmp/state\n", encoding="utf-8")
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

        digest.write_text(
            "digest | $ twill digest --week 2026-W38 --stdout --state-dir /tmp/twill-state\n",
            encoding="utf-8",
        )
        measurement = self.artifacts / "measurements/L-0123abcd.jsonl"
        measurement.write_text(measurement.read_text(encoding="utf-8") * 2, encoding="utf-8")
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

        measurement.write_text(
            "{\"lesson_id\":\"L-0123abcd\",\"detector_id\":\"D-01@1\",\"measured_at\":\"2026-09-20T00:00:00Z\",\"window_days\":7,\"sessions\":1,\"events\":1}\n",
            encoding="utf-8",
        )
        guard = self.artifacts / "guards/L-0123abcd.hook.json"
        guard.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

        guard.write_text(
            "{\"schema\":\"twill-guard/v1\",\"lesson_id\":\"L-0123abcd\",\"detector\":\"D-01\",\"key\":\"command-not-found:test\",\"install\":{\"human_only\":true,\"instruction\":\"Review and install manually.\"}}\n",
            encoding="utf-8",
        )
        lesson.write_text(
            lesson.read_text(encoding="utf-8").replace(
                "guard: {layer: null, artifact: null, installed: false}",
                'guard: {layer: hook, artifact: "guards/L-deadbeef.hook.json", installed: false}',
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

    def test_consumer_rejects_manifest_for_missing_or_partial_artifact(self):
        write_manifest(self.artifacts, repo_root=ROOT)
        manifest_path = self.artifacts / "manifest.json"
        digest = self.artifacts / "digests/2026-W38.txt"
        digest.unlink()

        # A manifest copied from a candidate commit must never make a missing
        # artifact look consumable.
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

        digest.write_bytes(b"partial artifact")
        # The old manifest still names the complete artifact's size/hash; a
        # partially written replacement is rejected before indexing too.
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)

    def test_measurements_and_guards_must_reference_an_existing_lesson(self):
        write_manifest(self.artifacts, repo_root=ROOT)
        (self.artifacts / "lessons/L-0123abcd.md").unlink()
        with self.assertRaises(ArtifactContractError):
            read_manifest(self.artifacts, repo_root=ROOT)


if __name__ == "__main__":
    unittest.main()
