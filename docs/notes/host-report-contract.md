# Phase 7 host report contract

This specifies the first cross-host input to TWILL. A TWILL instance reads
transcripts only on its own host, derives observations there, and writes a
report into that host's configured private `artifacts_root`. The existing
private-repository publisher carries a validated commit upstream as in
§7.3 and `artifact-contract.md`. TWILL neither contacts another host nor
commits or pushes the repository. No transcript, state database, or local
observation row is transported. The upstream consumer reads an exact,
validated commit, never a publisher worktree.

This contract supports a cross-host **normalized error signature** detector.
Other local detectors do not acquire cross-host evidence merely because a
host report exists. In particular, counts in a report are derived evidence,
not a substitute for a session id or an excerpt when a lesson needs local
review.

## Artifact and version boundary

Phase 7 adds `hosts/<host_id>/report.json` as a fifth reserved namespace to
the private artifacts repository. The file has schema
`twill-host-report/v1`; the containing `manifest.json` has schema
`twill-artifacts/v2`. A v2 manifest inventories every recognized file in
the original four namespaces and in `hosts/`, with the existing byte count
and SHA-256 rules. Its `paths` map adds this entry:

```json
"hosts": {"pattern": "hosts/<host_id>/report.json", "schema": "twill-host-report/v1"}
```

Each report also appears once in `artifacts` with that schema, its relative
path, byte count, and SHA-256. The original four artifact schemas and paths
keep their v1 meanings. A v1 artifact reader MUST reject a v2 manifest; the
first host report MUST NOT be published until the publisher and upstream consumer both
validate v2. A v2 reader accepts additive unknown fields but rejects an
unknown major version, an unrecognized path under `hosts/`, a duplicate or
missing inventory entry, a symlink, or a hash/size mismatch. An incompatible
host report or manifest change increments its respective major version.

`host_id` is an operator-configured, stable, lowercase identifier matching
`[a-z0-9][a-z0-9-]{0,62}`. It is not taken from a transcript or guessed from
an observation. A configured host roster names the expected publishers; a
report from an unknown host is rejected, and an expected host with no report
is visible as stale. The path component, report `host_id`, and roster entry
MUST match. A host rename is an explicit roster migration: the old identity
is retired before the new one contributes to fleet counts.

## Report body

The UTF-8 JSON document has exactly one current snapshot per host. A valid
v1 report has exactly these top-level fields; timestamps are UTC RFC 3339
with `Z`. Unknown top-level fields are rejected so an unreviewed payload
cannot cross hosts under an additive field:

| Field | Meaning |
| --- | --- |
| `schema` | Literal `twill-host-report/v1`. |
| `host_id` | Stable configured publisher identity. |
| `generated_at` | Time the host built this snapshot, including an empty one. |
| `last_ingest_success_at` | Last successful local ingest, including a successful no-work run. |
| `window_start`, `window_end` | Inclusive UTC dates covered by the complete snapshot. |
| `signature_scheme` | Literal `twill-normalized-signature-sha256/v1`. |
| `records` | Complete, sorted array of daily aggregate records for that window. |

Each record has exactly `day` (UTC `YYYY-MM-DD`), `kind` (one of
`run_failed`, `tool_error`, `tool_rejected`, `hook_denial`),
`signature_sha256` (64 lowercase hexadecimal characters), `sessions`,
`events`, `first_seen`, and `last_seen`. Counts are positive integers;
`first_seen` and `last_seen` are UTC timestamps within `day`, ordered
earliest to latest. There is at most one record per
`(day, kind, signature_sha256)`, sorted by that tuple. `sessions` counts
distinct local sessions in that bucket and `events` counts the capped local
observations. A session spanning two dates contributes once to each date;
adding daily `sessions` does **not** produce a distinct-session count for a
multi-day window. The cross-host detector counts distinct hosts with
reported evidence for a signature, not a sum of those session counts.

The exporter selects only post-redaction local observations with a nonempty
normalized `signature` and one of the four listed kinds. It computes the
full SHA-256 of that normalized signature. The existing local `sig_hash`
stores only a 12-character prefix and MUST NOT be used as the cross-host
identity. The signature scheme version changes if redaction, normalization,
or hashing changes in a way that changes grouping; that change also requires
a new host-report major version. Consumers MUST NOT merge records from
different schemes. No record contains the normalized signature
itself. Hashes can still be guessed for common errors, so this entire
namespace remains private and is never mirrored into this public repository
or sent to the Phase 8 recall index.

For example, an idle but healthy reporter publishes an empty `records` array
and advances `generated_at` and `last_ingest_success_at`:

