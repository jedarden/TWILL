"""Committed snapshot publication and retry coverage."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_artifacts import (  # noqa: E402
    ARTIFACT_DIRS,
    ArtifactContractError,
    ArtifactPublicationError,
    publish_snapshot,
    read_committed_manifest,
    read_manifest,
    write_manifest,
)
from twill_digest import write_digest_file  # noqa: E402


LESSON = (
    "---\n"
    "id: L-0123abcd\n"
    "summary: \"A safe lesson.\"\n"
    "state: draft\n"
    "detector: D-01\n"
    "key: \"command-not-found:test\"\n"
    "evidence: {sessions: 1, events: 1, first_seen: \"2026-09-01\", session_ids: [\"session-1\"]}\n"
    "routing: {recommended: null, reason: null, applied: null, applied_at: null, bead: null}\n"
    "backtest: {window_days: 30, sessions: 1, first_seen: \"2026-09-01\", weeks_present: 1}\n"
    "guard: {layer: null, artifact: null, installed: false}\n"
    "---\n"
    "A safe lesson.\n"
)
GUARD = (
    '{"schema":"twill-guard/v1","lesson_id":"L-0123abcd",'
    '"detector":"D-01","key":"command-not-found:test",'
    '"install":{"human_only":true,"instruction":"Review and install manually."}}\n'
)
MEASUREMENT = (
    '{"lesson_id":"L-0123abcd","detector_id":"D-01@1",'
    '"measured_at":"2026-09-20T00:00:00Z","window_days":7,'
    '"sessions":1,"events":1}\n'
)


class ArtifactPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.artifacts = root / "artifacts"
        self.remote = root / "artifacts-origin.git"
        self.remote.mkdir()
        self._run_git(self.remote, "init", "--bare", "-q")
        for directory in ARTIFACT_DIRS:
            (self.artifacts / directory).mkdir(parents=True)
        write_manifest(self.artifacts, repo_root=ROOT)
        self._run_git(self.artifacts, "init", "-q", "-b", "main")
        self._run_git(self.artifacts, "config", "user.name", "fixture")
        self._run_git(self.artifacts, "config", "user.email", "fixture@example.test")
        self._run_git(self.artifacts, "remote", "add", "origin", str(self.remote))
        self._run_git(self.artifacts, "add", "--", "manifest.json")
        self._run_git(self.artifacts, "commit", "-qm", "initial empty artifact snapshot")
        self._run_git(self.artifacts, "push", "-q", "-u", "origin", "HEAD:main")

    def _run_git(self, cwd: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def add_complete_snapshot(self):
        (self.artifacts / "lessons/L-0123abcd.md").write_text(LESSON, encoding="utf-8")
        (self.artifacts / "digests/2026-W38.txt").write_text(
            "digest | $ twill digest --week 2026-W38 --stdout --state-dir /tmp/twill-state\n",
            encoding="utf-8",
        )
        (self.artifacts / "measurements/L-0123abcd.jsonl").write_text(
            MEASUREMENT, encoding="utf-8"
        )
        (self.artifacts / "guards/L-0123abcd.hook.json").write_text(
            GUARD, encoding="utf-8"
        )
        write_manifest(self.artifacts, repo_root=ROOT)

    def test_success_commits_one_complete_snapshot_consumers_can_validate(self):
        self.add_complete_snapshot()

        result = publish_snapshot(self.artifacts, repo_root=ROOT)

        self.assertTrue(result.created_commit)
        self.assertTrue(result.pushed)
        self.assertEqual(
            set(result.changed_paths),
            {
                "manifest.json",
                "lessons/L-0123abcd.md",
                "digests/2026-W38.txt",
                "measurements/L-0123abcd.jsonl",
                "guards/L-0123abcd.hook.json",
            },
        )
        remote_head = self._run_git(self.remote, "rev-parse", "refs/heads/main")
        self.assertEqual(remote_head, result.commit)
        self.assertEqual(read_committed_manifest(self.artifacts, repo_root=ROOT)["schema"], "twill-artifacts/v1")

        consumer = Path(self.temporary.name) / "consumer"
        self._run_git(
            Path(self.temporary.name),
            "clone",
            "-q",
            "-b",
            "main",
            str(self.remote),
            str(consumer),
        )
        self.assertEqual(
            read_manifest(consumer, repo_root=ROOT)["artifacts"],
            read_committed_manifest(self.artifacts, repo_root=ROOT)["artifacts"],
        )

    def test_successful_artifact_write_refreshes_manifest_before_publication(self):
        initial = read_manifest(self.artifacts, repo_root=ROOT)
        digest = "digest | $ twill digest --week 2026-W38 --stdout --state-dir /tmp/twill-state\n"

        path = write_digest_file(digest, self.artifacts, (2026, 38), repo_root=ROOT)

        manifest = read_manifest(self.artifacts, repo_root=ROOT)
        self.assertEqual(manifest["schema"], "twill-artifacts/v1")
        self.assertNotEqual(manifest, initial)
        self.assertEqual(
            manifest["artifacts"],
            [
                {
                    "bytes": len(digest.encode("utf-8")),
                    "path": "digests/2026-W38.txt",
                    "schema": "twill-digest/v1",
                    "sha256": hashlib.sha256(digest.encode("utf-8")).hexdigest(),
                }
            ],
        )
        self.assertEqual(path, self.artifacts / "digests/2026-W38.txt")

        result = publish_snapshot(self.artifacts, repo_root=ROOT)

        committed = read_committed_manifest(
            self.artifacts, commit=result.commit, repo_root=ROOT
        )
        self.assertEqual(committed["schema"], "twill-artifacts/v1")
        self.assertEqual(committed["artifacts"], manifest["artifacts"])

    def test_failed_publication_keeps_partial_write_invisible_to_consumers(self):
        self.add_complete_snapshot()
        published = publish_snapshot(self.artifacts, repo_root=ROOT)
        previous_manifest = read_committed_manifest(
            self.artifacts, commit=published.commit, repo_root=ROOT
        )
        digest = self.artifacts / "digests/2026-W38.txt"
        previous_digest = digest.read_bytes()
        digest.write_bytes(previous_digest + b"partial\n")

        with self.assertRaises(ArtifactContractError):
            publish_snapshot(self.artifacts, repo_root=ROOT)

        self.assertEqual(
            self._run_git(self.remote, "rev-parse", "refs/heads/main"), published.commit
        )
        consumer = Path(self.temporary.name) / "consumer-after-failure"
        self._run_git(
            Path(self.temporary.name),
            "clone",
            "-q",
            "-b",
            "main",
            str(self.remote),
            str(consumer),
        )
        self.assertEqual(
            read_committed_manifest(consumer, repo_root=ROOT), previous_manifest
        )
        self.assertEqual(
            (consumer / "digests/2026-W38.txt").read_bytes(), previous_digest
        )

    def test_second_attempt_is_a_noop_and_reuses_the_committed_snapshot(self):
        self.add_complete_snapshot()
        first = publish_snapshot(self.artifacts, repo_root=ROOT)

        second = publish_snapshot(self.artifacts, repo_root=ROOT)

        self.assertFalse(second.created_commit)
        self.assertEqual(second.commit, first.commit)
        self.assertEqual(self._run_git(self.remote, "rev-parse", "refs/heads/main"), first.commit)

    def test_push_failure_leaves_clean_commit_for_same_commit_retry(self):
        self.add_complete_snapshot()
        self._run_git(self.artifacts, "remote", "set-url", "origin", str(self.remote) + ".missing")

        with self.assertRaises(ArtifactPublicationError):
            publish_snapshot(self.artifacts, repo_root=ROOT)

        local_commit = self._run_git(self.artifacts, "rev-parse", "HEAD")
        self.assertEqual(self._run_git(self.artifacts, "status", "--porcelain"), "")
        self._run_git(self.artifacts, "remote", "set-url", "origin", str(self.remote))
        retry = publish_snapshot(self.artifacts, repo_root=ROOT)

        self.assertFalse(retry.created_commit)
        self.assertEqual(retry.commit, local_commit)
        self.assertEqual(self._run_git(self.remote, "rev-parse", "refs/heads/main"), local_commit)

    def test_invalid_worktree_never_reaches_the_remote_snapshot(self):
        self.add_complete_snapshot()
        before = self._run_git(self.remote, "rev-parse", "refs/heads/main")
        path = self.artifacts / "digests/2026-W38.txt"
        path.write_text(path.read_text(encoding="utf-8") + "partial\n", encoding="utf-8")

        with self.assertRaises(ArtifactContractError):
            publish_snapshot(self.artifacts, repo_root=ROOT)

        self.assertEqual(self._run_git(self.remote, "rev-parse", "refs/heads/main"), before)
        self.assertEqual(read_committed_manifest(self.artifacts, repo_root=ROOT)["schema"], "twill-artifacts/v1")

    def test_unrelated_checkout_change_is_rejected_before_staging(self):
        self.add_complete_snapshot()
        (self.artifacts / "README.md").write_text("private notes\n", encoding="utf-8")

        with self.assertRaises(ArtifactPublicationError):
            publish_snapshot(self.artifacts, repo_root=ROOT)

        status = self._run_git(self.artifacts, "status", "--porcelain")
        self.assertIn("?? README.md", status)
        self.assertEqual(
            self._run_git(self.remote, "rev-parse", "refs/heads/main"),
            self._run_git(self.artifacts, "rev-parse", "HEAD"),
        )


if __name__ == "__main__":
    unittest.main()
