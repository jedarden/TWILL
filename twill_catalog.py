#!/usr/bin/env python3
"""Read the optional ICG always/never catalog export.

ICG owns the policy surface.  TWILL invokes its structured catalog command and
never reads the rule-pack files directly.  The export is deliberately an
optional input: an absent binary, a failed command, or an export whose format
TWILL does not know is reported as unavailable so transcript-only detection
can continue.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence


CATALOG_FORMAT = "icg-catalog/v2"
LEGACY_CATALOG_FORMAT = "icg-catalog/v1"
CATALOG_COMMAND = ("icg", "catalog", "--json")
READER_NAME = "icg_catalog"
CATALOG_DIGEST_META_KEY = "icg_catalog_digest"
CATALOG_FORMAT_META_KEY = "icg_catalog_format"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_EVENTS = 10_000
_MAX_JSON_BYTES = 16 * 1024 * 1024

_TOP_LEVEL_KEYS = frozenset(
    {"format", "catalog_digest", "icg_version", "pack_source", "never", "always"}
)
_PACK_SOURCE_KEYS = frozenset({"origin", "root", "trusted_ref"})
_NEVER_KEYS = frozenset(
    {
        "id",
        "pack",
        "severity",
        "tier",
        "action",
        "destructive",
        "enabled",
        "check",
        "match",
        "explanation",
        "sanctioned_alternative",
    }
)
_ALWAYS_KEYS = frozenset({"id", "pack", "check", "match"})
_ALTERNATIVE_KEYS = frozenset({"reason", "rewrite"})
_CHECKS = frozenset({"command_regex", "content_regex", "predicate"})
_SEVERITIES = frozenset({"Critical", "High", "Medium"})
_TIERS = frozenset({"tier1", "tier2", "tier3"})
_ACTIONS = frozenset({"deny", "updated_input", "additional_context"})


class CatalogError(ValueError):
    """The external command returned a catalog that violates its contract."""


class CatalogUnavailable(RuntimeError):
    """The optional catalog command could not provide an export."""


@dataclass(frozen=True)
class CatalogEvent:
    """One event from either side of the ICG catalog."""

    id: str
    pack: str
    check: str
    matching: Mapping[str, str]
    severity: str | None = None
    tier: str | None = None
    action: str | None = None
    destructive: bool | None = None
    enabled: bool | None = None
    explanation: str | None = None
    sanctioned_alternative: Mapping[str, str] | None = None

    @property
    def identity(self) -> tuple[str, str]:
        """The stable denial/catalog identity: (pack, id)."""

        return self.pack, self.id

    @property
    def match(self) -> Mapping[str, str]:
        """Compatibility spelling matching the wire field name."""

        return self.matching

    @property
    def is_never(self) -> bool:
        return self.severity is not None

    def as_dict(self) -> dict[str, object]:
        """Return the contract-shaped event, preserving producer field order."""

        result: dict[str, object] = {
            "id": self.id,
            "pack": self.pack,
        }
        if self.is_never:
            result.update(
                {
                    "severity": self.severity,
                    "tier": self.tier,
                    "action": self.action,
                    "destructive": self.destructive,
                    "enabled": self.enabled,
                }
            )
        result["check"] = self.check
        result["match"] = dict(self.matching)
        if self.is_never:
            result["explanation"] = self.explanation
            alternative = self.sanctioned_alternative or {}
            result["sanctioned_alternative"] = {"reason": alternative["reason"]}
            if "rewrite" in alternative:
                result["sanctioned_alternative"]["rewrite"] = alternative["rewrite"]
        return result


@dataclass(frozen=True)
class Catalog:
    """A validated, versioned ICG catalog."""

    format: str
    catalog_digest: str
    icg_version: str
    pack_source: Mapping[str, object]
    never: tuple[CatalogEvent, ...]
    always: tuple[CatalogEvent, ...]

    @property
    def digest(self) -> str:
        """Short alias used by state tracking and detector callers."""

        return self.catalog_digest

    @property
    def events(self) -> tuple[CatalogEvent, ...]:
        """All events, with never events first as on the wire."""

        return self.never + self.always


@dataclass(frozen=True)
class CatalogRead:
    """A non-throwing result for the optional external input."""

    status: str
    catalog: Catalog | None = None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.catalog is not None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"status": self.status, "available": self.available}
        if self.catalog is not None:
            result.update(
                {
                    "format": self.catalog.format,
                    "catalog_digest": self.catalog.catalog_digest,
                    "never": len(self.catalog.never),
                    "always": len(self.catalog.always),
                }
            )
        if self.reason:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True)
class CatalogObservation:
    """The digest transition observed while loading one catalog."""

    status: str
    format: str | None = None
    digest: str | None = None
    previous_format: str | None = None
    previous_digest: str | None = None

    @property
    def drifted(self) -> bool:
        return self.status == "drifted"

    def as_dict(self) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "status": self.status,
                "format": self.format,
                "catalog_digest": self.digest,
                "previous_format": self.previous_format,
                "previous_digest": self.previous_digest,
                "drifted": self.drifted,
            }.items()
            if value is not None or key == "drifted"
        }


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError(f"duplicate catalog key: {key}")
        result[key] = value
    return result


def _string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise CatalogError(f"{field} must be a non-empty string")
    return value


def _mapping(value: object, field: str, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise CatalogError(f"{field} fields do not match the catalog contract")
    return value


def _match(value: object, check: str, field: str) -> dict[str, str]:
    expected = "predicate" if check == "predicate" else "regex"
    raw = _mapping(value, field, frozenset({expected}))
    return {expected: _string(raw[expected], f"{field}.{expected}")}


def _event(value: object, *, never: bool) -> CatalogEvent:
    keys = _NEVER_KEYS if never else _ALWAYS_KEYS
    raw = _mapping(value, "catalog event", keys)
    event_id = _string(raw["id"], "event.id")
    pack = _string(raw["pack"], "event.pack")
    check = _string(raw["check"], "event.check")
    if check not in _CHECKS:
        raise CatalogError(f"event.check has unsupported value: {check}")
    matching = _match(raw["match"], check, "event.match")
    if not never:
        return CatalogEvent(event_id, pack, check, matching)

    severity = _string(raw["severity"], "event.severity")
    if severity not in _SEVERITIES:
        raise CatalogError(f"event.severity has unsupported value: {severity}")
    tier = _string(raw["tier"], "event.tier")
    if tier not in _TIERS:
        raise CatalogError(f"event.tier has unsupported value: {tier}")
    action = _string(raw["action"], "event.action")
    if action not in _ACTIONS:
        raise CatalogError(f"event.action has unsupported value: {action}")
    explanation = _string(raw["explanation"], "event.explanation")
    for field in ("destructive", "enabled"):
        if not isinstance(raw[field], bool):
            raise CatalogError(f"event.{field} must be boolean")
    if not isinstance(raw["sanctioned_alternative"], dict):
        raise CatalogError("event.sanctioned_alternative must be an object")
    alternative = raw["sanctioned_alternative"]
    if frozenset(alternative) not in {
        frozenset({"reason"}),
        _ALTERNATIVE_KEYS,
    }:
        raise CatalogError(
            "event.sanctioned_alternative fields do not match the catalog contract"
        )
    _string(alternative["reason"], "event.sanctioned_alternative.reason")
    if "rewrite" in alternative and not isinstance(alternative["rewrite"], str):
        raise CatalogError("event.sanctioned_alternative.rewrite must be a string")
    return CatalogEvent(
        event_id,
        pack,
        check,
        matching,
        severity=severity,
        tier=tier,
        action=action,
        destructive=raw["destructive"],
        enabled=raw["enabled"],
        explanation=explanation,
        sanctioned_alternative=alternative,
    )


def _events(value: object, *, never: bool) -> tuple[CatalogEvent, ...]:
    if not isinstance(value, list) or len(value) > _MAX_EVENTS:
        raise CatalogError("catalog event list is missing or too large")
    events = tuple(_event(item, never=never) for item in value)
    identities = [event.identity for event in events]
    if identities != sorted(identities):
        side = "never" if never else "always"
        raise CatalogError(f"catalog {side} events are not ordered by (pack, id)")
    if len(set(identities)) != len(identities):
        side = "never" if never else "always"
        raise CatalogError(f"catalog {side} events contain duplicate (pack, id) identities")
    return events


def _canonical_content(catalog_format: str, never: Sequence[CatalogEvent], always: Sequence[CatalogEvent]) -> bytes:
    content = {
        "format": catalog_format,
        "never": [event.as_dict() for event in never],
        "always": [event.as_dict() for event in always],
    }
    return json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def canonical_digest(catalog: Catalog) -> str:
    """Compute the ICG digest for a validated catalog."""

    return hashlib.sha256(
        _canonical_content(catalog.format, catalog.never, catalog.always)
    ).hexdigest()


def parse_catalog(value: object) -> Catalog:
    """Validate one ICG catalog document without reading rule packs."""

    if not isinstance(value, dict):
        raise CatalogError("catalog must be an object")
    catalog_format = _string(value.get("format"), "catalog.format")
    # v1 is still emitted by installed ICG releases; v2 adds pack_source.
    # Dispatch on format so a future shape is never guessed as either one.
    if catalog_format not in (LEGACY_CATALOG_FORMAT, CATALOG_FORMAT):
        raise CatalogError(f"unsupported catalog format: {catalog_format}")
    keys = _TOP_LEVEL_KEYS
    if catalog_format == LEGACY_CATALOG_FORMAT:
        keys = keys - {"pack_source"}
    raw = _mapping(value, "catalog", frozenset(keys))
    digest = _string(raw["catalog_digest"], "catalog.catalog_digest")
    if not _DIGEST_RE.fullmatch(digest):
        raise CatalogError("catalog.catalog_digest must be lowercase SHA-256 hex")
    icg_version = _string(raw["icg_version"], "catalog.icg_version")
    pack_source: Mapping[str, object] = {}
    if catalog_format == CATALOG_FORMAT:
        pack_source = _mapping(raw["pack_source"], "catalog.pack_source", _PACK_SOURCE_KEYS)
        if _string(pack_source["origin"], "catalog.pack_source.origin") not in {
            "explicit",
            "installed",
            "repository",
        }:
            raise CatalogError("catalog.pack_source.origin is unsupported")
        if pack_source["root"] is not None and not isinstance(pack_source["root"], str):
            raise CatalogError("catalog.pack_source.root must be a string or null")
        if (
            pack_source["trusted_ref"] is not None
            and not isinstance(pack_source["trusted_ref"], str)
        ):
            raise CatalogError("catalog.pack_source.trusted_ref must be a string or null")
    never = _events(raw["never"], never=True)
    always = _events(raw["always"], never=False)
    expected = hashlib.sha256(_canonical_content(catalog_format, never, always)).hexdigest()
    if digest != expected:
        raise CatalogError("catalog_digest does not match the exported event set")
    return Catalog(catalog_format, digest, icg_version, pack_source, never, always)


def _default_runner(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, **kwargs)


def read_catalog(
    command: Sequence[str] = CATALOG_COMMAND,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _default_runner,
    timeout: float = 5.0,
) -> Catalog:
    """Run ICG's read-only export command and return a validated catalog."""

    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("catalog command must be a non-empty string sequence")
    try:
        completed = runner(
            tuple(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        raise CatalogUnavailable(f"catalog command unavailable: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        suffix = f": {detail[0][:240]}" if detail else ""
        raise CatalogUnavailable(
            f"catalog command exited {completed.returncode}{suffix}"
        )
    output = completed.stdout or ""
    if len(output.encode("utf-8", "replace")) > _MAX_JSON_BYTES:
        raise CatalogError("catalog export is too large")
    try:
        value = json.loads(output, object_pairs_hook=_strict_object)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"catalog export is not valid JSON: {exc}") from exc
    return parse_catalog(value)


def load_catalog(
    command: Sequence[str] = CATALOG_COMMAND,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _default_runner,
    timeout: float = 5.0,
) -> Catalog | None:
    """Return the catalog when available; otherwise degrade to ``None``."""

    try:
        return read_catalog(command, runner=runner, timeout=timeout)
    except (CatalogUnavailable, CatalogError, ValueError):
        return None


def read_catalog_status(
    command: Sequence[str] = CATALOG_COMMAND,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _default_runner,
    timeout: float = 5.0,
) -> CatalogRead:
    """Read the optional input while retaining a safe operator-facing reason."""

    try:
        return CatalogRead("available", read_catalog(command, runner=runner, timeout=timeout))
    except CatalogUnavailable as exc:
        return CatalogRead("absent", reason=str(exc))
    except CatalogError as exc:
        reason = str(exc)
        status = "unsupported" if reason.startswith("unsupported catalog format:") else "invalid"
        return CatalogRead(status, reason=reason)
    except ValueError as exc:
        return CatalogRead("absent", reason=str(exc))


def catalog_live() -> bool:
    """Return whether the current ICG export is a supported valid catalog."""

    return read_catalog_status().available


def register_reader() -> object:
    """Build the reader registration without mutating the global registry."""

    from twill_reader import ReaderRegistration

    return ReaderRegistration(READER_NAME, catalog_live)


def _meta_value(connection: object, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def record_catalog(
    connection: object,
    catalog: Catalog,
    *,
    now: datetime | str | None = None,
) -> CatalogObservation:
    """Store the last-seen format/digest and classify first, same, or drifted."""

    previous_digest = _meta_value(connection, CATALOG_DIGEST_META_KEY)
    previous_format = _meta_value(connection, CATALOG_FORMAT_META_KEY)
    if previous_digest is None:
        status = "first_seen"
    elif previous_digest == catalog.catalog_digest and previous_format == catalog.format:
        status = "unchanged"
    else:
        status = "drifted"
    if now is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    elif isinstance(now, datetime):
        timestamp = now.isoformat()
    else:
        timestamp = str(now)
    with connection:
        connection.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (CATALOG_DIGEST_META_KEY, catalog.catalog_digest, timestamp),
        )
        connection.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (CATALOG_FORMAT_META_KEY, catalog.format, timestamp),
        )
    return CatalogObservation(
        status,
        format=catalog.format,
        digest=catalog.catalog_digest,
        previous_format=previous_format,
        previous_digest=previous_digest,
    )


def load_and_record(
    connection: object,
    *,
    command: Sequence[str] = CATALOG_COMMAND,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _default_runner,
    timeout: float = 5.0,
) -> tuple[CatalogRead, CatalogObservation | None]:
    """Load the optional catalog and update drift state only on success."""

    result = read_catalog_status(command, runner=runner, timeout=timeout)
    observation = record_catalog(connection, result.catalog) if result.catalog else None
    return result, observation


# The observation kinds that prove a command executed: the gate sits at the
# PreToolUse boundary, so any of these — succeeded, failed, or result-unknown —
# means the call got past it.  Denials and rejections never produce these
# kinds, so a matching row cannot double-count a stopped attempt.
GATE_GAP_KINDS = ("run", "run_succeeded", "run_failed")


def gate_gap_coverage(catalog: Catalog) -> tuple[int, int]:
    """Count enforced blocking events D-10 can and cannot inspect.

    Transcript observations retain command text, but not the complete
    content or engine state needed to replay content_regex and predicate
    checks. Surface that limit so zero matches are not mistaken for proof
    that every ICG rule held.
    """

    ambiguous_packs = {
        event.pack for event in catalog.always
        if event.check != "command_regex"
    }
    candidates = [
        event for event in catalog.never
        if event.enabled is True and event.action == "deny"
    ]
    supported = sum(
        event.check == "command_regex" and event.pack not in ambiguous_packs
        for event in candidates
    )
    return supported, len(candidates) - supported


def _gate_gap_allow_matchers(
    catalog: Catalog,
) -> tuple[dict[str, list[re.Pattern[str]]], set[str]]:
    """Compile same-pack exceptions; hide packs with opaque allow checks."""

    matchers: dict[str, list[re.Pattern[str]]] = {}
    ambiguous_packs: set[str] = set()
    for event in catalog.always:
        if event.check != "command_regex":
            ambiguous_packs.add(event.pack)
            continue
        try:
            compiled = re.compile(event.matching["regex"])
        except re.error as exc:
            raise CatalogError(
                f"always event {event.pack}/{event.id} carries a regex TWILL "
                f"cannot evaluate: {exc}"
            ) from exc
        matchers.setdefault(event.pack, []).append(compiled)
    return matchers, ambiguous_packs


def _gate_gap_matchers(catalog: Catalog) -> list[tuple[CatalogEvent, re.Pattern[str]]]:
    """Compile the never events D-10 can actually hold against execution.

    Three narrowing rules, each pinned by the ICG contract: an event the
    engine does not enforce (``enabled = false``) is cataloged visibility,
    not a hole in the gate; an event the gate answers without blocking
    (``additional_context``) executed by design; and ``updated_input``
    rewrites the input before execution, while the transcript records the
    invocation as the agent wrote it — so a rewritten force-push would read
    as a gap.  ``content_regex`` subjects are not stored (excerpts are
    post-redaction and bounded) and ``predicate`` logic is engine-side code
    the catalog says a consumer cannot re-implement.
    """

    matchers: list[tuple[CatalogEvent, re.Pattern[str]]] = []
    for event in catalog.never:
        if event.action != "deny" or event.check != "command_regex":
            continue
        if event.enabled is not True:
            continue
        pattern = event.matching["regex"]
        try:
            # The packs' flavor is plain Rust regex — anchors, classes,
            # inline flags — which Python's re accepts as-is; anything it
            # cannot compile raises and names the event rather than
            # silently matching nothing.
            compiled = re.compile(pattern)
        except re.error as exc:
            raise CatalogError(
                f"never event {event.pack}/{event.id} carries a regex TWILL "
                f"cannot evaluate: {exc}"
            ) from exc
        matchers.append((event, compiled))
    return matchers


def materialize_gate_gaps(
    connection: object,
    catalog: Catalog,
    *,
    window_days: int,
    now: datetime | None = None,
) -> int:
    """Refresh D-10's evidence table from a valid catalog and the corpus.

    One row lands per executed observation inside the trailing window whose
    command matched a deny-channel ``command_regex`` never event and no
    same-pack always exception. A pack with an opaque always check is omitted
    conservatively because the transcript cannot prove its exception failed.
    The whole
    table is replaced in one transaction, so evidence is always attributed to
    exactly the catalog passed in — never a blend of two digests.  Returns
    the number of matches written.
    """

    if not isinstance(window_days, int) or isinstance(window_days, bool):
        raise ValueError("window_days must be an integer")
    if window_days < 1:
        raise ValueError(f"window_days must be at least 1: {window_days}")
    reference = now if now is not None else datetime.now(timezone.utc)
    window_start = (reference - timedelta(days=window_days)).isoformat()
    matchers = _gate_gap_matchers(catalog)
    allow_matchers, ambiguous_packs = _gate_gap_allow_matchers(catalog)
    placeholders = ", ".join("?" for _ in GATE_GAP_KINDS)
    rows = connection.execute(
        f"""
        SELECT obs_id, command FROM observation
        WHERE ts_utc >= ?
          AND kind IN ({placeholders})
          AND command IS NOT NULL
          AND trim(command) <> ''
        ORDER BY obs_id
        """,
        (window_start, *GATE_GAP_KINDS),
    ).fetchall()
    evidence: list[tuple[str, str, int]] = []
    for obs_id, command in rows:
        allowed_packs = {
            pack for pack, patterns in allow_matchers.items()
            if any(pattern.search(command) for pattern in patterns)
        }
        for event, compiled in matchers:
            if event.pack in allowed_packs or event.pack in ambiguous_packs:
                continue
            if compiled.search(command):
                evidence.append((event.pack, event.id, int(obs_id)))
    with connection:
        connection.execute("DELETE FROM gate_gap")
        connection.executemany(
            "INSERT INTO gate_gap(pack, id, obs_id, catalog_digest) "
            "VALUES (?, ?, ?, ?)",
            (
                (pack, event_id, obs_id, catalog.catalog_digest)
                for pack, event_id, obs_id in evidence
            ),
        )
    return len(evidence)
