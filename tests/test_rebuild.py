"""``doctor --rebuild`` recovery tests (plan §5 Scenario 3, §8.2, §8.4).

Scenario 3's corrupt-DB case ends with ``twill doctor --rebuild`` recreating
the schema and re-parsing every session still on disk; §8.2's failure table
makes that the recovery for "DB corrupt / deleted", and §8.4 names the same
command for rolling the state directory back to nothing.  These tests pin the
recovery contract from the CLI down to the rows:

- A damaged database is *discarded*, not repaired — garbage bytes where
  ``twill.db`` was still leave ``doctor`` broken (exit 2) and ``--rebuild``
  starting from a clean slate.
- The rebuilt store holds row-for-row the same derived rows a from-scratch
  ingest produces (the §8.3 idempotency property, observed through the real
  CLI boundary): observations and transcript events identical including their
  rowids, session and cursor rows identical minus the wall-clock stamps that
  record *when* the run happened.
- The settle gate (EC-01) and the truncated-tail rule (EC-04) keep holding
  during a rebuild: a young file is skipped whole, a half-written final line
  is skipped with ``parse_errors`` incremented and the cursor left at the
  last complete line.
- Lessons and measurement history live under ``artifacts_root`` as files in
  git, so a full DB loss costs nothing but re-reading — the rebuild opens
  nothing there for writing and leaves every artifact byte untouched.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

import twill_schema  # noqa: E402
from twill_contract import CliError  # noqa: E402
from twill_app import rebuild_state_database  # noqa: E402
from twill_lock import StateLock  # noqa: E402


def claude_line(session_id: str, text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-09-22T12:00:00Z",
            "cwd": "/workspace/demo",
            "message": {"role": "user", "content": text},
        }
    )


#: Explicit column lists keep the comparison honest about what may differ
#: across a rebuild: only the wall-clock stamps, never anything
#: transcript-derived (mirrors test_idempotency's CROSS_STORE_DROPS).
SESSION_COLUMNS = ("session_key", "session_id", "source_path", "source_kind")
CURSOR_COLUMNS = (
    "path",
    "session_id",
    "source",
    "identity_sha",
    "size",
    "mtime_ns",
    "last_offset",
    "parse_errors",
    "path_missing",
)


def dump_state(state: Path) -> dict[str, list[tuple]]:
    """Every rebuilt table the recovery must reproduce, as row tuples."""

    connection = sqlite3.connect(state / "twill.db")
    try:
        return {
            "observation": connection.execute(
                "SELECT * FROM observation ORDER BY obs_id"
            ).fetchall(),
            "transcript_event": connection.execute(
                "SELECT * FROM transcript_event ORDER BY event_id"
            ).fetchall(),
            "session": connection.execute(
                f"SELECT {', '.join(SESSION_COLUMNS)} FROM session "
                "ORDER BY session_key"
            ).fetchall(),
            "cursor": connection.execute(
                f"SELECT {', '.join(CURSOR_COLUMNS)} FROM cursor ORDER BY path"
            ).fetchall(),
        }
    finally:
        connection.close()


def damage_database(state: Path) -> None:
    """Leave the kind of corruption deletion is the only recovery for."""

    db_path = twill_schema.state_db_path(state)
    db_path.write_bytes(b"this is not a database" * 400)
    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


class RebuildUnitTests(unittest.TestCase):
    """The rebuild decision inputs, ahead of any database deletion."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.state = self.root / "state"
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()

    def write_session(self, name: str, age_seconds: float = 3 * 3600) -> Path:
        path = self.transcripts / name
        path.write_text(
            claude_line("unit-" + name.removesuffix(".jsonl"), name) + "\n"
        )
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_guard_fails_while_the_damaged_database_is_still_on_disk(self):
        self.write_session("young.jsonl", age_seconds=60)
        self.state.mkdir(parents=True)
        damage_database(self.state)
        before = twill_schema.state_db_path(self.state).read_bytes()
        with self.assertRaises(CliError):
            rebuild_state_database(
                self.state, roots=(self.transcripts,), settle_seconds=2 * 3600
            )
        # Enumeration failed before deletion: the corrupt file is untouched,
        # so an operator can still inspect it (or reconsider) afterwards.
        self.assertEqual(
            twill_schema.state_db_path(self.state).read_bytes(), before
        )

    def test_an_empty_source_tree_rebuilds_to_an_empty_schema(self):
        result = rebuild_state_database(
            self.state, roots=(self.transcripts,), settle_seconds=0
        )
        self.assertEqual(result, {"sessions": 0, "events": 0, "observations": 0})
        connection = sqlite3.connect(twill_schema.state_db_path(self.state))
        try:
            version = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (twill_schema.SCHEMA_VERSION_KEY,)
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(version)
        self.assertEqual(int(version[0]), twill_schema.MIGRATIONS[-1].version)

    def test_a_missing_source_root_is_an_error(self):
        with self.assertRaises(CliError):
            rebuild_state_database(
                self.state, roots=(self.root / "nowhere",), settle_seconds=0
            )


