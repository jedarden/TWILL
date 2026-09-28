"""End-to-end proof that the pipeline retains derived fields, not raw JSONL."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_explainer  # noqa: E402


EVENT_AT = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
WEEK = "2026-W39"
PROGRAM = "raw-retention-missing-tool"
RAW_MARKER = "raw-jsonl-record-marker-7f2d"
RAW_PAYLOAD = "unbounded-raw-payload-" + ("x" * 8192)
FENCED_VALUE = "raw-retention-fenced-value-9a3b"


def _record(session_id: str, timestamp: datetime, content: object, record_type: str) -> str:
    """Build a record whose unknown fields must never cross the ingest boundary."""

    return json.dumps(
        {
            "type": record_type,
            "sessionId": session_id,
            "cwd": "/workspace/raw-retention",
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "opaque_raw_record": {
                "marker": RAW_MARKER,
                "payload": RAW_PAYLOAD,
                "fenced_value": FENCED_VALUE,
            },
            "message": {
                "role": "assistant" if record_type == "assistant" else "user",
                "content": content,
            },
        },
        separators=(",", ":"),
    )


class RawTranscriptRetentionEndToEndTests(unittest.TestCase):
    """Exercise every lifecycle stage and inspect all durable TWILL outputs."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.source = self.home / ".claude" / "projects" / "raw-retention"
        self.source.mkdir(parents=True)
        self.artifacts = self.root / "artifacts"
        self.state = self.root / "state"

        config = self.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f'artifacts_root = "{self.artifacts}"\n'
            f'source_globs = ["{self.source / "*.jsonl"}"]\n'
            f'content_fences = ["{FENCED_VALUE}"]\n'
            'retention = "3650d"\n',
            encoding="utf-8",
        )

        self.raw_lines: list[bytes] = []
        for index in range(2):
            session_id = f"raw-retention-{index + 1}"
            tool_id = f"toolu-{index + 1}"
            assistant = _record(
                session_id,
                EVENT_AT,
                [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Bash",
                        "input": {"command": f"{PROGRAM} --version"},
                        "opaque_tool_payload": RAW_PAYLOAD,
                    }
                ],
                "assistant",
            )
            result = _record(
                session_id,
                EVENT_AT + timedelta(seconds=1),
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": (
                            f"Exit code 127\n{PROGRAM}: command not found\n"
                            f"Redaction-Fence: {FENCED_VALUE}"
                        ),
                        "is_error": True,
                    }
                ],
                "user",
            )
            path = self.source / f"{session_id}.jsonl"
            path.write_text(f"{assistant}\n{result}\n", encoding="utf-8")
            settled = (EVENT_AT - timedelta(hours=3)).timestamp()
            os.utime(path, (settled, settled))
            self.raw_lines.extend((assistant.encode(), result.encode()))

        self.raw_jsonl = b"\n".join(self.raw_lines)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args, "--state-dir", str(self.state)],
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home)},
            check=False,
            text=True,
            capture_output=True,
        )

    def run_main_json(self, *args: str) -> dict[str, object]:
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), mock.patch(
            "sys.stdout", stdout
        ):
            code = twill_app.main(
                [*args, "--json", "--state-dir", str(self.state)]
            )
        self.assertEqual(code, 0, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def seed_detector_contract_rows(self) -> None:
        """Seed Phase 2's normalized D-01 rows after real ingest.

        The current ingest slice persists the generic D-00 observation stream;
        detector-specific columns are the normalized contract used by the
        versioned registry.  This mirrors the existing Scenario 1 lifecycle
        test without ever inserting transcript records or raw payloads.
        """

        connection = sqlite3.connect(self.state / "twill.db")
        try:
            session_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT session_id FROM session ORDER BY session_id"
                )
            ]
            self.assertEqual(session_ids, ["raw-retention-1", "raw-retention-2"])
            excerpt = (
                f"{PROGRAM}: command not found\nRedaction-Fence: {FENCED_VALUE}"
            )
            safe_excerpt = twill_app.redact(excerpt, [FENCED_VALUE])
            normalized = twill_app.signature(safe_excerpt)
            connection.executemany(
                "INSERT INTO observation(session_id, ts_utc, ts_local, kind, "
                "program, command, signature, sig_hash, excerpt, cwd) "
                "VALUES (?, ?, ?, 'run_failed', ?, ?, ?, ?, ?, ?)",
                [
                    (
                        session_id,
                        EVENT_AT.isoformat(),
                        EVENT_AT.isoformat(),
                        PROGRAM,
                        f"{PROGRAM} --version",
                        normalized,
                        twill_app.h12(normalized),
                        safe_excerpt,
                        "/workspace/raw-retention",
                    )
                    for session_id in session_ids
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def durable_files(self) -> tuple[Path, ...]:
        roots = (self.state, self.artifacts)
        return tuple(
            path
            for root in roots
            if root.exists()
            for path in root.rglob("*")
            if path.is_file()
        )

    def durable_bytes(self) -> bytes:
        return b"\n".join(path.read_bytes() for path in self.durable_files())

    def assert_no_raw_record_retained(self, prompt: str = "") -> None:
        durable = self.durable_bytes()
        for name, value in (
            ("raw JSONL records", self.raw_jsonl),
            ("opaque marker", RAW_MARKER.encode()),
            ("unbounded payload", RAW_PAYLOAD.encode()),
            ("fenced value", FENCED_VALUE.encode()),
        ):
            self.assertNotIn(value, durable, name)
        if prompt:
            self.assertNotIn(self.raw_jsonl, prompt.encode())
            self.assertNotIn(RAW_MARKER, prompt)
            self.assertNotIn(RAW_PAYLOAD, prompt)
            self.assertNotIn(FENCED_VALUE, prompt)

    def assert_bounded_transcript_fields(self) -> None:
        connection = sqlite3.connect(self.state / "twill.db")
        try:
            for table, column in (
                ("session", "source_path"),
                ("transcript_event", "text"),
                ("observation", "program"),
                ("observation", "command"),
                ("observation", "signature"),
                ("observation", "excerpt"),
                ("observation", "path"),
                ("observation", "cwd"),
                ("cluster", "key"),
            ):
                largest = connection.execute(
                    f'SELECT COALESCE(MAX(length("{column}")), 0) FROM "{table}"'
                ).fetchone()[0]
                self.assertLessEqual(
                    largest,
                    240,
                    f"{table}.{column} retained an unbounded transcript field",
                )
            excerpts = [
                row[0]
                for row in connection.execute(
                    "SELECT excerpt FROM observation WHERE excerpt IS NOT NULL"
                )
            ]
        finally:
            connection.close()
        self.assertTrue(any("<redacted:content-fence>" in excerpt for excerpt in excerpts))

    def test_full_lifecycle_never_retains_raw_jsonl_or_unbounded_payload(self):
        ingest = self.run_cli("ingest", "--settle", "0", "--limit", "10", "--json")
        self.assertEqual(ingest.returncode, 0, ingest.stderr)
        self.assertEqual(json.loads(ingest.stdout)["data"]["sessions"], 2)
        self.assert_no_raw_record_retained()

        self.seed_detector_contract_rows()

        detect = self.run_cli(
            "detect", "--detector", "D-01", "--window", "3650d", "--json"
        )
        self.assertEqual(detect.returncode, 0, detect.stderr)
        detector = json.loads(detect.stdout)["data"]["detectors"][0]
        self.assertEqual(detector["clusters"], 1)

        rank = self.run_cli("rank", "--top", "1", "--json")
        self.assertEqual(rank.returncode, 0, rank.stderr)
        self.assertEqual(
            json.loads(rank.stdout)["data"]["clusters"][0]["key"],
            f"command-not-found:{PROGRAM}",
        )

        digest = self.run_cli("digest", "--week", WEEK)
        self.assertEqual(digest.returncode, 0, digest.stderr)
        digest_path = self.artifacts / "digests" / f"{WEEK}.txt"
        self.assertTrue(digest_path.is_file())

        prompt_calls: list[str] = []
        model_output = json.dumps(
            {
                "lessons": [
                    {
                        "cluster_id": f"D-01:command-not-found:{PROGRAM}",
                        "summary": (
                            "A missing command caused the normalized failure cluster. "
                            "Install the command before retrying."
                        ),
                    }
                ]
            },
            separators=(",", ":"),
        )

        def fake_claude(prompt: str, *, model: str) -> str:
            prompt_calls.append(prompt)
            return model_output

        with mock.patch.object(twill_explainer, "invoke_claude", side_effect=fake_claude):
            explain = self.run_main_json("explain", "--top", "1")
        self.assertEqual(explain["data"]["drafts"], 1)
        self.assertEqual(len(prompt_calls), 1)
        self.assertLessEqual(
            len(prompt_calls[0].encode()), twill_explainer.MAX_TOTAL_PROMPT_BYTES
        )
        self.assertIn("<redacted:content-fence>", prompt_calls[0])
        self.assert_no_raw_record_retained(prompt_calls[0])

        lesson_paths = sorted((self.artifacts / "lessons").glob("L-*.md"))
        self.assertEqual(len(lesson_paths), 1)
        lesson_id = lesson_paths[0].stem
        accepted = self.run_cli("accept", lesson_id, "--json")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        measure = self.run_cli("measure", "--json")
        self.assertEqual(measure.returncode, 0, measure.stderr)
        measurements = json.loads(measure.stdout)["data"]["measurements"]
        self.assertEqual(len(measurements), 1)
        self.assertEqual(measurements[0]["events"], 2)

        self.assert_bounded_transcript_fields()
        self.assert_no_raw_record_retained(prompt_calls[0])


if __name__ == "__main__":
    unittest.main()
