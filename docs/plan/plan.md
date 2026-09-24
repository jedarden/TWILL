# TWILL

> Transcripts Woven Into Lasting Lessons — turns raw agent session transcripts into reviewed,
> routed, and measured learnings about how this environment actually behaves.

**Type:** Greenfield
**Status:** Draft
**Last updated:** 2026-09-19

---

## 1. Mission & North Star

**North Star:** Success is when a recurring friction in this environment — a missing binary, a
rule agents keep breaking, a fact rediscovered every week — is detected from transcripts without
anyone reading one, written up as a lesson with evidence, routed to the layer that actually stops
it, and then *observed to stop* by the same query that found it.

**Background:** Agent sessions on this fleet already carry the whole record of what went wrong:
failed commands, hook denials, rejected tool calls, interruptions, and the corrections that
followed. Those transcripts are already preserved and structurally indexed by a separate archive
pipeline on the same host, but nothing converts them into changes to the environment.
Today every lesson is hand-written: an incident happens, a human notices, a memory file or a
CLAUDE.md rule or an `org-rule-guard` clause gets written by hand. Measured on 2026-09-19 against
the existing graph, `sqlite3: command not found` appeared in 1,096 distinct sessions since 08-20,
`bf: command not found` in 697, and `go: command not found` in 632. All three were eventually
fixed on the host, but only after thousands of sessions had paid for them — and nothing recorded
that the fix worked. TWILL is the missing loop.

