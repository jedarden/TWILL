import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_perf  # noqa: E402


class IngestPerformanceTests(unittest.TestCase):
    def test_vm_hwm_is_converted_from_procfs_kibibytes(self):
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status"
            status.write_text("Name:\tpython\nVmHWM:\t1234 kB\n")
            self.assertEqual(twill_perf.read_peak_rss_bytes(status), 1234 * 1024)

    def test_missing_vm_hwm_is_a_measurement_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status"
            status.write_text("Name:\tpython\nVmRSS:\t12 kB\n")
            with self.assertRaises(twill_perf.PerformanceMeasurementError):
                twill_perf.read_peak_rss_bytes(status)

    def test_budgets_are_strict_and_missing_rss_fails_closed(self):
        passing = twill_perf.assess_ingest(
            twill_perf.HOURLY_INGEST_WALL_TIME_BUDGET_SECONDS - 0.001,
            twill_perf.SINGLE_FILE_PARSE_TIME_BUDGET_SECONDS - 0.001,
            twill_perf.PEAK_RSS_BUDGET_BYTES - 1,
        )
        self.assertEqual(passing["misses"], [])

        at_boundary = twill_perf.assess_ingest(
            twill_perf.HOURLY_INGEST_WALL_TIME_BUDGET_SECONDS,
            twill_perf.SINGLE_FILE_PARSE_TIME_BUDGET_SECONDS,
            twill_perf.PEAK_RSS_BUDGET_BYTES,
        )
        self.assertEqual(len(at_boundary["misses"]), 3)

        unavailable = twill_perf.assess_ingest(0.1, 0.1, None)
        self.assertEqual(len(unavailable["misses"]), 1)
        self.assertIn("VmHWM", unavailable["misses"][0])


class LaterPhasePerformanceTests(unittest.TestCase):
    def test_detect_budget_is_strict(self):
        passing = twill_perf.assess_detect(
            twill_perf.DETECT_PASS_WALL_TIME_BUDGET_SECONDS - 0.001
        )
        self.assertEqual(passing["misses"], [])

        at_boundary = twill_perf.assess_detect(
            twill_perf.DETECT_PASS_WALL_TIME_BUDGET_SECONDS
        )
        self.assertEqual(len(at_boundary["misses"]), 1)
        self.assertIn("wall_time_seconds", at_boundary["misses"][0])

    def test_db_size_includes_sqlite_sidecars_and_is_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "twill.db").write_bytes(b"db")
            (state / "twill.db-wal").write_bytes(b"wal")
            (state / "twill.db-shm").write_bytes(b"shm")
            self.assertEqual(twill_perf.read_db_size_bytes(state), 8)

        passing = twill_perf.assess_db_size(
            twill_perf.DB_SIZE_BUDGET_BYTES - 1
        )
        self.assertEqual(passing["misses"], [])
        at_boundary = twill_perf.assess_db_size(twill_perf.DB_SIZE_BUDGET_BYTES)
        self.assertEqual(len(at_boundary["misses"]), 1)
        self.assertIn("db_bytes", at_boundary["misses"][0])

    def test_missing_database_size_is_a_measurement_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(twill_perf.PerformanceMeasurementError):
                twill_perf.read_db_size_bytes(Path(directory))


if __name__ == "__main__":
    unittest.main()
