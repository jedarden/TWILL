"""Opt-in lesson experiments and reversible instruction delivery.

This controller is separate from the legacy proposes-only pipeline. Only a
local policy can authorize targets and bind independently authored test suites;
transcripts, lesson text and model outputs cannot grant delivery authority.
"""

from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from functools import lru_cache

from twill_lock import StateLock
from twill_redactor import redact_text
from twill_contract import CliError, emit_error, emit_success
import twill_learning_health


SCHEMA = "twill-learning/v1"
POLICY_SCHEMA = "twill-learning-policy/v1"
ID_RE = re.compile(r"LC-[a-f0-9]{16}")
DEFAULT_POLICY = Path("~/.config/twill/learning.json")
MAX_JSON = 2 * 1024 * 1024


class LearningError(ValueError):
    """An unsafe, incomplete or contradictory learning operation."""


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: object) -> str:
    raw = value if isinstance(value, bytes) else canonical(value)
    return hashlib.sha256(raw).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode()


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON:
        raise LearningError(f"not a bounded regular JSON file: {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise LearningError("expected a JSON object")
    return value


def atomic(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.absolute() != path.resolve():
        raise LearningError("refusing symlink write")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".learning-")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def safe_path(value: str) -> Path:
    path = Path(value).expanduser().absolute()
    # Never silently deliver through a symlink or redirected ancestor.
    if path != path.resolve():
        raise LearningError(f"policy path must be canonical: {path}")
    return path


def bounded_int(value: object, low: int, high: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise LearningError(f"{label} must be an integer in {low}..{high}")
    return value


def enrolled_source(value: str, patterns: list[str]) -> bool:
    """Match enrolled path components, including zero-directory **, without I/O."""
    parts = Path(value).parts
    for pattern in patterns:
        expected = Path(pattern).expanduser().parts

        @lru_cache(maxsize=4096)
        def matches(index: int, position: int) -> bool:
            if index == len(expected):
                return position == len(parts)
            if expected[index] == "**":
                return matches(index + 1, position) or (position < len(parts) and matches(index, position + 1))
            return (position < len(parts) and fnmatch.fnmatchcase(parts[position], expected[index])
                    and matches(index + 1, position + 1))

        if matches(0, 0):
            return True
    return False


class Controller:
    def __init__(self, policy: dict):
        if policy.get("schema") != POLICY_SCHEMA or policy.get("enabled") is not True:
            raise LearningError("learning requires an enabled v1 policy")
        self.policy = policy
        from twill_intervention_interpret import validate_policy as validate_two_pass
        self.two_pass = validate_two_pass(policy)
        if self.two_pass["enabled"]:
            from twill_intervention_mining import validate_policy as validate_mining
            validate_mining(policy)
        from twill_human_steering import validate_policy as validate_steering_policy
        validate_steering_policy(policy)
        from twill_source_discovery import validate_limits
        validate_limits(policy)
        feed = policy.get("archive_feed", {})
        if (not isinstance(feed, dict) or set(feed) - {"enabled", "status_path", "stale_seconds"}
                or type(feed.get("enabled", False)) is not bool):
            raise LearningError("archive_feed requires enabled, status_path and optional stale_seconds")
        if feed.get("enabled") is True:
            if not isinstance(feed.get("status_path"), str):
                raise LearningError("enabled archive_feed needs an exact status_path")
            safe_path(feed["status_path"])
            bounded_int(feed.get("stale_seconds", 86400), 60, 604800, "archive feed freshness")
        if policy.get("auto_evaluators"):
            from twill_learning_intake import validate_policy
            validate_policy(policy)
        self.root = safe_path(policy["artifacts_root"])
        # Reuse the engine's public/private boundary, also for explicit policies.
        from twill_lessons import lessons_dir
        lessons_dir(self.root)
        self.store = self.root / "learning"
        if self.store.is_symlink():
            raise LearningError("private learning store cannot be a symlink")
        self.work = safe_path(policy["work_root"])
        public = Path(__file__).resolve().parent
        if self.work == public or public in self.work.parents:
            raise LearningError("experiment work cannot live in public checkout")
        self.scopes = policy.get("scopes", {})
        if not isinstance(self.scopes, dict):
            raise LearningError("scopes must be a mapping")
        for scope in self.scopes.values():
            targets = scope.get("targets", [])
            if not targets or len(targets) > 8 or len(set(targets)) != len(targets):
                raise LearningError("scope needs 1..8 exact targets")
            for value in targets:
                target = safe_path(value)
                if target.name not in {"AGENTS.md", "CLAUDE.md"}:
                    raise LearningError("delivery is limited to instruction files")
                if target == public or public in target.parents:
                    raise LearningError("private learned content cannot enter public TWILL")
        self.limits = policy.get("limits", {})
        self.repetitions = bounded_int(self.limits.get("repetitions", 2), 2, 5, "repetitions")
        if self.repetitions % 2:
            raise LearningError("repetitions must be even for balanced paired ordering")
        self.calls = bounded_int(self.limits.get("max_calls_per_cycle", 36), 4, 100, "max calls")
        self.timeout = bounded_int(self.limits.get("timeout_seconds", 90), 5, 180, "timeout")
        self.max_lessons = bounded_int(self.limits.get("max_lessons_per_cycle", 1), 1, 1, "max lessons")
        self.interval = bounded_int(self.limits.get("revalidate_seconds", 604800), 60, 2592000, "revalidation interval")
        bounded_int(self.limits.get("stale_cycle_seconds", 10800), 60, 604800, "stale cycle interval")
        bounded_int(self.limits.get("pending_attention_seconds", 86400), 60, 2592000, "pending attention interval")
        self.providers = policy.get("providers", {})
        if set(self.providers) != {"claude", "codex"}:
            raise LearningError("both Claude and Codex providers are required")
        for provider in self.providers.values():
            if not isinstance(provider.get("model"), str) or not provider["model"]:
                raise LearningError("each provider needs an explicit model")

    def path(self, lesson_id: str) -> Path:
        if not ID_RE.fullmatch(lesson_id):
            raise LearningError("invalid lifecycle lesson id")
        return self.store / "records" / f"{lesson_id}.json"

    def load(self, lesson_id: str) -> dict:
        record = read_json(self.path(lesson_id))
        if record.get("schema") != SCHEMA or record.get("id") != lesson_id:
            raise LearningError("lesson identity/schema mismatch")
        if digest(record["instruction"].encode()) != record["instruction_sha"]:
            raise LearningError("lesson instruction changed outside versioning")
        return record

    def save(self, record: dict, event: str, **details: object) -> dict:
        revision = record.get("revision", 0) + 1
        history_dir = self.store / "history" / record["id"]
        # An orphan revision after a failed pointer update remains immutable.
        while (history_dir / f"{revision:06d}.json").exists():
            revision += 1
        record.update(revision=revision, updated_at=stamp())
        entry = {"schema": SCHEMA, "id": record["id"], "revision": revision,
                 "at": record["updated_at"], "event": event, "details": details,
                 "record_sha": digest(record)}
        history = history_dir / f"{revision:06d}.json"
        # Record snapshot in an immutable revision before advancing the pointer.
        atomic(history, canonical({"event": entry, "record": record}))
        atomic(self.path(record["id"]), canonical(record))
        return record

    def scope(self, record: dict) -> dict:
        scope = self.scopes.get(record["scope"])
        if scope is None:
            raise LearningError("lesson scope is not enrolled")
        return scope

    def create(self, spec: dict) -> dict:
        instruction = spec.get("instruction", "").strip()
        if not instruction or len(instruction) > 4096 or "<!--" in instruction:
            raise LearningError("instruction must be 1..4096 characters without markers")
        if redact_text(instruction) != instruction:
            raise LearningError("instruction contains credential-shaped content")
        source = spec.get("source", {})
        source_path = safe_path(source.get("path", ""))
        if not source_path.is_file() or source_path.stat().st_size > MAX_JSON:
            raise LearningError("source must be a bounded evidence file")
        source_sha = digest(source_path.read_bytes())
        if source.get("sha256") and source_sha != source["sha256"]:
            raise LearningError("source evidence hash mismatch")
        scope = spec.get("scope", "unassigned")
        kind = "inferred"
        evaluator = spec.get("evaluator")
        source_evidence = spec.get("source_evidence", {})
        # Provenance is established from the original human message, never
        # from a candidate's kind flag, paraphrase or supplied evidence dict.
        try:
            source_payload = json.loads(source_path.read_text())
        except ValueError:
            source_payload = None
        if isinstance(source_payload, dict) and source_payload.get("schema") == "twill-intervention-interpretation/v1":
            from twill_intervention_interpret import verified_candidate
            candidate = verified_candidate(source_path, self.policy)
            if candidate is None or candidate["instruction"].strip() != instruction:
                raise LearningError("interpreted lesson requires a verified exact candidate instruction")
            expected_kind = "human_directed" if candidate["kind"] == "human_directed" else "inferred"
            if scope != candidate["scope"] or spec.get("kind", expected_kind) != expected_kind:
                raise LearningError("interpreted lesson scope and kind must match verified source")
            kind = expected_kind
            source_evidence = self.interpreted_source_evidence(candidate)
            from twill_human_steering import classify
            evaluator = classify(instruction) if kind == "human_directed" else None
        elif isinstance(source_payload, dict) and source_payload.get("schema") == "twill-human-steering/v1":
            from twill_human_steering import verify, resolve_scope, classify
            source_evidence = verify(source_path, instruction)
            if not source_evidence.get("complete"):
                raise LearningError("human steering requires a verified durable human directive")
            self.check_human_sources(source_evidence)
            resolved = resolve_scope(source_evidence, self.policy)
            if scope != "unassigned" and scope != resolved:
                raise LearningError("human steering scope differs from captured intent and policy")
            scope = resolved or "unassigned"
            kind = "human_directed"
            evaluator = classify(instruction)
        if spec.get("kind", kind) != kind:
            raise LearningError("lesson kind must match verified source provenance")
        version = digest({"instruction": instruction, "source_sha": source_sha, "scope": scope})
        if kind == "human_directed":
            version = digest({"instruction": instruction, "source_sha": source_sha, "scope": scope,
                              "kind": kind, "evaluator": evaluator, "evidence_sha": digest(source_evidence)})
        elif spec.get("evaluator"):
            version = digest({"instruction": instruction, "source_sha": source_sha, "scope": scope,
                              "evaluator": spec["evaluator"], "evidence_sha": digest(spec.get("source_evidence", {}))})
            evidence = spec.get("source_evidence", {})
            if (spec["evaluator"] == "workflow-completion/v1" and evidence.get("complete")
                    and evidence.get("references")):
                version = digest({"instruction": instruction, "scope": scope,
                                  "evaluator": spec["evaluator"], "episode": evidence["references"][-1]})
        lesson_id = "LC-" + version[:16]
        if self.path(lesson_id).exists():
            return self.load(lesson_id)
        record = {"schema": SCHEMA, "id": lesson_id, "state": "draft",
                  "instruction": instruction, "instruction_sha": digest(instruction.encode()),
                  "source": {"path": str(source_path), "sha256": source_sha,
                             "legacy_id": source.get("legacy_id"),
                             "detector": source.get("detector")},
                  "scope": scope, "created_at": stamp(), "experiments": [],
                  "kind": kind, "evaluator": evaluator,
                  "source_evidence": source_evidence,
                  "application": None, "reason": "awaiting independent evaluation"}
        return self.save(record, "created")

    def _evaluation_parent(self, record: dict) -> str | None:
        """Validate a reconsideration's lineage before inheriting a binding."""
        parent_id = record.get("evaluation_parent")
        suite_sha = record.get("evaluation_suite_sha")
        if parent_id is None and suite_sha is None:
            return None
        if (not isinstance(parent_id, str) or not ID_RE.fullmatch(parent_id)
                or not isinstance(suite_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", suite_sha)):
            raise LearningError("invalid reconsideration lineage")
        parent = self.load(parent_id)
        if parent.get("state") != "rejected" or parent.get("application") is not None or not parent.get("experiments"):
            raise LearningError("reconsideration parent must remain rejected and unapplied with prior evidence")
        for field in ("instruction", "instruction_sha", "source", "scope", "kind", "evaluator", "source_evidence"):
            if record.get(field) != parent.get(field):
                raise LearningError("reconsideration must preserve the exact parent instruction and source")
        expected = "LC-" + digest({"reevaluation_of": parent_id,
                                    "source_sha": parent["source"]["sha256"],
                                    "instruction_sha": parent["instruction_sha"],
                                    "suite_sha": suite_sha})[:16]
        if record["id"] != expected:
            raise LearningError("reconsideration identity does not match its parent and suite")
        return parent_id

    def _select_suite(self, record: dict, *, require_binding: bool = False) -> dict:
        parent_id = self._evaluation_parent(record)
        if record.get("source_evidence", {}).get("type") == "interpreted-intervention":
            self.interpreted_evidence(record)
        human = record.get("kind") == "human_directed"
        if human:
            self.human_evidence(record)
        matches = [b for b in self.policy.get("bindings", [])
                   if b.get("scope") == record["scope"] and
                   (b.get("lesson_id") == record["id"] or
                    (parent_id is not None and b.get("lesson_id") == parent_id) or
                    (human and b.get("instruction_sha256") == record["instruction_sha"]) or
                    (not human and b.get("detector") and b["detector"] == record["source"].get("detector")))]
        if (require_binding or parent_id is not None) and len(matches) != 1:
            raise LearningError("reconsideration requires exactly one explicit policy-bound independent suite")
        if not matches and human and record.get("evaluator"):
            from twill_learning_intake import catalog_suite
            suite = catalog_suite(record["evaluator"])
        elif not matches and record.get("evaluator"):
            from twill_learning_intake import catalog_suite, validate_catalog_admission
            validate_catalog_admission(record["evaluator"], record["scope"], self.policy,
                                       record["source"].get("detector"))
            suite = catalog_suite(record["evaluator"], record.get("source_evidence", {}))
        elif len(matches) != 1:
            raise LearningError("exactly one policy-bound independent suite is required")
        else:
            binding = matches[0]
            path = safe_path(binding["suite"])
            suite = read_json(path)
            if digest(path.read_bytes()) != binding.get("suite_sha256"):
                raise LearningError("independent suite drifted; update policy explicitly")
        cases = suite.get("cases", [])
        if not cases or not any(c.get("split") == "holdout" for c in cases):
            raise LearningError("evaluation needs holdout cases")
        if not any(c.get("guardrail") is True for c in cases):
            raise LearningError("evaluation needs an explicit counterexample guardrail")
        if not any(c.get("split") == "train" for c in cases):
            raise LearningError("evaluation needs a training case")
        return suite

    def suite(self, record: dict) -> dict:
        suite = self._select_suite(record)
        if record.get("evaluation_parent") is not None and digest(suite) != record["evaluation_suite_sha"]:
            raise LearningError("reconsideration suite changed; create a new explicitly reconsidered version")
        return suite

    def reconsider(self, lesson_id: str, reason: str) -> dict:
        """Start a fresh, suite-pinned draft without reopening terminal history."""
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
            raise LearningError("reconsideration reason must contain 1..1000 nonblank characters")
        reason = reason.strip()
        if redact_text(reason) != reason:
            raise LearningError("reconsideration reason contains credential-shaped content")
        parent = self.load(lesson_id)
        if parent.get("state") != "rejected" or parent.get("application") is not None:
            raise LearningError("reconsideration requires a rejected, unapplied lesson")
        experiments = parent.get("experiments")
        if (not isinstance(experiments, list) or not experiments or not isinstance(experiments[-1], dict)
                or not isinstance(experiments[-1].get("suite_sha"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", experiments[-1]["suite_sha"])):
            raise LearningError("reconsideration requires prior experiment suite evidence")
        if not self.source_current(parent):
            raise LearningError("reconsideration source evidence is no longer current")
        # Explicit operator policy selects the revised oracle. Ordinary suite()
        # always enforces an existing child's pin, including during later trials.
        suite_sha = digest(self._select_suite(parent, require_binding=True))
        if suite_sha == experiments[-1]["suite_sha"]:
            raise LearningError("reconsideration requires a revised independent suite")
        child_id = "LC-" + digest({"reevaluation_of": lesson_id,
                                   "source_sha": parent["source"]["sha256"],
                                   "instruction_sha": parent["instruction_sha"],
                                   "suite_sha": suite_sha})[:16]
        if self.path(child_id).exists():
            child = self.load(child_id)
            if child.get("evaluation_parent") != lesson_id or child.get("evaluation_suite_sha") != suite_sha:
                raise LearningError("reconsideration child identity conflicts with existing history")
            self.suite(child)
            return child
        child = {field: copy.deepcopy(parent[field]) for field in
                 ("instruction", "instruction_sha", "source", "scope", "kind", "evaluator", "source_evidence")
                 if field in parent}
        child.update(schema=SCHEMA, id=child_id, state="draft", created_at=stamp(),
                     experiments=[], application=None, evaluation_parent=lesson_id,
                     evaluation_suite_sha=suite_sha, reconsideration_reason=reason,
                     reason="awaiting fresh independent evaluation under a revised suite")
        self.suite(child)
        return self.save(child, "reconsidered", evaluation_parent=lesson_id,
                         suite_sha=suite_sha, reason=reason)

    def interpreted_source_evidence(self, candidate: dict) -> dict:
        """Derive provenance from reverified native sources, never supplied flags."""
        from twill_intervention_mining import verify_episode
        episode = verify_episode(Path(candidate["source_evidence"]["episode_path"]),
                                 enrolled_sources=self.policy.get("intervention_sources", []))
        self.check_human_sources({"raw_sources": episode["resolved_sources"]})
        evidence = dict(candidate["source_evidence"])
        sources = {ref["source_path"]: ref["file_sha256"] for ref in episode["turns"]}
        evidence.update(complete=True, raw_sources=[{"path": path, "sha256": sha} for path, sha in sorted(sources.items())],
                        instruction=candidate["instruction"], scope=candidate["scope"],
                        durable_intent=candidate["durable_intent"], explicit_authority=candidate["explicit_authority"])
        return evidence

    def interpreted_evidence(self, record: dict) -> dict:
        from twill_intervention_interpret import verified_candidate
        owned_blocks = {}
        current = {owned["id"]: owned for owned in self.records()}
        current[record["id"]] = record
        for owned in current.values():
            if not owned.get("application"):
                continue
            try:
                self.verify_application(owned)
            except LearningError:
                # Missing or edited blocks cannot excuse any context drift.
                continue
            block = self.block(owned)
            for entry in owned["application"]["files"]:
                addition = entry.get("addition")
                if addition in (block + "\n", "\n\n" + block + "\n"):
                    owned_blocks.setdefault(entry["path"], []).append(addition)
        candidate = verified_candidate(Path(record["source"]["path"]), self.policy, owned_blocks=owned_blocks)
        if candidate is None:
            raise LearningError("interpreted source no longer verifies")
        kind = "human_directed" if candidate["kind"] == "human_directed" else "inferred"
        from twill_human_steering import classify
        evaluator = classify(candidate["instruction"]) if kind == "human_directed" else None
        evidence = self.interpreted_source_evidence(candidate)
        if (record["instruction"] != candidate["instruction"].strip() or record["scope"] != candidate["scope"]
                or record.get("kind") != kind or record.get("source_evidence") != evidence
                or record.get("evaluator") != evaluator):
            raise LearningError("interpreted instruction, scope, intent or enrollment changed")
        return evidence

    def human_evidence(self, record: dict) -> dict:
        if record.get("source_evidence", {}).get("type") == "interpreted-intervention":
            return self.interpreted_evidence(record)
        from twill_human_steering import verify, resolve_scope, classify
        evidence = verify(Path(record["source"]["path"]), record["instruction"])
        self.check_human_sources(evidence)
        if (not evidence.get("complete") or resolve_scope(evidence, self.policy) != record["scope"]
                or record["scope"] == "unassigned"
                or evidence != record.get("source_evidence")
                or classify(record["instruction"]) != record.get("evaluator")):
            raise LearningError("human steering source, intent, scope or enrollment changed")
        return evidence

    def check_human_sources(self, evidence: dict) -> None:
        sources = evidence.get("raw_sources", [])
        if not sources or any(not enrolled_source(source["path"], self.policy.get("intervention_sources", []))
                              for source in sources):
            raise LearningError("human steering raw source is not enrolled")

    def source_current(self, record: dict) -> bool:
        path = Path(record["source"]["path"])
        current = path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == record["source"]["sha256"]
        if current and (record.get("kind") == "human_directed" or record.get("source_evidence", {}).get("type") == "interpreted-intervention"):
            try:
                if record.get("source_evidence", {}).get("type") == "interpreted-intervention":
                    self.interpreted_evidence(record)
                else:
                    self.human_evidence(record)
            except (LearningError, OSError, ValueError):
                return False
        return current

    def context(self, record: dict) -> dict:
        """Freeze actual surrounding guidance, excluding this owned lesson."""
        contexts = {"claude": [], "codex": []}
        block = self.block(record)
        scope = self.scope(record)
        configured = scope.get("context_paths")
        if configured is not None:
            if not isinstance(configured, dict) or set(configured) != {"claude", "codex"}:
                raise LearningError("context_paths must name both provider manifests")
            paths = [(name, path) for name in ("claude", "codex") for path in configured[name]]
            if not all(isinstance(configured[name], list) and len(configured[name]) <= 16 for name in configured):
                raise LearningError("context manifest must contain at most 16 paths per provider")
        else:
            paths = [("codex" if Path(p).name == "AGENTS.md" else "claude", p) for p in scope["targets"]]
        for name, target in paths:
            path = safe_path(target)
            text = path.read_text() if path.exists() else ""
            entries = (record.get("application") or {}).get("files", [])
            entry = next((e for e in entries if e["path"] == str(path)), None)
            if entry and entry["addition"] in text:
                text = text.replace(entry["addition"], "", 1)
            else:
                text = text.replace(block + "\n", "").replace(block, "")
            contexts[name].append(text)
        return {name: "\n\n".join(texts) for name, texts in contexts.items()}

    def fresh_evidence(self, record: dict) -> None:
        policy_path = getattr(self, "policy_path", None)
        if policy_path is not None and digest(read_json(policy_path)) != digest(self.policy):
            raise LearningError("operator policy changed during this operation")
        if not record["experiments"]:
            raise LearningError("no passing experiment evidence")
        receipt = record["experiments"][-1]
        suite = self.suite(record)
        if receipt.get("suite_sha") != digest(suite):
            raise LearningError("evaluation suite changed since experiment")
        if record.get("kind") == "human_directed":
            import twill_human_steering
            if receipt.get("steering_sha") != digest(Path(twill_human_steering.__file__).read_bytes()):
                raise LearningError("human steering verifier changed since experiment")
        if record.get("source_evidence", {}).get("type") == "interpreted-intervention":
            import twill_intervention_mining
            import twill_intervention_interpret
            for name, module in (("miner_sha", twill_intervention_mining), ("interpreter_sha", twill_intervention_interpret)):
                if receipt.get(name) != digest(Path(module.__file__).read_bytes()):
                    raise LearningError("intervention evidence verifier changed since experiment")
        if any("oracle" in case for case in suite["cases"]):
            import twill_execution_oracle
            if receipt.get("execution_sha") != digest(Path(twill_execution_oracle.__file__).read_bytes()):
                raise LearningError("execution grader changed since experiment")
        import twill_experiments
        adapter_file = getattr(twill_experiments, "__file__", None)
        adapter_sha = digest(Path(adapter_file).read_bytes()) if adapter_file else "test-adapter"
        if receipt.get("adapter_sha") != adapter_sha or receipt.get("controller_sha") != digest(Path(__file__).read_bytes()):
            raise LearningError("learning controller or experiment implementation changed")
        report = read_json(self.store / receipt["path"])
        if digest(report) != receipt["sha256"]:
            raise LearningError("experiment receipt hash mismatch")
        if receipt.get("context_sha") != digest(self.context(record)):
            raise LearningError("surrounding instructions changed since evaluation")
        from twill_experiments import provider_fingerprints
        fingerprints = provider_fingerprints(self.providers)
        for name in ("claude", "codex"):
            if fingerprints[name].get("error") or fingerprints[name]["fingerprint"] != report["providers"][name].get("model_fingerprint"):
                raise LearningError("provider model or CLI version changed since evaluation")

    def experiment(self, lesson_id: str, *, validation: bool = False) -> dict:
        record = self.load(lesson_id)
        if validation:
            if record["state"] != "active":
                raise LearningError("validation requires an active applied lesson")
            self.verify_application(record)
        if record["state"] in {"invalidated", "rejected", "superseded", "conflict", "applying", "removing"}:
            raise LearningError("lesson is not eligible for an experiment")
        if record["state"] == "active" and not validation:
            raise LearningError("use validate for an applied lesson")
        if not self.source_current(record):
            if record["application"]:
                return self.invalidate(lesson_id, "source evidence changed")
            record.update(state="needs_evaluation", reason="source evidence changed")
            return self.save(record, "source_drift")
        suite = self.suite(record)
        frozen_context = self.context(record)
        import twill_experiments
        from twill_experiments import run_experiment
        adapter_file = getattr(twill_experiments, "__file__", None)
        try:
            result = run_experiment({"id": lesson_id, "instruction": record["instruction"],
                                     "context": frozen_context, "cases": suite["cases"],
                                     "files": suite.get("files", {})}, work_root=self.work,
                                    providers=self.providers, timeout_seconds=self.timeout,
                                    repetitions=self.repetitions, max_calls=self.calls)
        except (RuntimeError, OSError) as exc:
            record["assessment"] = {"verdict": "inconclusive", "at": stamp(),
                                    "setup_error": redact_text(str(exc))[:500], "providers": {}}
            record["reason"] = "experiment runtime unavailable; no behavioral result established"
            if not validation:
                record["state"] = "unvalidated"
            self.save(record, "experiment_setup_error")
            return self.suspend(lesson_id, record["reason"]) if validation else record
        result_id = digest(result)
        report_path = self.store / "experiments" / f"{result_id}.json"
        atomic(report_path, canonical(result))
        record["experiments"].append({"path": str(report_path.relative_to(self.store)),
                                      "sha256": result_id, "at": stamp(),
                                      "policy_sha": digest(self.policy),
                                      "context_sha": digest(frozen_context),
                                      "adapter_sha": digest(Path(adapter_file).read_bytes()) if adapter_file else "test-adapter",
                                      "controller_sha": digest(Path(__file__).read_bytes()),
                                      "admission_basis": "human_adherence" if record.get("kind") == "human_directed" else "measured_improvement",
                                      "suite_sha": digest(suite), "validation": validation})
        if record.get("kind") == "human_directed":
            import twill_human_steering
            record["experiments"][-1]["steering_sha"] = digest(Path(twill_human_steering.__file__).read_bytes())
        if record.get("source_evidence", {}).get("type") == "interpreted-intervention":
            import twill_intervention_mining
            import twill_intervention_interpret
            record["experiments"][-1]["miner_sha"] = digest(Path(twill_intervention_mining.__file__).read_bytes())
            record["experiments"][-1]["interpreter_sha"] = digest(Path(twill_intervention_interpret.__file__).read_bytes())
        if any("oracle" in case for case in suite["cases"]):
            import twill_execution_oracle
            record["experiments"][-1]["execution_sha"] = digest(Path(twill_execution_oracle.__file__).read_bytes())
        # A runtime error is unknown; only a complete candidate failure can invalidate.
        reports = result.get("providers", {})
        record["assessment"] = {"verdict": result.get("verdict"), "at": stamp(),
                                "admission_basis": record["experiments"][-1]["admission_basis"],
                                "providers": {name: {k: r.get(k) for k in
                                              ("verdict", "errors", "counts", "actual_model", "model_fingerprint")}
                                              for name, r in reports.items()}}
        complete = set(reports) == {"claude", "codex"}
        errors = (not complete or result.get("candidate_sha") != record["instruction_sha"]
                  or self.context(record) != frozen_context or not self.source_current(record)
                  or any(r.get("errors", 0) for r in reports.values()))
        record["assessment"]["unknown"] = bool(errors)
        passed = complete and all(r.get("candidate_passed") is True and
                                  r.get("holdout_passed") is True for r in reports.values())
        human = record.get("kind") == "human_directed"
        if human:
            expected = len(suite["cases"]) * self.repetitions
            # Human preferences need adherence and guardrails, not a contrived
            # baseline failure. Missing/incomplete trials remain unknown.
            for report in reports.values():
                counts = report.get("counts", {})
                for arm in ("baseline", "candidate"):
                    arm_counts = counts.get(arm, {}) if isinstance(counts, dict) else {}
                    if (set(arm_counts) != {"pass", "fail", "error"}
                            or any(type(value) is not int or value < 0 for value in arm_counts.values())
                            or sum(arm_counts.values()) != expected or arm_counts.get("error") != 0):
                        errors = True
                if not report.get("actual_model") or not report.get("model_fingerprint"):
                    errors = True
                if any(type(report.get(flag)) is not bool for flag in
                       ("candidate_passed", "holdout_passed", "guardrails_passed")):
                    errors = True
            passed = passed and all(r.get("guardrails_passed") is True
                                    and r.get("verdict") != "regressed"
                                    and r.get("counts", {}).get("candidate", {}).get("pass") == expected
                                    for r in reports.values())
            record["assessment"]["unknown"] = bool(errors)
        if validation:
            record["last_validation"] = stamp()
            if not errors and not passed:
                self.save(record, "validation_failed")
                return self.invalidate(lesson_id, "candidate failed independent revalidation")
            record["reason"] = "validation inconclusive" if errors else "revalidated for both agents"
            if not errors and passed:
                record["validated_policy_sha"] = digest(self.policy)
            elif errors:
                self.save(record, "validation_inconclusive")
                return self.suspend(lesson_id, "revalidation unavailable; exposure suspended")
        else:
            promote = not errors and passed and (human or result.get("verdict") == "promote")
            record.update(state="validated" if promote else "unvalidated",
                          reason=("verified human steering passed adherence and guardrails for both agents" if human
                                  else "both agents improved") if promote else "experiment did not establish admission")
            # Complete unfavorable or redundant evidence is a disposition,
            # rather than an indefinitely retried unvalidated candidate.
            known_counts = complete and all(isinstance(r.get("counts"), dict) for r in reports.values())
            if not errors and not promote and known_counts:
                baseline_correct = all(r["counts"].get("baseline", {}).get("fail", 0) == 0
                                       and r["counts"].get("baseline", {}).get("pass", 0) > 0
                                       for r in reports.values())
                if baseline_correct and passed:
                    record.update(state="rejected", reason="redundant: both agent baselines already pass the independent suite")
                elif not passed:
                    record.update(state="rejected", reason="candidate failed independent behavioral checks")
                elif any(r.get("verdict") != "improved" for r in reports.values()):
                    record.update(state="rejected", reason="no demonstrated shared improvement across both agents")
            if promote:
                record["validated_policy_sha"] = digest(self.policy)
        return self.save(record, "validated" if validation else "experimented", result=result_id)

    @staticmethod
    def block(record: dict) -> str:
        return (f"<!-- twill-learning:{record['id']}:{record['instruction_sha']} -->\n"
                f"{record['instruction']}\n<!-- /twill-learning:{record['id']} -->")

    def _check_targets(self, record: dict) -> None:
        expected = set(self.scope(record)["targets"])
        application = record.get("application")
        if application and {e["path"] for e in application["files"]} != expected:
            raise LearningError("delivery targets changed; restore policy to recover owned blocks")

    def apply(self, lesson_id: str) -> dict:
        record = self.load(lesson_id)
        self._check_targets(record)
        if record["state"] == "active":
            self.verify_application(record)
            return record
        if record["state"] != "validated" or record.get("validated_policy_sha") != digest(self.policy):
            raise LearningError("only lessons validated under this exact policy can be applied")
        if self.scope(record).get("auto_apply") is not True:
            raise LearningError("scope does not authorize automatic application")
        if not self.source_current(record):
            raise LearningError("lesson source changed since experiment")
        # Suite binding is rechecked at the delivery boundary.
        self.suite(record)
        self.fresh_evidence(record)
        files = []
        block = self.block(record)
        for value in self.scope(record)["targets"]:
            path = safe_path(value)
            previous = path.read_text() if path.exists() else ""
            learned = re.findall(r"<!-- twill-learning:.*?<!-- /twill-learning:.*?-->", previous, re.S)
            if sum(map(len, learned)) + len(block) > 8192:
                raise LearningError("managed instruction budget exceeds 8192 characters")
            if f"twill-learning:{record['id']}" in previous:
                raise LearningError("unowned lesson marker already exists")
            separator = "\n\n" if previous and not previous.endswith("\n\n") else ""
            addition = separator + block + "\n"
            files.append({"path": str(path), "before_sha": digest(previous.encode()),
                          "after_sha": digest((previous + addition).encode()),
                          "existed": path.exists(), "addition": addition,
                          "mode": path.stat().st_mode & 0o777 if path.exists() else 0o644})
        record.update(state="applying", application={"files": files, "at": stamp()})
        self.save(record, "apply_intent")
        return self.recover(lesson_id)

    def suspend(self, lesson_id: str, reason: str) -> dict:
        """Remove stale exposure without treating missing evidence as harm."""
        record = self.load(lesson_id)
        record["removal_state"] = "unvalidated"
        record["reevaluate_after_drift"] = True
        record["reason"] = reason
        record["state"] = "removing"
        self.save(record, "suspend_intent")
        return self.recover(lesson_id)

    def recover(self, lesson_id: str) -> dict:
        record = self.load(lesson_id)
        if record["state"] not in {"applying", "removing"}:
            return record
        self._check_targets(record)
        removing = record["state"] == "removing"
        if not removing:
            try:
                if (record.get("validated_policy_sha") != digest(self.policy) or
                        self.scope(record).get("auto_apply") is not True or
                        not self.source_current(record)):
                    raise LearningError("application authority or source changed")
                self.suite(record)
                self.fresh_evidence(record)
            except LearningError as exc:
                record.update(state="removing", removal_state="unvalidated", reason=str(exc))
                self.save(record, "stale_apply_intent")
                return self.recover(lesson_id)
        block = self.block(record)
        # Preflight all files before any write. The write-ahead intent makes
        # an interrupted multi-file delivery resumable, not a false success.
        actions = []
        for entry in record["application"]["files"]:
            path = safe_path(entry["path"])
            current = path.read_text() if path.exists() else ""
            if not removing:
                current_sha = digest(current.encode())
                if current_sha not in {entry["before_sha"], entry["after_sha"]}:
                    raise LearningError("target changed during pending application")
                after = current + entry["addition"] if current_sha == entry["before_sha"] else current
                actions.append((path, current, after, entry["mode"], False))
            elif block in current:
                if current.count(block) != 1:
                    record.update(state="conflict", reason="duplicate owned block")
                    return self.save(record, "removal_conflict")
                # Exact owned bytes, preserving edits around the block.
                if digest(current.encode()) == entry["after_sha"]:
                    after = current[:-len(entry["addition"])]
                else:
                    after = current.replace(block + "\n", "", 1) if block + "\n" in current else current.replace(block, "", 1)
                actions.append((path, current, after, entry["mode"],
                                not entry["existed"] and after == ""))
            elif f"twill-learning:{record['id']}" in current:
                record.update(state="conflict", reason="owned lesson block was edited; manual reconciliation required")
                return self.save(record, "removal_conflict")
            else:
                actions.append((path, current, current, entry["mode"],
                                not entry["existed"] and current == ""))
        for path, before, after, mode, remove_empty in actions:
            if (path.read_text() if path.exists() else "") != before:
                raise LearningError("concurrent target edit; recover pending intent")
            if remove_empty:
                path.unlink(missing_ok=True)
            else:
                atomic(path, after.encode(), mode)
        record.update(state=record.pop("removal_state", "invalidated") if removing else "active")
        if removing and record["state"] == "unvalidated":
            record["application"] = None
        if not removing:
            record["last_validation"] = stamp()
            self.verify_application(record)
        return self.save(record, "removed" if removing else "applied")

    def verify_application(self, record: dict) -> None:
        self._check_targets(record)
        for entry in record["application"]["files"]:
            path = safe_path(entry["path"])
            if not path.is_file() or path.read_text().count(self.block(record)) != 1:
                raise LearningError("applied instruction is missing, duplicated or modified")

    def invalidate(self, lesson_id: str, reason: str) -> dict:
        record = self.load(lesson_id)
        reason = redact_text(reason)[:1000]
        if not reason:
            raise LearningError("invalidation needs a reason")
        if record["state"] == "invalidated":
            return record
        record["reason"] = reason
        record.pop("removal_state", None)
        if record["application"]:
            record["state"] = "removing"
            self.save(record, "remove_intent", reason=reason)
            return self.recover(lesson_id)
        record["state"] = "invalidated"
        return self.save(record, "invalidated", reason=reason)

    def records(self) -> list[dict]:
        directory = self.store / "records"
        return [self.load(p.stem) for p in sorted(directory.glob("LC-*.json"))]

    def publish(self) -> dict:
        """Publish only this controller's durable private evidence files."""
        inventory = []
        for namespace in ("records", "history", "experiments", "sources", "interventions", "interpretations", "intervention-reviews", "evaluations"):
            for path in sorted((self.store / namespace).rglob("*.json")):
                if path.is_symlink() or path.absolute() != path.resolve():
                    raise LearningError("private evidence path cannot be redirected")
                inventory.append({"path": str(path.relative_to(self.root)),
                                  "sha256": digest(path.read_bytes())})
        manifest = {"schema": SCHEMA, "files": inventory}
        atomic(self.store / "manifest.json", canonical(manifest))
        if self.policy.get("publish") is not True:
            return {"published": False, "reason": "local durability only"}
        from twill_artifacts import _git, _git_repo
        _git_repo(self.root, require_origin=True)
        branch = _git(self.root, ("branch", "--show-current")).stdout.strip()
        if branch != "main":
            raise LearningError("private evidence publication requires main")
        paths = tuple(item["path"] for item in inventory) + ("learning/manifest.json",)
        changed = _git(self.root, ("status", "--porcelain", "--", *paths)).stdout.strip()
        if changed:
            _git(self.root, ("add", "--", *paths))
            _git(self.root, ("commit", "--only", "-m", "Record private learning lifecycle evidence", "--", *paths))
        _git(self.root, ("push", "origin", "main"))
        return {"published": True, "commit": _git(self.root, ("rev-parse", "HEAD")).stdout.strip()}

    def import_drafts(self) -> int:
        from twill_lessons import list_lessons
        count = 0
        for lesson in list_lessons(self.root, state="draft"):
            # No independently bound evaluation means no promotion, even
            # when the old ranker has produced a plausible draft.
            matches = [b for b in self.policy.get("bindings", [])
                       if b.get("detector") == lesson.detector]
            scope = matches[0]["scope"] if len(matches) == 1 else "unassigned"
            resolution = None
            if scope == "unassigned" and self.policy.get("auto_evaluators"):
                from twill_learning_intake import resolve
                resolution = resolve(lesson, self.policy)
                scope = resolution.get("scope") or "unassigned"
            instruction = lesson.summary
            before = len(self.records())
            record = self.create({"instruction": instruction, "scope": scope,
                                  "evaluator": resolution.get("evaluator") if resolution else None,
                                  "source_evidence": resolution.get("source_evidence", {}) if resolution else {},
                                  "source": {"path": str(lesson.path), "legacy_id": lesson.id,
                                             "detector": lesson.detector}})
            if resolution and record["state"] in {"draft", "needs_evaluation"}:
                disposition = resolution.get("disposition")
                if disposition in {"rejected", "needs_evaluation"}:
                    record.update(state=disposition, reason=resolution.get("reason", "intake assessment"))
                    self.save(record, "intake_disposition", evaluator=record.get("evaluator"))
            # Assigning a supported scope creates a new immutable version;
            # keep the unassigned predecessor as an explicit supersession.
            for old in self.records():
                if (old["id"] != record["id"] and old["scope"] == "unassigned"
                        and old["source"] == record["source"]
                        and old["state"] in {"draft", "needs_evaluation", "unvalidated"}
                        and scope != "unassigned"):
                    old.update(state="superseded", successor=record["id"], reason="automatically routed to an admitted evaluator and scope")
                    self.save(old, "superseded", successor=record["id"])
            count += len(self.records()) > before
        return count

    def import_sources(self) -> int:
        """Route verified role-bearing intervention extractions without a hand binding."""
        from twill_learning_intake import resolve, catalog_instruction, discover_sources
        if not self.two_pass["enabled"] and self.policy.get("auto_evaluators"):
            discover_sources(self.policy, self.root)
        from twill_human_steering import discover as discover_steering, verify, resolve_scope
        if not self.two_pass["enabled"]:
            discover_steering(self.policy, self.root)
        count = self.import_interpretations() if self.two_pass["enabled"] else 0
        for path in sorted((self.store / "sources").glob("*.json")):
            if path.name.startswith("."):
                continue
            payload = read_json(path)
            if payload.get("schema") == "twill-human-steering/v1":
                if self.policy.get("human_steering", {}).get("enabled") is not True:
                    continue
                evidence = verify(path)
                if not evidence.get("complete"):
                    # Existing versions are withdrawn by source_current below.
                    continue
                before = {r["id"] for r in self.records()}
                record = self.create({"instruction": evidence["instruction"],
                                      "scope": resolve_scope(evidence, self.policy) or "unassigned",
                                      "source": {"path": str(path), "detector": "human-steering"}})
                count += record["id"] not in before
                if record["scope"] != "unassigned":
                    for old in self.records():
                        if (old["id"] != record["id"] and old.get("kind") == "human_directed"
                                and old["scope"] == "unassigned" and old["source"] == record["source"]
                                and old["state"] in {"draft", "needs_evaluation", "unvalidated"}):
                            old.update(state="superseded", successor=record["id"],
                                       reason="human directive now resolves to an enrolled exact scope")
                            self.save(old, "superseded", successor=record["id"])
                if record["state"] == "draft":
                    try:
                        self.suite(record)
                    except LearningError as exc:
                        record.update(state="needs_evaluation", reason=redact_text(str(exc))[:500])
                        self.save(record, "evaluation_unavailable")
                continue
            if payload.get("schema") != "twill-workflow-intervention/v1":
                continue
            detector = payload.get("detector")
            result = resolve({"detector": detector, "source": {"path": str(path)}}, self.policy)
            evaluator = result.get("evaluator")
            if not evaluator:
                continue
            old_ids = {r["id"] for r in self.records()}
            record = self.create({"instruction": catalog_instruction(evaluator),
                                  "scope": result.get("scope") or "unassigned",
                                  "evaluator": evaluator, "source_evidence": result.get("source_evidence", {}),
                                  "source": {"path": str(path), "detector": detector}})
            if record["id"] not in old_ids:
                count += 1
            if record["state"] in {"draft", "needs_evaluation"} and result.get("disposition"):
                record.update(state=result["disposition"], reason=result["reason"])
                self.save(record, "intake_disposition", evaluator=evaluator)
        return count

    def import_interpretations(self, episode_ids: list[str] | None = None) -> int:
        from twill_intervention_interpret import verified_candidate, verified_candidates
        count = 0
        candidates = (verified_candidates(self.policy, self.root) if episode_ids is None else
                      [verified_candidate(self.store / "interpretations" / (ident + ".json"), self.policy)
                       for ident in episode_ids])
        for candidate in candidates:
            if candidate is None:
                continue
            before = {r["id"] for r in self.records()}
            kind = "human_directed" if candidate["kind"] == "human_directed" else "inferred"
            record = self.create({"instruction": candidate["instruction"], "scope": candidate["scope"], "kind": kind,
                                  "source": {"path": candidate["source"], "detector": "contextual-intervention"}})
            count += record["id"] not in before
            if record["state"] == "draft":
                try:
                    self.suite(record)
                except LearningError as exc:
                    record.update(state="needs_evaluation", reason=redact_text(str(exc))[:500])
                    self.save(record, "evaluation_unavailable")
        return count

    def interpret(self, limit: int | None = None, episode_ids: list[str] | None = None) -> dict:
        """Run the explicitly enrolled contextual pass; no delivery side effects."""
        from twill_intervention_interpret import interpret
        result = interpret(self.policy, self.root, self.work, self.providers, limit=limit, episode_ids=episode_ids)
        result["imported"] = self.import_interpretations(episode_ids) if self.two_pass["enabled"] else 0
        atomic(self.store / "interpretation-status.json", canonical(result))
        # CLI summaries contain state/references, never private instruction prose.
        result["candidates"] = [{key: candidate[key] for key in ("interpretation_id", "episode_id", "scope", "kind", "assessment")}
                                for candidate in result["candidates"]]
        return result

    def cycle(self) -> dict:
        started_at = stamp()
        interpretation = None
        if self.two_pass["enabled"]:
            from twill_intervention_mining import discover
            discover(self.policy, self.root)
            interpretation = self.interpret()
        imported = self.import_sources() + self.import_drafts()
        results = []
        spent = 0
        records = sorted(self.records(), key=lambda r: (
            0 if r["state"] in {"active", "applying", "removing"} else 1,
            r["experiments"][-1]["at"] if r["experiments"] else "", r["created_at"], r["id"]))
        before_experiments = sum(len(r["experiments"]) for r in records)
        for record in records:
            try:
                lesson_id = record["id"]
                if record["state"] in {"applying", "removing"}:
                    record = self.recover(lesson_id)
                if record["state"] == "active":
                    self.verify_application(record)
                    if not self.source_current(record):
                        record = self.invalidate(lesson_id, "source evidence changed")
                    else:
                        try:
                            if self.scope(record).get("auto_apply") is not True:
                                raise LearningError("automatic exposure authority revoked")
                            self.suite(record)
                            self.fresh_evidence(record)
                        except LearningError as exc:
                            record = self.suspend(lesson_id, str(exc))
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(record["last_validation"])).total_seconds()
                        if record["state"] == "active" and (age >= self.interval or record.get("validated_policy_sha") != digest(self.policy)) and spent < self.max_lessons:
                            record = self.experiment(lesson_id, validation=True)
                            spent += 1
                if record["state"] == "validated":
                    record = self.apply(lesson_id)
                elif record["state"] in {"draft", "unvalidated", "needs_evaluation"} and spent < self.max_lessons:
                    # Do not repeatedly spend quota on unchanged inconclusive runs.
                    if record["experiments"] and not record.get("reevaluate_after_drift"):
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(record["experiments"][-1]["at"])).total_seconds()
                        if age < self.interval:
                            results.append({"id": lesson_id, "state": record["state"], "cooldown": True})
                            continue
                    self.suite(record)
                    if record.pop("reevaluate_after_drift", False):
                        self.save(record, "drift_retest")
                    record = self.experiment(lesson_id)
                    spent += 1
                    if record["state"] == "validated":
                        record = self.apply(lesson_id)
                results.append({"id": lesson_id, "state": record["state"]})
            except (LearningError, OSError, ValueError) as exc:
                if record["state"] == "draft":
                    record.update(state="needs_evaluation", reason=redact_text(str(exc))[:500])
                    self.save(record, "evaluation_unavailable")
                results.append({"id": record["id"], "state": record["state"],
                                "attention": redact_text(str(exc))[:500]})
        summary = {"schema": SCHEMA, "started_at": started_at, "at": stamp(), "imported": imported + (interpretation.get("imported", 0) if interpretation else 0),
                   "interpretation": interpretation, "attempted": spent,
                   "experimented": sum(len(r["experiments"]) for r in self.records()) - before_experiments,
                   "results": results}
        summary["input_coverage"] = self.input_coverage()
        summary["health"] = self.health(summary)
        if self.policy.get("require_fresh_sources"):
            from twill_learning_impact import snapshot
            summary["observed_impact"] = snapshot(
                self.records(), state_dir=Path(self.policy.get("pipeline_state_dir", "~/.local/state/twill")).expanduser(),
                scopes=self.scopes)
        atomic(self.store / "status.json", canonical(summary))
        return summary

    def discover(self) -> dict:
        """Advance input backfill without making native model requests."""
        if self.two_pass["enabled"]:
            from twill_intervention_mining import discover
            discover(self.policy, self.root)
        imported = self.import_sources() + self.import_drafts()
        summary = {"schema": SCHEMA, "at": stamp(), "imported": imported,
                   "experimented": 0, "input_coverage": self.input_coverage(),
                   "health": self.health()}
        atomic(self.store / "discovery-status.json", canonical(summary))
        return summary

    def input_coverage(self) -> dict:
        from twill_source_discovery import discovery_coverage
        coverage = {"discovery": discovery_coverage(self.root)}
        if self.two_pass["enabled"]:
            from twill_intervention_mining import metrics
            coverage["interventions"] = metrics(self.root)
            interpretation_status = self.store / "interpretation-status.json"
            if interpretation_status.exists():
                status = read_json(interpretation_status)
                coverage["interpretation"] = {key: status.get(key) for key in ("enabled", "processed", "calls", "daily_calls", "dispositions", "errors")}
            else:
                coverage["interpretation"] = {"enabled": True, "processed": 0, "calls": 0}
        feed = self.policy.get("archive_feed", {})
        if feed.get("enabled") is True:
            path = safe_path(feed["status_path"])
            try:
                coverage["archive_feed"] = read_json(path)
            except (OSError, ValueError):
                coverage["archive_feed"] = {"status": "unavailable"}
        return coverage

    def health(self, last_cycle: dict | None = None) -> dict:
        if last_cycle is None:
            path = self.store / "status.json"
            last_cycle = read_json(path) if path.exists() else None
        pipeline = None
        if self.policy.get("require_fresh_sources"):
            from twill_status import read_status
            state = Path(self.policy.get("pipeline_state_dir", "~/.local/state/twill")).expanduser()
            pipeline = read_status(state).get("data", {})
        result = twill_learning_health.report(
            self.records(), last_cycle,
            stale_seconds=self.limits.get("stale_cycle_seconds", 10800),
            pending_seconds=self.limits.get("pending_attention_seconds", 86400), pipeline=pipeline)
        feed = self.policy.get("archive_feed", {})
        if feed.get("enabled") is True:
            observation = self.input_coverage().get("archive_feed", {})
            when = observation.get("last_success") or observation.get("at")
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(when).replace("Z", "+00:00"))).total_seconds()
            except (ValueError, TypeError):
                age = None
            if (observation.get("status") not in {"healthy", "ok", "in_progress", "partial"}
                    or observation.get("errors") or observation.get("cache_limit") or age is None
                    or age > feed.get("stale_seconds", 86400)):
                result["findings"].append({"kind": "archive_feed_unavailable_or_stale",
                                           "action": "Inspect the optional archive feed service and its input coverage."})
                result["status"] = "needs_attention"
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true", help="machine-readable output (also the default)")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--spec", type=Path, required=True)
    for name in ("experiment", "apply", "validate", "invalidate", "reconsider"):
        command = sub.add_parser(name)
        command.add_argument("id")
        if name in {"invalidate", "reconsider"}:
            command.add_argument("--reason", required=True)
    sub.add_parser("cycle")
    sub.add_parser("discover")
    interpret_command = sub.add_parser("interpret")
    interpret_command.add_argument("--limit", type=int)
    interpret_command.add_argument("--episode", dest="episode_ids", action="append",
                                   help="interpret only this canonical episode ID (repeatable, at most 32)")
    sub.add_parser("status")
    sub.add_parser("doctor")
    # Accept --json in the familiar post-subcommand position too.
    for command in sub.choices.values():
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        controller = Controller(read_json(args.policy.expanduser()))
        controller.policy_path = args.policy.expanduser().resolve()
        with StateLock(controller.store):
            if args.command == "create":
                result = controller.create(read_json(args.spec))
            elif args.command == "validate":
                result = controller.experiment(args.id, validation=True)
            elif args.command == "invalidate":
                result = controller.invalidate(args.id, args.reason)
            elif args.command == "reconsider":
                result = controller.reconsider(args.id, args.reason)
            elif args.command in {"status", "doctor"}:
                result = {"schema": SCHEMA, "lessons": [
                    {k: r.get(k) for k in ("id", "state", "scope", "kind", "reason", "revision", "evaluator", "assessment")}
                    for r in controller.records()]}
                result["health"] = controller.health()
                result["input_coverage"] = controller.input_coverage()
                status_path = controller.store / "status.json"
                if status_path.exists():
                    result["last_cycle"] = read_json(status_path)
            elif args.command == "cycle":
                result = controller.cycle()
            elif args.command == "discover":
                result = controller.discover()
            elif args.command == "interpret":
                result = controller.interpret(args.limit, episode_ids=args.episode_ids)
            else:
                result = getattr(controller, args.command)(args.id)
            if args.command not in {"status", "doctor"}:
                publication = controller.publish()
                if "id" not in result:
                    result["publication"] = publication
        # Print state and references, never instruction/evidence contents.
        if "id" in result:
            result = {k: result[k] for k in ("id", "state", "reason", "revision")}
        emit_success(result, json_mode=True)
        attention = result.get("health", {}).get("status") != "healthy" if "health" in result else False
        return 4 if ((args.command in {"cycle", "doctor", "discover"} and attention)
                     or any(r.get("state") == "conflict" for r in result.get("results", []))) else 0
    except CliError as exc:
        emit_error(exc.code, exc.message, exc.hint, json_mode=True)
        return exc.code
    except LearningError as exc:
        emit_error(4, redact_text(str(exc))[:500], json_mode=True)
        return 4
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        emit_error(1, redact_text(str(exc))[:500], json_mode=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
