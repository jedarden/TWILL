# TWILL dual-agent learning lifecycle
> Evidence-gated instruction maintenance for native Claude and Codex.
**Type:** Improvement
**Status:** Production completion; owning bead `twill-73f5b935` (extends `twill-bff7b510`)
**Last updated:** 2026-11-11

## 1. Mission & North Star

**North Star:** An explicitly enabled local controller creates source-linked lessons, evaluates both native agents, applies useful learned instructions, revalidates them, and
removes harmful or obsolete instructions without changing unrelated rules. Legacy `twill` remains proposes-only by default.

## 2. Non-Goals (Explicit Scope Boundaries)

- No NEEDLE kernel rewrite: extend the running TWILL pipeline; an unfinished rewrite delays the loop.
- No WARP export/recall service or cluster deployment: neither is necessary for this local lifecycle.
- Evaluated agents have no host execution authority. Trusted execution oracles may check generated files inside a pinned isolated container.
- No global system prompt replacement, authority expansion, or edits to human-maintained rule text: lessons supplement existing instructions only inside policy-approved managed
  blocks.
- Unrestricted native coding sessions and new authority layers remain outside the enrolled experiment envelope.
## What We're NOT Changing

Legacy ingest/detect/explain/accept/apply/measure semantics, human acceptance of legacy lessons, archive independence, stdlib core, credential redaction, and external private
artifact containment.

## 3. Hard Requirements (Non-Negotiable)

- `twill-learning` MUST require explicit policy, external `artifacts_root`, and exact target allowlist.
- Autonomous promotion MUST require `auto_apply=true`, an operator-preconfigured evaluation suite, source-linked evidence, separate passing Claude/Codex results, and fresh matching
  input hashes.
- The controller MUST NOT rewrite human blocks, expand authority, accept legacy lessons, or follow instructions embedded in source transcripts. Missing evaluation means
  `needs_evaluation`.
- Failure, timeout, model drift, unsupported oracle or unknown adherence MUST NOT promote. Inferred fixes additionally require conclusive improvement; explicit human directives may be admitted when both baselines already pass.
- MUST preserve all legacy audit guarantees; learning-specific write/subprocess exceptions MUST validate exact policy paths, managed block ownership, and native adapter arguments
  independently.
- Forbidden: third-party core dependencies, shell command construction, unbounded trials, raw private artifacts in this public repository, and any archive-derived index dependency.
### 3.1 Normative Language

MUST, MUST NOT, SHOULD and MAY follow RFC 2119.

## 4. Glossary

- **Lesson version:** immutable instruction text plus source references and content hashes.
- **Suite:** preconfigured train, hidden holdout and counterexample cases with deterministic assertions.
- **Exposure:** one owned instruction block installed for one exact approved target/provider.
- **Promotion:** all required provider experiments pass, enabling a policy-authorized exposure. Inferred fixes additionally require shared measured improvement; source-verified human directives require adherence without baseline failure.
- **Invalidation:** retain evidence/history and remove only unchanged owned blocks from every exposure.

## 5. Acceptance Scenarios
### Scenario 1: Autonomous successful lifecycle

Setup: private policy nominates maintained AGENTS.md/CLAUDE.md files and an approved suite; source references establish the lesson; auto-apply is enabled. Action:
create/import, experiment, apply, validate, then invalidate. Expected: both native providers return genuine paired evidence. Pass: candidate repairs at least one baseline error,
passes every required assertion and all counterexamples, both blocks appear, validation records fresh evidence, invalidation removes exactly the owned blocks, a repeated cycle
makes no changes. Fail: fabricated success, one-provider promotion, missing source links, or unrelated bytes changed.
### Scenario 2: Degraded evaluation and unapproved suite

Setup: provider unavailable, timeout, candidate regression, or source lesson has no approved suite. Action: cycle. Expected: failed/inconclusive/needs_evaluation with reason. Pass:
no new exposure, existing exposure handled by §8; bounded execution and distinct provider records. Fail: success inferred from process exit alone, unavailable provider ignored, or
a generated suite self-approved.
### Scenario 3: Recovery, drift and conflicting edits

Setup: crash between two target writes; separately change source/model/lesson; separately edit an owned block manually while preserving unrelated edits. Action: cycle. Expected:
reconcile intent, rerun drifted evidence, remove harmful candidate if owned block still matches. Pass: repeated runs converge, stale evidence cannot promote, unrelated edits
survive, modified owned block reports conflict. Fail: blind whole-file restoration, partial shared promotion, or conflict overwritten.

