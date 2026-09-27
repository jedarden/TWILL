"""Regression oracle for the Phase 2 probe counts.

The 2026-09-19 probe counts are real observations, but the transcripts that
produced them are not public-repository fixtures.  This test therefore builds
a compact equivalent Claude tool-use/result replay in a temporary directory.
The replay still crosses the real parser boundary: a parser that silently
stops emitting failed Bash runs produces no detector clusters and fails here.
"""

import hashlib
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_detectors  # noqa: E402
import twill_digest  # noqa: E402
import twill_schema  # noqa: E402
from twill_reader import ClaudeCodeLineParser, KIND_RUN  # noqa: E402


PROBE_COUNTS = {
    "sqlite3": 1096,
    "bf": 697,
    "go": 632,
}
PROBE_TIMESTAMP = "2026-09-19T12:00:00Z"
DETECT_NOW = "2026-09-27T00:00:00+00:00"
PROBE_WEEK = (2026, 38)
UNGATED_FIXTURE = ROOT / "tests" / "fixtures" / "detectors" / "phase2_ungated.json"


def _claude_record(session_id, timestamp, content, *, record_type):
    return json.dumps(
        {
            "type": record_type,
            "sessionId": session_id,
            "timestamp": timestamp,
            "message": {"role": "assistant" if record_type == "assistant" else "user", "content": content},
        }
    )


def _probe_lines(program, index):
    session_id = f"probe-{program}-{index}"
    tool_id = f"toolu-{program}-{index}"
    command = program
    yield _claude_record(
        session_id,
        PROBE_TIMESTAMP,
        [
            {
                "type": "tool_use",
                "id": tool_id,
                "name": "Bash",
                "input": {"command": command},
            }
        ],
        record_type="assistant",
    )
    yield _claude_record(
        session_id,
        PROBE_TIMESTAMP,
        [
            {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": f"Exit code 127\n/bin/sh: 1: {program}: command not found",
                "is_error": True,
            }
        ],
        record_type="user",
    )


class Phase2RegressionOracleTests(unittest.TestCase):
    def test_thirty_day_probe_counts_survive_parser_and_digest(self):
        parser = ClaudeCodeLineParser()
        events = []
        source_line = 0
        for program, count in PROBE_COUNTS.items():
            for index in range(count):
                for line in _probe_lines(program, index):
                    source_line += 1
                    events.extend(parser.parse_line(line, source_line))
        events.extend(parser.finish())

        self.assertEqual(len(events), sum(PROBE_COUNTS.values()))
        self.assertTrue(all(event.kind == KIND_RUN for event in events))
        self.assertEqual(
            Counter(event.command for event in events),
            Counter({program: count for program, count in PROBE_COUNTS.items()}),
        )

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            connection = twill_schema.connect(state)
            try:
                connection.executemany(
                    "INSERT INTO observation(session_id, ts_utc, ts_local, kind, "
                    "program, command, signature, sig_hash, excerpt) "
                    "VALUES (?, ?, ?, 'run_failed', ?, ?, ?, ?, ?)",
                    [
                        (
                            event.session_id,
                            event.timestamp,
                            event.timestamp,
                            event.command.split(maxsplit=1)[0],
                            event.command,
                            event.error_excerpt,
                            hashlib.sha256(event.error_excerpt.encode()).hexdigest()[:12],
                            event.error_excerpt,
                        )
                        for event in events
                    ],
                )
                connection.commit()

                report = twill_detectors.run_detectors(
                    connection,
                    window_days=30,
                    registry=(twill_detectors.MISSING_BINARY,),
                    now=DETECT_NOW,
                )
                self.assertEqual(report.exit_code, 0)
                self.assertEqual(report.outcomes[0].clusters, len(PROBE_COUNTS))
                clusters = {
                    key: sessions
                    for key, sessions in connection.execute(
                        "SELECT key, sessions FROM cluster WHERE detector_id = 'D-01'"
                    )
                }
            finally:
                connection.close()

            for program, expected in PROBE_COUNTS.items():
                key = f"command-not-found:{program}"
                self.assertIn(key, clusters)
                actual = clusters[key]
                self.assertLessEqual(
                    abs(actual - expected), expected * 0.05,
                    f"30-day D-01 count drifted for {program}: {actual} vs {expected}",
                )

            digest = twill_digest.build_digest(
                state,
                PROBE_WEEK,
                registry=(twill_detectors.MISSING_BINARY,),
            )
            findings = {finding.key: finding for finding in digest.findings}
            self.assertEqual(set(findings), set(clusters))
            for program, expected in PROBE_COUNTS.items():
                finding = findings[f"command-not-found:{program}"]
                self.assertEqual(finding.verdict, "new")
                self.assertIsNotNone(finding.current)
                actual = finding.current[0]
                self.assertLessEqual(
                    abs(actual - expected), expected * 0.05,
                    f"digest count drifted for {program}: {actual} vs {expected}",
                )

    def test_ungated_detector_fixture_has_known_counts(self):
        fixture = json.loads(UNGATED_FIXTURE.read_text())
        expected = fixture["expected"]
        registry = (
            twill_detectors.RETRY_LOOP,
            twill_detectors.REJECTED_TOOL_CALL,
            twill_detectors.INTERRUPT_CORRECTION,
            twill_detectors.REDISCOVERY,
            twill_detectors.STALE_RULE,
            twill_detectors.UNREAD_RULE_DOC,
        )

        with tempfile.TemporaryDirectory() as directory:
            connection = twill_schema.connect(Path(directory) / "state")
            try:
                connection.executemany(
                    "INSERT INTO observation(session_id, ts_utc, ts_local, kind, "
                    "program, command, signature, sig_hash, tool, path, excerpt, host) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            row["session_id"],
                            row["ts_utc"],
                            row["ts_utc"],
                            row["kind"],
                            row.get("program"),
                            row.get("command"),
                            row.get("signature"),
                            row.get("sig_hash"),
                            row.get("tool"),
                            row.get("path"),
                            row.get("excerpt"),
                            row.get("host", "codinghome"),
                        )
                        for row in fixture["observations"]
                    ],
                )
                for row in fixture["rule_docs"]:
                    connection.execute(
                        "INSERT INTO rule_doc(path, layer, sha, indexed_at, "
                        "last_read_by_agent, stale) VALUES (?, 'memory', ?, ?, ?, 0)",
                        (
                            row["path"],
                            row["sha"],
                            row["indexed_at"],
                            row.get("last_read_by_agent"),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO rule_fts(text, path) VALUES (?, ?)",
                        (row["text"], row["path"]),
                    )
                connection.commit()

                report = twill_detectors.run_detectors(
                    connection,
                    window_days=fixture["window_days"],
                    registry=registry,
                    now=fixture["now"],
                )
                self.assertEqual(report.exit_code, 0)
                self.assertEqual(
                    [outcome.detector_id for outcome in report.outcomes],
                    list(expected),
                )

                for detector_id, detector_expected in expected.items():
                    with self.subTest(detector_id=detector_id):
                        outcome = next(
                            outcome
                            for outcome in report.outcomes
                            if outcome.detector_id == detector_id
                        )
                        self.assertEqual(outcome.status, "ok")
                        self.assertEqual(outcome.clusters, detector_expected["clusters"])
                        rows = connection.execute(
                            "SELECT key, sessions, events FROM cluster "
                            "WHERE detector_id = ? ORDER BY key",
                            (detector_id,),
                        ).fetchall()
                        actual = {
                            key: {"sessions": sessions, "events": events}
                            for key, sessions, events in rows
                        }
                        self.assertEqual(actual, detector_expected["rows"])
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
