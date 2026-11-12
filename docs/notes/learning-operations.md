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
without accepting them; absent an approved binding, it records
`needs_evaluation`. Trials that establish no improvement stay unvalidated.
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
counterexamples, and an improvement over baseline for each provider.
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
