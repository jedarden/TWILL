"""Prove Explain's dry-run prompt is bounded to selected cluster evidence."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "explain"
FIXTURES = (
    FIXTURE_ROOT / "raw-session-boundary.jsonl",
    FIXTURE_ROOT / "raw-session-boundary-unrelated.jsonl",
)
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_explainer  # noqa: E402
import twill_schema  # noqa: E402


APPROVED_SESSION = "explain-boundary-approved"
UNRELATED_SESSION = "explain-boundary-unrelated"
UNRELATED_CONTENT = (
    "UNRELATED_SESSION_CONTENT_9d52 must not enter the boundary-tool cluster prompt."
)
PROGRAM = "boundary-tool"
SIGNATURE = "boundary-tool: command not found"
FENCE = "boundary-fence-value"
APPROVED_RAW_EXCERPT = f"{SIGNATURE}; context={FENCE}"
RAW_MARKERS = (
    "RAW_SESSION_ONLY_MARKER_7f31",
    "RAW_ASSISTANT_ONLY_MARKER_2c84",
    "/home/coding/agent-transcript-archive/sessions/raw-boundary.jsonl",
    UNRELATED_SESSION,
    "UNRELATED_SESSION_CONTENT_9d52",
)


class ExplainRawSessionBoundaryTests(unittest.TestCase):
    """The dry-run prompt contains selected redacted evidence, not raw sessions."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.home = root / "home"
        self.source = self.home / ".claude" / "projects" / "explain-boundary"
        self.source.mkdir(parents=True)
        self.state = root / "state"
        self.artifacts = root / "artifacts"
        config = self.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f'artifacts_root = "{self.artifacts}"\n'
            f'content_fences = ["{FENCE}"]\n',
            encoding="utf-8",
        )
        self.fixtures = tuple(self.source / fixture.name for fixture in FIXTURES)
        for source, fixture in zip(self.fixtures, FIXTURES):
            shutil.copyfile(fixture, source)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home)},
            text=True,
            capture_output=True,
            check=False,
        )

    def seed_cluster_evidence(self) -> None:
        connection = twill_schema.connect(self.state)
        try:
            safe_excerpt = twill_app.redact(APPROVED_RAW_EXCERPT, [FENCE])
            connection.execute(
                "INSERT INTO observation(session_id, ts_utc, ts_local, kind, "
                "program, signature, sig_hash, excerpt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    APPROVED_SESSION,
                    "2026-09-27T12:00:01+00:00",
                    "2026-09-27T12:00:01+00:00",
                    "run_failed",
                    PROGRAM,
                    SIGNATURE,
                    twill_app.h12(SIGNATURE),
                    safe_excerpt,
                ),
            )
            connection.execute(
                "INSERT INTO observation(session_id, ts_utc, ts_local, kind, "
                "program, signature, sig_hash, excerpt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    UNRELATED_SESSION,
                    "2026-09-27T12:00:03+00:00",
                    "2026-09-27T12:00:03+00:00",
                    "run_failed",
                    "unrelated-tool",
                    "unrelated-tool: command not found",
                    twill_app.h12("unrelated-tool: command not found"),
                    UNRELATED_CONTENT,
                ),
            )
            connection.execute(
                "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
                "first_seen, last_seen, score, covered_by, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "D-01",
                    f"command-not-found:{PROGRAM}",
                    30,
                    1,
                    1,
                    "2026-09-27T12:00:01+00:00",
                    "2026-09-27T12:00:01+00:00",
                    1.0,
                    None,
                    "open",
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def test_dry_run_prompt_contains_only_redacted_cluster_evidence(self):
        for fixture in self.fixtures:
            ingest = self.run_cli(
                "ingest",
                "--file",
                str(fixture),
                "--settle",
                "0",
                "--state-dir",
                str(self.state),
            )
            self.assertEqual(ingest.returncode, 0, ingest.stderr)
        self.seed_cluster_evidence()

        dry_run = self.run_cli(
            "explain",
            "--dry-run",
            "--json",
            "--top",
            "1",
            "--state-dir",
            str(self.state),
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        envelope = json.loads(dry_run.stdout)
        prompt = envelope["data"]["prompt"]

        self.assertEqual(envelope["data"]["clusters"], 1)
        self.assertEqual(envelope["data"]["prompt_bytes"], len(prompt.encode("utf-8")))
        self.assertLessEqual(
            envelope["data"]["prompt_bytes"], twill_explainer.MAX_TOTAL_PROMPT_BYTES
        )
        self.assertIn('"cluster_id":"D-01:command-not-found:boundary-tool"', prompt)
        self.assertIn('"session_id":"explain-boundary-approved"', prompt)
        self.assertIn(SIGNATURE, prompt)
        self.assertIn("<redacted:content-fence>", prompt)
        self.assertNotIn(FENCE, prompt)
        self.assertEqual(prompt.count(twill_explainer.EXCERPT_BEGIN), 1)

        for marker in RAW_MARKERS:
            self.assertNotIn(marker, prompt)
        raw_transcript = "\n".join(
            fixture.read_text(encoding="utf-8") for fixture in FIXTURES
        )
        self.assertNotIn(raw_transcript, prompt)

        connection = sqlite3.connect(self.state / "twill.db")
        try:
            unrelated = connection.execute(
                "SELECT excerpt FROM observation WHERE session_id = ?",
                (UNRELATED_SESSION,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(unrelated, UNRELATED_CONTENT)
        self.assertNotIn(unrelated, prompt)


if __name__ == "__main__":
    unittest.main()
