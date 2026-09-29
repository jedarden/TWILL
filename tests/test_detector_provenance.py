"""Contract test for detector provenance across the lesson lifecycle.

The README's Measure row promises that every lesson carries the detector
that found it and that Measure re-runs that detector.  This module pins
the whole chain in one place — cluster row, drafted lesson (with its
backtest replay), accepted lesson, measurement — together with the EC-12
guarantees the chain rests on: a measurement series is never silently
redefined, so historical detector versions stay citable and a version's
query-semantics hashes (``semantics_sha``, ``backtest_sha``) cannot be
replaced under the same version number.

The identity convention under test (docs/notes/detector-registry.md):
``cluster`` rows and lesson frontmatter cite the *base* id (``D-01``)
because the cluster is the real-world problem and must survive version
bumps, while run records and measurements cite the *versioned* id
(``D-01@1``) so every point in a series names the semantics that
produced it.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_detectors  # noqa: E402
import twill_explainer  # noqa: E402
import twill_measure  # noqa: E402
import twill_schema  # noqa: E402
from twill_config import TwillConfig  # noqa: E402
from twill_contract import EXIT_VALIDATION_FAILURE, EXIT_SUCCESS  # noqa: E402
from twill_lessons import accept_lesson, load_lesson  # noqa: E402

KEY = "command-not-found:sqlite3"
CLUSTER_ID = f"D-01:{KEY}"
SUMMARY = (
    "Agents repeatedly invoke a missing command, which wastes time. "
    "Install the command before retrying the operation."
)


def _rewritten(sql: str, old: str, new: str) -> str:
    """Apply one textual rewrite and refuse a silent no-op."""

    rewritten = sql.replace(old, new)
    assert rewritten != sql, f"the shipped D-01 SQL no longer contains {old!r}"
    return rewritten


def widened_kind_detector(version: int) -> twill_detectors.Detector:
    """A realistic v-next: tool_error failures join the cluster family."""

    def widen(sql: str) -> str:
        return _rewritten(
            sql, "kind = 'run_failed'", "kind IN ('run_failed', 'tool_error')"
        )

    return twill_detectors.Detector(
        "D-01",
        version,
        "command-not-found across distinct sessions, tool errors included",
        widen(twill_detectors.MISSING_BINARY_SQL),
        session_hits_sql=widen(twill_detectors.MISSING_BINARY_HIT_SQL),
        week_hits_sql=widen(twill_detectors.MISSING_BINARY_WEEK_HIT_SQL),
        weekly_hits_sql=widen(twill_detectors.MISSING_BINARY_WEEKLY_HIT_SQL),
    )


def drifted_cluster_detector() -> twill_detectors.Detector:
    """Same version, changed cluster SQL: the drift EC-12 must refuse."""

    return twill_detectors.Detector(
        "D-01",
        1,
        "command-not-found across distinct sessions",
        _rewritten(
            twill_detectors.MISSING_BINARY_SQL,
            "HAVING count(DISTINCT session_id) >= 2",
            "HAVING count(DISTINCT session_id) >= 1",
        ),
        session_hits_sql=twill_detectors.MISSING_BINARY_HIT_SQL,
        week_hits_sql=twill_detectors.MISSING_BINARY_WEEK_HIT_SQL,
        weekly_hits_sql=twill_detectors.MISSING_BINARY_WEEKLY_HIT_SQL,
    )


def drifted_week_detector() -> twill_detectors.Detector:
    """Cluster SQL untouched, week SQL changed: weeks_present redefined."""

    return twill_detectors.Detector(
        "D-01",
        1,
        "command-not-found across distinct sessions",
        twill_detectors.MISSING_BINARY_SQL,
        session_hits_sql=twill_detectors.MISSING_BINARY_HIT_SQL,
        week_hits_sql=_rewritten(
            twill_detectors.MISSING_BINARY_WEEK_HIT_SQL,
            "HAVING count(DISTINCT session_id) >= 2",
            "HAVING count(DISTINCT session_id) >= 3",
        ),
        weekly_hits_sql=twill_detectors.MISSING_BINARY_WEEKLY_HIT_SQL,
    )


class DetectorProvenanceTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.artifacts = self.root / "artifacts"
        self.config = TwillConfig(artifacts_root=self.artifacts)
        self.connection = twill_schema.connect(self.root / "state")
        self.addCleanup(self.connection.close)
        self.now = datetime.now(timezone.utc)

    # -- fixtures ---------------------------------------------------------

    def seed_observation(self, session_id, days_ago, *, kind="run_failed"):
        observed_at = self.now - timedelta(days=days_ago)
        self.connection.execute(
            "INSERT INTO observation(session_id, ts_utc, ts_local, kind, program, "
            "signature, sig_hash, excerpt) VALUES (?, ?, ?, ?, 'sqlite3', "
            "'sqlite3: command not found', ?, 'sqlite3: command not found')",
            (
                session_id,
                observed_at.isoformat(),
                observed_at.isoformat(),
                kind,
                f"hash-{session_id}",
            ),
        )
        self.connection.commit()

    def seed_recurring_failure(self):
        # Exactly seven days apart, so the two observations always land in
        # consecutive ISO weeks and the backtest's weeks_present is 2
        # regardless of when the test runs.
        self.seed_observation("provenance-a", 10)
        self.seed_observation("provenance-b", 3)

    def detect(self, *, registry=None, days_ago=2.0):
        return twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=registry,
            now=self.now - timedelta(days=days_ago),
        )

    def measure(self, *, registry=None, days_ago=2.0):
        return twill_measure.measure_lessons(
            self.connection,
            self.artifacts,
            registry=registry,
            now=self.now - timedelta(days=days_ago),
            window_days=30,
        )

    def write_accepted_lesson(self, lesson_id, *, detector="D-01"):
        directory = self.artifacts / "lessons"
        directory.mkdir(parents=True, exist_ok=True)
        text = "\n".join(
            (
                "---",
                f"id: {lesson_id}",
                'summary: "A command fails repeatedly. Install it before retrying."',
                "state: accepted",
                f"detector: {detector}",
                f'key: "{KEY}"',
                'evidence: {sessions: 2, events: 2, first_seen: 2026-09-20, '
                'session_ids: ["s1", "s2"]}',
                "routing: {recommended: null, applied: null, applied_at: null, bead: null}",
                "backtest: {window_days: 180, sessions: 2, first_seen: 2026-09-20, "
                "weeks_present: 1}",
                "guard: {layer: null, artifact: null, installed: false}",
                "---",
                "",
            )
        )
        path = directory / f"{lesson_id}.md"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def draft_and_accept_lesson(self):
        """Draft from the live cluster through the real Explain writer."""

        candidates = twill_explainer.load_candidate_prompt_clusters(
            self.connection, 1
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].cluster.detector_id, "D-01")
        self.assertEqual(candidates[0].cluster.key, KEY)
        path = twill_explainer.write_lesson_files(
            (twill_explainer.LessonDraft(CLUSTER_ID, SUMMARY),),
            candidates,
            self.config,
            connection=self.connection,
        )[0]
        draft = load_lesson(path)
        accepted = accept_lesson(self.artifacts, path.stem)
        return path, draft, accepted

    def run_stamps(self):
        return self.connection.execute(
            "SELECT full_id, semantics_sha, attribution_sha, backtest_sha, "
            "weekly_sha, last_status FROM detector_run "
            "WHERE detector_id = 'D-01' ORDER BY version"
        ).fetchall()

    def measurement_rows(self):
        return self.connection.execute(
            "SELECT detector_id, sessions, events FROM measurement "
            "ORDER BY measured_at"
        ).fetchall()

    def mirror_lines(self, lesson_id):
        path = twill_measure.measurement_path(self.artifacts, lesson_id)
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    # -- the chain --------------------------------------------------------

    def test_detector_identity_survives_draft_accept_and_measurement(self):
        self.seed_recurring_failure()
        report = self.detect()
        self.assertEqual(report.exit_code, EXIT_SUCCESS)

        # The cluster row cites the base id; the run record cites the
        # versioned id and stamps every query-semantics hash.
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id, key, sessions, events FROM cluster"
            ).fetchall(),
            [("D-01", KEY, 2, 2)],
        )
        shipped = twill_detectors.select_detectors(
            twill_detectors.REGISTRY, ("D-01",)
        )[0]
        self.assertEqual(
            self.run_stamps(),
            [
                (
                    "D-01@1",
                    shipped.semantics_sha,
                    shipped.attribution_sha,
                    shipped.backtest_sha,
                    shipped.weekly_sha,
                    "ok",
                )
            ],
        )

        path, draft, accepted = self.draft_and_accept_lesson()

        # The draft carries the cluster's detector and a backtest the
        # detector itself produced: replayed over 180 days, both sessions,
        # both ISO weeks.
        self.assertEqual(draft.state, "draft")
        self.assertEqual(draft.detector, "D-01")
        self.assertEqual(draft.key, KEY)
        self.assertEqual(
            draft.backtest,
            {
                "window_days": 180,
                "sessions": 2,
                "first_seen": (self.now - timedelta(days=10)).date().isoformat(),
                "weeks_present": 2,
            },
        )

        # Accepting is an operator transition; provenance never moves.
        self.assertEqual(accepted.state, "accepted")
        self.assertEqual(accepted.detector, draft.detector)
        self.assertEqual(accepted.key, draft.key)
        self.assertEqual(accepted.backtest, draft.backtest)

        measurement = self.measure()
        self.assertEqual(measurement.skipped, ())
        self.assertEqual(len(measurement.measurements), 1)
        point = measurement.measurements[0]
        self.assertEqual(point.lesson_id, path.stem)
        self.assertEqual(point.detector_id, "D-01@1")
        self.assertEqual((point.sessions, point.events), (2, 2))
        self.assertEqual(self.measurement_rows(), [("D-01@1", 2, 2)])
        self.assertEqual(
            [(line["detector_id"], line["sessions"]) for line in self.mirror_lines(path.stem)],
            [("D-01@1", 2)],
        )

    def test_version_bump_redefines_the_series_and_keeps_history(self):
        self.seed_recurring_failure()
        self.detect()
        path, _, _ = self.draft_and_accept_lesson()
        self.measure(days_ago=2)
        self.assertEqual(self.measurement_rows(), [("D-01@1", 2, 2)])

        v2 = widened_kind_detector(2)
        v1 = twill_detectors.select_detectors(twill_detectors.REGISTRY, ("D-01",))[0]
        v1_stamps = self.run_stamps()[0]
        self.assertEqual(
            v1_stamps[1:5],
            (v1.semantics_sha, v1.attribution_sha, v1.backtest_sha, v1.weekly_sha),
        )
        self.assertNotEqual(v2.semantics_sha, v1.semantics_sha)
        self.assertNotEqual(v2.attribution_sha, v1.attribution_sha)
        self.assertNotEqual(v2.backtest_sha, v1.backtest_sha)
        self.assertNotEqual(v2.weekly_sha, v1.weekly_sha)
        self.seed_observation("provenance-c", 2, kind="tool_error")
        bumped = self.detect(registry=(v2,), days_ago=1)
        self.assertEqual(bumped.exit_code, EXIT_SUCCESS)

        # Both versions stay on record with their own hashes: the v1 row
        # is history, not a template to overwrite.
        stamps = self.run_stamps()
        self.assertEqual([row[0] for row in stamps], ["D-01@1", "D-01@2"])
        self.assertEqual(stamps[0][5], "ok")
        self.assertEqual(stamps[0][1:5], v1_stamps[1:5])
        self.assertEqual(stamps[1][1], v2.semantics_sha)
        self.assertEqual(stamps[1][2], v2.attribution_sha)
        self.assertEqual(stamps[1][3], v2.backtest_sha)
        self.assertEqual(stamps[1][4], v2.weekly_sha)
        self.assertNotEqual(stamps[0][1], stamps[1][1])
        self.assertNotEqual(stamps[0][3], stamps[1][3])

        # The lesson still cites the base id, so it rides the active
        # version without an edit, and the new point names D-01@2.
        lesson = load_lesson(path)
        self.assertEqual(lesson.detector, "D-01")
        measurement = self.measure(registry=(v2,), days_ago=1)
        self.assertEqual(measurement.measurements[0].detector_id, "D-01@2")
        self.assertEqual(self.measurement_rows(), [("D-01@1", 2, 2), ("D-01@2", 3, 3)])
        self.assertEqual(
            [(line["detector_id"], line["sessions"]) for line in self.mirror_lines(path.stem)],
            [("D-01@1", 2), ("D-01@2", 3)],
        )

        # A lesson pinned to the historical version is refused rather
        # than silently measured with the old semantics.
        self.write_accepted_lesson("L-00000002", detector="D-01@1")
        with self.assertRaises(twill_measure.MeasurementError) as raised:
            self.measure(registry=(v2,), days_ago=1)
        self.assertEqual(raised.exception.code, EXIT_VALIDATION_FAILURE)
        self.assertIn("is not the active detector version", str(raised.exception))
        self.assertEqual(self.measurement_rows(), [("D-01@1", 2, 2), ("D-01@2", 3, 3)])
        self.assertEqual(len(self.mirror_lines(path.stem)), 2)

    def test_cluster_semantics_drift_is_refused_at_detect_and_measure(self):
        self.seed_recurring_failure()
        self.detect()
        self.write_accepted_lesson("L-00000001")
        self.measure(days_ago=2)

        drifted = drifted_cluster_detector()
        shipped = twill_detectors.select_detectors(
            twill_detectors.REGISTRY, ("D-01",)
        )[0]
        self.assertNotEqual(drifted.semantics_sha, shipped.semantics_sha)

        refused = self.detect(registry=(drifted,), days_ago=1)
        self.assertEqual(refused.exit_code, EXIT_VALIDATION_FAILURE)
        self.assertEqual(refused.outcomes[0].status, "refused")
        self.assertIn("semantics changed without a version bump", refused.outcomes[0].error)
        # The refusal leaves the recorded hash alone: drift never silently
        # redefines the series.
        stamps = self.run_stamps()
        self.assertEqual(stamps[0][1], shipped.semantics_sha)
        self.assertEqual(stamps[0][5], "refused")

        with self.assertRaises(twill_measure.MeasurementError) as raised:
            self.measure(registry=(drifted,), days_ago=1)
        self.assertEqual(raised.exception.code, EXIT_VALIDATION_FAILURE)
        self.assertIn("D-01@1 replay failed validation", str(raised.exception))
        self.assertIn("bump the version", str(raised.exception))
        self.assertEqual(self.measurement_rows(), [("D-01@1", 2, 2)])
        self.assertEqual(len(self.mirror_lines("L-00000001")), 1)

    def test_backtest_semantics_drift_blocks_drafting_and_measuring(self):
        self.seed_recurring_failure()
        self.detect()
        drifted = drifted_week_detector()
        shipped = twill_detectors.select_detectors(
            twill_detectors.REGISTRY, ("D-01",)
        )[0]
        self.assertEqual(drifted.semantics_sha, shipped.semantics_sha)
        self.assertNotEqual(drifted.backtest_sha, shipped.backtest_sha)

        refused = self.detect(registry=(drifted,), days_ago=1)
        self.assertEqual(refused.exit_code, EXIT_VALIDATION_FAILURE)
        self.assertEqual(refused.outcomes[0].status, "refused")
        self.assertIn("backtest semantics changed", refused.outcomes[0].error)
        self.assertEqual(self.run_stamps()[0][3], shipped.backtest_sha)

        # Drafting replays the week query, so a silently redefined
        # weeks_present cannot enter review: the write is refused whole.
        candidates = twill_explainer.load_candidate_prompt_clusters(
            self.connection, 1
        )
        with mock.patch.object(twill_explainer, "REGISTRY", (drifted,)):
            with self.assertRaises(twill_explainer.ValidationError) as raised:
                twill_explainer.write_lesson_files(
                    (twill_explainer.LessonDraft(CLUSTER_ID, SUMMARY),),
                    candidates,
                    self.config,
                    connection=self.connection,
                )
        self.assertIn("backtest replay failed", str(raised.exception))
        self.assertIn("backtest semantics changed", str(raised.exception))
        self.assertEqual(list(self.artifacts.rglob("L-*.md")), [])
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM cluster WHERE detector_id = 'D-01'"
            ).fetchone()[0],
            "open",
        )

        # Measurement replays the same registry and is refused for the
        # same reason.
        self.write_accepted_lesson("L-00000001")
        with self.assertRaises(twill_measure.MeasurementError) as raised:
            self.measure(registry=(drifted,), days_ago=1)
        self.assertEqual(raised.exception.code, EXIT_VALIDATION_FAILURE)
        self.assertIn("backtest semantics changed", str(raised.exception))
        self.assertEqual(self.measurement_rows(), [])


if __name__ == "__main__":
    unittest.main()
