# The accepted-lesson payload contract

This note pins the TWILL-side payload that an accepted lesson carries. The
source of the field list is `LessonRecord` in `twill_lessons.py`; the parser's
validation and the explicit lifecycle operations are the source of its value
rules. The inventory is exercised by `tests/test_lesson_payload_contract.py`.

An accepted lesson is a validated record in the private `artifacts_root`.
`LessonRecord.as_dict()` contains the frontmatter fields below, while `body`
is the operator-owned document body and is part of the accepted lesson even
though `as_dict()` leaves it out. `LessonRecord.path`, `_opening`,
`_frontmatter_lines`, and `_closing` are local file/loading state; they are not
payload fields and never cross a consumer boundary.

## Payload inventory

All text values in frontmatter go through `redact_text()` before they are
accepted. If redaction would change a value while loading a lesson, validation
rejects it rather than silently storing a different value. Text values are
single-line, contain no control characters, and are bounded at 240 characters.
Operator-supplied lifecycle tokens are redacted before they are written, so an
accepted record contains no raw credential or other redacted secret.

| Field | Validation and meaning | Redaction | Immutable after acceptance? |
|---|---|---|---|
| `id` | Required text matching `L-[0-9a-f]{8}`; the filename stem must match it. | Required text is checked by the common rule. | Yes. |
| `summary` | Required, non-empty bounded text describing the lesson. | Common rule; a value that would be redacted is rejected on load. | Yes. |
| `state` | One of `draft`, `accepted`, `resolved`, `escalated`, `retired`, or `applied:<layer>`. The layer must be one of `environment`, `hook`, `wrapper`, `skill`, `agents_md`, `memory`, or `retrieval_only`; state and `routing.applied` must agree. | It is validated as a state token; it is not free-form text. | No: changed only by an explicit lifecycle operation. |
| `detector` | Required identifier matching `[A-Za-z0-9][A-Za-z0-9._@-]*`. | Common rule. | Yes. |
| `key` | Required bounded text identifying the detector finding. | Common rule. | Yes. |
| `evidence` | Mapping with `sessions` (positive integer), `events` (non-negative integer), `first_seen` (ISO `YYYY-MM-DD` date or null), and a non-empty `session_ids` list. Each session id is required bounded text; duplicate ids are removed while preserving first-seen order. | Every session id is checked by the common rule. | Yes. |
| `routing` | Mapping with `recommended`, `reason`, `applied`, `applied_at`, and `bead`. Layer values are null or known layers. A non-null recommendation requires `reason`; an applied layer requires an ISO-8601 `applied_at` and an owner `bead`, except `retrieval_only`, which must not have a bead. | `reason` and `bead` use the common rule; timestamps are parsed and normalized to UTC `Z`. | Partly: `recommended` and `reason` are immutable; `applied`, `applied_at`, and `bead` change only through apply/unapply lifecycle operations. |
| `backtest` | Mapping with `window_days` (positive integer), `sessions` (non-negative integer), `first_seen` (ISO `YYYY-MM-DD` date or null), and `weeks_present` (non-negative integer). | No free-form text is accepted in this mapping. | Yes. |
| `guard` | Null or a mapping normalized to `layer`, `artifact`, and `installed`. `layer` is null or a known layer; `artifact` is nullable bounded text; `installed` is boolean. Missing guard values default to null, null, and `false`. | `artifact` uses the common rule. | No: guard metadata is added or updated only by the explicit guard operation. |
| `body` | The complete document body. Every logical line must remain unchanged by redaction and must be at most 240 characters; redaction is checked before the bound. | A line that would be redacted is rejected; TWILL does not substitute redacted text into an accepted body. | Yes. |

The validator returns normalized mappings for `evidence`, `routing`,
`backtest`, and `guard`; unrecognized nested input is not part of the
`LessonRecord` payload. Numeric booleans are not accepted as integers. Dates
and timestamps are validated at the lesson boundary, and timestamps are
stored as UTC with a trailing `Z`.

Acceptance itself is an explicit operator transition from `draft` to
`accepted`. Once accepted, an operator may move the lifecycle state to an
`applied:<layer>` state and later to a terminal state, or unapply back to
`accepted`; those transitions update only the lifecycle-controlled routing
values described above. The record's identity, detector provenance, evidence,
backtest, summary, key, and body are not editable through a transition.

## Consumer-side boundary

Any future intake contract, including NEEDLE N-T09's proposed `POST
/v1/ingest` lesson schema, must be able to receive one lesson object carrying
all ten payload fields above. To survive the kernel hop it must preserve the
field names, nested mappings, null values, integer values, ordered
`session_ids`, `applied:<layer>` state, and body line content. It may wrap the
lesson in a transport envelope, but it must not require TWILL to invent a
second identifier, flatten the nested metadata, drop the body, or replace the
accepted record with a lossy summary. The consumer must treat the record as
validated data and retain the distinction between `accepted`, an applied
layer, and a terminal state.

This is an inventory, not an export or endpoint specification. The external
decision is already settled: WARP subsumes serving, and TWILL lessons reach
workers only through NEEDLE's kernel. Delivery therefore remains gated on
NEEDLE N-T09's intake contract; this repository adds no network call or
exporter here.

TWILL will never add a model prompt, prompt-injection wrapper, tool command, or
instruction to the payload. `summary`, `evidence`, and `body` remain untrusted
lesson data for the consumer to interpret under its own authority; they are
not a request to override kernel policy or execute an action. TWILL also will
not create a second lesson store or disposable projection as part of this
boundary. The existing private artifact store remains the source document
until a separately authorized consumer contract exists.

## Drift guard

`tests/test_lesson_payload_contract.py` derives the public dataclass fields,
compares them with the ten-field inventory above, checks that the note names
each field, and deliberately checks synthetic records with one added or one
removed field. A change to the public `LessonRecord` shape therefore fails the
test until this contract note and its expected inventory are reviewed
together.
