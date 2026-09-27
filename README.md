# TWILL

**T**ranscripts **W**oven **I**nto **L**asting **L**essons.

TWILL reads raw agent session transcripts and turns recurring friction into reviewed, routed,
measured lessons about how this environment actually behaves — a missing binary agents keep
reaching for, a rule they keep breaking, a fact rediscovered every week.

A twill is the weave whose diagonal comes from a *repeating* pattern, which is the whole signal
here: a one-off error is noise, the same one across six hundred sessions is a lesson.

## The five phases

| Phase | What it does |
|---|---|
| **Detect** | Versioned SQL detectors over observations parsed from transcripts — failed commands, hook denials, rejected tool calls, interruptions, rediscovered facts |
| **Rank** | Cluster and score findings, then check them against the rules that already exist (MEMORY.md and its leaves, CLAUDE.md, repo `AGENTS.md`, skills). A rule that exists and still gets broken is an escalation, not a new lesson |
| **Rules** | Invert coverage into a per-rule earnings and decay report: covered clusters, rising or falling weekly recurrence, last read time, and deletion candidates |
| **Explain** | One bounded, schema-validated `claude -p` pass writes up the top clusters as draft lessons. Never runs on raw sessions |
| **Apply** | Route each lesson to the strongest layer that would actually stop it: environment fix > hook/gate > wrapper > skill > `AGENTS.md` > memory > retrieval-only. TWILL emits the change; a human applies it |
| **Measure** | Every lesson carries the detector that found it. Re-run it: if the problem did not stop, escalate a layer; if it has not appeared in 90 days, propose retiring the rule |

## What it is not

- **Not a transcript archive.** A separate pipeline owns capture and durability. TWILL copies and
  retains nothing.
- **Not part of that pipeline.** TWILL has its own repo, its own schedule, its own cursor and its
  own state. Ingest never calls it, it never reads the archive's derived index, and it keeps working
  when the archive is stopped or deleted.
- **Not an unsupervised writer.** It never edits CLAUDE.md, memory, hooks, skills, or another
  repository. It proposes; a human accepts. (NEEDLE's Reflect strand is the cautionary precedent:
  left to write into prompts by itself, it reinforced `Read -> File read successfully` 10,930 times.)

## Layout

```
docs/notes/        design decisions, detector catalog, redaction policy
docs/research/     prior art and source material
docs/plan/         plan.md — the complete plan (start here)
systemd/           the ingest hourly and digest weekly user timers
Makefile           `make install` — CLI symlink + config skeleton (§13.1)
config.toml.skeleton  the operator config `make install` lays down on first install
```

## This repo is the engine, not the output

**Nothing TWILL produces lives here.** Lessons, digests, measurements and guard artifacts are
distilled from sessions across every repository on the host — including private ones — and
redaction stops credentials, not business context. A public repository that accumulated them would
be a continuously-updating window into private work.

They are written under `artifacts_root`, a **separate private repository**, which is also the
transport the recall service pulls from. `artifacts_root` has no default: unset, or pointing inside
this tree, is a startup error rather than a fallback. Three mechanical guards keep it that way —
the `.gitignore` here, an open-path test that fails on an artifact write into this tree, and a CI
assertion that the published tree contains no artifact. A convention alone lasts exactly as long as
the first default-config run.

Working state lives outside the repo in `~/.local/state/twill/` (mode 600). The database is derived
and disposable — never committed, never off the host: an index of transcript text is *designed* to
be surfaced into future prompts, which makes a leaked secret in it worse than one sitting inert in
a transcript.

## Install

```sh
make install
```

Links `twill` into `~/.local/bin` and, on first install, lays the config
skeleton down at `~/.config/twill/config.toml` (mode 600). Both steps are
idempotent and never overwrite operator state: an existing config is kept, and
a non-symlink file already sitting at `~/.local/bin/twill` is a loud refusal
rather than a clobber.

`artifacts_root` is the one setting the skeleton deliberately does not provide
— it has no default (see above), so edit the laid-down config and point it at
the private artifacts repository before the first run; `twill` aborts at
startup until it is set. `make install` also lays down the hourly ingest units
and the weekly digest units under `~/.config/systemd/user/`; enable both timers
with `systemctl --user enable --now twill-ingest.timer twill-digest.timer` after
configuring the tool. The digest timer runs every Monday at 08:00 local time,
and its oneshot service runs rank, explain, and digest in order. Each service
remains a single oneshot activation, so overlapping timer firings are not
started.

## Tests