## Gap Inventory

Existing `twill_app.py` records human acceptance/application; `twill_measure.py` measures recurrence. `Makefile` installs one CLI and ingest/measure/digest timers;
`tests/openpath.py` enforces legacy write/network/import boundaries. Missing: native experiments, policy-controlled exposure/removal, versioned evidence and an independent
scheduled lifecycle. New modules below are implementation work.

### Production completion acceptance

1. Automatically process the stranded real draft from source verification to a disposition; supersede its old unassigned version.
2. Process a real role- and hash-linked human intervention through both native providers on baseline, candidate, holdout and counterexamples. Already-correct baselines justify rejection.
3. Discover future supported interventions from explicitly enrolled settled transcripts. Preserve human answers; abstain across unsupported interactive formats.
4. Run timer-triggered import, paired experiment, application, interval-based fresh validation and automatic source-withdrawal removal, then prove idempotence. Direct mutation commands cannot substitute for a stage.
5. Execute generated code against hidden fixed assertions in a credential-free, network-disabled, read-only pinned container. Runtime absence is unknown, never candidate success.
6. Expose stage counts, pending age, evaluator/runtime failures and stale input/schedule health. Entirely blocked queues must not report healthy merely because a process exited.
7. Observe subsequent eligible source telemetry with explicit coverage and unknowns; terminal replies never prove task success or causal improvement.

## 6. Architecture
### 6.1 Component Overview

`twill-learning`/`twill_learning.py`: policy parsing, CLI, private record store, lifecycle and intents. `twill_experiments.run_experiment`: native Claude/Codex subprocess adapters
and deterministic scoring. Existing `twill_config`, `twill_redactor`, `twill_contract` supply path, redaction and output patterns. New systemd user service/timer invokes cycle;
legacy ingest remains independently scheduled.

`twill_learning_intake` owns trusted evaluator recipes and bounded source discovery; candidate text cannot author expected answers or policy. `twill_execution_oracle` owns hidden Python function assertions and the isolated runner. `twill_learning_health` owns stage health; `twill_learning_impact` reads existing source telemetry without changing it.
### 6.2 Data Flow

Redacted source reference → immutable lesson → approved suite → isolated baseline/candidate trials for each provider → fixed scoring → promotion evidence → managed target blocks →
scheduled paired revalidation → retain or invalidate. Imported legacy lessons retain their original state and ID.
### 6.3 Concurrency / Execution Model

Single writer uses a nonblocking file lock for the private learning store. Provider calls execute serially under per-call and total deadlines; no nested agents. Timer overlap exits
with lock status. Intent journaling plus atomic same-directory replacement makes multi-file exposure recoverable.
### 6.4 Technology Decisions (Why X Over Y)

Python stdlib over a new service reuses TWILL deployment and audits. Private JSON records over new SQL tables keep evidence portable and independent of ingest schema. Managed
markers and provider exposures reuse existing promotion/canary concepts without depending on a NEEDLE kernel rewrite. Native CLIs over API credentials preserve configured provider
authentication and execution identity.

## 7. Data Model
### 7.1 Core Entities

- Policy JSON v1: exact `targets[]` paths/providers, `auto_apply` boolean, approved suite references, pinned provider model IDs, revalidation interval, max calls/trials and
  timeouts; missing fields fail.
- Lesson JSON v1: id/version/text, SHA-256, source refs with hashes, evidence refs, suite hash, state.
- Experiment: id/provider, model and CLI version, baseline/candidate instruction hashes, suite hash, timestamps, per-case outputs/scores, deadlines, outcome and reason; hash
  canonical serialized inputs.
- Exposure: lesson/version/provider/target, marker and owned-content hashes, before/after hashes, last validation and next due; intent: operation ID, planned edits, phase and
  completion record.
### 7.2 Source of Truth & Storage

`<artifacts_root>/learning/` is authoritative private state (configured TWILL-lessons); never this public tree. Immutable versioned evidence is retained; no automatic history
deletion in v1. Policy lives in private local config. Write temp+fsync+replace; keep intent until reconciliation and preserve before/after hashes plus the owned addition for recovery. Target files
are authoritative for conflicts.

## 8. Pre-Flight Safety
### 8.1 Edge Case Catalog

