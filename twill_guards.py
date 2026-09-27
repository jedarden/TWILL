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
import shlex
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
WRAPPER_SUFFIX = ".wrapper.sh"
GATE_SUFFIX = ".gate.txt"
AGENTS_MD_SUFFIX = ".agents.md"
MEMORY_SUFFIX = ".memory.md"
HOOK_TOOL_MATCHER = "Write|Edit|MultiEdit|Bash"
HOOK_COMMAND = "python3 ~/.claude/hooks/org-rule-guard.py"
HOOK_TIMEOUT_SECONDS = 10
GUARD_SCHEMA = "twill-guard/v1"
MAX_GUARD_BYTES = 64 * 1024
TEMPLATE_LAYERS = ("hook", "wrapper", "gate", "agents_md", "memory")
_TEMPLATE_SUFFIXES = {
    "hook": HOOK_SUFFIX,
    "wrapper": WRAPPER_SUFFIX,
    "gate": GATE_SUFFIX,
    "agents_md": AGENTS_MD_SUFFIX,
    "memory": MEMORY_SUFFIX,
}


def _guard_error(message: str, hint: str = "") -> ValidationError:
    return ValidationError(message, hint)


def guards_dir(artifacts_root: Path, *, repo_root: Path | None = None) -> Path:
    """Return the validated external guard directory without creating it."""

    return lessons_dir(artifacts_root, repo_root=repo_root).parent / GUARD_DIRNAME


def guard_path(
    artifacts_root: Path,
    lesson_id: str,
    *,
    target_layer: str = "hook",
    repo_root: Path | None = None,
) -> Path:
    """Return the safe path for a rendered guard belonging to one lesson."""

    # lessons_dir validates the root and the lesson module owns the strict id
    # grammar.  Reusing its path construction also keeps artifact naming in
    # lockstep with the frontmatter contract.
    from twill_lessons import lesson_path

    lesson = lesson_path(artifacts_root, lesson_id, repo_root=repo_root)
    suffix = _template_suffix(target_layer)
    return guards_dir(artifacts_root, repo_root=repo_root) / f"{lesson.stem}{suffix}"


def _template_suffix(target_layer: str) -> str:
    try:
        return _TEMPLATE_SUFFIXES[target_layer]
    except (KeyError, TypeError):
        choices = ", ".join(TEMPLATE_LAYERS)
        raise _guard_error(
            f"guard template must be one of: {choices}",
            "choose a supported template with --guard-template",
        ) from None


def _required_routing_layer(target_layer: str) -> str:
    _template_suffix(target_layer)
    return "hook" if target_layer == "gate" else target_layer


def _require_template_layer(record: LessonRecord, target_layer: str) -> None:
    routing_layer = _required_routing_layer(target_layer)
    if record.state in {"draft", "accepted"}:
        return
    if record.layer != routing_layer:
        raise _guard_error(
            f"lesson {record.id} is not applied at the {routing_layer} layer",
            f"apply the lesson with --layer {routing_layer} before emitting its guard",
        )


def _safe_explanation(record: LessonRecord) -> str:
    explanation = redact_text(record.summary).replace("\n", " ").strip()
    if explanation != record.summary or not explanation:
        raise _guard_error("lesson summary cannot be used in a guard explanation")
    if len(explanation) > 240:
        raise _guard_error("lesson summary exceeds the guard explanation limit")
    return explanation


def _safe_template_fields(record: LessonRecord) -> tuple[str, str, str]:
    """Return bounded, redaction-checked values used by every template."""

    summary = _safe_explanation(record)
    key = record.key
    if redact_text(key) != key or "\n" in key or "\r" in key or not key:
        raise _guard_error("guard template key contains unsafe or unredacted content")
    if len(key) > 240:
        raise _guard_error("guard template key exceeds 240 characters")
    return record.id, record.detector, summary


def render_hook_guard(record: LessonRecord, *, target_layer: str = "hook") -> str:
    """Render an org-rule-guard/ICG-compatible matcher fragment.

    The JSON has two intentionally separate pieces: ``hook`` is the
    drop-in Claude ``PreToolUse`` matcher, while ``rule`` is the rule-shaped
    fragment a human can copy into the selected guard's rule set.  TWILL does
    not install or execute either piece.
    """

    if target_layer != "hook":
        raise _guard_error(
            f"hook guard generation does not support template {target_layer!r}",
            "use render_guard for the selected per-layer template",
        )
    _require_template_layer(record, target_layer)
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


def render_wrapper_guard(record: LessonRecord, *, target_layer: str = "wrapper") -> str:
    """Render a shell wrapper skeleton that a human can adapt and install."""

    if target_layer != "wrapper":
        raise _guard_error(
            f"wrapper guard generation does not support template {target_layer!r}",
            "select the wrapper template explicitly",
        )
    _require_template_layer(record, target_layer)
    lesson_id, detector, summary = _safe_template_fields(record)
    key = record.key
    quoted_key = shlex.quote(key)
    text = (
        "#!/bin/sh\n"
        "set -eu\n"
        f"# TWILL guard proposal {lesson_id} ({detector})\n"
        f"# Lesson: {summary}\n"
        f"# Recurring key: {key}\n"
        "# Review the lesson and replace this placeholder check before installing.\n"
        "\n"
        "if [ \"$#\" -eq 0 ]; then\n"
        "    echo \"usage: $0 <command>\" >&2\n"
        "    exit 2\n"
        "fi\n"
        "\n"
        f"# TODO: enforce the reviewed handling for {quoted_key} here.\n"
        'exec "$@"\n'
    )
    return _bounded_template(text)


