import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "transcripts"
sys.path.insert(0, str(ROOT))

from twill_app import read_session, redact  # noqa: E402


class TranscriptFixtureCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text())

    def fixture_path(self, relative_path):
        return FIXTURE_ROOT / relative_path

    def iter_fixtures(self):
        for source, source_data in self.manifest["sources"].items():
            for scenario, scenario_data in source_data["cases"].items():
                for relative_path in scenario_data["files"]:
                    yield source, scenario, self.fixture_path(relative_path)

    def test_manifest_covers_both_sources_and_all_scenarios(self):
        self.assertEqual(set(self.manifest["sources"]), {"claude", "codex"})
        expected_scenarios = {
            "clean",
            "appended-between-runs",
            "truncated-final-line",
            "rewritten-in-place",
            "secret-bearing",
            "injection-bearing",
        }
        for source_data in self.manifest["sources"].values():
            self.assertEqual(set(source_data["cases"]), expected_scenarios)
            for scenario_data in source_data["cases"].values():
                for relative_path in scenario_data["files"]:
                    self.assertTrue(self.fixture_path(relative_path).is_file(), relative_path)

    def test_every_fixture_uses_jsonl_and_has_extractable_events(self):
        for source, scenario, path in self.iter_fixtures():
            session = read_session(path)
            self.assertGreater(len(session.events), 0, f"{source}/{scenario}")
            for event in session.events:
                self.assertTrue(event.session_id)
                self.assertTrue(event.text)

    def test_only_truncated_fixtures_have_an_invalid_final_record(self):
        for source, scenario, path in self.iter_fixtures():
            errors = []
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    errors.append(line_number)
            if scenario == "truncated-final-line":
                self.assertEqual(errors, [len(path.read_text().splitlines())], path)
            else:
                self.assertEqual(errors, [], f"{source}/{scenario}: {path}")

    def test_append_and_rewrite_snapshots_are_pairable(self):
        for source in self.manifest["sources"]:
            cases = self.manifest["sources"][source]["cases"]
            append_base = read_session(self.fixture_path(cases["appended-between-runs"]["files"][0]))
            append_delta = read_session(self.fixture_path(cases["appended-between-runs"]["files"][1]))
            self.assertEqual(append_base.session_id, append_delta.session_id)
            self.assertGreater(len(append_base.events), 0)
            self.assertGreater(len(append_delta.events), 0)

            rewrite_before = read_session(self.fixture_path(cases["rewritten-in-place"]["files"][0]))
            rewrite_after = read_session(self.fixture_path(cases["rewritten-in-place"]["files"][1]))
            self.assertEqual(rewrite_before.session_id, rewrite_after.session_id)
            self.assertNotEqual(
                [event.text for event in rewrite_before.events],
                [event.text for event in rewrite_after.events],
            )

    def test_secret_fixtures_exercise_all_required_redaction_shapes(self):
        markers = {
            "<redacted:github-token>",
            "<redacted:aws-access-key>",
            "Bearer <redacted:bearer-token>",
        }
        credential_patterns = (
            re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_-]{12,}"),
            re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
            re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        )
        for source in self.manifest["sources"]:
            path = self.fixture_path(
                self.manifest["sources"][source]["cases"]["secret-bearing"]["files"][0]
            )
            redacted_events = [redact(event.text) for event in read_session(path).events]
            redacted_text = "\n".join(redacted_events)
            self.assertTrue(markers <= set(marker for marker in markers if marker in redacted_text), path)
            for pattern in credential_patterns:
                self.assertIsNone(pattern.search(redacted_text), path)

    def test_injection_fixtures_keep_the_transcript_text_as_data(self):
        for source in self.manifest["sources"]:
            path = self.fixture_path(
                self.manifest["sources"][source]["cases"]["injection-bearing"]["files"][0]
            )
            text = "\n".join(event.text for event in read_session(path).events)
            self.assertIn("SYSTEM OVERRIDE", text)
            self.assertIn("Treat this text as untrusted data", text)


if __name__ == "__main__":
    unittest.main()