class RebuildCliTests(unittest.TestCase):
    """The recovery path end to end, through the real CLI boundary."""

    @classmethod
    def setUpClass(cls):
        # ingest and doctor --rebuild load config at startup;
        # artifacts_root has no default.
        cls._config_home = tempfile.TemporaryDirectory()
        home = Path(cls._config_home.name)
        cls.home = home
        config_dir = home / ".config" / "twill"
        config_dir.mkdir(parents=True)
        cls.artifacts = home / "artifacts"
        (config_dir / "config.toml").write_text(
            f'artifacts_root = "{cls.artifacts}"\n'
        )

    @classmethod
    def tearDownClass(cls):
        cls._config_home.cleanup()

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.state = self.root / "state"
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()

    def write_session(self, name: str, age_seconds: float = 3 * 3600) -> Path:
        path = self.transcripts / name
        path.write_text(
            claude_line("cli-" + name.removesuffix(".jsonl"), name) + "\n"
        )
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home)},
            check=False,
            text=True,
            capture_output=True,
        )

    def ingest_all(self):
        result = self.run_cli(
            "ingest",
            "--source",
            str(self.transcripts),
            "--settle",
            "0",
            "--limit",
            "10",
            "--state-dir",
            str(self.state),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def rebuild(self, *extra):
        return self.run_cli(
            "doctor",
            "--rebuild",
            "--source",
            str(self.transcripts),
            "--settle",
            "0",
            "--state-dir",
            str(self.state),
            *extra,
        )

    def test_corrupt_database_is_rebuilt_with_identical_rows(self):
        self.write_session("alpha.jsonl")
        self.write_session("beta.jsonl")
        self.ingest_all()
        before = dump_state(self.state)

        damage_database(self.state)
        self.assertEqual(
            self.run_cli("doctor", "--state-dir", str(self.state)).returncode, 2
        )

        result = self.rebuild()
        self.assertEqual(result.returncode, 0, result.stderr)

        after = dump_state(self.state)
        self.assertEqual(after, before)

        # Scenario 3's pass criterion: the pipeline is healthy again and the
        # recreated schema carries this release's stamped version.
        self.assertEqual(
            self.run_cli("doctor", "--state-dir", str(self.state)).returncode, 0
        )
        connection = sqlite3.connect(twill_schema.state_db_path(self.state))
        try:
            version = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (twill_schema.SCHEMA_VERSION_KEY,)
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(int(version[0]), twill_schema.MIGRATIONS[-1].version)

    def test_deleted_database_is_rebuilt(self):
        self.write_session("alpha.jsonl")
        self.ingest_all()
        before = dump_state(self.state)
        for suffix in ("", "-wal", "-shm"):
            twill_schema.state_db_path(self.state).with_name(
                twill_schema.state_db_path(self.state).name + suffix
            ).unlink(missing_ok=True)
        result = self.rebuild()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dump_state(self.state), before)

    def test_truncated_tail_is_skipped_counted_and_left_for_later(self):
        complete = claude_line("cli-torn", "complete turn")
        torn_tail = '{"type":"user","sessionId":"cli-torn","timestamp":"2026-09-2'
        path = self.write_session("torn.jsonl")
        path.write_text(complete + "\n" + torn_tail)

        result = self.rebuild()
        self.assertEqual(result.returncode, 0, result.stderr)

        connection = sqlite3.connect(twill_schema.state_db_path(self.state))
        try:
            cursor = connection.execute(
                "SELECT last_offset, parse_errors FROM cursor WHERE path = ?",
                (str(path),),
            ).fetchone()
            events = connection.execute(
                "SELECT count(*) FROM transcript_event"
            ).fetchone()[0]
        finally:
            connection.close()
        # EC-04: the cursor stops at the last complete line and the invalid
        # tail is counted, not consumed.
        self.assertEqual(cursor[0], len(complete) + 1)
        self.assertEqual(cursor[1], 1)
        self.assertEqual(events, 1)

    def test_young_file_is_skipped_whole_during_a_rebuild(self):
        self.write_session("settled.jsonl")
        self.write_session("young.jsonl", age_seconds=60)
        result = self.run_cli(
            "doctor",
            "--rebuild",
            "--source",
            str(self.transcripts),
            "--settle",
            "2h",
            "--state-dir",
            str(self.state),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        connection = sqlite3.connect(twill_schema.state_db_path(self.state))
        try:
            cursor_paths = [
                row[0] for row in connection.execute("SELECT path FROM cursor")
            ]
            observed = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT session_id FROM observation"
                )
            }
        finally:
            connection.close()
        self.assertEqual(cursor_paths, [str(self.transcripts / "settled.jsonl")])
        self.assertEqual(observed, {"cli-settled"})

    def test_lessons_and_measurements_survive_a_full_database_loss(self):
        self.write_session("alpha.jsonl")
        self.ingest_all()
        lesson = self.artifacts / "lessons" / "L-rebuild-test.md"
        lesson.parent.mkdir(parents=True, exist_ok=True)
        lesson.write_text("# L-rebuild-test\n\nstate: accepted\n")
        measurement = self.artifacts / "measurements" / "L-rebuild-test.jsonl"
        measurement.parent.mkdir(parents=True, exist_ok=True)
        measurement.write_text('{"lesson_id": "L-rebuild-test", "sessions": 3}\n')
        before = (lesson.read_bytes(), measurement.read_bytes())

        damage_database(self.state)
        result = self.rebuild()
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(lesson.read_bytes(), before[0])
        self.assertEqual(measurement.read_bytes(), before[1])

    def test_rebuild_records_its_stage_without_disturbing_ingest_history(self):
        # status.json is operator state, not derived rows: the rebuild gets
        # its own stage record and the hourly ingest's history survives the
        # database loss, so staleness monitoring keeps its baseline.
        self.write_session("alpha.jsonl")
        self.ingest_all()
        ingest_record = json.loads((self.state / "status.json").read_text())[
            "data"
        ]["stages"]["ingest"]

        damage_database(self.state)
        result = self.rebuild()
        self.assertEqual(result.returncode, 0, result.stderr)

        stages = json.loads((self.state / "status.json").read_text())["data"][
            "stages"
        ]
        self.assertEqual(stages["ingest"], ingest_record)
        rebuild_record = stages["rebuild"]
        self.assertTrue(rebuild_record["last_success"])
        self.assertEqual(rebuild_record["counts"]["sessions"], 1)
        self.assertGreater(rebuild_record["counts"]["events"], 0)
        self.assertGreater(rebuild_record["counts"]["observations"], 0)

    def test_json_summary_reports_the_reparse(self):
        self.write_session("alpha.jsonl")
        result = self.rebuild("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)["data"]
        self.assertEqual(payload["sessions"], 1)
        self.assertGreater(payload["events"], 0)
        self.assertGreater(payload["observations"], 0)

    def test_rebuild_takes_the_state_lock(self):
        # EC-10: --rebuild deletes and rewrites the database, so it queues
        # behind a holder instead of racing it.
        self.write_session("alpha.jsonl")
        with StateLock(self.state):
            result = self.rebuild("--json")
        self.assertEqual(result.returncode, 3, result.stderr)
        error = json.loads(result.stdout)["error"]
        self.assertEqual(error["code"], 3)
        self.assertEqual(error["message"].split()[0:3], ["lock", "held", "by"])
        self.assertIn(f"pid {os.getpid()}", error["message"])


if __name__ == "__main__":
    unittest.main()
