"""Configuration loading with working defaults (plan §13.1).

TWILL reads ``~/.config/twill/config.toml`` for its settle window, retention,
top-K, source globs, content fences and Explain model.  Every key has a
working default except ``artifacts_root`` (plan §3, §7.2): it has none, and
:func:`load_config` raises when it is unset or resolves inside this
repository's working tree — a startup error, before any verb opens a file,
because the failure mode it prevents is publishing private work in a public
repository.  A missing config file is the same error: it leaves
``artifacts_root`` unset.  Unset and in-tree fail identically at load;
neither is deferred to the verb that would have written the artifact.

Config that is present but wrong — unparsable TOML, an unknown key, a bad
duration — is likewise a loud error, never a silent fallback to the default:
a default exists for *absence*, not to paper over a typo.

Key → consumer, for the keys no verb reads yet (plan §14):

- ``retention`` — ``twill prune`` (Phase 3)
- ``top_k`` — ``twill rank`` / ``twill explain`` (Phases 3–4)
- ``content_fences`` — the redactor, and again at lesson write (§11)
- ``model`` — ``twill explain`` (Phase 4); the operator's final choice is
  Open Question 2, this default is only §13.1's working value

``artifacts_root`` is consumed the moment config loads, and again by every
artifact writer through :meth:`TwillConfig.require_artifacts_root`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path("~/.config/twill/config.toml")

#: Duration units accepted by :func:`parse_duration`, shared with the CLI.
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

DEFAULT_SETTLE_WINDOW = "2h"
DEFAULT_RETENTION = "180d"
DEFAULT_TOP_K = 10
DEFAULT_SOURCE_GLOBS = (
    "~/.claude/projects/**/*.jsonl",
    "~/.codex/sessions/**/*.jsonl",
)
DEFAULT_CONTENT_FENCES: tuple[str, ...] = ()
# A working default only — the operator resolves the real model in Phase 4
# (plan §16, Open Question 2).  Explain is one weekly, cost-capped
# summarization pass, so the small current-generation model is the default.
DEFAULT_MODEL = "claude-haiku-4-5"

_BARE_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_SUFFIXED_DURATION_RE = re.compile(r"(?i)([0-9]+(?:\.[0-9]+)?)([smhd])")

_KNOWN_KEYS = frozenset(
    {
        "settle_window",
        "retention",
        "top_k",
        "source_globs",
        "content_fences",
        "model",
        "artifacts_root",
    }
)


class ConfigError(Exception):
    """A configuration file that cannot be used as written (plan §13.1)."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def parse_duration(value: object) -> float:
    """Parse a duration into seconds: ``0``, ``90``, ``30m``, ``2h``, ``180d``.

    Accepts the same forms as the ``--settle`` CLI flag so a config value and
    a flag value never disagree about syntax.  TOML numbers are accepted
    as bare seconds.
    """

    if isinstance(value, bool):
        raise ConfigError("duration must be a number or a string such as 2h, not a boolean")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ConfigError(f"duration must not be negative: {value}")
        return float(value)
    if not isinstance(value, str):
        raise ConfigError(
            "duration must be seconds or a form such as 2h, 30m, or 0; "
            f"got {type(value).__name__}"
        )
    text = value.strip()
    if _BARE_NUMBER_RE.fullmatch(text):
        return float(text)
    match = _SUFFIXED_DURATION_RE.fullmatch(text)
    if match is None:
        raise ConfigError(f"invalid duration {value!r}: use seconds or a form such as 2h, 30m, or 0")
    return float(match.group(1)) * DURATION_UNITS[match.group(2).lower()]


# Resolved through parse_duration so the string and numeric defaults of a key
# can never disagree.
DEFAULT_SETTLE_WINDOW_SECONDS = parse_duration(DEFAULT_SETTLE_WINDOW)
DEFAULT_RETENTION_SECONDS = parse_duration(DEFAULT_RETENTION)


@dataclass(frozen=True)
class TwillConfig:
    """The effective configuration: every key resolved to a working value.

    ``artifacts_root`` is the one setting with no default (plan §3): a
    config cannot exist without naming where distilled artifacts go, so a
    direct construction states its destination as deliberately as a file
    does.
    """

    artifacts_root: Path
    settle_window: float = DEFAULT_SETTLE_WINDOW_SECONDS
    retention: float = DEFAULT_RETENTION_SECONDS
    top_k: int = DEFAULT_TOP_K
    source_globs: tuple[str, ...] = DEFAULT_SOURCE_GLOBS
    content_fences: tuple[str, ...] = DEFAULT_CONTENT_FENCES
    model: str = DEFAULT_MODEL

    def require_artifacts_root(self, repo_root: Path | None = None) -> Path:
        """Return ``artifacts_root``: the gate every artifact writer uses.

        :func:`load_config` already refused an unset or in-tree value at
        startup; this re-checks a configuration built in code rather than
        loaded, so no writer can inherit a destination that was never
        validated (plan §3, §7.2).
        """

        if self.artifacts_root is None:
            # The field is typed Path and load_config never leaves it unset;
            # this guards a TwillConfig built in code with the destination
            # missing, which has no default to fall back to.
            raise ConfigError(
                "artifacts_root is not set and it has no default",
                "set artifacts_root in the TWILL config file to a directory "
                "outside the TWILL repository tree",
            )
        _ensure_outside_repo(self.artifacts_root, _repo_tree(repo_root))
        return self.artifacts_root