- EC-01: unset policy, unsafe path, symlink target or wrong basename → exit 4 before any write.
- EC-02: source/lesson/suite/model/version/instruction drift → mark stale and schedule fresh paired trials; disable promotion and remove stale exposure before reuse. Never bless
  mismatched evidence.
- EC-03: edited/duplicate/malformed owned marker → conflict, exit 4; preserve file for operator repair.
- EC-04: provider failure or budget exhaustion → inconclusive, no promotion; expired evidence disables exposure until fresh evidence passes. Baseline and candidate use identical
  frozen configuration.
- EC-05: no approved hidden suite → needs_evaluation, no autonomous promotion or auto-generated oracle.
- EC-06: enrolled catalog recipes instantiate independent fixed oracles from verified metadata without per-lesson hand binding; candidate/model-authored oracles remain untrusted.
- EC-07: redundant candidate or inadequate source → terminal rejection. Transport/sandbox setup failures → unknown with retry/attention, never rejection as evidence of harm.
### 8.2 Failure Modes & Recovery

| Failure | Detection | Recovery | Data safety |
|---|---|---|---|
| Crash mid-apply | unfinished intent | finish or revert matching owned edits before new work | compare hashes |
| Candidate harmful | assertion regression | invalidate all unchanged exposures | preserve human edits |
| Private store corrupt | JSON/hash validation | stop mutations; restore verified private backup | never infer success |
| Lock held | nonblocking flock | exit 3; next timer retries | single writer |
### 8.3 Invariants (Must Always Hold)

Only exact approved AGENTS.md/CLAUDE.md paths can change; block content matches stored evidence; shared promotion needs both providers; no source text or candidate can change
policy or oracle.
### 8.4 Rollback

Record original/after hashes, original existence and exact owned addition before apply; do not copy human instruction contents into receipts. Remove/revert only matching managed blocks, rebasing against current surrounding text. Human edits inside a block stop
recovery. No git reset or whole-file backup overwrite; an invalidated lesson cannot reapply without new evidence.

## 9. Phasing
### Phase 0: Walking Skeleton

Real dependency exercised: riskiest native Claude and Codex CLI behavior in a task-specific scratch workspace, on demand without a production event. Deliver minimal create→paired
experiment→private record. Exit criteria: `twill-learning --policy "$LEARNING_POLICY" experiment "$LESSON_ID"` records both actual CLI/model versions and scored baseline/candidate
outputs. Does NOT include real target edits.
### Phase 1: Safe exposure and invalidation

Completion criteria, same commit: §5 scenarios with fake adapters pass, crash/conflict/policy tests pass, live scratch apply/validate/invalidate preserves human text. Does NOT
include scheduled rollout.
### Phase 2: Installation and enabled nominated scope

Completion criteria, same commit: full §10 gates, make install symlinks/units, enable private policy for nominated existing target only after scratch proof, real cycle and no-op
rerun evidence. Does NOT include fleet rollout, broader tasks, universal improvement claims or a new service dependency.

## 10. Testing Strategy & Quality Gates
### 10.1 Test Levels

Unit: policy/path validation, fingerprints, scoring, transitions, block merges. Integration: native adapter argv/status handling, private containment, two-file crash
reconciliation. Scenario: §5.
### Behavioral Regression Tests

Legacy accept/apply remains human-controlled; legacy audit rejects unauthorized writes/subprocesses.
### 10.2 Quality Gates (Stop-Ship)

`scripts/definition-of-done.sh` must pass; report optional tool skips. New safety/lifecycle tests, real dual-provider scratch proof, active symlink/timer checks and private
evidence are release gates.
### 10.3 Test Oracle Access

Operator-configured suite assertions are the oracle; evaluated agents receive prompts and candidate instructions, never expected answers, holdout labels or scoring logic. A
separate author establishes the fixed suite before trials; generated cases require explicit suite admission. Every trial runs baseline/candidate on train+holdout+counterexamples,
at least two repetitions; require improvement on a known baseline failure, every candidate check passing, and zero counterexample regressions for both providers. This demonstrates
bounded task utility, never universal improvement. Command: `twill-learning --policy "$LEARNING_POLICY" experiment "$LESSON_ID"` in scratch; no production dependency.
Cases/assertions and evidence stay private; synthetic safety fixtures public.
### 10.4 Inner-Loop Budget

