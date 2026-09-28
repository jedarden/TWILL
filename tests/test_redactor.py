import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_app import Store, SessionData, TranscriptEvent, redact  # noqa: E402
from twill_redactor import CONTENT_FENCE_MARKER, MAX_EXCERPT_LENGTH, Redactor  # noqa: E402


class RedactorTests(unittest.TestCase):
    def test_redacts_the_documented_credential_inventory(self):
        cases = (
            ("GitHub classic ghp token", "gh" + "p_0123456789ab", "<redacted:github-token>"),
            ("GitHub classic gho token", "gh" + "o_0123456789ab", "<redacted:github-token>"),
            ("GitHub classic ghu token", "gh" + "u_0123456789ab", "<redacted:github-token>"),
            ("GitHub classic ghs token", "gh" + "s_0123456789ab", "<redacted:github-token>"),
            ("GitHub classic ghr token", "gh" + "r_0123456789ab", "<redacted:github-token>"),
            (
                "GitHub fine-grained token",
                "github" + "_pat_0123456789ab",
                "<redacted:github-token>",
            ),
            ("AWS AKIA access key", "AKIA" + "1234567890ABCDEF", "<redacted:aws-access-key>"),
            ("AWS ASIA access key", "ASIA" + "1234567890ABCDEF", "<redacted:aws-access-key>"),
            ("Bearer token", "Bea" + "rer abcdefgh", "Bearer <redacted:bearer-token>"),
            ("OpenAI-style token", "s" + "k-0123456789ab", "<redacted:api-key>"),
            ("Slack xoxb token", "xox" + "b-0123456789", "<redacted:slack-token>"),
            ("Slack xoxa token", "xox" + "a-0123456789", "<redacted:slack-token>"),
            ("Slack xoxp token", "xox" + "p-0123456789", "<redacted:slack-token>"),
            ("Slack xoxr token", "xox" + "r-0123456789", "<redacted:slack-token>"),
            ("Slack xoxs token", "xox" + "s-0123456789", "<redacted:slack-token>"),
            (
                "api_key assignment",
                "api" + "_key=assignment-value",
                "api_key=<redacted:secret>",
            ),
            (
                "access_token assignment",
                "access" + "_token=assignment-value",
                "access_token=<redacted:secret>",
            ),
            (
                "auth_token assignment",
                "auth" + "_token=assignment-value",
                "auth_token=<redacted:secret>",
            ),
            (
                "password assignment",
                "pass" + "word=assignment-value",
                "password=<redacted:secret>",
            ),
            ("secret assignment", "sec" + "ret=assignment-value", "secret=<redacted:secret>"),
            ("token assignment", "tok" + "en=assignment-value", "token=<redacted:secret>"),
            (
                "PEM private-key block",
                "before\n-----BEGIN RSA PRIVATE KEY-----\nbase64-secret\n"
                "-----END RSA PRIVATE KEY-----\nafter",
                "before\n<redacted:private-key>\nafter",
            ),
        )

        redactor = Redactor()
        for name, value, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(redactor.redact_text(f"prefix {value} suffix"), f"prefix {expected} suffix")

    def test_documented_case_insensitive_patterns(self):
        cases = (
            ("GitHub classic token", "GH" + "P_0123456789AB", "<redacted:github-token>"),
            (
                "GitHub fine-grained token",
                "GITHUB" + "_PAT_0123456789AB",
                "<redacted:github-token>",
            ),
            ("Bearer token", "bEa" + "ReR AbCdEfGh", "Bearer <redacted:bearer-token>"),
            ("OpenAI-style token", "S" + "K-0123456789AB", "<redacted:api-key>"),
            (
                "assignment name",
                "PaSs" + "WoRd=assignment-value",
                "PaSsWoRd=<redacted:secret>",
            ),
        )

        redactor = Redactor()
        for name, value, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(redactor.redact_text(f"prefix {value} suffix"), f"prefix {expected} suffix")

    def test_content_fences_match_case_insensitively(self):
        redactor = Redactor(("Restricted Vendor",))

        self.assertEqual(
            redactor.redact_text("before rEsTrIcTeD vEnDoR after"),
            "before <redacted:content-fence> after",
        )

    def test_content_fences_are_ordered_longest_first(self):
        redactor = Redactor(("Restricted", "Restricted Vendor"))

        self.assertEqual(
            redactor.redact_text("before Restricted Vendor after"),
            "before <redacted:content-fence> after",
        )

    def test_short_fence_cannot_expose_suffix_of_a_longer_fence(self):
        # Without longest-first alternatives, the short match would leave
        # `` Vendor`` in the output and expose part of the fenced name.
        redactor = Redactor(("Vendor", "Restricted Vendor"))

        self.assertEqual(
            redactor.redact_text("before Restricted Vendor after"),
            "before <redacted:content-fence> after",
        )

    def test_redacts_before_truncating_excerpts(self):
        long_token = "gh" + "p_" + "0123456789abcdef" * 2
        long_fence = "Restricted Vendor Entity With More Words Than Usual"
        cases = (
            (
                "credential",
                Redactor(),
                long_token,
                "<redacted:github-token>",
                202,
                " TAIL-REMAINS",
            ),
            (
                "content fence",
                Redactor((long_fence,)),
                long_fence,
                CONTENT_FENCE_MARKER,
                195,
                " TAIL-REMAINS",
            ),
        )

        for name, redactor, secret, replacement, prefix_length, suffix in cases:
            with self.subTest(name=name):
                value = "x" * prefix_length + " " + secret + suffix
                expected = "x" * prefix_length + " " + replacement + suffix
                self.assertGreater(len(value), MAX_EXCERPT_LENGTH)
                self.assertLessEqual(len(expected), MAX_EXCERPT_LENGTH)
                self.assertEqual(redactor.redact_excerpt(value), expected)

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
            path=Path(tempfile.gettempdir()) / f"source-{token}" / "session.jsonl",
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
                    (
                        "observation",
                        ("session_id", "kind", "excerpt", "launch_dir", "cwd"),
                    ),
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