Two constraints shaped the design before any code: it must be **independent of transcript
ingestion** (a distiller failure must never touch capture, and learning must not be triggered by
a transcript arriving), and it must **never write its own conclusions into agent context
unsupervised** — NEEDLE's Reflect strand did exactly that and reinforced `Read -> File read
successfully` 10,930 times into every worker prompt.

## 2. Non-Goals (Explicit Scope Boundaries)

- **Not a transcript archive.** TWILL copies, retains, and backs up nothing. A separate archive
  pipeline owns durability; duplicating it would double both disk and the blast radius of a secrets
  leak, for no gain.
- **No integration with that archive, and no reads of its derived index.** Operator decision,
  2026-09-19. Ingest and learning have different cadences and failure modes; a distiller bug or a
  slow LLM pass must never stall capture, and learning must not fire per ingest event.
- **No automatic edits to CLAUDE.md, memory files, hooks, skills, or any other repository.** The
  Reflect precedent is the rationale: an unsupervised writer degraded every prompt on the fleet for
  weeks. TWILL proposes; a human accepts; the change lands through the normal path for that layer.
- **No model-based summarization of whole sessions.** The Explain step sees only ranked, redacted
  clusters. Feeding raw transcripts to a model is both the expensive option and the one that turns
  third-party text pasted into a session into instructions.
- **No catalog of its own for always/never events.** The authoritative list of events that must
  never happen — force-push, a mutating `kubectl` verb on a managed resource, a credential in argv —
  belongs to **ICG** (`irreversible-command-gate`), which enforces it at the `PreToolUse` boundary.
  TWILL consumes that catalog and reports where an event got through anyway (a gate gap); a second
  copy of the list would drift from the one doing the enforcing.
- **Delivery to agents is pull-only.** `twill brief <repo>` answers when asked. Nothing TWILL
  produces is injected into a prompt, a rules file, or a system context by TWILL itself — that is
  the Reflect failure mode, and it is what §2's no-auto-edit rule exists to prevent.
- **No fleet-wide collection in v1.** codinghome only (`~/.claude/projects`, `~/.codex/sessions`).
  lab, bench, and agent-sandbox come in Phase 7 through TWILL's own pull, never through the
  archive's `mirror/`.
- **Not a dashboard.** The deliverable is a markdown digest and a CLI. A web surface can be built
  later on the same store if the digest proves worth reading.
- **This repository never holds a distilled artifact.** TWILL is public; what it produces is not.
  Lessons, digests, measurements and guard artifacts are distilled from sessions across every
  repository on the host — including deliberately private ones — and the redactor stops credentials,
  not business context. A public repository that accumulated them would be a continuously-updating
  window into private work. They live in a separate private repository (§7.2), and three mechanical
  guards keep it that way (§10.2), because a convention here lasts exactly as long as the first
  default-config run.

## 3. Hard Requirements (Non-Negotiable)

- TWILL **MUST** run on its own schedule and its own cursor, and **MUST NOT** be invoked by, or
  add any code, timer, hook, or configuration to, the transcript archive pipeline.
- TWILL **MUST** be read-only with respect to everything outside its own repository, its state
  directory (`~/.local/state/twill`) and `artifacts_root`. Writes anywhere else are a bug, and are
  tested for.
- TWILL **MUST** write every distilled artifact under the configured `artifacts_root`, which
  **MUST** resolve outside this repository's working tree. There is no working default: an unset or
  in-tree `artifacts_root` is a startup error, not a fallback. This is the one setting that fails
  loudly rather than defaulting, because the failure mode it prevents is publishing private work.
- TWILL **MUST NOT** store, print, log, or commit a credential value. Any credential encountered in
  a transcript is redacted before it reaches the database, and a lesson records a *path*, never a value.
- Every quoted fragment that reaches a committed artifact (lesson, digest) **MUST** pass the
  redactor and **MUST** be ≤ 240 characters. Evidence is otherwise ids and counts.
- Transcript content **MUST** be treated as untrusted data at every stage, including inside the
  Explain prompt. Instructions found in transcript text are never followed.
- Every lesson **MUST** carry at least one detector id and at least one evidence session id, and
  **MUST** be re-measurable by re-running that detector.
- A lesson **MUST NOT** change its state from `draft` to `accepted` without a human action.
- If TWILL is ever deployed to a cluster it **MUST** be a Deployment with an internal scheduling
  loop; `kind: Job` and `kind: CronJob` are forbidden org-wide. CI **MUST** be an Argo
  WorkflowTemplate in `iad-ci`; `.github/workflows/*` is forbidden org-wide.
- **Forbidden dependencies/patterns:** no reads of the archive's derived index or its materialized
  episodes; no network calls
  except the local `claude` CLI invocation in the Explain step; no credential store access
  (TWILL needs none); no third-party Python packages in the core path beyond the standard library
  (see §6.4).

### 3.1 Normative Language

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY follow RFC 2119. When capitalized they
are binding; in lowercase prose they are descriptive.

## 4. Glossary

- **Settled session** — a transcript file whose mtime is older than the settle window (default 2h),
  and which is therefore eligible for parsing. Sessions are never parsed while actively being written.
- **Observation** — one durable derived row extracted from a transcript: a failed command, an error
  signature, a hook denial, a rejected tool call, an interrupt, a file read. The atom of evidence.
- **Detector** — a named, versioned SQL query (`D-01` … ) over observations that emits candidate
  findings. A detector is both how a problem is found and how its disappearance is later proven.
- **Cluster** — observations grouped by `(detector_id, normalized_key)`, scored and ranked. The unit
  a human reviews.
- **Lesson** — a reviewed, redacted, committed markdown document describing one recurring problem,
  its correct handling, its evidence, its routing layer, and its detector.
- **Routing layer** — where a lesson is enacted, in descending order of strength: environment fix >
  hook/gate > wrapper script > skill > repo `AGENTS.md` > memory file > retrieval-only.
- **Coverage** — the property that an existing rule (in MEMORY.md, a memory leaf, CLAUDE.md, a repo
  `AGENTS.md`, or a skill) already addresses a cluster. A covered cluster that still recurs is an
  **escalation**, not a new lesson.
- **Escalation** — moving an already-enacted lesson one layer stronger because measurement shows the
  problem did not stop.
- **Digest** — the weekly markdown report of ranked clusters, lesson state changes, and measurements.

## 5. Acceptance Scenarios

### Scenario 1: A recurring failure becomes a measured fix (happy path)

- **Setup:** 30 settled Claude sessions on codinghome contain `foo: command not found`. No existing
  rule mentions `foo`. TWILL has been ingesting hourly for at least a day.
- **Action:** the hourly `twill ingest` runs, then the weekly `twill digest`; the operator accepts
  the drafted lesson, applies the routing recommendation (install `foo`), and lets `twill measure`
  run for 21 days.
- **Expected:** the cluster appears in the digest's top 10 with `sessions=30`; a lesson file is
  written to `lessons/L-<id>.md` in state `draft`; after acceptance its state is `accepted`, then
  `applied:environment`; measurements recorded after the fix show 0 occurrences in the trailing 7 days.
- **Pass:** the lesson names detector `D-01` and ≥ 1 evidence session id; `twill lessons --json`
  reports `state=resolved` once the trailing-7-day count has been 0 for 21 consecutive days;
  `twill measure --json` shows the count series dropping to 0 *after* the applied-at timestamp.
- **Fail:** the lesson is written with no detector or no evidence id; the state advances to
  `accepted` without an operator action; the count series shows no drop yet `state=resolved`.

### Scenario 2: The archive is down, deleted, or mid-rebuild (independence / degraded path)

- **Setup:** `agent-transcript-armor.timer` and `agent-transcript-fleet.timer` are stopped,
  `graph.db` is deleted, and `~/agent-transcript-archive` is moved aside.
- **Action:** `twill ingest && twill detect && twill digest`.
- **Expected:** all three succeed and produce the same output they would have produced otherwise,
  because TWILL reads `~/.claude/projects` and `~/.codex/sessions` directly.
- **Pass:** exit code 0 from each; the digest is byte-identical to a run with the archive present;
  `strace`-free verification is sufficient here — a test asserts no path under `~/agent-transcript-archive`
  is opened (§10.2 open-path test).
- **Fail:** any command exits non-zero, any output differs, or any archive path is opened.

### Scenario 3: Interrupted mid-ingest, corrupt DB, truncated transcript (recovery path)

- **Setup:** `twill ingest` is killed (SIGKILL) halfway through a 10 MB session; separately,
  `twill.db` is truncated to 0 bytes; separately, a session file ends in a half-written JSON line.
- **Action:** re-run `twill ingest`, then `twill doctor --rebuild` for the corrupt-DB case.
- **Expected:** the killed run left no partial observations (single transaction per file span, cursor
  advanced only with it), so the re-run reproduces the identical rows. `doctor --rebuild` recreates
  the schema and re-parses every session still on disk. The truncated line is skipped, the cursor
  stops at the last complete line, and the parse-error counter increments.
- **Pass:** row-for-row identical observations after re-ingest (idempotency test); `doctor` exits 0;
  lessons and measurement history survive a full DB loss because they are files in git.
- **Fail:** duplicate or missing observations after re-ingest; a cursor advanced past an uncommitted
  span; `doctor --rebuild` losing an accepted lesson.

### Scenario 4: A secret is pasted into a transcript (safety path)

- **Setup:** a fixture session contains a realistic-looking `ghp_…` token, an `AKIA…` key, and a
  `Bearer` header, inside a failing command that also produces a genuine error signature.
- **Action:** `twill ingest && twill detect && twill digest && twill explain --dry-run`.
- **Expected:** the error signature is captured; every credential-shaped value is replaced with
  `<redacted:kind>` before it reaches the database, and therefore before any digest, lesson, or
  Explain prompt.
- **Pass:** a grep for each fixture value across `twill.db`, `digests/`, `lessons/`, and the
  Explain prompt file returns nothing; the test fails the build if it returns anything.
- **Fail:** any fixture value appears anywhere outside the fixture file itself.

## 6. Architecture

### 6.1 Component Overview

| Component | Single responsibility | Talks to |
|---|---|---|
| `reader` | Enumerate transcript files, decide which are settled, parse JSONL into normalized events, per-message usage rows, and the record-type histogram that feeds the drift alarm. Also reads two optional external inputs if present: the `org-rule-guard` denial log and per-session **friction receipts** (§6.5). Owns nothing else. | filesystem (read-only), `cursor` |
| `cursor` | Per-file identity + offset bookkeeping so appended files resume and rewritten files reparse. | `store` |
| `redactor` | Replace credential-shaped values and apply content fences before anything is persisted. | called by `reader` before every write |
| `store` | SQLite schema, transactions, migrations, retention. Single writer. | local state dir |
| `detectors` | Versioned SQL emitting findings from observations. Groups on a **normalized error signature**: paths, numbers, hexes and UUIDs are masked before hashing, which collapses one failure to one signature at the cost of a known limit — a differing context prefix can still split one failure across two signatures, so a count is a floor, not a total. | `store` |
| `ranker` | Cluster, score, and check coverage against the rule corpus. | `store`, `rulecorpus` |
| `rulecorpus` | Index the *existing* rules (MEMORY.md + leaves, CLAUDE.md, repo `AGENTS.md`, skills) for coverage matching. Read-only. | filesystem (read-only), `store` |
| `explainer` | Bounded, schema-validated LLM write-up of top clusters into draft lessons. | `claude` CLI (local), `store` |
| `router` | Recommend a routing layer and emit the bead-create command for the owning repo. Emits text; never executes. | `store` |
| `trend` | Per-signature weekly rates and change-point detection, so *new and accelerating* friction outranks chronic volume. | `store` |
| `economics` | Attribute token/cost usage to clusters so the digest ranks by waste, not by count. | `store` |
| `rulesreport` | Invert the pipeline: per existing rule, what it plausibly prevented, what never applied, what nobody read. | `rulecorpus`, `store` |
| `briefer` | Answer `twill brief <repo>` from accepted lessons and open clusters. Pull-only; never pushes anywhere. | `store` |
| `measurer` | Re-run each accepted lesson's detector, append counts, drive escalate/retire. | `store`, `detectors` |
| `cli` | `twill <verb>`, human and `--json` surfaces, exit contract. | all of the above |

### 6.2 Data Flow

One operation, end-to-end: the hourly ingest.

1. `cli` takes the state lock (`flock` on `~/.local/state/twill/lock`); if held, exit 3.
2. `reader` walks `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/**/*.jsonl`, and for each
   file asks `cursor` for `(identity_sha, last_offset, size, mtime)`.
3. Files with `now - mtime < settle_window` are skipped. Files whose size shrank or whose identity
   hash changed are marked for full reparse (derived rows for that session deleted in the same
   transaction).
4. For each eligible file, `reader` seeks to `last_offset` and parses complete lines only. Each line
   yields zero or more normalized events: `run` (command, exit, error excerpt), `tool_error`,
   `tool_rejected`, `interrupt`, `file_read`, `user_turn_after_correction`.
5. Every text field passes through `redactor` before it is bound to a statement. Excerpts are
   truncated to 240 characters *after* redaction.
6. `store` writes observations and the new cursor row **in one transaction**. A crash therefore
   either advances both or neither.
7. `detectors` run over the trailing window and upsert findings; `ranker` refreshes cluster scores
   and coverage flags.
8. `status.json` records stage, duration, counts, and last success.

The weekly path continues: `ranker` selects top-K uncovered clusters → `explainer` builds one
bounded prompt (≤ 8 KB per cluster, ids and counts plus redacted excerpts) → `claude -p` returns
strict JSON → schema validation → draft lesson files → digest markdown. The daily path is
`measurer` only.

### 6.3 Concurrency / Execution Model

Single-writer, single-process, no threads. All mutating verbs take an exclusive `flock` and run
serially; systemd timers are additionally `Persistent=true` with no overlap. Read verbs open SQLite
`mode=ro` and never block a writer (WAL). Parsing is IO-bound and sequential by design: the box runs
a NEEDLE fleet under cgroup caps, and a parallel parser competing for page cache is the failure mode
to avoid, not the performance win to chase. The Explain step shells out to one `claude -p` process
at a time and is the only step that leaves the machine.

### 6.4 Technology Decisions (Why X Over Y)

- **Python 3.13 + stdlib `sqlite3`, chosen over Rust.** The neighbouring transcript tooling is
  Python; the work is IO-bound JSONL parsing where Rust buys nothing; and `cargo test` on this box
  is redirected to iad-ci, which would put a CI round-trip in the inner loop of a tool whose whole
  value is fast iteration on detector queries.
- **Raw JSONL parsing, chosen over reading `graph.db`.** Operator decision for independence, and a
  technical one: the graph aggregates a session into edges and drops turn order, so it cannot answer
  "what did the user say immediately after interrupting this tool call" — which is the correction
  signal TWILL exists to find.
- **SQLite, chosen over Parquet/DuckDB.** Detectors are ad-hoc, indexed, incremental point queries
  with upserts, not columnar scans; and SQLite is already the fleet's idiom (`beads.db`, `graph.db`).
- **Files in git for lessons and measurements, chosen over rows in the DB.** Lessons are the durable
  product and must survive a DB rebuild, be diffable, and be reviewable in a normal commit. The DB is
  derived and disposable by design.
- **`claude -p` shelled out, chosen over an API client.** No credential to hold, no SDK dependency,
  and the subscription is already the sanctioned path on this host. The cost is that the child
  session inherits `CLAUDE_CODE_*` env vars when run from inside a session, which the explainer
  unsets explicitly (see EC-09).
- **Stdlib-only core.** `ruff` and `pytest` are dev-only. A distiller that breaks because a
  dependency moved is a distiller that stops producing lessons; this is the one place where
  boring wins outright.

### 6.5 Optional external inputs (owned elsewhere, read here)

Three inputs make detection cheaper and more reliable, and **none of them is TWILL's to install** —
each lives outside TWILL's two trees, which TWILL may not write to (§3):

- **`org-rule-guard` denial log.** The hook appends `{ts, rule, tool, session_id}` on every deny.
  Without it, denials are unmeasurable: counting them from transcript text is polluted because
  CLAUDE.md's own statement of the rules is itself indexed. Ownership is Open Question 1.
- **Friction receipt.** A `SessionEnd` hook writes one small structured record per session — rules
  consulted, denials, unresolved error signatures, whether the session ended mid-task — so TWILL
  reads facts instead of inferring them from prose. Ownership is Open Question 7.
- **The ICG always/never catalog.** `irreversible-command-gate` owns the authoritative list of
  events that must never happen and enforces it at the `PreToolUse` boundary. TWILL reads the
  exported catalog and runs one detector against it (`D-10`, gate gap): an event on that list that
  nevertheless executed is a hole in the gate, and is reported as such rather than as a new lesson.
  Export format is Open Question 8.

All three are strictly optional: every detector degrades to transcript-only parsing when its input
is absent, and `doctor` reports which inputs are live rather than failing without them.

**Degrading at runtime and being buildable are different things, and the distinction decides the
dependency graph.** At *runtime*, every reader degrades: an absent input is reported by `doctor`,
never a failed run. At *build* time, a reader cannot be written before its input's format is
settled — a parser for an undecided shape is not work, it is a guess. So each optional input's
reader waits on the question that fixes its format (Open Questions 1, 7 and 8), and the detectors
behind those readers inherit that wait.

**Two detectors additionally cannot degrade even once built: `D-04` and `D-10`.** Denials exist
nowhere else — counting them from transcript text is polluted because CLAUDE.md's own statement of
the rules is itself indexed — and a gate gap is meaningless without the catalog that defines what
must never happen. Every other detector runs on transcripts alone.

### 6.6 The serving layer runs on a cluster, not on this box

**Decided 2026-09-21 (operator).** The distiller stays on codinghome because transcripts are local
and must not be copied anywhere. The **agent-facing recall service does not** — it runs on a
Kubernetes cluster, reached over the tailnet, so a roaming worker on lab, bench or agent-sandbox can
ask the same question as one on codinghome.

The shape, which follows `git-activity-exporter` on ardenone-cluster (a Deployment with an internal
poll loop, a pinned semver image and a secret, cloning from `git.ardenone.com`):

- **Transport is git.** codinghome commits accepted lessons to the private lessons repository; the
  service polls that repository and rebuilds a **disposable** SQLite FTS projection from it. No
  write credential ever points from codinghome at the cluster, nothing new has to be exposed here,
  and the projection can be thrown away and rebuilt from the repo — the same
  authoritative-journal / disposable-projection split WARP is built on.
- **A Deployment with an internal scheduling loop**, never a `Job` or `CronJob` (forbidden
  org-wide: ArgoCD cannot manage them idempotently and their pods are never pruned).
- **Image** `ronaldraygun/twill-serve`, pinned to a semver tag from `containers/twill-serve/VERSION`
  — never `:latest`, never a bare SHA.
- **Exposure through the cluster's existing Traefik** as an IngressRoute on a tailnet-only
  entrypoint, never a new `tailscale.com/expose` Service: the rule is exactly one Tailscale-exposed
  Service per cluster. Bearer token for agents; forward-auth for a human opening it in a browser.
- **Manifests live in `declarative-config`** under that cluster's directory and sync via ArgoCD. The
  owning bead stays in this repository per the deployment-bead convention, and records the
  declarative-config paths, the target ArgoCD Application and the deployment commit.
- **Retrieval is reactive, not polled.** The service answers "what is known about this repo" and
  "what is known about this error signature". NEEDLE queries it on a failure, the way its retry
  prompts already query prior fixes; nothing polls on a timer, and nothing is injected by TWILL.
- **The export stays WARP-compatible.** If WARP ever ships its recall surface (its F9), this service
  is the thing that goes away, not a second store to reconcile.

## 7. Data Model

### 7.1 Core Entities

```sql
-- identity + resume position for every transcript file ever seen
CREATE TABLE cursor(
  path TEXT PRIMARY KEY, session_id TEXT NOT NULL, source TEXT NOT NULL,   -- claude|codex
  identity_sha TEXT NOT NULL,        -- sha256 of first 4 KiB, detects rewrite-in-place
  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  last_offset INTEGER NOT NULL DEFAULT 0,
  parse_errors INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL, last_indexed_at TEXT NOT NULL);

