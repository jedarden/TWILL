"""D-10 (ICG gate gap) contract tests: matcher, detector, and the skip path.

Plan §6.5 and the Phase 2 entry pin three behaviors:

* **The matcher** — :func:`twill_catalog.materialize_gate_gaps` turns a valid
  catalog plus the observation corpus into ``gate_gap`` evidence rows: one
  per executed command that matched a deny-channel ``command_regex`` never
  event, replaced wholesale per read so evidence never spans two digests.
* **The detector** — ``D-10@1`` aggregates that evidence into one cluster per
  catalog ``(pack, id)`` attribution, with the window and executed-kind
  filters restated in SQL so the detector's meaning does not depend on the
  matcher's discipline.
* **No degradation (§6.5)** — without a readable catalog D-10 is *skipped*
  with a reason rather than run transcript-only or failed: the run exits
  zero, the skip is visible in the report, and the detector's clusters
  survive untouched until the input returns.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_catalog  # noqa: E402
import twill_detectors  # noqa: E402
import twill_explainer  # noqa: E402
import twill_ranker  # noqa: E402
import twill_schema  # noqa: E402
from twill_config import TwillConfig  # noqa: E402
from twill_contract import EXIT_SUCCESS  # noqa: E402

NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
D10_FIXTURE = ROOT / "tests" / "fixtures" / "detectors" / "phase2_d10.json"


def never(**overrides):
    """One wire-shaped never event, with any field overridable."""

    event = {
        "id": "never-one",
        "pack": "pack-a",
        "severity": "High",
        "tier": "tier1",
        "action": "deny",
        "destructive": True,
        "enabled": True,
        "check": "command_regex",
        "match": {"regex": r"^git\s+push\s+--force\b"},
        "explanation": "the event must not execute",
        "sanctioned_alternative": {"reason": "use the safe path instead"},
    }
    event.update(overrides)
    return event


def catalog_of(*events, always=()):
    """Parse a catalog document carrying exactly these never events."""

    ordered = sorted(events, key=lambda event: (event["pack"], event["id"]))
    ordered_always = sorted(always, key=lambda event: (event["pack"], event["id"]))
    content = {
        "format": twill_catalog.CATALOG_FORMAT,
        "never": ordered,
        "always": ordered_always,
    }
    digest = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return twill_catalog.parse_catalog(
        {
            "format": twill_catalog.CATALOG_FORMAT,
            "catalog_digest": digest,
            "icg_version": "0.2.0-test",
            "pack_source": {
                "origin": "installed",
                "root": "/etc/icg/packs",
                "trusted_ref": None,
            },
            "never": ordered,
            "always": ordered_always,
        }
    )


def seed_observation(
    connection,
    *,
    session_id,
    kind,
    command,
    days_ago=1.0,
    now=NOW,
    **columns,
):
    observed_at = (now - timedelta(days=days_ago)).isoformat()
    row: dict[str, object] = {
        "session_id": session_id,
        "ts_utc": observed_at,
        "ts_local": observed_at,
        "kind": kind,
        "command": command,
        **columns,
    }
    names = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    cursor = connection.execute(
        f"INSERT INTO observation({names}) VALUES ({placeholders})",
        tuple(row.values()),
    )
    return int(cursor.lastrowid)


def cluster_rows(connection, detector_id="D-10"):
    return connection.execute(
        "SELECT detector_id, key, sessions, events, first_seen, last_seen, state "
        "FROM cluster WHERE detector_id = ? ORDER BY key",
        (detector_id,),
    ).fetchall()


def gate_gap_rows(connection):
    return connection.execute(
        "SELECT pack, id, obs_id, catalog_digest FROM gate_gap ORDER BY pack, id, obs_id"
    ).fetchall()


class GateGapMatcherTests(unittest.TestCase):
    """The catalog reader's evidence materialization for D-10."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.connection = twill_schema.connect(Path(self._temporary.name) / "state")
        self.addCleanup(self.connection.close)

    def test_executed_matches_in_window_become_evidence_rows(self):
        matched = seed_observation(
            self.connection,
            session_id="s1",
            kind="run_succeeded",
            command="git push --force origin main",
        )
        failed = seed_observation(
            self.connection,
            session_id="s2",
            kind="run_failed",
            command="git push --force origin other",
        )
        seed_observation(
            self.connection, session_id="s1", kind="run", command="git status"
        )
        seed_observation(
            self.connection,
            session_id="s1",
            kind="run",
            command="git push --force origin unknown-result",
        )
        seed_observation(
            self.connection,
            session_id="s1",
            kind="tool_error",
            command="git push --force via another tool",
        )
        seed_observation(
            self.connection,
            session_id="s1",
            kind="run_succeeded",
            command="git push --force outside the window",
            days_ago=40.0,
        )
        catalog = catalog_of(never())

        written = twill_catalog.materialize_gate_gaps(
            self.connection, catalog, window_days=30, now=NOW
        )

        self.assertEqual(written, 2)
        self.assertEqual(
            gate_gap_rows(self.connection),
            [
                ("pack-a", "never-one", matched, catalog.catalog_digest),
                ("pack-a", "never-one", failed, catalog.catalog_digest),
            ],
        )

    def test_only_enforced_blocking_command_regexes_can_gap(self):
        # Every variant below carries a pattern the seeded command matches,
        # yet none may become evidence: a disabled rule is visibility, not
        # enforcement; updated_input/additional_context answer without a
        # deny; content_regex and predicate are not command checks.
        seed_observation(
            self.connection,
            session_id="s1",
            kind="run_succeeded",
            command="git push --force origin main",
        )
        events = [
            never(id="disabled", enabled=False, match={"regex": r"^git"}),
            never(
                id="rewritten",
                action="updated_input",
                match={"regex": r"^git"},
                # The rewrite channel owes a rewrite (contract pin): an
                # updated_input event without one is not a valid document.
                sanctioned_alternative={
                    "reason": "use the safe path instead",
                    "rewrite": "git push --lease origin main",
                },
            ),
            never(
                id="annotated",
                action="additional_context",
                match={"regex": r"^git"},
            ),
            never(id="content", check="content_regex", match={"regex": r"git"}),
            never(id="opaque", check="predicate", match={"predicate": "is_fatal"}),
        ]

        written = twill_catalog.materialize_gate_gaps(
            self.connection, catalog_of(*events), window_days=30, now=NOW
        )

        self.assertEqual(written, 0)
        self.assertEqual(gate_gap_rows(self.connection), [])

    def test_coverage_names_unobservable_enabled_denials(self):
        catalog = catalog_of(
            never(),
            never(id="content", check="content_regex", match={"regex": "secret"}),
            never(id="opaque", check="predicate", match={"predicate": "is_fatal"}),
            never(id="disabled", enabled=False),
        )
        self.assertEqual(twill_catalog.gate_gap_coverage(catalog), (1, 2))

    def test_same_pack_always_exception_prevents_false_gap(self):
        obs_id = seed_observation(
            self.connection,
            session_id="s1",
            kind="run_succeeded",
            command="git push --force origin main",
        )
        catalog = catalog_of(
            never(),
            never(pack="pack-b", id="never-two"),
            always=(
                {
                    "id": "allow-main",
                    "pack": "pack-a",
                    "check": "command_regex",
                    "match": {"regex": r"^git push --force origin main$"},
                },
            ),
        )

        written = twill_catalog.materialize_gate_gaps(
            self.connection, catalog, window_days=30, now=NOW
        )

        self.assertEqual(written, 1)
        self.assertEqual(
            gate_gap_rows(self.connection),
            [("pack-b", "never-two", obs_id, catalog.catalog_digest)],
        )

    def test_opaque_allow_check_suppresses_ambiguous_pack(self):
        seed_observation(
            self.connection,
            session_id="s1",
            kind="run_succeeded",
            command="git push --force origin main",
        )
        catalog = catalog_of(
            never(),
            always=(
                {
                    "id": "opaque-allow",
                    "pack": "pack-a",
                    "check": "predicate",
                    "match": {"predicate": "allow_known_case"},
                },
            ),
        )

        self.assertEqual(twill_catalog.gate_gap_coverage(catalog), (0, 1))
        self.assertEqual(
            twill_catalog.materialize_gate_gaps(
                self.connection, catalog, window_days=30, now=NOW
            ),
            0,
        )

    def test_unevaluable_regex_is_a_named_error(self):
        seed_observation(
            self.connection,
            session_id="s1",
            kind="run_succeeded",
            command="git push --force origin main",
        )
        with self.assertRaises(twill_catalog.CatalogError) as context:
            twill_catalog.materialize_gate_gaps(
                self.connection,
                catalog_of(never(id="broken", match={"regex": "("})),
                window_days=30,
                now=NOW,
            )
        self.assertIn("pack-a/broken", str(context.exception))

    def test_materialization_replaces_evidence_wholesale(self):
        obs_id = seed_observation(
            self.connection,
            session_id="s1",
            kind="run_succeeded",
            command="git push --force origin main",
        )
        first = twill_catalog.materialize_gate_gaps(
            self.connection, catalog_of(never()), window_days=30, now=NOW
        )
        self.assertEqual(first, 1)
        second_catalog = catalog_of(
            never(pack="pack-b", id="never-two", match={"regex": r"^git\s+push"})
        )
        second = twill_catalog.materialize_gate_gaps(
            self.connection, second_catalog, window_days=30, now=NOW
        )

        self.assertEqual(second, 1)
        self.assertEqual(
            gate_gap_rows(self.connection),
            [("pack-b", "never-two", obs_id, second_catalog.catalog_digest)],
        )

    def test_window_must_be_whole_positive_days(self):
        catalog = catalog_of(never())
        for bad in (0, -1, 1.5, True, "30"):
            with self.assertRaises(ValueError):
                twill_catalog.materialize_gate_gaps(
                    self.connection, catalog, window_days=bad, now=NOW
                )


