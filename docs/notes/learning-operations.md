# Operating the opt-in learning cycle

The legacy CLI still proposes lessons. `twill-learning` is a separate,
explicitly enrolled controller that can experiment and maintain owned blocks.
Its policy is local operator configuration, never model-authored authority.

## Installation and enrollment

Run `make install`. It installs the CLI symlink, a disabled example policy,
and the systemd user service/timer without replacing existing configuration.
Edit `~/.config/twill/learning.json` using `learning.policy.example.json`.
Use canonical absolute paths: a symlink target or ancestor is refused.

Set explicit Claude and Codex model IDs. Each scope lists exact instruction
files and `auto_apply`. A binding names either one lifecycle lesson ID or one
nonempty detector ID, its scope, an independently authored suite file and the
SHA-256 of that file's bytes. Changing the suite requires updating its policy
hash; a candidate cannot approve or rewrite its own oracle. Synthetic initial
autonomy scenarios live in `tests/fixtures/evaluation/autonomy-suite.json`.

For path-specific scopes, provide `context_paths` as a mapping from each of
`claude` and `codex` to the exact canonical guidance paths loaded at that
launch location, in order (global, repository and nested rules, including
imported guidance). These read-only manifests are separately frozen and hashed.
Without them, the context is the scope's nominated instruction files only.

`tests/fixtures/evaluation/autonomy-suite.json` evaluates continuing authorized
work, status exchanges, explicit cancellation and reserved spending authority.
Passing these text decisions alone makes no claim about live coding outcomes.
Candidate and baseline receive identical actual scoped instruction context,
without the candidate's owned block. Expected answers and split labels are
not passed to evaluated agents. Claude uses a verified explicit instruction
file manifest; Codex uses verified native AGENTS discovery in an isolated root.

One cycle may evaluate one lesson. Defaults are two balanced repetitions,
90 seconds per process, 36 experiment process invocations (including version
probes), and 30 minutes total experiment wall time. Offline CLI fingerprint
checks make no model requests. The systemd service has a 35-minute ceiling
and kills its process group on stop. Provider-internal HTTP retries are
bounded by process time, rather than counted individually.

Enable and start after the scratch rehearsal passes:

```sh
systemctl --user daemon-reload
systemctl --user enable --now twill-learning.timer
systemctl --user start twill-learning.service
twill-learning status --json
```

This host enrolls the existing, intentionally local global AGENTS.md and
CLAUDE.md files, whose ownership predates this controller. Codex's global
symlink continues resolving to that maintained AGENTS.md. Their recovery path
is managed-block invalidation, not restoration of a stale whole-file backup.
Repository-owned instruction paths require the repository's ordinary bead,
review and commit delivery; do not point a scope at a generated installed copy.
Local policy changes can be backed up before editing; the example restores
an unconfigured, disabled installation. No cluster deployment is required.

## Lifecycle and evidence

```sh
twill-learning create --spec /private/source-linked-spec.json
twill-learning experiment LC-0123456789abcdef
twill-learning apply LC-0123456789abcdef
twill-learning validate LC-0123456789abcdef
twill-learning invalidate LC-0123456789abcdef --reason "Counterexample failed"
twill-learning cycle
```

Create takes instruction text, scope and a source path with optional expected
source SHA-256, legacy lesson ID and detector ID. The ID versions the source,
instruction and scope. A cycle automatically imports existing legacy drafts
without accepting them; absent a trusted catalog admission or an approved
binding, it records `needs_evaluation`. Inconclusive trials remain unvalidated. Complete redundant
or harmful evidence produces a terminal rejection rather than repeated retries.

Supported source types use `auto_evaluators`: enroll a catalog name and exact
scope names once, with `global:true` or source `launchdirs` on each scope.
The controller verifies evidence, routes the source and instantiates independent
fixed cases automatically. The initial catalog contains terminal-traceback,
authorized-continuation and workflow-completion recipes. Candidate prose cannot
select labels, scopes or delivery authority; no per-lesson binding is needed.

`intervention_sources` explicitly enrolls raw transcript globs for bounded
completion-intervention discovery for native Codex and Claude JSONL, with a
two-hour default settlement window. The scanner requires captured completion authority, a native terminal delivery
claim, human followup, explicit incompleteness admission and renewed authority
in order. It preserves role-bearing answers and abstains across unsupported
interactive tool formats. Private offsets resume bounded scans; duplicate
episodes do not repeatedly spend experiment quota.
No-op retries are cooled down until the revalidation interval (seven days by
default). Invalidation is terminal for that lesson version.