-- the atom of evidence; text fields are POST-redaction and ≤240 chars
CREATE TABLE observation(
  obs_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
  ts_utc TEXT NOT NULL, ts_local TEXT NOT NULL,        -- EC-15: both, from the v1 DDL onward
  kind TEXT NOT NULL,                -- run_failed|tool_error|tool_rejected|interrupt|file_read|hook_denial
  program TEXT, command TEXT, signature TEXT, sig_hash TEXT,
  tool TEXT, path TEXT, rule TEXT, excerpt TEXT,
  launch_dir TEXT, cwd TEXT, host TEXT NOT NULL DEFAULT 'codinghome');
CREATE INDEX obs_sig ON observation(sig_hash, ts_utc);
CREATE INDEX obs_kind_ts ON observation(kind, ts_utc);
CREATE INDEX obs_session ON observation(session_id);

-- detector output, refreshed per run; (detector_id, key) is the cluster identity
CREATE TABLE cluster(
  detector_id TEXT NOT NULL, key TEXT NOT NULL, window_days INTEGER NOT NULL,
  sessions INTEGER NOT NULL, events INTEGER NOT NULL,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  score REAL NOT NULL, covered_by TEXT,        -- rule file path, or NULL
  state TEXT NOT NULL DEFAULT 'open',          -- open|drafted|escalation|dismissed
  PRIMARY KEY(detector_id, key));

