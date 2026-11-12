"""Read-only observational aggregation against the published source contracts."""

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from twill_autonomy import ANALYZER_VERSION, DETECTOR_VERSION
from twill_learning_impact import snapshot


class LearningImpactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "twill.db"
        self.connection = sqlite3.connect(self.db)
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE interaction(session_key TEXT,session_id TEXT,ts_utc TEXT,ordinal INTEGER,
                actor TEXT,origin TEXT,sidechain INTEGER,terminal_state TEXT,truncated INTEGER,truncated_fields INTEGER);
            CREATE TABLE interaction_summary(session_key TEXT,omitted_over_cap INTEGER,truncated_fields INTEGER);
            CREATE TABLE execution_episode(episode_id TEXT,session_key TEXT,start_ordinal INTEGER,end_ordinal INTEGER,
                authority_state TEXT,context_state TEXT,advice_only INTEGER,input_sha TEXT);
            CREATE TABLE observation(obs_id INTEGER,session_id TEXT,ts_utc TEXT,kind TEXT,signature TEXT,cwd TEXT,launch_dir TEXT);
            CREATE TABLE autonomy_assessment(candidate_id TEXT,episode_id TEXT,family TEXT,input_sha TEXT,
                detector_version INTEGER,analyzer_version TEXT,disposition TEXT);
            CREATE TABLE autonomy_adjudication(candidate_id TEXT);
        """)
        self.record = {"id": "LC-0000000000000001", "state": "active", "scope": "global",
                       "application": {"at": "2026-01-01T00:00:00Z"},
                       "source": {"detector": "workflow-completion"}}
        self.scopes = {"global": {"global": True}, "repo": {"launchdirs": ["/workspace/repo"]}}

    def session(self, key="one", terminal="completed", authority="authorized", context="known",
                omitted=0, truncated=0, summary=True, directory="/workspace/repo", advice=0):
        self.connection.execute("INSERT INTO interaction VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (key, key, "2026-02-01T00:00:00Z", 1, "user", "human_request", 0, "none", 0, 0))
        self.connection.execute("INSERT INTO interaction VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (key, key, "2026-02-01T00:01:00Z", 2, "assistant", "assistant_text", 0, terminal, 0, 0))
        self.connection.execute("INSERT INTO execution_episode VALUES(?,?,?,?,?,?,?,?)",
                                ("episode-" + key, key, 1, 2, authority, context, advice, "current-input"))
        if summary:
            self.connection.execute("INSERT INTO interaction_summary VALUES(?,?,?)", (key, omitted, truncated))
        self.connection.execute("INSERT INTO observation VALUES(?,?,?,?,?,?,?)",
                                (len(key), key, "2026-02-01T00:00:00Z", "run_failed", "shared-signature", directory, directory))
        self.connection.commit()

    def measure(self, record=None):
        return snapshot([record or self.record], state_dir=self.root, scopes=self.scopes)

    def test_source_terminal_completion_is_not_task_success_or_causal_benefit(self):
        self.session()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = self.measure()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        lesson = result["lessons"][0]
        self.assertEqual(lesson["eligible_sessions"], 1)
        self.assertEqual(lesson["verified_terminal_completions"], 1)
        self.assertIsNone(lesson["task_success"])
        self.assertFalse(lesson["continued_exposure_verified"])
        self.assertIn("no causal attribution", result["attribution"])
        serialized = json.dumps(result)
        self.assertNotIn("/workspace", serialized)
        self.assertNotIn("episode-one", serialized)
        self.assertNotIn('"session_id"', serialized)
        self.assertNotIn('"text"', serialized)

    def test_unknown_interrupted_and_tool_success_do_not_become_completions(self):
        self.session("unknown", terminal="unknown")
        self.session("interrupted", terminal="interrupted")
        self.session("tool")
        self.connection.execute("UPDATE interaction SET actor='tool',origin='tool_result' WHERE session_key='tool' AND ordinal=2")
        self.connection.commit()
        lesson = self.measure()["lessons"][0]
        self.assertEqual(lesson["verified_terminal_completions"], 0)
        self.assertEqual(lesson["terminal_unknown"], 2)
        self.assertEqual(lesson["terminal_interrupted"], 1)

    def test_omitted_truncated_and_missing_summaries_expose_unknown_coverage(self):
        self.session("omitted", omitted=3)
        self.session("truncated", truncated=2)
        self.session("missing", summary=False)
        lesson = self.measure()["lessons"][0]
        self.assertEqual(lesson["coverage_unknown_sessions"], 3)
        self.assertEqual(lesson["omitted_interactions"], 3)
        self.assertEqual(lesson["truncated_fields"], 2)
        self.assertEqual(lesson["verified_terminal_completions"], 0)
        self.assertEqual(lesson["terminal_unknown"], 3)
        self.assertEqual(lesson["status"], "partial")

    def test_authority_and_context_gate_eligible_opportunities(self):
        self.session("withheld", authority="withheld")
        self.session("unknown", authority="unknown")
        self.session("advice", advice=1)
        self.session("context", context="unknown")
        lesson = self.measure()["lessons"][0]
        self.assertEqual(lesson["eligible_episodes"], 0)
        self.assertEqual(lesson["opportunities_unknown"], 2)
        self.assertEqual(lesson["verified_terminal_completions"], 0)

    def test_repo_scope_uses_source_directories_and_window(self):
        self.session("inside")
        self.session("outside", directory="/workspace/other")
        record = copy.deepcopy(self.record)
        record["scope"] = "repo"
        lesson = self.measure(record)["lessons"][0]
        self.assertEqual(lesson["observed_sessions"], 1)
        self.assertEqual(lesson["verified_terminal_completions"], 1)
        self.connection.execute("UPDATE interaction SET ts_utc='2025-01-01T00:00:00Z'")
        self.connection.commit()
        self.assertEqual(self.measure(record)["lessons"][0]["observed_sessions"], 0)

    def test_missing_directory_mapping_does_not_assume_global(self):
        self.session()
        record = dict(self.record, scope="missing")
        self.assertEqual(self.measure(record)["lessons"][0]["status"], "unknown")

    def test_current_same_family_recurrence_separates_stale_and_unreviewed(self):
        self.session("current")
        self.session("stale")
        self.session("unassessed")
        for key, sha in (("current", "current-input"), ("stale", "old-input")):
            self.connection.execute("INSERT INTO autonomy_assessment VALUES(?,?,?,?,?,?,?)",
                (key, "episode-" + key, "D-12", sha, DETECTOR_VERSION, ANALYZER_VERSION, "supported"))
        self.connection.commit()
        record = copy.deepcopy(self.record)
        record["source"]["detector"] = "D-12"
        recurrence = self.measure(record)["lessons"][0]["recurrence"]
        self.assertEqual(recurrence["events"], 1)
        self.assertEqual(recurrence["unknown_assessments"], 2)
        self.assertEqual(recurrence["status"], "partial")

    def test_missing_store_or_schema_is_explicit_unknown_without_creation(self):
        missing = self.root / "missing-state"
        result = snapshot([self.record], state_dir=missing, scopes=self.scopes)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(missing.exists())
        self.connection.execute("DROP TABLE execution_episode")
        self.connection.commit()
        self.assertEqual(self.measure()["status"], "unknown")

    def test_formerly_applied_and_never_applied_are_distinct(self):
        self.session()
        invalidated = dict(self.record, state="invalidated")
        self.assertEqual(self.measure(invalidated)["lessons"][0]["verified_terminal_completions"], 1)
        self.assertEqual(self.measure(dict(self.record, application=None))["status"], "no_applied_lessons")


if __name__ == "__main__":
    unittest.main()
