# Phase 9A interaction contract

`interaction` is an additive, versioned conversation record (`twill-interaction/v1`).
The six detector-facing normalized event kinds in [event-contract.md](event-contract.md)
remain separate and unchanged. Claude and Codex parsers emit interactions while replaying
source JSONL through the same CLI ingest path as events. They retain no raw transcript
body in the state database.

| Field group | Meaning |
|---|---|
| `session_key`, `session_id`, `thread_id`, `turn_id`, `message_id` | File identity, provider session/thread and parser turn/message identities. `thread_id` is nullable when the source omits it; `sidechain` distinguishes delegated content even then. |
| `source`, `contract_version`, `source_path`, `source_identity`, `source_offset`, `source_line`, `event_index`, `locator` | Parser/source version and source location. `source_identity` and offset come from the cursor for the ingested prefix. |
| `ordinal`, `parent_ordinal`, `call_ordinal`, `question_ordinal` | Stable per-session sequence and links. A source parent UUID resolves to its emitted record when present; absence stays null. A tool result links its call; a human answer delivered in a tool result or user turn links the question. |
| `ts_utc`, `ts_local`, `actor`, `channel`, `origin`, `sidechain` | Source time and roles. Origins are `human_request`, `human_answer`, `assistant_text`, `question`, `tool_call`, `tool_result`, `harness`, and `quoted_history`. |
| `terminal_state`, `truncated`, `text` | `completed`, `interrupted`, `unknown`, or `none`; explicit bound/omission flag; redacted text of at most 240 decoded characters. |

An assistant `phase=final_answer` or similar label does not prove completion.
Codex JSONL `task_complete`/`turn_complete`/`task_finished` can prove completion
of the trailing assistant reply; an explicit abort proves interruption. Claude
JSONL has no supported native completion record, so its trailing reply remains
`unknown` unless a future source-native proof is added. A new user turn closes
an earlier reply as `unknown`; intermediate prompts therefore do not imply
abandonment. Repeated provider message IDs emit a single interaction.

The optional `TWILL_HISTORY_DB` setting points at a Codex-owned
`thread_history_1.sqlite`. The `codex-thread-history/v1` adapter recognizes
`thread_turns` (`thread_id`, `turn_id`, `status`, `completed_at`,
`final_agent_item_id`) and `thread_items` (`thread_id`, `turn_id`, `item_id`).
Only a completed turn with a non-null completion time and a matching final
item can upgrade an unknown Codex reply whose provider message ID matches that
item ID. The adapter opens SQLite in `mode=ro`, sets `query_only`, and reads
inside one transaction for a consistent snapshot. It does not read item JSON,
write, migrate, or repair the source. An absent or unsupported history source
leaves JSONL ingestion available and terminality unknown.

Redaction runs before any interaction value is bound to SQLite. `text` is cut
after redaction; `truncated=1` records a material omission. At most 1,000
interactions per session are stored in source order after the retention filter.
`interaction_summary` records retained count, cap omissions and their ordinal
bounds, truncations, unknown terminality and optional history confirmations.
A downstream assessment must abstain when omitted or truncated context may
hide authority, scope or a disconfirmer. Interaction replacement, cursor
update, and observation derivation commit in one transaction. Append, rewrite,
rebuild, prune and `doctor --rescan-redaction` cover the interaction rows.
