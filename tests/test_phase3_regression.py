"""Phase 3 regression anchors: ranking, weekly aggregation, and rule decay.

Each test pins an exact value the plan states as a formula or a decision —
the score weights, the EWMA band, the equal-split attribution arithmetic, the
coverage gate, the recurrence directions — computed here from the plan's own
description rather than by calling the implementation's helpers, so an
accidental change to any weight, tie-break, or empty-input default fails with
a diff against the intended behaviour instead of a silently moved expectation.
"""

import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_detectors  # noqa: E402
import twill_digest  # noqa: E402
import twill_ranker  # noqa: E402
import twill_rules  # noqa: E402
import twill_schema  # noqa: E402
import twill_trend  # noqa: E402

DETECT_NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
RANK_NOW = "2026-09-24T00:00:00+00:00"
RULES_NOW = "2026-09-28T00:00:00+00:00"
MALFORMED = "not-a-timestamp"


def seed_failure(connection, session_id, at, *, program="sqlite3"):
    """Insert one D-01-shaped run_failed observation."""

    connection.execute(
        "INSERT INTO observation(session_id, ts_utc, ts_local, kind, program, "
        "command, signature, sig_hash) VALUES (?, ?, ?, 'run_failed', ?, ?, ?, ?)",
        (
            session_id,
            at.isoformat(),
            at.isoformat(),
            program,
            f"{program} --version",
            f"{program}: command not found",
            f"hash-{program}",
        ),
    )


def seed_usage(
    connection,
    session_id,
    *,
    input_tokens=None,
    output_tokens=None,
    cache_read_tokens=None,
    cost_usd=None,
):
    connection.execute(
        "INSERT INTO session_usage(session_id, input_tokens, output_tokens, "
        "cache_read_tokens, cost_usd) VALUES (?, ?, ?, ?, ?)",
        (
            session_id,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cost_usd,
        ),
    )


def plan_score(sessions, events, age_days, window_days):
    """The score exactly as plan §9 Phase 3 states it (2026-09-24 decision)."""

    return (
        4.0 * math.log1p(sessions)
        + math.log1p(events)
        + 2.0 ** (-age_days / window_days)
    )


def plan_band(prior, alpha, sigmas):
    """An EWMA level plus a population-sigma band, per the trend decision."""

    level = float(prior[0])
    for value in prior[1:]:
        level = alpha * float(value) + (1.0 - alpha) * level
    mean = sum(float(value) for value in prior) / len(prior)
    variance = sum((float(value) - mean) ** 2 for value in prior) / len(prior)
    return level, level + sigmas * math.sqrt(variance)


