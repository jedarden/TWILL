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
import shlex
import stat
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from twill_lessons import LESSON_ID_RE, lessons_dir
from twill_redactor import redact_text


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
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GUARD_SUFFIXES = (
    ".environment.md",
    ".hook.json",
    ".wrapper.sh",
    ".gate.txt",
    ".skill.md",
    ".agents.md",
    ".memory.md",
    ".retrieval.md",
)
_MAX_LINE_LENGTH = 240

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


class _ArtifactSnapshot:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.exists = os.path.lexists(path)
        self.content = None
        self.mode = None
        if not self.exists:
            return
        if path.is_symlink() or not path.is_file():
            raise ArtifactContractError(
                f"artifact path is not a regular file: {path}"
            )
        self.content = path.read_bytes()
        self.mode = stat.S_IMODE(path.stat().st_mode)

    def restore(self) -> None:
        if not self.exists:
            self.path.unlink(missing_ok=True)
            return
        assert self.content is not None
        assert self.mode is not None
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=".artifact-restore-", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, self.mode)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(self.content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, self.mode)
            directory_fd = os.open(
                self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


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
        if not os.path.lexists(directory):
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


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _read_json(path: Path, *, label: str) -> object:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArtifactContractError(f"{label} is not valid UTF-8 JSON") from exc


def _read_text(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ArtifactContractError(f"{label} is not valid UTF-8 text") from exc


def _validate_lines(text: str, *, label: str) -> None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        if len(line) > _MAX_LINE_LENGTH:
            raise ArtifactContractError(
                f"{label} line {line_number} exceeds {_MAX_LINE_LENGTH} characters"
            )
        content = line.strip()
        if redact_text(content) != content:
            raise ArtifactContractError(
                f"{label} line {line_number} contains unredacted content"
            )


def _validate_lesson(path: Path, *, repo_root: Path | None) -> object:
    from twill_lessons import load_lesson

    try:
        record = load_lesson(path, repo_root=repo_root)
    except Exception as exc:
        raise ArtifactContractError(f"invalid lesson content: {path.name}") from exc
    _validate_lines(_read_text(path, label=f"lesson {path.name}"), label=f"lesson {path.name}")
    return record


def _validate_digest(path: Path, *, label: str) -> None:
    import twill_digest

    week_label = path.stem
    try:
        year, week = twill_digest.parse_week(week_label)
    except Exception as exc:
        raise ArtifactContractError(f"digest filename is not a valid ISO week: {path.name}") from exc
    week_end = date.fromisocalendar(year, week, 1).toordinal() + 6
    if week_end >= datetime.now(timezone.utc).date().toordinal():
        raise ArtifactContractError(f"digest week is not complete: {path.name}")
    text = _read_text(path, label=label)
    if not text.splitlines():
        raise ArtifactContractError(f"digest {path.name} must contain at least one line")
    try:
        twill_digest._validate_digest_text(text)
    except Exception as exc:
        raise ArtifactContractError(f"invalid digest content: {path.name}") from exc
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise ArtifactContractError(f"digest {path.name} line {line_number} is empty")
        marker = " | $ "
        marker_position = line.rfind(marker)
        if marker_position <= 0:
            raise ArtifactContractError(
                f"digest {path.name} line {line_number} has no reproduction command"
            )
        command = line[marker_position + len(marker) :]
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            raise ArtifactContractError(
                f"digest {path.name} line {line_number} has an invalid reproduction command"
            ) from exc
        if len(tokens) != 7 or tokens[:4] != ["twill", "digest", "--week", week_label]:
            raise ArtifactContractError(
                f"digest {path.name} line {line_number} has an invalid reproduction command"
            )
        if tokens[4:6] != ["--stdout", "--state-dir"] or not tokens[6]:
            raise ArtifactContractError(
                f"digest {path.name} line {line_number} has an invalid reproduction command"
            )
    _validate_lines(text, label=f"digest {path.name}")


def _validate_measurement_json(path: Path, *, label: str) -> None:
    text = _read_text(path, label=label)
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ArtifactContractError(
                f"measurement {path.name} line {line_number} is not valid JSON"
            ) from exc


def _validate_guard(path: Path, lesson_id: str, *, label: str) -> None:
    text = _read_text(path, label=label)
    _validate_lines(text, label=f"guard {path.name}")
    if path.name.endswith(".hook.json"):
        payload = _read_json(path, label=f"guard {path.name}")
        if not isinstance(payload, dict):
            raise ArtifactContractError(f"guard {path.name} must contain a JSON object")
        required = {"schema", "lesson_id", "detector", "key", "install"}
        if not required.issubset(payload):
            raise ArtifactContractError(f"guard {path.name} is missing required fields")
        if payload["schema"] != GUARD_SCHEMA or payload["lesson_id"] != lesson_id:
            raise ArtifactContractError(f"guard {path.name} has an invalid lesson reference")
        if not isinstance(payload["detector"], str) or not payload["detector"]:
            raise ArtifactContractError(f"guard {path.name} has an invalid detector")
        if not isinstance(payload["key"], str) or not payload["key"]:
            raise ArtifactContractError(f"guard {path.name} has an invalid key")
        for field in ("detector", "key"):
            value = payload[field]
            if (
                redact_text(value) != value
                or "\n" in value
                or "\r" in value
                or len(value) > _MAX_LINE_LENGTH
            ):
                raise ArtifactContractError(f"guard {path.name} has an invalid {field}")
        install = payload["install"]
        if not isinstance(install, dict) or install.get("human_only") is not True:
            raise ArtifactContractError(f"guard {path.name} must be human-installable")
        if not isinstance(install.get("instruction"), str) or not install["instruction"]:
            raise ArtifactContractError(f"guard {path.name} has an invalid install block")
        return
    expected_prefix = {
        ".environment.md": f"# TWILL environment-fix proposal {lesson_id}\n",
        ".wrapper.sh": f"#!/bin/sh\nset -eu\n# TWILL guard proposal {lesson_id} ",
        ".gate.txt": f"- [ ] TWILL guard {lesson_id} (",
        ".skill.md": f"---\nid: {lesson_id}\n",
        ".agents.md": f"<!-- TWILL guard {lesson_id};",
        ".memory.md": f"---\nid: {lesson_id}\n",
        ".retrieval.md": f"---\nid: {lesson_id}\n",
    }
    suffix = next((item for item in _GUARD_SUFFIXES if path.name.endswith(item)), None)
    if suffix is None or not text.startswith(expected_prefix[suffix]):
        raise ArtifactContractError(f"guard {path.name} has invalid template content")


def _validate_snapshot_contents(
    root: Path,
    entries: Sequence[Mapping[str, object]],
    *,
    repo_root: Path | None,
) -> None:
    lesson_records: dict[str, object] = {}
    lesson_paths = {
        str(entry["path"]): root / str(entry["path"])
        for entry in entries
        if str(entry["path"]).startswith("lessons/")
    }
    for relative, path in lesson_paths.items():
        lesson_id = path.stem
        lesson_records[lesson_id] = _validate_lesson(path, repo_root=repo_root)

    measurement_paths = {
        str(entry["path"]): root / str(entry["path"])
        for entry in entries
        if str(entry["path"]).startswith("measurements/")
    }
    digest_paths = {
        str(entry["path"]): root / str(entry["path"])
        for entry in entries
        if str(entry["path"]).startswith("digests/")
    }
    guard_paths = {
        str(entry["path"]): root / str(entry["path"])
        for entry in entries
        if str(entry["path"]).startswith("guards/")
    }
    for relative, path in digest_paths.items():
        _validate_digest(path, label=f"digest {path.name}")
    for relative, path in measurement_paths.items():
        lesson_id = path.stem
        if lesson_id not in lesson_records:
            raise ArtifactContractError(f"measurement {relative} references a missing lesson")
        _validate_measurement_json(path, label=f"measurement {path.name}")
        import twill_measure

        try:
            twill_measure.read_measurements(root, lesson_id, repo_root=repo_root)
        except Exception as exc:
            raise ArtifactContractError(f"invalid measurement content: {relative}") from exc
    for relative, path in guard_paths.items():
        lesson_id = path.name.split(".", 1)[0]
        if lesson_id not in lesson_records:
            raise ArtifactContractError(f"guard {relative} references a missing lesson")
        _validate_guard(path, lesson_id, label=f"guard {path.name}")

    available_guards = set(guard_paths)
    for lesson_id, record in lesson_records.items():
        guard = getattr(record, "guard", {})
        artifact = guard.get("artifact") if isinstance(guard, dict) else None
        if artifact is None:
            continue
        if not isinstance(artifact, str) or artifact not in available_guards:
            raise ArtifactContractError(f"lesson {lesson_id} references a missing guard")
        if Path(artifact).parts[0:1] != ("guards",) or Path(artifact).name.split(".", 1)[0] != lesson_id:
            raise ArtifactContractError(f"lesson {lesson_id} references the wrong guard")


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
    manifest_path = root / MANIFEST_FILENAME
    previous_manifest = _ArtifactSnapshot(manifest_path)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=root, prefix=".manifest-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    replaced = False
    try:
        os.fchmod(descriptor, MANIFEST_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, manifest_path)
        replaced = True
        os.chmod(manifest_path, MANIFEST_FILE_MODE)
        directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if replaced:
            previous_manifest.restore()
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return root / MANIFEST_FILENAME


@contextmanager
def manifest_after_write(
    artifacts_root: Path,
    paths: Sequence[Path],
    *,
    repo_root: Path | None = None,
) -> Iterator[None]:
    """Regenerate the manifest and roll back artifact paths if it fails.

    Artifact files are individually atomic, but an artifact and its manifest
    cannot be renamed as one filesystem operation.  Keeping the prior bytes
    for the paths touched by one operation prevents a failed inventory or
    manifest publication from leaving a changed artifact paired with stale
    metadata.  ``write_manifest`` itself uses a temporary file and replace, so
    an interrupted manifest write also leaves its previous version intact.
    """

    root = _root(artifacts_root, repo_root=repo_root)
    unique_paths = tuple(dict.fromkeys(Path(path) for path in paths))
    snapshots = tuple(_ArtifactSnapshot(path) for path in unique_paths)
    try:
        yield
        write_manifest(root, repo_root=repo_root)
    except BaseException:
        for snapshot in reversed(snapshots):
            snapshot.restore()
        raise


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
    payload = _read_json(path, label="manifest.json")
    if not isinstance(payload, dict) or payload.get("schema") != CONTRACT_SCHEMA:
        raise ArtifactContractError(f"manifest schema must be {CONTRACT_SCHEMA}")
    if payload.get("producer") != "twill":
        raise ArtifactContractError("manifest producer must be twill")
    _timestamp(payload.get("generated_at"))
    paths = payload.get("paths")
    if not isinstance(paths, dict) or any(
        not isinstance(paths.get(name), dict)
        or paths[name].get("pattern") != PATH_CONTRACT[name]["pattern"]
        or paths[name].get("schema") != PATH_CONTRACT[name]["schema"]
        for name in ARTIFACT_DIRS
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
        candidate = Path(relative) if isinstance(relative, str) else None
        if (
            not isinstance(relative, str)
            or candidate is None
            or candidate.is_absolute()
            or candidate.as_posix() != relative
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ArtifactContractError("manifest artifact paths must be relative")
        if relative in listed:
            raise ArtifactContractError(f"manifest lists an artifact twice: {relative}")
        assert candidate is not None
        expected_schema = _schema_for(candidate)
        if item.get("schema") != expected_schema:
            raise ArtifactContractError(f"manifest schema mismatch for {relative}")
        if (
            isinstance(item.get("bytes"), bool)
            or not isinstance(item.get("bytes"), int)
            or item["bytes"] < 0
        ):
            raise ArtifactContractError(f"manifest byte count is invalid for {relative}")
        if not isinstance(item.get("sha256"), str) or not _SHA256_RE.fullmatch(item["sha256"]):
            raise ArtifactContractError(f"manifest hash is invalid for {relative}")
        listed[relative] = item
    try:
        actual = {item["path"]: item for item in _inventory(root)}
    except (OSError, UnicodeError) as exc:
        raise ArtifactContractError("artifact tree cannot be read") from exc
    if set(listed) != set(actual):
        raise ArtifactContractError("manifest inventory does not match the artifact tree")
    for relative, item in listed.items():
        path = root / relative
        content = path.read_bytes()
        if len(content) != item["bytes"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ArtifactContractError(f"manifest hash mismatch for {relative}")
    _validate_snapshot_contents(root, tuple(actual.values()), repo_root=repo_root)
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
    "manifest_after_write",
    "read_manifest",
    "write_manifest",
]
