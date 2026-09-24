# The normalized event contract

The record every transcript parser must emit, pinning plan §6.2 step 4. A parser
consumes complete JSONL lines from one transcript format and yields zero or more
normalized events per line; the detector layer keys on nothing else. Today two
parsers implement it — `twill_reader.py` and `codex_reader.py` — and their
`NormalizedEvent`, `MessageUsage`, and `TokenUsage` dataclasses are the normative
field lists; the rules each format follows to produce them are that format's
provenance rules, kept in a separate section so the contract itself stays
source-agnostic.
Exercised by `tests/test_reader.py` and `tests/test_codex_reader.py`.

## `NormalizedEvent` — one detector-facing event

| Field | Type | Meaning |
|---|---|---|
| `kind` | `str` | One of the six kinds below. |
| `source_line` | `int` | 1-based line number in the source file of the record that produced the event. For `run`, the invocation line, never the result line. |
| `event_index` | `int` | Position of this event among those produced by the same source line, 0-based. |
| `timestamp` | `str \| None` | The record's timestamp exactly as the source wrote it; `None` when the record states none. |
| `session_id` | `str \| None` | Session identity, sticky per file: the most recent value the file stated, `None` until the first one. |
| `cwd` | `str \| None` | Working directory, sticky per file, same rule as `session_id`. |
| `sidechain` | `bool` | `True` when the record belongs to a sidechain (sub-agent) transcript. |
| `text` | `str` | The event's digest content — the command for `run`, the path for `file_read`, the user's words for `user_turn_after_correction`, the tool output or sentinel otherwise. Never truncated by the parser. |
| `tool_name` | `str \| None` | The tool involved, when one was. `None` for user-turn and interrupt events. |
| `command` | `str \| None` | `run` only: the command string. |
| `exit_code` | `int \| None` | `run` only: the exit status the transcript states, including `0` where a source states it. `None` means the transcript states none — it is never an implicit success. |
| `error_excerpt` | `str \| None` | `run` only: the failure text that follows the stated exit status. `None` on success and on an unknown exit. |
| `file_path` | `str \| None` | `file_read` only: the path read. |

## The six event kinds

1. **`run`** — one shell command invocation paired with its result. Carries
   `command` (mirrored in `text`), optionally `exit_code` and `error_excerpt`.
   A run whose result never arrives (session ended, final line incomplete) is
   still a `run`, flushed at `finish()` with an unknown exit. A call that was
   rejected before executing is never a `run`.
2. **`tool_error`** — a failing result of any tool other than the run tool. A
   failed run is reported by its own `run` event and must not double-count here.
3. **`tool_rejected`** — a call the user declined before it executed, or a user
   turn that is itself the refusal. Classified before any run or error
   check: a rejected call becomes neither `run` nor `tool_error`, so it never
   feeds run-failure detectors.
4. **`interrupt`** — the user cut off a turn. Recognized by exact comparison
   with the source's interrupt sentinel; substring matching would false-positive
   on prose about being interrupted.
5. **`file_read`** — a file the agent read. Emitted at the invocation, not at
   the result, and carries `file_path`.
6. **`user_turn_after_correction`** — the first real user turn after an
   `interrupt` or a `tool_rejected`, before the agent has acted again. Once the
   agent acts — a new tool invocation or a completed result — the user's next
   turn is a new instruction, not the correction, and is not an event at all.
   Turns the source marks as harness bookkeeping rather than user input never
   count.

Everything else in a transcript — assistant prose, successful non-run tool
results, ordinary user turns — yields no event. Denial events (detector `D-04`)
are not transcript events either; their count comes from the external denial
log as a reader input (plan §6.5), not from parsing.

## `TokenUsage` — one positive usage delta

Accounting data, not detector events: usage rows are exposed separately from
events and never become observations. A parser emits usage rows when — and only
when — its source format carries usage snapshots in the transcript stream.

| Field | Type | Meaning |
|---|---|---|
| `source_line` | `int` | Line whose snapshot produced the row. |
| `event_index` | `int` | Always `0`; one snapshot yields at most one row. |
| `timestamp` / `session_id` | `str \| None` | As on `NormalizedEvent`. |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_output_tokens`, `total_tokens` | `int` | Non-negative delta since the previous snapshot. |
| `model_context_window` | `int \| None` | Stated context-window size, when the snapshot carries one. |

Snapshots are cumulative; the parser converts each to the delta against the
previous snapshot. The first snapshot's delta is its full value. A counter that
moved backwards is treated as a restart, not a negative: the new value itself
becomes the delta. A snapshot whose every counter is unchanged emits no row.
Source-specific counter spellings normalize to these six names.

## `MessageUsage` — one deduplicated Claude provider message

Claude assistant records carry usage counters on `message.usage` and identify
one provider response with `message.id`. `MessageUsage` preserves that identity
and the maximum value observed for every counter when a transcript repeats the
same provider message. A missing provider id is retained as an unkeyed row
rather than discarded. The persistence boundary sums these deduplicated rows
into `session_usage`; the message id itself is not stored in the corpus.

| Field | Type | Meaning |
|---|---|---|
| `source_line`, `event_index` | `int` | Location of the first record for this provider message. |
| `timestamp`, `session_id` | `str \| None` | Record context, with the session id sticky per file. |
| `message_id` / `provider_message_id` | `str \| None` | Provider message identity, when stated. |
| `model` | `str \| None` | Model stated by the message or cumulative cost state. |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_output_tokens`, `total_tokens` | `int` | Non-negative message counters; duplicate records contribute their component-wise maximum. |
| `cost_usd` | `float \| None` | Per-message cost when the source states one. |

