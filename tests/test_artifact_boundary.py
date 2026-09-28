"""End-to-end containment and mode checks for every artifact writer."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_digest  # noqa: E402
import twill_explainer  # noqa: E402
import twill_guards  # noqa: E402
import twill_measure  # noqa: E402
import twill_schema  # noqa: E402
import twill_lessons  # noqa: E402
from twill_config import ConfigError, TwillConfig  # noqa: E402
from twill_artifacts import read_manifest, write_manifest  # noqa: E402
from twill_ranker import RankedCluster  # noqa: E402


WEEK = (2026, 38)
WEEK_LABEL = "2026-W38"
LESSON_ID = "L-" + hashlib.sha256(
    b"D-01:command-not-found:sqlite3"
).hexdigest()[:8]


class ArtifactBoundaryTests(unittest.TestCase):
    """Prove that artifact producers share one external, private boundary."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts = self.root / "private-artifacts"
        self.artifacts.mkdir(mode=0o700)
        self.state = self.root / "state"
        self.in_tree_root = ROOT / ".artifact-boundary-root"
        self.addCleanup(self._remove_in_tree_root)
        self.assertFalse(self.in_tree_root.exists())

    def _remove_in_tree_root(self):
        if self.in_tree_root.is_dir() and not self.in_tree_root.is_symlink():
            for path in sorted(self.in_tree_root.rglob("*"), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            self.in_tree_root.rmdir()

    def _candidate(self):
        cluster = RankedCluster(
            detector_id="D-01",
            key="command-not-found:sqlite3",
            window_days=30,
            sessions=2,
            events=2,
            first_seen="2026-09-20T00:00:00+00:00",
            last_seen="2026-09-20T00:00:00+00:00",
            score=1.0,
            covered_by=None,
            state="open",
        )
        return twill_explainer.PromptCluster(
            cluster,
            (twill_explainer.PromptExcerpt(1, "session-1", "bounded evidence"),),
        )

    def _draft(self):
        return twill_explainer.LessonDraft(
            cluster_id="D-01:command-not-found:sqlite3",
            summary="A command fails repeatedly. Install it before retrying.",
        )

    def _write_lesson(self, artifacts_root):
        paths = twill_explainer.persist_lesson_drafts(
            (self._draft(),),
            (self._candidate(),),
            TwillConfig(artifacts_root=artifacts_root),
            repo_root=ROOT,
        )
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].name, f"{LESSON_ID}.md")
        self.assertIn(
            f"lessons/{LESSON_ID}.md",
            {entry["path"] for entry in read_manifest(artifacts_root, repo_root=ROOT)["artifacts"]},
        )
        return twill_lessons.load_lesson(paths[0], repo_root=ROOT)

    def _write_digest(self, artifacts_root):
        report = twill_digest.build_digest(
            self.state,
            WEEK,
            registry=(),
            artifacts_root=artifacts_root,
        )
        path = twill_digest.write_digest_file(
            twill_digest.render_text(report),
            artifacts_root,
            WEEK,
            repo_root=ROOT,
        )
        self.assertIn(
            f"digests/{WEEK_LABEL}.txt",
            {entry["path"] for entry in read_manifest(artifacts_root, repo_root=ROOT)["artifacts"]},
        )
        return path

    def _write_measurement(self, record):
        twill_lessons.accept_lesson(self.artifacts, record.id)
        connection = twill_schema.connect(self.state)
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, program, "
            "signature, sig_hash) VALUES (?, ?, ?, 'run_failed', ?, ?, ?)",
            (
                "session-1",
                "2026-09-23T00:00:00+00:00",
                "2026-09-23T00:00:00+00:00",
                "sqlite3",
                "sqlite3: command not found",
                "boundary-hash",
            ),
        )
        connection.commit()
        report = twill_measure.measure_lessons(
            connection,
            self.artifacts,
            now="2026-09-24T12:00:00Z",
            repo_root=ROOT,
        )
        self.assertEqual(len(report.measurements), 1)
        path = twill_measure.measurement_path(
            self.artifacts,
            record.id,
            repo_root=ROOT,
        )
        self.assertIn(
            f"measurements/{record.id}.jsonl",
            {entry["path"] for entry in read_manifest(self.artifacts, repo_root=ROOT)["artifacts"]},
        )
        return path

    def _write_guards(self, record):
        writers = (
            twill_guards.write_environment_guard,
            twill_guards.write_hook_guard,
            twill_guards.write_gate_guard,
            twill_guards.write_wrapper_guard,
            twill_guards.write_skill_guard,
            twill_guards.write_agents_md_guard,
            twill_guards.write_memory_guard,
            twill_guards.write_retrieval_guard,
        )
        paths = []
        for writer in writers:
            path = writer(self.artifacts, record, repo_root=ROOT)
            paths.append(path)
            self.assertIn(
                f"guards/{path.name}",
                {entry["path"] for entry in read_manifest(self.artifacts, repo_root=ROOT)["artifacts"]},
            )
        return tuple(paths)

    def _manifest_timestamp(self, manifest):
        return datetime.fromisoformat(
            manifest["generated_at"].replace("Z", "+00:00")
        )

    def _assert_complete_manifest(self):
        manifest = read_manifest(self.artifacts, repo_root=ROOT)
        schemas = {
            "lessons": "twill-lesson/v1",
            "digests": "twill-digest/v1",
            "measurements": "twill-measurement/v1",
            "guards": "twill-guard/v1",
        }
        expected = {}
        for namespace, schema in schemas.items():
            directory = self.artifacts / namespace
            if not directory.exists():
                continue
            for path in directory.iterdir():
                if not path.is_file():
                    continue
                content = path.read_bytes()
                relative = path.relative_to(self.artifacts).as_posix()
                expected[relative] = {
                    "bytes": len(content),
                    "path": relative,
                    "schema": schema,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }

        self.assertEqual(
            {entry["path"] for entry in manifest["artifacts"]},
            set(expected),
        )
        for entry in manifest["artifacts"]:
            self.assertEqual(entry, expected[entry["path"]])
        timestamp = self._manifest_timestamp(manifest)
        self.assertIsNotNone(timestamp.tzinfo)
        return manifest

    def _assert_failed_writer_restored(self, operation, target):
        previous_manifest = (self.artifacts / "manifest.json").read_bytes()
        with mock.patch(
            "twill_artifacts.write_manifest",
            side_effect=RuntimeError("injected manifest failure"),
        ):
            with self.assertRaises(RuntimeError):
                operation()

        self.assertFalse(target.exists(), target)
        self.assertEqual(
            (self.artifacts / "manifest.json").read_bytes(), previous_manifest
        )
        self._assert_complete_manifest()
        self.assertEqual(
            [
                path
                for path in self.artifacts.rglob("*")
                if path.is_file() and path.name.startswith(".")
            ],
            [],
        )

    def test_every_exported_writer_stays_external_and_private(self):
        record = self._write_lesson(self.artifacts)
        measurement = self._write_measurement(record)
        guards = self._write_guards(record)
        digest = self._write_digest(self.artifacts)

        returned = (
            record.path,
            measurement,
            digest,
            *guards,
        )
        external = self.artifacts.resolve()
        for path in returned:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), path)
                self.assertFalse(path.is_symlink(), path)
                self.assertTrue(path.resolve().is_relative_to(external), path)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)

        self.assertEqual(
            {path.relative_to(external) for path in self.artifacts.rglob("*")},
            {
                Path("lessons"),
                Path("measurements"),
                Path("digests"),
                Path("guards"),
                Path("lessons") / f"{LESSON_ID}.md",
                Path("measurements") / f"{LESSON_ID}.jsonl",
                Path("digests") / f"{WEEK_LABEL}.txt",
                Path("guards") / f"{LESSON_ID}.environment.md",
                Path("guards") / f"{LESSON_ID}.hook.json",
                Path("guards") / f"{LESSON_ID}.gate.txt",
                Path("guards") / f"{LESSON_ID}.wrapper.sh",
                Path("guards") / f"{LESSON_ID}.skill.md",
                Path("guards") / f"{LESSON_ID}.agents.md",
                Path("guards") / f"{LESSON_ID}.memory.md",
                Path("guards") / f"{LESSON_ID}.retrieval.md",
                Path("manifest.json"),
            },
        )
        for path in (self.artifacts, *self.artifacts.rglob("*")):
            self.assertFalse(path.is_symlink(), path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_every_writer_refreshes_complete_manifest_metadata(self):
        write_manifest(
            self.artifacts,
            generated_at="2026-09-27T00:00:00Z",
            repo_root=ROOT,
        )
        previous = self._assert_complete_manifest()

        time.sleep(0.001)
        record = self._write_lesson(self.artifacts)
        current = self._assert_complete_manifest()
        self.assertGreater(
            self._manifest_timestamp(current), self._manifest_timestamp(previous)
        )

        time.sleep(0.001)
        measurement = self._write_measurement(record)
        current = self._assert_complete_manifest()
        self.assertGreater(
            self._manifest_timestamp(current), self._manifest_timestamp(previous)
        )
        self.assertTrue(measurement.is_file())
        previous = current

        for writer in (
            twill_guards.write_environment_guard,
            twill_guards.write_hook_guard,
            twill_guards.write_gate_guard,
            twill_guards.write_wrapper_guard,
            twill_guards.write_skill_guard,
            twill_guards.write_agents_md_guard,
            twill_guards.write_memory_guard,
            twill_guards.write_retrieval_guard,
        ):
            with self.subTest(writer=writer.__name__):
                time.sleep(0.001)
                writer(self.artifacts, record, repo_root=ROOT)
                current = self._assert_complete_manifest()
                self.assertGreater(
                    self._manifest_timestamp(current),
                    self._manifest_timestamp(previous),
                )
                previous = current

        time.sleep(0.001)
        self._write_digest(self.artifacts)
        current = self._assert_complete_manifest()
        self.assertGreater(
            self._manifest_timestamp(current), self._manifest_timestamp(previous)
        )

        self.assertEqual(
            {entry["path"] for entry in current["artifacts"]},
            {
                f"lessons/{LESSON_ID}.md",
                f"measurements/{LESSON_ID}.jsonl",
                f"digests/{WEEK_LABEL}.txt",
                *{
                    f"guards/{LESSON_ID}{suffix}"
                    for suffix in (
                        ".environment.md",
                        ".hook.json",
                        ".gate.txt",
                        ".wrapper.sh",
                        ".skill.md",
                        ".agents.md",
                        ".memory.md",
                        ".retrieval.md",
                    )
                },
            },
        )

    def test_manifest_failure_rolls_back_each_writer_without_stale_publication(self):
        write_manifest(self.artifacts, repo_root=ROOT)
        self._assert_failed_writer_restored(
            lambda: twill_explainer.persist_lesson_drafts(
                (self._draft(),),
                (self._candidate(),),
                TwillConfig(artifacts_root=self.artifacts),
                repo_root=ROOT,
            ),
            self.artifacts / "lessons" / f"{LESSON_ID}.md",
        )
        self._assert_failed_writer_restored(
            lambda: twill_digest.write_digest_file(
                "empty | $ twill digest --week 2026-W38 --stdout "
                "--state-dir /tmp/twill-state\n",
                self.artifacts,
                WEEK,
                repo_root=ROOT,
            ),
            self.artifacts / "digests" / f"{WEEK_LABEL}.txt",
        )

        record = self._write_lesson(self.artifacts)
        self._assert_failed_writer_restored(
            lambda: twill_guards.write_hook_guard(
                self.artifacts,
                record,
                repo_root=ROOT,
            ),
            self.artifacts / "guards" / f"{LESSON_ID}.hook.json",
        )

        twill_lessons.accept_lesson(self.artifacts, record.id, repo_root=ROOT)
        connection = twill_schema.connect(self.state)
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, program, "
            "signature, sig_hash) VALUES (?, ?, ?, 'run_failed', ?, ?, ?)",
            (
                "session-1",
                "2026-09-23T00:00:00+00:00",
                "2026-09-23T00:00:00+00:00",
                "sqlite3",
                "sqlite3: command not found",
                "boundary-hash",
            ),
        )
        connection.commit()
        self._assert_failed_writer_restored(
            lambda: twill_measure.measure_lessons(
                connection,
                self.artifacts,
                now="2026-09-24T12:00:00Z",
                repo_root=ROOT,
            ),
            self.artifacts / "measurements" / f"{LESSON_ID}.jsonl",
        )

    def test_each_writer_rejects_unset_and_in_tree_roots_before_writing(self):
        record = self._write_lesson(self.artifacts)
        digest_text = "empty | $ twill digest --week 2026-W38 --stdout\n"

        def lesson_writer(root):
            return twill_explainer.persist_lesson_drafts(
                (self._draft(),),
                (self._candidate(),),
                TwillConfig(artifacts_root=root),
                repo_root=ROOT,
            )

        def measurement_writer(root):
            connection = twill_schema.connect(self.root / "rejection-state")
            try:
                return twill_measure.measure_lessons(
                    connection,
                    root,
                    repo_root=ROOT,
                )
            finally:
                connection.close()

        def guard_writer(root):
            return twill_guards.write_guard(
                root,
                record,
                target_layer="hook",
                repo_root=ROOT,
            )

        writers = {
            "lessons": lesson_writer,
            "measurements": measurement_writer,
            "guards": guard_writer,
            "digests": lambda root: twill_digest.write_digest_file(
                digest_text,
                root,
                WEEK,
                repo_root=ROOT,
            ),
        }
        for root_name, root in (("unset", None), ("in-tree", self.in_tree_root)):
            for name, writer in writers.items():
                with self.subTest(root=root_name, producer=name):
                    with self.assertRaises(ConfigError):
                        writer(root)
                    self.assertFalse(self.in_tree_root.exists())

        with self.assertRaises(ConfigError):
            TwillConfig(artifacts_root=None).require_artifacts_root(ROOT)


if __name__ == "__main__":
    unittest.main()