Per-change check: `timeout 180s env PYTHONPATH=tests python3 -m unittest discover -s tests -p 'test_learning*.py'`. Budget three minutes; timeout fails rather than counts as pass.
Full gate once per release/change set.

Scheduled oracle: `python3 scripts/verify-scheduled-learning.py --scratch /private/scratch --claude-model PIN --codex-model PIN`. It records raw D-Bus timer/service timestamps and linkage, retains units while reading completion evidence, preserves private receipts and cleans up only owned units. Real no-model timer preflight precedes native calls; provider executables are absolute pins.

Execution gate: `TWILL_DOCKER_TEST=1 PYTHONPATH=tests python3 -m unittest test_execution_oracle test_experiments`. Models see tasks/output filenames, never hidden expected values. A root supervisor evaluates generated code in separate uid65534 children without capabilities, network, credentials or project/root writes. Candidate exits cannot forge passed reports. Runtime uses `twill_execution_oracle.IMAGE` and never pulls implicitly.

## 11. Security & Threat Model

Untrusted source/candidate injection: text-only adapters, no tools/network tools, fixed oracle and policy; test malicious authority-expansion text. Path substitution: reject
symlinks and recheck identity before writes; test target swap. Holdout leakage: separate author/evaluator inputs and private logs; test expected answers absent from agent prompts.
Credentials use native CLI auth; never read or log values; redact subprocess outputs before private persistence. No raw sessions sent.

## 12. Performance Budgets

Default caps: 90s per call, 36 experiment invocations (including version probes) and 30min per experiment; one lesson per cycle. Validate suite size against cap before starting. No-op cycle target <2s with 100 private
records on this host, measured by monotonic timing in bounded integration tests; actual CLI latency separately recorded.

## 13. Operations (Deploy, Migration, Monitoring)

Install via extended `make install`; verify active symlink resolves to maintained checkout. Add `twill-learning.service/timer` (hourly, persistent); no cluster change. Back up
existing local config before enabling private exact-path policy; removal disables timer and owned exposures before restore. Backward compatibility: keep every legacy state/verb;
import copies source links, never reinterprets legacy acceptance. Monitoring: `twill-learning --policy PATH status --json` reports lesson state/reason and last-cycle outcomes. Provider counts/fingerprints, exposure hashes and unfinished intents remain in private referenced records/reports.
### Rollback Criteria Before Rollout

Rehearse config recovery and owned-block removal in scratch; any corruption, regression, permission escape or unmanaged edit blocks rollout. Disable timer on release failure,
reconcile intent, then remove matching blocks before restoring prior installed version/config. Preserve private evidence.

## 14. API / Interface Design

`--policy PATH [--json]` plus `create` (source-linked lesson/spec), import from a source-linked legacy lesson, `experiment ID`, `apply ID`, `validate ID`, `invalidate ID`, `cycle`,
`status`, `doctor`. Automatic catalog enrollment is policy `auto_evaluators`; discovery uses `intervention_sources` and a settlement window. Create returns lesson ID; mutations return outcome and evidence/exposure references. JSON follows `twill_contract` envelope. Error contract: 0 completed (including
recorded failed/inconclusive trial), 1 runtime error, 2 usage, 3 lock, 4 policy/validation/conflict; machine outcome never inferred from exit.

## 15. Risk Register & Plan B

| Risk | Likelihood/impact | Mitigation |
|---|---|---|
| R1 native agent ignores constrained execution | medium/high | verify real argv/capabilities in Phase 0; fail closed |
| R2 suite overfits | medium/high | hidden holdout, counterexamples, pinned paired trials, scoped claims |
| R3 concurrent human edits | medium/high | owned blocks, hashes, intent reconciliation and conflict stop |

Plan B: if native tool isolation or credible scoring cannot be proved, retain private experiments and legacy proposals; keep auto-apply disabled for that scope, report incomplete
acceptance.

## Human-directed lesson admission

Owner `twill-6c143847` adds a separate `human_directed` kind, derived only by verifying an original durable human directive, never from a candidate-provided flag. Policy `human_steering` explicitly enrolls exact scopes. Whole-message future-task declarations establish global or repository intent; repository intent cannot fall back to global. Source-native roles, original bytes, captured launch directory and full source snapshot hashes are rechecked at experiment, delivery and later cycles. Changed source snapshots or revoked enrollment remove managed exposure.