class GateGapDetectorTests(unittest.TestCase):
    """D-10@1 over the evidence table, through the isolation runner."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.connection = twill_schema.connect(Path(self._temporary.name) / "state")
        self.addCleanup(self.connection.close)

    def test_fixture_has_known_evidence_and_cluster_counts(self):
        fixture = json.loads(D10_FIXTURE.read_text())
        catalog = twill_catalog.parse_catalog(fixture["catalog"])
        self.connection.executemany(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, command) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    row["session_id"],
                    row["ts_utc"],
                    row["ts_utc"],
                    row["kind"],
                    row["command"],
                )
                for row in fixture["observations"]
            ],
        )
        self.connection.commit()

        written = twill_catalog.materialize_gate_gaps(
            self.connection,
            catalog,
            window_days=fixture["window_days"],
            now=datetime.fromisoformat(fixture["now"]),
        )
        report = twill_detectors.run_detectors(
            self.connection,
            window_days=fixture["window_days"],
            registry=(twill_detectors.GATE_GAP,),
            now=fixture["now"],
        )

        expected = fixture["expected"]
        self.assertEqual(written, expected["evidence_rows"])
        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        self.assertEqual(report.outcomes[0].clusters, expected["clusters"])
        actual = {
            key: {"sessions": sessions, "events": events}
            for key, sessions, events in self.connection.execute(
                "SELECT key, sessions, events FROM cluster "
                "WHERE detector_id = 'D-10' ORDER BY key"
            )
        }
        self.assertEqual(actual, expected["rows"])

    def _seed_gap(self, *, pack, event_id, session_id, command, days_ago=1.0):
        obs_id = seed_observation(
            self.connection,
            session_id=session_id,
            kind="run_succeeded",
            command=command,
            days_ago=days_ago,
        )
        self.connection.execute(
            "INSERT INTO gate_gap(pack, id, obs_id, catalog_digest) "
            "VALUES (?, ?, ?, 'digest-a')",
            (pack, event_id, obs_id),
        )
        return obs_id

    def test_one_cluster_per_catalog_identity(self):
        self._seed_gap(
            pack="git", event_id="git-force-push", session_id="s1",
            command="git push --force origin main", days_ago=2.0,
        )
        self._seed_gap(
            pack="git", event_id="git-force-push", session_id="s2",
            command="git push --force origin other", days_ago=1.0,
        )
        self._seed_gap(
            pack="secrets", event_id="credential-in-argv", session_id="s1",
            command="bao kv put secret/x token=literal", days_ago=1.0,
        )

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.GATE_GAP,),
            now=NOW,
        )

        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        rows = {
            row[1]: row
            for row in cluster_rows(self.connection)
        }
        self.assertEqual(
            sorted(rows),
            ["gate-gap:git:git-force-push", "gate-gap:secrets:credential-in-argv"],
        )
        git_row = rows["gate-gap:git:git-force-push"]
        self.assertEqual(git_row[2], 2)  # sessions
        self.assertEqual(git_row[3], 2)  # events
        self.assertEqual(
            git_row[4], (NOW - timedelta(days=2.0)).isoformat()
        )
        self.assertEqual(
            git_row[5], (NOW - timedelta(days=1.0)).isoformat()
        )
        run = self.connection.execute(
            "SELECT full_id, last_status, clusters FROM detector_run"
        ).fetchall()
        self.assertEqual(run, [("D-10@1", "ok", 2)])

    def test_window_and_executed_kind_filters_live_in_the_sql(self):
        self._seed_gap(
            pack="git", event_id="git-force-push", session_id="s1",
            command="git push --force origin main", days_ago=1.0,
        )
        stale = seed_observation(
            self.connection,
            session_id="s1",
            kind="run_succeeded",
            command="git push --force origin old",
            days_ago=40.0,
        )
        self.connection.execute(
            "INSERT INTO gate_gap(pack, id, obs_id, catalog_digest) "
            "VALUES ('git', 'git-force-push', ?, 'digest-a')",
            (stale,),
        )
        not_run = seed_observation(
            self.connection,
            session_id="s1",
            kind="file_read",
            command=None,
            days_ago=1.0,
        )
        self.connection.execute(
            "INSERT INTO gate_gap(pack, id, obs_id, catalog_digest) "
            "VALUES ('git', 'git-force-push', ?, 'digest-a')",
            (not_run,),
        )
        unknown_result = seed_observation(
            self.connection,
            session_id="s1",
            kind="run",
            command="git push --force origin maybe",
            days_ago=1.0,
        )
        self.connection.execute(
            "INSERT INTO gate_gap(pack, id, obs_id, catalog_digest) "
            "VALUES ('git', 'git-force-push', ?, 'digest-a')",
            (unknown_result,),
        )
        empty_command = seed_observation(
            self.connection,
            session_id="s1",
            kind="run_succeeded",
            command="",
            days_ago=1.0,
        )
        self.connection.execute(
            "INSERT INTO gate_gap(pack, id, obs_id, catalog_digest) "
            "VALUES ('git', 'git-force-push', ?, 'digest-a')",
            (empty_command,),
        )

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.GATE_GAP,),
            now=NOW,
        )

        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        rows = cluster_rows(self.connection)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 1)
        self.assertEqual(rows[0][3], 1)
        # All five evidence rows remain in the table; SQL drops the stale,
        # unknown-result, non-executed, and empty-command observations.
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM gate_gap").fetchone()[0],
            5,
        )

    def test_refresh_deletes_stale_open_clusters_but_keeps_review_state(self):
        self._seed_gap(
            pack="git", event_id="git-force-push", session_id="s1",
            command="git push --force origin main",
        )
        twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.GATE_GAP,),
            now=NOW,
        )
        self.connection.execute(
            "UPDATE cluster SET state = 'dismissed', dismiss_reason = 'gate fixed' "
            "WHERE detector_id = 'D-10'"
        )
        self.connection.commit()
        self.connection.execute("DELETE FROM gate_gap")

        twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.GATE_GAP,),
            now=NOW,
        )

        rows = cluster_rows(self.connection)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][6], "dismissed")

    def test_gate_gap_is_reported_but_never_a_lesson_candidate(self):
        self._seed_gap(
            pack="git", event_id="git-force-push", session_id="s1",
            command="git push --force origin main",
        )
        twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.GATE_GAP,),
            now=NOW,
        )
        report = twill_ranker.rank_clusters(self.connection, as_of=NOW)
        self.assertEqual(len(report.all_clusters), 1)
        self.assertEqual(report.all_clusters[0].detector_id, "D-10")
        self.assertEqual(report.clusters, ())
        self.assertFalse(report.all_clusters[0].new_lesson_candidate)
        self.assertEqual(twill_explainer.load_candidate_clusters(self.connection), ())


class GateGapSkipTests(unittest.TestCase):
    """A D-10 that cannot run is skipped and reported, never failed (§6.5)."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.connection = twill_schema.connect(Path(self._temporary.name) / "state")
        self.addCleanup(self.connection.close)

    def test_skip_is_reported_without_failing_the_run(self):
        # Two sessions: D-01's own contract needs two before it clusters, so
        # the healthy-detector-still-committed check exercises a real commit.
        for session in ("s1", "s2"):
            seed_observation(
                self.connection,
                session_id=session,
                kind="run_failed",
                program="sqlite3",
                command="sqlite3 --dump",
                signature="sqlite3: command not found",
                sig_hash="sha",
            )
        missing_binary = twill_detectors.REGISTRY[0]
        reason = "ICG catalog absent: catalog command unavailable: no icg"

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(missing_binary, twill_detectors.GATE_GAP),
            skip={"D-10": reason},
            now=NOW,
        )

        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        self.assertEqual(report.failed, ())
        outcome = report.outcomes[-1]
        self.assertEqual(outcome.detector_id, "D-10")
        self.assertEqual(outcome.status, twill_detectors.STATUS_SKIPPED)
        self.assertEqual(outcome.reason, reason)
        self.assertIsNone(outcome.error)
        self.assertEqual(
            cluster_rows(self.connection, "D-10"),
            [],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM detector_run WHERE detector_id = 'D-10'"
            ).fetchone()[0],
            0,
        )
        # The healthy detector still committed.
        self.assertEqual(
            [row[1] for row in cluster_rows(self.connection, "D-01")],
            ["command-not-found:sqlite3"],
        )

    def test_skip_leaves_existing_clusters_untouched(self):
        seed_observation(
            self.connection,
            session_id="s1",
            kind="run_succeeded",
            command="git push --force origin main",
        )
        self.connection.execute(
            "INSERT INTO gate_gap(pack, id, obs_id, catalog_digest) "
            "VALUES ('git', 'git-force-push', "
            "(SELECT max(obs_id) FROM observation), 'digest-a')"
        )
        twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.GATE_GAP,),
            now=NOW,
        )
        self.connection.execute("DELETE FROM gate_gap")
        self.connection.commit()

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=60,
            registry=(twill_detectors.GATE_GAP,),
            skip={"D-10": "ICG catalog absent"},
            now=NOW,
        )

        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        rows = cluster_rows(self.connection)
        self.assertEqual(len(rows), 1)
        # Not refreshed to the skipped run's window: untouched means exactly
        # what the last successful run wrote.
        self.assertEqual(rows[0][2], 1)
        stamped = self.connection.execute(
            "SELECT window_days FROM detector_run WHERE detector_id = 'D-10'"
        ).fetchone()
        self.assertEqual(stamped[0], 30)


