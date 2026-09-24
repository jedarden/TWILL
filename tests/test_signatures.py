"""Tests for normalized error signatures and their persistence."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_app import (  # noqa: E402
    MAX_EXCERPT_LENGTH,
    SIGNATURE_HASH_LENGTH,
    SIGNATURE_INPUT_LIMIT,
    Store,
    SessionData,
    TranscriptEvent,
    hash_error_signature,
    h12,
    signature,
)
import twill_schema  # noqa: E402


class SignatureTests(unittest.TestCase):
    def test_hashes_the_normalized_value(self):
        text = "FAILED /home/coding/project/build-123 at 2026-09-18"
        normalized = "FAILED <path> at <n>-<n>-<n>"

        self.assertEqual(signature(text), normalized)
        self.assertEqual(
            h12(normalized),
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()[
                :SIGNATURE_HASH_LENGTH
            ],
        )
        self.assertEqual(hash_error_signature(text), h12(normalized))
        self.assertRegex(hash_error_signature(text), r"^[0-9a-f]{12}$")

    def test_masks_paths_hex_values_uuids_numbers_and_whitespace(self):
        first = (
            "FAILED /home/coding/WARP/run-123: "
            "uuid 01234567-89ab-cdef-0123-456789abcdef "
            "commit 0123456789abcdef0123456789abcdef01234567\n"
            "hex fedcba987654 attempt 42"
        )
        second = (
            "FAILED /tmp/other/run-987: "
            "uuid fedcba98-7654-3210-fedc-ba9876543210 "
            "commit fedcba9876543210fedcba9876543210fedcba98\t"
            "hex 0123456789ab attempt 7"
        )

        self.assertEqual(signature(first), signature(second))
        self.assertIn("<path>", signature(first))
        self.assertIn("<uuid>", signature(first))
        self.assertIn("<sha>", signature(first))
        self.assertIn("<hex>", signature(first))
        self.assertIn("<n>", signature(first))

    def test_prefixed_and_long_hex_values_fold(self):
        first = "failure 0xdeadbeef digest 0123456789abcdef0123456789abcdef"
        second = "failure 0x1234567 digest fedcba9876543210fedcba9876543210"

        self.assertEqual(signature(first), signature(second))
        self.assertEqual(signature(first), "failure <hex> digest <hex>")

    def test_legacy_transcript_rows_are_backfilled_on_open(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            connection = twill_schema.connect(state)
            connection.executescript(
                """
                CREATE TABLE session(
                    session_key TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    source_path TEXT NOT NULL UNIQUE, source_kind TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
                CREATE TABLE transcript_event(
                    event_id INTEGER PRIMARY KEY, session_key TEXT NOT NULL,
                    source_line INTEGER NOT NULL, event_index INTEGER NOT NULL,
                    ts_utc TEXT NOT NULL, ts_local TEXT NOT NULL,
                    kind TEXT NOT NULL, text TEXT NOT NULL, cwd TEXT,
                    UNIQUE(session_key, source_line, event_index)
                );
                """
            )
            connection.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
                ("key", "session", "source", "claude", "now"),
            )
            connection.execute(
                "INSERT INTO transcript_event(session_key, source_line, event_index, "
                "ts_utc, ts_local, kind, text, cwd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("key", 1, 0, "time", "time", "run_failed", "failure /tmp/one", "/tmp"),
            )
            connection.execute(
                "INSERT INTO observation(session_id, ts_utc, ts_local, kind, excerpt, cwd) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "session",
                    "time",
                    "time",
                    "session_activity",
                    "failure /tmp/one",
                    "/tmp",
                ),
            )
            connection.commit()
            connection.close()

            store = Store(state)
            try:
                event = store.connection.execute(
                    "SELECT signature, sig_hash FROM transcript_event"
                ).fetchone()
                observation = store.connection.execute(
                    "SELECT signature, sig_hash FROM observation"
                ).fetchone()
            finally:
                store.close()

        self.assertEqual(event, ("failure <path>", h12("failure <path>")))
        self.assertEqual(observation, event)

    def test_relative_paths_fold_without_masking_prose_slashes(self):
        self.assertEqual(
            signature("failed: src/lib.rs:42"),
            signature("failed: tests/main.rs:99"),
        )
        self.assertEqual(
            signature("expected and/or received"), signature("expected and/or received")
        )
        self.assertIn("and/or", signature("expected and/or received 5 errors"))

    def test_a_different_context_prefix_remains_a_different_signature(self):
        self.assertNotEqual(
            signature("runner: failure at /tmp/one"),
            signature("worker: failure at /tmp/two"),
        )

    def test_input_is_capped_before_normalization(self):
        prefix = "FAILED " + ("x" * (SIGNATURE_INPUT_LIMIT - len("FAILED ")))
        self.assertEqual(
            signature(prefix + " first-suffix"),
            signature(prefix + " second-suffix"),
        )

    def test_store_persists_redacted_bounded_excerpt_and_signature_hash(self):
        token = "gh" + "p_" + "1234567890abcdefghijklmnop"
        text = (
            "failure at /tmp/run-123 with "
            f"{token} and 0123456789abcdef0123456789abcdef01234567 "
            + "x" * 300
        )
        session = SessionData(
            path=Path(tempfile.gettempdir()) / "signature-session.jsonl",
            session_id="signature-session",
            source_kind="claude",
            events=(
                TranscriptEvent(
                    session_id="signature-session",
                    timestamp="2026-09-22T12:00:00Z",
                    kind="run_failed",
                    text=text,
                    source_line=1,
                    event_index=0,
                ),
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state")
            try:
                store.ingest(session)
                row = store.connection.execute(
                    "SELECT signature, sig_hash, excerpt FROM observation"
                ).fetchone()
            finally:
                store.close()

        self.assertEqual(row[0], signature(text))
        self.assertEqual(row[1], hash_error_signature(text))
        self.assertNotIn(token, row[0])
        self.assertNotIn(token, row[2])
        self.assertLessEqual(len(row[2]), MAX_EXCERPT_LENGTH)


if __name__ == "__main__":
    unittest.main()
