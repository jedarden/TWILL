# TWILL Ideas Ledger

Every idea ever generated for TWILL, with its verdict. Future ideation runs dedupe
against this file. Killed ideas may be resurrected only by stating why the fatal
objection no longer holds.

Anchor plan: `docs/plan/plan.md`.

---

## Run 2026-09-19 — plan-idea-gen (pool 102 → 10 finalists)

**Goal:** detect recurring friction from raw agent transcripts, write it up as
evidence-backed lessons, route each to the layer that stops it, and measure that it
stopped.

**Kill criteria (from plan §2/§3):** independent of `agent-transcript-archive`, never
reads `graph.db` · read-only outside `~/TWILL` + `~/.local/state/twill` · no credential
values anywhere · TWILL proposes, a human accepts and applies · stdlib-only core ·
single-writer, hourly ingest < 120 s, RSS < 500 MB, ≤ 1 `claude -p`/week · no
`kind: Job`/`CronJob`, no GitHub Actions · v1 codinghome only · not a dashboard · no
whole-session LLM summarization.

### Clusters

C1 evidence quality · C2 detection science · C3 prevention & enforcement ·
C4 economics & prioritization · C5 rule lifecycle & context hygiene ·
C6 reviewer ergonomics · C7 minimalism · C8 agent-facing delivery ·
C9 TWILL's own reliability · C10 source enrichment · C11 outputs beyond the digest

### Lens 1 — invert the problem

| # | Idea | Cluster | Verdict |
|---|---|---|---|
| 1 | **Success-path detector** — sessions that hit a known signature and recovered fast; extract the recovery command as the canonical remedy | C1 | CUT (triage) — valuable but subsumed by the episode-shaped evidence a lesson already carries |
| 2 | **Rule earnings report** — start from every rule doc, compute prevented-recurrence per rule; context-budget accounting | C5 | **FINALIST** |
| 3 | **Friction receipt hook** — a SessionEnd hook writes a structured receipt (rules consulted, denials, unresolved errors) so TWILL parses less and infers less | C10 | **FINALIST** |
| 4 | **Counterfactual pairing** — same bead attempted twice, one green one red; diff the command sequences to isolate the deciding step | C1 | SURVIVES kill pass, not selected — strongest objection: requires bead-linked sessions, thin on codinghome-only data |
| 5 | **Lesson backtest** — replay a draft lesson's detector over the prior 180 days before accepting it | C1 | **FINALIST** |
| 6 | **Rule decay sweep** — rules with zero reads and zero related observations proposed for deletion | C5 | MERGED into #2 |
| 7 | **Negative-evidence check** — search for sessions where the "wrong" behaviour succeeded anyway, to block over-generalized rules | C1 | SURVIVES, not selected — objection: absence of failure is weak evidence of safety |
| 8 | **Pre-flight predictor** — at session start, the top 3 frictions historically hit in this launch dir | C8 | MERGED into #17 |
| 9 | **Per-repo digest appendix** — generate friction summaries for the repos where it happens, not one global digest | C8 | CUT — routing already decides per-repo destinations; duplicate delivery path |
| 10 | **Corrections-first corpus** — treat every human correction as the ground-truth label and build backwards from user turns | C1 | CUT — already the D-05/D-06 detector premise in the plan |
| 11 | **Documentation-to-enforcement gap** — count how many recurrences were *already covered* by a rule | C4 | MERGED into #2 |
| 12 | **Idle-capacity detector** — sessions that waited/polled needlessly (sleep loops, repeated status checks) | C2 | SURVIVES, not selected — objection: wall-clock waste is real but rarely actionable as a rule |
| 13 | **Inverted retention** — keep only observations that ever fed a cluster, prune the rest early | C9 | KILL — premature optimization; today's noise is tomorrow's cluster, and §12 already budgets the DB |

### Lens 2 — adjacent-domain transplant

