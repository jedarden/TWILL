# The versioned detector registry

How detectors are named, versioned, run and isolated. Pins plan §4 (the
glossary's detector definition), §8.1 EC-12 (versioning) and §8.2 (per-detector
isolation). The code counterpart is `twill_detectors.py`; the run record lives
in the `detector_run` table (schema migration 2). Exercised by
`tests/test_detectors.py`.

## Identity: `D-NN@N`, one active version

A detector is a name (`D-01` … `D-10` in the Phase 2 catalog), an integer
version starting at 1, a prose description, one cluster query, and optional
session-hit and week-hit queries used by attribution and lesson backtests. The versioned
identity `D-01@2` is what run records and measurements cite; the *base* id is
what `cluster` rows and lesson frontmatter cite, because the cluster is the
real-world problem and must stay stable across version bumps — a bump refreshes
`(detector_id, key)` in place rather than forking every cluster's history.

`twill_detectors.REGISTRY` holds exactly one active version per name. A
registry carrying `D-01@1` and `D-01@2` together is a construction error, not a
runtime surprise: that ambiguity is what EC-12 exists to prevent. When a new
version lands, the old one leaves the registry (its `detector_run` row and any
measurements remain, which is the point).

## The SQL contract

`cluster_sql` is a single read-only `SELECT` (a `WITH` CTE is fine) — anything
else is rejected at registration, so detector SQL can never write. The runner
binds two named parameters:

- `:window_start_utc` — ISO-8601 UTC timestamp of the window's start;
- `:window_days` — the window's length in days (integer).

The query must emit the columns `key`, `sessions`, `events`, `first_seen`,
`last_seen` (any order; extras ignored). `sessions` and `events` must be
integers ≥ 0; `key` must be non-empty text. A violation is isolated exactly
like a SQL error — it is the detector self-test failing at run time.

A detector that can draft lessons also supplies `week_hits_sql`. It binds the
same window parameters plus an optional `key`, and emits `key` and an ISO
`week` (`YYYY-Www`). Its rows identify observations belonging to groups the
cluster query emits over the full window; it does not apply the distinct-session
threshold once per week, because one chronic hit per week is still a standing
problem. The backtest counts the distinct weeks for the requested key. A missing
or invalid week query fails the draft; it never silently renders zero history.

## What the runner does with the output

Per detector, in one `BEGIN IMMEDIATE` transaction:

1. run the query and validate every emitted row against the contract;
2. redact each key and truncate it to 240 chars (§8.3 — the key is derived from
   already-redacted observation columns, but the runner re-asserts the
   invariant);
3. upsert into `cluster` keyed by `(detector_id, key)`, writing `score` as 0.0
   (the ranker owns scoring) and never overwriting `state` or `covered_by` —
   review state outlives re-runs, so a dismissed or covered cluster is not
   resurrected by a refresh;
4. delete this detector's `state = 'open'` clusters that the run no longer
   emits (cluster output is refreshed per run, §7.1);
5. upsert the `detector_run` record.

Because all five steps commit together, a failure leaves no partial cluster
writes and no semantics stamp for output that never committed.

## Isolation (§8.2)

A detector whose SQL errors or whose output breaks the contract is rolled back,
skipped and reported; every other detector still runs and commits. The digest's
"detectors that ran" line and `detector_run.last_status` make the skip visible.
Exit codes: any refused detector → 4 (validation failure — the detector
self-test); else any errored detector → 1 (runtime error); else 0. In both
failure cases the healthy detectors' clusters are committed.

## Versioning discipline (EC-12)

`semantics_sha` fingerprints what a version *means*: its id, version and SQL
with whitespace normalized. Prose is outside the hash — rewording a description
does not redefine a detector.

The sha is stamped into `detector_run` **only when that version commits
clusters**. Consequences, each deliberate:

- Re-registering the same version with different SQL is *refused* before
  anything runs (exit 4, "semantics changed without a version bump"). A
  measurement series is never silently redefined; changing what a detector
  means requires a new version, openly.
- A version that has only ever errored has no stamp, so *fixing* a broken
  detector under the same version does not trip the drift check — it never
  produced a series to redefine.
- A runtime failure after a success updates `last_status`/`last_error` but
  never the stamp: a disk error and its recovery are not semantics changes.
- After a version bump, both versions' rows remain in `detector_run`, so a
  measurement series can name the version each point ran (`D-01@1`, then
  `D-01@2`), which is how §8.3's "a measurement always records the detector
  version it ran" is satisfied downstream.

`week_hits_sql` has its own normalized `backtest_sha` in `detector_run`, leaving
`semantics_sha` and the attribution series stable. A successful detect stamps it;
changing it later under the same version is refused, and a run predating the
stamp must be refreshed before lesson drafting. This keeps `weeks_present`
versioned without redefining ordinary cluster counts.

The stamp lives in the state DB, which is derived and disposable: after a
`doctor --rebuild` the first run of each version re-stamps from the registry.
The durable cross-rebuild record of what a version meant is the registry's own
history in git plus the versioned ids recorded in measurements.

## D-01@1: missing binary

D-01 emits one cluster for each program with a non-empty normalized `run_failed` signature containing
`command not found` in at least two distinct sessions inside the trailing window. It requires a
non-empty `program`, groups by that program name, and returns `command-not-found:<program>` as the
key. `events`, `first_seen`, and `last_seen` describe every qualifying observation; query output is
ordered by sessions descending, `last_seen` descending, then key ascending. The registry keeps
`cluster.score` at zero; Phase 3's ranker owns the global score built from these fields.

## D-02@1: recurring error signature

D-02 emits one cluster for each normalized error signature seen in at least two distinct sessions
inside the trailing window. It considers `run_failed` and `tool_error` observations with a non-empty
`signature` and `sig_hash`, groups by both the hash and the normalized text, and returns the
normalized text as `key`. `sessions` counts distinct session ids, while `events`, `first_seen`, and
`last_seen` describe all matching observations. Query output is ordered by sessions descending,
`last_seen` descending, then key ascending. The registry keeps `cluster.score` at zero; Phase 3's
ranker owns the global score built from these fields.

## D-09@1: unread rule document

D-09 emits one cluster for each live indexed rule document whose newest `last_read_by_agent` value
for its content hash is missing or earlier than the active window start and which has no matching
`file_read` observation in the window. Reading either live copy of identical content marks that content
read; stale rows are excluded because they are retained only for coverage and corpus diagnosis. The key
is `unread-rule-doc:<path>`. `sessions` and `events` are zero because the finding has no contributing
observations, and `first_seen` and `last_seen` use the row's `indexed_at` timestamp. Output is ordered
by `indexed_at` descending, then key ascending. D-09 feeds rule decay and deletion review rather than
the new-lesson path.

## Adding or changing a detector

To add one (the D-01 … D-10 beads): append a `Detector` to
`twill_detectors.REGISTRY` at version 1, with a fixture-driven test asserting a
known expected count. A detector eligible for Explain lessons also supplies the
`week_hits_sql` contract above. To change what an existing detector means: bump its
version in the same edit, and leave a note in the description of what changed;
never register two versions of one name. To change only prose: no bump needed.
