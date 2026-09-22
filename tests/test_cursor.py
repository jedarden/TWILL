"""Cursor identity and offset bookkeeping tests (plan §7.1, §8.1 EC-02..EC-05).

Unit tests pin the arithmetic — identity hashing, the parse/resume/reparse
decision, complete-line scanning — against hand-built files, and store-level
tests drive the same paths end-to-end through :meth:`Store.ingest_path` and
the CLI, using the transcript fixture corpus's appended / rewritten /
truncated snapshots where one exists.
"""

import hashlib
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
FIXTURES = ROOT / "tests" / "fixtures" / "transcripts" / "claude"
sys.path.insert(0, str(ROOT))

import twill_cursor  # noqa: E402
from twill_app import Store  # noqa: E402


def claude_line(session_id: str, text: str, index: int) -> str:
    return json.dumps(
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": f"2026-09-22T12:00:{index:02d}Z",
            "cwd": "/workspace/demo",
            "message": {"role": "user", "content": text},
        }
    )


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_identity_hash_covers_whole_file_shorter_than_prefix(self):
        path = self.root / "short.jsonl"
        payload = b"abc" * 10
        path.write_bytes(payload)
        self.assertEqual(
            twill_cursor.identity_hash(path), hashlib.sha256(payload).hexdigest()
        )

    def test_identity_hash_covers_only_the_first_4kib(self):
        path = self.root / "long.jsonl"
        payload = bytes(range(256)) * 32  # 8 KiB
        path.write_bytes(payload)
        self.assertEqual(
            twill_cursor.identity_hash(path),
            hashlib.sha256(payload[:4096]).hexdigest(),
        )

    def test_file_facts_records_size_and_mtime_ns(self):
        path = self.root / "facts.jsonl"
        path.write_bytes(b"payload")
        facts = twill_cursor.file_facts(path)
        stat = path.stat()
        self.assertEqual(facts.size, stat.st_size)
        self.assertEqual(facts.mtime_ns, stat.st_mtime_ns)
        self.assertEqual(facts.identity_sha, hashlib.sha256(b"payload").hexdigest())


class PlanTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def write(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    @staticmethod
    def row(path: Path, **overrides) -> twill_cursor.CursorRow:
        values = dict(
            path=str(path),
            session_id="s1",
            source="claude",
            identity_sha=twill_cursor.identity_hash(path),
            size=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns,
            last_offset=path.stat().st_size,
            parse_errors=0,
            first_seen="2026-09-22T00:00:00+00:00",
            last_indexed_at="2026-09-22T00:00:00+00:00",
            path_missing=False,
        )
        values.update(overrides)
        return twill_cursor.CursorRow(**values)

    def plan(self, path: Path, row: twill_cursor.CursorRow | None):
        return twill_cursor.plan_ingest(row, path, twill_cursor.file_facts(path))

    def test_first_sighting_parses_from_zero(self):
        path = self.write("new.jsonl", b"{}\n")
        plan = self.plan(path, None)
        self.assertEqual(plan.action, twill_cursor.ACTION_PARSE)
        self.assertIsNone(plan.reason)
        self.assertEqual(plan.start_offset, 0)
        self.assertFalse(plan.replace_session)

    def test_unchanged_file_resumes_at_last_offset(self):
        path = self.write("same.jsonl", b"{}\n{}\n")
        plan = self.plan(path, self.row(path))
        self.assertEqual(plan.action, twill_cursor.ACTION_RESUME)
        self.assertEqual(plan.start_offset, path.stat().st_size)

    def test_grown_file_resumes(self):
        path = self.write("grew.jsonl", b"{}\n")
        row = self.row(path, last_offset=4)
        with path.open("ab") as handle:
            handle.write(b'{"second":true}\n')
        plan = self.plan(path, row)
        self.assertEqual(plan.action, twill_cursor.ACTION_RESUME)
        self.assertEqual(plan.start_offset, 4)

    def test_growth_past_the_4kib_boundary_still_resumes(self):
        # The stored digest covered the whole (short) file; the comparison
        # re-hashes exactly that span of the grown file, so crossing the
        # 4 KiB prefix boundary is an append, not a spurious rewrite.
        small = b'{"n":"pad"}\n' + b"//" + b"x" * 4000 + b"\n"  # 4014 bytes
        path = self.write("boundary.jsonl", small)
        row = self.row(path)
        growth = b'{"appended":true,"pad":"' + b"y" * 200 + b'"}\n'
        with path.open("ab") as handle:
            handle.write(growth)
        self.assertLess(len(small), twill_cursor.IDENTITY_PREFIX_BYTES)
        self.assertGreater(path.stat().st_size, twill_cursor.IDENTITY_PREFIX_BYTES)
        plan = self.plan(path, row)
        self.assertEqual(plan.action, twill_cursor.ACTION_RESUME)
        self.assertEqual(plan.start_offset, len(small))

    def test_rewrite_in_place_reparses(self):
        path = self.write("rewritten.jsonl", b'{"version":1}\n{"x":1}\n')
        row = self.row(path)
        path.write_bytes(b'{"version":2}\n{"x":1}\n')  # same size, new head
        plan = self.plan(path, row)
        self.assertEqual(plan.action, twill_cursor.ACTION_REPARSE)
        self.assertEqual(plan.reason, twill_cursor.REASON_IDENTITY_CHANGED)
        self.assertEqual(plan.start_offset, 0)
        self.assertTrue(plan.replace_session)

    def test_shrink_below_last_offset_reparses(self):
        path = self.write("shrank.jsonl", b'{"one":1}\n{"two":2}\n')
        row = self.row(path)
        path.write_bytes(b"{}\n")  # smaller than the committed offset
        plan = self.plan(path, row)
        self.assertEqual(plan.action, twill_cursor.ACTION_REPARSE)
        self.assertEqual(plan.reason, twill_cursor.REASON_SHRANK)
        self.assertTrue(plan.replace_session)

    def test_shrink_above_last_offset_with_new_head_reparses(self):
        # A rewrite that stays larger than last_offset is caught by the
        # identity hash, not the size check (EC-03's other half).
        path = self.write("resized.jsonl", b'{"a":1}\n{"b":2}\n{"c":3}\n')
        row = self.row(path, last_offset=9)
        path.write_bytes(b'{"z":1}\n{"b":2}\n')  # >= last_offset, head changed
        plan = self.plan(path, row)
        self.assertEqual(plan.action, twill_cursor.ACTION_REPARSE)
        self.assertEqual(plan.reason, twill_cursor.REASON_IDENTITY_CHANGED)

    def test_returned_missing_file_plans_by_identity(self):
        # A path_missing row is not special: the file is back, and whether it
        # resumes or reparses is still an identity question.
        path = self.write("back.jsonl", b"{}\n")
        row = self.row(path, path_missing=True)
        plan = self.plan(path, row)
        self.assertEqual(plan.action, twill_cursor.ACTION_RESUME)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def write(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    def test_yields_complete_lines_with_whole_file_numbers(self):
        path = self.write("lines.jsonl", b'{"one":1}\n{"two":2}\n')
        scan = twill_cursor.scan_lines(path, 0)
        self.assertEqual(
            scan.lines, ((1, '{"one":1}'), (2, '{"two":2}'))
        )
        self.assertEqual(scan.new_offset, path.stat().st_size)
        self.assertFalse(scan.pending_tail)
        self.assertFalse(scan.region_empty)  # lines were offered and consumed

    def test_line_numbers_continue_across_the_skipped_prefix(self):
        path = self.write("span.jsonl", b'{"one":1}\n{"two":2}\n{"three":3}\n')
        boundary = path.stat().st_size - len(b'{"three":3}\n')
        scan = twill_cursor.scan_lines(path, boundary)
        self.assertEqual(scan.lines, ((3, '{"three":3}'),))
        self.assertEqual(scan.start_offset, boundary)
        self.assertEqual(scan.new_offset, path.stat().st_size)

    def test_unterminated_tail_is_not_consumed(self):
        path = self.write(
            "torn.jsonl", b'{"one":1}\n{"half":'
        )
        scan = twill_cursor.scan_lines(path, 0)
        self.assertEqual(scan.lines, ((1, '{"one":1}'),))
        self.assertEqual(scan.new_offset, len(b'{"one":1}\n'))
        self.assertTrue(scan.pending_tail)
        self.assertFalse(scan.region_empty)

    def test_empty_region_reports_nothing(self):
        path = self.write("empty-region.jsonl", b'{"one":1}\n')
        scan = twill_cursor.scan_lines(path, path.stat().st_size)
        self.assertEqual(scan.lines, ())
        self.assertEqual(scan.new_offset, path.stat().st_size)
        self.assertFalse(scan.pending_tail)
        self.assertTrue(scan.region_empty)

    def test_region_of_only_a_pending_tail_has_no_lines(self):
        path = self.write("tail-only.jsonl", b'{"one":1}\n{"half":')
        scan = twill_cursor.scan_lines(path, len(b'{"one":1}\n'))
        self.assertEqual(scan.lines, ())
        self.assertTrue(scan.pending_tail)
        self.assertEqual(scan.new_offset, len(b'{"one":1}\n'))

    def test_start_past_end_of_file_raises(self):
        path = self.write("small.jsonl", b"{}\n")
        with self.assertRaises(ValueError):
            twill_cursor.scan_lines(path, 999)

    def test_multi_byte_utf8_survives_the_decode(self):
        payload = ('{"text":"héllo wörld"}\n').encode("utf-8")
        path = self.write("utf8.jsonl", payload)
        scan = twill_cursor.scan_lines(path, 0)
        self.assertEqual(scan.lines, ((1, '{"text":"héllo wörld"}'),))


class StoreCursorTests(unittest.TestCase):
    """EC-02..EC-05 through the real store, one temp file per scenario."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)

    def write_session(self, name: str, texts, session_id: str = "cursor-session") -> Path:
        path = self.root / name
        path.write_text(
            "".join(
                claude_line(session_id, text, index) + "\n"
                for index, text in enumerate(texts)
            )
        )
        return path

    def cursor_row(self, path: Path):
        return twill_cursor.load_cursor(
            self.store.connection, str(path.resolve())
        )

    def observation_texts(self, session_id: str = "cursor-session"):
        return sorted(
            row[0]
            for row in self.store.connection.execute(
                "SELECT excerpt FROM observation WHERE session_id = ?", (session_id,)
            )
        )

    # -- EC-02: file grew since last run --------------------------------------

    def test_append_resumes_at_last_offset(self):
        base_texts = ["first turn", "second turn"]
        delta_texts = ["appended turn"]
        path = self.write_session("append.jsonl", base_texts)
        first = self.store.ingest_path(path)
        self.assertEqual(first["action"], twill_cursor.ACTION_PARSE)
        self.assertEqual(self.observation_texts(), sorted(base_texts))

        with path.open("a") as handle:
            for index, text in enumerate(delta_texts, start=len(base_texts)):
                handle.write(claude_line("cursor-session", text, index) + "\n")
        second = self.store.ingest_path(path)

        self.assertEqual(second["action"], twill_cursor.ACTION_RESUME)
        self.assertEqual(self.observation_texts(), sorted(base_texts + delta_texts))
        row = self.cursor_row(path)
        self.assertEqual(row.last_offset, path.stat().st_size)
        self.assertEqual(row.parse_errors, 0)
        self.assertEqual(row.size, path.stat().st_size)
        self.assertFalse(row.path_missing)
        # Appended events number contiguously with the base parse.
        lines = [
            row[0]
            for row in self.store.connection.execute(
                "SELECT source_line FROM transcript_event ORDER BY source_line"
            )
        ]
        self.assertEqual(lines, [1, 2, 3])

    def test_append_fixture_pair_resumes(self):
        path = self.root / "pair.jsonl"
        base = (FIXTURES / "appended-between-runs" / "base.jsonl").read_bytes()
        delta = (FIXTURES / "appended-between-runs" / "append.jsonl").read_bytes()
        path.write_bytes(base)
        self.store.ingest_path(path)
        path.write_bytes(base + delta)
        summary = self.store.ingest_path(path)
        self.assertEqual(summary["action"], twill_cursor.ACTION_RESUME)
        self.assertEqual(summary["events"], 2)
        self.assertEqual(len(self.observation_texts("claude-append-001")), 4)
        self.assertEqual(self.cursor_row(path).last_offset, len(base) + len(delta))

    def test_unchanged_file_is_a_byte_identical_no_op(self):
        path = self.write_session("same.jsonl", ["turn one", "turn two"])
        self.store.ingest_path(path)
        before = self.store.connection.execute(
            "SELECT obs_id, session_id, ts_utc, ts_local, kind, excerpt FROM observation ORDER BY obs_id"
        ).fetchall()
        first_seen = self.cursor_row(path).first_seen

        summary = self.store.ingest_path(path)

        self.assertEqual(summary["action"], twill_cursor.ACTION_RESUME)
        self.assertEqual(summary["events"], 0)
        after = self.store.connection.execute(
            "SELECT obs_id, session_id, ts_utc, ts_local, kind, excerpt FROM observation ORDER BY obs_id"
        ).fetchall()
        self.assertEqual(after, before)
        self.assertEqual(self.cursor_row(path).first_seen, first_seen)

    # -- EC-03: file shrank or was rewritten in place -------------------------

    def test_rewrite_in_place_reparses_and_replaces_derived_rows(self):
        path = self.root / "rewrite.jsonl"
        path.write_bytes((FIXTURES / "rewritten-in-place" / "before.jsonl").read_bytes())
        self.store.ingest_path(path)
        self.assertIn("Original snapshot", " ".join(self.observation_texts("claude-rewrite-001")))

        path.write_bytes((FIXTURES / "rewritten-in-place" / "after.jsonl").read_bytes())
        summary = self.store.ingest_path(path)

        self.assertEqual(summary["action"], twill_cursor.ACTION_REPARSE)
        texts = " ".join(self.observation_texts("claude-rewrite-001"))
        self.assertIn("Replacement snapshot", texts)
        self.assertNotIn("Original snapshot", texts)
        row = self.cursor_row(path)
        self.assertEqual(
            row.identity_sha,
            twill_cursor.identity_hash(path),
        )
        self.assertEqual(row.last_offset, path.stat().st_size)

    def test_shrunk_file_reparses_from_zero(self):
        path = self.write_session("shrink.jsonl", ["keep me", "drop me too"])
        self.store.ingest_path(path)
        path.write_text(claude_line("cursor-session", "keep me", 0) + "\n")
        summary = self.store.ingest_path(path)
        self.assertEqual(summary["action"], twill_cursor.ACTION_REPARSE)
        self.assertEqual(self.observation_texts(), ["keep me"])
        self.assertEqual(self.cursor_row(path).last_offset, path.stat().st_size)

    # -- EC-04: half-written final JSON line ----------------------------------

    def test_unterminated_tail_stops_at_the_last_complete_line(self):
        complete = claude_line("cursor-session", "complete turn", 0) + "\n"
        path = self.root / "torn.jsonl"
        path.write_text(complete + '{"type":"user","message":{"content":"hal')
        self.store.ingest_path(path)

        row = self.cursor_row(path)
        self.assertEqual(row.last_offset, len(complete.encode()))
        self.assertEqual(row.parse_errors, 1)
        self.assertEqual(self.observation_texts(), ["complete turn"])

        # Completing the torn line parses it on the next run and clears the
        # error counter for the span.
        with path.open("a") as handle:
            handle.write('f turn"}}\n')
        self.store.ingest_path(path)
        row = self.cursor_row(path)
        self.assertEqual(row.parse_errors, 0)
        self.assertEqual(row.last_offset, path.stat().st_size)
        self.assertEqual(self.observation_texts(), ["complete turn", "half turn"])

    def test_pending_tail_error_count_is_stable_across_idle_runs(self):
        complete = claude_line("cursor-session", "complete turn", 0) + "\n"
        path = self.root / "idle.jsonl"
        path.write_text(complete + '{"half":')
        self.store.ingest_path(path)
        self.assertEqual(self.cursor_row(path).parse_errors, 1)
        self.store.ingest_path(path)
        self.store.ingest_path(path)
        self.assertEqual(self.cursor_row(path).parse_errors, 1)
        self.assertEqual(self.cursor_row(path).last_offset, len(complete.encode()))

    def test_flushed_but_invalid_final_line_is_counted_and_passed(self):
        # The corpus's truncated fixture: the record is torn but its newline
        # was flushed, so the producer will never repair it — the cursor
        # advances past the line and only the error is recorded.
        path = self.root / "flushed.jsonl"
        path.write_bytes((FIXTURES / "truncated-final-line.jsonl").read_bytes())
        self.store.ingest_path(path)

        row = self.cursor_row(path)
        self.assertEqual(row.last_offset, path.stat().st_size)
        self.assertEqual(row.parse_errors, 1)
        self.assertIn("The complete line is available", " ".join(self.observation_texts("claude-truncated-001")))
        self.assertNotIn("half-written", " ".join(self.observation_texts("claude-truncated-001")))

        with path.open("a") as handle:
            handle.write(claude_line("claude-truncated-001", "after repair", 3) + "\n")
        self.store.ingest_path(path)
        self.assertEqual(self.cursor_row(path).parse_errors, 0)
        self.assertIn("after repair", self.observation_texts("claude-truncated-001"))

    # -- EC-05: transcript disappears between runs ----------------------------

    def test_vanished_file_is_flagged_without_losing_observations(self):
        path = self.write_session("vanishing.jsonl", ["precious evidence"])
        self.store.ingest_path(path)
        path.unlink()

        changed = self.store.mark_missing_paths()

        self.assertEqual(changed, 1)
        row = self.cursor_row(path)
        self.assertTrue(row.path_missing)
        # Absence is never a reason to delete evidence (EC-05).
        self.assertEqual(self.observation_texts(), ["precious evidence"])

        # Reappearance clears the flag on the next sweep.
        self.write_session("vanishing.jsonl", ["precious evidence"])
        self.store.mark_missing_paths()
        self.assertFalse(self.cursor_row(path).path_missing)

    def test_sweep_never_touches_existing_files(self):
        path = self.write_session("present.jsonl", ["still here"])
        self.store.ingest_path(path)
        os.utime(path, None)  # unsettled by mtime, but it exists
        self.assertEqual(self.store.mark_missing_paths(), 0)
        self.assertFalse(self.cursor_row(path).path_missing)

    def test_cursor_offset_is_monotone_except_across_identity_changes(self):
        # Plan §10.1's property: a cursor never moves backwards except on an
        # identity change.  Drive one file through the whole lifecycle.
        path = self.write_session(
            "life.jsonl", ["one", "two"]
        )
        self.store.ingest_path(path)
        previous = self.cursor_row(path)

        def step(expect_actions):
            summary = self.store.ingest_path(path)
            self.assertIn(summary["action"], expect_actions)
            current = self.cursor_row(path)
            if current.identity_sha == previous.identity_sha:
                self.assertGreaterEqual(
                    current.last_offset,
                    previous.last_offset,
                    "offset moved backwards without an identity change",
                )
            self.assertLessEqual(current.last_offset, path.stat().st_size)
            return current

        # Grow: resumes, offset advances.
        with path.open("a") as handle:
            handle.write(claude_line("cursor-session", "three", 2) + "\n")
        grown = step({twill_cursor.ACTION_RESUME})
        self.assertGreater(grown.last_offset, previous.last_offset)
        previous = grown

        # Rewrite in place: identity changes, offset restarts from zero.
        self.write_session("life.jsonl", ["fresh one"])
        rewritten = step({twill_cursor.ACTION_REPARSE})
        self.assertNotEqual(rewritten.identity_sha, previous.identity_sha)
        self.assertEqual(rewritten.last_offset, path.stat().st_size)
        previous = rewritten

        # Shrink below the committed offset: identity changes again.
        with path.open("r") as handle:
            head = handle.read(len(claude_line("cursor-session", "fresh one", 0)) // 2)
        path.write_text(head)  # half a line: no complete line survives
        shrunk = step({twill_cursor.ACTION_REPARSE})
        self.assertEqual(shrunk.last_offset, 0)
        self.assertEqual(shrunk.parse_errors, 1)
        previous = shrunk

        # Idle run: nothing moves but last_indexed_at.
        idle = step({twill_cursor.ACTION_RESUME})
        self.assertEqual(
            (idle.identity_sha, idle.size, idle.last_offset, idle.parse_errors),
            (previous.identity_sha, previous.size, previous.last_offset, previous.parse_errors),
        )


class CursorCliTests(unittest.TestCase):
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

    def test_cli_run_flags_a_vanished_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            keep = root / "keep.jsonl"
            gone = root / "gone.jsonl"
            keep.write_text(claude_line("cli-keep", "kept turn", 0) + "\n")
            gone.write_text(claude_line("cli-gone", "lost file turn", 0) + "\n")

            first = self.run_cli(
                "ingest", "--source", str(root), "--settle", "0",
                "--limit", "5", "--state-dir", str(state),
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            gone.unlink()
            second = self.run_cli(
                "ingest", "--source", str(root), "--settle", "0",
                "--limit", "5", "--state-dir", str(state),
            )
            self.assertEqual(second.returncode, 0, second.stderr)

            connection = sqlite3.connect(state / "twill.db")
            try:
                flagged = connection.execute(
                    "SELECT path_missing FROM cursor WHERE path = ?", (str(gone),)
                ).fetchone()
                kept = connection.execute(
                    "SELECT path_missing FROM cursor WHERE path = ?", (str(keep),)
                ).fetchone()
                survivors = connection.execute(
                    "SELECT count(*) FROM observation WHERE session_id = 'cli-gone'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(flagged, (1,))
            self.assertEqual(kept, (0,))
            self.assertEqual(survivors, 1)

            digest = self.run_cli("digest", "--stdout", "--state-dir", str(state))
            self.assertEqual(digest.returncode, 0, digest.stderr)
            self.assertIn("lost file turn", digest.stdout)

    def test_cli_sweep_runs_even_when_no_file_is_settled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            transcript = root / "only.jsonl"
            transcript.write_text(claude_line("cli-only", "only turn", 0) + "\n")
            settled = self.run_cli(
                "ingest", "--source", str(root), "--settle", "0",
                "--limit", "5", "--state-dir", str(state),
            )
            self.assertEqual(settled.returncode, 0, settled.stderr)

            transcript.unlink()
            unsettled = self.run_cli(
                "ingest", "--source", str(root), "--settle", "2h",
                "--state-dir", str(state),
            )
            self.assertNotEqual(unsettled.returncode, 0)
            self.assertIn("no settled", unsettled.stderr)

            connection = sqlite3.connect(state / "twill.db")
            try:
                flagged = connection.execute(
                    "SELECT path_missing FROM cursor WHERE path = ?",
                    (str(transcript),),
                ).fetchone()
                survivors = connection.execute(
                    "SELECT count(*) FROM observation WHERE session_id = 'cli-only'"
                ).fetchone()[0]
            finally:
                connection.close()
            # The sweep recorded the vanishing before the error short-circuited.
            self.assertEqual(flagged, (1,))
            self.assertEqual(survivors, 1)


if __name__ == "__main__":
    unittest.main()