-- the rule corpus TWILL checks coverage against (read-only inputs, hashed for staleness)
CREATE TABLE rule_doc(
  path TEXT PRIMARY KEY, layer TEXT NOT NULL,  -- memory|claude_md|agents_md|skill|hook
  sha TEXT NOT NULL, indexed_at TEXT NOT NULL, last_read_by_agent TEXT);
CREATE VIRTUAL TABLE rule_fts USING fts5(text, path UNINDEXED, tokenize='porter unicode61');

-- per-session token/cost usage, extracted during ingest; feeds waste attribution
CREATE TABLE session_usage(
  session_id TEXT PRIMARY KEY, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cost_usd REAL, wall_seconds INTEGER, messages INTEGER);

-- weekly rate per signature; the series change-point detection runs over
CREATE TABLE cluster_week(
  detector_id TEXT NOT NULL, key TEXT NOT NULL, week TEXT NOT NULL,   -- ISO yyyy-Www
  sessions INTEGER NOT NULL, events INTEGER NOT NULL,
  est_waste_usd REAL,
  PRIMARY KEY(detector_id, key, week));

-- per-run record-type histogram per source; a shifted distribution is the drift alarm
CREATE TABLE parse_shape(
  run_at TEXT NOT NULL, source TEXT NOT NULL, record_type TEXT NOT NULL,
  n INTEGER NOT NULL, PRIMARY KEY(run_at, source, record_type));

-- small key/value store for state that is not a series: the last-seen ICG catalog version,
-- the schema version, the trailing medians the drift alarm compares against
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

-- one row per (lesson, measurement day); mirrored to measurements/<lesson>.jsonl for durability
-- detector_id carries the version (`D-01@2`, per EC-12), which is how §8.3's
-- "a measurement always records the detector version it ran" is satisfied
CREATE TABLE measurement(
  lesson_id TEXT NOT NULL, detector_id TEXT NOT NULL, measured_at TEXT NOT NULL,
  window_days INTEGER NOT NULL, sessions INTEGER NOT NULL, events INTEGER NOT NULL,
  PRIMARY KEY(lesson_id, measured_at));
