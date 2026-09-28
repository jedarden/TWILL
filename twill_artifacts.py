"""The versioned interchange boundary for the private artifact repository.

The public TWILL checkout is the engine, not the transport.  This module keeps
the small amount of metadata a downstream recall consumer needs at the root of
``artifacts_root`` without copying any artifact content into the engine tree.
The manifest is an inventory of one publishable snapshot: every recognized
artifact has a relative path, schema name, byte count, and SHA-256 digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from twill_lessons import LESSON_ID_RE, lessons_dir


MANIFEST_FILENAME = "manifest.json"
CONTRACT_SCHEMA = "twill-artifacts/v1"
LESSON_SCHEMA = "twill-lesson/v1"
DIGEST_SCHEMA = "twill-digest/v1"
MEASUREMENT_SCHEMA = "twill-measurement/v1"
GUARD_SCHEMA = "twill-guard/v1"
ARTIFACT_DIRS = ("lessons", "digests", "measurements", "guards")
MANIFEST_FILE_MODE = 0o600
MANIFEST_DIR_MODE = 0o700

_DIGEST_NAME_RE = re.compile(r"^\d{4}-W\d{2}\.txt$")
_MEASUREMENT_NAME_RE = re.compile(r"^L-[0-9a-f]{8}\.jsonl$")
_GUARD_NAME_RE = re.compile(
    r"^L-[0-9a-f]{8}\.(?:environment\.md|hook\.json|wrapper\.sh|gate\.txt|"
    r"skill\.md|agents\.md|memory\.md|retrieval\.md)$"
)

PATH_CONTRACT: Mapping[str, Mapping[str, str]] = {
    "lessons": {
        "pattern": "lessons/L-<8 lowercase hex>.md",
        "schema": LESSON_SCHEMA,
    },
    "digests": {
        "pattern": "digests/YYYY-Www.txt",
        "schema": DIGEST_SCHEMA,
    },
    "measurements": {
        "pattern": "measurements/L-<8 lowercase hex>.jsonl",
        "schema": MEASUREMENT_SCHEMA,
    },
    "guards": {
        "pattern": "guards/L-<8 lowercase hex>.<template>",
        "schema": GUARD_SCHEMA,
    },
}


class ArtifactContractError(ValueError):
    """An artifact snapshot is missing required contract metadata or content."""


def _root(artifacts_root: Path, repo_root: Path | None = None) -> Path:
    """Validate the same external boundary used by every artifact writer."""

    # lessons_dir owns the repository-boundary check.  Its parent is the
    # configured root even when the root does not exist yet.
    return lessons_dir(artifacts_root, repo_root=repo_root).parent


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ArtifactContractError("manifest generated_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArtifactContractError(
            "manifest generated_at must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ArtifactContractError("manifest generated_at must include a timezone")
    return value


def _schema_for(relative: Path) -> str:
    if len(relative.parts) != 2:
        raise ArtifactContractError(f"artifact path must be directly below its namespace: {relative}")
    namespace, name = relative.parts
    if namespace == "lessons" and LESSON_ID_RE.fullmatch(name.removesuffix(".md")):
        return LESSON_SCHEMA
    if namespace == "digests" and _DIGEST_NAME_RE.fullmatch(name):
        return DIGEST_SCHEMA
    if namespace == "measurements" and _MEASUREMENT_NAME_RE.fullmatch(name):
        return MEASUREMENT_SCHEMA
    if namespace == "guards" and _GUARD_NAME_RE.fullmatch(name):
        return GUARD_SCHEMA
    raise ArtifactContractError(f"unrecognized artifact path: {relative}")


def _inventory(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for namespace in ARTIFACT_DIRS:
        directory = root / namespace
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise ArtifactContractError(f"artifact namespace is not a regular directory: {namespace}")
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file():
                raise ArtifactContractError(f"artifact path is not a regular file: {path.relative_to(root)}")
            relative = path.relative_to(root)
            schema = _schema_for(relative)
            content = path.read_bytes()
            entries.append(
                {
                    "path": relative.as_posix(),
                    "schema": schema,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
    return entries


def _manifest_payload(root: Path, generated_at: str | None) -> dict[str, object]:
    timestamp = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _timestamp(timestamp)
    return {
        "schema": CONTRACT_SCHEMA,
        "producer": "twill",
        "generated_at": timestamp,
        "paths": {name: dict(PATH_CONTRACT[name]) for name in ARTIFACT_DIRS},
        "artifacts": _inventory(root),
    }


def write_manifest(
    artifacts_root: Path,
    *,
    generated_at: str | None = None,
    repo_root: Path | None = None,
) -> Path:
    """Atomically publish the current external artifact inventory."""

    root = _root(artifacts_root, repo_root=repo_root)
    root.mkdir(parents=True, exist_ok=True, mode=MANIFEST_DIR_MODE)
    if root.is_symlink() or not root.is_dir():
        raise ArtifactContractError("artifacts_root must be a regular directory")
    os.chmod(root, MANIFEST_DIR_MODE)
    payload = _manifest_payload(root, generated_at)
    content = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=root, prefix=".manifest-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, MANIFEST_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / MANIFEST_FILENAME)
        os.chmod(root / MANIFEST_FILENAME, MANIFEST_FILE_MODE)
        directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return root / MANIFEST_FILENAME


def read_manifest(
    artifacts_root: Path,
    *,
    repo_root: Path | None = None,
) -> dict[str, object]:
    """Validate and return a consumer-visible v1 manifest.

    Unknown manifest fields are intentionally ignored so a v1 reader remains
    compatible with additive metadata.  A stale inventory, an unknown major
    contract, a symlink, or a hash mismatch is not a usable publication.
    """

    root = _root(artifacts_root, repo_root=repo_root)
    path = root / MANIFEST_FILENAME
    if path.is_symlink() or not path.is_file():
        raise ArtifactContractError("artifact root has no regular manifest.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactContractError("manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != CONTRACT_SCHEMA:
        raise ArtifactContractError(f"manifest schema must be {CONTRACT_SCHEMA}")
    if payload.get("producer") != "twill":
        raise ArtifactContractError("manifest producer must be twill")
    _timestamp(payload.get("generated_at"))
    paths = payload.get("paths")
    if not isinstance(paths, dict) or any(
        paths.get(name) != dict(PATH_CONTRACT[name]) for name in ARTIFACT_DIRS
    ):
        raise ArtifactContractError("manifest paths do not match the twill-artifacts/v1 contract")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise ArtifactContractError("manifest artifacts must be an array")
    listed: dict[str, dict[str, object]] = {}
    for item in artifacts:
        if not isinstance(item, dict):
            raise ArtifactContractError("manifest artifact entries must be objects")
        relative = item.get("path")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ArtifactContractError("manifest artifact paths must be relative")
        if relative in listed:
            raise ArtifactContractError(f"manifest lists an artifact twice: {relative}")
        expected_schema = _schema_for(Path(relative))
        if item.get("schema") != expected_schema:
            raise ArtifactContractError(f"manifest schema mismatch for {relative}")
        if not isinstance(item.get("bytes"), int) or item["bytes"] < 0:
            raise ArtifactContractError(f"manifest byte count is invalid for {relative}")
        if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ArtifactContractError(f"manifest hash is invalid for {relative}")
        listed[relative] = item
    actual = {item["path"]: item for item in _inventory(root)}
    if set(listed) != set(actual):
        raise ArtifactContractError("manifest inventory does not match the artifact tree")
    for relative, item in listed.items():
        path = root / relative
        content = path.read_bytes()
        if len(content) != item["bytes"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ArtifactContractError(f"manifest hash mismatch for {relative}")
    return payload


__all__ = [
    "ARTIFACT_DIRS",
    "CONTRACT_SCHEMA",
    "DIGEST_SCHEMA",
    "GUARD_SCHEMA",
    "LESSON_SCHEMA",
    "MANIFEST_FILENAME",
    "MEASUREMENT_SCHEMA",
    "PATH_CONTRACT",
    "ArtifactContractError",
    "read_manifest",
    "write_manifest",
]