def load_config(path: Path | None = None, repo_root: Path | None = None) -> TwillConfig:
    """Load the config file, defaulting every key it does not set.

    A file that cannot be parsed, names an unknown key, or assigns a bad
    value raises :class:`ConfigError`, and so does an ``artifacts_root`` that
    is unset — including when the whole file is missing — or that resolves
    inside the repository tree (plan §3, §13.1): all of them are startup
    errors raised before any verb opens a file.
    """

    config_path = Path(path).expanduser() if path is not None else CONFIG_PATH.expanduser()
    values: dict[str, object] = {}
    if config_path.is_file():
        try:
            with config_path.open("rb") as handle:
                values = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{config_path}: invalid TOML: {exc}") from exc
        if not isinstance(values, dict):  # pragma: no cover - tomllib always returns a table
            raise ConfigError(f"{config_path}: top level must be a table")
    return _build_config(values, config_path, _repo_tree(repo_root))


def _build_config(values: dict[str, object], config_path: Path, repo: Path) -> TwillConfig:
    unknown = sorted(set(values) - _KNOWN_KEYS)
    if unknown:
        raise ConfigError(
            f"{config_path}: unknown key(s): {', '.join(unknown)}",
            f"known keys are: {', '.join(sorted(_KNOWN_KEYS))}",
        )
    return TwillConfig(
        settle_window=_duration("settle_window", values, config_path),
        retention=_duration("retention", values, config_path),
        top_k=_positive_int("top_k", values, config_path),
        source_globs=_string_list("source_globs", values, config_path),
        content_fences=_string_list("content_fences", values, config_path),
        model=_non_empty_string("model", values, config_path),
        artifacts_root=_artifacts_root("artifacts_root", values, config_path, repo),
    )


def _duration(key: str, values: dict[str, object], config_path: Path) -> float:
    try:
        return parse_duration(values.get(key, _default_for(key)))
    except ConfigError as exc:
        hint = exc.hint or "fix the value in the TWILL config file and try again"
        raise ConfigError(f"{config_path}: {key}: {exc.message}", hint) from exc


def _positive_int(key: str, values: dict[str, object], config_path: Path) -> int:
    value = values.get(key, _default_for(key))
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{config_path}: {key} must be a positive integer")
    if value < 1:
        raise ConfigError(f"{config_path}: {key} must be at least 1")
    return value


def _string_list(key: str, values: dict[str, object], config_path: Path) -> tuple[str, ...]:
    value = values.get(key, _default_for(key))
    if isinstance(value, str) or not isinstance(value, list):
        # A bare string would otherwise iterate character by character.
        raise ConfigError(f"{config_path}: {key} must be a list of strings")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{config_path}: {key} entries must be non-empty strings")
    return tuple(value)


def _non_empty_string(key: str, values: dict[str, object], config_path: Path) -> str:
    value = values.get(key, _default_for(key))
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{config_path}: {key} must be a non-empty string")
    return value


def _artifacts_root(
    key: str, values: dict[str, object], config_path: Path, repo: Path
) -> Path:
    value = values.get(key)
    if value is None:
        # A missing file and a file that omits the key are the same error:
        # there is no working default to fall back to (plan §3, §13.1).
        raise ConfigError(
            f"{config_path}: {key} is not set and it has no default",
            f"set {key} in the TWILL config file to a directory outside "
            "the TWILL repository tree",
        )
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{config_path}: {key} must be a path string")
    root = Path(value).expanduser().resolve()
    try:
        _ensure_outside_repo(root, repo)
    except ConfigError as exc:
        raise ConfigError(f"{config_path}: {key}: {exc.message}", exc.hint) from exc
    return root


def _ensure_outside_repo(root: Path, repo: Path) -> None:
    resolved = root.expanduser().resolve()
    if resolved == repo or repo in resolved.parents:
        raise ConfigError(
            f"{resolved} resolves inside the TWILL repository tree ({repo})",
            "this repository is public; distilled artifacts belong outside it "
            "in their own private one (plan §3)",
        )


def _repo_tree(repo_root: Path | None = None) -> Path:
    """Resolve the repository tree that must never hold artifacts."""

    if repo_root is not None:
        return Path(repo_root).resolve()
    return Path(__file__).resolve().parent


def _default_for(key: str) -> object:
    return {
        "settle_window": DEFAULT_SETTLE_WINDOW,
        "retention": DEFAULT_RETENTION,
        "top_k": DEFAULT_TOP_K,
        "source_globs": list(DEFAULT_SOURCE_GLOBS),
        "content_fences": list(DEFAULT_CONTENT_FENCES),
        "model": DEFAULT_MODEL,
    }[key]
