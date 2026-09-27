import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

import twill_detectors  # noqa: E402
import twill_doctor  # noqa: E402
import twill_perf  # noqa: E402
import twill_rulecorpus  # noqa: E402
import twill_schema  # noqa: E402
from twill_status import record_stage, status_path  # noqa: E402


class DoctorChecksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"

    def create_database(self):
        connection = twill_schema.connect(self.state)
        connection.close()

    def healthy_report(self, now=None, free=twill_doctor.FREE_DISK_WARN_BYTES):
        self.create_database()
        record_stage(self.state, "ingest", 0.1, {"events": 0})
        return twill_doctor.run_doctor(
            self.state,
            now=now,
            disk_usage=lambda _: SimpleNamespace(free=free),
        )

    def check(self, report, name):
        return next(check for check in report.checks if check.name == name)

    def test_healthy_checks_return_zero(self):
        report = self.healthy_report()
        self.assertEqual(report.status, twill_doctor.HEALTHY)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(
            [check.name for check in report.checks],
            [
                "db_integrity",
                "db_schema",
                "timer_freshness",
                "performance_budgets",
                "cursor_health",
                "rule_corpus",
                "dead_man_switch",
                "detector_self_test",
                "disk_space",
            ],
        )
        self.assertTrue(all(check.status == twill_doctor.HEALTHY for check in report.checks))

    def test_ingest_budget_miss_is_broken_and_keeps_the_last_success(self):
        self.create_database()
        successful = twill_perf.assess_ingest(1.0, 1.0, 100 * 1024)
        record_stage(
            self.state,
            "ingest",
            1.0,
            {"events": 1},
            performance=successful,
        )
        miss = twill_perf.assess_ingest(120.0, 1.0, 100 * 1024)
        record_stage(
            self.state,
            "ingest",
            120.0,
            {"events": 1},
            succeeded=False,
            performance=miss,
        )

        report = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        check = self.check(report, "performance_budgets")
        self.assertEqual(check.status, twill_doctor.BROKEN)
        self.assertIn("wall_time_seconds", check.message)
        self.assertTrue(check.details["misses"])
        status = json.loads(status_path(self.state).read_text())
        self.assertIsNotNone(status["data"]["stages"]["ingest"]["last_success"])
        self.assertIn("last_failure", status["data"]["stages"]["ingest"])

    def test_later_phase_budget_miss_is_broken(self):
        self.create_database()
        detect_miss = twill_perf.assess_detect(
            twill_perf.DETECT_PASS_WALL_TIME_BUDGET_SECONDS
        )
        record_stage(
            self.state,
            "detect",
            twill_perf.DETECT_PASS_WALL_TIME_BUDGET_SECONDS,
            {"detectors": 8},
            succeeded=False,
            performance=detect_miss,
        )
        report = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        check = self.check(report, "performance_budgets")
        self.assertEqual(check.status, twill_doctor.BROKEN)
        self.assertEqual(check.details["stage"], "detect")
        self.assertTrue(check.details["misses"])

    def add_cursor(self, *, now, first_seen=None, mtime_ns=None, session_id="s1"):
        first_seen = first_seen or now
        mtime_ns = (
            mtime_ns
            if mtime_ns is not None
            else int(first_seen.timestamp() * 1_000_000_000)
        )
        connection = twill_schema.connect(self.state)
        connection.execute(
            "INSERT INTO cursor(path, session_id, source, identity_sha, size, mtime_ns, "
            "last_offset, parse_errors, first_seen, last_indexed_at) "
            "VALUES (?, ?, 'claude', 'sha', 1, ?, 1, 0, ?, ?)",
            (f"/transcripts/{session_id}.jsonl", session_id, mtime_ns, first_seen.isoformat(), now.isoformat()),
        )
        connection.commit()
        connection.close()

    def add_observation(self, *, now, session_id="s1"):
        connection = twill_schema.connect(self.state)
        connection.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind) VALUES (?, ?, ?, 'file_read')",
            (session_id, now.isoformat(), now.isoformat()),
        )
        connection.commit()
        connection.close()

    def test_dead_man_switch_fails_after_a_day_of_arrivals_without_observations(self):
        now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        self.create_database()
        self.add_cursor(now=now, first_seen=now - timedelta(hours=24))
        report = twill_doctor.run_doctor(
            self.state,
            now=now,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        check = self.check(report, "dead_man_switch")
        self.assertEqual(check.status, twill_doctor.BROKEN)
        self.assertEqual(check.details["recent_file_count"], 1)
        self.assertEqual(check.details["observations_ingested"], 0)
        self.assertIn("zero observations", check.message)
        self.assertEqual(report.exit_code, twill_doctor.EXIT_BROKEN)

    def test_dead_man_switch_is_healthy_when_recent_arrival_has_an_observation(self):
        now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        self.create_database()
        self.add_cursor(now=now, first_seen=now - timedelta(hours=24))
        self.add_observation(now=now)
        report = twill_doctor.run_doctor(
            self.state,
            now=now,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        check = self.check(report, "dead_man_switch")
        self.assertEqual(check.status, twill_doctor.HEALTHY)
        self.assertEqual(check.details["observations_ingested"], 1)

    def test_dead_man_switch_does_not_alarm_for_a_quiet_window(self):
        now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        self.create_database()
        self.add_cursor(now=now, first_seen=now - timedelta(hours=24, seconds=1))
        report = twill_doctor.run_doctor(
            self.state,
            now=now,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        self.assertEqual(self.check(report, "dead_man_switch").status, twill_doctor.HEALTHY)

    def test_missing_database_is_broken_without_creating_state(self):
        report = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        self.assertEqual(report.status, twill_doctor.BROKEN)
        self.assertEqual(self.check(report, "db_integrity").status, twill_doctor.BROKEN)
        self.assertEqual(self.check(report, "db_schema").status, twill_doctor.BROKEN)
        self.assertEqual(self.check(report, "timer_freshness").status, twill_doctor.DEGRADED)
        self.assertFalse(self.state.exists())

    def test_cli_json_reports_broken_health_as_success_envelope(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "doctor", "--json", "--state-dir", str(self.state)],
            cwd=ROOT,
            env=os.environ.copy(),
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(
            set(payload), {"schema_version", "generated_at", "data", "warnings"}
        )
        self.assertEqual(payload["data"]["status"], twill_doctor.BROKEN)
        self.assertNotIn("error", payload)

    def test_cli_usage_error_still_uses_error_envelope(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "doctor", "--json", "--unknown"],
            cwd=ROOT,
            env=os.environ.copy(),
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], 2)

    def test_existing_database_read_does_not_create_wal_sidecars(self):
        self.create_database()
        db_path = twill_schema.state_db_path(self.state)
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        for suffix in ("-wal", "-shm"):
            self.assertFalse(db_path.with_name(db_path.name + suffix).exists())

    def test_schema_behind_is_broken_and_ahead_is_compatible(self):
        self.create_database()
        connection = twill_schema.connect(self.state)
        connection.execute(
            "UPDATE meta SET value = '1' WHERE key = ?",
            (twill_schema.SCHEMA_VERSION_KEY,),
        )
        connection.commit()
        connection.close()
        behind = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        self.assertEqual(self.check(behind, "db_schema").status, twill_doctor.BROKEN)

        connection = twill_schema.connect(self.state)
        connection.execute(
            "UPDATE meta SET value = '999' WHERE key = ?",
            (twill_schema.SCHEMA_VERSION_KEY,),
        )
        connection.commit()
        connection.close()
        ahead = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        self.assertEqual(self.check(ahead, "db_schema").status, twill_doctor.HEALTHY)

    def test_schema_missing_required_table_is_broken(self):
        self.create_database()
        connection = twill_schema.connect(self.state)
        connection.execute("DROP TABLE measurement")
        connection.commit()
        connection.close()
        report = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        self.assertEqual(self.check(report, "db_schema").status, twill_doctor.BROKEN)

    def test_schema_missing_attribution_relation_is_broken(self):
        self.create_database()
        connection = twill_schema.connect(self.state)
        connection.execute("DROP TABLE cluster_session")
        connection.commit()
        connection.close()
        report = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        self.assertEqual(self.check(report, "db_schema").status, twill_doctor.BROKEN)

    def test_malformed_cursor_counter_is_broken(self):
        self.create_database()
        connection = twill_schema.connect(self.state)
        connection.execute(
            "INSERT INTO cursor(path, session_id, source, identity_sha, size, mtime_ns, "
            "last_offset, parse_errors, first_seen, last_indexed_at) "
            "VALUES ('/t/bad.jsonl', 's1', 'claude', 'sha', 1, 1, 0, 'bad', 't', 't')"
        )
        connection.commit()
        connection.close()
        report = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        self.assertEqual(self.check(report, "cursor_health").status, twill_doctor.BROKEN)

    def test_timer_cutoff_is_strictly_greater_than_three_intervals(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        self.create_database()
        record_stage(self.state, "ingest", 0.1, {"events": 0})
        payload = json.loads(status_path(self.state).read_text(encoding="utf-8"))
        payload["data"]["stages"]["ingest"]["last_success"] = (
            now - timedelta(seconds=3 * 3600)
        ).isoformat()
        status_path(self.state).write_text(json.dumps(payload), encoding="utf-8")
        report = twill_doctor.run_doctor(
            self.state,
            now=now,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        self.assertEqual(self.check(report, "timer_freshness").status, twill_doctor.HEALTHY)

        payload["data"]["stages"]["ingest"]["last_success"] = (
            now - timedelta(seconds=3 * 3600 + 1)
        ).isoformat()
        status_path(self.state).write_text(json.dumps(payload), encoding="utf-8")
        report = twill_doctor.run_doctor(
            self.state,
            now=now,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        self.assertEqual(self.check(report, "timer_freshness").status, twill_doctor.DEGRADED)
        self.assertEqual(report.exit_code, 1)

    def test_cursor_errors_and_missing_paths_degrade(self):
        self.create_database()
        connection = twill_schema.connect(self.state)
        connection.executemany(
            "INSERT INTO cursor(path, session_id, source, identity_sha, size, mtime_ns, "
            "last_offset, parse_errors, first_seen, last_indexed_at, path_missing) "
            "VALUES (?, ?, 'claude', 'sha', 1, 1, 0, ?, 't', 't', ?)",
            [
                ("/t/errors.jsonl", "s1", 1, 0),
                ("/t/missing.jsonl", "s2", 0, 1),
            ],
        )
        connection.commit()
        connection.close()
        report = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        check = self.check(report, "cursor_health")
        self.assertEqual(check.status, twill_doctor.DEGRADED)
        self.assertEqual(check.details["parse_error_file_count"], 1)
        self.assertEqual(check.details["missing_path_count"], 1)

    def test_rule_corpus_reports_hash_drift_and_vanished_paths(self):
        self.create_database()
        changed = self.root / "rules" / "changed.md"
        missing = self.root / "rules" / "missing.md"
        changed.parent.mkdir()
        changed.write_text("original rule\n")
        connection = twill_schema.connect(self.state)
        connection.executemany(
            "INSERT INTO rule_doc(path, layer, sha, indexed_at, stale) "
            "VALUES (?, 'agents_md', ?, 't', 0)",
            [
                (str(changed), twill_rulecorpus.content_sha(b"original rule\n")),
                (str(missing), twill_rulecorpus.content_sha(b"missing rule\n")),
            ],
        )
        connection.commit()
        connection.close()
        changed.write_text("edited rule\n")

        report = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )

        check = self.check(report, "rule_corpus")
        self.assertEqual(check.status, twill_doctor.DEGRADED)
        self.assertEqual(check.details["hash_mismatch_count"], 1)
        self.assertEqual(
            check.details["hash_mismatches"][0]["path"], str(changed)
        )
        self.assertEqual(check.details["vanished_paths"], [str(missing)])
        self.assertEqual(check.details["vanished_path_count"], 1)

    def test_rule_corpus_reports_persisted_stale_rows(self):
        self.create_database()
        path = self.root / "rules.md"
        path.write_text("rule\n")
        connection = twill_schema.connect(self.state)
        connection.execute(
            "INSERT INTO rule_doc(path, layer, sha, indexed_at, stale) "
            "VALUES (?, 'memory', ?, 't', 1)",
            (str(path), twill_rulecorpus.content_sha(b"rule\n")),
        )
        connection.commit()
        connection.close()

        report = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )

        check = self.check(report, "rule_corpus")
        self.assertEqual(check.status, twill_doctor.DEGRADED)
        self.assertEqual(check.details["stale_rows"], [str(path)])
        self.assertEqual(check.details["vanished_paths"], [])

    def test_free_disk_threshold_is_inclusive(self):
        report = self.healthy_report(free=twill_doctor.FREE_DISK_WARN_BYTES - 1)
        self.assertEqual(self.check(report, "disk_space").status, twill_doctor.DEGRADED)
        report = self.healthy_report(free=twill_doctor.FREE_DISK_WARN_BYTES)
        self.assertEqual(self.check(report, "disk_space").status, twill_doctor.HEALTHY)

    def test_ingest_free_disk_floor_is_inclusive(self):
        below = twill_doctor.check_ingest_disk_space(
            self.state,
            disk_usage=lambda _: SimpleNamespace(
                free=twill_doctor.FREE_DISK_INGEST_FLOOR_BYTES - 1
            ),
        )
        self.assertEqual(below.status, twill_doctor.BROKEN)
        self.assertIn("ingest refused", below.message)

        at_floor = twill_doctor.check_ingest_disk_space(
            self.state,
            disk_usage=lambda _: SimpleNamespace(
                free=twill_doctor.FREE_DISK_INGEST_FLOOR_BYTES
            ),
        )
        self.assertEqual(at_floor.status, twill_doctor.HEALTHY)

    def test_cursor_paths_are_redacted_in_machine_output(self):
        self.create_database()
        token = "ghp_" + "1234567890" + "abcdefghijklmnop"
        connection = twill_schema.connect(self.state)
        connection.execute(
            "INSERT INTO cursor(path, session_id, source, identity_sha, size, mtime_ns, "
            "last_offset, parse_errors, first_seen, last_indexed_at) "
            "VALUES (?, 's1', 'claude', 'sha', 1, 1, 0, 1, 't', 't')",
            (f"/t/{token}.jsonl",),
        )
        connection.commit()
        connection.close()
        report = twill_doctor.run_doctor(
            self.state,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )
        rendered = json.dumps(report.as_dict())
        self.assertNotIn(token, rendered)
        self.assertIn("<redacted:github-token>", rendered)

    def test_rescan_redaction_repairs_observation_and_source_excerpts(self):
        self.create_database()
        token = "ghp_" + "1234567890" + "abcdefghijklmnop"
        connection = twill_schema.connect(self.state)
        connection.execute(
            "CREATE TABLE transcript_event ("
            "event_id INTEGER PRIMARY KEY, text TEXT)"
        )
        connection.execute(
            "INSERT INTO transcript_event(event_id, text) VALUES (1, ?)",
            (f"source {token}",),
        )
        connection.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, excerpt) "
            "VALUES ('s1', 't', 't', 'session_activity', ?)",
            (f"observation {token}",),
        )
        connection.commit()

        result = twill_doctor.rescan_redaction(connection)

        self.assertEqual(result, {"rows_scanned": 2, "rows_changed": 2, "fields_changed": 2})
        self.assertEqual(
            connection.execute("SELECT text FROM transcript_event").fetchone()[0],
            "source <redacted:github-token>",
        )
        self.assertEqual(
            connection.execute("SELECT excerpt FROM observation").fetchone()[0],
            "observation <redacted:github-token>",
        )
        connection.close()

    def test_rescan_redaction_is_idempotent_and_preserves_nulls(self):
        self.create_database()
        connection = twill_schema.connect(self.state)
        connection.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, excerpt) "
            "VALUES ('s1', 't', 't', 'session_activity', NULL)"
        )
        connection.commit()

        first = twill_doctor.rescan_redaction(connection)
        second = twill_doctor.rescan_redaction(connection)

        self.assertEqual(first["rows_scanned"], 1)
        self.assertEqual(first["rows_changed"], 0)
        self.assertEqual(second, first)
        self.assertIsNone(
            connection.execute("SELECT excerpt FROM observation").fetchone()[0]
        )
        connection.close()

    def test_cli_rescan_redaction_uses_configured_fences_and_records_status(self):
        self.create_database()
        connection = twill_schema.connect(self.state)
        connection.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, excerpt) "
            "VALUES ('s1', 't', 't', 'session_activity', 'before private.example after')"
        )
        connection.commit()
        connection.close()

        home = self.root / "home"
        config_dir = home / ".config" / "twill"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            f'artifacts_root = "{home / "artifacts"}"\n'
            'content_fences = ["private.example"]\n',
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "doctor",
                "--rescan-redaction",
                "--json",
                "--state-dir",
                str(self.state),
            ],
            cwd=ROOT,
            env={**os.environ, "HOME": str(home)},
            check=False,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["data"],
            {"fields_changed": 1, "rows_changed": 1, "rows_scanned": 1},
        )
        connection = twill_schema.connect_read_only(self.state)
        self.assertEqual(
            connection.execute("SELECT excerpt FROM observation").fetchone()[0],
            "before <redacted:content-fence> after",
        )
        connection.close()
        status = json.loads((self.state / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(
            status["data"]["stages"]["rescan_redaction"]["counts"],
            {"records": 1, "rows": 1},
        )


class DetectorSelfTestTests(unittest.TestCase):
    """§13.3's detector self-test: the registry must run before a digest."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        # Deliberately absent: the self-test replays the registry over its
        # own in-memory fixture and must not need the state database.
        self.state = Path(self._temporary.name) / "state"
        self.now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

    def report(self, registry=None):
        return twill_doctor.run_doctor(
            self.state,
            now=self.now,
            registry=registry,
            disk_usage=lambda _: SimpleNamespace(free=twill_doctor.FREE_DISK_WARN_BYTES),
        )

    def self_test(self, report):
        return next(
            check for check in report.checks if check.name == "detector_self_test"
        )

    def detector(self, detector_id, sql, **kwargs):
        return twill_detectors.Detector(detector_id, 1, "self-test probe", sql, **kwargs)

    def test_self_test_passes_the_shipped_registry_over_the_fixture(self):
        check = self.self_test(self.report())
        self.assertEqual(check.status, twill_doctor.HEALTHY)
        self.assertIn(
            f"all {len(twill_detectors.REGISTRY)} registered detector(s) passed",
            check.message,
        )
        self.assertEqual(
            set(check.details["detectors"]),
            {detector.full_id for detector in twill_detectors.REGISTRY},
        )
        self.assertTrue(
            all(
                entry["status"] == "ok" and entry["clusters"] >= 1
                for entry in check.details["detectors"].values()
            )
        )

    def test_self_test_runs_without_a_state_database(self):
        check = self.self_test(self.report())
        self.assertEqual(check.status, twill_doctor.HEALTHY)
        self.assertFalse(self.state.exists())

    def test_expectation_map_exactly_covers_the_shipped_registry(self):
        self.assertEqual(
            set(twill_doctor.SELFTEST_EXPECTED_CLUSTERS),
            {detector.detector_id for detector in twill_detectors.REGISTRY},
        )

    def test_self_test_flags_sql_that_cannot_parse(self):
        broken = self.detector(
            "D-98",
            "SELECT no_such_column AS key, "
            "count(DISTINCT session_id) AS sessions, count(*) AS events, "
            "min(ts_utc) AS first_seen, max(ts_utc) AS last_seen "
            "FROM observation WHERE ts_utc >= :window_start_utc "
            "GROUP BY no_such_column",
        )
        report = self.report(registry=(broken,))
        check = self.self_test(report)
        self.assertEqual(check.status, twill_doctor.BROKEN)
        self.assertIn("D-98@1", check.message)
        self.assertIn(
            "no such column",
            check.details["detectors"]["D-98@1"]["error"],
        )
        self.assertEqual(report.exit_code, twill_doctor.EXIT_BROKEN)

    def test_self_test_flags_a_contract_violation(self):
        shapeless = self.detector(
            "D-98",
            "SELECT program AS key, count(*) AS events "
            "FROM observation WHERE ts_utc >= :window_start_utc GROUP BY program",
        )
        check = self.self_test(self.report(registry=(shapeless,)))
        self.assertEqual(check.status, twill_doctor.BROKEN)
        self.assertIn(
            "must emit the columns",
            check.details["detectors"]["D-98@1"]["error"],
        )

    def test_self_test_flags_a_detector_that_silently_selects_nothing(self):
        dead = self.detector(
            "D-01",
            "SELECT program AS key, "
            "count(DISTINCT session_id) AS sessions, count(*) AS events, "
            "min(ts_utc) AS first_seen, max(ts_utc) AS last_seen "
            "FROM observation "
            "WHERE kind = 'never_happens' AND ts_utc >= :window_start_utc "
            "GROUP BY program",
        )
        check = self.self_test(self.report(registry=(dead,)))
        self.assertEqual(check.status, twill_doctor.BROKEN)
        self.assertIn(
            "the self-test fixture is known to hold at least 1",
            check.details["detectors"]["D-01@1"]["error"],
        )

    def test_unlisted_detectors_are_only_required_to_run(self):
        quiet = self.detector(
            "D-97",
            "SELECT program AS key, "
            "count(DISTINCT session_id) AS sessions, count(*) AS events, "
            "min(ts_utc) AS first_seen, max(ts_utc) AS last_seen "
            "FROM observation "
            "WHERE kind = 'never_happens' AND ts_utc >= :window_start_utc "
            "GROUP BY program",
        )
        check = self.self_test(self.report(registry=(quiet,)))
        self.assertEqual(check.status, twill_doctor.HEALTHY)
        self.assertEqual(check.details["detectors"]["D-97@1"]["clusters"], 0)

    def test_self_test_flags_a_session_hit_count_mismatch(self):
        cluster_sql = (
            "SELECT program AS key, "
            "count(DISTINCT session_id) AS sessions, count(*) AS events, "
            "min(ts_utc) AS first_seen, max(ts_utc) AS last_seen "
            "FROM observation "
            "WHERE ts_utc >= :window_start_utc AND program = 'sqlite3' "
            "GROUP BY program"
        )
        hit_sql = (
            "SELECT program AS key, session_id FROM observation "
            "WHERE ts_utc >= :window_start_utc AND program = 'sqlite3' "
            "AND session_id = 'selftest-a' "
            "GROUP BY program, session_id"
        )
        mismatch = self.detector("D-95", cluster_sql, session_hits_sql=hit_sql)
        check = self.self_test(self.report(registry=(mismatch,)))
        self.assertEqual(check.status, twill_doctor.BROKEN)
        self.assertIn(
            "session hit(s)",
            check.details["detectors"]["D-95@1"]["error"],
        )

    def test_self_test_flags_a_non_iso_weekly_week(self):
        cluster_sql = (
            "SELECT program AS key, "
            "count(DISTINCT session_id) AS sessions, count(*) AS events, "
            "min(ts_utc) AS first_seen, max(ts_utc) AS last_seen "
            "FROM observation "
            "WHERE ts_utc >= :window_start_utc AND program IS NOT NULL "
            "GROUP BY program"
        )
        weekly_sql = (
            "SELECT program AS key, '2026-W99' AS week, "
            "count(DISTINCT session_id) AS sessions, count(*) AS events "
            "FROM observation "
            "WHERE ts_utc >= :window_start_utc AND program IS NOT NULL "
            "GROUP BY program"
        )
        broken_week = self.detector("D-96", cluster_sql, weekly_hits_sql=weekly_sql)
        check = self.self_test(self.report(registry=(broken_week,)))
        self.assertEqual(check.status, twill_doctor.BROKEN)
        self.assertIn(
            "non-ISO week",
            check.details["detectors"]["D-96@1"]["error"],
        )


if __name__ == "__main__":
    unittest.main()
