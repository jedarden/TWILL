"""Consumer-facing reads of committed twill-artifacts/v1 snapshots."""

from __future__ import annotations

import json
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
    read_committed_lessons,
    read_committed_manifest,
    read_retrieval_lessons,
    write_manifest,
)


def lesson_text(state: str, applied: str | None = None) -> str:
    bead = "twill-reader" if applied not in {None, "retrieval_only"} else "null"
    applied_at = '"2026-09-01T00:00:00Z"' if applied is not None else "null"
    return (
        "---\n"
        "id: L-0123abcd\n"
        'summary: "A safe lesson."\n'
        f"state: {state}\n"
        "detector: D-01\n"
        'key: "command-not-found:test"\n'
        'evidence: {sessions: 1, events: 1, first_seen: "2026-09-01", session_ids: ["session-1"]}\n'
        f"routing: {{recommended: null, reason: null, applied: {applied or 'null'}, applied_at: {applied_at}, bead: {bead}}}\n"
        'backtest: {window_days: 30, sessions: 1, first_seen: "2026-09-01", weeks_present: 1}\n'
        "guard: {layer: null, artifact: null, installed: false}\n"
        "---\n"
        "A safe lesson.\n"
    )


class ArtifactReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts = self.root / "artifacts"
        for directory in ARTIFACT_DIRS:
            (self.artifacts / directory).mkdir(parents=True)
        self.lesson = self.artifacts / "lessons/L-0123abcd.md"
        self.lesson.write_text(lesson_text("accepted"), encoding="utf-8")
        write_manifest(
            self.artifacts,
            generated_at="2026-09-28T12:00:00Z",
            repo_root=ROOT,
        )

    def _run_git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.artifacts,
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def _commit(self, message: str = "snapshot") -> str:
        self._run_git("add", "--", "manifest.json", "lessons/L-0123abcd.md")
        self._run_git("commit", "-qm", message)
        return self._run_git("rev-parse", "HEAD")

    def _commit_manifest(self, manifest: dict[str, object], message: str) -> str:
        (self.artifacts / "manifest.json").write_text(
            json.dumps(manifest) + "\n", encoding="utf-8"
        )
        return self._commit(message)

    def test_committed_reader_accepts_additive_fields_and_ignores_root_files(self):
        (self.artifacts / "README.md").write_text(
            "consumer documentation\n", encoding="utf-8"
        )
        manifest_path = self.artifacts / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["future_metadata"] = {"retention_hint": "consumer-defined"}
        manifest["paths"]["lessons"]["future_pattern"] = "consumer-defined"
        manifest["artifacts"][0]["future_field"] = True
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

        self._run_git("init", "-q", "-b", "main")
        self._run_git("config", "user.name", "fixture")
        self._run_git("config", "user.email", "fixture@example.test")
        self._run_git(
            "add",
            "--",
            "manifest.json",
            "lessons/L-0123abcd.md",
            "README.md",
        )
        self._run_git("commit", "-qm", "additive snapshot")
        commit = self._run_git("rev-parse", "HEAD")

        accepted = read_committed_manifest(self.artifacts, commit=commit, repo_root=ROOT)
        self.assertEqual(accepted["schema"], "twill-artifacts/v1")
        self.assertEqual(
            [record.id for record in read_committed_lessons(
                self.artifacts, commit=commit, repo_root=ROOT
            )],
            ["L-0123abcd"],
        )

    def test_committed_reader_uses_the_immutable_commit_not_the_worktree(self):
        self._run_git("init", "-q", "-b", "main")
        self._run_git("config", "user.name", "fixture")
        self._run_git("config", "user.email", "fixture@example.test")
        commit = self._commit()

        self.lesson.write_text("not a lesson\n", encoding="utf-8")
        self.assertEqual(
            [record.id for record in read_committed_lessons(
                self.artifacts, commit=commit, repo_root=ROOT
            )],
            ["L-0123abcd"],
        )

    def test_committed_reader_rejects_invalid_inventory_and_content_claims(self):
        self._run_git("init", "-q", "-b", "main")
        self._run_git("config", "user.name", "fixture")
        self._run_git("config", "user.email", "fixture@example.test")
        baseline = json.loads(
            (self.artifacts / "manifest.json").read_text(encoding="utf-8")
        )

        mutations = (
            ("unknown schema", lambda value: value.update(schema="twill-artifacts/v2")),
            (
                "missing required field",
                lambda value: value.pop("producer"),
            ),
            (
                "malformed path",
                lambda value: value["artifacts"][0].update(
                    path="lessons/../lessons/L-0123abcd.md"
                ),
            ),
            (
                "duplicate entry",
                lambda value: value["artifacts"].append(
                    dict(value["artifacts"][0])
                ),
            ),
            (
                "missing entry",
                lambda value: value["artifacts"].clear(),
            ),
            (
                "size mismatch",
                lambda value: value["artifacts"][0].update(bytes=0),
            ),
            (
                "hash mismatch",
                lambda value: value["artifacts"][0].update(sha256="0" * 64),
            ),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                candidate = json.loads(json.dumps(baseline))
                mutate(candidate)
                commit = self._commit_manifest(candidate, f"invalid {name}")
                with self.assertRaises(ArtifactContractError):
                    read_committed_manifest(
                        self.artifacts, commit=commit, repo_root=ROOT
                    )

        self._commit_manifest(baseline, "restore valid snapshot")
        symlink = self.artifacts / "lessons/L-ffffffff.md"
        symlink.symlink_to("L-0123abcd.md")
        self._run_git("add", "--", "lessons/L-ffffffff.md")
        self._run_git("commit", "-qm", "symlink snapshot")
        with self.assertRaises((ArtifactContractError, ArtifactPublicationError)):
            read_committed_manifest(self.artifacts, repo_root=ROOT)

    def test_retrieval_eligibility_includes_applied_and_terminal_layers(self):
        cases = (
            ("draft", None, False),
            ("accepted", None, True),
            ("applied:retrieval_only", "retrieval_only", True),
            ("resolved", "retrieval_only", True),
            ("escalated", "retrieval_only", True),
            ("retired", "retrieval_only", True),
        )
        for state, applied, eligible in cases:
            with self.subTest(state=state):
                self.lesson.write_text(lesson_text(state, applied), encoding="utf-8")
                write_manifest(self.artifacts, repo_root=ROOT)
                records = read_retrieval_lessons(self.artifacts, repo_root=ROOT)
                self.assertEqual(bool(records), eligible)


if __name__ == "__main__":
    unittest.main()
