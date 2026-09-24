"""Tests for the operator-gated lesson lifecycle."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_config import TwillConfig  # noqa: E402
from twill_contract import EXIT_VALIDATION_FAILURE, ValidationError  # noqa: E402
from twill_explainer import (  # noqa: E402
    LessonDraft,
    PromptCluster,
    PromptExcerpt,
    write_lesson_files,
)  # noqa: E402
from twill_lessons import (  # noqa: E402
    accept_lesson,
    apply_lesson,
    escalate_lesson,
    list_lessons,
    load_lesson,
    resolve_lesson,
    retire_lesson,
    transition_lesson,
)
from twill_ranker import RankedCluster  # noqa: E402
from twill_lock import StateLock  # noqa: E402


class LessonLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts = self.root / "artifacts"
        self.config = TwillConfig(artifacts_root=self.artifacts)

    def write_draft(self, key="command-not-found:sqlite3"):
        cluster = RankedCluster(
            "D-01",
            key,
            30,
            3,
            7,
            "2026-09-01T00:00:00+00:00",
            "2026-09-24T00:00:00+00:00",
            1.0,
            None,
            "open",
        )
        candidate = PromptCluster(
            cluster,
            (PromptExcerpt(1, "session-a", "safe evidence"),),
        )
        draft = LessonDraft(
            f"D-01:{key}",
            "A command fails repeatedly. Install the command before retrying.",
        )
        return write_lesson_files((draft,), (candidate,), self.config)[0]

    def test_draft_to_accepted_requires_explicit_operator_action(self):
        path = self.write_draft()
        lesson_id = path.stem
        original = path.read_bytes()

        self.assertEqual(load_lesson(path).state, "draft")
        with self.assertRaises(ValidationError) as raised:
            transition_lesson(self.artifacts, lesson_id, "accepted")
        self.assertEqual(raised.exception.code, EXIT_VALIDATION_FAILURE)
        self.assertIn("without an explicit operator command", str(raised.exception))
        self.assertEqual(path.read_bytes(), original)
        with self.assertRaises(ValidationError):
            accept_lesson(self.artifacts, lesson_id, operator=False)

        accepted = accept_lesson(self.artifacts, lesson_id)
        self.assertEqual(accepted.state, "accepted")
        self.assertEqual(load_lesson(path).state, "accepted")

    def test_apply_records_layer_timestamp_and_bead(self):
        path = self.write_draft()
        lesson_id = path.stem
        accept_lesson(self.artifacts, lesson_id)

        applied = apply_lesson(
            self.artifacts,
            lesson_id,
            layer="environment",
            bead="twill-example",
            applied_at="2026-09-24T12:00:00Z",
        )

        self.assertEqual(applied.state, "applied:environment")
        self.assertEqual(applied.routing["recommended"], "environment")
        self.assertIn("installing or repairing", applied.routing["reason"])
        self.assertEqual(applied.routing["applied"], "environment")
        self.assertEqual(applied.routing["applied_at"], "2026-09-24T12:00:00Z")
        self.assertEqual(applied.routing["bead"], "twill-example")
        self.assertIn("state: applied:environment\n", path.read_text())
        self.assertIn("applied: environment", path.read_text())

    def test_each_terminal_state_is_reachable_only_from_applied(self):
        for target, operation in (
            ("resolved", resolve_lesson),
            ("escalated", escalate_lesson),
            ("retired", retire_lesson),
        ):
            with self.subTest(target=target):
                path = self.write_draft(f"command-not-found:{target}")
                lesson_id = path.stem
                with self.assertRaises(ValidationError):
                    operation(self.artifacts, lesson_id)
                accept_lesson(self.artifacts, lesson_id)
                apply_lesson(
                    self.artifacts,
                    lesson_id,
                    layer="hook",
                    bead=f"twill-{target}",
                    applied_at="2026-09-24T12:00:00Z",
                )
                result = operation(self.artifacts, lesson_id)
                self.assertEqual(result.state, target)
                with self.assertRaises(ValidationError):
                    transition_lesson(
                        self.artifacts,
                        lesson_id,
                        "draft",
                        operator=True,
                    )

    def test_generic_transition_refuses_without_operator_even_after_acceptance(self):
        path = self.write_draft()
        lesson_id = path.stem
        accept_lesson(self.artifacts, lesson_id)
        original = path.read_bytes()
        with self.assertRaises(ValidationError):
            transition_lesson(
                self.artifacts,
                lesson_id,
                "applied:hook",
                layer="hook",
                bead="twill-example",
            )
        self.assertEqual(path.read_bytes(), original)

    def test_invalid_apply_metadata_and_missing_backtest_fail_closed(self):
        path = self.write_draft()
        lesson_id = path.stem
        accept_lesson(self.artifacts, lesson_id)
        with self.assertRaises(ValidationError):
            apply_lesson(
                self.artifacts,
                lesson_id,
                layer="not-a-layer",
                bead="twill-example",
            )
        with self.assertRaises(ValidationError):
            apply_lesson(
                self.artifacts,
                lesson_id,
                layer="hook",
                bead="",
            )
        self.assertEqual(load_lesson(path).state, "accepted")

        text = path.read_text()
        path.write_text(text.replace("backtest: {", "missing_backtest: {", 1))
        with self.assertRaises(ValidationError):
            accept_lesson(self.artifacts, lesson_id)

    def test_recommendation_requires_recorded_reasoning(self):
        path = self.write_draft()
        text = path.read_text()
        path.write_text(text.replace('reason: "', 'unrecorded: "', 1))

        with self.assertRaises(ValidationError) as raised:
            load_lesson(path)
        self.assertIn("routing.reason must accompany", str(raised.exception))

    def test_body_and_file_mode_survive_a_transition(self):
        path = self.write_draft()
        body = "\nOperator note.\n"
        path.write_text(path.read_text() + body)
        lesson_id = path.stem
        accept_lesson(self.artifacts, lesson_id)
        self.assertTrue(path.read_text().endswith(body))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_lesson_id_filename_mismatch_and_duplicate_fields_are_rejected(self):
        path = self.write_draft()
        text = path.read_text()
        path.write_text(text.replace("id: ", "id: L-deadbeef\nid: ", 1))
        with self.assertRaises(ValidationError):
            load_lesson(path)

        path = self.write_draft("command-not-found:duplicate")
        text = path.read_text()
        path.write_text(
            text.replace("state: draft\n", "state: draft\nstate: accepted\n", 1)
        )
        with self.assertRaises(ValidationError):
            load_lesson(path)

    def test_listing_filters_applied_category(self):
        first = self.write_draft("command-not-found:one")
        second = self.write_draft("command-not-found:two")
        accept_lesson(self.artifacts, first.stem)
        apply_lesson(
            self.artifacts,
            first.stem,
            layer="memory",
            bead="twill-one",
        )
        self.assertEqual(
            [record.id for record in list_lessons(self.artifacts, state="draft")],
            [second.stem],
        )
        self.assertEqual(
            [record.id for record in list_lessons(self.artifacts, state="applied")],
            [first.stem],
        )

    def test_apply_redacts_operator_input_before_writing(self):
        path = self.write_draft()
        lesson_id = path.stem
        accept_lesson(self.artifacts, lesson_id)
        token = "ghp_" + "1234567890abcdefghijklmnop"
        apply_lesson(self.artifacts, lesson_id, layer="hook", bead=token)
        self.assertNotIn(token, path.read_text())
        self.assertIn("<redacted:github-token>", path.read_text())


class LessonCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.home = Path(cls.temporary.name) / "home"
        config = cls.home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f'artifacts_root = "{Path(cls.temporary.name) / "artifacts"}"\n'
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "twill"), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home)},
            check=False,
            text=True,
            capture_output=True,
        )

    def test_accept_and_apply_cli_use_json_envelopes_and_lockable_verbs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = Path(self.temporary.name) / "artifacts"
            cluster = RankedCluster(
                "D-01",
                "command-not-found:cli",
                30,
                3,
                7,
                "2026-09-01T00:00:00+00:00",
                "2026-09-24T00:00:00+00:00",
                1.0,
                None,
                "open",
            )
            candidate = PromptCluster(
                cluster,
                (PromptExcerpt(1, "session-cli", "safe evidence"),),
            )
            path = write_lesson_files(
                (
                    LessonDraft(
                        "D-01:command-not-found:cli",
                        "A command fails repeatedly. Install the command before retrying.",
                    ),
                ),
                (candidate,),
                TwillConfig(artifacts_root=artifacts),
            )[0]
            lesson_id = path.stem
            state = root / "state"
            with StateLock(state):
                locked = self.run_cli(
                    "accept",
                    lesson_id,
                    "--json",
                    "--state-dir",
                    str(state),
                )
            self.assertEqual(locked.returncode, 3, locked.stderr)
            self.assertEqual(json.loads(locked.stdout)["error"]["code"], 3)
            accepted = self.run_cli(
                "accept",
                lesson_id,
                "--json",
                "--state-dir",
                str(root / "state"),
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            envelope = json.loads(accepted.stdout)
            self.assertEqual(envelope["data"]["lesson"]["state"], "accepted")

            applied = self.run_cli(
                "apply",
                lesson_id,
                "--layer",
                "environment",
                "--bead",
                "twill-cli",
                "--json",
                "--state-dir",
                str(root / "state"),
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(
                json.loads(applied.stdout)["data"]["lesson"]["state"],
                "applied:environment",
            )


if __name__ == "__main__":
    unittest.main()
