"""Scenario 1: recurring friction becomes a measured fix (plan §5/§10.1)."""

import hashlib
import io
import json
import os
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
import twill_lessons  # noqa: E402
import twill_measure  # noqa: E402
import twill_schema  # noqa: E402
from twill_reader import ClaudeCodeLineParser, KIND_RUN  # noqa: E402


PROGRAM = "foo"
KEY = "command-not-found:foo"
APPLIED_AT = "2026-09-01T00:00:00Z"
BASE_FAILURE_AT = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
MEASUREMENT_START = datetime(2026, 9, 2, tzinfo=timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def claude_record(
    session_id: str,
    timestamp: datetime,
    content: object,
    record_type: str,
) -> str:
    return json.dumps(
        {
            "type": record_type,
            "sessionId": session_id,
            "cwd": "/workspace/scenario-1",
            "timestamp": isoformat(timestamp),
            "message": {
                "role": "assistant" if record_type == "assistant" else "user",
                "content": content,
            },
        },
        separators=(",", ":"),
    )


def failed_run_lines(session_id: str, timestamp: datetime, index: int) -> tuple[str, str]:
    tool_id = f"toolu-{session_id}-{index}"
    return (
        claude_record(
            session_id,
            timestamp,
            [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Bash",
                    "input": {"command": f"{PROGRAM} --version"},
                }
            ],
            "assistant",
        ),
        claude_record(
            session_id,
            timestamp + timedelta(seconds=1),
            [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Exit code 127\n/bin/sh: {PROGRAM}: command not found",
                    "is_error": True,
                }
            ],
            "user",
        ),
    )