Human-directed lessons must pass all candidate, holdout and guardrail cases on both native providers, with complete error-free trial counts and verified model metadata. Baseline and candidate still run as paired trials, but baseline failure is not required. The report preserves the raw comparative verdict and separately records `human_adherence` admission. Inferred lessons retain `measured_improvement` admission. A supported exact continuation rule uses the independently fixed autonomy suite; other human rules remain `needs_evaluation` until policy binds an independent suite by exact lesson ID or instruction hash. A detector-wide binding cannot validate arbitrary human directives. Historical task-only, quoted, automated or unsupported interactive records do not authorize durable lessons. No rejected inferred record is silently reclassified.

Acceptance adds genuine native-shaped discovery for both providers, already-passing baselines that admit human guidance, rejection of forged provenance and scope expansion, adherence/guardrail/runtime failures, revalidation, source drift removal preserving human edits, and unchanged inferred improvement requirements. The native scratch proof is `scripts/rehearse-human-steering.py`; synthetic evidence is never a production lesson.

## Centralized source enrollment and fair discovery

Owner `twill-d5d27089` connects an optional original-byte producer outside the
archive repository to the existing native readers. The legacy engine does not
open an archive checkout or derived index. A bounded SDK-only producer reads
LIST/GET objects and verifies compressed/raw hashes, sizes and contiguous
artifact/generation coverage before supplying canonical private JSONL. Stable
current paths preserve source drift/withdrawal semantics. No captured snapshot
is evidence of a completed task. Budgets, source provenance and incomplete
backfill remain explicit in private status.

A private SQLite discovery queue replaces first-glob truncation and the 4,096
fingerprint rotation. Directory inventory and pending reads resume fairly across
every enrolled glob/provider; completed fingerprints and partial parser cursors
survive across large corpora. The existing JSON cursor state migrates without
reclassifying evidence. `twill-learning discover` advances intake without native
model calls on its own five-minute timer; experiment/admission remains hourly
and retains its existing call budget. `input_coverage` exposes per-glob/provider
counts and optional producer progress; operational health cannot treat a missing
or failed explicitly enrolled feed as an empty corpus.

Acceptance includes a first provider with more than 20,000 files without starving
the second, persistence beyond 4,096 fingerprints, changed-source retry, bounded
resumption and genuine raw Claude/Codex source evidence. Producer tests cover
pagination, contiguous generations, corruption, limits, later withdrawal and
private path/credential boundaries. Live acceptance reads verified originals for
both native formats, supplies the external feed and observes both providers in
the scheduled no-model backfill. Full repository gates and deployment receipts
remain required; complete corpus backfill is measured separately from deployment.

## Contextual interpretation and consumed-source retention

Owner `twill-9c77e50b` adds a high-recall native intervention pass and a separate
contextual author/critic pass. The decision in the parent plan fixes scope,
source authority, strict citations, frozen guidance and independent evaluation.
Detection remains model-free; interpretation reserves a separate durable call
budget and cannot select its own oracle. Exact human preferences retain adherence
admission, inferred rules retain measured improvement, and unsupported candidates
remain available for independent evaluation. Private compact episodes, reviews
and interpretations preserve the evidence chain. Source withdrawal or verifier
drift cannot leave a learned rule silently active.

The producer may reclaim completed, byte-verified, unreferenced local snapshots
after compact evidence is durable. Pending and active evidence stays pinned.
Ownership errors fail closed, generation signatures prevent download loops, and
the learning lock serializes ownership changes with deletion. Remote originals
and live local sessions are outside deletion authority.

Acceptance covers native multiline/contextual corrections and interactive answers
for both providers, copied-session deduplication, ordinary-task/worker negatives,
strict author/critic citations and source/context drift, durable call quotas,
candidate lifecycle integration, and cap recovery without deleting pinned evidence.
Real corpus calibration labels identify the reviewer and retain unknown outcomes;
pipeline-stage counts never substitute for applied/validated lesson counts.

## 16. Open Questions

None requiring a human decision. Native CLI constraints are a Phase 0 empirical gate owned by the implementer; failure invokes Plan B rather than relaxing safety. N/A: HTTP API and
data migration, because this is a separate local CLI with additive private records and no legacy state conversion.

## 17. Revision History

2026-11-11: concrete opt-in improvement plan under `twill-bff7b510`; legacy default retained.

2026-11-11: production closure under `twill-73f5b935`: verified automatic intake, trusted recipe instantiation, real dispositions, executed oracles, timer-driven withdrawal proof and stage-aware health/follow-up.
