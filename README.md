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
| **Explain** | One bounded, schema-validated `claude -p` pass writes up the top clusters as draft lessons. Never runs on raw sessions |
| **Apply** | Route each lesson to the strongest layer that would actually stop it: environment fix > hook/gate > wrapper > skill > `AGENTS.md` > memory > retrieval-only. TWILL emits the change; a human applies it |
| **Measure** | Every lesson carries the detector that found it. Re-run it: if the problem did not stop, escalate a layer; if it has not appeared in 90 days, propose retiring the rule |

## What it is not

- **Not a transcript archive.** `agent-transcript-archive` owns capture and durability
  (ARMOR→B2, restic). TWILL copies and retains nothing.
- **Not part of that pipeline.** TWILL has its own repo, its own schedule, its own cursor and its
  own state. Ingest never calls it, it never reads `graph.db`, and it keeps working when the
  archive is stopped or deleted.
- **Not an unsupervised writer.** It never edits CLAUDE.md, memory, hooks, skills, or another
  repository. It proposes; a human accepts. (NEEDLE's Reflect strand is the cautionary precedent:
  left to write into prompts by itself, it reinforced `Read -> File read successfully` 10,930 times.)

## Layout

```
docs/notes/     design decisions, detector catalog, redaction policy
docs/research/  prior art and source material
docs/plan/      plan.md — the complete plan (start here)
lessons/        the durable product: one reviewed lesson per file
measurements/   per-lesson recurrence series, mirrored from the DB
digests/        weekly reports
systemd/        the three user timers (ingest hourly, measure daily, digest weekly)
```

State lives outside the repo in `~/.local/state/twill/` (mode 600). The database is derived and
disposable — it is never committed and never leaves the host, for the same reason `graph.db` is
not committed: an index of transcript text is *designed* to be surfaced into future prompts, which
makes a leaked secret in it worse than one sitting inert in a transcript.

## Status

Plan complete (`docs/plan/plan.md`, 36/36 on the completeness bar). No code yet — Phase 0 is a
walking skeleton: one settled session through reader → redactor → store → detector → digest.
