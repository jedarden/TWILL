# AGENTS.md — TWILL

Rules for any agent working in this repository. The home `CLAUDE.md` still applies; this file wins
where it is more specific.

## This repository is PUBLIC. Its output is not.

**Never commit a lesson, digest, measurement or guard artifact here.** They are distilled from
sessions across every repository on this host, including private ones, and redaction stops
credential values, not business context. They belong under `artifacts_root` — the separate private
repository `TWILL-lessons` (Forgejo only, no mirror, never public).

Three guards enforce this; do not weaken any of them:

1. `lessons/`, `digests/`, `measurements/`, `guards/` are gitignored here.
2. The open-path test harness fails on an artifact write into this tree.
3. `twill-ci` asserts the published checkout contains no artifact file.

`artifacts_root` has **no default**. Unset, or resolving inside this tree, is a startup error — not
a fallback to something convenient.

## Do not describe private systems

This tree is world-readable. Name a private repository if a test or invariant genuinely needs the
path (the independence assertions do), but do not document another private system's architecture,
credentials, topology or incident history here.

## Work tracking

`bead` (bead-rs), prefix `twill`; `.beads/config.json` + `.beads/checkpoint/` is the shape — never
run `bf` against it. Every repository change needs an owning bead; record the actual verification
commands and outcomes on it before closing. Descriptions are immutable, so a rescope is
close-and-recreate, never a note.

## The plan is the decision record

`docs/plan/plan.md` decides every fork inline. Do not write an ADR mid-build and do not re-open a
locked decision in a bead description — if you hit a fork the plan has not made, add it to §16 Open
Questions. A bead is work; an unresolved decision never is.

## Inherited prohibitions that bite here

- No `.github/workflows/*` — CI is an Argo WorkflowTemplate in `iad-ci`.
- No `kind: Job` / `kind: CronJob` — a Deployment with an internal loop if this ever runs in-cluster.
- No `:latest` or bare-SHA image tags; pin a semver from `containers/<name>/VERSION`.
- Never force-push. Push only to Forgejo `origin`.
- Secrets travel by reference: a path, never a value — not in a file, commit, bead, log, or argv.
