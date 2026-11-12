"""Outcome-aware learning health; a successful process is not stage coverage."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone


TERMINAL = frozenset({"rejected", "invalidated", "superseded", "retired"})
PENDING = frozenset({"draft", "needs_evaluation", "unvalidated", "validated"})


def _age(value: object, now: datetime) -> float | None:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if when.tzinfo is None:
            return None
        return max(0.0, (now - when).total_seconds())
    except (ValueError, TypeError):
        return None


def report(records: list[dict], last_cycle: dict | None, *, now: datetime | None = None,
           stale_seconds: int = 10800, pending_seconds: int = 86400,
           pipeline: dict | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    counts = dict(Counter(record["state"] for record in records))
    findings = []
    if last_cycle is None:
        findings.append({"kind": "not_started", "action": "Run the installed learning cycle."})
    else:
        age = _age(last_cycle.get("at"), now)
        if age is None or age > stale_seconds:
            findings.append({"kind": "stale_cycle", "age_seconds": age,
                             "action": "Inspect the learning timer and last service result."})
    pending = [r for r in records if r["state"] in PENDING]
    for record in records:
        state = record["state"]
        if state == "conflict":
            findings.append({"kind": "ownership_conflict", "id": record["id"],
                             "action": "Reconcile the modified managed block without replacing human edits."})
        elif state == "needs_evaluation":
            findings.append({"kind": "missing_evaluator", "id": record["id"],
                             "reason": record.get("reason"),
                             "action": "Check catalog admission, source evidence and nominated scope."})
        if state in PENDING:
            if record.get("assessment", {}).get("unknown"):
                findings.append({"kind": "unverified_experiment", "id": record["id"],
                                 "action": "Inspect input drift, missing provider results or unverified experiment metadata before retrying."})
            if record.get("assessment", {}).get("setup_error"):
                findings.append({"kind": "evaluation_runtime_unavailable", "id": record["id"],
                                 "reason": record["assessment"]["setup_error"],
                                 "action": "Restore the trusted execution runtime before retrying; no model result was established."})
            for provider, assessment in record.get("assessment", {}).get("providers", {}).items():
                if assessment.get("errors"):
                    findings.append({"kind": "provider_evaluation_error", "id": record["id"],
                                     "provider": provider, "action": "Inspect failed experiment transport or oracle setup before retrying."})
            age = _age(record.get("created_at"), now)
            if age is None or age > pending_seconds:
                findings.append({"kind": "stale_pending", "id": record["id"], "age_seconds": age,
                                 "action": "Resolve or explicitly reject this candidate; inspect its latest evidence."})
    if pending and all(r["state"] == "needs_evaluation" for r in pending):
        findings.append({"kind": "all_candidates_blocked", "action": "Repair intake/evaluator coverage before claiming an operating learning loop."})
    if last_cycle:
        for item in last_cycle.get("results", []):
            if item.get("attention") and not any(f.get("id") == item.get("id") for f in findings):
                findings.append({"kind": "stage_error", "id": item.get("id"),
                                 "reason": item["attention"], "action": "Inspect the lesson and last-cycle receipt."})
    if pipeline is not None:
        for name in ("ingest", "detect"):
            stage = pipeline.get("stages", {}).get(name, {})
            age = _age(stage.get("last_success"), now)
            if age is None or age > stale_seconds:
                findings.append({"kind": "stale_input_stage", "stage": name, "age_seconds": age,
                                 "action": "Inspect the transcript pipeline; an empty lesson queue does not establish healthy input."})
    experiment_count = sum(len(r.get("experiments", [])) for r in records)
    return {"schema": "twill-learning-health/v1", "status": "needs_attention" if findings else "healthy",
            "state_counts": counts, "candidates": len(records), "pending": len(pending),
            "experiments": experiment_count, "active": counts.get("active", 0),
            "terminal": sum(counts.get(s, 0) for s in TERMINAL), "findings": findings}
