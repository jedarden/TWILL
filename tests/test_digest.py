"""Contract tests for the reproducible weekly digest."""

import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))
twill_digest = importlib.import_module("twill_digest")
twill_schema = importlib.import_module("twill_schema")
twill_detectors = importlib.import_module("twill_detectors")
twill_contract = importlib.import_module("twill_contract")
MISSING_BINARY = twill_detectors.MISSING_BINARY
SUBJECT = (2026, 38)
SUBJECT_LABEL = "2026-W38"
PREVIOUS_LABEL = "2026-W37"
IN_WEEK = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
IN_PREVIOUS = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
START_OF_NEXT_WEEK = datetime(2026, 9, 21, tzinfo=timezone.utc)
AFTER_WEEK = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def seed_failure(
    connection,
    program,
    sessions,
    observed_at,
    *,
    signature=None,
):
    text = signature or "error: command not found"
    sig_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    rows = [
        (
            f"{program}-session-{index}",
            (observed_at + timedelta(minutes=index)).isoformat(),
            program,
            f"{program} --version",
            text,
            sig_hash,
        )
        for index in range(sessions)
    ]
    connection.executemany(
        "INSERT INTO observation(session_id, ts_utc, ts_local, kind, program, "
        "command, signature, sig_hash, excerpt) "
        "VALUES (?, ?, ?, 'run_failed', ?, ?, ?, ?, ?)",
        [
            (
                session_id,
                timestamp,
                timestamp,
                program,
                command,
                text,
                sig_hash,
                text,
            )
            for session_id, timestamp, program, command, text, sig_hash in rows
        ],
    )


class DigestStateCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state dir's data"
        self.connection = twill_schema.connect(self.state)
        self.addCleanup(self.connection.close)

    def build(self, *, registry=(MISSING_BINARY,)):
        self.connection.commit()
        return twill_digest.build_digest(
            self.state,
            SUBJECT,
            registry=registry,
        )


class WeekContractTests(unittest.TestCase):
    def test_iso_week_validation_bounds_and_predecessor(self):
        self.assertEqual(twill_digest.parse_week("2026-W38"), (2026, 38))
        self.assertEqual(
            twill_digest.week_bounds((2026, 38)),
            (
                "2026-09-14T00:00:00+00:00",
                "2026-09-21T00:00:00+00:00",
            ),
        )
        self.assertEqual(twill_digest.previous_week((2026, 1)), (2025, 52))
        for invalid in ("2024-W53", "2026-W00", "26-W38", "2026-w38", "x"):
            with self.subTest(invalid=invalid), self.assertRaises(twill_digest.WeekError):
                twill_digest.parse_week(invalid)

    def test_default_week_is_the_most_recently_completed_iso_week(self):
        self.assertEqual(
            twill_digest.default_week(
                datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
            ),
            (2026, 38),
        )
        self.assertEqual(
            twill_digest.default_week(
                datetime(2026, 9, 21, 8, tzinfo=timezone.utc)
            ),
            (2026, 38),
        )
        self.assertEqual(
            twill_digest.default_week(
                datetime(2026, 9, 20, 23, tzinfo=timezone.utc)
            ),
            (2026, 37),
        )


