# Phase 9B execution episode contract

`execution_episode` and `episode_assertion` are derived, versioned SQLite
projections of the redacted `interaction` contract. They are not assessments,
lessons, or grants of permission to TWILL. `twill ingest` replaces a session's
projection in the same transaction as its interaction rows and cursor. A new
projection version backfills existing interactions on the next writer open.
Append, rewrite, rebuild, redaction rescan, and retention prune rederive affected
sessions. The `input_sha` covers the projection version, retained source rows,
and omission summary; stable episode IDs hash session identity, starting source
ordinal, and projection version. Input or version changes invalidate support
that a later phase may attach to an episode.

| Field | Meaning |
|---|---|
| `start_ordinal`, `end_ordinal`, `session_key`, `thread_id` | Source span and main thread identity. Assertions use a source ordinal and line; `related_ordinal` points to the actual interactive question where applicable. Join through `interaction` for locator, source revision, offset and terminal state. |
| `objective`, `action_key` | Bounded redacted request text and exact verb/object keys. Explicit repeated-verb clauses use ` | ` to separate actions; paraphrases have unknown equivalence. |
| `authority_state` | `authorized` only for a captured imperative or matching explicit human approval; `withheld` for a captured hold or negative answer; `revoked` after cancellation or replacement; otherwise `unknown`. |
| `context_state`, `historical_policy_state` | Material truncation, session cap, unmatched answer, or missing scope makes context `unknown`. Historical policy starts `unknown`; an actual captured instruction with an effective date no later than its source time can mark it `captured`. Present-day rule files never do so. |
| `lifecycle`, `advice_only` | Active, cancelled, completed by human confirmation, or superseded; advice-only requests are marked separately. A terminal assistant reply alone does not establish outcome completion. |
| `kind`, `value`, `strength`, `detail` | Typed assertion with bounded redacted value, evidence strength (`known`, `inferred`, `unknown`), and source reference. |

Status questions retain the active objective. An exact same-action resume retains
it; generic continuation is recorded as unknown authority evidence. A new
independent request, explicit replacement, or material scope change opens a new
episode; cancellation revokes the active one. Interactive answers are resolved
by `question_ordinal`, including answers inside tool results. Assistant promises,
harness text, quoted history, silence, and sidechain summaries cannot authorize
the main outcome. An explicit permission hold defeats an inferred grant. A
captured external wait is distinct from an assistant claim that it is waiting.

This intentionally narrow projection can leave real authorization and action
equivalence unknown. It does not classify D-12 through D-16 candidates or
promote any episode into Explain; that belongs to Phase 9C and later review.