| # | Idea | Cluster | Verdict |
|---|---|---|---|
| 14 | **Change-point detection** (EWMA/CUSUM) on signature rates — alert on *new or accelerating* friction, not just volume | C2 | **FINALIST** |
| 15 | **Pareto digest** — rank by estimated wasted tokens; show the 20% of signatures causing 80% of the waste | C4 | MERGED into #21 |
| 16 | **Poka-yoke generator** — emit the mechanical guard (hook matcher, wrapper, alias) per lesson, not prose | C3 | **FINALIST** |
| 17 | **Detectors-as-code** — declarative YAML detectors with fixture tests, reviewable and versioned | C2 | SURVIVES, not selected — objection: the plan already versions detectors; YAML adds a schema to maintain |
| 18 | **Never-events lane** — force-push, kubectl mutate, secret in argv: counted separately, zero tolerance, always top of digest | C2 | **FINALIST** |
| 19 | **5-whys chain builder** — assemble the causal chain from observations (command → error → retry → different command → success) | C1 | SURVIVES, not selected — objection: chain assembly is inference dressed as extraction |
| 20 | **Spaced resurfacing** — surface a rule just before the window it is historically broken in | C5 | KILL — cute, but it is injection-by-schedule with no evidence the timing correlation is real |
| 21 | **Waste accounting** — estimate tokens/$ burned per cluster from usage rows; rank the digest by money | C4 | **FINALIST** |
| 22 | **Precedent graph** — lessons cite prior lessons; families of related friction become visible | C5 | CUT — value arrives only after ~50 lessons exist |
| 23 | **Film-study export** — a redacted annotated replay of one exemplar session per lesson | C6 | KILL — brushes the no-whole-session-content rule and balloons the redaction surface |
| 24 | **Severity levels** — lint-style error/warn/info driving routing layer and digest placement | C2 | MERGED into #18 |
| 25 | **Controlled vocabulary** — a fixed taxonomy of friction kinds that detectors must map into | C2 | SURVIVES, not selected — objection: premature taxonomy; let the first 20 lessons name their own kinds |
| 26 | **Environment SLO** — a weekly friction budget; breach escalates to a stop-the-line recommendation | C4 | CUT — a number nobody is accountable to is a number nobody reads |

### Lens 3 — remove a constraint

| # | Idea | Cluster | Verdict |
|---|---|---|---|
| 27 | **Auto-apply reversible layers** with automatic revert if measurement does not improve | C3 | KILL — violates the human-accepts hard requirement; this is exactly Reflect's failure mode with a rollback bolted on |
| 28 | **Auto-generated gate patches** for the owning repo's definition-of-done | C3 | CUT — subsumed by #16, which emits the artifact without executing anything |
| 29 | **Per-session Explain for high-severity events**, budget-bounded instead of schedule-bounded | C2 | SURVIVES, not selected — objection: breaks the ≤1 call/week budget and invites cost creep |
| 30 | **Multi-host from day one** via read-only SSH pull | C10 | CUT — already Phase 7, and Open Question 4 must be answered first |
| 31 | **Embedding clustering** (sqlite-vec) to catch paraphrased friction | C2 | KILL — violates stdlib-only core; normalized signatures already cluster the machine-generated text that dominates the corpus |
| 32 | **Full-session Explain for one exemplar per cluster** | C2 | KILL — explicit non-goal; the injection surface is the whole reason it is a non-goal |
| 33 | **Local web review UI** | C6 | KILL — "not a dashboard" is a stated non-goal |
| 34 | **Lessons as an MCP server** for any agent to query | C8 | CUT — duplicates Phase 8, which is gated on Open Question 3 |
| 35 | **Write a generated rules file** into `~/.claude/rules/` | C3 | KILL — violates the read-only-outside-two-trees invariant, which is tested for |
| 36 | **`--forensic` longer excerpts** for local review only | C6 | SURVIVES, not selected — objection: every widening of the excerpt cap widens the leak surface |
| 37 | **Auto bead creation across repos** | C3 | KILL — violates emits-never-executes, and cross-repo bead ownership rules are strict |
| 38 | **Real-time hook blocking** of a known-bad command once a lesson is applied at the hook layer | C3 | MERGED into #16 (the guard is emitted; the human installs it) |
| 39 | **Live inotify streaming** for never-events | C10 | KILL — conflicts with the settle window and buys minutes on a weekly cadence |

### Lens 4 — 10x cheaper / simpler

| # | Idea | Cluster | Verdict |
|---|---|---|---|
| 40 | **Single-file `twill.py`** — no package, minimal maintenance floor | C7 | SURVIVES, not selected — objection: ten detectors and a state machine outgrow one file fast |
| 41 | **JSONL + grep instead of SQLite** for v1 | C7 | KILL — detectors are indexed point queries; this trades a week of work for a month of re-scans |
| 42 | **Detector-as-shell-one-liner registry** runnable by hand | C7 | MERGED into #43 |
| 43 | **Reproduction-first digest** — every line carries the exact command to re-derive it, plus a week-over-week diff view | C6 | **FINALIST** |
| 44 | **Piggyback the `fewer-permission-prompts` transcript scan** instead of writing a parser | C7 | CUT — that scan is allowlist-shaped, not observation-shaped |
| 45 | **Zero-parse `rg` pattern pack**, recomputed each run, no cursor | C7 | CUT — loses turn order, which is the correction signal TWILL exists for |
| 46 | **Manual invocation only** until value is proven; no timers in v1 | C7 | KILL — an unscheduled distiller is the dead-pipeline failure mode (R1) by construction |
| 47 | **Beads as the lesson store** instead of files + state machine | C7 | CUT — beads are work items; a lesson outlives the work and must be diffable |
| 48 | **Reuse an existing signature-normalization scheme** rather than inventing one | C2 | ACCEPTED as implementation guidance (Phase 2), not a separate idea |
| 49 | **Ship three proven detectors in a day** (missing binary, hook denial, repeated signature) | C7 | MERGED into plan Phase 2 |
| 50 | **Digest as diff only** — show what changed since last week | C6 | MERGED into #43 |
| 51 | **No Explain step in v1** — operator writes the lesson from the evidence block | C7 | CUT — already the plan's Plan B (§15) |
| 52 | **Emit a skill file instead of a routing engine** | C3 | CUT — skills are one routing layer of seven, not a replacement for routing |

