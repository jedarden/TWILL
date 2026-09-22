import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_app import Store, SessionData, TranscriptEvent, redact  # noqa: E402
from twill_redactor import CONTENT_FENCE_MARKER, MAX_EXCERPT_LENGTH, Redactor  # noqa: E402


class RedactorTests(unittest.TestCase):
    def test_redacts_credentials_and_fences_before_the_excerpt_cut(self):
        token = "ghp_1234567890abcdefghijklmnop"
        suffix = " TAIL-REMAINS"
        value = "x" * 202 + " " + token + suffix

        redacted = redact(value, ("fenced entity",))

        self.assertLessEqual(len(redacted), MAX_EXCERPT_LENGTH)
        self.assertNotIn(token, redacted)
        self.assertIn("<redacted:github-token>", redacted)
        self.assertTrue(redacted.endswith(suffix))
        self.assertEqual(
            Redactor(("Fenced Entity",)).redact_text("before fenced entity after"),
            f"before {CONTENT_FENCE_MARKER} after",
        )

    def test_store_redacts_every_transcript_derived_text_parameter(self):
        token = "ghp_1234567890abcdefghijklmnop"
        fenced = "Restricted Vendor"
        session = SessionData(
            path=Path(tempfile.gettempdir()) / f"source-{token}.jsonl",
            session_id=f"session-{token}",
            source_kind=f"source-{token}",
            events=(
                TranscriptEvent(
                    session_id=f"session-{token}",
                    timestamp="2026-09-22T12:00:00Z",
                    kind=f"kind-{token}",
                    text=f"{fenced} leaked {token}",
                    source_line=1,
                    event_index=0,
                    cwd=f"/tmp/{token}",
                ),
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state", content_fences=(fenced,))
            try:
                store.ingest(session)
                values = []
                for table, columns in (
                    ("session", ("session_id", "source_path", "source_kind")),
                    ("transcript_event", ("kind", "text", "cwd")),
                    ("observation", ("session_id", "kind", "excerpt", "cwd")),
                ):
                    selected = ", ".join(columns)
                    values.extend(
                        value
                        for row in store.connection.execute(f"SELECT {selected} FROM {table}")
                        for value in row
                        if value is not None
                    )
            finally:
                store.close()

        persisted = "\n".join(values)
        self.assertNotIn(token, persisted)
        self.assertNotIn(fenced, persisted)
        self.assertIn("<redacted:github-token>", persisted)
        self.assertIn(CONTENT_FENCE_MARKER, persisted)


if __name__ == "__main__":
    unittest.main()
