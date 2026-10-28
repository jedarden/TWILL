"""Private observational coverage and intervention measurements for autonomy."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import twill_autonomy
import twill_autonomy_review
from twill_artifacts import (AUTONOMY_IMPACT_SCHEMA, AUTONOMY_INTERVENTION_SCHEMA,
                             ArtifactContractError, manifest_after_write,
                             read_autonomy_manifest)
from twill_redactor import redact_text


_ID = re.compile(r"[0-9a-f]{32}\Z")


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs an offset")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write(root: Path, path: Path, payload: bytes) -> bool:
    if path.is_symlink():
        raise ArtifactContractError("autonomy artifact cannot be a symlink")
    if path.exists() and path.read_bytes() == payload:
        return False
    autonomy_dir = root / "autonomy"
    if autonomy_dir.is_symlink() or path.parent.is_symlink():
        raise ArtifactContractError("autonomy artifact directory cannot be a symlink")
    autonomy_dir.mkdir(mode=0o700, exist_ok=True)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(autonomy_dir, 0o700)
    os.chmod(path.parent, 0o700)
    with manifest_after_write(root, (path,)):
        descriptor, name = tempfile.mkstemp(prefix=".impact-", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            Path(name).unlink(missing_ok=True)
    return True


def _interventions(root: Path) -> list[dict]:
    path = root / "autonomy" / "interventions.jsonl"
    if not path.exists():
        return []
    read_autonomy_manifest(root)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if (row.get("schema") != AUTONOMY_INTERVENTION_SCHEMA
                or not _ID.fullmatch(row.get("candidate_id", ""))
                or not isinstance(row.get("reason"), str)
                or not row["reason"].strip()
                or len(row["reason"]) > 240
                or redact_text(row["reason"]) != row["reason"]):
            raise ArtifactContractError("invalid autonomy intervention")
        _time(row["intervened_at"])
    return rows


def record_intervention(connection, root: Path, candidate_id: str, intervened_at: str,
                        reason: str) -> dict:
    """Append one explicitly dated change; no outcome is inferred from it."""
    if not _ID.fullmatch(candidate_id):
        raise ValueError("candidate id must be 32 lowercase hex characters")
    when = _time(intervened_at)
    if not reason.strip() or len(reason) > 240 or redact_text(reason) != reason:
        raise ValueError("reason must be nonempty, redacted, and at most 240 characters")
    twill_autonomy_review.assert_reconciled(connection, root)
    row = connection.execute(
        "SELECT a.family, a.detector_version, a.analyzer_version, a.input_sha, "
        "a.evidence_sha, x.source FROM autonomy_assessment a "
        "JOIN interaction_summary x ON x.session_key=a.session_key WHERE a.candidate_id=?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise ValueError("candidate not found")
    record = {"schema": AUTONOMY_INTERVENTION_SCHEMA, "candidate_id": candidate_id,
              "intervened_at": _stamp(when), "reason": reason,
              "source": row[5], "family": row[0], "detector_version": row[1],
              "analyzer_version": row[2], "input_sha": row[3], "evidence_sha": row[4]}
    rows = _interventions(root)
    if any(item["candidate_id"] == candidate_id and
           item["intervened_at"] == record["intervened_at"] for item in rows):
        raise ValueError("intervention already recorded at this time")
    rows.append(record)
    path = root / "autonomy" / "interventions.jsonl"
    _write(root, path, b"".join((json.dumps(item, sort_keys=True) + "\n").encode()
                           for item in rows))
    return record


def summarize(connection, root: Path, start: str, end: str) -> dict:
    """Replay a half-open UTC window from source-linked derived rows.

    A group is source × family × detector/analyzer version. A supported
    intervention is counted once per episode even if several families fire.
    """
    beginning, finish = _time(start), _time(end)
    if finish <= beginning:
        raise ValueError("impact window must be positive")
    required = {"session", "interaction", "interaction_summary",
                "execution_episode", "episode_assertion",
                "autonomy_assessment", "autonomy_adjudication"}
    present = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= present:
        return {"window_start": start, "window_end": end, "groups": [],
                "supported_interventions": 0,
            "source_coverage": [],
                "telemetry": {"exchange_tokens": None, "exchange_cost_usd": None,
                              "exchange_elapsed_seconds": None}}
    twill_autonomy_review.assert_reconciled(connection, root)
    candidates = twill_autonomy_review.inspect(
        connection, root, Path("."), window_start=start)
    current_candidates = []
    for original in candidates:
        if _time(original["assessed_at"]) >= finish:
            continue
        item = dict(original)
        episode = connection.execute(
            "SELECT input_sha FROM execution_episode WHERE episode_id=?",
            (item["episode_id"],),
        ).fetchone()
        if episode is None or item["input_sha"] != episode[0]:
            item["disposition"] = "needs_review"
            item["stale_adjudication"] = True
            item["adjudication_revision"] = None
        current_candidates.append(item)
    candidates = current_candidates
    source_coverage = [
        {"source": source, "sessions": sessions, "interactions": interactions,
         "omitted_over_cap": omitted or 0, "truncated_fields": truncated or 0,
         "missing_summaries": sessions - summarized}
        for source, sessions, interactions, omitted, truncated, summarized in connection.execute(
            "SELECT COALESCE(x.source,s.source_kind), count(*), sum((SELECT count(*) FROM interaction i "
            "WHERE i.session_key=s.session_key AND i.ts_utc >= ? AND i.ts_utc < ?)), "
            "sum(COALESCE(x.omitted_over_cap,0)), sum(COALESCE(x.truncated_fields,0)), "
            "count(x.session_key) FROM session s "
            "LEFT JOIN interaction_summary x ON x.session_key=s.session_key "
            "WHERE EXISTS (SELECT 1 FROM interaction i WHERE i.session_key=s.session_key "
            "AND i.ts_utc >= ? AND i.ts_utc < ?) "
            "GROUP BY COALESCE(x.source,s.source_kind) ORDER BY COALESCE(x.source,s.source_kind)",
            (start, end, start, end),
        )]
    episodes = connection.execute(
        "SELECT e.episode_id, e.session_key, COALESCE(x.source,s.source_kind), e.authority_state, "
        "e.context_state, e.advice_only, e.start_ordinal, e.end_ordinal "
        "FROM execution_episode e JOIN session s ON s.session_key=e.session_key "
        "LEFT JOIN interaction_summary x ON x.session_key=e.session_key "
        "JOIN interaction i ON i.session_key=e.session_key AND i.ordinal=e.start_ordinal "
        "WHERE i.ts_utc >= ? AND i.ts_utc < ?",
        (start, end),
    ).fetchall()
    groups = defaultdict(lambda: {"eligible_opportunities": 0, "opportunities_unknown": 0,
                                  "terminal_known": 0,
                                  "terminal_unknown": 0, "candidates": {state: 0 for state in
                                  twill_autonomy_review.DISPOSITIONS},
                                  "supported_findings": 0, "unknown_unassessed": 0,
                                  "explicit_restart_exchanges": 0,
                                  "explicit_approval_exchanges": 0,
                                  "adjudicated_positive": 0, "adjudicated_negative": 0,
                                  "adjudicated_abstained": 0,
                                  "adjudicated_true_positive": 0,
                                  "adjudicated_false_positive": 0})
    by_episode = defaultdict(list)
    elapsed_intervals = []
    for item in candidates:
        by_episode[item["episode_id"]].append(item)
        source = connection.execute(
            "SELECT source FROM interaction_summary WHERE session_key=?",
            (item["session_key"],),
        ).fetchone()[0]
        key = (source, item["family"], item["detector_version"],
               item["analyzer_version"])
        group = groups[key]
        group["candidates"][item["disposition"]] += 1
        current_version = (item["detector_version"] == twill_autonomy.DETECTOR_VERSION
                           and item["analyzer_version"] == twill_autonomy.ANALYZER_VERSION)
        group["supported_findings"] += int(current_version and
                                           item["disposition"] == "supported")
        if item["adjudication_revision"] is not None:
            if item["disposition"] == "supported":
                group["adjudicated_positive"] += 1
            elif item["disposition"] == "dismissed":
                group["adjudicated_negative"] += 1
            if item["inferred_disposition"] == "needs_review":
                group["adjudicated_abstained"] += 1
            if item["inferred_disposition"] == "supported":
                if item["disposition"] == "supported":
                    group["adjudicated_true_positive"] += 1
                elif item["disposition"] == "dismissed":
                    group["adjudicated_false_positive"] += 1
    # Report a source even when it has no candidate. Version is the active
    # analyzer contract; old, mismatched adjudications are handled as unknown.
    for episode_id, session_key, source, authority, context, advice, first, last in episodes:
        for family in twill_autonomy.FAMILIES:
            key = (source, family, twill_autonomy.DETECTOR_VERSION,
                   twill_autonomy.ANALYZER_VERSION)
            group = groups[key]
            if advice:
                continue
            if authority in ("withheld", "revoked"):
                continue
            if authority != "authorized" or context != "known":
                group["opportunities_unknown"] += 1
                continue
            group["eligible_opportunities"] += 1
            terminal = connection.execute(
                "SELECT ordinal, ts_utc FROM interaction WHERE session_key=? "
                "AND ordinal BETWEEN ? AND ? AND terminal_state IN ('completed','interrupted') "
                "ORDER BY ordinal DESC LIMIT 1",
                (session_key, first, last),
            ).fetchone()
            group["terminal_known" if terminal else "terminal_unknown"] += 1
            assertions = connection.execute(
                "SELECT kind, source_ordinal FROM episode_assertion WHERE episode_id=? AND "
                "strength='known' AND kind IN ('same_outcome_resume','explicit_approval')",
                (episode_id,),
            ).fetchall()
            group["explicit_restart_exchanges"] += sum(kind == "same_outcome_resume" for kind, _ in assertions)
            group["explicit_approval_exchanges"] += sum(kind == "explicit_approval" for kind, _ in assertions)
            if family == twill_autonomy.FAMILIES[0] and terminal:
                for _, ordinal in assertions:
                    if ordinal is None or ordinal > terminal[0]:
                        continue
                    exchange = connection.execute(
                        "SELECT ts_utc FROM interaction WHERE session_key=? AND ordinal=?",
                        (session_key, ordinal),
                    ).fetchone()
                    if exchange:
                        seconds = (_time(terminal[1]) - _time(exchange[0])).total_seconds()
                        if seconds >= 0:
                            elapsed_intervals.append(seconds)
            matching = [item for item in by_episode[episode_id] if item["family"] == family
                        and item["detector_version"] == key[2]
                        and item["analyzer_version"] == key[3]]
            coverage = connection.execute(
                "SELECT omitted_over_cap, truncated_fields FROM interaction_summary "
                "WHERE session_key=?", (session_key,),
            ).fetchone()
            unknown = (not terminal or not matching or coverage is None or
                       bool(coverage[0] or coverage[1]) or
                       any(item["stale_adjudication"] or
                           item["disposition"] == "needs_review" for item in matching))
            group["unknown_unassessed"] += int(unknown)
    result = []
    for (source, family, detector_version, analyzer_version), values in sorted(groups.items()):
        known = values["terminal_known"]
        labeled = values["adjudicated_positive"] + values["adjudicated_negative"]
        predicted_labeled = (values["adjudicated_true_positive"] +
                             values["adjudicated_false_positive"])
        current_version = (detector_version == twill_autonomy.DETECTOR_VERSION
                           and analyzer_version == twill_autonomy.ANALYZER_VERSION)
        result.append({"source": source, "family": family,
                       "detector_version": detector_version,
                       "analyzer_version": analyzer_version,
                       "current_version": current_version,
                       **values, "terminal_coverage": (known / values["eligible_opportunities"]
                                                       if values["eligible_opportunities"] else None),
                       "assessed_eligible": max(0, values["eligible_opportunities"] -
                                                 values["unknown_unassessed"]),
                       "supported_rate": (values["supported_findings"] / known
                                          if current_version and known and
                                          not values["unknown_unassessed"] else None),
                       "adjudicated_precision": (values["adjudicated_true_positive"] /
                                                predicted_labeled if current_version and
                                                predicted_labeled else None),
                       "adjudicated_abstention": (
                           values["adjudicated_abstained"] /
                           labeled if current_version and labeled else None)})
    # A single intervention may be detected by several families. Its union is
    # episode identity; the family cells remain useful diagnostic projections.
    unique_supported = len({item["episode_id"] for item in candidates
                            if item["disposition"] == "supported" and
                            item["detector_version"] == twill_autonomy.DETECTOR_VERSION and
                            item["analyzer_version"] == twill_autonomy.ANALYZER_VERSION})
    return {"window_start": start, "window_end": end, "groups": result,
            "supported_interventions": unique_supported,
            "source_coverage": source_coverage,
            "telemetry": {"exchange_tokens": None, "exchange_cost_usd": None,
                          "exchange_elapsed_seconds": (
                              {"observed": len(elapsed_intervals),
                               "mean": sum(elapsed_intervals) / len(elapsed_intervals),
                               "minimum": min(elapsed_intervals),
                               "maximum": max(elapsed_intervals)}
                              if elapsed_intervals else None)}}


def measure(connection, root: Path, now: datetime, days: int = 7,
            current: dict | None = None) -> dict:
    measured_at = _stamp(now)
    start = _stamp(now - timedelta(days=days))
    if current is None:
        current = summarize(connection, root, start, measured_at)
    interventions = []
    for item in _interventions(root):
        at = _time(item["intervened_at"])
        before = summarize(connection, root, _stamp(at - timedelta(days=days)), _stamp(at))
        after = summarize(connection, root, _stamp(at), _stamp(at + timedelta(days=days)))
        before_keys = {(row["source"], row["family"], row["detector_version"],
                        row["analyzer_version"]) for row in before["groups"]
                       if row["eligible_opportunities"] and not row["unknown_unassessed"]
                       and not row["opportunities_unknown"]}
        after_keys = {(row["source"], row["family"], row["detector_version"],
                       row["analyzer_version"]) for row in after["groups"]
                      if row["eligible_opportunities"] and not row["unknown_unassessed"]
                      and not row["opportunities_unknown"]}
        complete = now >= at + timedelta(days=days)
        interventions.append({"candidate_id": item["candidate_id"],
                              "intervened_at": item["intervened_at"],
                              "before": before, "after": after,
                              "after_complete": complete,
                              "comparable": complete and bool(before_keys & after_keys),
                              "comparison": "observational; no causal attribution"})
    payload = {"schema": AUTONOMY_IMPACT_SCHEMA, "measured_at": measured_at,
               "window_days": days, **current, "interventions": interventions}
    path = root / "autonomy" / "impact" / (now.date().isoformat() + ".json")
    _write(root, path, (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode())
    return payload
