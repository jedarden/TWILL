import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_reader import CodexRolloutReader  # noqa: E402
from twill_app import Store, read_session  # noqa: E402
from twill_reader import ClaudeCodeLineParser  # noqa: E402


def claude_usage_line(
    message_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    timestamp: str,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "sessionId": "claude-usage",
            "timestamp": timestamp,
            "message": {
                "id": message_id,
                "model": "claude-test",
                "role": "assistant",
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": cache_read_tokens,
                },
            },
        }
    )


def codex_usage_line(
    snapshot: dict[str, int], timestamp: str
) -> str:
    return json.dumps(
        {
            "type": "event_msg",
            "timestamp": timestamp,
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": snapshot},
            },
        }
    )


class UsageExtractionTests(unittest.TestCase):
    def test_claude_duplicate_message_ids_keep_component_maxima(self):
        parser = ClaudeCodeLineParser()
        parser.parse_line(
            claude_usage_line("message-1", 10, 2, 1, "2026-09-24T00:00:00Z"),
            1,
        )
        parser.parse_line(
            claude_usage_line("message-1", 8, 5, 3, "2026-09-24T00:00:01Z"),
            2,
        )
        parser.parse_line(
            claude_usage_line("message-2", 4, 1, 0, "2026-09-24T00:00:02Z"),
            3,
        )

        self.assertEqual(len(parser.usage), 2)
        first = parser.usage[0]
        self.assertEqual(first.message_id, "message-1")
        self.assertEqual(first.input_tokens, 10)
        self.assertEqual(first.output_tokens, 5)
        self.assertEqual(first.cache_read_tokens, 3)

    def test_store_aggregates_claude_messages_and_cumulative_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.jsonl"
            path.write_text(
                "\n".join(
                    [
                        claude_usage_line("message-1", 10, 2, 1, "2026-09-24T00:00:00Z"),
                        claude_usage_line("message-1", 8, 5, 3, "2026-09-24T00:00:01Z"),
                        claude_usage_line("message-2", 4, 1, 0, "2026-09-24T00:00:02Z"),
                        json.dumps(
                            {
                                "type": "cost-state",
                                "sessionId": "claude-usage",
                                "totalCostUSD": 1.25,
                                "totalDuration": 12000,
                            }
                        ),
                    ]
                )
                + "\n"
            )
            store = Store(root / "state")
            try:
                store.ingest_path(path)
                row = store.connection.execute(
                    "SELECT model, input_tokens, output_tokens, cache_read_tokens, "
                    "cost_usd, wall_seconds, messages FROM session_usage"
                ).fetchone()
            finally:
                store.close()

        self.assertEqual(row, ("claude-test", 14, 6, 3, 1.25, 12, 2))

    def test_codex_token_snapshots_are_maxima_not_sums(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        codex_usage_line(
                            {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 1},
                            "2026-09-24T00:00:00Z",
                        ),
                        codex_usage_line(
                            {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 1},
                            "2026-09-24T00:00:01Z",
                        ),
                        codex_usage_line(
                            {"input_tokens": 8, "output_tokens": 4, "cached_input_tokens": 3},
                            "2026-09-24T00:00:02Z",
                        ),
                        codex_usage_line(
                            {"input_tokens": 2, "output_tokens": 1, "cached_input_tokens": 0},
                            "2026-09-24T00:00:03Z",
                        ),
                    ]
                )
                + "\n"
            )
            reader = CodexRolloutReader()
            reader.parse(path)
            row = reader.usage_max[0]
            self.assertEqual(row.input_tokens, 10)
            self.assertEqual(row.output_tokens, 4)
            self.assertEqual(row.cache_read_tokens, 3)

            store = Store(Path(directory) / "state")
            try:
                store.ingest_path(path)
                persisted = store.connection.execute(
                    "SELECT input_tokens, output_tokens, cache_read_tokens, messages "
                    "FROM session_usage"
                ).fetchone()
            finally:
                store.close()

        self.assertEqual(persisted, (10, 4, 3, 4))

    def test_append_recomputes_usage_and_respects_the_committed_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.jsonl"
            first = claude_usage_line(
                "message-1", 10, 2, 1, "2026-09-24T00:00:00Z"
            ) + "\n"
            path.write_text(first)
            store = Store(root / "state")
            try:
                store.ingest_path(path)
                with path.open("a") as handle:
                    handle.write(
                        claude_usage_line(
                            "message-1", 99, 3, 4, "2026-09-24T00:00:01Z"
                        )
                        + "\n"
                    )
                    handle.write(
                        claude_usage_line(
                            "message-2", 7, 1, 0, "2026-09-24T00:00:02Z"
                        )
                        + "\n"
                    )
                    handle.write(
                        claude_usage_line(
                            "message-3", 999, 99, 99, "2026-09-24T00:00:03Z"
                        )
                    )
                store.ingest_path(path)
                row = store.connection.execute(
                    "SELECT input_tokens, output_tokens, cache_read_tokens, messages "
                    "FROM session_usage"
                ).fetchone()
            finally:
                store.close()

        self.assertEqual(row, (106, 4, 4, 2))

    def test_rewrite_to_a_usage_free_session_removes_the_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.jsonl"
            path.write_text(
                claude_usage_line("message-1", 10, 2, 1, "2026-09-24T00:00:00Z")
                + "\n"
            )
            store = Store(root / "state")
            try:
                store.ingest_path(path)
                path.write_text(
                    json.dumps(
                        {
                            "type": "user",
                            "sessionId": "claude-usage",
                            "timestamp": "2026-09-24T00:00:01Z",
                            "message": {"content": "done"},
                        }
                    )
                    + "\n"
                )
                store.ingest_path(path)
                count = store.connection.execute(
                    "SELECT count(*) FROM session_usage"
                ).fetchone()[0]
            finally:
                store.close()

        self.assertEqual(count, 0)

    def test_read_session_exposes_the_aggregate_and_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text(
                claude_usage_line("message-1", 2, 3, 1, "2026-09-24T00:00:00Z")
                + "\n"
            )
            session = read_session(path)

        self.assertEqual(session.usage.input_tokens, 2)
        self.assertEqual(session.usage.messages, 1)
        self.assertEqual(len(session.usage_rows), 1)


if __name__ == "__main__":
    unittest.main()
