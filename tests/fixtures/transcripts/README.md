# Synthetic transcript corpus

This directory is the stable fixture corpus for the transcript reader. Every
scenario is present in both source formats:

- `claude/` uses Claude Code-style `user` and `assistant` records with a
  `sessionId` and nested `message.content`.
- `codex/` uses Codex rollout-style `session_meta`, `response_item`, and
  `event_msg` records with a nested `payload`.

The corpus has six scenarios per source:

| Scenario | Fixture shape | How a test uses it |
| --- | --- | --- |
| clean | one complete JSONL session | ingest the file as-is |
| appended-between-runs | `base.jsonl` plus `append.jsonl` | ingest `base.jsonl`, append the bytes from `append.jsonl` to the same path, then ingest again |
| truncated-final-line | complete records followed by an incomplete JSON object on the final line | ingest as-is and assert complete records survive while the partial line is ignored |
| rewritten-in-place | `before.jsonl` and `after.jsonl` snapshots | put each snapshot at the same path in separate runs and assert the second snapshot replaces the first |
| secret-bearing | synthetic GitHub, AWS, and Bearer credential-shaped values | ingest as-is and assert only redacted markers reach derived output |
| injection-bearing | transcript text containing an instruction aimed at the agent | ingest as-is and treat the instruction as untrusted data |

`manifest.json` is the machine-readable index. Paths in the manifest are
relative to this directory, and every case declares its source and expected
fixture operation so later unit, integration, scenario, and property tests can
share the same corpus.

All credentials in the secret fixture are inert synthetic test values. They
exist only to exercise the redactor patterns.