Private `<artifacts_root>/learning/` contains records, immutable history,
experiment reports, a hash inventory and last-cycle status. `publish=true`
commits only that evidence fileset to the private repository's main branch
and pushes only its configured origin. Status and lock files remain local;
the namespace has an independent manifest and is not an accepted recall feed.
Publication failures return an error and retain locally durable evidence.

Every experiment records separate provider results, actual/pinned model,
CLI version, context/suite/instruction hashes, all scored trials and errors.
Shared promotion requires every candidate assertion, including holdout and
counterexamples. Inferred fixes also require improvement over baseline for each
provider; source-verified human directives use the separate admission below.
Transport failures and unchanged performance cannot establish improvement.

An applied block's source, surrounding context, suite and CLI fingerprints
are rechecked on each cycle. Drift or unavailable revalidation suspends the
exposure; a complete harmful result invalidates it. A write-ahead intent
recovers partial applications only while admission remains current. Removal
preserves surrounding edits and stops on an edited owned block. Inspect any
`conflict` before rerunning; do not overwrite it with an old file snapshot.

## Verification and rollback

The live rehearsal creates a fresh scratch directory, uses explicitly synthetic
evidence and requires both native providers to improve before applying any
block. It then runs fresh revalidation, removes the block, preserves a human
edit and verifies no reapplication:

```sh
python3 scripts/rehearse-learning.py --scratch /private/scratch \
  --claude-model EXPLICIT_CLAUDE_MODEL --codex-model EXPLICIT_CODEX_MODEL
scripts/definition-of-done.sh
```

To withdraw the deployment, stop the timer first, invalidate each active lesson
while its exact-target policy remains available, then disable the policy.
Keep private history and experiment receipts for diagnosis. A modified owned
block requires manual reconciliation; no reset or force-push is part of recovery.
The legacy `twill doctor` remains the source for ingestion/parser health;
successful lifecycle scheduling is not proof that all historical input
warnings or longitudinal efficacy have been resolved.

`twill-learning doctor --json` reports state counts, pending age, missing
evaluators, unavailable grading/runtime providers and stale schedule/input
stages. Blocked queues return attention and nonzero cycle/doctor status.
`require_fresh_sources` enables pipeline freshness checks and observational
follow-up metrics in cycle status. These distinguish source-native terminal
replies, unknown/truncated coverage and same-family recurrence; they do not
equate a final reply with successful task completion or claim causality.

The actual scheduled acceptance harness is:

```sh
python3 scripts/verify-scheduled-learning.py --scratch /private/scratch \
  --claude-model EXPLICIT_CLAUDE_MODEL --codex-model EXPLICIT_CODEX_MODEL
```

It verifies real timer triggers, both providers, fresh interval-based validation,
automatic source-withdrawal removal and a no-op followup. It first checks a
harmless timer twice without model calls to catch unit/parser/D-Bus differences.
It uses an isolated synthetic source and never alters the production targets.

For code behavior, a trusted suite may supply a `python-functions/v1` oracle.
Models emit strict JSON Python files; the controller checks them in the pinned
Docker image without credentials/network or host/project write access. Hidden
input/output assertions remain inaccessible to the generated child process.
Unavailable runtime or malformed supervisor output is unknown; executed
assertion failures are failures. All execution inputs, image digest and grader
implementation are bound into evidence. Opt-in real isolation checks run with
`TWILL_DOCKER_TEST=1 PYTHONPATH=tests python3 -m unittest test_execution_oracle test_experiments`.


## Human-driven steering

Enable `human_steering: {"enabled": true, "scopes": ["EXACT_SCOPE"]}` in the local
policy to retain explicit durable human guidance from the enrolled transcript
globs. Examples are “For all future tasks, …” and “For future tasks in this
repository, …”. Natural standalone “Always…”, “Never…”, “Going forward…” and
“From now on…” directives default to repository scope. Repository intent requires
matching repository `launchdirs`; it cannot
be promoted into a global scope. These conservative whole-message forms avoid
silently treating task requests, pasted instructions or unsupported question
answers as persistent policy. Unrecognized corrections still need contextual
review and an explicit durable directive; this is not universal natural-language
preference inference.