```json
{
  "schema": "twill-host-report/v1",
  "host_id": "example-host",
  "generated_at": "2026-10-16T12:00:00Z",
  "last_ingest_success_at": "2026-10-16T11:55:00Z",
  "window_start": "2026-09-17",
  "window_end": "2026-10-16",
  "signature_scheme": "twill-normalized-signature-sha256/v1",
  "records": []
}
```

The v1 boundary permits **no free-form text** in `records`: no transcript
line, command, excerpt, normalized signature, program name, tool output,
repository or filesystem path, rule text, session id, cursor, usage row, or
model prompt. Unknown record fields are rejected rather than forwarded.
All strings are validated for their formats and lengths before publication.
The report is at most 16 MiB encoded as UTF-8 and at most 50,000 records.
If the complete report exceeds either bound, the producer MUST fail that
publication and surface the overflow locally; it MUST NOT truncate, sample,
or publish a partial snapshot that looks complete.

`last_ingest_success_at` MUST NOT be later than `generated_at`, and
`generated_at` MUST NOT be more than five minutes ahead of the consumer's
clock. A report with a `generated_at` older than the last accepted report
for that host is rejected; an equal timestamp is an idempotent retry only
when its bytes are identical. These rules keep a replayed or future-dated
snapshot from making a silent host appear current.

## Time window, retention, and import

The reporter publishes at least once per 24 hours, including when there are
no eligible observations. Its snapshot covers the last 30 complete or
current UTC dates, ending on the date of `generated_at`; `window_start`
is 29 dates earlier. It rebuilds the complete rolling snapshot from local
derived observations on each publication. A successful empty ingest still
advances the heartbeat. Report generation does not extend local transcript
or observation retention: the plan's 180-day local observation prune remains
in force, and transcripts remain under their existing owner.

The upstream importer validates the manifest and report before replacing
the prior accepted snapshot for that host. In one transaction it replaces
all previously accepted buckets for that host **inside the declared window**
with the complete `records` array, keyed by
`(host_id, day, kind, signature_scheme, signature_sha256)`. An empty array
therefore clears that window, and retries or overlapping rolling windows
are idempotent. It retains accepted buckets for 180 days from their UTC
`day`, then prunes them. A newer snapshot does not delete earlier accepted
buckets merely because they rolled out of its 30-day window; those buckets
age out at 180 days. A gap longer than the rolling window cannot be
inferred to contain zero events. The importer
records the coverage dates actually received and `doctor` reports missing
coverage rather than filling the gap. Records older than 180 days and dates
outside the declared window are rejected. The upstream consumer never
imports report records as raw observation rows or invents session ids.

Only a validated, committed snapshot is visible upstream. The publisher
stages the report and v2 manifest in one commit and pushes to the private
Forgejo origin without force. A failed report build, validation, commit, or
push leaves the previous remote snapshot in place; retrying a transient push
uses the same commit. An upstream validation failure retains the last valid
snapshot and its original freshness time. A new commit containing an old
report does not refresh that time.

## Doctor's per-host coverage

`doctor` evaluates the configured host roster, including hosts that have
never produced a record. For each host it reports `host_id`, state, last
accepted `generated_at`, last reported ingest success, most recent covered
UTC date, retained coverage gaps, and a reason. It uses `generated_at` and
`last_ingest_success_at` from the accepted report, not the git commit time,
file mtime, or most recent event timestamp. A host with zero observations
can therefore be healthy.

| State | Condition | Doctor result |
| --- | --- | --- |
| `current` | Valid report is at most 72 hours old, reported ingest success is at most 72 hours old, and the accepted coverage has no known gap in the last 30 dates. | Healthy for that host. |
| `stale_host` | Expected host has never reported, either timestamp is older than 72 hours, or current-window coverage is missing. | Degraded, with the specific missing or stale signal. |

An invalid candidate adds a `candidate_error` diagnostic and degrades the
check; it does not replace the last accepted report or reset that report's
age. The host's `state` still reflects the last accepted report. Historical
coverage gaps outside the current 30-day window remain visible until they
age out of the 180-day retention period; they never become invented zeros.

The aggregate host-coverage check is degraded (doctor exit 1) if any expected
host is stale or has an invalid candidate. An absent or silent remote host
MUST NOT turn the local ingest or detector run into a failed run or a broken
(exit 2) doctor verdict. Local corruption and local timer failures retain
their existing health semantics. A cross-host finding may say a signature
occurred on two hosts when two accepted reports contain positive evidence
in the detector window, even if one host has since gone stale. It MUST show
the contributing host ids, evidence dates, and freshness states, and MUST NOT
claim that a stale or missing host had zero occurrences or that fleet
coverage is complete. Recovery is a validated fresh report with contiguous
current coverage, never an inference from silence; no operator action is
inferred from silence either.
