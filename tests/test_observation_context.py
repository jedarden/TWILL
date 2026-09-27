"""Tests for source and session context on persisted observations."""

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twill_app import Store, read_session  # noqa: E402


class ObservationContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_claude_observations_use_transcript_parent_and_sticky_session_cwd(self):
        path = self.root / "launch-dir" / "session.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "sessionId": "claude-context",
                            "timestamp": "2026-09-27T12:00:00Z",
                            "cwd": "/workspace/project",
                            "message": {"role": "user", "content": "first"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "sessionId": "claude-context",
                            "timestamp": "2026-09-27T12:00:01Z",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "second"}],
                            },
                        }
                    ),
                ]
            )
            + "\n"
        )

        store = Store(self.root / "state")
        self.addCleanup(store.close)
        store.ingest(read_session(path))

        self.assertEqual(
            store.connection.execute(
                "SELECT launch_dir, cwd FROM observation ORDER BY obs_id"
            ).fetchall(),
            [(str(path.parent), "/workspace/project")] * 2,
        )

    def test_codex_observations_use_session_metadata_cwd(self):
        path = self.root / ".codex" / "sessions" / "rollout.jsonl"
        path.parent.mkdir(parents=True)
        records = [
            {
                "type": "session_meta",
                "payload": {
                    "session_id": "codex-context",
                    "cwd": "/workspace/codex-project",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "id": "call-1",
                    "call_id": "call-1",
                    "name": "exec",
                    "input": "false",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-1",
                    "output": "Process exited with code 1\nfailed",
                },
            },
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records))

        store = Store(self.root / "state")
        self.addCleanup(store.close)
        store.ingest(read_session(path))

        self.assertEqual(
            store.connection.execute(
                "SELECT launch_dir, cwd FROM observation"
            ).fetchall(),
            [(str(path.parent), "/workspace/codex-project")],
        )


if __name__ == "__main__":
    unittest.main()