class Phase3StateCase(unittest.TestCase):
    """Shared connection over a disposable state directory."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.connection = twill_schema.connect(self.root / "state")
        self.addCleanup(self.connection.close)

    def add_cluster(
        self,
        detector_id,
        key,
        *,
        sessions=2,
        events=2,
        first_seen="2026-09-01T00:00:00+00:00",
        last_seen=RANK_NOW,
        window_days=30,
        state="open",
        covered_by=None,
    ):
        self.connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score, covered_by, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?)",
            (
                detector_id,
                key,
                window_days,
                sessions,
                events,
                first_seen,
                last_seen,
                covered_by,
                state,
            ),
        )
        # run_rank opens its own BEGIN IMMEDIATE, so seeds must not leave an
        # implicit transaction open — the same convention test_ranker uses.
        self.connection.commit()

    def add_week(self, detector_id, key, week, sessions, events):
        self.connection.execute(
            "INSERT INTO cluster_week(detector_id, key, week, sessions, events) "
            "VALUES (?, ?, ?, ?, ?)",
            (detector_id, key, week, sessions, events),
        )


class ScoreFormulaRegressionTests(Phase3StateCase):
    """The persisted score is the plan formula, weight for weight."""

    def test_formula_anchors_pin_the_weights_and_recency(self):
        # (2, 2) at age zero: 4·ln3 + ln3 + 2^0.
        self.assertAlmostEqual(
            twill_ranker.score_cluster(2, 2, RANK_NOW, 30, as_of=RANK_NOW),
            5.0 * math.log1p(2) + 1.0,
            places=12,
        )
        # One window of age with no counts is exactly the half-life term.
        half_life = (
            datetime(2026, 9, 24, tzinfo=timezone.utc) - timedelta(days=30)
        ).isoformat()
        self.assertAlmostEqual(
            twill_ranker.score_cluster(0, 0, half_life, 30, as_of=RANK_NOW),
            0.5,
            places=12,
        )
        # The recency term is window-relative: 15 days is a half-life only
        # for a 15-day window.
        age_15 = (
            datetime(2026, 9, 24, tzinfo=timezone.utc) - timedelta(days=15)
        ).isoformat()
        self.assertAlmostEqual(
            twill_ranker.score_cluster(0, 0, age_15, 30, as_of=RANK_NOW),
            2.0 ** -0.5,
            places=12,
        )
        self.assertAlmostEqual(
            twill_ranker.score_cluster(0, 0, age_15, 15, as_of=RANK_NOW),
            0.5,
            places=12,
        )

    def test_breadth_outweighs_event_volume(self):
        # With recency removed, one extra session is worth more than three
        # extra events: the 4x session weight is the point of that constant.
        one_session = twill_ranker.score_cluster(1, 0, MALFORMED, 30)
        three_events = twill_ranker.score_cluster(0, 3, MALFORMED, 30)
        self.assertAlmostEqual(one_session, 4.0 * math.log1p(1), places=12)
        self.assertAlmostEqual(three_events, 1.0 * math.log1p(3), places=12)
        self.assertGreater(one_session, three_events)

    def test_malformed_last_seen_contributes_zero_recency(self):
        self.assertAlmostEqual(
            twill_ranker.score_cluster(3, 5, MALFORMED, 30, as_of=RANK_NOW),
            4.0 * math.log1p(3) + math.log1p(5),
            places=12,
        )
        self.assertAlmostEqual(
            twill_ranker.score_cluster(3, 5, "", 30, as_of=RANK_NOW),
            4.0 * math.log1p(3) + math.log1p(5),
            places=12,
        )

    def test_future_last_seen_is_clamped_to_full_recency(self):
        future = "2026-10-01T00:00:00+00:00"
        self.assertAlmostEqual(
            twill_ranker.score_cluster(2, 2, future, 30, as_of=RANK_NOW),
            5.0 * math.log1p(2) + 1.0,
            places=12,
        )

    def test_refresh_persists_the_formula_for_every_cluster(self):
        reference = datetime(2026, 9, 24, tzinfo=timezone.utc)
        rows = [
            ("D-01", "broad", 40, 900, 1, 30, "2026-09-23T00:00:00+00:00"),
            ("D-02", "fresh-small", 2, 2, 7, 30, "2026-09-17T00:00:00+00:00"),
            ("D-03", "short-window", 3, 4, 2, 7, "2026-09-22T06:00:00+00:00"),
        ]
        for detector_id, key, sessions, events, _w, window_days, last_seen in rows:
            self.add_cluster(
                detector_id,
                key,
                sessions=sessions,
                events=events,
                window_days=window_days,
                last_seen=last_seen,
            )

        twill_ranker.refresh_scores(self.connection, as_of=RANK_NOW)

        for detector_id, key, sessions, events, _, window_days, last_seen in rows:
            observed = self.connection.execute(
                "SELECT score FROM cluster WHERE detector_id = ? AND key = ?",
                (detector_id, key),
            ).fetchone()[0]
            age_days = (
                reference - datetime.fromisoformat(last_seen)
            ).total_seconds() / 86400.0
            self.assertAlmostEqual(
                observed,
                plan_score(sessions, events, age_days, window_days),
                places=9,
                msg=f"{detector_id}/{key}",
            )


class RankTieRegressionTests(Phase3StateCase):
    """Equal scores fall through a fixed tie-break chain, stably."""

    FUTURE_A = "2026-09-25T00:00:00+00:00"
    FUTURE_B = "2026-09-26T00:00:00+00:00"

    def test_full_tie_falls_through_to_last_seen_then_detector_then_key(self):
        # All four rows share sessions/events; future timestamps clamp to the
        # same full recency, so the scores tie exactly.
        rows = [
            ("D-02", "alpha", self.FUTURE_A),
            ("D-01", "zeta", self.FUTURE_A),
            ("D-03", "alpha", self.FUTURE_B),
            ("D-01", "alpha", self.FUTURE_A),
        ]
        for detector_id, key, last_seen in rows:
            self.add_cluster(detector_id, key, last_seen=last_seen)

        report = twill_ranker.rank_clusters(self.connection, 10, as_of=RANK_NOW)

        self.assertEqual(
            [(row.detector_id, row.key) for row in report.all_clusters],
            [
                ("D-03", "alpha"),
                ("D-01", "alpha"),
                ("D-01", "zeta"),
                ("D-02", "alpha"),
            ],
        )
        scores = [row.score for row in report.all_clusters]
        self.assertAlmostEqual(scores[0], scores[-1], places=12)
        self.assertEqual(
            [(row.detector_id, row.key) for row in report.clusters],
            [(row.detector_id, row.key) for row in report.all_clusters],
        )

    def test_top_k_cuts_tied_candidates_in_tie_break_order(self):
        for key in ("bravo", "charlie", "alpha"):
            self.add_cluster("D-01", f"command-not-found:{key}")

        report = twill_ranker.rank_clusters(self.connection, 2, as_of=RANK_NOW)

        self.assertEqual(
            [row.key for row in report.clusters],
            ["command-not-found:alpha", "command-not-found:bravo"],
        )
        # The cut candidate survives in the full ordering — top-k limits the
        # review lane, not the evidence — and it is still a new-lesson
        # candidate: suppression is a review-state gate, not the top-k cut.
        self.assertEqual(
            [row.key for row in report.all_clusters],
            ["command-not-found:alpha", "command-not-found:bravo", "command-not-found:charlie"],
        )
        self.assertEqual(report.suppressed_clusters, ())

    def test_ranking_is_identical_across_a_rerun(self):
        self.add_cluster("D-01", "old", last_seen="2026-09-09T00:00:00+00:00")
        self.add_cluster("D-01", "big", sessions=10, events=20)
        self.add_cluster("D-02", "tied", last_seen=self.FUTURE_A)
        self.add_cluster("D-02", "also-tied", last_seen=self.FUTURE_A)

        first = twill_ranker.rank_clusters(self.connection, 10, as_of=RANK_NOW)
        second = twill_ranker.rank_clusters(self.connection, 10, as_of=RANK_NOW)

        self.assertEqual(
            [(row.detector_id, row.key) for row in first.all_clusters],
            [(row.detector_id, row.key) for row in second.all_clusters],
        )
        self.assertEqual(
            [row.score for row in first.all_clusters],
            [row.score for row in second.all_clusters],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT score FROM cluster ORDER BY detector_id, key"
            ).fetchall(),
            self.connection.execute(
                "SELECT score FROM cluster ORDER BY detector_id, key"
            ).fetchall(),
        )


class WeeklyAggregationRegressionTests(Phase3StateCase):
    """cluster_week cells, equal-split dollars, and idempotent re-derivation."""

    def seed_series(self):
        # Session "a" fails twice in W38 and once in W39 (a repeated
        # observation must not raise that week's share); "b" only W38;
        # "c" only W39 with a known cost; "d" only W39 with no cost.
        seed_failure(self.connection, "a", datetime(2026, 9, 15, 10, tzinfo=timezone.utc))
        seed_failure(self.connection, "a", datetime(2026, 9, 16, 10, tzinfo=timezone.utc))
        seed_failure(self.connection, "b", datetime(2026, 9, 15, 11, tzinfo=timezone.utc))
        seed_failure(self.connection, "a", datetime(2026, 9, 22, 10, tzinfo=timezone.utc))
        seed_failure(self.connection, "c", datetime(2026, 9, 22, 11, tzinfo=timezone.utc))
        seed_failure(self.connection, "d", datetime(2026, 9, 23, 10, tzinfo=timezone.utc))
        seed_usage(self.connection, "a", cost_usd=2.0)
        seed_usage(self.connection, "b", cost_usd=1.0)
        seed_usage(self.connection, "c", cost_usd=1.0)
        seed_usage(self.connection, "d", cost_usd=None)
        self.connection.commit()

    def weekly_rows(self):
        return self.connection.execute(
            "SELECT detector_id, key, week, sessions, events, est_waste_usd "
            "FROM cluster_week ORDER BY week"
        ).fetchall()

    def test_weekly_cells_counts_and_equal_split_dollars(self):
        self.seed_series()

        report = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.MISSING_BINARY,),
            now=DETECT_NOW,
        )

        self.assertEqual(report.exit_code, 0)
        # Four distinct sessions, six observations: a three times, b, c, and
        # d once each.
        self.assertEqual(
            self.connection.execute(
                "SELECT sessions, events, first_seen, last_seen FROM cluster"
            ).fetchall(),
            [
                (
                    4,
                    6,
                    "2026-09-15T10:00:00+00:00",
                    "2026-09-23T10:00:00+00:00",
                )
            ],
        )
        # W38: two distinct sessions, three events; "a" is counted once for
        # the split even though it failed twice.  W39: three sessions, and
        # the estimate is NULL because "d" has no known cost.
        self.assertEqual(
            self.weekly_rows(),
            [
                ("D-01", "command-not-found:sqlite3", "2026-W38", 2, 3, 2.0),
                ("D-01", "command-not-found:sqlite3", "2026-W39", 3, 3, None),
            ],
        )
        # W38 = a(2.00 split over its two cells → 1.00) + b(1.00 whole).

    def test_a_rerun_rederives_identical_cluster_and_week_rows(self):
        self.seed_series()
        first = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.MISSING_BINARY,),
            now=DETECT_NOW,
        )
        cluster_before = self.connection.execute(
            "SELECT detector_id, key, sessions, events, first_seen, last_seen, "
            "score, covered_by, state FROM cluster"
        ).fetchall()
        weeks_before = self.weekly_rows()

        second = twill_detectors.run_detectors(
            self.connection,
            window_days=30,
            registry=(twill_detectors.MISSING_BINARY,),
            now=DETECT_NOW,
        )

        self.assertEqual((first.exit_code, second.exit_code), (0, 0))
        self.assertEqual(
            self.connection.execute(
                "SELECT detector_id, key, sessions, events, first_seen, "
                "last_seen, score, covered_by, state FROM cluster"
            ).fetchall(),
            cluster_before,
        )
        self.assertEqual(self.weekly_rows(), weeks_before)


class CoverageRegressionTests(Phase3StateCase):
    """The Phase 3 completion criterion: covered vs open after one rank."""

    def test_run_rank_marks_the_seeded_match_and_leaves_the_rest_open(self):
        rules = self.root / "rules"
        rules.mkdir()
        useful = rules / "useful.md"
        useful.write_text(
            "Install the real binary instead.\ncommand-not-found:sqlite3\n"
        )
        (rules / "unrelated.md").write_text("Nothing about binaries here.\n")
        self.add_cluster(
            "D-01",
            "command-not-found:sqlite3",
            sessions=5,
            events=9,
            last_seen="2026-09-23T00:00:00+00:00",
        )
        self.add_cluster(
            "D-01",
            "command-not-found:ghost-binary",
            sessions=2,
            events=2,
            last_seen="2026-09-23T00:00:00+00:00",
        )

        run = twill_ranker.run_rank(
            self.connection,
            (f"memory:{rules}/*.md",),
            top_k=10,
            as_of=RANK_NOW,
        )

        self.assertEqual(run.index.docs, 2)
        self.assertEqual(run.ranking.coverage.covered, 1)
        self.assertEqual(run.ranking.coverage.uncovered, 1)
        self.assertEqual(
            [row.key for row in run.ranking.clusters],
            ["command-not-found:ghost-binary"],
        )
        covered = run.ranking.covered_clusters
        self.assertEqual([row.key for row in covered], ["command-not-found:sqlite3"])
        self.assertEqual(covered[0].covered_by, str(useful))
        self.assertFalse(covered[0].covered_by.endswith("unrelated.md"))
        self.assertTrue(covered[0].escalation_candidate)
        self.assertFalse(covered[0].new_lesson_candidate)
        self.assertTrue(run.ranking.clusters[0].new_lesson_candidate)
        self.assertEqual(
            self.connection.execute(
                "SELECT covered_by FROM cluster WHERE key = 'command-not-found:sqlite3'"
            ).fetchone()[0],
            str(useful),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM cluster WHERE key = 'command-not-found:sqlite3'"
            ).fetchone()[0],
            "open",
        )


class TrendRegressionTests(Phase3StateCase):
    """EWMA change-point classification with zero-filled empty weeks."""

    def seed_series(self):
        # "steady" keeps every week present (and gives the detector six
        # weeks of history); "gap" misses W36; "fresh" appears only in the
        # latest week; "quiet" stops before the latest week.
        for index in range(6):
            week = f"2026-W{33 + index:02d}"
            self.add_week("D-02", "steady", week, 1, 1)
        for week in ("2026-W33", "2026-W34", "2026-W35", "2026-W37"):
            self.add_week("D-02", "gap", week, 1, 1)
        self.add_week("D-02", "gap", "2026-W38", 2, 2)
        self.add_week("D-02", "fresh", "2026-W38", 2, 2)
        for week in ("2026-W33", "2026-W34", "2026-W35", "2026-W36"):
            self.add_week("D-02", "quiet", week, 1, 1)
        self.connection.commit()

    def test_flat_gap_new_and_gone_series_classify_deterministically(self):
        self.seed_series()

        report = twill_trend.build_trend_report(
            self.connection,
            detector="D-02",
            weeks=6,
        )

        self.assertTrue(report.history_sufficient)
        self.assertEqual(report.latest_week, "2026-W38")
        self.assertEqual(
            [(finding.key, finding.status) for finding in report.findings],
            [
                ("fresh", twill_trend.TREND_NEW),
                ("gap", twill_trend.TREND_ACCELERATING),
            ],
        )
        # "steady" is chronic but flat and "quiet" is gone: neither is a
        # change point, and neither borrows the other's baseline.
        self.assertNotIn(
            "steady", {finding.key for finding in report.findings}
        )
        self.assertNotIn("quiet", {finding.key for finding in report.findings})

        # The empty W36 reads as zero: the band over [1, 1, 1, 0, 1] is
        # EWMA 0.79 + 2σ(0.4) = 1.59, so the latest 2 exceeds it by 0.41.
        alpha = twill_trend.EWMA_ALPHA
        sigmas = twill_trend.EWMA_BAND_SIGMAS
        self.assertEqual((alpha, sigmas), (0.3, 2.0))
        level, band = plan_band([1, 1, 1, 0, 1], alpha, sigmas)
        gap = report.findings[1]
        self.assertAlmostEqual(gap.events_ewma, level, places=12)
        self.assertAlmostEqual(gap.sessions_ewma, level, places=12)
        self.assertAlmostEqual(gap.events_band, band, places=12)
        self.assertAlmostEqual(gap.signal_excess, 2.0 - band, places=12)
        self.assertEqual(gap.signal_metric, "events")
        self.assertEqual(gap.latest_week, "2026-W38")
        self.assertEqual(gap.history_weeks, 6)

        fresh = report.findings[0]
        zero_band = plan_band([0, 0, 0, 0, 0], alpha, sigmas)
        self.assertAlmostEqual(fresh.events_ewma, zero_band[0], places=12)
        self.assertAlmostEqual(fresh.events_band, zero_band[1], places=12)
        self.assertAlmostEqual(fresh.signal_excess, 2.0, places=12)

    def test_insufficient_history_is_explicit_not_silent(self):
        self.add_week("D-02", "thin", "2026-W37", 2, 2)
        self.add_week("D-02", "thin", "2026-W38", 9, 9)
        self.connection.commit()

        report = twill_trend.build_trend_report(
            self.connection,
            detector="D-02",
            weeks=12,
        )

        self.assertFalse(report.history_sufficient)
        self.assertEqual(report.history_weeks, 2)
        finding = report.findings[0]
        self.assertEqual(finding.status, twill_trend.TREND_INSUFFICIENT_HISTORY)
        self.assertIsNone(finding.sessions_ewma)
        self.assertIsNone(finding.events_band)
        self.assertIn("at least 6 weeks", report.warnings[0])

    def test_no_history_at_all_is_a_warning_not_an_error(self):
        report = twill_trend.build_trend_report(self.connection)

        self.assertIsNone(report.latest_week)
        self.assertEqual(report.findings, ())
        self.assertEqual(report.history_weeks, 0)
        self.assertIn("run twill detect", report.warnings[0])


class RulesRegressionTests(Phase3StateCase):
    """Recurrence directions, decay, deletion candidates, retirement."""

    def add_rule(
        self,
        path,
        sha,
        indexed_at,
        *,
        last_read=None,
        stale=0,
    ):
        self.connection.execute(
            "INSERT INTO rule_doc(path, layer, sha, indexed_at, "
            "last_read_by_agent, stale) VALUES (?, 'memory', ?, ?, ?, ?)",
            (path, sha, indexed_at, last_read, stale),
        )

    def test_every_recurrence_direction_is_classified_with_exact_deltas(self):
        self.add_rule(
            "/rules/live.md",
            "sha-live",
            "2026-08-01T00:00:00+00:00",
            last_read="2026-09-27T00:00:00+00:00",
        )
        series = {
            "up-key": [("2026-W37", 2, 3), ("2026-W38", 3, 5)],
            "down-key": [("2026-W37", 3, 5), ("2026-W38", 2, 3)],
            "flat-key": [("2026-W37", 2, 3), ("2026-W38", 2, 3)],
            "sessions-up-key": [("2026-W37", 2, 5), ("2026-W38", 3, 5)],
            "sessions-down-key": [("2026-W37", 3, 5), ("2026-W38", 2, 5)],
            # W38 sits between W37 and W39, so the two rows are not adjacent.
            "gap-key": [("2026-W37", 2, 2), ("2026-W39", 2, 2)],
            "single-week-key": [("2026-W38", 2, 2)],
            "no-series-key": [],
        }
        for key, weeks in series.items():
            self.add_cluster("D-01", key, covered_by="/rules/live.md")
            for week, sessions, events in weeks:
                self.add_week("D-01", key, week, sessions, events)
        self.connection.commit()

        report = twill_rules.build_rules_report(
            self.connection,
            unread_days=90,
            now=RULES_NOW,
        )

        self.assertEqual(len(report.rules), 1)
        rule = report.rules[0]
        self.assertTrue(rule.covered)
        self.assertFalse(rule.unread)
        self.assertFalse(rule.deletion_candidate)
        self.assertEqual(report.deletion_candidates, ())
        by_key = {cluster.key: cluster.recurrence for cluster in rule.clusters}
        expected = {
            "up-key": (
                twill_rules.RECURRENCE_UP,
                "2026-W37",
                "2026-W38",
                1,
                2,
            ),
            "down-key": (
                twill_rules.RECURRENCE_DOWN,
                "2026-W37",
                "2026-W38",
                -1,
                -2,
            ),
            "flat-key": (
                twill_rules.RECURRENCE_FLAT,
                "2026-W37",
                "2026-W38",
                0,
                0,
            ),
            # Events tie: the session breadth decides.
            "sessions-up-key": (
                twill_rules.RECURRENCE_UP,
                "2026-W37",
                "2026-W38",
                1,
                0,
            ),
            "sessions-down-key": (
                twill_rules.RECURRENCE_DOWN,
                "2026-W37",
                "2026-W38",
                -1,
                0,
            ),
        }
        for key, (direction, previous, current, sessions, events) in expected.items():
            recurrence = by_key[key]
            self.assertEqual(recurrence.direction, direction, key)
            self.assertEqual(recurrence.previous_week, previous, key)
            self.assertEqual(recurrence.current_week, current, key)
            self.assertEqual(recurrence.sessions_delta, sessions, key)
            self.assertEqual(recurrence.events_delta, events, key)
            self.assertTrue(recurrence.comparable, key)
        for key in ("gap-key", "single-week-key", "no-series-key"):
            recurrence = by_key[key]
            self.assertEqual(recurrence.direction, twill_rules.RECURRENCE_UNKNOWN, key)
            self.assertFalse(recurrence.comparable, key)
            self.assertIsNone(recurrence.sessions_delta, key)
            self.assertIsNone(recurrence.events_delta, key)
        self.assertEqual(by_key["gap-key"].current_week, "2026-W39")
        self.assertIsNone(by_key["gap-key"].previous_week)
        self.assertIsNone(by_key["no-series-key"].current_week)

    def test_decay_lanes_and_retirement_proposals_are_separated(self):
        old = "2026-06-01T00:00:00+00:00"
        self.add_rule("/rules/dead.md", "sha-dead", old)
        self.add_rule("/rules/faded-covered.md", "sha-faded", old)
        self.add_cluster(
            "D-01",
            "faded-key",
            covered_by="/rules/faded-covered.md",
            last_seen="2026-06-15T00:00:00+00:00",
        )
        self.add_rule("/rules/dormant-covered.md", "sha-dormant", old)
        self.add_cluster(
            "D-01",
            "dormant-key",
            covered_by="/rules/dormant-covered.md",
            last_seen="2026-09-27T00:00:00+00:00",
        )
        self.add_rule(
            "/rules/active.md",
            "sha-active",
            old,
            last_read="2026-09-27T00:00:00+00:00",
        )
        self.add_rule("/rules/young.md", "sha-young", "2026-09-27T00:00:00+00:00")
        self.connection.commit()

        report = twill_rules.build_rules_report(
            self.connection,
            unread_days=90,
            now=RULES_NOW,
        )

        self.assertEqual(
            [rule.path for rule in report.deletion_candidates],
            ["/rules/dead.md"],
        )
        self.assertEqual(
            [proposal.path for proposal in report.retirement_proposals],
            ["/rules/dead.md", "/rules/faded-covered.md"],
        )
        self.assertEqual(
            report.as_dict()["retirement_proposal_paths"],
            ["/rules/dead.md", "/rules/faded-covered.md"],
        )
        by_path = {rule.path: rule for rule in report.rules}
        self.assertFalse(by_path["/rules/dormant-covered.md"].deletion_candidate)
        self.assertFalse(by_path["/rules/active.md"].unread)
        self.assertTrue(by_path["/rules/young.md"].unread)
        self.assertFalse(by_path["/rules/young.md"].deletion_candidate)
        faded = next(
            proposal
            for proposal in report.retirement_proposals
            if proposal.path == "/rules/faded-covered.md"
        )
        self.assertEqual(faded.last_occurrence, "2026-06-15T00:00:00+00:00")
        self.assertAlmostEqual(faded.occurrence_age_days, 105.0, places=6)
        self.assertEqual(faded.covered_clusters, 1)
        self.assertEqual(faded.removal_owner, "human edit in the owning layer")
        dead = next(
            proposal
            for proposal in report.retirement_proposals
            if proposal.path == "/rules/dead.md"
        )
        self.assertIsNone(dead.last_occurrence)
        self.assertIsNone(dead.occurrence_age_days)
        payload = report.as_dict()
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_an_empty_corpus_reports_itself_as_empty(self):
        report = twill_rules.build_rules_report(
            self.connection,
            unread_days=90,
            now=RULES_NOW,
        )

        self.assertEqual(report.rules, ())
        self.assertEqual(report.deletion_candidates, ())
        self.assertEqual(report.retirement_proposals, ())
        self.assertEqual(report.warnings, ())
        text = twill_rules.render_text(report)
        self.assertIn("no live rule documents", text)


class DigestWasteRankRegressionTests(Phase3StateCase):
    """The digest's waste-ranked output and its stability guarantees."""

    WEEK = (2026, 38)

    def seed_ranked_programs(self):
        # Every program qualifies in W38 with two sessions; none ran in W37,
        # so all findings share the "new" verdict and the waste order alone
        # decides position.
        at = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
        for index in range(2):
            seed_failure(self.connection, f"costly-s{index}", at, program="costly")
            seed_failure(self.connection, f"cheap-s{index}", at, program="cheap")
            seed_failure(self.connection, f"unknown-s{index}", at, program="unknown")
            seed_failure(self.connection, f"blind-s{index}", at, program="blind")
            seed_failure(self.connection, f"blind2-s{index}", at, program="blind2")
        for index in range(2):
            seed_usage(
                self.connection,
                f"costly-s{index}",
                input_tokens=100,
                output_tokens=200,
                cache_read_tokens=300,
                cost_usd=1.0,
            )
            seed_usage(
                self.connection,
                f"cheap-s{index}",
                input_tokens=10,
                output_tokens=20,
                cache_read_tokens=30,
                cost_usd=0.25,
            )
            # Tokens known, cost unknown: dollars stay unavailable, tokens
            # still rank the finding ahead of a fully blind one.
            seed_usage(
                self.connection,
                f"unknown-s{index}",
                input_tokens=100,
                output_tokens=200,
                cache_read_tokens=300,
                cost_usd=None,
            )
        self.connection.commit()

    def build(self):
        return twill_digest.build_digest(
            self.root / "state",
            self.WEEK,
            registry=(twill_detectors.MISSING_BINARY,),
        )

    def test_findings_order_by_dollars_then_tokens_then_identity(self):
        self.seed_ranked_programs()

        report = self.build()

        self.assertEqual(
            [finding.key for finding in report.findings],
            [
                "command-not-found:costly",
                "command-not-found:cheap",
                "command-not-found:unknown",
                "command-not-found:blind",
                "command-not-found:blind2",
            ],
        )
        self.assertTrue(
            all(finding.verdict == "new" for finding in report.findings)
        )
        data = twill_digest.render_data(report)
        estimates = [
            (
                finding["estimated_waste_usd"],
                finding["estimated_tokens"],
            )
            for finding in data["findings"]
        ]
        self.assertEqual(estimates[0], (2.0, 1200.0))
        self.assertEqual(estimates[1], (0.5, 120.0))
        self.assertEqual(estimates[2], (None, 1200.0))
        self.assertEqual(estimates[3], (None, None))
        self.assertEqual(estimates[4], (None, None))
        for finding in data["findings"]:
            self.assertEqual(
                finding["waste_attribution_method"],
                "equal_split_across_distinct_cluster_hits",
            )
        text = twill_digest.render_text(report)
        self.assertIn("estimated waste: 2.000000 USD; estimated tokens: 1,200.00", text)
        self.assertIn("estimated waste: unavailable USD; estimated tokens: 1,200.00", text)

    def test_the_same_state_renders_byte_identical_text(self):
        self.seed_ranked_programs()

        first = self.build()
        second = self.build()

        self.assertEqual(
            twill_digest.render_text(first),
            twill_digest.render_text(second),
        )
        self.assertEqual(
            [finding.key for finding in first.findings],
            [finding.key for finding in second.findings],
        )
        self.assertEqual(first.command, second.command)
        self.assertEqual(
            twill_digest.render_data(first)["findings"],
            twill_digest.render_data(second)["findings"],
        )

    def test_an_empty_week_is_clean_and_names_the_detector_that_ran(self):
        # Two settled failures exist, but only in W36: the W38 report has no
        # current or previous observations and must say so rather than
        # looking like a skipped run.
        at = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
        seed_failure(self.connection, "ghost-s0", at, program="ghost")
        seed_failure(self.connection, "ghost-s1", at, program="ghost")
        self.connection.commit()

        report = self.build()

        self.assertTrue(report.database)
        self.assertTrue(report.clean)
        self.assertEqual(report.findings, ())
        self.assertEqual(report.observations, 2)
        self.assertEqual(report.observations_in_week, 0)
        self.assertEqual(report.observations_in_previous_week, 0)
        summary = report.detectors[0]
        self.assertEqual(
            (summary.full_id, summary.current_status, summary.previous_status),
            ("D-01@1", "ok", "ok"),
        )
        self.assertEqual(
            (summary.current_clusters, summary.previous_clusters), (0, 0)
        )
        text = twill_digest.render_text(report)
        self.assertIn("clean week: no findings; detectors ran: D-01@1", text)
        self.assertIn("observations: 2 total, 0 current, 0 previous", text)


if __name__ == "__main__":
    unittest.main()