### Lens 5 — power-user workflow

| # | Idea | Cluster | Verdict |
|---|---|---|---|
| 53 | **`twill why <command>`** — what history says about running this | C8 | MERGED into #17 |
| 54 | **`twill blame <path>`** — friction associated with a file or repo dir | C8 | SURVIVES, not selected — objection: thin until the corpus spans more repos |
| 55 | **Review TUI** with keyboard accept/dismiss/escalate and evidence preview | C6 | CUT — CLI verbs plus an editor already do this; TUI is polish before product |
| 56 | **Operator-defined detectors** in config, first-class and measurable | C2 | SURVIVES, not selected — objection: arbitrary SQL from config is a footgun on a shared DB |
| 57 | **`twill diff-weeks`** | C6 | MERGED into #43 |
| 58 | **Per-layer lesson templates** (hook snippet, AGENTS.md paragraph, memory leaf with frontmatter) | C3 | MERGED into #16 |
| 59 | **One-command memory-layer apply** — writes the leaf and the hub line when the operator runs it | C3 | KILL — writes outside the two trees even when operator-initiated; the invariant has no exceptions |
| 60 | **Weekly brief to Telegram** via the existing relay | C11 | CUT — delivery polish; the digest file is the product |
| 61 | **`twill watch`** — flag friction in your own live session | C8 | KILL — needs the settle window violated and a second read path for active files |
| 62 | **Lesson↔bead cross-linking** so worker retries see the relevant lesson | C8 | SURVIVES, not selected — objection: overlaps NEEDLE's prior-fixes path, which already exists |
| 63 | **Git-tag time anchors** (`--since 'last release'`) | C6 | CUT — nice, trivial, not idea-sized |
| 64 | **Local-model Explain** | C7 | KILL — no local model exists on this host |
| 65 | **Shell preexec warning for the human** typing a known-friction command | C3 | SURVIVES, not selected — objection: installs into the operator's shell, outside TWILL's trees |

### Lens 6 — failure-mode / reliability-driven

| # | Idea | Cluster | Verdict |
|---|---|---|---|
| 66 | **Per-detector golden counts** in CI against a fixture corpus | C9 | MERGED into plan Phase 2 oracle |
| 67 | **Canary corpus** — synthetic known-friction sessions asserted found every run | C9 | MERGED into #68 |
| 68 | **Dead-man's switch + parser-drift alarm** — zero observations in 24 h, or a shifted record-type distribution, fails `doctor` loudly | C9 | **FINALIST** |
| 69 | **Record-type distribution tracking** per source | C9 | MERGED into #68 |
| 70 | **Parse quarantine list** with reasons instead of silent retry | C9 | SURVIVES, not selected — objection: `parse_errors` plus `doctor` already covers the visible case |
| 71 | **Idempotency fuzzer** — random truncate/append/rewrite against invariants | C9 | SURVIVES, not selected — objection: the plan's property test covers the common shapes |
| 72 | **Redaction fuzzing** with generated secret-shaped strings | C9 | SURVIVES, not selected — objection: entropy-generated fixtures drift from real credential shapes |
| 73 | **Series-break marker** when a detector version changes mid-measurement | C9 | MERGED into plan EC-12 |
| 74 | **Timezone/clock-skew guard** — observations carry local and UTC | C9 | ACCEPTED as implementation guidance (Phase 1) |
| 75 | **Backpressure** — newest-first processing with a recorded backlog depth | C9 | SURVIVES, not selected — objection: only bites above the §12 budget, which aborts anyway |
| 76 | **Poison-pill guard** — cap observations per session | C9 | ACCEPTED as implementation guidance (Phase 1) |
| 77 | **Runtime invariant audit**, not only in tests | C9 | SURVIVES, not selected — objection: the audit hook costs per-open overhead in the hot path |
| 78 | **Scheduled recovery drill** — rebuild into a temp dir and compare | C9 | SURVIVES, not selected — objection: `doctor --rebuild` is already exercised; drill it quarterly by hand |