class DigestDiffTests(DigestStateCase):
    def populate_four_verdicts(self):
        seed_failure(self.connection, "alpha", 2, IN_PREVIOUS)
        seed_failure(self.connection, "alpha", 3, IN_WEEK)
        seed_failure(self.connection, "alpha", 5, START_OF_NEXT_WEEK)
        seed_failure(self.connection, "alpha", 4, AFTER_WEEK)
        seed_failure(self.connection, "beta", 2, IN_PREVIOUS)
        seed_failure(self.connection, "gamma", 2, IN_WEEK)
        seed_failure(self.connection, "delta", 3, IN_PREVIOUS)
        seed_failure(self.connection, "delta", 2, IN_WEEK)
        seed_failure(self.connection, "stable", 2, IN_PREVIOUS)
        seed_failure(self.connection, "stable", 2, IN_WEEK)

    def test_render_time_diff_is_exact_deterministic_and_reproducible(self):
        self.populate_four_verdicts()
        report = self.build()
        self.assertEqual(report.week_id, SUBJECT_LABEL)
        self.assertEqual(report.previous_week_id, PREVIOUS_LABEL)
        self.assertEqual(
            [(finding.verdict, finding.key) for finding in report.findings],
            [
                ("new", "command-not-found:gamma"),
                ("worsening", "command-not-found:alpha"),
                ("improving", "command-not-found:delta"),
                ("gone", "command-not-found:beta"),
            ],
        )
        alpha = next(
            finding for finding in report.findings if finding.key.endswith("alpha")
        )
        self.assertEqual(alpha.current[0], 3)
        self.assertEqual(alpha.previous[0], 2)
        data = twill_digest.render_data(report)
        by_key = {finding["key"]: finding for finding in data["findings"]}
        self.assertIsNone(by_key["command-not-found:beta"]["sessions"])
        self.assertEqual(
            by_key["command-not-found:beta"]["previous"]["sessions"],
            2,
        )
        self.assertNotIn("command-not-found:stable", by_key)

        text = twill_digest.render_text(report)
        lines = text.splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertLessEqual(len(line), twill_digest.MAX_LINE_LENGTH)
            self.assertTrue(line.endswith(f" | $ {report.command}"))
        self.assertIn("new: 1", text)
        self.assertIn("worsening: 1", text)
        self.assertIn("improving: 1", text)
        self.assertIn("gone: 1", text)
        self.assertIn("clean week: no; 4 finding(s)", text)

        self.assertIn("'\"'\"'", report.command)

    def test_findings_are_ranked_by_estimated_waste_not_session_count(self):
        seed_failure(self.connection, "alpha", 2, IN_WEEK)
        seed_failure(self.connection, "beta", 3, IN_WEEK)
        self.connection.executemany(
            "INSERT INTO session_usage(session_id, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd) VALUES (?, ?, ?, ?, ?)",
            [
                ("alpha-session-0", 100, 200, 300, 1.0),
                ("alpha-session-1", 100, 200, 300, 1.0),
                ("beta-session-0", 100, 200, 300, 0.5),
                ("beta-session-1", 100, 200, 300, 0.5),
                ("beta-session-2", 100, 200, 300, 0.5),
            ],
        )
        self.connection.commit()

        report = self.build(registry=(MISSING_BINARY,))

        self.assertEqual(
            [finding.key for finding in report.findings],
            ["command-not-found:alpha", "command-not-found:beta"],
        )
        data = twill_digest.render_data(report)
        self.assertEqual(
            [finding["key"] for finding in data["findings"]],
            ["command-not-found:alpha", "command-not-found:beta"],
        )
        self.assertEqual(data["findings"][0]["estimated_waste_usd"], 2.0)
        self.assertEqual(data["findings"][1]["estimated_waste_usd"], 1.5)
        self.assertEqual(data["findings"][0]["estimated_waste_window"], "current")
        self.assertIn("estimated waste: 2.000000 USD", twill_digest.render_text(report))

    def test_trend_signals_precede_chronic_waste_in_digest_ranking(self):
        seed_failure(self.connection, "chronic", 5, IN_WEEK)
        seed_failure(self.connection, "rising", 4, IN_WEEK)
        self.connection.executemany(
            "INSERT INTO session_usage(session_id, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd) VALUES (?, ?, ?, ?, ?)",
            [
                (f"chronic-session-{index}", 100, 200, 300, 1.0)
                for index in range(5)
            ]
            + [
                (f"rising-session-{index}", 100, 200, 300, 0.1)
                for index in range(4)
            ],
        )
        for index in range(7):
            week = f"2026-W{32 + index:02d}"
            self.connection.execute(
                "INSERT INTO cluster_week(detector_id, key, week, sessions, events) "
                "VALUES ('D-01', ?, ?, ?, ?)",
                ("command-not-found:chronic", week, 5, 5),
            )
            if index < 6:
                sessions, events = 1, 1
            else:
                sessions, events = 4, 4
            self.connection.execute(
                "INSERT INTO cluster_week(detector_id, key, week, sessions, events) "
                "VALUES ('D-01', ?, ?, ?, ?)",
                ("command-not-found:rising", week, sessions, events),
            )
        self.connection.commit()

        report = self.build(registry=(MISSING_BINARY,))

        self.assertEqual(
            [finding.key for finding in report.findings],
            ["command-not-found:rising", "command-not-found:chronic"],
        )
        rising = report.findings[0]
        self.assertEqual(rising.trend_status, "accelerating")
        self.assertEqual(rising.trend_metric, "events")
        self.assertEqual(report.findings[1].trend_status, None)
        data = twill_digest.render_data(report)
        self.assertEqual(data["trend"]["accelerating"], 1)
        self.assertEqual(data["findings"][0]["trend"], "accelerating")
        text = twill_digest.render_text(report)
        self.assertIn("trend: 0 new, 1 accelerating", text)
        self.assertLess(
            text.index("command-not-found:rising"),
            text.index("command-not-found:chronic"),
        )

    def test_production_report_command_reproduces_exact_text(self):
        seed_failure(self.connection, "sqlite3", 2, IN_WEEK)
        self.connection.commit()
        report = twill_digest.build_digest(self.state, SUBJECT)
        text = twill_digest.render_text(report)
        environment = {**os.environ, "PATH": f"{ROOT}{os.pathsep}{os.environ['PATH']}"}
        reproduced = subprocess.run(
            report.command,
            shell=True,
            executable="/bin/sh",
            cwd=ROOT,
            env=environment,
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(reproduced.returncode, 0, reproduced.stderr)
        self.assertEqual(reproduced.stdout, text)

    def test_clean_week_names_every_detector_that_ran(self):
        report = self.build(registry=())
        self.assertTrue(report.clean)
        text = twill_digest.render_text(report)
        self.assertIn("clean week: no findings; detectors ran: none registered", text)
        self.assertIn("lesson flow (last 60 days): unavailable", text)
        self.assertTrue(
            all(
                line.endswith(f" | $ {report.command}")
                for line in text.splitlines()
            )
        )

    def test_lesson_flow_health_counts_states_in_the_completed_week_window(self):
        artifacts = self.root / "artifacts"
        lessons = artifacts / "lessons"
        lessons.mkdir(parents=True)

        def write_lesson(lesson_id, state, timestamp, *, applied_at=None):
            if state.startswith("applied:") or state == "resolved":
                layer = (
                    state.split(":", 1)[1]
                    if state.startswith("applied:")
                    else "environment"
                )
                routing = (
                    f'routing: {{recommended: {layer}, reason: "Use the routed fix.", '
                    f"applied: {layer}, applied_at: {applied_at}, bead: twill-test}}"
                )
            else:
                routing = (
                    'routing: {recommended: environment, reason: "Use the routed fix.", '
                    "applied: null, applied_at: null, bead: null}"
                )
            path = lessons / f"{lesson_id}.md"
            path.write_text(
                "\n".join(
                    [
                        "---",
                        f"id: {lesson_id}",
                        'summary: "A command fails repeatedly. Use the routed fix."',
                        f"state: {state}",
                        "detector: D-01",
                        'key: "command-not-found:test"',
                        'evidence: {sessions: 2, events: 2, first_seen: 2026-08-01, session_ids: ["s1", "s2"]}',
                        routing,
                        "backtest: {window_days: 180, sessions: 2, first_seen: 2026-08-01, weeks_present: 1}",
                        "guard: {layer: null, artifact: null, installed: false}",
                        "---",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            instant = datetime.fromisoformat(timestamp).timestamp()
            os.utime(path, (instant, instant))

        write_lesson("L-00000001", "draft", "2026-08-01T00:00:00+00:00")
        write_lesson("L-00000002", "accepted", "2026-08-02T00:00:00+00:00")
        write_lesson(
            "L-00000003",
            "applied:environment",
            "2026-09-10T00:00:00+00:00",
            applied_at="2026-09-10T00:00:00Z",
        )
        write_lesson(
            "L-00000004",
            "resolved",
            "2026-09-11T00:00:00+00:00",
            applied_at="2026-09-09T00:00:00Z",
        )
        write_lesson("L-00000005", "draft", "2026-06-01T00:00:00+00:00")

        report = twill_digest.build_digest(
            self.state,
            SUBJECT,
            registry=(),
            artifacts_root=artifacts,
        )

        self.assertEqual(
            report.lesson_flow.as_dict(),
            {
                "window_days": 60,
                "window_start": "2026-07-22T23:59:59.999999Z",
                "window_end": "2026-09-20T23:59:59.999999Z",
                "drafted": 1,
                "accepted": 1,
                "applied": 1,
                "resolved": 1,
                "available": True,
                "warning": None,
            },
        )
        data = twill_digest.render_data(report)
        self.assertEqual(data["lesson_flow"]["applied"], 1)
        text = twill_digest.render_text(report)
        self.assertIn(
            "lesson flow (last 60 days): drafted 1, accepted 1, applied 1, resolved 1",
            text,
        )
        self.assertNotIn("no lessons reached applied", text)

    def test_redaction_and_line_budget_never_truncate_the_command(self):
        token = "ghp_" + "1234567890abcdefghijklmnop"
        seed_failure(self.connection, token, 2, IN_WEEK)
        report = self.build()
        text = twill_digest.render_text(report)
        self.assertNotIn(token, text)
        self.assertIn("<redacted:github-token>", text)
        for line in text.splitlines():
            self.assertLessEqual(len(line), twill_digest.MAX_LINE_LENGTH)
            self.assertTrue(line.endswith(f" | $ {report.command}"))

    def test_credential_shaped_state_path_uses_an_environment_reference(self):
        token = "ghp_" + "1234567890abcdefghijklmnop"
        secret_state = self.root / token / "state"
        report = twill_digest.build_digest(secret_state, SUBJECT)
        text = twill_digest.render_text(report)
        data = json.dumps(twill_digest.render_data(report))
        self.assertIn("TWILL_STATE_DIR:?", report.command)
        self.assertNotIn(token, text)
        self.assertNotIn(token, data)
        environment = {
            **os.environ,
            "PATH": f"{ROOT}{os.pathsep}{os.environ['PATH']}",
            "TWILL_STATE_DIR": str(secret_state),
        }
        reproduced = subprocess.run(
            report.command,
            shell=True,
            executable="/bin/sh",
            cwd=ROOT,
            env=environment,
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(reproduced.returncode, 0, reproduced.stderr)
        self.assertEqual(reproduced.stdout, text)
        unset_environment = {
            key: value for key, value in environment.items() if key != "TWILL_STATE_DIR"
        }
        blocked = subprocess.run(
            report.command,
            shell=True,
            executable="/bin/sh",
            cwd=ROOT,
            env=unset_environment,
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("TWILL_STATE_DIR", blocked.stderr)

    def test_nearly_full_command_budget_never_overflows_a_line(self):
        checked: set[int] = set()
        for size in range(1, 300):
            state = self.root / ("x" * size)
            command = twill_digest.reproduction_command(state, SUBJECT)
            if len(command) not in range(231, 235):
                continue
            report = twill_digest.build_digest(state, SUBJECT)
            for line in twill_digest.render_text(report).splitlines():
                self.assertLessEqual(len(line), twill_digest.MAX_LINE_LENGTH)
                self.assertTrue(line.endswith(f" | $ {command}"))
            checked.add(len(command))
        self.assertEqual(checked, set(range(231, 235)))
        long_state = self.root / ("x" * 100) / ("y" * 100) / ("z" * 100)
        long_command = twill_digest.reproduction_command(long_state, SUBJECT)
        self.assertIn("TWILL_STATE_DIR:?", long_command)
        long_report = twill_digest.build_digest(long_state, SUBJECT)
        self.assertTrue(
            all(
                len(line) <= twill_digest.MAX_LINE_LENGTH
                and line.endswith(f" | $ {long_command}")
                for line in twill_digest.render_text(long_report).splitlines()
            )
        )

    def test_non_printable_state_path_cannot_split_a_digest_line(self):
        state = self.root / "line\u2028separator"
        with self.assertRaisesRegex(ValueError, "printable"):
            twill_digest.reproduction_command(state, SUBJECT)

    def test_recorded_detector_semantics_drift_is_not_silently_replayed(self):
        self.connection.execute(
            "INSERT INTO detector_run(detector_id, version, full_id, semantics_sha, "
            "first_run_at, last_run_at, last_status, last_error, clusters, "
            "window_days, attribution_sha, backtest_sha) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "D-01",
                1,
                "D-01@1",
                "drifted",
                IN_WEEK.isoformat(),
                IN_WEEK.isoformat(),
                "ok",
                None,
                0,
                30,
                MISSING_BINARY.attribution_sha,
                MISSING_BINARY.backtest_sha,
            ),
        )
        report = self.build()
        self.assertFalse(report.clean)
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("semantics changed", report.warnings[0])
        self.assertIn("detector error: D-01@1", twill_digest.render_text(report))

    def test_missing_database_is_not_reported_as_clean(self):
        missing = self.root / "missing state"
        report = twill_digest.build_digest(missing, SUBJECT)
        self.assertFalse(report.clean)
        self.assertFalse(missing.exists())
        self.assertIn("no state database", twill_digest.render_text(report))
        self.assertTrue(report.warnings)

    def test_default_registry_replays_rule_documents_for_d09(self):
        self.connection.execute(
            "INSERT INTO rule_doc(path, layer, sha, indexed_at, last_read_by_agent, stale) "
            "VALUES ('/rules/old.md', 'memory', 'sha-old', ?, NULL, 0)",
            (IN_WEEK.isoformat(),),
        )
        self.connection.commit()

        report = twill_digest.build_digest(self.state, SUBJECT)

        d09 = next(
            detector for detector in report.detectors if detector.detector_id == "D-09"
        )
        self.assertEqual(d09.current_status, "ok")
        self.assertEqual(d09.current_clusters, 1)
        self.assertEqual(d09.previous_status, "ok")
        self.assertEqual(d09.previous_clusters, 1)
        self.assertEqual(report.findings, ())

    def test_digest_replays_rule_fts_for_d08(self):
        seed_failure(
            self.connection,
            "sqlite3",
            2,
            IN_WEEK,
            signature="sqlite3: command not found",
        )
        self.connection.execute(
            "INSERT INTO rule_doc(path, layer, sha, indexed_at, last_read_by_agent, stale) "
            "VALUES ('/rules/tools.md', 'memory', 'sha-tools', ?, NULL, 0)",
            (IN_WEEK.isoformat(),),
        )
        self.connection.execute(
            "INSERT INTO rule_fts(text, path) VALUES (?, '/rules/tools.md')",
            ("keep sqlite3 handy for database dumps",),
        )
        self.connection.commit()

        report = self.build(registry=(twill_detectors.STALE_RULE,))

        d08 = next(
            detector for detector in report.detectors if detector.detector_id == "D-08"
        )
        self.assertEqual(d08.current_status, "ok")
        self.assertIsNone(d08.current_error)
        self.assertEqual(d08.current_clusters, 1)
        self.assertEqual(d08.previous_status, "ok")
        self.assertEqual(
            [finding.key for finding in report.findings],
            ["stale-rule:binary:sqlite3:/rules/tools.md"],
        )

    def test_digest_contains_a_read_only_escalation_proposal(self):
        artifacts = self.root / "artifacts"
        lessons = artifacts / "lessons"
        measurements = artifacts / "measurements"
        lessons.mkdir(parents=True)
        measurements.mkdir()
        lesson = lessons / "L-00000001.md"
        lesson.write_text(
            "\n".join(
                [
                    "---",
                    "id: L-00000001",
                    'summary: "A command fails repeatedly. Install it before retrying."',
                    "state: applied:hook",
                    "detector: D-01",
                    'key: "command-not-found:sqlite3"',
                    'evidence: {sessions: 4, events: 4, first_seen: 2026-08-01, session_ids: ["s1", "s2"]}',
                    'routing: {recommended: hook, reason: "Use a hook to stop the recurring failure.", applied: hook, applied_at: 2026-08-20T00:00:00Z, bead: twill-test}',
                    "backtest: {window_days: 180, sessions: 4, first_seen: 2026-08-01, weeks_present: 2}",
                    "guard: {layer: null, artifact: null, installed: false}",
                    "---",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        measurement_path = measurements / "L-00000001.jsonl"
        measurement_path.write_text(
            json.dumps(
                {
                    "lesson_id": "L-00000001",
                    "detector_id": "D-01@1",
                    "measured_at": "2026-09-10T00:00:00Z",
                    "window_days": 7,
                    "sessions": 4,
                    "events": 4,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lesson_before = lesson.read_bytes()
        measurement_before = measurement_path.read_bytes()

        report = twill_digest.build_digest(
            self.state,
            SUBJECT,
            registry=(),
            artifacts_root=artifacts,
        )

        self.assertFalse(report.clean)
        self.assertEqual(len(report.escalations), 1)
        proposal = report.escalations[0]
        self.assertEqual(proposal.next_layer, "environment")
        data = twill_digest.render_data(report)
        self.assertEqual(data["escalations"][0]["lesson_id"], "L-00000001")
        self.assertIn("escalation proposals: 1", twill_digest.render_text(report))
        self.assertEqual(lesson.read_bytes(), lesson_before)
        self.assertEqual(measurement_path.read_bytes(), measurement_before)

    def test_digest_contains_a_read_only_retirement_proposal(self):
        self.connection.execute(
            "INSERT INTO rule_doc(path, layer, sha, indexed_at, stale) "
            "VALUES ('/rules/dormant.md', 'memory', 'sha-dormant', ?, 0)",
            ("2026-05-01T00:00:00+00:00",),
        )
        self.connection.commit()
        before = self.connection.execute(
            "SELECT path, sha, stale FROM rule_doc"
        ).fetchall()

        report = self.build(registry=())

        self.assertFalse(report.clean)
        self.assertEqual(len(report.retirements), 1)
        proposal = report.retirements[0]
        self.assertEqual(proposal.path, "/rules/dormant.md")
        self.assertIsNone(proposal.last_occurrence)
        data = twill_digest.render_data(report)
        self.assertEqual(data["retirements"][0]["path"], "/rules/dormant.md")
        self.assertEqual(
            data["retirements"][0]["removal_owner"],
            "human edit in the owning layer",
        )
        self.assertIn("retirement proposals: 1", twill_digest.render_text(report))
        self.assertEqual(
            self.connection.execute(
                "SELECT path, sha, stale FROM rule_doc"
            ).fetchall(),
            before,
        )

    def test_detector_failure_is_visible_and_not_clean(self):
        detector = twill_digest.Detector(
            "D-99",
            1,
            "references a table that is absent",
            "SELECT key, sessions, events, first_seen, last_seen "
            "FROM missing_table GROUP BY key",
        )
        report = self.build(registry=(detector,))
        self.assertFalse(report.clean)
        self.assertEqual(len(report.warnings), 1)
        text = twill_digest.render_text(report)
        self.assertIn("detector error: D-99@1", text)
        self.assertIn("detector failures: D-99@1", text)


class DigestCliTests(DigestStateCase):
    def run_cli(self, *args, home=None):
        environment = {**os.environ, "PATH": f"{ROOT}{os.pathsep}{os.environ['PATH']}"}
        if home is not None:
            environment["HOME"] = str(home)
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            env=environment,
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        )

    def test_stdout_json_and_usage_contract(self):
        seed_failure(self.connection, "sqlite3", 2, IN_WEEK)
        self.connection.commit()
        result = self.run_cli(
            "digest",
            "--week",
            SUBJECT_LABEL,
            "--stdout",
            "--state-dir",
            str(self.state),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TWILL digest", result.stdout)
        self.assertIn("new: 3", result.stdout)
        self.assertIn(f" | $ twill digest --week {SUBJECT_LABEL} --stdout", result.stdout)

        machine = self.run_cli(
            "digest",
            "--week",
            SUBJECT_LABEL,
            "--json",
            "--state-dir",
            str(self.state),
        )
        self.assertEqual(machine.returncode, 0, machine.stderr)
        payload = json.loads(machine.stdout)
        self.assertEqual(payload["data"]["week"], SUBJECT_LABEL)
        self.assertEqual(payload["data"]["previous_week"], PREVIOUS_LABEL)
        self.assertEqual(len(payload["data"]["findings"]), 3)

        empty = self.run_cli(
            "digest",
            "--week",
            "",
            "--json",
            "--state-dir",
            str(self.state),
        )
        self.assertEqual(empty.returncode, 2)
        self.assertEqual(json.loads(empty.stdout)["error"]["code"], 2)

        invalid = self.run_cli(
            "digest",
            "--week",
            "2024-W53",
            "--json",
            "--state-dir",
            str(self.state),
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(json.loads(invalid.stdout)["error"]["code"], 2)

    def test_bare_command_writes_only_to_configured_artifacts_root(self):
        seed_failure(self.connection, "sqlite3", 2, IN_WEEK)
        self.connection.commit()
        home = self.root / "home"
        config = home / ".config" / "twill" / "config.toml"
        config.parent.mkdir(parents=True)
        token = "ghp_" + "1234567890abcdefghijklmnop"
        artifacts = home / token / "artifacts"
        config.write_text(f'artifacts_root = "{artifacts}"\n', encoding="utf-8")
        result = self.run_cli(
            "digest",
            "--week",
            SUBJECT_LABEL,
            "--state-dir",
            str(self.state),
            home=home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = artifacts / "digests" / f"{SUBJECT_LABEL}.txt"
        self.assertNotIn(token, result.stdout)
        self.assertIn("<redacted:github-token>", result.stdout)
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        self.assertIn(f" | $ twill digest --week {SUBJECT_LABEL} --stdout", artifact.read_text())


class DigestWriteInvariantTests(DigestStateCase):
    """The write boundary enforces the committed-digest line invariants (§3, §8.3)."""

    def setUp(self):
        super().setUp()
        self.artifacts = self.root / "artifacts"
        self.digests = self.artifacts / "digests"

    def test_write_commits_rendered_report_text(self):
        seed_failure(self.connection, "sqlite3", 2, IN_WEEK)
        text = twill_digest.render_text(self.build())

        path = twill_digest.write_digest_file(text, self.artifacts, SUBJECT)

        self.assertEqual(path, self.digests / f"{SUBJECT_LABEL}.txt")
        self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_write_refuses_an_unbounded_line_before_creating_anything(self):
        overlong = "x" * (twill_digest.MAX_LINE_LENGTH + 1)

        with self.assertRaises(twill_contract.ValidationError) as raised:
            twill_digest.write_digest_file(
                f"{overlong} | $ twill digest --week {SUBJECT_LABEL} --stdout\n",
                self.artifacts,
                SUBJECT,
            )

        self.assertIn("exceeds", str(raised.exception))
        self.assertFalse(self.digests.exists())

    def test_write_refuses_unredacted_credential_content_before_creating_anything(self):
        token = "ghp_" + "1234567890abcdefghijklmnop"

        with self.assertRaises(twill_contract.ValidationError) as raised:
            twill_digest.write_digest_file(
                f"new: leaked {token} in a line | $ twill digest --stdout\n",
                self.artifacts,
                SUBJECT,
            )

        self.assertIn("redacted content", str(raised.exception))
        self.assertFalse(self.digests.exists())


if __name__ == "__main__":
    unittest.main()
