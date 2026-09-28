"""The end-to-end integration level over the fixture corpus (plan §10.1).

§10.1's Integration row is "a fixture corpus of synthetic transcripts
(clean, appended-between-runs, truncated, rewritten, secret-bearing,
injection-bearing) driven through real ingest → detect → rank → digest".
Every other level in that table already has its module; this one drives the
shared corpus in ``tests/fixtures/transcripts`` through the real CLI verbs
in the source layout the config's default globs describe —
``~/.claude/projects`` and ``~/.codex/sessions`` under a redirected ``HOME``
— with no ``--source``, ``--settle`` or ``--state-dir`` overrides, so the
default config resolution, the settle gate and the default state directory
all take part.  The pipeline exercised is the one the timers run: §5
Scenario 2's action is the plain ``twill ingest && twill detect && twill
digest``.

Scenario 2 (§5) is the archive-absent half.  Its setup deletes ``graph.db``
and moves ``~/agent-transcript-archive`` aside; its first pass criterion is
exit 0 from every verb, and its third — no archive path opened — is the
open-path audit hook every spawned verb self-installs (§10.2), baited here
by a copy of a real fixture planted inside the archive tree: a pipeline
that leaned on the archive's index would count the copy's session twice
and fail the byte-identity assertion below, if the hook let it get that
far.  The middle criterion, "the digest is byte-identical to a run with
the archive present", is the assertion only this module makes: two complete
pipelines over the same corpus, identical except for the archive's
presence, must render the same digest artifact byte for byte.

Determinism, because byte-identity is only meaningful when each run alone
is reproducible: every staged transcript carries a fixed mtime hours after
the last fixture event, so the default 2h settle gate admits the files
whenever the suite runs; the digest is pinned to the ISO week the corpus
timestamps live in (2026-09-20, the Sunday closing 2026-W38) rather than
the clock-derived default week; and the test config sets a retention long
enough that the fixtures never age out of observation derivation.  The
corpus's codex fixtures are deliberately prose-shaped, so the codex parser
yielding zero events for them is the contract-correct outcome recorded in
``tests/test_codex_corpus.py`` — the sessions, cursors and parse shapes are
still ingested, which is what the pipeline here asserts for that source.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "transcripts"
sys.path.insert(0, str(ROOT))

from twill_app import read_session  # noqa: E402

#: The ISO week every corpus timestamp falls in.  The digest is pinned to it
#: so the rendered week is a property of the corpus, not of the clock, and
#: the "previous week" the findings compare against is the empty one before
#: the corpus exists.
WEEK = "2026-W38"
WEEK_START = datetime(2026, 9, 14, tzinfo=timezone.utc)
WEEK_END = datetime(2026, 9, 21, tzinfo=timezone.utc)

# This record exists only in the unavailable archive.  If the lifecycle ever
# reads or copies the archive instead of the configured source globs, the
# marker will show up in TWILL's own state or artifacts.
ARCHIVE_BAIT_SESSION = "archive-only-001"
ARCHIVE_BAIT_MARKER = "archive-only-marker"

#: The mtime stamped on every staged transcript: hours after the last fixture
#: event (17:00Z) so the default settle window admits the file, and fixed so
#: the enumeration order — newest first, ties by path — is the same in every
#: run of this test, forever.
SETTLED_STAMP = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc).timestamp()

#: Where each corpus case is staged inside the redirected home.  The paths
#: match the default ``source_globs`` so ingest needs no ``--source``: the
#: scenario's point is that TWILL reads these trees directly (§5 Scenario 2).
STAGED = {
    "claude": {
        "clean": "clean.jsonl",
        "appended-between-runs": "appended.jsonl",
        "truncated-final-line": "truncated.jsonl",
        "rewritten-in-place": "rewritten.jsonl",
        "secret-bearing": "secret.jsonl",
        "injection-bearing": "injection.jsonl",
    },
    "codex": {
        "clean": "rollout-clean.jsonl",
        "appended-between-runs": "rollout-appended.jsonl",
        "truncated-final-line": "rollout-truncated.jsonl",
        "rewritten-in-place": "rollout-rewritten.jsonl",
        "secret-bearing": "rollout-secret.jsonl",
        "injection-bearing": "rollout-injection.jsonl",
    },
}


def fixture(source: str, scenario: str, snapshot: str | None = None) -> Path:
    """Resolve one corpus fixture; two-file cases name their snapshot."""

    relative = {
        ("claude", "appended-between-runs"): {
            "base": "claude/appended-between-runs/base.jsonl",
            "append": "claude/appended-between-runs/append.jsonl",
        },
        ("codex", "appended-between-runs"): {
            "base": "codex/appended-between-runs/base.jsonl",
            "append": "codex/appended-between-runs/append.jsonl",
        },
        ("claude", "rewritten-in-place"): {
            "before": "claude/rewritten-in-place/before.jsonl",
            "after": "claude/rewritten-in-place/after.jsonl",
        },
        ("codex", "rewritten-in-place"): {
            "before": "codex/rewritten-in-place/before.jsonl",
            "after": "codex/rewritten-in-place/after.jsonl",
        },
    }.get((source, scenario))
    if relative is not None:
        if snapshot is None:
            raise ValueError(f"{source}/{scenario} is a two-file case")
        return FIXTURE_ROOT / relative[snapshot]
    return FIXTURE_ROOT / source / f"{scenario}.jsonl"


def texts_of(path: Path) -> list[str]:
    """The normalized event texts of one fixture, in order."""

    return [event.text for event in read_session(path).events]


class FixtureCorpusEndToEndTests(unittest.TestCase):
    """One shared archive-present run; frozen facts; a fresh archive-absent run."""

    @classmethod
    def setUpClass(cls):
        cls._home_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._home_dir.cleanup)
        cls.home = Path(cls._home_dir.name)
        config_dir = cls.home / ".config" / "twill"
        config_dir.mkdir(parents=True)
        cls.artifacts = cls.home / "artifacts"
        (config_dir / "config.toml").write_text(
            f'artifacts_root = "{cls.artifacts}"\n'
            f'source_globs = ["{cls.home / ".claude" / "projects" / "**" / "*.jsonl"}", '
            f'"{cls.home / ".codex" / "sessions" / "**" / "*.jsonl"}"]\n'
            # Long enough that the 2026-09-20 fixtures never age out of
            # observation derivation, which filters on now - retention.
            'retention = "3650d"\n'
        )
        cls.claude_dir = cls.home / ".claude" / "projects" / "-workspace-demo"
        cls.codex_dir = cls.home / ".codex" / "sessions" / "2026" / "09" / "20"
        cls.state_db = cls.home / ".local" / "state" / "twill" / "twill.db"
        cls._plant_archive()
        cls.run_a = cls._pipeline()

    @classmethod
    def _plant_archive(cls) -> None:
        """Lay in the archive the scenario deletes: a graph.db and unique bait."""

        archive = cls.home / "agent-transcript-archive"
        (archive / "sessions").mkdir(parents=True)
        (archive / "graph.db").write_bytes(bytes(range(256)) * 8)
        bait = fixture("claude", "clean").read_text(encoding="utf-8")
        bait = bait.replace("claude-clean-001", ARCHIVE_BAIT_SESSION).replace(
            "Check the project status and summarize the next safe step.",
            ARCHIVE_BAIT_MARKER,
        )
        (archive / "sessions" / "archived.jsonl").write_text(bait, encoding="utf-8")

    @classmethod
    def _stage_corpus(cls) -> None:
        """Lay the corpus out as of the first run: base snapshots only."""

        shutil.rmtree(cls.home / ".claude", ignore_errors=True)
        shutil.rmtree(cls.home / ".codex", ignore_errors=True)
        for source, staging in STAGED.items():
            for scenario, name in staging.items():
                if scenario == "appended-between-runs":
                    data = fixture(source, scenario, "base").read_bytes()
                elif scenario == "rewritten-in-place":
                    data = fixture(source, scenario, "before").read_bytes()
                else:
                    data = fixture(source, scenario).read_bytes()
                cls._stage_by_source(source, name, data)
        # One rule document, so rank's coverage pass indexes a real corpus.
        memory = cls.claude_dir / "memory" / "MEMORY.md"
        memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_text("# Memory\n\n- Prefer precise staging paths.\n")

    @classmethod
    def _stage_by_source(cls, source: str, name: str, data: bytes) -> None:
        directory = cls.claude_dir if source == "claude" else cls.codex_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        os.utime(path, (SETTLED_STAMP, SETTLED_STAMP))

    @classmethod
    def _grow_corpus(cls) -> None:
        """Advance the corpus between the two ingests of one pipeline.

        The appended-between-runs files gain their second span at the same
        path (EC-02 resume), and the rewritten-in-place files are replaced
        by their after snapshot (EC-03 reparse).
        """

        for source, staging in STAGED.items():
            appended = cls._path_for(source, staging["appended-between-runs"])
            with appended.open("ab") as handle:
                handle.write(fixture(source, "appended-between-runs", "append").read_bytes())
            rewritten = cls._path_for(source, staging["rewritten-in-place"])
            rewritten.write_bytes(fixture(source, "rewritten-in-place", "after").read_bytes())
            for path in (appended, rewritten):
                os.utime(path, (SETTLED_STAMP, SETTLED_STAMP))

    @classmethod
    def _path_for(cls, source: str, name: str) -> Path:
        return (cls.claude_dir if source == "claude" else cls.codex_dir) / name

    @classmethod
    def run_cli(cls, *args):
        environment = {**os.environ, "HOME": str(cls.home)}
        environment.pop("TWILL_SOURCE_ROOTS", None)
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            env=environment,
            check=False,
            text=True,
            capture_output=True,
        )

    @classmethod
    def _pipeline(cls) -> dict[str, object]:
        """One full pass — ingest twice, detect, rank, digest, measure."""

        shutil.rmtree(cls.home / ".local", ignore_errors=True)
        shutil.rmtree(cls.artifacts / "digests", ignore_errors=True)
        cls._stage_corpus()
        ingest_first = cls.run_cli("ingest", "--limit", "50", "--json")
        cls._grow_corpus()
        ingest_second = cls.run_cli("ingest", "--limit", "50", "--json")
        detect = cls.run_cli("detect", "--window", "3650d", "--json")
        rank = cls.run_cli("rank", "--json")
        digest = cls.run_cli("digest", "--week", WEEK)
        measure = cls.run_cli("measure", "--json")
        digest_path = cls.artifacts / "digests" / f"{WEEK}.txt"
        return {
            "ingest_first": ingest_first,
            "ingest_second": ingest_second,
            "detect": detect,
            "rank": rank,
            "digest": digest,
            "measure": measure,
            "digest_path": digest_path,
            "digest_bytes": digest_path.read_bytes() if digest_path.is_file() else None,
            "database": cls._database_facts(),
        }

    @classmethod
    def _database_facts(cls) -> dict[str, object]:
        """The derived rows the assertions read, frozen at pipeline time."""

        connection = sqlite3.connect(cls.state_db)
        try:
            sessions = connection.execute(
                "SELECT session_id, source_kind FROM session ORDER BY session_id"
            ).fetchall()
            source_paths = [
                row[0]
                for row in connection.execute(
                    "SELECT source_path FROM session ORDER BY source_path"
                )
            ]
            events_per_session = dict(
                connection.execute(
                    "SELECT s.session_id, count(*) FROM transcript_event te "
                    "JOIN session s ON s.session_key = te.session_key "
                    "GROUP BY s.session_id"
                )
            )
            texts = {
                session_id: [row[0] for row in connection.execute(
                    "SELECT te.text FROM transcript_event te "
                    "JOIN session s ON s.session_key = te.session_key "
                    "WHERE s.session_id = ? ORDER BY te.event_id",
                    (session_id,),
                )]
                for (session_id, _) in sessions
            }
            observations = int(
                connection.execute("SELECT count(*) FROM observation").fetchone()[0]
            )
            cursors = connection.execute(
                "SELECT path, session_id, parse_errors FROM cursor ORDER BY path"
            ).fetchall()
            bounds = connection.execute(
                "SELECT min(ts_utc), max(ts_utc) FROM observation"
            ).fetchone()
        finally:
            connection.close()
        return {
            "sessions": sessions,
            "source_paths": source_paths,
            "events_per_session": events_per_session,
            "texts": texts,
            "observations": observations,
            "cursors": cursors,
            "first_ts": bounds[0],
            "last_ts": bounds[1],
        }

    # -- The pipeline itself (§10.1 Integration) --------------------------

    def test_every_verb_exits_zero_with_the_archive_present(self):
        for step in ("ingest_first", "ingest_second", "detect", "rank", "digest", "measure"):
            result = self.run_a[step]
            self.assertEqual(result.returncode, 0, f"{step}: {result.stderr}")

    def test_only_configured_sources_reach_twill_state(self):
        sessions = dict(self.run_a["database"]["sessions"])
        self.assertNotIn(ARCHIVE_BAIT_SESSION, sessions)
        for source_path in self.run_a["database"]["source_paths"]:
            path = Path(source_path)
            self.assertTrue(
                path.is_relative_to(self.claude_dir)
                or path.is_relative_to(self.codex_dir),
                source_path,
            )
            self.assertNotIn("agent-transcript-archive", source_path)

    def test_both_sources_and_every_scenario_were_ingested(self):
        sessions = dict(self.run_a["database"]["sessions"])
        self.assertEqual(len(sessions), 12)
        for source in ("claude", "codex"):
            staged = [f"{source}-clean-001", f"{source}-append-001",
                      f"{source}-truncated-001", f"{source}-rewrite-001",
                      f"{source}-secret-001", f"{source}-injection-001"]
            for session_id in staged:
                self.assertEqual(sessions.get(session_id), source, session_id)

    def test_claude_prose_turns_become_events_and_observations(self):
        database = self.run_a["database"]
        self.assertEqual(sum(database["events_per_session"].values()), 15)
        self.assertEqual(database["observations"], 15)
        for session_id, count in database["events_per_session"].items():
            if not session_id.startswith("claude-"):
                continue
            # Every fixture has at least one extractable event
            # (test_fixture_corpus), so every claude session must carry its
            # events into the store — no file silently dropped on the way.
            self.assertGreaterEqual(count, 1, session_id)

    def test_codex_fixtures_ingest_as_sessions_with_contract_zero_events(self):
        # The corpus's codex records are deliberately prose-shaped; zero
        # normalized events is their documented contract (test_codex_corpus).
        database = self.run_a["database"]
        codex_events = sum(
            count for session_id, count in database["events_per_session"].items()
            if session_id.startswith("codex-")
        )
        self.assertEqual(codex_events, 0)
        # ...while the codex side of every scenario still reached the store:
        # six sessions, and the truncated rollout's tail was counted, not lost.
        codex_cursors = {
            Path(path).name: errors
            for path, session_id, errors in database["cursors"]
            if session_id.startswith("codex-")
        }
        self.assertEqual(len(codex_cursors), 6)
        self.assertEqual(codex_cursors["rollout-truncated.jsonl"], 1)

    def test_appended_sessions_resume_without_duplicating_the_base(self):
        texts = self.run_a["database"]["texts"]["claude-append-001"]
        expected = texts_of(fixture("claude", "appended-between-runs", "base"))
        expected += texts_of(fixture("claude", "appended-between-runs", "append"))
        self.assertEqual(texts, expected)

    def test_rewritten_sessions_replace_their_previous_snapshot(self):
        texts = self.run_a["database"]["texts"]["claude-rewrite-001"]
        self.assertEqual(texts, texts_of(fixture("claude", "rewritten-in-place", "after")))
        before = texts_of(fixture("claude", "rewritten-in-place", "before"))
        self.assertTrue(set(before).isdisjoint(texts))

    def test_truncated_tail_is_counted_not_parsed(self):
        claude_cursors = {
            Path(path).name: errors
            for path, session_id, errors in self.run_a["database"]["cursors"]
            if session_id.startswith("claude-")
        }
        self.assertEqual(claude_cursors["truncated.jsonl"], 1)
        # The two complete records survived; the half-written one did not.
        self.assertEqual(
            self.run_a["database"]["texts"]["claude-truncated-001"],
            texts_of(fixture("claude", "truncated-final-line")),
        )

    def test_detect_runs_every_detector_cleanly_over_the_corpus(self):
        self.assertEqual(self.run_a["detect"].returncode, 0)
        outcomes = json.loads(self.run_a["detect"].stdout)["data"]["detectors"]
        self.assertTrue(outcomes)
        for outcome in outcomes:
            self.assertEqual(outcome["status"], "ok", outcome)
            # Nothing in this corpus recurs across distinct sessions, so a
            # clean sheet is the contract-correct cluster count here.
            self.assertEqual(outcome["clusters"], 0, outcome)

    def test_rank_indexes_the_rule_corpus_and_reports_coverage(self):
        self.assertEqual(self.run_a["rank"].returncode, 0)
        corpus = json.loads(self.run_a["rank"].stdout)["data"]["corpus"]
        self.assertEqual(corpus["documents"], 1)
        self.assertEqual(corpus["skipped"], 0)

    def test_digest_writes_the_pinned_week_artifact(self):
        self.assertEqual(self.run_a["digest"].returncode, 0)
        digest_path = self.run_a["digest_path"]
        self.assertTrue(digest_path.is_file(), digest_path)
        text = self.run_a["digest_bytes"].decode("utf-8")
        self.assertIn(f"week: {WEEK} (", text)
        self.assertIn("observations: 15 total, 15 current, 0 previous", text)

    def test_measure_runs_from_twill_state_without_archive_data(self):
        self.assertEqual(self.run_a["measure"].returncode, 0, self.run_a["measure"].stderr)
        measured = json.loads(self.run_a["measure"].stdout)
        self.assertEqual(measured["data"]["measurements"], [])
        self.assertNotIn(ARCHIVE_BAIT_MARKER.encode(), self._own_data_bytes())
        self.assertFalse(
            any(
                path.name in {"archived.jsonl", "graph.db"}
                for path in self._own_files()
            )
        )

    def test_corpus_timestamps_stay_inside_the_pinned_week(self):
        # A fixture added outside 2026-W38 would silently fall outside the
        # digest's bounds; fail here instead, naming the offending bounds.
        first = datetime.fromisoformat(self.run_a["database"]["first_ts"])
        last = datetime.fromisoformat(self.run_a["database"]["last_ts"])
        self.assertGreaterEqual(first, WEEK_START)
        self.assertLess(last, WEEK_END)

    # -- §5 Scenario 2: the archive-absent run ----------------------------

    @classmethod
    def _own_files(cls) -> tuple[Path, ...]:
        roots = (cls.state_db.parent, cls.artifacts)
        return tuple(
            path
            for root in roots
            if root.exists()
            for path in root.rglob("*")
            if path.is_file()
        )

    @classmethod
    def _own_data_bytes(cls) -> bytes:
        return b"\n".join(path.read_bytes() for path in cls._own_files())

    def test_scenario2_archive_absent_digest_is_byte_identical(self):
        """Delete graph.db, move the archive aside, re-run the full lifecycle."""

        archive = self.home / "agent-transcript-archive"
        (archive / "graph.db").unlink()
        os.rename(archive, self.home / "agent-transcript-archive.moved")

        run_b = self._pipeline()

        for step in ("ingest_first", "ingest_second", "detect", "rank", "digest", "measure"):
            result = run_b[step]
            self.assertEqual(result.returncode, 0, f"{step}: {result.stderr}")
        # The digest artifact was genuinely re-rendered by the degraded run,
        # not left over from the archive-present one.
        self.assertTrue(run_b["digest_path"].is_file())
        self.assertEqual(run_b["database"]["observations"], 15)
        self.assertIsNotNone(self.run_a["digest_bytes"])
        self.assertEqual(run_b["digest_bytes"], self.run_a["digest_bytes"])
        self.assertNotIn(ARCHIVE_BAIT_SESSION, dict(run_b["database"]["sessions"]))
        for source_path in run_b["database"]["source_paths"]:
            path = Path(source_path)
            self.assertTrue(
                path.is_relative_to(self.claude_dir)
                or path.is_relative_to(self.codex_dir),
                source_path,
            )
            self.assertNotIn("agent-transcript-archive", source_path)
        self.assertEqual(
            json.loads(run_b["measure"].stdout)["data"]["measurements"], []
        )
        self.assertNotIn(ARCHIVE_BAIT_MARKER.encode(), self._own_data_bytes())
        self.assertFalse(
            any(
                path.name in {"archived.jsonl", "graph.db"}
                for path in self._own_files()
            )
        )


if __name__ == "__main__":
    unittest.main()