```sh
make test                        # or: pytest
scripts/definition-of-done.sh    # the one "done" command: the gated suite, pytest, ruff
```

The suite runs under the open-path audit harness (§8.3, §10.2): an
`open()`/`os.open` audit hook refuses any write outside this repository
(never its artifact directories), the state directory, `artifacts_root` and
test scratch space, and refuses any open under
`~/agent-transcript-archive` outright. Every Python subprocess the suite
spawns — each CLI verb — installs the same hook, so the verbs themselves
are policed. A bare `python3 -m unittest discover` skips the harness's
startup and deliberately fails; run the suite through `make test` or
pytest. See `docs/notes/open-path-audit.md`.

`scripts/definition-of-done.sh` (§10.2) is the workspace's declared
definition of done: one lane — the gated suite, the same suite under
pytest, and `ruff check .`. NEEDLE's verification gate runs it against a
clean `git archive` extraction of committed state, so it depends on
nothing but a POSIX shell and whatever tools are on PATH; a missing
optional tool (pytest, ruff) is a loud skip, never a silent pass.

## Phase 0 walking skeleton

The first executable slice reads one settled JSONL session from either local transcript source,
redacts credential-shaped values before persistence, stores normalized events in
`~/.local/state/twill/twill.db`, runs the trivial `D-00@1` activity detector, and renders the
result without writing an artifact into this public repository:

```sh
twill ingest --limit 1
twill digest --stdout
```

Use `--settle 0` for a deliberately controlled fixture, `--file PATH` to select one session, or
`--state-dir PATH` and `--source PATH` for an isolated run. The initial walking skeleton did not
implement ranking, coverage, LLM explanation, measurement, or timers; cursor and schema support now
live in the engine.

## Health checks

`twill doctor` is the read-only Phase 1 health entry point. It checks SQLite integrity and schema
version, ingest timer freshness, cursor parse or missing-path anomalies, rule-corpus hash drift or
vanished paths, the detector self-test, and free disk space:

The detector self-test (plan §13.3) replays every registered detector's whole query family —
cluster, session-hit, week-hit, and weekly SQL — against a small in-memory fixture built from the
shipped schema and seeded with one known-true finding per catalog detector. A detector that fails
to parse, breaks its emission contract, or quietly stops selecting its fixture finding is reported
broken by name before a weekly digest is built from the registry.

Ingest refuses to start below 2 GiB free space (exit 1); doctor warns below 5 GiB so the operator
has time to recover before writes fail.

```sh
twill doctor
twill doctor --json --state-dir ~/.local/state/twill
```

The command exits `0` when healthy, `1` when degraded, and `2` when broken. These health exits are
reported through the normal JSON envelope; argument errors retain the CLI usage-error contract.

`doctor --rebuild` is the one recovery action (plan §5 Scenario 3, §8.2): after database loss or
corruption it discards `twill.db` and its WAL sidecars, recreates the schema, and reparses every
settled session still on disk through the ordinary ingest path — so the rebuilt rows match a
from-scratch ingest and the settle gate still applies. It takes the state lock like any mutating
verb, records a `rebuild` stage in `status.json`, and never touches the lesson, digest, or
measurement files under `artifacts_root`, which is why a full database loss costs nothing but
re-reading:

```sh
twill doctor --rebuild
```

After reverting a redactor regression, the stored bounded excerpts can be repaired in place with
the current redactor. The operation takes the state lock, applies configured content fences, and
records a `rescan_redaction` stage without rereading transcript files:

```sh
twill doctor --rescan-redaction
```

Enumeration happens before the database is touched: a missing source root or a tree whose files are
all younger than the settle window fails with exit 1 while the damaged `twill.db` is still on disk
for inspection. A tree with no transcripts at all rebuilds to an empty schema — the recovery is
"delete and reparse," and there is nothing to reparse. Source roots and the settle window come from
`config.toml` unless overridden with `--source` and `--settle` as in ingest.

## CLI output contract

Read verbs accept `--json` and write one JSON object to stdout. Successful commands use this
envelope:

```json
{"schema_version": 1, "generated_at": "2026-09-22T12:00:00Z", "data": {}, "warnings": []}
```

Failures use the same machine-readable surface regardless of whether the failure came from
argument parsing or command execution:

```json
{"error": {"code": 1, "message": "...", "hint": "..."}}
```

Exit codes are `0` success, `1` runtime error, `2` usage error, `3` lock held, and `4` validation
failure. JSON failures are written to stdout with no prose on stderr. Human-readable failures are
written to stderr, and credential-shaped values are redacted in both modes.