class Scenario1LifecycleTests(unittest.TestCase):
    """Exercise the public pipeline and the operator-owned lifecycle together."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.source = self.home / ".claude" / "projects" / "scenario-1"
        self.source.mkdir(parents=True)
        self.artifacts = self.root / "artifacts"
        self.state = self.root / "state"
        config = self.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f'artifacts_root = "{self.artifacts}"\nretention = "3650d"\n',
            encoding="utf-8",
        )

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args, "--state-dir", str(self.state)],
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home)},
            check=False,
            text=True,
            capture_output=True,
        )

    def run_json_cli(self, *args: str) -> dict[str, object]:
        result = self.run_cli(*args, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

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

    def write_sessions(self, *, flat_series: bool) -> tuple[Path, ...]:
        paths = []
        for index in range(30):
            session_id = f"scenario-1-{index + 1:02d}"
            timestamps = [BASE_FAILURE_AT]
            if flat_series and index < 2:
                timestamps.extend(
                    BASE_FAILURE_AT + timedelta(days=2 + day)
                    for day in range(28)
                )
            lines = [
                line
                for event_index, timestamp in enumerate(timestamps)
                for line in failed_run_lines(session_id, timestamp, event_index)
            ]
            path = self.source / f"{session_id}.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            paths.append(path)
        return tuple(paths)

    def seed_normalized_detector_rows(self, paths: tuple[Path, ...]) -> None:
        rows = []
        for path in paths:
            parser = ClaudeCodeLineParser()
            events = []
            for source_line, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                events.extend(parser.parse_line(line, source_line))
            events.extend(parser.finish())
            self.assertTrue(events, path)
            self.assertTrue(all(event.kind == KIND_RUN for event in events))
            for event in events:
                self.assertIsNotNone(event.error_excerpt)
                assert event.error_excerpt is not None
                rows.append(
                    (
                        event.session_id,
                        event.timestamp,
                        event.timestamp,
                        PROGRAM,
                        event.command,
                        event.error_excerpt,
                        hashlib.sha256(event.error_excerpt.encode()).hexdigest()[:12],
                        event.error_excerpt,
                        event.cwd,
                    )
                )

        connection = twill_schema.connect(self.state)
        try:
            connection.executemany(
                "INSERT INTO observation(session_id, ts_utc, ts_local, kind, "
                "program, command, signature, sig_hash, excerpt, cwd) "
                "VALUES (?, ?, ?, 'run_failed', ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()
        finally:
            connection.close()

    def drive_to_draft(self, *, flat_series: bool) -> str:
        paths = self.write_sessions(flat_series=flat_series)
        ingest = self.run_cli("ingest", "--settle", "0", "--limit", "100", "--json")
        self.assertEqual(ingest.returncode, 0, ingest.stderr)
        self.seed_normalized_detector_rows(paths)

        detected = self.run_json_cli(
            "detect", "--detector", "D-01", "--window", "3650d"
        )
        self.assertEqual(detected["data"]["detectors"][0]["clusters"], 1)

        ranked = self.run_json_cli("rank", "--top", "1")
        cluster = ranked["data"]["clusters"][0]
        self.assertEqual(cluster["detector_id"], "D-01")
        self.assertEqual(cluster["key"], KEY)
        self.assertEqual(cluster["sessions"], 30)
        self.assertEqual(cluster["state"], "open")

        digest = self.run_json_cli("digest", "--week", "2026-W35")
        finding = next(
            item
            for item in digest["data"]["findings"]
            if item["detector"] == "D-01@1" and item["key"] == KEY
        )
        self.assertEqual(finding["sessions"], 30)

        model_output = json.dumps(
            {
                "lessons": [
                    {
                        "cluster_id": f"D-01:{KEY}",
                        "summary": (
                            "The missing foo command causes recurring failures. "
                            "Install foo before retrying the operation."
                        ),
                    }
                ]
            },
            separators=(",", ":"),
        )
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), mock.patch(
            "sys.stdout", stdout
        ), mock.patch.object(
            twill_explainer, "invoke_claude", return_value=model_output
        ) as invoke:
            code = twill_app.main(
                [
                    "explain",
                    "--top",
                    "1",
                    "--json",
                    "--state-dir",
                    str(self.state),
                ]
            )
        self.assertEqual(code, 0, stdout.getvalue())
        invoke.assert_called_once()
        draft_result = json.loads(stdout.getvalue())
        self.assertEqual(draft_result["data"]["drafts"], 1)

        lesson_paths = sorted((self.artifacts / "lessons").glob("L-*.md"))
        self.assertEqual(len(lesson_paths), 1)
        lesson_id = lesson_paths[0].stem
        draft = twill_lessons.load_lesson(self.artifacts, lesson_id)
        self.assertEqual(draft.state, "draft")
        self.assertEqual(draft.detector, "D-01")
        self.assertGreaterEqual(len(draft.evidence["session_ids"]), 1)
        self.assertEqual(draft.evidence["sessions"], 30)

        return lesson_id

    def apply_lesson_via_operator(self, lesson_id: str) -> None:
        accepted = self.run_main_json("accept", lesson_id)
        self.assertEqual(accepted["data"]["lesson"]["state"], "accepted")
        with mock.patch.object(twill_lessons, "_utc_now", return_value=APPLIED_AT):
            applied = self.run_main_json(
                "apply", lesson_id, "--layer", "environment", "--bead", "twill-scenario1"
            )
        self.assertEqual(
            applied["data"]["lesson"]["state"], "applied:environment"
        )
        self.assertEqual(
            applied["data"]["lesson"]["routing"]["applied_at"], APPLIED_AT
        )

    def measure_days(self, lesson_id: str, count: int = 27) -> tuple[twill_measure.Measurement, ...]:
        connection = twill_schema.connect(self.state)
        try:
            for offset in range(count):
                twill_measure.measure_lessons(
                    connection,
                    self.artifacts,
                    lesson_id=lesson_id,
                    now=isoformat(MEASUREMENT_START + timedelta(days=offset)),
                )
        finally:
            connection.close()
        return twill_measure.read_measurements(self.artifacts, lesson_id)

    def test_scenario1_resolves_after_a_post_application_zero_series(self):
        lesson_id = self.drive_to_draft(flat_series=False)
        self.apply_lesson_via_operator(lesson_id)

        points = self.measure_days(lesson_id)
        self.assertEqual(len(points), 26)
        self.assertEqual((points[0].sessions, points[0].events), (30, 30))
        self.assertTrue(any(point.sessions == 0 and point.events == 0 for point in points[1:]))
        self.assertEqual(
            twill_lessons.load_lesson(self.artifacts, lesson_id).state,
            "resolved",
        )
        resolved = self.run_json_cli("lessons", "--state", "resolved")
        self.assertEqual(
            [item["id"] for item in resolved["data"]["lessons"]], [lesson_id]
        )
        self.assertEqual(
            twill_measure.resolution_candidates(
                self.artifacts, as_of=points[-1].measured_at
            ),
            (),
        )

    def test_scenario1_flat_post_application_counts_never_resolve(self):
        lesson_id = self.drive_to_draft(flat_series=True)
        self.apply_lesson_via_operator(lesson_id)

        points = self.measure_days(lesson_id)
        self.assertEqual(len(points), 27)
        self.assertTrue(all(point.sessions > 0 and point.events > 0 for point in points))
        record = twill_lessons.load_lesson(self.artifacts, lesson_id)
        self.assertEqual(record.state, "applied:environment")
        self.assertNotEqual(
            record.state,
            "resolved",
            "a flat count series after applied_at must not resolve the lesson",
        )
        self.assertEqual(
            twill_measure.resolution_candidates(
                self.artifacts, as_of=points[-1].measured_at
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
