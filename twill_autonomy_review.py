"""Private, revisioned operator adjudication for autonomy candidates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from twill_redactor import redact_text


SCHEMA = "twill-autonomy-assessment/v1"
ID_RE = re.compile(r"[0-9a-f]{32}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
DISPOSITIONS = ("supported", "needs_review", "dismissed")
FIELDS = {"schema", "candidate_id", "revision", "disposition", "reason",
          "detector_version", "analyzer_version", "input_sha", "evidence_sha",
          "source_refs", "supersedes", "reviewed_at"}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_ref(ref: object) -> None:
    from twill_artifacts import ArtifactContractError
    if not isinstance(ref, dict) or set(ref) - {"kind", "ordinal", "source_line", "related_ordinal"}:
        raise ArtifactContractError("invalid autonomy source reference")
    if not isinstance(ref.get("kind"), str) or len(ref["kind"]) > 80:
        raise ArtifactContractError("invalid autonomy reference kind")
    for name in ("ordinal", "source_line", "related_ordinal"):
        if name in ref and ref[name] is not None and (type(ref[name]) is not int or ref[name] < 0):
            raise ArtifactContractError("invalid autonomy reference locator")


def validate_journal(path: Path) -> list[dict]:
    from twill_artifacts import ArtifactContractError, _reject_duplicate_json_keys
    if path.is_symlink() or not path.is_file():
        raise ArtifactContractError("autonomy journal must be a regular file")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ArtifactContractError("autonomy journal must be UTF-8") from exc
    if not content or not content.endswith("\n"):
        raise ArtifactContractError("autonomy journal is empty or torn")
    rows = []
    for line in content.splitlines():
        if len(line.encode()) > 8192:
            raise ArtifactContractError("autonomy journal record exceeds byte limit")
        try:
            record = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
        except ValueError as exc:
            raise ArtifactContractError("invalid autonomy journal JSON") from exc
        if not isinstance(record, dict) or set(record) != FIELDS or record["schema"] != SCHEMA:
            raise ArtifactContractError("invalid autonomy journal schema")
        candidate_id = record["candidate_id"]
        if not isinstance(candidate_id, str) or not ID_RE.fullmatch(candidate_id) or path.name != f"A-{candidate_id}.jsonl":
            raise ArtifactContractError("autonomy journal candidate mismatch")
        if type(record["revision"]) is not int or record["revision"] != len(rows) + 1:
            raise ArtifactContractError("autonomy revision sequence is invalid")
        if record["disposition"] not in DISPOSITIONS:
            raise ArtifactContractError("invalid autonomy disposition")
        reason = record["reason"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 240 or redact_text(reason) != reason:
            raise ArtifactContractError("invalid or unredacted autonomy reason")
        if type(record["detector_version"]) is not int or record["detector_version"] < 1:
            raise ArtifactContractError("invalid autonomy detector version")
        if not isinstance(record["analyzer_version"], str) or len(record["analyzer_version"]) > 80:
            raise ArtifactContractError("invalid autonomy analyzer version")
        for name in ("input_sha", "evidence_sha"):
            if not isinstance(record[name], str) or not SHA_RE.fullmatch(record[name]):
                raise ArtifactContractError("invalid autonomy input/evidence hash")
        refs = record["source_refs"]
        if not isinstance(refs, list) or not refs or len(refs) > 24:
            raise ArtifactContractError("autonomy review requires 1–24 source references")
        for ref in refs:
            _validate_ref(ref)
        if record["supersedes"] != (len(rows) if rows else None):
            raise ArtifactContractError("autonomy supersession chain is invalid")
        try:
            stamp = datetime.fromisoformat(record["reviewed_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise ArtifactContractError("invalid autonomy review timestamp") from exc
        rows.append(record)
    return rows


def _journal_paths(root: Path) -> list[Path]:
    from twill_artifacts import read_autonomy_manifest, _read_json
    if not (root / "autonomy").exists():
        manifest_path = root / "manifest.json"
        if manifest_path.exists():
            payload = _read_json(manifest_path, label="manifest.json")
            extension = payload.get("extensions") if isinstance(payload, dict) else None
            if isinstance(extension, dict) and "autonomy" in extension:
                read_autonomy_manifest(root)
        return []
    sidecar = read_autonomy_manifest(root)
    return [root / entry["path"] for entry in sidecar["artifacts"]
            if entry["schema"] == SCHEMA]


def latest_journals(root: Path) -> dict[str, tuple[dict, str]]:
    result = {}
    for path in _journal_paths(root):
        rows = validate_journal(path)
        result[rows[-1]["candidate_id"]] = (rows[-1], _sha(path.read_bytes()))
    return result


def reconcile(connection: sqlite3.Connection, root: Path) -> int:
    """Replay validated authoritative journals into the disposable DB projection."""
    latest = latest_journals(root)
    with connection:
        connection.execute("DELETE FROM autonomy_adjudication")
        connection.executemany(
            "INSERT INTO autonomy_adjudication VALUES (?,?,?,?,?,?,?,?,?)",
            [(candidate_id, row["revision"], row["disposition"], row["reason"],
              row["detector_version"], row["analyzer_version"], row["input_sha"],
              row["evidence_sha"], digest)
             for candidate_id, (row, digest) in latest.items()],
        )
    return len(latest)


def assert_reconciled(connection: sqlite3.Connection, root: Path) -> None:
    from twill_artifacts import ArtifactContractError
    latest = latest_journals(root)
    actual = {row[0]: tuple(row[1:]) for row in connection.execute(
        "SELECT candidate_id, revision, disposition, detector_version, analyzer_version, "
        "input_sha, evidence_sha, journal_sha FROM autonomy_adjudication")}
    expected = {candidate_id: (row["revision"], row["disposition"], row["detector_version"],
               row["analyzer_version"], row["input_sha"], row["evidence_sha"], digest)
                for candidate_id, (row, digest) in latest.items()}
    if actual != expected:
        raise ArtifactContractError("autonomy adjudication projection is stale; run twill detect")


def review(connection: sqlite3.Connection, root: Path, candidate_id: str,
           disposition: str, reason: str) -> dict:
    from twill_artifacts import _ArtifactSnapshot, manifest_after_write, publish_snapshot
    from twill_contract import UsageError
    if not isinstance(candidate_id, str) or not ID_RE.fullmatch(candidate_id):
        raise UsageError("autonomy ID must be 32 lowercase hex characters")
    if disposition not in DISPOSITIONS:
        raise UsageError("invalid autonomy disposition")
    safe_reason = redact_text(reason).strip()
    if not safe_reason or len(safe_reason) > 240:
        raise UsageError("review reason must contain 1–240 redacted characters")
    row = connection.execute(
        "SELECT detector_version, analyzer_version, input_sha, evidence_sha, "
        "supporting_refs, disconfirming_refs FROM autonomy_assessment WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise UsageError("autonomy candidate not found; run twill detect first")
    refs = (json.loads(row[4]) + json.loads(row[5]))[:24]
    if not refs:
        raise UsageError("autonomy candidate has no source references")
    root = Path(root)
    path = root / "autonomy" / "assessments" / f"A-{candidate_id}.jsonl"
    autonomy_existed = (root / "autonomy").exists()
    assessments_existed = path.parent.exists()
    previous = validate_journal(path) if path.exists() else []
    record = {"schema": SCHEMA, "candidate_id": candidate_id,
              "revision": len(previous) + 1, "disposition": disposition,
              "reason": safe_reason, "detector_version": row[0],
              "analyzer_version": row[1], "input_sha": row[2], "evidence_sha": row[3],
              "source_refs": refs, "supersedes": len(previous) if previous else None,
              "reviewed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    content = "".join(_json(item) + "\n" for item in (*previous, record)).encode()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    rollback = [_ArtifactSnapshot(item) for item in
                (path, root / "autonomy" / "manifest.json", root / "manifest.json")]
    try:
        with manifest_after_write(root, (path,)):
            fd, name = tempfile.mkstemp(dir=path.parent, prefix=".assessment-", suffix=".tmp")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, path)
            finally:
                if fd >= 0:
                    os.close(fd)
                Path(name).unlink(missing_ok=True)
        # Commit locally before the DB projection changes. Transport remains
        # the explicit `twill publish` operation for the private checkout.
        publication = publish_snapshot(root, push=False,
                                       message=f"twill: adjudicate autonomy A-{candidate_id}")
    except BaseException:
        for snapshot in reversed(rollback):
            snapshot.restore()
        if not assessments_existed:
            path.parent.rmdir()
        if not autonomy_existed:
            (root / "autonomy").rmdir()
        raise
    reconcile(connection, root)
    return {"candidate_id": candidate_id, "revision": record["revision"],
            "disposition": disposition, "commit": publication.commit}


def inspect(connection: sqlite3.Connection, root: Path, state_dir: Path, *,
            window_start: str, state: str | None = None) -> list[dict]:
    """Read bounded, source-linked evidence without mutating the state database."""
    assert_reconciled(connection, root)
    if state is not None and state not in DISPOSITIONS:
        raise ValueError("invalid autonomy state")
    rows = connection.execute(
        "SELECT a.candidate_id, a.episode_id, a.session_key, a.family, a.cluster_key, "
        "a.detector_version, a.analyzer_version, a.input_sha, a.evidence_sha, "
        "a.disposition, a.reason, a.supporting_refs, a.disconfirming_refs, a.missing, "
        "a.assessed_at, e.authority_state, e.context_state, e.lifecycle, "
        "e.objective, e.action_key, e.start_ordinal, e.end_ordinal, "
        "s.source_path, j.disposition, j.reason, j.revision, "
        "j.detector_version, j.analyzer_version, j.input_sha, j.evidence_sha "
        "FROM autonomy_assessment a "
        "JOIN execution_episode e ON e.episode_id=a.episode_id "
        "JOIN session s ON s.session_key=a.session_key "
        "LEFT JOIN autonomy_adjudication j ON j.candidate_id=a.candidate_id "
        "WHERE a.assessed_at >= ? ORDER BY a.assessed_at DESC, a.candidate_id",
        (window_start,),
    ).fetchall()
    result = []
    for row in rows:
        (candidate_id, episode_id, session_key, family, cluster_key,
         detector_version, analyzer_version, input_sha, evidence_sha,
         inferred, inferred_reason, support_json, adverse_json, missing_json,
         assessed_at, authority_state, context_state, lifecycle, objective,
         action, start, end, source_path, decision, decision_reason, revision,
         decision_detector, decision_analyzer, decision_input, decision_evidence) = row
        matching = decision is not None and (
            decision_detector, decision_analyzer, decision_input, decision_evidence
        ) == (detector_version, analyzer_version, input_sha, evidence_sha)
        disposition = decision if matching else ("needs_review" if decision is not None else inferred)
        if state is not None and state != disposition:
            continue
        supporting = json.loads(support_json)
        disconfirming = json.loads(adverse_json)
        refs = (supporting + disconfirming)[:24]
        evidence = []
        for ref in refs:
            ordinal = ref.get("ordinal")
            interaction = connection.execute(
                "SELECT source_line, origin, actor, terminal_state, text, truncated "
                "FROM interaction WHERE session_key=? AND ordinal=?",
                (session_key, ordinal),
            ).fetchone() if ordinal is not None else None
            evidence.append({"kind": ref["kind"], "ordinal": ordinal,
                             "source_line": ref.get("source_line"),
                             "related_ordinal": ref.get("related_ordinal"),
                             "origin": interaction[1] if interaction else None,
                             "actor": interaction[2] if interaction else None,
                             "terminal_state": interaction[3] if interaction else None,
                             "text": interaction[4] if interaction else None,
                             "truncated": bool(interaction[5]) if interaction else None})
        reproduction = ("twill ingest --file " + shlex.quote(source_path) +
                        " --settle 0 --state-dir " + shlex.quote(str(state_dir)) +
                        " && twill detect --state-dir " + shlex.quote(str(state_dir)) +
                        " && twill autonomy --json --state-dir " + shlex.quote(str(state_dir)))
        safe_source_path = redact_text(source_path)
        safe_state_dir = redact_text(str(state_dir))
        if safe_source_path != source_path or safe_state_dir != str(state_dir):
            source_arg = (shlex.quote(source_path) if safe_source_path == source_path else
                          '"${TWILL_SOURCE_FILE:?set TWILL_SOURCE_FILE to the source path}"')
            state_arg = (shlex.quote(str(state_dir)) if safe_state_dir == str(state_dir) else
                         '"${TWILL_STATE_DIR:?set TWILL_STATE_DIR to the state directory}"')
            reproduction = ("twill ingest --file " + source_arg + " --settle 0 --state-dir " +
                            state_arg + " && twill detect --state-dir " + state_arg +
                            " && twill autonomy --json --state-dir " + state_arg)
        result.append({"candidate_id": candidate_id, "episode_id": episode_id,
                       "family": family, "cluster_key": cluster_key,
                       "disposition": disposition, "inferred_disposition": inferred,
                       "reason": decision_reason if matching else inferred_reason,
                       "adjudication_revision": revision if matching else None,
                       "stale_adjudication": decision is not None and not matching,
                       "detector_version": detector_version,
                       "analyzer_version": analyzer_version,
                       "input_sha": input_sha, "evidence_sha": evidence_sha,
                       "assessed_at": assessed_at, "source_path": safe_source_path,
                       "session_key": session_key, "objective": objective,
                       "action": action, "authority_state": authority_state,
                       "context_state": context_state, "lifecycle": lifecycle,
                       "episode_bounds": [start, end],
                       "supporting_refs": supporting, "disconfirming_refs": disconfirming,
                       "missing": json.loads(missing_json), "evidence": evidence,
                       "omitted_evidence": max(0, len(supporting) + len(disconfirming) - len(refs)),
                       "reproduce": reproduction})
    return result
