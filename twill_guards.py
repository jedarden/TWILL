"""Render human-installable guard artifacts for reviewed lessons.

Guard files are proposals, not live policy.  They are deliberately written to
the external ``artifacts_root`` so a public TWILL checkout can never become a
transport for private lesson content.  The hook fragment mirrors the
PreToolUse wiring used by the org guard and keeps the lesson key as a literal
regular-expression match: a key may describe punctuation, but it must not be
able to inject regex operators into the generated policy.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from twill_contract import ValidationError
from twill_lessons import lessons_dir
from twill_redactor import redact_text

if TYPE_CHECKING:
    from twill_lessons import LessonRecord


GUARD_DIRNAME = "guards"
HOOK_SUFFIX = ".hook.json"
HOOK_TOOL_MATCHER = "Write|Edit|MultiEdit|Bash"
HOOK_COMMAND = "python3 ~/.claude/hooks/org-rule-guard.py"
HOOK_TIMEOUT_SECONDS = 10
GUARD_SCHEMA = "twill-guard/v1"
MAX_GUARD_BYTES = 64 * 1024


def _guard_error(message: str, hint: str = "") -> ValidationError:
    return ValidationError(message, hint)


def guards_dir(artifacts_root: Path, *, repo_root: Path | None = None) -> Path:
    """Return the validated external guard directory without creating it."""

    return lessons_dir(artifacts_root, repo_root=repo_root).parent / GUARD_DIRNAME


def guard_path(
    artifacts_root: Path,
    lesson_id: str,
    *,
    repo_root: Path | None = None,
) -> Path:
    """Return the safe path for a hook guard belonging to one lesson."""

    # lessons_dir validates the root and the lesson module owns the strict id
    # grammar.  Reusing its path construction also keeps artifact naming in
    # lockstep with the frontmatter contract.
    from twill_lessons import lesson_path

    lesson = lesson_path(artifacts_root, lesson_id, repo_root=repo_root)
    return guards_dir(artifacts_root, repo_root=repo_root) / f"{lesson.stem}{HOOK_SUFFIX}"


def _require_hook_layer(record: LessonRecord, target_layer: str) -> None:
    if target_layer != "hook":
        raise _guard_error(
            f"guard generation only supports the hook layer, not {target_layer!r}",
            "use --emit-guard with --layer hook; other layer templates are a separate phase",
        )
    if record.state in {"draft", "accepted"}:
        return
    if record.layer != target_layer:
        raise _guard_error(
            f"lesson {record.id} is not applied at the hook layer",
            "apply the lesson with --layer hook before emitting its guard",
        )


def _safe_explanation(record: LessonRecord) -> str:
    explanation = redact_text(record.summary).replace("\n", " ").strip()
    if explanation != record.summary or not explanation:
        raise _guard_error("lesson summary cannot be used in a guard explanation")
    if len(explanation) > 240:
        raise _guard_error("lesson summary exceeds the guard explanation limit")
    return explanation


def render_hook_guard(record: LessonRecord, *, target_layer: str = "hook") -> str:
    """Render an org-rule-guard/ICG-compatible matcher fragment.

    The JSON has two intentionally separate pieces: ``hook`` is the
    drop-in Claude ``PreToolUse`` matcher, while ``rule`` is the rule-shaped
    fragment a human can copy into the selected guard's rule set.  TWILL does
    not install or execute either piece.
    """

    _require_hook_layer(record, target_layer)
    key = record.key
    if not isinstance(key, str) or not key:
        raise _guard_error("hook guard requires a non-empty lesson key")
    if redact_text(key) != key or "\n" in key or "\r" in key:
        raise _guard_error("hook guard key contains unsafe or unredacted content")
    if len(key) > 240:
        raise _guard_error("hook guard key exceeds 240 characters")

    explanation = _safe_explanation(record)
    rule_id = f"twill-{record.id.lower()}"
    fragment = {
        "schema": GUARD_SCHEMA,
        "lesson_id": record.id,
        "detector": record.detector,
        "key": key,
        "backend": "org-rule-guard",
        "hook": {
            "event": "PreToolUse",
            "matcher": HOOK_TOOL_MATCHER,
            "hooks": [
                {
                    "type": "command",
                    "command": HOOK_COMMAND,
                    "timeout": HOOK_TIMEOUT_SECONDS,
                }
            ],
        },
        "rule": {
            "id": rule_id,
            "type": "command_regex",
            "regex": re.escape(key),
            "tier": "tier1",
            "severity": "High",
            "explanation": explanation,
            "destructive": False,
            "redirect": {
                "channel": "deny",
                "reason_template": explanation,
                "rewrite_template": None,
            },
        },
        "install": {
            "human_only": True,
            "instruction": (
                "Review this fragment, merge the rule into the selected guard, "
                "then install the PreToolUse matcher manually."
            ),
        },
    }
    text = json.dumps(fragment, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_GUARD_BYTES:
        raise _guard_error("generated hook guard exceeds the artifact size limit")
    return text


def _write_atomic(path: Path, text: str) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise _guard_error(f"guard path is not a regular file: {path.name}")
    parent = path.parent
    if parent.is_symlink():
        raise _guard_error("guard directory may not be a symbolic link")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not parent.is_dir() or parent.is_symlink():
        raise _guard_error("guard directory is not a regular directory")
    os.chmod(parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent, prefix=".guard-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def write_hook_guard(
    artifacts_root: Path,
    record: LessonRecord,
    *,
    target_layer: str = "hook",
    repo_root: Path | None = None,
) -> Path:
    """Write one hook guard atomically and return its external artifact path."""

    text = render_hook_guard(record, target_layer=target_layer)
    return _write_atomic(
        guard_path(artifacts_root, record.id, repo_root=repo_root), text
    )


__all__ = [
    "GUARD_DIRNAME",
    "GUARD_SCHEMA",
    "HOOK_COMMAND",
    "HOOK_SUFFIX",
    "HOOK_TIMEOUT_SECONDS",
    "HOOK_TOOL_MATCHER",
    "guard_path",
    "guards_dir",
    "render_hook_guard",
    "write_hook_guard",
]