class DetectCommandGateGapTests(unittest.TestCase):
    """The detect verb's catalog wiring end to end."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.state_dir = Path(self._temporary.name) / "state"
        self.artifacts = Path(self._temporary.name) / "artifacts"
        config = TwillConfig(artifacts_root=self.artifacts)
        self._config_patch = mock.patch.object(twill_app, "load_config")
        load_config = self._config_patch.start()
        load_config.return_value = config
        self.addCleanup(self._config_patch.stop)

    def _args(self, **overrides):
        values = {
            "window": None,
            "detector": None,
            "state_dir": str(self.state_dir),
            "json": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def _catalog_result(self, catalog):
        return twill_catalog.CatalogRead(
            "available" if catalog is not None else "absent", catalog=catalog
        )

    def test_detect_skips_d10_when_the_catalog_is_unavailable(self):
        connection = twill_schema.connect(self.state_dir)
        seed_observation(
            connection,
            session_id="s1",
            kind="run_failed",
            program=None,
            command="sqlite3 --dump",
        )
        connection.close()
        absent = twill_catalog.CatalogRead(
            "absent", reason="catalog command unavailable: icg not installed"
        )
        registry = (twill_detectors.REGISTRY[0], twill_detectors.GATE_GAP)
        captured = StringIO()
        with mock.patch.object(twill_detectors, "REGISTRY", registry):
            with mock.patch.object(
                twill_catalog, "load_and_record", return_value=(absent, None)
            ):
                with redirect_stdout(captured):
                    code = twill_app.detect_command(self._args())

        self.assertEqual(code, EXIT_SUCCESS)
        envelope = json.loads(captured.getvalue())
        payload = envelope["data"]
        outcomes = {entry["detector_id"]: entry for entry in payload["detectors"]}
        self.assertEqual(outcomes["D-01"]["status"], "ok")
        self.assertEqual(outcomes["D-10"]["status"], "skipped")
        self.assertIn("absent", outcomes["D-10"]["reason"])
        self.assertIn("icg not installed", outcomes["D-10"]["reason"])
        self.assertNotIn("gate_gap_evidence", payload["catalog"])
        self.assertTrue(any("D-10" in warning for warning in envelope["warnings"]))
        connection = sqlite3.connect(self.state_dir / "twill.db")
        try:
            self.assertEqual(cluster_rows(connection), [])
        finally:
            connection.close()

    def test_detect_runs_d10_when_the_catalog_is_available(self):
        catalog = catalog_of(never())
        observation = twill_catalog.CatalogObservation(
            "first_seen",
            format=catalog.format,
            digest=catalog.catalog_digest,
        )
        registry = (twill_detectors.REGISTRY[0], twill_detectors.GATE_GAP)
        captured = StringIO()
        with mock.patch.object(twill_detectors, "REGISTRY", registry):
            with mock.patch.object(
                twill_catalog,
                "load_and_record",
                return_value=(self._catalog_result(catalog), observation),
            ):
                with redirect_stdout(captured):
                    twill_app.detect_command(self._args())

        payload = json.loads(captured.getvalue())["data"]
        outcomes = {entry["detector_id"]: entry for entry in payload["detectors"]}
        self.assertEqual(outcomes["D-10"]["status"], "ok")
        self.assertEqual(payload["catalog"]["gate_gap_evidence"], 0)
        self.assertEqual(outcomes["D-10"]["clusters"], 0)

        # With a matching executed command in the corpus the same wiring
        # reports the gap itself.
        connection = twill_schema.connect(self.state_dir)
        seed_observation(
            connection,
            session_id="s1",
            kind="run_succeeded",
            command="git push --force origin main",
        )
        connection.commit()
        connection.close()
        captured = StringIO()
        with mock.patch.object(twill_detectors, "REGISTRY", registry):
            with mock.patch.object(
                twill_catalog,
                "load_and_record",
                return_value=(self._catalog_result(catalog), observation),
            ):
                with redirect_stdout(captured):
                    code = twill_app.detect_command(self._args())

        self.assertEqual(code, EXIT_SUCCESS)
        payload = json.loads(captured.getvalue())["data"]
        outcomes = {entry["detector_id"]: entry for entry in payload["detectors"]}
        self.assertEqual(outcomes["D-10"]["status"], "ok")
        self.assertEqual(outcomes["D-10"]["clusters"], 1)
        self.assertEqual(payload["catalog"]["gate_gap_evidence"], 1)
        connection = sqlite3.connect(self.state_dir / "twill.db")
        try:
            rows = cluster_rows(connection)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], "gate-gap:pack-a:never-one")
        finally:
            connection.close()

    def test_detect_skips_d10_when_the_catalog_cannot_be_evaluated(self):
        catalog = catalog_of(never(id="broken", match={"regex": "("}))
        registry = (twill_detectors.GATE_GAP,)
        captured = StringIO()
        with mock.patch.object(twill_detectors, "REGISTRY", registry):
            with mock.patch.object(
                twill_catalog,
                "load_and_record",
                return_value=(self._catalog_result(catalog), None),
            ):
                with redirect_stdout(captured):
                    code = twill_app.detect_command(self._args())

        self.assertEqual(code, EXIT_SUCCESS)
        payload = json.loads(captured.getvalue())["data"]
        outcome = payload["detectors"][0]
        self.assertEqual(outcome["detector_id"], "D-10")
        self.assertEqual(outcome["status"], "skipped")
        self.assertIn("could not be evaluated", outcome["reason"])
        self.assertIn("pack-a/broken", outcome["reason"])

    def test_detect_reports_rules_it_cannot_evaluate(self):
        catalog = catalog_of(
            never(),
            never(id="content", check="content_regex", match={"regex": "secret"}),
        )
        captured = StringIO()
        with mock.patch.object(twill_detectors, "REGISTRY", (twill_detectors.GATE_GAP,)):
            with mock.patch.object(
                twill_catalog,
                "load_and_record",
                return_value=(self._catalog_result(catalog), None),
            ):
                with redirect_stdout(captured):
                    code = twill_app.detect_command(self._args())

        self.assertEqual(code, EXIT_SUCCESS)
        envelope = json.loads(captured.getvalue())
        self.assertEqual(envelope["data"]["catalog"]["gate_gap_events_evaluated"], 1)
        self.assertEqual(envelope["data"]["catalog"]["gate_gap_events_unobservable"], 1)
        self.assertTrue(any("cannot evaluate 1" in item for item in envelope["warnings"]))

    def test_detect_reports_the_skip_on_the_human_surface(self):
        absent = twill_catalog.CatalogRead("absent", reason="no icg on PATH")
        registry = (twill_detectors.GATE_GAP,)
        captured = StringIO()
        with mock.patch.object(twill_detectors, "REGISTRY", registry):
            with mock.patch.object(
                twill_catalog, "load_and_record", return_value=(absent, None)
            ):
                with redirect_stdout(captured):
                    code = twill_app.detect_command(self._args(json=False))

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn("D-10@1: skipped — ICG catalog absent", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
