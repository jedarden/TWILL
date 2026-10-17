"""D-11 accepts only multi-host, scheme-matched report evidence."""

import argparse
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_crosshost  # noqa: E402
import twill_detectors  # noqa: E402
import twill_schema  # noqa: E402
from twill_config import TwillConfig  # noqa: E402
from twill_contract import CliError  # noqa: E402


SCHEME = twill_crosshost.SIGNATURE_SCHEME
SIGNATURE = hashlib.sha256(b"permission denied").hexdigest()
OTHER_SIGNATURE = hashlib.sha256(b"missing executable").hexdigest()


class CrossHostSignatureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.connection = twill_schema.connect(self.state_dir)
        self.addCleanup(self.connection.close)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def add_tables(self):
        self.connection.execute(twill_crosshost.HOST_REPORT_DDL)
        self.connection.execute(twill_crosshost.HOST_REPORT_BUCKET_DDL)
        self.connection.commit()

    def add_report(self, host, *, days_old=0, scheme=SCHEME):
        generated = self.now - timedelta(days=days_old)
        stamp = generated.isoformat().replace("+00:00", "Z")
        self.connection.execute(
            "INSERT INTO host_report VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                host,
                stamp,
                stamp,
                (generated.date() - timedelta(days=29)).isoformat(),
                generated.date().isoformat(),
                scheme,
                "0" * 64,
            ),
        )
        self.connection.commit()

    def add_bucket(
        self, host, *, days_old=1, kind="run_failed", signature=SIGNATURE,
        scheme=SCHEME, events=2,
    ):
        when = self.now - timedelta(days=days_old)
        stamp = when.isoformat().replace("+00:00", "Z")
        self.connection.execute(
            "INSERT INTO host_report_bucket VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (host, when.date().isoformat(), kind, scheme, signature, 1, events, stamp, stamp),
        )
        self.connection.commit()

    def run_detector(self):
        return twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_crosshost.CROSS_HOST_SIGNATURE,),
            now=self.now,
        )

    def test_old_database_and_empty_accepted_report(self):
        self.assertFalse(twill_crosshost.host_reports_imported(self.connection))
        self.add_tables()
        self.assertFalse(twill_crosshost.host_reports_imported(self.connection))
        self.add_report("host-a")
        self.assertTrue(twill_crosshost.host_reports_imported(self.connection))
        report = self.run_detector()
        self.assertEqual(report.exit_code, 0)
        self.assertEqual([(item.full_id, item.clusters) for item in report.outcomes], [("D-11@1", 0)])

    def test_distinct_hosts_across_kinds_and_days_with_stale_contributor(self):
        self.add_tables()
        self.add_report("host-a")
        self.add_report("host-b", days_old=4)
        self.add_report("host-c")
        self.add_report("other-scheme", scheme="future/v2")
        self.add_bucket("host-a", events=3)
        self.add_bucket("host-a", days_old=2, kind="tool_error", events=5)
        self.add_bucket("host-b", kind="hook_denial", events=7)
        self.add_bucket("host-c", signature=OTHER_SIGNATURE)
        self.add_bucket("other-scheme", scheme="future/v2", events=19)
        self.add_bucket("orphan", events=23)

        report = self.run_detector()
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.outcomes[0].clusters, 1)
        row = self.connection.execute(
            "SELECT key, sessions, events FROM cluster WHERE detector_id = 'D-11'"
        ).fetchone()
        self.assertEqual(row, (twill_crosshost.KEY_PREFIX + SIGNATURE, 2, 15))
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM cluster_session WHERE detector_id = 'D-11'"
            ).fetchone()[0],
            0,
        )
        findings = twill_crosshost.findings(
            self.connection,
            window_start_utc=report.window_start_utc,
            window_days=report.window_days,
            now=self.now,
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["hosts"], 2)
        self.assertEqual(findings[0]["events"], 15)
        self.assertEqual(
            [(item["host_id"], item["freshness"]) for item in findings[0]["contributors"]],
            [("host-a", "current"), ("host-b", "stale_host")],
        )
        self.assertEqual(len(findings[0]["contributors"][0]["days"]), 2)

    def test_one_host_and_out_of_window_evidence_do_not_form_finding(self):
        self.add_tables()
        self.add_report("host-a")
        self.add_report("host-b")
        self.add_bucket("host-a")
        self.add_bucket("host-a", days_old=2, kind="tool_error")
        self.add_bucket("host-b", days_old=32)
        report = self.run_detector()
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.outcomes[0].clusters, 0)

    def test_systemic_signature_keeps_single_host_weeks_in_its_series(self):
        self.add_tables()
        self.add_report("host-a")
        self.add_report("host-b")
        self.add_bucket("host-a", days_old=1, events=3)
        self.add_bucket("host-b", days_old=8, events=4)
        report = self.run_detector()
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.outcomes[0].clusters, 1)
        weeks = self.connection.execute(
            "SELECT sessions, events FROM cluster_week WHERE detector_id = 'D-11' "
            "ORDER BY week"
        ).fetchall()
        self.assertEqual(weeks, [(1, 4), (1, 3)])

    def test_weekly_series_includes_current_day_buckets(self):
        self.add_tables()
        self.add_report("host-a")
        self.add_report("host-b")
        self.add_bucket("host-a", days_old=0, events=3)
        self.add_bucket("host-b", days_old=0, kind="tool_error", events=4)

        report = self.run_detector()

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT sessions, events FROM cluster_week WHERE detector_id = 'D-11'"
            ).fetchall(),
            [(2, 7)],
        )

    def test_detect_cli_lists_contributing_hosts_and_rejects_explicit_d11_without_input(self):
        args = argparse.Namespace(
            window=None, detector=["D-11"], state_dir=str(self.state_dir), json=True
        )
        config = TwillConfig(artifacts_root=Path(self.temporary.name) / "artifacts")
        with mock.patch.object(twill_app, "load_config", return_value=config):
            with self.assertRaises(CliError) as missing:
                twill_app.detect_command(args)
            self.assertEqual(missing.exception.code, 1)
            self.add_tables()
            self.add_report("host-a")
            self.add_report("host-b")
            self.add_bucket("host-a")
            self.add_bucket("host-b", kind="tool_error")
            captured = StringIO()
            with redirect_stdout(captured):
                self.assertEqual(twill_app.detect_command(args), 0)
            args.json = False
            human = StringIO()
            with redirect_stdout(human):
                self.assertEqual(twill_app.detect_command(args), 0)
        payload = json.loads(captured.getvalue())["data"]
        self.assertEqual(payload["detectors"][0]["full_id"], "D-11@1")
        self.assertEqual(
            [item["host_id"] for item in payload["cross_host_signatures"][0]["contributors"]],
            ["host-a", "host-b"],
        )
        self.assertIn("host-a:", human.getvalue())
        self.assertIn("host-b:", human.getvalue())
        self.assertIn("(current)", human.getvalue())


if __name__ == "__main__":
    unittest.main()
