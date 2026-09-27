"""Per-run record-type histogram tests (plan §6.1, §8.2, Phase 1).

``parse_shape`` is the drift alarm's substrate: every ingest run counts the
records its parser consumed by declared type per source — new span only, so a
resumed codex parse never re-counts its replayed prefix — and an upstream
format change therefore shifts a distribution instead of silently losing
extraction.  These tests drive the real persistence boundary
(:meth:`Store.ingest_path`) and the CLI over claude, codex, and untyped
fixtures; the trailing-median comparison itself is the doctor alarm's business
(plan Phase 2), not this table's.
"""

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

import twill_cursor  # noqa: E402
from twill_app import SessionData, Store  # noqa: E402


def claude_turn(session_id: str, text: str, index: int) -> str:
    return json.dumps(
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": f"2026-09-27T12:00:{index:02d}Z",
            "cwd": "/workspace/demo",
            "message": {"role": "user", "content": text},
        }
    )


def claude_assistant(session_id: str, text: str, index: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": f"2026-09-27T12:00:{index:02d}Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


def codex_line(record_type: str, payload: dict, index: int) -> str:
    return json.dumps(
        {
            "type": record_type,
            "timestamp": f"2026-09-27T13:00:{index:02d}Z",
            "payload": payload,
        }
    )


def histogram(store: Store) -> dict[str, dict[str, dict[str, int]]]:
    """parse_shape as ``{run_at: {source: {record_type: n}}}``."""

    runs: dict[str, dict[str, dict[str, int]]] = {}
    for run_at, source, record_type, count in store.connection.execute(
        "SELECT run_at, source, record_type, n FROM parse_shape "
        "ORDER BY run_at, source, record_type"
    ):
        runs.setdefault(run_at, {}).setdefault(source, {})[record_type] = count
    return runs


class ParseShapeStoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.state = self.root / "state"

    def claude_path(self, name: str = "transcript.jsonl") -> Path:
        directory = self.root / ".claude" / "projects"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / name

    def codex_path(self, name: str = "rollout.jsonl") -> Path:
        directory = self.root / ".codex" / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / name

    # -- the histogram counts records, not events -----------------------------

    def test_first_parse_counts_records_by_declared_type(self):
        path = self.claude_path()
        # A summary record yields no event (no text key the reader traverses)
        # but is still a parsed record the drift alarm must see.
        summary = json.dumps(
            {
                "type": "summary",
                "sessionId": "shape-claude",
                "summary": "a session summary record",
            }
        )
        path.write_text(
            "\n".join(
                [
                    claude_turn("shape-claude", "first turn", 0),
                    claude_turn("shape-claude", "second turn", 1),
                    claude_assistant("shape-claude", "reply", 2),
                    summary,
                ]
            )
            + "\n"
        )
        store = Store(self.state)
        self.addCleanup(store.close)
        summary_row = store.ingest_path(path)

        self.assertEqual(summary_row["action"], twill_cursor.ACTION_PARSE)
        self.assertEqual(summary_row["events"], 3)
        runs = histogram(store)
        self.assertEqual(len(runs), 1)
        self.assertEqual(
            next(iter(runs.values())),
            {"claude": {"assistant": 1, "summary": 1, "user": 2}},
        )

    def test_codex_parse_counts_rollout_record_types(self):
        path = self.codex_path()
        path.write_text(
            "\n".join(
                [
                    codex_line(
                        "session_meta",
                        {"session_id": "shape-codex", "cwd": "/workspace/demo"},
                        0,
                    ),
                    codex_line(
                        "response_item",
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hello"}],
                        },
                        1,
                    ),
                    codex_line("event_msg", {"type": "agent_message", "message": "hi"}, 2),
                ]
            )
            + "\n"
        )
        store = Store(self.state)
        self.addCleanup(store.close)
        store.ingest_path(path)

        runs = histogram(store)
        self.assertEqual(len(runs), 1)
        self.assertEqual(
            next(iter(runs.values())),
            {"codex": {"event_msg": 1, "response_item": 1, "session_meta": 1}},
        )

    # -- new span only: resume and replay never double-count ------------------

    def test_appended_span_counts_only_the_new_records(self):
        path = self.claude_path("append.jsonl")
        path.write_text(
            claude_turn("shape-append", "base turn", 0) + "\n"
            + claude_turn("shape-append", "another base turn", 1) + "\n"
        )
        store = Store(self.state)
        self.addCleanup(store.close)
        store.ingest_path(path)

        with path.open("a") as handle:
            handle.write(
                claude_assistant("shape-append", "delta reply", 2) + "\n"
                + claude_turn("shape-append", "delta turn", 3) + "\n"
            )
        resumed = store.ingest_path(path)

        self.assertEqual(resumed["action"], twill_cursor.ACTION_RESUME)
        # One run, one Store: the base parse and the appended span accumulate
        # on the same run_at — the run saw all four records.
        runs = histogram(store)
        self.assertEqual(len(runs), 1)
        self.assertEqual(
            next(iter(runs.values())),
            {"claude": {"assistant": 1, "user": 3}},
        )

    def test_codex_resume_never_recounts_the_replayed_prefix(self):
        path = self.codex_path()
        path.write_text(
            "\n".join(
                [
                    codex_line(
                        "session_meta",
                        {"session_id": "shape-replay", "cwd": "/workspace/demo"},
                        0,
                    ),
                    codex_line(
                        "response_item",
                        {
                            "type": "custom_tool_call",
                            "call_id": "run-1",
                            "name": "exec",
                            "input": "true",
                        },
                        1,
                    ),
                ]
            )
            + "\n"
        )
        first_store = Store(self.state)
        try:
            first_store.ingest_path(path)
        finally:
            first_store.close()

        with path.open("a") as handle:
            handle.write(
                "\n".join(
                    [
                        codex_line("event_msg", {"type": "turn_aborted"}, 2),
                        codex_line("event_msg", {"type": "agent_reasoning"}, 3),
                    ]
                )
                + "\n"
            )
        second_store = Store(self.state)
        try:
            resumed = second_store.ingest_path(path)
            runs = histogram(second_store)
        finally:
            second_store.close()

        self.assertEqual(resumed["action"], twill_cursor.ACTION_RESUME)
        # Two runs: the first saw the base records, the second only the two
        # appended event_msg records — the replayed prefix restores codex
        # parser state but is not part of the new run's shape.
        self.assertEqual(len(runs), 2)
        first, second = list(runs.values())
        self.assertEqual(first, {"codex": {"response_item": 1, "session_meta": 1}})
        self.assertEqual(second, {"codex": {"event_msg": 2}})

    def test_reparse_counts_the_replacement_bytes_under_the_same_run(self):
        path = self.claude_path("rewrite.jsonl")
        path.write_text(
            claude_turn("shape-rewrite", "before one", 0) + "\n"
            + claude_turn("shape-rewrite", "before two", 1) + "\n"
        )
        store = Store(self.state)
        self.addCleanup(store.close)
        store.ingest_path(path)

        path.write_text(
            "\n".join(
                claude_assistant("shape-rewrite", f"after {index}", index)
                for index in range(3)
            )
            + "\n"
        )
        reparsed = store.ingest_path(path)

        self.assertEqual(reparsed["action"], twill_cursor.ACTION_REPARSE)
        # A reparse is a full parse of the new bytes: the run's histogram
        # carries both what the first pass consumed and the reparse.
        runs = histogram(store)
        self.assertEqual(len(runs), 1)
        self.assertEqual(
            next(iter(runs.values())),
            {"claude": {"assistant": 3, "user": 2}},
        )

    def test_idle_pass_writes_no_rows(self):
        path = self.claude_path("idle.jsonl")
        path.write_text(claude_turn("shape-idle", "only turn", 0) + "\n")
        store = Store(self.state)
        self.addCleanup(store.close)
        store.ingest_path(path)
        before = histogram(store)

        idle = store.ingest_path(path)

        self.assertEqual(idle["action"], twill_cursor.ACTION_RESUME)
        self.assertEqual(idle["events"], 0)
        # An unchanged file parses nothing, so the run stays invisible to the
        # histogram — the dead-man's switch, not an empty row, covers "no work".
        self.assertEqual(histogram(store), before)

    # -- buckets -------------------------------------------------------------

    def test_unparseable_type_fields_land_in_the_untyped_bucket(self):
        path = self.root / "untyped.jsonl"
        untyped_record = json.dumps(
            {
                "sessionId": "shape-untyped",
                "timestamp": "2026-09-27T12:00:00Z",
                "message": {"role": "user", "content": "no declared type"},
            }
        )
        numeric_type_record = json.dumps(
            {
                "type": 3,
                "sessionId": "shape-untyped",
                "timestamp": "2026-09-27T12:00:01Z",
                "message": {"role": "user", "content": "non-string type"},
            }
        )
        path.write_text(
            untyped_record + "\n" + numeric_type_record + "\n" + "not json\n"
        )
        store = Store(self.state)
        self.addCleanup(store.close)
        store.ingest_path(path)

        runs = histogram(store)
        # A bare directory is the generic source; records whose type is
        # missing or not a string stay countable in one bucket, while the
        # unparseable line is a parse error, not a record.
        self.assertEqual(
            next(iter(runs.values())),
            {"jsonl": {"untyped": 2}},
        )

    def test_hand_built_session_writes_no_parse_shape_rows(self):
        store = Store(self.state)
        self.addCleanup(store.close)
        path = self.claude_path("hand-built.jsonl")
        session = SessionData(path, "shape-hand", "claude", ())
        store.ingest(session)

        self.assertEqual(histogram(store), {})


class ParseShapeCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ingest loads config at startup; artifacts_root has no default.
        cls._config_home = tempfile.TemporaryDirectory()
        home = Path(cls._config_home.name)
        config_dir = home / ".config" / "twill"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            f'artifacts_root = "{home / "artifacts"}"\n'
        )

    @classmethod
    def tearDownClass(cls):
        cls._config_home.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(Path(self._config_home.name))},
            check=False,
            text=True,
            capture_output=True,
        )

    @staticmethod
    def read_histogram(state: Path) -> dict[str, dict[str, dict[str, int]]]:
        connection = sqlite3.connect(state / "twill.db")
        try:
            runs: dict[str, dict[str, dict[str, int]]] = {}
            for run_at, source, record_type, count in connection.execute(
                "SELECT run_at, source, record_type, n FROM parse_shape "
                "ORDER BY run_at, source, record_type"
            ):
                runs.setdefault(run_at, {}).setdefault(source, {})[record_type] = count
            return runs
        finally:
            connection.close()

    def test_one_cli_run_stamps_one_run_at_across_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / ".claude" / "projects"
            projects.mkdir(parents=True)
            state = root / "state"
            first = projects / "first.jsonl"
            second = projects / "second.jsonl"
            first.write_text(
                claude_turn("shape-cli", "turn one", 0) + "\n"
                + claude_turn("shape-cli", "turn two", 1) + "\n"
            )
            second.write_text(
                claude_turn("shape-cli", "other turn", 0) + "\n"
                + claude_assistant("shape-cli", "other reply", 1) + "\n"
            )

            initial = self.run_cli(
                "ingest", "--source", str(root), "--settle", "0",
                "--limit", "5", "--state-dir", str(state),
            )
            self.assertEqual(initial.returncode, 0, initial.stderr)
            runs = self.read_histogram(state)
            self.assertEqual(len(runs), 1)
            self.assertEqual(
                next(iter(runs.values())),
                {"claude": {"assistant": 1, "user": 3}},
            )

            with first.open("a") as handle:
                handle.write(claude_turn("shape-cli", "appended turn", 2) + "\n")
            appended = self.run_cli(
                "ingest", "--source", str(root), "--settle", "0",
                "--limit", "5", "--state-dir", str(state),
            )
            self.assertEqual(appended.returncode, 0, appended.stderr)

            # The second invocation is a second run: its own run_at counting
            # only the appended record, with the first run's rows untouched —
            # the series the trailing median is computed over.
            runs = self.read_histogram(state)
            self.assertEqual(len(runs), 2)
            first_run, second_run = list(runs.values())
            self.assertEqual(first_run, {"claude": {"assistant": 1, "user": 3}})
            self.assertEqual(second_run, {"claude": {"user": 1}})


if __name__ == "__main__":
    unittest.main()