def render_gate_guard(record: LessonRecord, *, target_layer: str = "gate") -> str:
    """Render one definition-of-done checklist line for the hook-strength gate."""

    if target_layer != "gate":
        raise _guard_error(
            f"gate guard generation does not support template {target_layer!r}",
            "select the gate template explicitly",
        )
    _require_template_layer(record, target_layer)
    lesson_id, detector, summary = _safe_template_fields(record)
    key = record.key
    text = f"- [ ] TWILL guard {lesson_id} ({detector}): {summary} [key: {key}]\n"
    return _bounded_template(text)


def render_agents_md_guard(
    record: LessonRecord, *, target_layer: str = "agents_md"
) -> str:
    """Render a paragraph suitable for a repository ``AGENTS.md`` file."""

    if target_layer != "agents_md":
        raise _guard_error(
            f"AGENTS.md guard generation does not support template {target_layer!r}",
            "select the agents_md template explicitly",
        )
    _require_template_layer(record, target_layer)
    lesson_id, detector, summary = _safe_template_fields(record)
    key = record.key
    text = (
        f"<!-- TWILL guard {lesson_id}; human-installable proposal. -->\n"
        f"When the recurring condition `{key}` appears, {summary} "
        f"This guidance is backed by `{detector}` and belongs in the owning repository's AGENTS.md.\n"
    )
    return _bounded_template(text)


def render_memory_guard(record: LessonRecord, *, target_layer: str = "memory") -> str:
    """Render a memory leaf with frontmatter and a concise rule body."""

    if target_layer != "memory":
        raise _guard_error(
            f"memory guard generation does not support template {target_layer!r}",
            "select the memory template explicitly",
        )
    _require_template_layer(record, target_layer)
    lesson_id, detector, summary = _safe_template_fields(record)
    key = record.key
    text = (
        "---\n"
        f"id: {lesson_id}\n"
        f"summary: {json.dumps(summary, ensure_ascii=False)}\n"
        f"detector: {detector}\n"
        f"key: {json.dumps(key, ensure_ascii=False)}\n"
        "---\n"
        f"{summary} (trigger: `{key}`).\n"
    )
    return _bounded_template(text)


def _bounded_template(text: str) -> str:
    if len(text.encode("utf-8")) > MAX_GUARD_BYTES:
        raise _guard_error("generated guard template exceeds the artifact size limit")
    return text


def render_guard(record: LessonRecord, *, target_layer: str) -> str:
    """Render the selected human-installable template for a lesson."""

    renderers = {
        "hook": render_hook_guard,
        "wrapper": render_wrapper_guard,
        "gate": render_gate_guard,
        "agents_md": render_agents_md_guard,
        "memory": render_memory_guard,
    }
    try:
        renderer = renderers[target_layer]
    except (KeyError, TypeError):
        _template_suffix(target_layer)
        raise AssertionError("unreachable") from None
    return renderer(record, target_layer=target_layer)


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
        guard_path(
            artifacts_root,
            record.id,
            target_layer=target_layer,
            repo_root=repo_root,
        ),
        text,
    )


def write_guard(
    artifacts_root: Path,
    record: LessonRecord,
    *,
    target_layer: str,
    repo_root: Path | None = None,
) -> Path:
    """Render and atomically write one selected guard template."""

    text = render_guard(record, target_layer=target_layer)
    return _write_atomic(
        guard_path(
            artifacts_root,
            record.id,
            target_layer=target_layer,
            repo_root=repo_root,
        ),
        text,
    )


def write_wrapper_guard(
    artifacts_root: Path,
    record: LessonRecord,
    *,
    repo_root: Path | None = None,
) -> Path:
    return write_guard(artifacts_root, record, target_layer="wrapper", repo_root=repo_root)


def write_gate_guard(
    artifacts_root: Path,
    record: LessonRecord,
    *,
    repo_root: Path | None = None,
) -> Path:
    return write_guard(artifacts_root, record, target_layer="gate", repo_root=repo_root)


def write_agents_md_guard(
    artifacts_root: Path,
    record: LessonRecord,
    *,
    repo_root: Path | None = None,
) -> Path:
    return write_guard(artifacts_root, record, target_layer="agents_md", repo_root=repo_root)


def write_memory_guard(
    artifacts_root: Path,
    record: LessonRecord,
    *,
    repo_root: Path | None = None,
) -> Path:
    return write_guard(artifacts_root, record, target_layer="memory", repo_root=repo_root)


__all__ = [
    "GUARD_DIRNAME",
    "GUARD_SCHEMA",
    "TEMPLATE_LAYERS",
    "AGENTS_MD_SUFFIX",
    "GATE_SUFFIX",
    "HOOK_COMMAND",
    "HOOK_SUFFIX",
    "HOOK_TIMEOUT_SECONDS",
    "HOOK_TOOL_MATCHER",
    "MEMORY_SUFFIX",
    "WRAPPER_SUFFIX",
    "guard_path",
    "guards_dir",
    "render_agents_md_guard",
    "render_gate_guard",
    "render_guard",
    "render_hook_guard",
    "render_memory_guard",
    "render_wrapper_guard",
    "write_agents_md_guard",
    "write_gate_guard",
    "write_guard",
    "write_hook_guard",
    "write_memory_guard",
    "write_wrapper_guard",
]