```

A lesson is a file, not a table — `lessons/L-<8hex>.md` with YAML frontmatter:

```yaml
id: L-3b02ce2a
summary: "Two sentences, plain language: what goes wrong, and what to do instead."  # mandatory
state: draft            # draft -> accepted -> applied:<layer> -> resolved | escalated | retired
detector: D-01
key: "command-not-found:sqlite3"
evidence: {sessions: 1096, events: 1529, first_seen: 2026-08-20, session_ids: [...]}  # ids only
routing: {recommended: environment, applied: null, applied_at: null, bead: null}
backtest: {window_days: 180, sessions: 1096, first_seen: 2026-06-14, weeks_present: 13}
guard: {layer: hook, artifact: guards/L-3b02ce2a.hook.json, installed: false}
```

`backtest` is populated before a lesson may be reviewed (§9 Phase 4): the same detector is replayed
over the trailing 180 days, so a reviewer sees whether this is a standing problem or one bad week.
`guard` points at the ready-to-install artifact the router generated (§9 Phase 5); TWILL never
installs it.

### 7.2 Source of Truth & Storage

- **Transcripts are the upstream source of truth and are owned by someone else.** TWILL treats them
  as read-only and tolerates their disappearance; it never asks for retention changes.
- **`~/.local/state/twill/twill.db` (mode 600, WAL) is derived and disposable.** It is never
  committed and never leaves the host — identical reasoning to `graph.db`: a queryable index of
  transcript text is *designed* to be surfaced into future prompts, which makes a leaked secret in
  it worse than one sitting inert in a transcript.
- **The distilled artifacts are the durable product, and they live outside this repository.**
  `artifacts_root` (default `~/TWILL-lessons`) is a **separate private git repository** — Forgejo
  only, no GitHub mirror, never public — holding `lessons/`, `digests/`, `measurements/` and
  `guards/`. It is git rather than a directory for three reasons the plan depends on: a lesson
  survives a state-DB rebuild, a change to one is diffable and reviewable in a normal commit, and
  it is the transport the cluster recall service pulls from (§6.6). This repository — the engine —
  is public; that one is not, and nothing in the engine's tree is permitted to become an artifact.
- **Retention:** observations older than 180 days are pruned by `twill prune` (daily); clusters are
  recomputed from surviving observations; measurements and lessons are kept forever.

## 8. Pre-Flight Safety

### 8.1 Edge Case Catalog

- **EC-01: session file is still being appended.** Resolution: settle window (`--settle 2h`) gates
  eligibility; a file younger than the window is skipped entirely, not partially parsed.
- **EC-02: file grew since last run.** Resolution: resume at `last_offset`, parse complete lines only,
  advance the cursor to the last newline boundary.
- **EC-03: file shrank or was rewritten in place.** Resolution: identity hash mismatch or
  `size < last_offset` triggers full reparse; derived rows for that `session_id` are deleted in the
  same transaction that rewrites the cursor.
- **EC-04: half-written final JSON line.** Resolution: skip, do not advance past it, increment
  `parse_errors`; `doctor` reports any file whose `parse_errors > 0` on three consecutive runs.
- **EC-05: transcript disappears (upstream cleanup) between runs.** Resolution: keep observations,
  mark the cursor row `path_missing`; never treat absence as a reason to delete evidence.
- **EC-06: a session contains a credential.** Resolution: redactor runs before persistence (§11);
  a fixture test asserts non-appearance end-to-end.
- **EC-07: transcript text contains instructions aimed at an agent (prompt injection).** Resolution:
  Explain receives clusters, not sessions; the prompt frames every excerpt as untrusted data; output
  is schema-validated; a lesson cannot reach `accepted` without a human.
- **EC-08: `claude -p` is unavailable, rate-limited, or returns invalid JSON.** Resolution: Explain
  fails closed — no lesson is written, the cluster stays `open`, the digest still ships with the
  deterministic ranking. This is the Plan B boundary (§15).
- **EC-09: Explain invoked from inside a Claude Code session.** Resolution: unset
  `CLAUDE_CODE_CHILD_SESSION` and `CLAUDE_CODE_SESSION_ID` before spawning, otherwise the child
  writes no transcript of its own and the run is invisible to TWILL's own corpus.
- **EC-10: two timers fire at once, or an operator runs a verb by hand mid-timer.** Resolution:
  `flock`; the loser exits 3 with `lock held by pid N since T`, and systemd's next run picks it up.
- **EC-11: the rule corpus moves (a memory file renamed, a repo relocated).** Resolution: coverage
  matching is by content hash + FTS, not path identity; a `rule_doc` whose path vanished is marked
  stale and reported by `doctor` rather than silently dropping coverage.
- **EC-12: a detector is edited after lessons reference it.** Resolution: detectors are versioned
  (`D-01@2`); a measurement records the version it ran; changing a detector's semantics requires a
  new version, so a lesson's before/after series is never silently redefined.
- **EC-13: the disk is full** (`/` hit 100% on this box in August). Resolution: ingest checks free
  space first and refuses below 2 GB with exit 1; the DB is capped by retention; `doctor` warns below 5 GB.
- **EC-14: no new sessions since last run.** Resolution: exit 0 with `no work`, `status.json` still
  updated — an unchanged `last_success` is what staleness monitoring keys on.
- **EC-15: a detector's window spans hosts in different timezones.** Resolution: every observation
  carries its timestamp in both UTC and local time, so a detector can be explicit about which it
  means. This ships with the v1 schema, before any detector is written against a single column.
- **EC-16: one pathological session dominates a cluster.** Resolution: cap the observations a single
  session may contribute. A 500 MB transcript or a runaway retry loop otherwise outvotes a genuine
  cross-session pattern purely on volume, and ranking is a count of *sessions* for exactly that reason.

### 8.2 Failure Modes & Recovery

| Failure | Detection | Recovery | Data Safety |
|---|---|---|---|
| Ingest killed mid-file | cursor offset < file size on next run | resume from committed offset | one transaction per file span: rows + cursor advance together |
| DB corrupt / deleted | `PRAGMA integrity_check` in `doctor` | `twill doctor --rebuild` reparses every on-disk session | lessons + measurements are files in git, unaffected |
| Detector SQL error | non-zero from `twill detect`, per-detector isolation | that detector is skipped and reported; others still run | no partial cluster writes (per-detector transaction) |
| Explain returns junk | schema validation fails | cluster stays `open`; digest ships without it | no lesson written from unvalidated output |
| Timer stopped / host rebooted | `status.json` age > 3× interval | `doctor` exits non-zero; systemd `Persistent=true` catches up | none needed — ingest is idempotent |
| Upstream transcripts deleted early | `path_missing` cursor rows spike | nothing to recover; evidence already extracted | observations survive; lessons cite ids, not paths |
| Redactor regression | fixture test in the stop-ship gate | revert; re-run `doctor --rescan-redaction` over stored excerpts | test blocks release before any commit |
| **Parser silently stops extracting** (upstream transcript format changed) | dead-man's switch: zero observations in 24 h while transcript files are arriving; or `parse_shape` distribution shifts beyond tolerance vs the trailing median | `doctor` exits non-zero naming the source and the vanished/new record type; fixtures updated, parser fixed | no data loss — transcripts are re-readable, so a fixed parser backfills by resetting those cursors |

### 8.3 Invariants (Must Always Hold)

- No file outside `~/TWILL` and `~/.local/state/twill` is ever opened for writing.
- No path under `~/agent-transcript-archive` is ever opened at all.
- A cursor's `last_offset` never exceeds the byte offset of the last complete line whose observations
  are committed.
- Re-running ingest over unchanged inputs produces byte-identical derived rows (idempotency).
- Every `observation.excerpt`, `cluster.key`, lesson body, and digest line is post-redaction and ≤ 240 chars.
- Every lesson has ≥ 1 detector id and ≥ 1 evidence session id.
- A lesson's state advances beyond `draft` only through an explicit operator command.
- A measurement always records the detector *version* it ran.
- A lesson cannot be reviewed without a populated `backtest` block.
- TWILL never defines an always/never event of its own; `D-10` only reads ICG's exported catalog.
- No lesson, digest, measurement or guard artifact is ever written inside this repository's tree.

### 8.4 Rollback

TWILL writes nothing that needs undoing outside its own tree, which is the point of the
proposes-never-applies rule. Rollback per layer: the state directory can be deleted and rebuilt
(`doctor --rebuild`); a bad lesson is reverted with a normal git revert; a bad *applied* change is
reverted by whoever owns that layer (a hook edit, a memory file, an `AGENTS.md` line), and the
lesson's `routing.applied` field is cleared so measurement does not credit a change that was undone.
A bad TWILL release is rolled back by `git revert` + `systemctl --user restart` of the timers; no
schema migration is destructive (migrations are additive; a downgrade keeps unknown columns).

## 9. Phasing

### Phase 0: Walking Skeleton
**Goal:** prove the wiring — one settled session traverses reader → redactor → store → detector → digest.
**Delivers:** `twill ingest --limit 1`, one observation row, `twill digest --stdout`.
**Does NOT include:** cursors for appended files, ranking, coverage, LLM, measurement, timers.
**Exit criteria:** `twill ingest --limit 1 && twill digest --stdout` prints a digest naming ≥ 1
observation from a real local session, and `sqlite3 ~/.local/state/twill/twill.db 'select count(*) from observation'` > 0.

### Phase 1: Reader, cursor, redactor, store
**Delivers:** full enumeration of both sources, settle window, append/rewrite/truncate handling,
redaction, schema + migrations, `twill status --json`. Also the two extraction side-channels the
later phases depend on: **per-message usage rows** (`session_usage`, for waste attribution) and the
**record-type histogram** (`parse_shape`, for the drift alarm).
**Completion criteria (same commit):** unit tests for parser/cursor/redactor; the idempotency
property test; the secret-fixture test; `twill ingest` over the real local corpus completes within
the §12 budget and `doctor` is clean.
**Does NOT include:** detectors beyond a trivial one (Phase 2), coverage (Phase 3).

### Phase 2: Detectors + digest
**Delivers:** `D-01` missing binary, `D-02` recurring error signature, `D-03` retry loop, `D-04` hook denial, `D-05` rejected tool call, `D-06` interrupt-then-correction, `D-10` ICG gate gap
(an always/never event from ICG's catalog that executed anyway — reported as a hole in the gate, not
as a TWILL lesson); `twill detect`, `twill digest`; the `org-rule-guard` denial log and, if present,
friction receipts wired as inputs (both written by their owners, only read here — §6.5).

**D-02@1 decision (2026-09-23):** recurrence means a normalized error signature appearing in at least
`N = 2` distinct sessions inside the active trailing window. The detector considers only `run_failed`
and `tool_error` observations with a non-empty `signature` and `sig_hash`, groups by
`(sig_hash, signature)`, and orders its output by distinct sessions descending, then latest
`last_seen` descending, with `key` as a deterministic tie-breaker. Persisted clusters retain
`score = 0.0`; the Phase 3 ranker consumes these counts and timestamps for global ranking.


**Digest shape, from the first version:** every line carries the exact command that re-derives it;
the report is a week-over-week diff (new / worsening / improving / gone) rather than a standing top
ten; and a clean week says so explicitly and lists the detectors that ran, so silence is never
ambiguous between "nothing found" and "nothing ran".

**Dead-man's switch:** `doctor` fails when 24 h pass with transcript files arriving and zero
observations ingested, or when `parse_shape` drifts beyond tolerance against its trailing median.
**Completion criteria:** each detector has a fixture-driven test with a known expected count; the
digest for the last 30 days reproduces the three known-true findings from the 2026-09-19 probe
(`sqlite3` 1,096 / `bf` 697 / `go` 632 sessions, ±5% for parser differences) — a real regression oracle.
**Does NOT include:** ranking against existing rules, lessons, LLM.

### Phase 3: Rule corpus + ranking + coverage
**Delivers:** `rulecorpus` indexing of MEMORY.md + leaves, CLAUDE.md, repo `AGENTS.md`, skills;
coverage matching; scoring; `D-07` rediscovery, `D-08` stale-rule (a rule naming a binary absent
from PATH or a retired host), `D-09` unread rule docs. Plus three ranking inputs that decide what
a human actually reads:

- **Waste attribution** — `session_usage` joined to clusters, so the digest ranks by estimated
  tokens and dollars burned rather than by raw count. Attribution across a session that hit several
  frictions is proportional and is labelled an estimate wherever it is shown.
- **Change-point detection** (`trend`) — weekly rates per signature in `cluster_week`, flagging new
  and accelerating friction against its own trailing band. **EWMA, not CUSUM** — the decision is
  made here so an implementer does not have to: CUSUM needs a tuned reference shift per signature,
  and with weekly buckets over a corpus this size an exponentially-weighted mean plus a band is
  both sufficient and inspectable. Chronic-but-flat problems stop crowding out emerging ones. Needs
  ~6 weeks of history before it reports, and says so until then.
  The Phase 2 digest already compares this week against last week; that stays a two-window
  comparison computed at render time, while `cluster_week` is the durable series statistics run
  over. The overlap is deliberate — the digest must work in week two, long before a trailing band
  means anything.
- **Rule earnings & decay report** (`twill rules`) — the inverted view: per existing rule, the
  clusters it covers, whether those recurrences went up or down, when it was last read, and a
  deletion candidate list. This is the only output that *shrinks* the context budget, and it is why
  `D-09` exists: 424 of 562 memory files went unread in the month before TWILL was planned.
**Completion criteria:** `twill rank --json` marks a seeded cluster as covered by a seeded rule file
and leaves an uncovered one open; `D-08` finds the known live contradiction (MEMORY.md still tells
agents `bf`, never `br`, while CLAUDE.md made `bead` canonical on 2026-08-14).
**Does NOT include:** any writing of lessons.

### Phase 4: Explain
**Delivers:** bounded prompt builder, `claude -p` invocation with `CLAUDE_CODE_*` unset, strict JSON
schema, validation, draft lesson files, `twill explain --dry-run` (prompt to stdout, no spawn), and
a **backtest block on every draft** — the same detector replayed over the trailing 180 days, so a
reviewer can tell a standing problem from one bad week before spending attention on it. A draft with
no backtest cannot be reviewed; a backtest that finds nothing earlier is shown as such rather than
blocking, because genuinely new friction has no history.
**Completion criteria:** a golden-prompt test (prompt bytes stable for fixed input); schema-invalid
output produces zero lessons and exit 4; `--dry-run` output contains no fixture secret; one real
weekly run produces ≥ 1 reviewable draft lesson.
**Does NOT include:** routing, apply, measurement.

### Phase 5: Route + apply + review states
**Delivers:** routing recommendation per lesson, `twill apply <id>` emitting the exact
`bead create` command for the owning repo (never executing it), state transitions
`accept`/`apply`/`dismiss`, `twill lessons` listing, and the **guard generator**: each lesson ships
the mechanical artifact that would stop it at its recommended layer — an `org-rule-guard`/ICG
matcher fragment, a wrapper-script skeleton, a gate line, an `AGENTS.md` paragraph, or a memory leaf
with frontmatter — written to `guards/<lesson-id>.*` inside TWILL and installed by nobody but a
human. Prose describes a rule; an artifact is one someone can actually adopt in a minute.
**Completion criteria:** state machine tests including the refusal to auto-accept; an applied lesson
records layer + timestamp + bead id; the open-path test proves no write outside TWILL's two trees.
**Does NOT include:** escalation logic.

### Phase 6: Measure, escalate, retire — and self-measurement
**Delivers:** `twill measure` (daily), measurement mirror files, escalation rule (recurrence not down
≥ 50% after 21 days → propose the next-stronger layer), retirement rule (0 occurrences for 90 days
*and* the rule doc unread for 90 days → propose retire, fed by the Phase 3 rule-earnings report),
and the digest's own health line: lessons drafted / accepted / applied / resolved in the last 60 days.
Also **`twill brief <repo>`**: the pull-only pre-flight — what historically bites agents working in
this repo or launch dir, from accepted lessons and open clusters, rendered as plain text a human or
an agent can read on request. Pull, never push: nothing here is injected into a prompt by TWILL.
**Completion criteria:** a simulated series drives escalate and retire deterministically in tests;
the digest names its own zero-output state loudly if no lesson reached `applied` in 60 days.
**Does NOT include:** other hosts, retrieval surface.

### Phase 7: Other hosts
**Delivers:** TWILL's own pull of lab/bench/agent-sandbox transcripts into its state dir (never via
the archive's `mirror/`), host column populated, per-host coverage in `doctor`.
**Completion criteria:** a cross-host detector reports a signature seen on ≥ 2 hosts; pull failures
degrade to "stale host", never to a failed run.
**Does NOT include:** anything pushing to those hosts.

### Phase 8: Cluster-hosted recall service
**Delivers:** the read-only service of §6.6 — a Deployment with an internal poll loop that pulls the
private lessons repository, rebuilds a disposable FTS projection, and answers two queries (by repo,
by error signature) behind a bearer token on the cluster's existing Traefik. Plus the
WARP-compatible export, and the NEEDLE-side call on failure.
**Completion criteria (same commit):** a worker on a host other than codinghome gets an answer for a
repo it has never touched; the projection rebuilds from an empty volume without manual steps; the
image is a pinned semver tag from `containers/twill-serve/VERSION`; manifests are ArgoCD-synced and
the deployment commit is recorded on the owning bead; no `Job`/`CronJob`, no new Tailscale-exposed
Service, no injection anywhere.
**Does NOT include:** ranking or distillation in the cluster — the service serves what codinghome
decided. Gated on Open Questions 3a (which cluster) and 3b (WARP ownership).

## 10. Testing Strategy & Quality Gates

### 10.1 Test Levels

- **Unit:** JSONL parsing per source shape, cursor arithmetic (append/shrink/rewrite), redactor
  patterns, scoring, state machine.
- **Integration:** a fixture corpus of synthetic transcripts (clean, appended-between-runs,
  truncated, rewritten, secret-bearing, injection-bearing) driven through real ingest → detect →
  rank → digest.
- **Scenario:** one test per §5 scenario, including the archive-absent run (Scenario 2) and the
  kill-mid-ingest recovery (Scenario 3).
- **Property:** idempotency — ingest twice, assert identical rows; and monotonicity — a cursor never
  moves backwards except on an identity change.
- **Oracle:** the Phase 2 regression oracle against known real counts (§9, Phase 2).

### 10.2 Quality Gates (Stop-Ship)

- `pytest` green, `ruff` clean.
- **Secret-fixture test is mandatory and non-skippable**; a skipped or xfail'd redaction test fails the build.
- **Open-path test**: the whole test suite runs under an `open()`/`os.open` audit hook; any write
  outside this repository + the state dir + `artifacts_root`, or any read under the transcript
  archive, fails the run.
- **Artifact-containment gate** (three guards, because one is a promise and three are a mechanism):
  the engine repository `.gitignore`s the four artifact paths; the open-path harness fails on an
  artifact write *into* the repository tree; and `twill-ci` asserts the published tree contains no
  lesson, digest, measurement or guard file. The third one is the backstop that catches a run
  configured wrongly on a machine nobody is watching.
- Idempotency property test green.
- `twill doctor` exits 0 on the developer's own machine state before release.
- CI runs as an Argo WorkflowTemplate in `iad-ci` (`twill-ci`); GitHub Actions are forbidden org-wide.

## 11. Security & Threat Model

The attacker is not a person targeting TWILL; it is **the corpus itself**. Transcripts contain
credentials nobody meant to keep and text written by third parties (fetched pages, issue bodies,
tool output) that an agent later read.

| Threat | Vector | Mitigation | Test |
|---|---|---|---|
| Credential promoted into a searchable, prompt-bound artifact | token pasted into a session, captured as an excerpt | redactor before persistence; excerpts ≤ 240 chars; DB never committed, never leaves the host | secret-fixture test across DB, digests, lessons, prompt (§5 Scenario 4) |
| Prompt injection via transcript text | third-party text reaches the Explain prompt | clusters not sessions; excerpts framed as untrusted data; strict output schema; human acceptance gate | injection-bearing fixture asserts no schema deviation and no lesson auto-accepted |
| Poisoned lesson reaching agent context | a wrong lesson routed to CLAUDE.md/memory | TWILL never writes those files; human applies; measurement catches a non-fix | state-machine test refusing auto-accept |
| Content-fence breach | a lesson names a fenced entity (e.g. a third-party vendor feed under a licence that forbids naming it) | fence list checked in the redactor and again at lesson write | fence fixture test |
| Privilege creep | TWILL gaining write access it does not need | no credentials at all; read-only everywhere but two trees | open-path audit test |

**Secrets:** TWILL holds none — no OpenBao path, no token, no kubeconfig. It needs no credential to
do its job, and that is a design requirement rather than an accident. Any credential *observed* in a
transcript is redacted at the boundary; a lesson may record a retrieval path
(`secret/<cluster>/<app>/<key>`) but never a value, and nothing is ever echoed to stdout or a log.

## 12. Performance Budgets

Reference condition: codinghome (Hetzner EX44, NVMe), single process under the user slice with
`MemoryMax=2G`, `CPUQuota=100%`, while a NEEDLE fleet is running.

| Metric | Budget | Reference Condition | How Measured |
|---|---|---|---|
| Hourly ingest wall time | < 120 s p95 | steady state, ~60–120 newly settled sessions | `status.json` duration field, 7-day p95 |
| Single 10 MB session parse | < 5 s | cold page cache | `twill ingest --file <path> --time` |
| Peak RSS | < 500 MB | any verb | `systemd-run --scope` + `/proc/self/status` VmHWM in the test harness |
| Detect pass (all detectors, 30-day window) | < 20 s | ~1M observations | `twill detect --time` |
| DB size | < 2 GB at 12 months | 180-day retention, codinghome only | `du` recorded weekly in `status.json` |
| Explain weekly cost | ≤ 1 `claude -p` call, ≤ 64 KB prompt | top-10 clusters | prompt byte count asserted in the golden-prompt test |

A budget miss is a bug, not a tuning opportunity: the box has already been taken down by agents
consuming it (six OOM crashes in August), so exceeding RSS or wall time means the run aborts and
`doctor` reports it rather than degrading the host.

## 13. Operations (Deploy, Migration, Monitoring)

### 13.1 Deployment & Configuration

Install: `make install` symlinks `twill` into `~/.local/bin` and installs three systemd `--user` units
from `systemd/`. No container, no cluster — the data is host-local and so is the tool.

| Unit | Cadence | Verb |
|---|---|---|
| `twill-ingest.timer` | hourly | `twill ingest && twill detect` |
| `twill-measure.timer` | daily 06:00 | `twill measure && twill prune` |
| `twill-digest.timer` | weekly Mon 08:00 | `twill rank && twill explain && twill digest` |

Config: `~/.config/twill/config.toml` (settle window, retention, top-K, source globs, fence list,
model). Every value has a working default and a missing config file is not an error — **with one
exception: `artifacts_root`**, which has no default and must resolve outside this repository's tree
(§3). An unset or in-tree value aborts at startup rather than writing a lesson somewhere publishable. Non-interactive by
construction — all verbs are idempotent, `--json` everywhere, no prompts. If TWILL ever runs in a
cluster it is a Deployment with an internal loop; `kind: Job`/`kind: CronJob` are forbidden.

### 13.2 Migration

N/A — greenfield. Two explicit compatibility stances: TWILL does **not** import `graph.db` or
`episodes/` (independence, §2), and it starts from whatever transcripts exist on disk at first run,
accepting that history before that point is simply out of scope.

### 13.3 Monitoring & Health

`twill doctor` is the single health entry point: DB integrity, schema version, stale timers
(`status.json` age > 3× interval), cursor anomalies (`parse_errors`, `path_missing`), free disk,
rule-corpus staleness, detector self-test, which optional inputs of §6.5 are live, and the two
silent-death alarms — the dead-man's switch (transcripts arriving, zero observations for 24 h) and
`parse_shape` drift against its trailing median. Those two exist because a distiller that quietly
stops extracting looks exactly like a quiet week, and that is how this class of pipeline dies. Exit 0 healthy, 1 degraded, 2 broken. `status.json`
carries last-success timestamps per stage for the existing lab-health collector to pick up later
(allowlisted numeric fields only). The weekly digest doubles as the human health signal: if it ever
reports zero clusters *and* zero lessons for two consecutive weeks, TWILL itself is broken or the
environment is genuinely clean, and `doctor` is expected to say which.

## 14. API / Interface Design

```
twill ingest   [--limit N] [--file PATH] [--settle 2h] [--json]
twill detect   [--window 30d] [--detector D-01] [--json]
twill rank     [--top 10] [--json]
twill explain  [--top 10] [--dry-run] [--model MODEL] [--json]
twill digest   [--week YYYY-Www] [--stdout]
twill lessons  [--state draft|accepted|applied|resolved|escalated|retired] [--json]
twill rules    [--unread-days 90] [--deletion-candidates] [--json]   # rule earnings & decay
twill trend    [--detector D-02] [--weeks 12] [--new-only] [--json]  # change-point view
twill brief    <repo|launch-dir> [--top 10] [--json]                 # pull-only pre-flight
twill accept <id> | dismiss <id> --reason TEXT | apply <id> --layer LAYER [--bead ID] [--emit-guard]
twill measure  [--lesson ID] [--json]
twill prune    [--older-than 180d]
twill status   [--json]
twill doctor   [--rebuild] [--rescan-redaction] [--json]
```

Machine surface: every read verb accepts `--json` and emits a single JSON object with
`{schema_version, generated_at, data, warnings[]}`. Human surface: tables on stdout, warnings on stderr.

**Error contract:** `0` success · `1` runtime error (message on stderr, no partial commit) ·
`2` usage error · `3` lock held (`lock held by pid N since T`) · `4` validation failure (schema,
redaction, or detector self-test). Errors never print a credential-shaped value, and `--json` errors
are emitted as `{"error": {"code": N, "message": "...", "hint": "..."}}` so a caller never parses prose.

## 15. Risk Register & Plan B

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | TWILL becomes another designed-but-dead pipeline (MANA, Reflect, the 09-01 learning beads still 0/7 landed) | High | High | Phase 6 self-measurement: the digest reports its own lessons-applied count and says so loudly at zero; Phase 2 ships value with no LLM at all |
| R2 | Digest noise — hundreds of clusters, none actionable | Medium | High | minimum session thresholds, top-K cap, coverage suppression, and dismissal with a reason that suppresses the cluster permanently |
| R3 | A secret reaches a committed lesson | Low | Severe | redactor at the persistence boundary, mandatory fixture test, DB never committed, ≤240-char excerpts |
| R4 | Explain drafts confident nonsense from thin evidence | Medium | Medium | evidence ids required, schema validation, human acceptance, and measurement that exposes a non-fix within 21 days |
| R5 | Parser drift as Claude Code / Codex transcript shapes change | Medium | Medium | per-source parser with fixture corpus; `parse_errors` surfaced by `doctor`; the Phase 2 oracle catches silent extraction loss |
| R6 | Ingest competes with the NEEDLE fleet for IO/memory | Low | High | single-process sequential parse, RSS budget, abort-on-miss, hourly not continuous |

**Plan B:** if the Explain step proves low-value — hallucinated or generic lessons that never reach
`applied` — drop it and keep TWILL deterministic: Detect + Rank + digest, with lessons written by
hand from the ranked evidence. The 2026-09-19 probe is the existence proof that the deterministic half
already produces true, actionable findings (three missing binaries across 2,400+ sessions, 75% of
memory files unread in a month) without a model in the loop. The reverse fallback also exists: if
detectors prove too noisy to rank mechanically, Explain can be pointed at raw cluster dumps for
triage-only summarization while routing stays manual.

## 16. Open Questions

1. **Where does the `org-rule-guard` denial log live, and who owns it?** `~/.claude/hooks/` alongside
   the hook, or the `utilities` repo where `agent-secrets` already lives. Owner: operator.
   Resolve by: Phase 2. Impact if wrong: a second migration of the log path and a gap in `D-04` history.
2. **Which model and what weekly budget for Explain?** Owner: operator. Resolve by: Phase 4.
   Impact if wrong: either cost creep or lessons too thin to accept.
3. **~~Do accepted lessons feed WARP and/or NEEDLE's PromptBuilder~~** — the serving half is
   **decided (2026-09-21)**: a cluster-hosted read-only service, git as transport, WARP-compatible
   export, reactive queries from NEEDLE (§6.6). Two sub-forks remain: **(3a) which cluster** —
   ardenone-cluster is proposed, on the strength of the `git-activity-exporter` precedent, Traefik
   with existing auth middleware, and `needle-dashboard` already living there; and **(3b) does WARP
   subsume the service** once its recall surface exists, or does this stay TWILL's? Owner: operator.
   Resolve by: Phase 8 start. Impact if wrong: a second lesson store to reconcile, or a service on a
   cluster that cannot reach the lessons repository.
4. **How are other hosts' transcripts pulled in Phase 7** — SSH pull by TWILL, or a host-local TWILL
   per box reporting up? Owner: operator. Resolve by: Phase 7. Impact if wrong: plaintext copies of
   other hosts' transcripts land on codinghome, which is exactly the exposure §7.2 is trying to bound.
5. **Is 180-day observation retention enough** to see annual or quarterly patterns, given the DB-size
   budget? Owner: operator. Resolve by: Phase 6. Impact if wrong: seasonal recurrences look like new
   findings every cycle.
6. **Should TWILL detect fabricated work** (a bead closed with no artifact, the "fake-done" taxonomy)?
   It is the highest-value detector class and the most likely to be wrong about a human's intent.
   Owner: operator. Resolve by: Phase 6. Impact if wrong: false accusations in a digest a human reads.
7. **Who owns and installs the friction receipt hook**, and where does it write? It is a `SessionEnd`
   hook outside TWILL's trees, so it needs a home (`utilities`, alongside `agent-secrets`, is the
   obvious candidate). Owner: operator. Resolve by: Phase 2. Impact if wrong: detection stays
   inference-based and `D-04`/`D-06` keep a blind spot.
8. **In what form does ICG export its always/never catalog** for `D-10` to read — a versioned JSON
   file in the ICG repo, a `icg catalog --json` command, or the rule packs parsed directly? Owner:
   operator, with ICG. Resolve by: Phase 2. Impact if wrong: TWILL ends up parsing rule packs it
   does not own, which breaks the moment ICG refactors them.

## 17. Revision History

| Date | Change | Author |
|---|---|---|
| 2026-09-19 | Initial draft from brief; name, independence, and raw-transcript input decided in session. | plan-author |
| 2026-09-19 | Adopted 9 of 10 `plan-idea-gen` finalists into the phases that own them (backtest P4, change-point + waste + rule-earnings P3, guard generator P5, reproduction-first digest + dead-man's switch P2, usage/shape extraction P1, `twill brief` P6, receipts as an optional input §6.5). The always/never event catalog was routed to **ICG** instead of being adopted here; `D-10` consumes it. Open Questions 7–8 added. | plan-idea-gen (bead twill-502355a3) |