Claude `cost-state` records are cumulative session snapshots. Their maximum
`totalCostUSD` and duration supply the aggregate cost and wall time; they are
never added once per repeated snapshot.

## Invariants shared by every parser

- **Complete lines only.** A line that does not parse as a complete JSON object
  yields nothing; earlier complete lines remain usable.
- **Redaction before truncation.** Every text field passes the redactor before
  it is bound to a persisted statement, and excerpts are cut to 240 characters
  *after* redaction (plan §6.2 step 5). Truncating first could sever a
  credential into a prefix the redactor no longer recognizes, so the parser
  never truncates — the cut belongs to persistence (`twill_redactor.py`).
- **Rejection wins classification.** Rejection sentinels are checked before
  exit-status parsing and error heuristics, in that order.
- **`None` exit is unknown, not zero.** A `run` with `exit_code=None` must be
  countable separately from a passing one.
- **One call is one pairing.** A call record registers pending state under its
  call identifier; the matching result consumes it. A run call with no
  identifier can never be paired with a result, so it is resolved immediately
  with an unknown exit.

## Provenance rules by source

### Codex rollouts (`codex_reader.py`)

- Prompts arrive as `response_item` records with `role=user`. Legacy
  `event_msg` `user_message` records are deliberately not treated as a second
  prompt — only an exact interrupt sentinel inside one is meaningful — so a
  prompt is never emitted twice.
- Shell calls are `custom_tool_call` / `function_call` records (commonly named
  `exec`); their results arrive in the matching `*_tool_call_output` record.
  The command is recovered from the call's input (`cmd` / `command` key, or the
  raw string).
- There is no structured exit field: the exit code is recovered from the result
  text by bounded patterns ("Process exited with code N" and kin), and `0` is
  recognized explicitly. A result with no stated code that still looks like an
  error becomes a `run` with an unknown exit and an error excerpt.
- An explicit `turn_aborted` event record is an `interrupt`, as is the exact
  interrupt sentinel in a prompt.
- An assistant `response_item` clears the correction-waiting state even when it
  is prose only — "assistant spoke, then user spoke" must stay distinct from a
  correction after an interruption. One `response_item` is one prompt: at most
  one correction turn is emitted per prompt record, and additional content
  blocks are ignored rather than duplicated.
- Usage arrives in `event_msg` `token_count` snapshots (and `token_usage_record`
  records) as cumulative totals; `cached_input_tokens` and
  `cache_write_input_tokens` are the counter spellings that normalize to
  `cache_read_tokens` and `cache_write_tokens`. Persistence keeps the
  component-wise maximum snapshot for the rollout rather than summing repeated
  cumulative snapshots.

### Claude Code transcripts (`twill_reader.py`)

- An `assistant` record carries `tool_use` blocks; the matching `user` record
  carries the `tool_result`. Only the run tool's failing result states an exit
  status, as a leading `Exit code N` line whose remainder is the error excerpt;
  a passing run leaves `exit_code` unset.
- A non-run tool result is a `tool_error` exactly when the record carries the
  structured error flag; text heuristics are not used for this classification.
- Rejection sentinels are matched in tool-result content and in user text
  turns; the wording varies between client versions, so the sentinels live in
  one tuple in the parser and nowhere else.
- The interrupt sentinel is compared exactly against a user text turn; `isMeta`
  user records are harness bookkeeping and never count as corrections.
- The run tool and read tool are recognized by fixed tool names; a read is
  emitted at its invocation without waiting for a result.
- Assistant usage is read from `message.usage`. Repeated records with the same
  `message.id` are one `MessageUsage` row, with each counter reduced to its
  maximum; rows without a provider id remain distinct.
- `cost-state` records are cumulative: `totalCostUSD` and `totalDuration` are
  reduced to their maxima, and `modelUsage` supplies the model when no message
  states one.

## Related, and out of scope here

The per-run record-type histogram that feeds the drift alarm (`parse_shape`,
plan §6.1) counts source record shapes, not events, and is not part of this
contract. How events land in the `observation` table — redaction, excerpt cut,
signature derivation — is the persistence side (plan §6.2 steps 5–6); detector
`D-xx` mapping and ranking are the consumers' business (plan Phase 2).
