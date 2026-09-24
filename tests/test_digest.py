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
        self.assertTrue(
            all(
                line.endswith(f" | $ {report.command}")
                for line in text.splitlines()
            )
        )

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
        self.assertIn("new: 2", result.stdout)
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
        self.assertEqual(len(payload["data"]["findings"]), 2)

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


if __name__ == "__main__":
    unittest.main()
