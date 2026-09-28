import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

from twill_app import settled_files  # noqa: E402


class ConfiguredGlobTests(unittest.TestCase):
    def test_patterns_are_expanded_exactly_and_overlaps_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = root / ".claude" / "projects"
            codex = root / ".codex" / "sessions"
            direct = claude / "project-a" / "direct.jsonl"
            nested = claude / "project-a" / "nested" / "too-deep.jsonl"
            codex_file = codex / "2026" / "09" / "rollout.jsonl"
            for path in (direct, nested, codex_file):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n")
            (claude / "project-a" / "not-a-transcript.txt").write_text("{}\n")

            paths = settled_files(
                (
                    str(claude / "*" / "*.jsonl"),
                    str(claude / "project-a" / "*.jsonl"),
                    str(codex / "**" / "*.jsonl"),
                ),
                0,
            )

            self.assertEqual(set(paths), {direct.resolve(), codex_file.resolve()})
            self.assertEqual(len(paths), 2)
            self.assertEqual(len(paths), len(set(paths)))
            self.assertNotIn(nested.resolve(), paths)

    def test_legacy_directory_sources_still_walk_recursively(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "one" / "two" / "session.jsonl"
            nested.parent.mkdir(parents=True)
            nested.write_text("{}\n")
            (root / "one" / "two" / "notes.txt").write_text("{}\n")

            self.assertEqual(settled_files((root,), 0), [nested.resolve()])


class ConfiguredGlobIngestTests(unittest.TestCase):
    def test_matching_claude_and_codex_files_feed_the_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            claude_file = root / ".claude" / "projects" / "project-a" / "session.jsonl"
            codex_file = root / ".codex" / "sessions" / "2026" / "rollout.jsonl"
            claude_file.parent.mkdir(parents=True)
            codex_file.parent.mkdir(parents=True)
            claude_file.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "claude-enumerated",
                        "message": {"role": "user", "content": "claude turn"},
                    }
                )
                + "\n"
            )
            codex_file.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "session_id": "codex-enumerated",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "codex turn"}],
                        },
                    }
                )
                + "\n"
            )
            # This file would be picked up by the old literal-prefix rglob,
            # but it does not match the configured one-directory Claude glob.
            excluded = root / ".claude" / "projects" / "project-a" / "nested" / "excluded.jsonl"
            excluded.parent.mkdir()
            excluded.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "must-not-ingest",
                        "message": {"role": "user", "content": "excluded"},
                    }
                )
                + "\n"
            )

            config_dir = home / ".config" / "twill"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                "\n".join(
                    [
                        'settle_window = "0"',
                        f'source_globs = ["{root / ".claude" / "projects" / "*" / "*.jsonl"}", "{root / ".codex" / "sessions" / "**" / "*.jsonl"}"]',
                        f'artifacts_root = "{root / "artifacts"}"',
                    ]
                )
                + "\n"
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "ingest",
                    "--json",
                    "--limit",
                    "10",
                    "--state-dir",
                    str(root / "state"),
                ],
                cwd=ROOT,
                env={**os.environ, "HOME": str(home)},
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["data"]["sessions"], 2)

            connection = sqlite3.connect(root / "state" / "twill.db")
            try:
                rows = connection.execute(
                    "SELECT path, source FROM cursor ORDER BY path"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(
                rows,
                [
                    (str(claude_file.resolve()), "claude"),
                    (str(codex_file.resolve()), "codex"),
                ],
            )

    def test_narrowed_glob_does_not_hide_a_missing_stored_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            projects = root / ".claude" / "projects"
            kept = projects / "kept" / "session.jsonl"
            vanished = projects / "retired" / "session.jsonl"
            for path, session_id, text in (
                (kept, "glob-kept", "kept evidence"),
                (vanished, "glob-retired", "retired evidence"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "type": "user",
                            "sessionId": session_id,
                            "message": {"role": "user", "content": text},
                        }
                    )
                    + "\n"
                )

            config_dir = home / ".config" / "twill"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"

            def configure(pattern: Path) -> None:
                config_path.write_text(
                    "\n".join(
                        [
                            'settle_window = "0"',
                            f'source_globs = ["{pattern}"]',
                            f'artifacts_root = "{root / "artifacts"}"',
                        ]
                    )
                    + "\n"
                )

            configure(projects / "*" / "*.jsonl")
            state = root / "state"
            first = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "ingest",
                    "--limit",
                    "10",
                    "--state-dir",
                    str(state),
                ],
                cwd=ROOT,
                env={**os.environ, "HOME": str(home)},
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            connection = sqlite3.connect(state / "twill.db")
            try:
                offsets = dict(
                    connection.execute(
                        "SELECT path, last_offset FROM cursor ORDER BY path"
                    ).fetchall()
                )
            finally:
                connection.close()
            vanished.unlink()

            configure(projects / "kept" / "*.jsonl")
            second = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "ingest",
                    "--limit",
                    "10",
                    "--state-dir",
                    str(state),
                ],
                cwd=ROOT,
                env={**os.environ, "HOME": str(home)},
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(second.returncode, 0, second.stderr)

            connection = sqlite3.connect(state / "twill.db")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT path_missing, last_offset FROM cursor WHERE path = ?",
                        (str(vanished.resolve()),),
                    ).fetchone(),
                    (1, offsets[str(vanished.resolve())]),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT path_missing, last_offset FROM cursor WHERE path = ?",
                        (str(kept.resolve()),),
                    ).fetchone(),
                    (0, offsets[str(kept.resolve())]),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM observation WHERE session_id = 'glob-retired'"
                    ).fetchone(),
                    (1,),
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
