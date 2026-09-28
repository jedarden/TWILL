"""Compatibility tests for the private artifacts_root interchange snapshot."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_artifacts import (  # noqa: E402
    ARTIFACT_DIRS,
    CONTRACT_SCHEMA,
    ArtifactContractError,
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
            "lessons/L-0123abcd.md": b"lesson\n",
            "digests/2026-W38.txt": b"digest\n",
            "measurements/L-0123abcd.jsonl": b"measurement\n",
            "guards/L-0123abcd.hook.json": b"guard\n",
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


if __name__ == "__main__":
    unittest.main()
