# Plan review: dual-agent learning cycle

**Verdict: READY.** Improvement plan, reviewed inline using plan-review 2.1.
Scope: `learning-cycle.md`; owner `twill-bff7b510`. Implementation and
deployment still require the plan's actual release gates.

The first shipping unit is an opted-in local TWILL controller. It preserves
the old pipeline and avoids a dependency on an unfinished NEEDLE rewrite.
The independent oracle is deliberately limited to text decisions/action
plans; live code execution and fleet rollout remain outside this release.
The user's 2026-11-11 instruction authorizes implementation and deployment;
no unresolved operator decision is required for the nominated local scope.

## Decision ledger

| Fork | Resolution and enforcement |
|---|---|
| Runtime/deployment | Python stdlib and installed systemd user service; existing install path, no new cluster service |
| Authority | Explicit exact-target policy and fixed admitted suites; no model-controlled policy or oracle |
| Evidence/storage | Versioned external private JSON, immutable revisions and hashed report inventory; legacy lesson acceptance unchanged |
| Promotion | Both providers must improve, every candidate assertion must pass, missing metadata/errors never promote |
| Drift | Bind source, suite, actual scoped guidance, CLI/model and implementation hashes; stale exposure is removed/suspended |
| Recovery | Durable intent before writes; recheck admission on resume; remove exact owned block and preserve surrounding edits |
| Budget | One experiment per cycle, bounded invocations and monotonic wall deadline; process group termination |
| Delivery boundary | Nominated global files are intentionally local configuration with receipt-based rollback; private evidence commits push only origin/main |

No high-stakes open fork remains. Automatic repository-owned instruction
maintenance would additionally need that repository's ordinary bead/commit
delivery; it is not silently enrolled by the local deployment.

## Implementer dry run

Phase 0 reads native CLI help, probes real instruction loading, then runs the
fixed paired suite. Claude requires an explicit instruction-file manifest in
constrained mode; Codex's native AGENTS discovery is verified. The adapter
records that difference rather than claiming identical discovery.
Phase 1 records an intent, modifies only exact managed sections, and tests
revocation, interrupted writes, missing final newline and edited ownership.
Phase 2 installs the maintained CLI/units, enrolls the existing local files,
and verifies a real scheduled cycle and private evidence publication.

## Reality and implementation review

Verified existing installed TWILL symlink, Claude/Codex CLIs, private artifact
checkout, native instruction probes and Codex write denial. The stalled
pipeline needed forward migrations and query/reader fixes, not a new archive.

An independent code review found six consequential implementation defects:
missing deployed context, stale crash admission, absent-selector binding,
uncaught drift, newline-sensitive removal and multiplying per-cycle budgets.
The controller now freezes actual context, rechecks admission, requires an
explicit selector, fingerprints exposure inputs, removes complete owned text,
and permits one experiment per cycle. Regression tests cover these failures.
The first three-case live rehearsal held rather than promoting on formatting
or context-verification errors; the final rerun is a release gate.

## Safety caps and structural sweep

C1–C10 pass for the plan: observable lifecycle acceptance; reversible owned
edits; real native dependency in Phase 0; concrete early decisions; grounded
reuse claims; qualified legacy boundaries; private/redacted inputs; one local
improvement unit; independent scratch oracle; compact navigable plan.
Applicable scope, acceptance, architecture, edge cases, phasing, testing,
security, budget, operations, CLI and recovery categories are present.
Cluster/broker migration and HTTP API controls are inapplicable to this local
CLI release. The efficacy claim remains bounded to admitted scenarios;
longitudinal improvement requires subsequent eligible real tasks.

**Next action:** complete the full release gate and live lifecycle rehearsal,
then enable the installed timer and record the delivered revisions on the owner.
