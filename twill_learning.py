"""Opt-in lesson experiments and reversible instruction delivery.

This controller is separate from the legacy proposes-only pipeline. Only a
local policy can authorize targets and bind independently authored test suites;
transcripts, lesson text and model outputs cannot grant delivery authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from twill_lock import StateLock
from twill_redactor import redact_text
from twill_contract import CliError, emit_error, emit_success


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


class Controller:
    def __init__(self, policy: dict):
        if policy.get("schema") != POLICY_SCHEMA or policy.get("enabled") is not True:
            raise LearningError("learning requires an enabled v1 policy")
        self.policy = policy
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
        version = digest({"instruction": instruction, "source_sha": source_sha, "scope": scope})
        lesson_id = "LC-" + version[:16]
        if self.path(lesson_id).exists():
            return self.load(lesson_id)
        record = {"schema": SCHEMA, "id": lesson_id, "state": "draft",
                  "instruction": instruction, "instruction_sha": digest(instruction.encode()),
                  "source": {"path": str(source_path), "sha256": source_sha,
                             "legacy_id": source.get("legacy_id"),
                             "detector": source.get("detector")},
                  "scope": scope, "created_at": stamp(), "experiments": [],
                  "application": None, "reason": "awaiting independent evaluation"}
        return self.save(record, "created")

    def suite(self, record: dict) -> dict:
        matches = [b for b in self.policy.get("bindings", [])
                   if b.get("scope") == record["scope"] and
                   (b.get("lesson_id") == record["id"] or
                    (b.get("detector") and b["detector"] == record["source"].get("detector")))]
        if len(matches) != 1:
            raise LearningError("exactly one policy-bound independent suite is required")
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

    def source_current(self, record: dict) -> bool:
        path = Path(record["source"]["path"])
        return path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == record["source"]["sha256"]

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
        if record["state"] in {"invalidated", "rejected", "conflict", "applying", "removing"}:
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
        result = run_experiment({"id": lesson_id, "instruction": record["instruction"],
                                 "context": frozen_context, "cases": suite["cases"],
                                 "files": suite.get("files", {})}, work_root=self.work,
                                providers=self.providers, timeout_seconds=self.timeout,
                                repetitions=self.repetitions, max_calls=self.calls)
        result_id = digest(result)
        report_path = self.store / "experiments" / f"{result_id}.json"
        atomic(report_path, canonical(result))
        record["experiments"].append({"path": str(report_path.relative_to(self.store)),
                                      "sha256": result_id, "at": stamp(),
                                      "policy_sha": digest(self.policy),
                                      "context_sha": digest(frozen_context),
                                      "adapter_sha": digest(Path(adapter_file).read_bytes()) if adapter_file else "test-adapter",
                                      "controller_sha": digest(Path(__file__).read_bytes()),
                                      "suite_sha": digest(suite), "validation": validation})
        # A runtime error is unknown; only a complete candidate failure can invalidate.
        reports = result.get("providers", {})
        complete = set(reports) == {"claude", "codex"}
        errors = (not complete or result.get("candidate_sha") != record["instruction_sha"]
                  or self.context(record) != frozen_context or not self.source_current(record)
                  or any(r.get("errors", 0) for r in reports.values()))
        passed = complete and all(r.get("candidate_passed") is True and
                                  r.get("holdout_passed") is True for r in reports.values())
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
            promote = not errors and passed and result.get("verdict") == "promote"
            record.update(state="validated" if promote else "unvalidated",
                          reason="both agents improved" if promote else "experiment did not establish improvement")
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
        for namespace in ("records", "history", "experiments"):
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
            instruction = lesson.summary
            before = len(self.records())
            self.create({"instruction": instruction, "scope": scope,
                         "source": {"path": str(lesson.path), "legacy_id": lesson.id,
                                    "detector": lesson.detector}})
            count += len(self.records()) > before
        return count

    def cycle(self) -> dict:
        imported = self.import_drafts()
        results = []
        spent = 0
        for record in self.records():
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
                    if record["experiments"]:
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(record["experiments"][-1]["at"])).total_seconds()
                        if age < self.interval:
                            results.append({"id": lesson_id, "state": record["state"], "cooldown": True})
                            continue
                    self.suite(record)
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
        summary = {"schema": SCHEMA, "at": stamp(), "imported": imported,
                   "experimented": spent, "results": results}
        atomic(self.store / "status.json", canonical(summary))
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true", help="machine-readable output (also the default)")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--spec", type=Path, required=True)
    for name in ("experiment", "apply", "validate", "invalidate"):
        command = sub.add_parser(name)
        command.add_argument("id")
        if name == "invalidate":
            command.add_argument("--reason", required=True)
    sub.add_parser("cycle")
    sub.add_parser("status")
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
            elif args.command == "status":
                result = {"schema": SCHEMA, "lessons": [
                    {k: r[k] for k in ("id", "state", "scope", "reason", "revision")}
                    for r in controller.records()]}
                status_path = controller.store / "status.json"
                if status_path.exists():
                    result["last_cycle"] = read_json(status_path)
            elif args.command == "cycle":
                result = controller.cycle()
            else:
                result = getattr(controller, args.command)(args.id)
            if args.command != "status":
                publication = controller.publish()
                if "id" not in result:
                    result["publication"] = publication
        # Print state and references, never instruction/evidence contents.
        if "id" in result:
            result = {k: result[k] for k in ("id", "state", "reason", "revision")}
        emit_success(result, json_mode=True)
        return 4 if any(r.get("state") == "conflict" for r in result.get("results", [])) else 0
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
