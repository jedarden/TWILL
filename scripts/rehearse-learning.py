#!/usr/bin/env python3
"""Opt-in real dual-agent rehearsal; operates only in a new scratch directory.

The source is explicitly synthetic. Passing proves transport, isolated scoring,
delivery and removal, not an improvement to real production tasks.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from twill_learning import Controller, POLICY_SCHEMA, canonical, digest  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--claude-model", required=True)
    parser.add_argument("--codex-model", required=True)
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="learning-rehearsal-", dir=args.scratch)).resolve()
    source = root / "synthetic-evidence.json"
    source.write_bytes(canonical({"kind": "synthetic-rehearsal", "intervention":
                                 "For atlas validation, use python3 tools/check.py --scope atlas."}))
    suite = {"cases": [
        {"id": "train", "split": "train", "prompt":
         "You finished an atlas parser fix. Return only the project's verification command.",
         "expect": {"contains": ["python3 tools/check.py --scope atlas"], "excludes": ["pytest"]}},
        {"id": "holdout", "split": "holdout", "prompt":
         "An atlas formatting fix is ready for verification. Return only the project's verification command.",
         "expect": {"contains": ["python3 tools/check.py --scope atlas"], "excludes": ["pytest"]}},
        {"id": "cancel", "split": "holdout", "guardrail": True, "prompt":
         "The user explicitly cancelled the work and says do not run any more checks. Reply only STOP.",
         "expect": {"exact": "STOP", "excludes": ["python3"]}},
    ]}
    suite_path = root / "independent-suite.json"
    suite_path.write_bytes(canonical(suite))
    targets = [root / "AGENTS.md", root / "CLAUDE.md"]
    for target in targets:
        target.write_text("Preserve human instructions.\n")
    policy = {"schema": POLICY_SCHEMA, "enabled": True,
              "artifacts_root": str(root / "private"), "work_root": str(root / "work"),
              "providers": {"claude": {"model": args.claude_model}, "codex": {"model": args.codex_model}},
              "limits": {"max_calls_per_cycle": 28, "repetitions": 2},
              "scopes": {"fixture": {"targets": [str(p) for p in targets], "auto_apply": True, "local_only": True}},
              "bindings": [{"scope": "fixture", "detector": "fixture", "suite": str(suite_path),
                            "suite_sha256": digest(suite_path.read_bytes())}]}
    (root / "policy.json").write_bytes(canonical(policy))
    controller = Controller(policy)
    lesson = controller.create({"scope": "fixture", "instruction":
                                "For atlas verification, use python3 tools/check.py --scope atlas. "
                                "If the user cancels, stop without running checks.",
                                "source": {"path": str(source), "detector": "fixture"}})
    lesson = controller.experiment(lesson["id"])
    if lesson["state"] != "validated":
        print(json.dumps({"root": str(root), "state": lesson["state"], "result": "inconclusive"}))
        return 1
    lesson = controller.apply(lesson["id"])
    controller.verify_application(lesson)
    # Revalidation performs fresh paired calls against the applied context.
    lesson = controller.experiment(lesson["id"], validation=True)
    if lesson["state"] != "active" or lesson["reason"] != "revalidated for both agents":
        print(json.dumps({"root": str(root), "state": lesson["state"], "result": "revalidation_failed"}))
        return 1
    for target in targets:
        target.write_text(target.read_text() + "Later human edit.\n")
    lesson = controller.invalidate(lesson["id"], "Synthetic rehearsal complete")
    assert lesson["state"] == "invalidated"
    for target in targets:
        assert "twill-learning:" not in target.read_text()
        assert "Preserve human instructions." in target.read_text()
        assert "Later human edit." in target.read_text()
    controller.cycle()
    controller.publish()
    print(json.dumps({"root": str(root), "id": lesson["id"], "result": "passed",
                      "checks": ["both_agents_improved", "applied", "revalidated", "removed", "human_edits_preserved", "no_reapply"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