The controller verifies transcript-path enrollment, the native human role, exact
instruction, raw line hash,
full source snapshot and captured scope. Later human withdrawals or replacements
are checked before rediscovery can revive an older directive. A spec's `kind` or supplied evidence
cannot confer human authority. It records `kind: human_directed` and separately
records `admission_basis: human_adherence`. The baseline comparison is preserved,
but an already-correct baseline does not reject the human rule. Both candidates
must pass every adherence, holdout and guardrail case with complete error-free
trials and verified model metadata. Inferred fixes retain their improvement gate.

A narrow set of exact continuation/status directives has an independent catalog
suite. Other explicit directives are discovered as pending lessons; bind their
independently authored suite by `lesson_id` or `instruction_sha256`, plus exact
scope, `suite` path and `suite_sha256`. Detector-wide human-steering bindings
cannot stand in for rule-specific tests. Revalidation, conflicts and removal use
the ordinary lifecycle. Source snapshot changes or enrollment revocation withdraw
an applied rule; existing rejected lessons are not resurrected.

Verify the separate admission with both native providers without touching
production targets:

```sh
python3 scripts/rehearse-human-steering.py --scratch /private/scratch \
  --claude-model EXPLICIT_CLAUDE_MODEL --codex-model EXPLICIT_CODEX_MODEL
```

The synthetic proof requires both baselines and candidates to pass, applies the
human rule without improvement, performs fresh validation, then changes the raw
source and verifies automatic removal while preserving human edits.


## Centralized original transcripts and backfill

The optional producer is `twill-archive-feed --policy PRIVATE_CONFIG`, installed
by `make install`. The example `archive-feed.policy.example.json` is disabled.
Configure a read-only object-store identity via a mode-600 credential JSON file
with `access_key` and `secret_key`; values never belong in command arguments,
logs or source control. Configure exact endpoint, bucket, reference/blob prefixes,
canonical private feed/state roots and limits. The SDK dependency belongs only
to the optional producer entry point. Core and its ordinary tests remain stdlib
only, offline and independent of archive services and indexes.

The producer uses LIST/GET only. It checks compressed and raw hashes/sizes,
complete gzip and newline boundaries, artifact identity and contiguous ranges
within one generation. A complete observed archived generation is not a claim
that the original session ended. Unsettled, gapped, corrupt or unsupported input
is unknown and cannot become a complete native transcript. Seeded pilot scopes
are reported as partial rather than a full corpus sweep. Total cache size and
per-run page/reference/session/byte/time budgets bound work; a storage limit
requires explicit capacity management and never deletes archive data.

Enroll only `FEED_ROOT/current/claude-code/**/*.jsonl` and
`FEED_ROOT/current/codex/**/*.jsonl` in `intervention_sources`, alongside local
sources. Immutable object-cache history must not be enrolled as fresh sessions.
The producer atomically updates stable per-artifact current paths, so new source
bytes or withdrawals invalidate older human-directed exposure. Feed files and
sidecars stay private and outside this public repository; raw JSONL is never
published as lesson evidence. The producer status is `STATE_ROOT/status.json`.
Optionally enroll `archive_feed` in learning policy with `enabled:true`, exact
`status_path`, and `stale_seconds`; missing/failed/stale feed health is actionable.
Fresh bounded `in_progress`/`partial` coverage is visible without claiming the
archive is exhausted.

```sh
systemctl --user daemon-reload
systemctl --user enable --now twill-archive-feed.timer twill-discover.timer
systemctl --user start twill-archive-feed.service
systemctl --user start twill-discover.service
twill-learning doctor --json
```

Discovery inventory and pending work are fair across every enrolled glob and
provider. `discovery_limits` defaults to 128 files, 30 cooperative seconds and
512 directory inventory entries per glob per run. The local private SQLite
queue retains completed fingerprints and partial parser cursors beyond 4,096
files; legacy JSON cursors migrate. It is rebuildable derived state, never the
source of human authority. The five-minute discovery timer makes no model calls;
the hourly learning timer retains experiment limits. `input_coverage` reports
known/completed/pending/visited/error counts and backfill progress. Completed
means the native scan finished, including unsupported or malformed input; it
does not mean a lesson was accepted. An empty
candidate queue never establishes an empty transcript corpus.

Installation preserves existing policy files and does not enable optional timers
automatically. Recovery can disable the optional timers and remove their source
globs while keeping local learning operational. Keep the private source snapshots
needed by active lessons; deleting or replacing current raw sources withdraws
human exposures during reconciliation. Credential recovery uses the operator's
original secret reference. Rebuilding queue metadata from enrolled originals
does not require restoring a learned rule or weakening an audit guard.