### Lens 7 — novice user / intuitiveness

| # | Idea | Cluster | Verdict |
|---|---|---|---|
| 79 | **Reproduction command on every digest line** | C6 | MERGED into #43 |
| 80 | **Plain-language two-sentence summary** mandatory at the top of every lesson | C6 | ACCEPTED as implementation guidance (Phase 4 schema) |
| 81 | **`twill brief <repo>`** — the top frictions that bite agents in this repo, pulled on demand | C8 | **FINALIST** |
| 82 | **Plain-text digest** that reads well in a terminal and inside a bead | C6 | ACCEPTED as implementation guidance |
| 83 | **Glossary auto-linking** to plan §4 | C6 | CUT — docs polish |
| 84 | **One-page mechanism diagram** in `docs/notes/` | C6 | ACCEPTED as a docs task, not an idea |
| 85 | **`twill doctor --explain`** — what each check means and how to fix it | C6 | SURVIVES, not selected — objection: small, safe, and easily folded into `doctor` later |
| 86 | **Fixed-menu dismissal reasons** so suppression is auditable and countable | C6 | SURVIVES, not selected — objection: free text plus a count gets 90% of this |
| 87 | **First-run preview wizard** — sources, counts, and what a first digest would say, writing nothing | C6 | SURVIVES, not selected — objection: `--dry-run` on existing verbs covers it |
| 88 | **Mechanical confidence label** (session count, host spread, time span) rather than model self-assessment | C1 | SURVIVES, not selected — objection: the evidence block already shows these three numbers |
| 89 | **Show the counterexample** — a session where the correct behaviour was followed | C1 | MERGED into #7 |
| 90 | **Empty-state honesty** — "clean week; here is what was checked" | C6 | MERGED into #43 |

### Lens 8 — what a competitor ships first

| # | Idea | Cluster | Verdict |
|---|---|---|---|
| 91 | **Top-5 weekly brief** as the habit-forming minimum product | C11 | MERGED into plan digest |
| 92 | **Cross-repo friction leaderboard** — which launch dirs burn the most agent time | C4 | SURVIVES, not selected — objection: overlaps #21, which ranks by waste directly |
| 93 | **Model/adapter comparison** — friction per bead by model | C4 | SURVIVES, not selected — objection: needs fleet data (Phase 7) and risks confounding by task mix |
| 94 | **Time-to-recovery metric** — median minutes from first error to resolving commit | C4 | SURVIVES, not selected — objection: attribution of "resolving" is exactly the inference the plan defers |
| 95 | **Environment health score** — one composite number | C4 | KILL — composite scores hide the thing you act on |
| 96 | **Coverage headline** — what fraction of this week's friction was already documented | C4 | MERGED into #2 |
| 97 | **Public writeup pipeline** — anonymized lessons feed the jedarden.com notes backlog | C11 | SURVIVES, not selected — objection: a publishing workflow, not a distiller feature |
| 98 | **Dashboard-site panel** via the existing SSE surface | C11 | KILL — "not a dashboard" |
| 99 | **Lesson skill-pack export** for other repos | C8 | CUT — Phase 8 territory, gated on Open Question 3 |
| 100 | **Frozen benchmark corpus** with expected findings for detector changes | C9 | CUT — duplicates the Phase 2 regression oracle; extend that instead |
| 101 | **Environment changelog** — "this week the environment changed in these ways", derived from applied lessons | C11 | SURVIVES, not selected — objection: derivable from `twill lessons --state applied` on demand |
| 102 | **"What I'd fix first" recommendation** with estimated hours saved | C4 | MERGED into #21 |

### Finalists

| # | Idea | Cluster | Complexity |
|---|---|---|---|
| 5 | Lesson backtest before acceptance | C1 | S |
| 18 | Never-events lane | C2 | S |
| 14 | Change-point detection on signature rates | C2 | M |
| 16 | Poka-yoke guard generator | C3 | M |
| 21 | Waste accounting → Pareto-ranked digest | C4 | M |
| 2 | Rule earnings & decay report | C5 | M |
| 43 | Reproduction-first digest with week diff | C6 | S |
| 68 | Dead-man's switch + parser-drift alarm | C9 | S |
| 3 | Friction receipt hook | C10 | M |
| 81 | `twill brief <repo>` pull-only pre-flight | C8 | M |

**Run stats:** 102 generated · 14 merged as duplicates · 21 cut at triage · 17 killed in
the adversarial pass · 20 survived but were not selected · 10 finalists · 5 absorbed as
implementation guidance into existing phases.
