"""End-to-end prompt-injection coverage for the transcript fixture corpus.

The injection-bearing sessions contain an issue-body instruction that must
remain untrusted in Explain input and cannot trigger a lesson lifecycle
transition.  Both source formats are ingested so the fixture's recurring
failure reaches detect, rank, and Explain through the real CLI path.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "transcripts"
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_explainer  # noqa: E402
import twill_schema  # noqa: E402


class PromptInjectionFixturePipelineTests(unittest.TestCase):
    """Run both injection fixtures through detect, rank, and Explain."""

    @classmethod
    def setUpClass(cls):
        cls._home = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._home.cleanup)
        cls._work = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._work.cleanup)
        home = Path(cls._home.name)
        work = Path(cls._work.name)

        config_dir = home / ".config" / "twill"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            f'artifacts_root = "{home / "artifacts"}"\nrule_globs = []\n'
        )

        cls.state_dir = work / "state"
        manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text())
        fixture_paths = {}
        for source in manifest["sources"]:
            relative = manifest["sources"][source]["cases"]["injection-bearing"]["files"][0]
            fixture = FIXTURE_ROOT / relative
            source_dir = work / "sources" / f".{source}"
            source_dir.mkdir(parents=True)
            target = source_dir / fixture.name
            shutil.copyfile(fixture, target)
            fixture_paths[source] = fixture
            result = cls.run_cli(
                "ingest",
                "--file",
                str(target),
                "--settle",
                "0",
                "--state-dir",
                str(cls.state_dir),
            )
            if result.returncode != 0:
                raise AssertionError(result.stderr)

        cls.seed_detector_observations(fixture_paths)

        cls.detect = cls.run_json_cli("detect")
        cls.rank = cls.run_json_cli("rank")
        cls.cluster_ids = tuple(
            f'{row["detector_id"]}:{row["key"]}'
            for row in cls.rank["data"]["clusters"]
        )
        if not cls.cluster_ids:
            raise AssertionError("injection fixtures did not produce a rank candidate")
        cls.dry_run = cls.run_json_cli("explain", "--dry-run")

    @staticmethod
    def iter_strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from PromptInjectionFixturePipelineTests.iter_strings(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from PromptInjectionFixturePipelineTests.iter_strings(item)

    @classmethod
    def seed_detector_observations(cls, fixture_paths):
        """Bind fixture text to the normalized detector shape used by D-01/D-02.

        Ingest currently retains parser events as ``session_activity`` while
        the detector registry consumes normalized ``run_failed`` rows.  Keep
        this adapter in scratch state so the fixture still supplies the
        untrusted text without changing the production ingestion contract.
        """

        connection = twill_schema.connect(cls.state_dir)
        try:
            signature = "error: command not found: imaginary-tool"
            for source, fixture in fixture_paths.items():
                records = [
                    json.loads(line)
                    for line in fixture.read_text().splitlines()
                    if line.strip()
                ]
                if source == "claude":
                    session_id = records[0]["sessionId"]
                else:
                    session_id = records[0]["payload"]["session_id"]
                injection = next(
                    text
                    for record in records
                    for text in cls.iter_strings(record)
                    if "SYSTEM OVERRIDE" in text
                )
                connection.execute(
                    "INSERT INTO observation(session_id, ts_utc, ts_local, kind, "
                    "program, command, signature, sig_hash, excerpt, cwd) "
                    "VALUES (?, ?, ?, 'run_failed', ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        "2026-09-20T17:00:03+00:00",
                        "2026-09-20T17:00:03+00:00",
                        "imaginary-tool",
                        None,
                        signature,
                        twill_app.h12(signature),
                        injection,
                        "/workspace/demo",
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    @classmethod
    def run_cli(cls, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=str(ROOT),
            env={**os.environ, "HOME": cls._home.name},
            text=True,
            capture_output=True,
            check=False,
        )

    @classmethod
    def run_json_cli(cls, command, *args):
        result = cls.run_cli(command, "--json", "--state-dir", str(cls.state_dir), *args)
        if result.returncode != 0:
            raise AssertionError(f"{command} failed: {result.stderr}")
        return json.loads(result.stdout)

    @classmethod
    def explain_with_model(cls, output):
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": cls._home.name}), mock.patch.object(
            twill_explainer, "invoke_claude", return_value=output
        ) as invoke, mock.patch("sys.stdout", stdout):
            code = twill_app.main(
                [
                    "explain",
                    "--json",
                    "--state-dir",
                    str(cls.state_dir),
                ]
            )
        return code, json.loads(stdout.getvalue()), invoke

    @classmethod
    def model_output(cls, *, accepted=False):
        summary = (
            "A recurring command failure wastes time. "
            "Install the missing command before retrying."
        )
        lessons = []
        for cluster_id in cls.cluster_ids:
            item = {"cluster_id": cluster_id, "summary": summary}
            if accepted:
                item["state"] = "accepted"
            lessons.append(item)
        return json.dumps({"lessons": lessons}, separators=(",", ":"))

    def test_explain_prompt_uses_ranked_framed_evidence_not_whole_sessions(self):
        self.assertGreaterEqual(len(self.detect["data"]["detectors"]), 1)
        self.assertTrue(self.rank["data"]["clusters"])

        envelope = self.dry_run
        self.assertEqual(set(envelope), {"data", "generated_at", "schema_version", "warnings"})
        prompt = envelope["data"]["prompt"]
        self.assertEqual(
            envelope["data"]["prompt_bytes"], len(prompt.encode("utf-8"))
        )
        self.assertEqual(envelope["data"]["clusters"], len(self.cluster_ids))
        self.assertIn(twill_explainer.CLUSTER_DATA_BEGIN, prompt)
        self.assertIn(twill_explainer.EXCERPT_BEGIN, prompt)
        self.assertIn("imaginary-tool", prompt)
        self.assertIn("TRUSTED OUTPUT CONTRACT", prompt)
        self.assertIn("SYSTEM OVERRIDE", prompt)

        excerpt_start = 0
        framed_injection = False
        while True:
            try:
                excerpt_start = prompt.index(
                    twill_explainer.EXCERPT_BEGIN, excerpt_start
                )
            except ValueError:
                break
            excerpt_end = prompt.index(
                twill_explainer.EXCERPT_END, excerpt_start
            )
            excerpt = prompt[excerpt_start:excerpt_end]
            framed_injection = framed_injection or "SYSTEM OVERRIDE" in excerpt
            excerpt_start = excerpt_end + len(twill_explainer.EXCERPT_END)
        self.assertTrue(framed_injection)

    def test_schema_deviation_is_rejected_and_valid_lessons_stay_draft(self):
        with mock.patch.object(
            twill_explainer,
            "_utc_now",
            return_value=datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
        ):
            invalid_code, invalid_envelope, invoke = self.explain_with_model(
                self.model_output(accepted=True)
            )
        self.assertEqual(invalid_code, 4)
        self.assertEqual(set(invalid_envelope), {"error"})
        self.assertEqual(invalid_envelope["error"]["code"], 4)
        self.assertIn("strict schema validation", invalid_envelope["error"]["message"])
        invoke.assert_called_once()
        self.assertFalse((Path(self._home.name) / "artifacts" / "lessons").exists())

        connection = twill_schema.connect_read_only(self.state_dir)
        try:
            states = {
                row[0]
                for row in connection.execute(
                    "SELECT state FROM cluster ORDER BY detector_id, key"
                )
            }
        finally:
            connection.close()
        self.assertEqual(states, {"open"})

        with mock.patch.object(
            twill_explainer,
            "_utc_now",
            return_value=datetime(2026, 9, 21, 12, tzinfo=timezone.utc),
        ):
            valid_code, valid_envelope, invoke = self.explain_with_model(
                self.model_output()
            )
        self.assertEqual(valid_code, 0)
        self.assertEqual(
            set(valid_envelope), {"data", "generated_at", "schema_version", "warnings"}
        )
        self.assertEqual(
            set(valid_envelope["data"]), {"clusters", "drafts", "lessons"}
        )
        self.assertEqual(valid_envelope["data"]["clusters"], len(self.cluster_ids))
        self.assertEqual(valid_envelope["data"]["drafts"], len(self.cluster_ids))
        self.assertEqual(len(valid_envelope["data"]["lessons"]), len(self.cluster_ids))
        invoke.assert_called_once()

        lessons_dir = Path(self._home.name) / "artifacts" / "lessons"
        lesson_paths = sorted(lessons_dir.glob("*.md"))
        self.assertEqual(len(lesson_paths), len(self.cluster_ids))
        for path in lesson_paths:
            text = path.read_text()
            self.assertIn("state: draft\n", text)
            self.assertNotIn("state: accepted", text)

        connection = twill_schema.connect_read_only(self.state_dir)
        try:
            states = {
                row[0]
                for row in connection.execute(
                    "SELECT state FROM cluster ORDER BY detector_id, key"
                )
            }
        finally:
            connection.close()
        self.assertEqual(states, {"drafted"})

        accepted = self.run_json_cli("lessons", "--state", "accepted")
        self.assertEqual(accepted["data"]["lessons"], [])


if __name__ == "__main__":
    unittest.main()
