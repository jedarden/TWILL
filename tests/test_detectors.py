"""Contract tests for the versioned detector registry (plan §4, §8.1 EC-12, §8.2).

Three behaviors are pinned:

* **Naming and versioning (§4, EC-12)** — a detector is ``D-NN`` at an
  integer version, one active version per name, and a version's semantics
  (its SQL) are fingerprinted and stamped into ``detector_run`` the first
  time that version commits clusters.
* **Per-detector isolation (§8.2)** — a detector whose SQL errors or whose
  output breaks the column contract is rolled back, skipped and reported
  while the others still run and commit; no partial cluster writes survive.
* **Semantics discipline (EC-12)** — changing a detector's SQL under the same
  version is refused; bumping the version redefines the series openly, with
  the run record keeping every version that ever committed.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_detectors  # noqa: E402
import twill_schema  # noqa: E402
from twill_config import TwillConfig  # noqa: E402
from twill_contract import (  # noqa: E402
    EXIT_RUNTIME_ERROR,
    EXIT_SUCCESS,
    EXIT_VALIDATION_FAILURE,
    CliError,
)


def program_failure_sql(group: str = "program", kind: str = "run_failed") -> str:
    """A realistic detector query: group a failure kind by one column."""

    return f"""
        SELECT {group} AS key,
               count(DISTINCT session_id) AS sessions,
               count(*) AS events,
               min(ts_utc) AS first_seen,
               max(ts_utc) AS last_seen
        FROM observation
        WHERE kind = '{kind}' AND ts_utc >= :window_start_utc
        GROUP BY {group}
    """


def make_detector(
    detector_id: str = "D-01",
    version: int = 1,
    sql: str | None = None,
    description: str = "groups run failures",
    session_hits_sql: str | None = None,
    week_hits_sql: str | None = None,
) -> twill_detectors.Detector:
    return twill_detectors.Detector(
        detector_id,
        version,
        description,
        sql or program_failure_sql(),
        session_hits_sql,
        week_hits_sql,
    )


def seed_observation(
    connection: sqlite3.Connection,
    *,
    program: str = "sqlite3",
    session_id: str = "session-a",
    days_ago: float = 1.0,
    kind: str = "run_failed",
    tool: str | None = None,
    command: str | None = None,
    signature: str | None = None,
    sig_hash: str | None = None,
    excerpt: str | None = None,
) -> None:
    observed_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    stored_excerpt = excerpt if excerpt is not None else (
        f"{program}: command not found" if program else "tool failed"
    )
    stored_sig_hash = sig_hash
    if stored_sig_hash is None and signature is not None:
        stored_sig_hash = twill_app.h12(signature)
    connection.execute(
        "INSERT INTO observation(session_id, ts_utc, ts_local, kind, program, "
        "command, tool, signature, sig_hash, excerpt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            observed_at.isoformat(),
            observed_at.isoformat(),
            kind,
            program,
            command if command is not None else (
                f"{program} --flag" if program else None
            ),
            tool,
            signature,
            stored_sig_hash,
            stored_excerpt,
        ),
    )
    connection.commit()


def cluster_rows(connection: sqlite3.Connection, detector_id: str) -> list[tuple]:
    return connection.execute(
        "SELECT detector_id, key, window_days, sessions, events, first_seen, "
        "last_seen, score, covered_by, state FROM cluster WHERE detector_id = ? "
        "ORDER BY key",
        (detector_id,),
    ).fetchall()


def run_row(connection: sqlite3.Connection, detector_id: str, version: int):
    return connection.execute(
        "SELECT detector_id, version, full_id, semantics_sha, first_run_at, "
        "last_run_at, last_status, last_error, clusters, window_days, "
        "backtest_sha FROM detector_run WHERE detector_id = ? AND version = ?",
        (detector_id, version),
    ).fetchone()


class RegistryContractTests(unittest.TestCase):
    """§4 and EC-12: the registry's naming, versioning and SQL discipline."""

    def test_full_id_is_name_at_version(self):
        self.assertEqual(make_detector("D-01", 2).full_id, "D-01@2")
        self.assertEqual(make_detector("D-10", 1).full_id, "D-10@1")

    def test_semantics_sha_ignores_whitespace_only_edits(self):
        sql = program_failure_sql()
        reflowed = "  " + " \n ".join(sql.split()) + " "
        self.assertEqual(
            make_detector(sql=sql).semantics_sha,
            make_detector(sql=reflowed).semantics_sha,
        )

    def test_semantics_sha_tracks_sql_and_version(self):
        base = make_detector()
        self.assertNotEqual(
            base.semantics_sha, make_detector(sql=program_failure_sql("command")).semantics_sha
        )
        self.assertNotEqual(base.semantics_sha, make_detector(version=2).semantics_sha)

    def test_attribution_hash_tracks_only_session_hit_semantics(self):
        base = make_detector()
        self.assertIsNone(base.attribution_sha)
        hit_sql = "SELECT program AS key, session_id FROM observation"
        with_hits = make_detector(session_hits_sql=hit_sql)
        self.assertIsNotNone(with_hits.attribution_sha)
        self.assertEqual(base.semantics_sha, with_hits.semantics_sha)
        self.assertNotEqual(
            with_hits.attribution_sha,
            make_detector(
                session_hits_sql=hit_sql.replace("session_id", "session_id || '-x'")
            ).attribution_sha,
        )

    def test_backtest_hash_tracks_only_week_semantics(self):
        base = make_detector()
        week_sql = "SELECT program AS key, strftime('%G-W%V', ts_utc) AS week FROM observation"
        with_week = make_detector(week_hits_sql=week_sql)
        self.assertIsNone(base.backtest_sha)
        self.assertIsNotNone(with_week.backtest_sha)
        self.assertEqual(base.semantics_sha, with_week.semantics_sha)
        self.assertNotEqual(
            with_week.backtest_sha,
            make_detector(
                week_hits_sql=week_sql.replace("ts_utc", "datetime(ts_utc)")
            ).backtest_sha,
        )

    def test_build_registry_rejects_malformed_session_hit_sql(self):
        for bad in ("", "   ", "UPDATE cluster_session SET session_id = 'x'",
                    "SELECT 1; SELECT 2"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                twill_detectors.build_registry(make_detector(session_hits_sql=bad))

    def test_build_registry_rejects_malformed_week_hit_sql(self):
        for bad in ("", "   ", "UPDATE cluster_session SET session_id = 'x'",
                    "SELECT 1; SELECT 2"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                twill_detectors.build_registry(make_detector(week_hits_sql=bad))

    def test_build_registry_rejects_malformed_ids(self):
        for bad in ("D-1", "d-01", "D01", "D-01x", "X-01", "D-", "01"):
            with self.assertRaises(ValueError, msg=bad):
                twill_detectors.build_registry(make_detector(bad))

    def test_build_registry_rejects_nonpositive_versions(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                twill_detectors.build_registry(make_detector("D-01", bad))

    def test_build_registry_rejects_empty_prose_and_sql(self):
        with self.assertRaises(ValueError):
            twill_detectors.build_registry(make_detector(description="  "))
        with self.assertRaises(ValueError):
            twill_detectors.build_registry(make_detector(sql="   "))

    def test_build_registry_rejects_non_select_sql(self):
        for bad in (
            "INSERT INTO cluster SELECT 1",
            "UPDATE cluster SET state = 'open'",
            "DELETE FROM observation",
            "PRAGMA journal_mode = WAL",
            "CREATE TABLE x(id INTEGER)",
        ):
            with self.assertRaises(ValueError, msg=bad):
                twill_detectors.build_registry(make_detector(sql=bad))

    def test_build_registry_rejects_multiple_statements(self):
        with self.assertRaises(ValueError):
            twill_detectors.build_registry(
                make_detector(sql=program_failure_sql() + "; SELECT 1")
            )

    def test_one_active_version_per_name(self):
        with self.assertRaises(ValueError) as context:
            twill_detectors.build_registry(
                make_detector("D-01", 1), make_detector("D-01", 2)
            )
        self.assertIn("EC-12", str(context.exception))

    def test_shipped_registry_satisfies_the_contract(self):
        # The shipped catalog is whatever the detector beads have landed; it
        # must always satisfy the same discipline as any test registry.
        self.assertEqual(
            twill_detectors.build_registry(*twill_detectors.REGISTRY),
            twill_detectors.REGISTRY,
        )

    def test_select_detectors_rejects_unknown_names(self):
        with self.assertRaises(ValueError) as context:
            twill_detectors.select_detectors(
                (make_detector("D-01"),), ("D-02",)
            )
        self.assertIn("unknown detector", str(context.exception))

    def test_select_detectors_keeps_order_and_deduplicates(self):
        registry = (make_detector("D-01"), make_detector("D-02"))
        selected = twill_detectors.select_detectors(
            registry, ("D-02", "D-01", "D-02")
        )
        self.assertEqual(
            tuple(detector.detector_id for detector in selected), ("D-02", "D-01")
        )


class DetectorRunTests(unittest.TestCase):
    """The isolation runner over a real state database (§8.2, EC-12)."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.state_dir = Path(self._temporary.name) / "state"
        self.connection = twill_schema.connect(self.state_dir)
        self.addCleanup(self.connection.close)

    def test_run_writes_clusters_and_stamps_the_version(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        seed_observation(self.connection, program="sqlite3", session_id="s2")
        seed_observation(self.connection, program="bf", session_id="s1", days_ago=40)

        detector = make_detector("D-01", 1)
        report = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(detector,)
        )

        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        self.assertEqual(
            [(o.full_id, o.status, o.clusters) for o in report.outcomes],
            [("D-01@1", "ok", 1)],
        )
        # The 40-day-old observation is outside the 30-day window; the
        # 30-day window's boundary is derived from the same clock the seeds
        # use, so only 'bf' is excluded.
        rows = cluster_rows(self.connection, "D-01")
        self.assertEqual([row[1] for row in rows], ["sqlite3"])
        self.assertEqual(rows[0][2], 30)
        self.assertEqual(rows[0][3], 2)  # sessions
        self.assertEqual(rows[0][4], 2)  # events
        self.assertEqual(rows[0][7], 0.0)  # score: the ranker owns it
        self.assertEqual(rows[0][9], "open")

        record = run_row(self.connection, "D-01", 1)
        self.assertIsNotNone(record)
        self.assertEqual(record[2], "D-01@1")
        self.assertEqual(record[3], detector.semantics_sha)
        self.assertEqual(record[6], "ok")
        self.assertIsNone(record[7])
        self.assertEqual(record[8], 1)
        self.assertEqual(record[9], 30)

    def test_session_hits_commit_and_refresh_with_the_cluster(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        seed_observation(self.connection, program="sqlite3", session_id="s2")
        hits_sql = (
            "SELECT program AS key, session_id FROM observation "
            "WHERE kind = 'run_failed' AND ts_utc >= :window_start_utc "
            "GROUP BY program, session_id"
        )
        cluster_sql = program_failure_sql().replace(
            "GROUP BY program",
            "GROUP BY program HAVING count(DISTINCT session_id) >= 2",
        )
        detector = make_detector(sql=cluster_sql, session_hits_sql=hits_sql)

        first = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(detector,)
        )

        self.assertEqual(first.exit_code, EXIT_SUCCESS)
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id, key, session_id FROM cluster_session "
                "ORDER BY session_id"
            ).fetchall(),
            [("D-01", "sqlite3", "s1"), ("D-01", "sqlite3", "s2")],
        )
        self.connection.execute(
            "DELETE FROM observation WHERE session_id = 's2'"
        )
        self.connection.commit()
        second = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(detector,)
        )
        self.assertEqual(second.exit_code, EXIT_SUCCESS)
        self.assertEqual(cluster_rows(self.connection, "D-01"), [])
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM cluster_session"
            ).fetchone()[0],
            0,
        )

    def test_session_hit_contract_failure_rolls_back_clusters_and_hits(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        seed_observation(self.connection, program="sqlite3", session_id="s2")
        detector = make_detector(
            session_hits_sql=(
                "SELECT program AS key, session_id FROM observation "
                "WHERE kind = 'run_failed' AND ts_utc >= :window_start_utc "
                "GROUP BY program, session_id LIMIT 1"
            )
        )

        report = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(detector,)
        )

        self.assertEqual(report.exit_code, EXIT_RUNTIME_ERROR)
        self.assertEqual(cluster_rows(self.connection, "D-01"), [])
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM cluster_session"
            ).fetchone()[0],
            0,
        )

    def test_session_hit_drift_requires_a_version_bump(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        seed_observation(self.connection, program="sqlite3", session_id="s2")
        hits_sql = (
            "SELECT program AS key, session_id FROM observation "
            "WHERE kind = 'run_failed' AND ts_utc >= :window_start_utc "
            "GROUP BY program, session_id"
        )
        first = make_detector(session_hits_sql=hits_sql)
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(first,)
        )
        changed = make_detector(session_hits_sql=hits_sql + " LIMIT 1")

        report = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(changed,)
        )

        self.assertEqual(report.exit_code, EXIT_VALIDATION_FAILURE)
        self.assertEqual(report.outcomes[0].status, "refused")
        self.assertIn("waste-attribution", report.outcomes[0].error)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM cluster_session"
            ).fetchone()[0],
            2,
        )

    def test_run_requires_a_positive_whole_day_window(self):
        for bad in (0, -3, 1.5, True):
            with self.assertRaises(ValueError):
                twill_detectors.run_detectors(
                    self.connection, window_days=bad, registry=()
                )

    def test_sql_error_isolates_to_the_failing_detector(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        seed_observation(
            self.connection, program="hammer", session_id="s2", kind="tool_error", tool="Bash"
        )
        broken = make_detector(
            "D-02",
            1,
            sql=program_failure_sql().replace(
                "program AS key", "no_such_column AS key"
            ),
            description="references a column that does not exist",
        )
        tool_sql = program_failure_sql(group="tool", kind="tool_error")
        registry = (
            make_detector("D-01", 1),
            broken,
            make_detector("D-03", 1, sql=tool_sql, description="tool failures"),
        )

        report = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=registry
        )

        self.assertEqual(report.exit_code, EXIT_RUNTIME_ERROR)
        self.assertEqual(
            [(o.full_id, o.status) for o in report.outcomes],
            [("D-01@1", "ok"), ("D-02@1", "error"), ("D-03@1", "ok")],
        )
        self.assertIn("no such column", report.outcomes[1].error)
        # The healthy detectors committed; the failing one left nothing.
        self.assertEqual(
            [row[1] for row in cluster_rows(self.connection, "D-01")], ["sqlite3"]
        )
        self.assertEqual(
            [row[1] for row in cluster_rows(self.connection, "D-03")], ["Bash"]
        )
        self.assertEqual(cluster_rows(self.connection, "D-02"), [])
        # A version that has never committed clusters is not stamped: fixing
        # a detector that only ever errored must not trip the drift check.
        self.assertIsNone(run_row(self.connection, "D-02", 1))

    def test_fixed_detector_runs_after_its_first_run_errored(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        broken = make_detector(
            "D-02",
            1,
            sql=program_failure_sql().replace("program AS key", "no_such_column AS key"),
            description="references a column that does not exist",
        )
        first = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(broken,)
        )
        self.assertEqual(first.exit_code, EXIT_RUNTIME_ERROR)

        fixed = make_detector("D-02", 1, description="fixed")
        second = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(fixed,)
        )
        self.assertEqual(second.exit_code, EXIT_SUCCESS)
        self.assertEqual(run_row(self.connection, "D-02", 1)[6], "ok")

    def test_contract_violations_isolate_like_sql_errors(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        missing_column = make_detector(
            "D-04",
            1,
            sql=program_failure_sql().replace(
                "count(DISTINCT session_id) AS sessions,", ""
            ),
            description="forgets the sessions column",
        )
        fractional = make_detector(
            "D-05",
            1,
            sql=program_failure_sql().replace(
                "count(DISTINCT session_id) AS sessions", "1.5 AS sessions"
            ),
            description="emits a fractional session count",
        )
        null_key = make_detector(
            "D-06",
            1,
            sql=program_failure_sql().replace("program AS key", "NULL AS key"),
            description="emits a null key",
        )

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(missing_column, fractional, null_key),
        )

        self.assertEqual(report.exit_code, EXIT_RUNTIME_ERROR)
        self.assertEqual([o.status for o in report.outcomes], ["error"] * 3)
        self.assertIn("sessions", report.outcomes[0].error)
        self.assertIn("non-integer sessions", report.outcomes[1].error)
        self.assertIn("empty key", report.outcomes[2].error)
        for detector_id in ("D-04", "D-05", "D-06"):
            self.assertEqual(cluster_rows(self.connection, detector_id), [])

    def test_a_later_failure_records_error_on_the_successful_stamp(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        detector = make_detector("D-01", 1)
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(detector,)
        )
        stamped = run_row(self.connection, "D-01", 1)

        with mock.patch.object(
            twill_detectors,
            "_run_one",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            report = twill_detectors.run_detectors(
                self.connection, window_days=30, registry=(detector,)
            )
        self.assertEqual(report.exit_code, EXIT_RUNTIME_ERROR)
        self.assertIn("disk I/O error", report.outcomes[0].error)

        recorded = run_row(self.connection, "D-01", 1)
        self.assertEqual(recorded[6], "error")
        self.assertIn("disk I/O error", recorded[7])
        # The stamp itself is untouched: a runtime fault is not a semantics
        # change, and recovery must not be mistaken for drift.
        self.assertEqual(recorded[3], stamped[3])
        self.assertEqual(recorded[3], detector.semantics_sha)

        recovered = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(detector,)
        )
        self.assertEqual(recovered.exit_code, EXIT_SUCCESS)
        self.assertEqual(run_row(self.connection, "D-01", 1)[6], "ok")

    def test_semantics_drift_under_the_same_version_is_refused(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        seed_observation(self.connection, program="sqlite3", session_id="s2")
        # sql_a groups by program (one cluster); sql_b groups by command
        # (two clusters) — same detector, different meaning.
        first = make_detector("D-01", 1, sql=program_failure_sql("program"))
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(first,)
        )
        self.assertEqual(
            [row[1] for row in cluster_rows(self.connection, "D-01")], ["sqlite3"]
        )
        stamped = run_row(self.connection, "D-01", 1)

        drifted = make_detector("D-01", 1, sql=program_failure_sql("command"))
        report = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(drifted,)
        )

        self.assertEqual(report.exit_code, EXIT_VALIDATION_FAILURE)
        self.assertEqual(report.outcomes[0].status, "refused")
        self.assertIn("bump the version", report.outcomes[0].error)
        # The recorded stamp and the committed clusters are untouched: the
        # drift never silently redefines the series.
        after = run_row(self.connection, "D-01", 1)
        self.assertEqual(after[3], stamped[3])
        self.assertEqual(after[6], "ok")
        self.assertEqual(
            [row[1] for row in cluster_rows(self.connection, "D-01")], ["sqlite3"]
        )

    def test_week_query_rejects_a_malformed_week_for_another_key(self):
        for week in ("not-a-week", "2021-W53"):
            with self.subTest(week=week):
                detector = make_detector(
                    "D-01",
                    1,
                    week_hits_sql=f"SELECT 'other-key' AS key, '{week}' AS week",
                )
                with self.assertRaises(twill_detectors.DetectorContractError):
                    twill_detectors.read_cluster_weeks(
                        self.connection,
                        detector,
                        "sqlite3",
                        window_start_utc="2026-01-01T00:00:00+00:00",
                        window_days=180,
                    )

    def test_backtest_semantics_drift_under_the_same_version_is_refused(self):
        first = make_detector(
            "D-01",
            1,
            week_hits_sql=(
                "SELECT program AS key, '2026-W01' AS week FROM observation"
            ),
        )
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(first,)
        )
        stamped = run_row(self.connection, "D-01", 1)
        drifted = make_detector(
            "D-01",
            1,
            week_hits_sql=(
                "SELECT program AS key, '2026-W02' AS week FROM observation"
            ),
        )

        report = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(drifted,)
        )

        self.assertEqual(report.exit_code, EXIT_VALIDATION_FAILURE)
        self.assertEqual(report.outcomes[0].status, "refused")
        self.assertIn("backtest semantics changed", report.outcomes[0].error)
        self.assertEqual(run_row(self.connection, "D-01", 1)[10], stamped[10])
        self.assertEqual(stamped[10], first.backtest_sha)

    def test_removing_backtest_semantics_under_the_same_version_is_refused(self):
        first = make_detector(
            "D-01",
            1,
            week_hits_sql=(
                "SELECT program AS key, '2026-W01' AS week FROM observation"
            ),
        )
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(first,)
        )
        stamped = run_row(self.connection, "D-01", 1)

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(make_detector("D-01", 1),),
        )

        self.assertEqual(report.exit_code, EXIT_VALIDATION_FAILURE)
        self.assertIn("removed backtest semantics", report.outcomes[0].error)
        self.assertEqual(run_row(self.connection, "D-01", 1)[10], stamped[10])

    def test_drift_in_one_detector_does_not_stop_the_others(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        stable = make_detector("D-01", 1)
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(stable,)
        )
        drifted = make_detector(
            "D-02", 1, sql=program_failure_sql("command"), description="groups by command"
        )
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(drifted,)
        )
        changed = make_detector(
            "D-02", 1, sql=program_failure_sql("program"), description="groups by program"
        )

        report = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(stable, changed)
        )

        self.assertEqual(report.exit_code, EXIT_VALIDATION_FAILURE)
        self.assertEqual(
            [(o.full_id, o.status) for o in report.outcomes],
            [("D-01@1", "ok"), ("D-02@1", "refused")],
        )

    def test_version_bump_redefines_the_series_openly(self):
        seed_observation(
            self.connection, program="sqlite3", session_id="s1", command="sqlite3 one"
        )
        seed_observation(
            self.connection, program="sqlite3", session_id="s2", command="sqlite3 two"
        )
        v1 = make_detector("D-01", 1, sql=program_failure_sql("program"))
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(v1,)
        )
        # One program-level cluster covering both sessions.
        self.assertEqual(
            [row[1] for row in cluster_rows(self.connection, "D-01")], ["sqlite3"]
        )

        v2 = make_detector("D-01", 2, sql=program_failure_sql("command"))
        report = twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(v2,)
        )

        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        # Both versions are on record — the measurement series can name the
        # version each point ran — and the cluster identity (base id + key)
        # is stable across the bump: same detector_id, refreshed in place.
        self.assertIsNotNone(run_row(self.connection, "D-01", 1))
        self.assertIsNotNone(run_row(self.connection, "D-01", 2))
        self.assertEqual(run_row(self.connection, "D-01", 2)[3], v2.semantics_sha)
        self.assertEqual(
            [row[1] for row in cluster_rows(self.connection, "D-01")],
            ["sqlite3 one", "sqlite3 two"],
        )
        for row in cluster_rows(self.connection, "D-01"):
            self.assertEqual(row[3], 1)  # one session per command-level cluster

    def test_refresh_deletes_stale_open_clusters_and_keeps_review_state(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        seed_observation(self.connection, program="bf", session_id="s2")
        detector = make_detector("D-01", 1)
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(detector,)
        )
        self.assertEqual(
            [row[1] for row in cluster_rows(self.connection, "D-01")],
            ["bf", "sqlite3"],
        )
        # A reviewed cluster is review state, not detector output: it
        # survives a refresh that no longer emits it.
        with self.connection:
            self.connection.execute(
                "UPDATE cluster SET state = 'dismissed', covered_by = "
                "'~/.claude/CLAUDE.md' WHERE detector_id = 'D-01' AND key = 'bf'"
            )

        with self.connection:
            self.connection.execute(
                "DELETE FROM observation WHERE program = 'bf'"
            )
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(detector,)
        )

        rows = {row[1]: row for row in cluster_rows(self.connection, "D-01")}
        self.assertEqual(set(rows), {"bf", "sqlite3"})
        self.assertEqual(rows["bf"][9], "dismissed")
        self.assertEqual(rows["bf"][8], "~/.claude/CLAUDE.md")
        self.assertEqual(rows["sqlite3"][9], "open")

    def test_rerunning_over_unchanged_observations_is_idempotent(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        seed_observation(self.connection, program="sqlite3", session_id="s2")
        detector = make_detector("D-01", 1)
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(detector,)
        )
        first = cluster_rows(self.connection, "D-01")
        twill_detectors.run_detectors(
            self.connection, window_days=30, registry=(detector,)
        )
        self.assertEqual(cluster_rows(self.connection, "D-01"), first)

    def test_keys_are_redacted_and_bounded(self):
        seed_observation(self.connection, program="sqlite3", session_id="s1")
        # zeroblob-derived padding makes the emitted key far longer than the
        # §8.3 bound of 240 characters (each zero byte becomes one 'q').
        long_key_sql = (
            "SELECT program || substr(replace(hex(zeroblob(400)), '00', 'q'), 1, 400) "
            "AS key, count(DISTINCT session_id) AS sessions, count(*) AS events, "
            "min(ts_utc) AS first_seen, max(ts_utc) AS last_seen "
            "FROM observation WHERE ts_utc >= :window_start_utc GROUP BY program"
        )
        secret_key_sql = (
            "SELECT 'ghp_1234567890abcdefghijklmnop' AS key, 1 AS sessions, "
            "1 AS events, min(ts_utc) AS first_seen, max(ts_utc) AS last_seen "
            "FROM observation"
        )
        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(
                make_detector("D-01", 1, sql=long_key_sql, description="long keys"),
                make_detector("D-02", 1, sql=secret_key_sql, description="secret keys"),
            ),
        )
        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        long_key = cluster_rows(self.connection, "D-01")[0][1]
        self.assertEqual(len(long_key), twill_detectors.MAX_KEY_LENGTH)
        self.assertTrue(long_key.startswith("sqlite3"))
        secret_key = cluster_rows(self.connection, "D-02")[0][1]
        self.assertNotIn("ghp_", secret_key)
        self.assertEqual(secret_key, "<redacted:github-token>")


class MissingBinaryDetectorTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.state_dir = Path(self._temporary.name) / "state"
        self.connection = twill_schema.connect(self.state_dir)
        self.addCleanup(self.connection.close)

    def test_d01_groups_command_not_found_by_program_across_sessions(self):
        for program, session_id, days_ago, signature in (
            ("sqlite3", "s1", 5, "sqlite3: command not found"),
            ("sqlite3", "s1", 4, "running sqlite3: command not found"),
            ("sqlite3", "s2", 3, "sqlite3: command not found"),
            ("sqlite3", "s3", 2, "sqlite3: command not found"),
            ("bf", "s2", 2, "bf: command not found"),
            ("bf", "s3", 1, "bf: command not found"),
        ):
            seed_observation(
                self.connection,
                program=program,
                session_id=session_id,
                days_ago=days_ago,
                signature=signature,
            )
        seed_observation(
            self.connection,
            program="go",
            session_id="s1",
            signature="go: command not found",
        )
        seed_observation(
            self.connection,
            program="sqlite3",
            session_id="s1",
            signature="permission denied",
        )
        seed_observation(
            self.connection,
            program="sqlite3",
            session_id="s2",
            signature="permission denied",
        )
        seed_observation(
            self.connection,
            program="tool",
            session_id="s1",
            kind="tool_error",
            tool="Bash",
            signature="tool: command not found",
        )
        seed_observation(
            self.connection,
            program="tool",
            session_id="s2",
            kind="tool_error",
            tool="Bash",
            signature="tool: command not found",
        )
        seed_observation(
            self.connection,
            program="",
            session_id="s1",
            signature=": command not found",
        )
        seed_observation(
            self.connection,
            program="",
            session_id="s2",
            signature=": command not found",
        )
        seed_observation(
            self.connection,
            program="sqlite3",
            session_id="s1",
            days_ago=40,
            signature="sqlite3: command not found",
        )
        seed_observation(
            self.connection,
            program="sqlite3",
            session_id="s2",
            days_ago=40,
            signature="sqlite3: command not found",
        )

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.MISSING_BINARY,),
        )

        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        self.assertEqual(
            [(outcome.full_id, outcome.status, outcome.clusters) for outcome in report.outcomes],
            [("D-01@1", "ok", 2)],
        )
        rows = cluster_rows(self.connection, "D-01")
        self.assertEqual(
            [row[1] for row in rows],
            ["command-not-found:bf", "command-not-found:sqlite3"],
        )
        sqlite3 = next(row for row in rows if row[1].endswith(":sqlite3"))
        self.assertEqual(sqlite3[3], 3)
        self.assertEqual(sqlite3[4], 4)
        self.assertLess(sqlite3[5], sqlite3[6])
        self.assertEqual(sqlite3[7], 0.0)
        self.assertEqual(sqlite3[9], "open")
        bf = next(row for row in rows if row[1].endswith(":bf"))
        self.assertEqual(bf[3], 2)
        self.assertEqual(bf[4], 2)
        self.assertEqual(
            self.connection.execute(
                "SELECT key, session_id FROM cluster_session "
                "WHERE detector_id = 'D-01' AND key = ? ORDER BY session_id",
                ("command-not-found:sqlite3",),
            ).fetchall(),
            [
                ("command-not-found:sqlite3", "s1"),
                ("command-not-found:sqlite3", "s2"),
                ("command-not-found:sqlite3", "s3"),
            ],
        )

        parameters = {
            "window_start_utc": (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).isoformat(),
            "window_days": 30,
        }
        ordered = self.connection.execute(
            twill_detectors.MISSING_BINARY_SQL, parameters
        ).fetchall()
        self.assertEqual(
            [row[0] for row in ordered],
            ["command-not-found:sqlite3", "command-not-found:bf"],
        )


class RecurringErrorSignatureDetectorTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.state_dir = Path(self._temporary.name) / "state"
        self.connection = twill_schema.connect(self.state_dir)
        self.addCleanup(self.connection.close)

    def test_d02_requires_two_sessions_and_ranks_by_sessions_then_recency(self):
        seed_observation(
            self.connection,
            session_id="s1",
            days_ago=5,
            signature="shared failure",
        )
        seed_observation(
            self.connection,
            session_id="s1",
            days_ago=4,
            signature="shared failure",
        )
        seed_observation(
            self.connection,
            session_id="s2",
            days_ago=3,
            signature="shared failure",
        )
        seed_observation(
            self.connection,
            session_id="s3",
            days_ago=1,
            signature="shared failure",
        )
        seed_observation(
            self.connection,
            session_id="s1",
            kind="tool_error",
            tool="Edit",
            signature="tool failure",
        )
        seed_observation(
            self.connection,
            session_id="s2",
            kind="tool_error",
            tool="Edit",
            signature="tool failure",
        )
        seed_observation(
            self.connection,
            session_id="s1",
            days_ago=2,
            signature="one session only",
        )
        seed_observation(
            self.connection,
            session_id="s1",
            days_ago=2,
            signature="one session only",
        )
        seed_observation(
            self.connection,
            session_id="s1",
            days_ago=40,
            signature="outside window",
        )
        seed_observation(
            self.connection,
            session_id="s2",
            days_ago=40,
            signature="outside window",
        )
        seed_observation(
            self.connection,
            session_id="s1",
            kind="session_activity",
            signature="not an error",
        )
        seed_observation(
            self.connection,
            session_id="s2",
            kind="session_activity",
            signature="not an error",
        )
        seed_observation(
            self.connection,
            session_id="s1",
            signature=" ",
        )
        seed_observation(
            self.connection,
            session_id="s2",
            signature=" ",
        )

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.RECURRING_ERROR_SIGNATURE,),
        )

        self.assertEqual(report.exit_code, EXIT_SUCCESS)
        self.assertEqual(
            [(outcome.full_id, outcome.status, outcome.clusters) for outcome in report.outcomes],
            [("D-02@1", "ok", 2)],
        )
        rows = cluster_rows(self.connection, "D-02")
        self.assertEqual([row[1] for row in rows], ["shared failure", "tool failure"])
        shared = next(row for row in rows if row[1] == "shared failure")
        self.assertEqual(shared[3], 3)
        self.assertEqual(shared[4], 4)
        self.assertLess(shared[5], shared[6])
        tool = next(row for row in rows if row[1] == "tool failure")
        self.assertEqual(tool[3], 2)
        self.assertEqual(tool[4], 2)
        self.assertEqual(
            self.connection.execute(
                "SELECT key, session_id FROM cluster_session "
                "WHERE detector_id = 'D-02' AND key = 'shared failure' "
                "ORDER BY session_id"
            ).fetchall(),
            [("shared failure", "s1"), ("shared failure", "s2"), ("shared failure", "s3")],
        )

        parameters = {
            "window_start_utc": (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).isoformat(),
            "window_days": 30,
        }
        ordered = self.connection.execute(
            twill_detectors.RECURRING_ERROR_SIGNATURE.cluster_sql, parameters
        ).fetchall()
        self.assertEqual([row[0] for row in ordered], ["shared failure", "tool failure"])


class DetectCommandTests(unittest.TestCase):
    """The ``twill detect`` verb surface (§14) over the registry runner."""

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

    def _args(self, **overrides) -> argparse.Namespace:
        values = {
            "window": None,
            "detector": None,
            "state_dir": str(self.state_dir),
            "json": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def _seed(self):
        connection = twill_schema.connect(self.state_dir)
        try:
            seed_observation(connection, program="sqlite3", session_id="s1")
        finally:
            connection.close()

    def test_detect_reports_each_detector_and_commits_clusters(self):
        self._seed()
        detector = make_detector("D-01", 2)
        captured = StringIO()
        with mock.patch.object(twill_detectors, "REGISTRY", (detector,)):
            with redirect_stdout(captured):
                code = twill_app.detect_command(self._args())
        self.assertEqual(code, EXIT_SUCCESS)
        payload = json.loads(captured.getvalue())["data"]
        self.assertEqual(payload["window_days"], 30)
        self.assertEqual(
            payload["detectors"],
            [
                {
                    "detector_id": "D-01",
                    "version": 2,
                    "full_id": "D-01@2",
                    "status": "ok",
                    "clusters": 1,
                    "error": None,
                }
            ],
        )
        connection = sqlite3.connect(self.state_dir / "twill.db")
        try:
            self.assertEqual(
                [row[1] for row in cluster_rows(connection, "D-01")], ["sqlite3"]
            )
        finally:
            connection.close()

    def test_detect_failure_raises_a_contract_error_naming_the_detector(self):
        self._seed()
        broken = make_detector(
            "D-02",
            1,
            sql=program_failure_sql().replace("program AS key", "no_such_column AS key"),
            description="references a column that does not exist",
        )
        registry = (make_detector("D-01", 1), broken)
        with mock.patch.object(twill_detectors, "REGISTRY", registry):
            with self.assertRaises(CliError) as context:
                twill_app.detect_command(self._args())
        self.assertEqual(context.exception.code, EXIT_RUNTIME_ERROR)
        self.assertIn("D-02@1", context.exception.message)
        self.assertIn("1 of 2", context.exception.message)
        # The healthy detector's clusters were committed despite the error.
        connection = sqlite3.connect(self.state_dir / "twill.db")
        try:
            self.assertEqual(
                [row[1] for row in cluster_rows(connection, "D-01")], ["sqlite3"]
            )
        finally:
            connection.close()

    def test_detect_selects_one_detector_and_rejects_unknown_ids(self):
        self._seed()
        registry = (make_detector("D-01", 1), make_detector("D-02", 1))
        with mock.patch.object(twill_detectors, "REGISTRY", registry):
            with redirect_stdout(StringIO()):
                code = twill_app.detect_command(self._args(detector=["D-02"]))
            self.assertEqual(code, EXIT_SUCCESS)
            with self.assertRaises(CliError) as context:
                twill_app.detect_command(self._args(detector=["D-99"]))
        self.assertEqual(context.exception.code, 2)
        connection = sqlite3.connect(self.state_dir / "twill.db")
        try:
            self.assertEqual(cluster_rows(connection, "D-01"), [])
            self.assertEqual(
                [row[1] for row in cluster_rows(connection, "D-02")], ["sqlite3"]
            )
        finally:
            connection.close()

    def test_window_must_be_whole_days(self):
        for bad in (43200.0, 0.0, -86400.0):
            with self.assertRaises(CliError) as context:
                twill_app.detect_command(self._args(window=bad))
            self.assertEqual(context.exception.code, 2)
        with redirect_stdout(StringIO()):
            code = twill_app.detect_command(self._args(window=604800.0))
        self.assertEqual(code, EXIT_SUCCESS)


class DetectCliTests(unittest.TestCase):
    """End-to-end ``twill detect`` through the shipped CLI."""

    @classmethod
    def setUpClass(cls):
        # detect loads config at startup and artifacts_root has no default
        # (plan §13.1), so every CLI run here gets a throwaway HOME.
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

    def test_registered_detector_is_a_clean_run(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            result = self.run_cli("detect", "--json", "--state-dir", str(state))
            self.assertEqual(result.returncode, 0, result.stderr)
            envelope = json.loads(result.stdout)
            self.assertEqual(
                envelope["data"]["detectors"],
                [
                    {
                        "detector_id": "D-01",
                        "version": 1,
                        "full_id": "D-01@1",
                        "status": "ok",
                        "clusters": 0,
                        "error": None,
                    },
                    {
                        "detector_id": "D-02",
                        "version": 1,
                        "full_id": "D-02@1",
                        "status": "ok",
                        "clusters": 0,
                        "error": None,
                    },
                ],
            )
            self.assertEqual(envelope["data"]["window_days"], 30)
            self.assertEqual(envelope["warnings"], [])
            self.assertEqual(result.stderr, "")

            human = self.run_cli("detect", "--state-dir", str(state))
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn("D-01@1: 0 cluster(s)", human.stdout)
            self.assertIn("D-02@1: 0 cluster(s)", human.stdout)

    def test_unknown_detector_flag_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            result = self.run_cli(
                "detect", "--json", "--detector", "D-99", "--state-dir", str(state)
            )
            self.assertEqual(result.returncode, 2)
            error = json.loads(result.stdout)["error"]
            self.assertEqual(error["code"], 2)
            self.assertIn("unknown detector", error["message"])

    def test_partial_day_windows_are_usage_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            for window in ("12h", "0", "90"):
                result = self.run_cli(
                    "detect", "--json", "--window", window, "--state-dir", str(state)
                )
                self.assertEqual(result.returncode, 2, window)
                self.assertIn("whole number of days", result.stdout)

    def test_detect_takes_the_state_lock(self):
        from twill_lock import StateLock

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with StateLock(state):
                result = self.run_cli(
                    "detect", "--json", "--state-dir", str(state)
                )
            self.assertEqual(result.returncode, 3, result.stderr)
            error = json.loads(result.stdout)["error"]
            self.assertEqual(error["code"], 3)
            self.assertEqual(error["message"].split()[0:3], ["lock", "held", "by"])


if __name__ == "__main__":
    unittest.main()
