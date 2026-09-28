#!/usr/bin/env python3
"""Index the rule corpus into ``rule_doc`` and ``rule_fts`` (plan §6.1, §7.1, Phase 3).

Library-only for now, like the transcript readers: the coverage matcher that
consumes this index ships with Phase 3's rank verb, which is its own bead.
Until then the test suite is this module's only caller.

The corpus is the set of files that already tell an agent how to behave:
MEMORY.md and its memory leaves, CLAUDE.md, repo ``AGENTS.md`` files, and
skills.  Each source is configured as ``<layer>:<glob>`` -- the layer is
written out because path shape is exactly the identity this module must not
depend on: EC-11 moves and renames rule files, and coverage matching is by
content hash + FTS, not path identity.

Identity is the sha256 of a file's whole bytes, and a re-index classifies
each discovered document against its stored ``rule_doc`` row:

- new path             -> ``indexed``.  A row whose sha matches an existing
  row adopts that row's ``last_read_by_agent``: identical content is the
  same rule, so its read history follows it across a rename instead of
  feeding D-09 a false "never read".
- sha changed          -> ``reindexed``.  The FTS row is replaced and
  ``stale`` cleared.
- sha held, layer not  -> ``relayered``.  Reclassification bookkeeping;
  ``indexed_at`` keeps the content's indexing time.
- sha and layer held   -> ``restored`` when the row was stale, else
  ``unchanged`` (no write at all, so re-indexing an unchanged corpus is
  idempotent down to ``indexed_at``).

A stored path that no longer exists is marked ``stale`` -- never deleted,
and its FTS text is kept -- so a rename degrades coverage to a visible,
doctor-reportable state instead of silently dropping it (EC-11).  A vanished
path whose sha matches a document discovered this run is reported as a move:
old row stale by path, content alive at the new path, joined by hash.  A
stored path that still exists but fell out of the configured globs is left
alone -- existence is checked per stored path, not against this run's
discovery, so a narrowed glob is never mistaken for a vanished file (the
same discipline as the cursor's EC-05 sweep).

The whole index lands in one transaction: a crash either keeps the previous
index or lands the new one, and the next run recomputes it from the files.
"""

from __future__ import annotations

import glob
import hashlib
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

#: Per-file cap, guarding the plan's peak-RSS budget (§12) against a
#: misconfigured glob indexing something huge.  The real corpus is two
#: orders of magnitude under this; a skipped file is reported, never silent.
MAX_RULE_DOC_BYTES = 4 * 1024 * 1024

#: The schema's layers (plan §7.1 ``rule_doc.layer``), in display order.
RULE_LAYERS = ("memory", "claude_md", "agents_md", "skill", "hook")

#: The codinghome corpus.  ``memory`` lists MEMORY.md explicitly and then its
#: leaves: dedup makes the overlap free, and an operator who later narrows
#: the leaf glob still keeps the index file.  Repos on this host are direct
#: children of the home directory, so one wildcard level reaches their
#: ``AGENTS.md`` and ``.claude/skills``; deeper trees are the operator's to
#: add.  A layer exists for hooks in the schema, but no hook file is indexed
#: by default -- the plan's Phase 3 corpus does not include one.
DEFAULT_RULE_GLOBS = (
    "memory:~/.claude/projects/*/memory/MEMORY.md",
    "memory:~/.claude/projects/*/memory/*.md",
    "claude_md:~/CLAUDE.md",
    "agents_md:~/*/AGENTS.md",
    "skill:~/.claude/skills/*/SKILL.md",
    "skill:~/*/.claude/skills/*/SKILL.md",
)


def content_sha(payload: bytes) -> str:
    """sha256 over the whole file -- not the cursor's 4 KiB identity prefix.

    A rule file is small and static compared with a transcript; a prefix
    hash would miss an edit past the boundary and wrongly call the content
    unchanged.
    """

    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class RulePattern:
    """One configured source: the layer it assigns and the glob that finds it."""

    layer: str
    pattern: str


def parse_rule_pattern(entry: str) -> RulePattern:
    """Parse ``<layer>:<glob>``; anything else is a loud configuration error."""

    layer, separator, pattern = entry.partition(":")
    if not separator:
        raise ValueError(
            f"rule glob {entry!r} must be written as <layer>:<glob> "
            f"(one of: {', '.join(RULE_LAYERS)})"
        )
    if layer not in RULE_LAYERS:
        raise ValueError(
            f"rule glob {entry!r} names unknown layer {layer!r}; "
            f"known layers are: {', '.join(RULE_LAYERS)}"
        )
    if not pattern.strip():
        raise ValueError(f"rule glob {entry!r} has an empty pattern after {layer!r}")
    return RulePattern(layer, pattern)


@dataclass(frozen=True)
class RuleDoc:
    """One discovered rule file, read and hashed."""

    path: Path  # resolved, so the stored row is stable across CWDs
    layer: str
    sha: str
    text: str


@dataclass(frozen=True)
class SkippedDoc:
    """A discovered file this index deliberately left out, with the reason."""

    path: Path
    reason: str


@dataclass(frozen=True)
class Discovery:
    docs: tuple[RuleDoc, ...]
    skipped: tuple[SkippedDoc, ...]


def _candidate_files(pattern: str) -> list[Path]:
    """Files a configured glob matches, without reading any of them.

    A literal file is accepted as-is (``~/CLAUDE.md`` has no magic), a
    literal directory means ``**/*.md`` beneath it, and a missing literal is
    simply no candidates -- an absent source file is a normal corpus state,
    not an error.
    """

    expanded = Path(pattern).expanduser()
    if expanded.is_file():
        return [expanded]
    if expanded.is_dir():
        return sorted(expanded.rglob("*.md"))
    if not glob.has_magic(str(expanded)):
        return []
    return [Path(match) for match in glob.iglob(str(expanded), recursive=True)]


def discover_docs(patterns: Sequence[str]) -> Discovery:
    """Walk the configured globs and read every distinct file they match.

    Patterns are applied in order and a file already discovered keeps the
    first layer that claimed it, so overlapping globs resolve
    deterministically.  Reading is bounded by :data:`MAX_RULE_DOC_BYTES`;
    unreadable and oversized files come back as skips with reasons, never as
    failures of the whole run.
    """

    docs: dict[str, RuleDoc] = {}
    skipped: list[SkippedDoc] = []
    for entry in patterns:
        rule_pattern = parse_rule_pattern(entry)
        for path in _candidate_files(rule_pattern.pattern):
            resolved = path.resolve()
            key = str(resolved)
            if key in docs:
                continue
            try:
                payload = resolved.read_bytes()
            except OSError as exc:
                skipped.append(
                    SkippedDoc(resolved, f"unreadable: {exc.strerror or exc}")
                )
                continue
            if len(payload) > MAX_RULE_DOC_BYTES:
                skipped.append(
                    SkippedDoc(
                        resolved,
                        f"larger than the {MAX_RULE_DOC_BYTES}-byte rule-doc cap",
                    )
                )
                continue
            docs[key] = RuleDoc(
                path=resolved,
                layer=rule_pattern.layer,
                sha=content_sha(payload),
                text=payload.decode("utf-8", errors="replace"),
            )
    ordered = tuple(sorted(docs.values(), key=lambda doc: str(doc.path)))
    return Discovery(ordered, tuple(skipped))


@dataclass(frozen=True)
class IndexReport:
    """What one indexing run changed, in sorted-path order throughout."""

    #: Discovered documents read successfully.
    docs: int
    indexed: tuple[str, ...]
    reindexed: tuple[str, ...]
    relayered: tuple[str, ...]
    restored: tuple[str, ...]
    unchanged: int
    #: Stored paths that vanished this run and are now ``stale``.
    vanished: tuple[str, ...]
    #: Stored paths that were already stale and are still gone.
    still_stale: int
    #: (vanished path, current paths carrying the same sha) -- EC-11's
    #: move-by-hash detection.
    moves: tuple[tuple[str, tuple[str, ...]], ...]
    skipped: tuple[SkippedDoc, ...]


@dataclass(frozen=True)
class RuleReadReport:
    """The result of materializing persisted ``file_read`` observations."""

    #: Number of observations that named a rule content hash.
    matched: int
    #: Rule rows whose durable marker advanced, in path order.
    updated: tuple[str, ...]


def _fts_replace(connection: sqlite3.Connection, path_text: str, text: str) -> None:
    """Point the FTS row for ``path_text`` at ``text``.

    Stale rows keep their text (EC-11: coverage degrades visibly, it is not
    dropped), so deletion happens only here -- when a live document's
    content changed and the old text no longer exists anywhere.
    """

    connection.execute("DELETE FROM rule_fts WHERE path = ?", (path_text,))
    connection.execute(
        "INSERT INTO rule_fts(text, path) VALUES (?, ?)", (text, path_text)
    )


def _last_read_by_sha(connection: sqlite3.Connection, sha: str) -> str | None:
    """Newest known read across every row carrying ``sha``."""

    row = connection.execute(
        "SELECT last_read_by_agent FROM rule_doc "
        "WHERE sha = ? AND last_read_by_agent IS NOT NULL "
        "ORDER BY last_read_by_agent DESC LIMIT 1",
        (sha,),
    ).fetchone()
    return row[0] if row else None


def _read_timestamp(value: object) -> tuple[datetime, str] | None:
    """Parse a read timestamp and render it in one comparable UTC form."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed, parsed.isoformat()


def _read_path_candidates(
    path: object,
    cwd: object,
    launch_dir: object,
) -> tuple[Path, ...]:
    """Resolve absolute and transcript-relative spellings of one read path."""

    if not isinstance(path, str) or not path.strip():
        return ()
    text = path.strip()
    raw = Path(text).expanduser()
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        for base in (cwd, launch_dir):
            if isinstance(base, str) and base.strip():
                candidates.append(Path(base).expanduser() / raw)
        # This is only a last-resort filesystem probe.  Indexed rows use
        # resolved paths, so relative reports should normally match one of the
        # transcript's stored cwd/launch-dir spellings above.
        candidates.append(raw)
    resolved: list[Path] = []
    for candidate in candidates:
        try:
            normalized = candidate.resolve()
        except OSError:
            normalized = candidate.absolute()
        if normalized not in resolved:
            resolved.append(normalized)
    return tuple(resolved)


def ingest_rule_reads(
    connection: sqlite3.Connection,
    *,
    manage_transaction: bool = True,
) -> RuleReadReport:
    """Materialize file_read observations into hash-based read markers.

    A reader reports a read as an observation containing the path the agent
    opened.  The path is only a lookup hint: an indexed row supplies the
    content hash even when the file has since moved or gone stale, while a
    live unindexed spelling is hashed from disk.  Every rule_doc row with
    that hash receives the newest timestamp, so duplicate live paths and
    stale rows preserve one shared read history.
    """

    rows = connection.execute(
        "SELECT path, sha, last_read_by_agent FROM rule_doc"
    ).fetchall()
    sha_by_path = {str(path): str(sha) for path, sha, _ in rows}
    latest_by_sha: dict[str, tuple[datetime, str]] = {}
    matched = 0

    observations = connection.execute(
        "SELECT path, cwd, launch_dir, ts_utc FROM observation "
        "WHERE kind = 'file_read' AND path IS NOT NULL"
    ).fetchall()
    for path, cwd, launch_dir, timestamp in observations:
        sha: str | None = None
        for candidate in _read_path_candidates(path, cwd, launch_dir):
            sha = sha_by_path.get(str(candidate))
            if sha is not None:
                break
        if sha is None:
            for candidate in _read_path_candidates(path, cwd, launch_dir):
                try:
                    payload = candidate.read_bytes()
                except OSError:
                    continue
                if len(payload) > MAX_RULE_DOC_BYTES:
                    continue
                candidate_sha = content_sha(payload)
                if candidate_sha in sha_by_path.values():
                    sha = candidate_sha
                    break
        parsed = _read_timestamp(timestamp)
        if sha is None or parsed is None:
            continue
        matched += 1
        previous = latest_by_sha.get(sha)
        if previous is None or parsed[0] > previous[0]:
            latest_by_sha[sha] = parsed

    updated: list[str] = []
    transaction = connection if manage_transaction else nullcontext()
    with transaction:
        for sha, (read_at, timestamp) in latest_by_sha.items():
            for path, last_read in connection.execute(
                "SELECT path, last_read_by_agent FROM rule_doc "
                "WHERE sha = ? ORDER BY path",
                (sha,),
            ).fetchall():
                previous = _read_timestamp(last_read)
                if previous is not None and previous[0] >= read_at:
                    continue
                connection.execute(
                    "UPDATE rule_doc SET last_read_by_agent = ? WHERE path = ?",
                    (timestamp, path),
                )
                updated.append(str(path))

    return RuleReadReport(matched=matched, updated=tuple(sorted(updated)))


def index_corpus(
    connection: sqlite3.Connection,
    patterns: Sequence[str],
    *,
    now: str | None = None,
    manage_transaction: bool = True,
) -> IndexReport:
    """Re-index the corpus described by ``patterns`` in one transaction.

    ``patterns`` are ``<layer>:<glob>`` entries as produced by
    :func:`parse_rule_pattern`; ``now`` stamps new and reindexed rows and is
    injectable for tests.  Every stored path is afterwards checked for
    existence so a vanished rule file becomes ``stale`` rather than
    silently absent (EC-11).  ``manage_transaction`` is for callers that
    already own an outer transaction; the default keeps the index atomic on
    its own.  Returns the per-action report.
    """

    if now is None:
        now = datetime.now(timezone.utc).isoformat()
    discovery = discover_docs(patterns)

    indexed: list[str] = []
    reindexed: list[str] = []
    relayered: list[str] = []
    restored: list[str] = []
    unchanged = 0
    vanished: list[str] = []
    still_stale = 0
    moves: list[tuple[str, tuple[str, ...]]] = []

    discovered_paths = {str(doc.path) for doc in discovery.docs}
    paths_by_sha: dict[str, tuple[str, ...]] = {}
    for doc in discovery.docs:
        paths_by_sha[doc.sha] = paths_by_sha.get(doc.sha, ()) + (str(doc.path),)

    transaction = connection if manage_transaction else nullcontext()
    with transaction:
        for doc in discovery.docs:
            path_text = str(doc.path)
            row = connection.execute(
                "SELECT layer, sha, stale FROM rule_doc WHERE path = ?",
                (path_text,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO rule_doc(path, layer, sha, indexed_at, last_read_by_agent, stale) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (
                        path_text,
                        doc.layer,
                        doc.sha,
                        now,
                        _last_read_by_sha(connection, doc.sha),
                    ),
                )
                _fts_replace(connection, path_text, doc.text)
                indexed.append(path_text)
                continue
            stored_layer, stored_sha, stored_stale = row
            content_changed = stored_sha != doc.sha
            layer_changed = stored_layer != doc.layer
            if content_changed:
                connection.execute(
                    "UPDATE rule_doc SET sha = ?, layer = ?, indexed_at = ?, stale = 0 "
                    "WHERE path = ?",
                    (doc.sha, doc.layer, now, path_text),
                )
                _fts_replace(connection, path_text, doc.text)
                reindexed.append(path_text)
            elif layer_changed:
                connection.execute(
                    "UPDATE rule_doc SET layer = ? WHERE path = ?",
                    (doc.layer, path_text),
                )
                relayered.append(path_text)
            elif stored_stale:
                connection.execute(
                    "UPDATE rule_doc SET stale = 0 WHERE path = ?", (path_text,)
                )
                restored.append(path_text)
            else:
                unchanged += 1

        # EC-11 sweep: existence per stored path, never per discovery, so a
        # narrowed glob cannot masquerade as a vanished file.
        for path_text, sha, stale in connection.execute(
            "SELECT path, sha, stale FROM rule_doc ORDER BY path"
        ).fetchall():
            if path_text in discovered_paths or Path(path_text).is_file():
                continue
            if stale:
                still_stale += 1
                continue
            connection.execute(
                "UPDATE rule_doc SET stale = 1 WHERE path = ?", (path_text,)
            )
            vanished.append(path_text)
            successors = paths_by_sha.get(sha, ())
            if successors:
                moves.append((path_text, successors))

        # Read reports are path-shaped observations, but their durable marker
        # is content-shaped.  Run this after indexing so a newly discovered
        # path can join an observation from the same or an earlier ingest.
        ingest_rule_reads(connection, manage_transaction=False)

    return IndexReport(
        docs=len(discovery.docs),
        indexed=tuple(sorted(indexed)),
        reindexed=tuple(sorted(reindexed)),
        relayered=tuple(sorted(relayered)),
        restored=tuple(sorted(restored)),
        unchanged=unchanged,
        vanished=tuple(sorted(vanished)),
        still_stale=still_stale,
        moves=tuple(moves),
        skipped=discovery.skipped,
    )


if __name__ == "__main__":
    raise SystemExit("module is library-only; import twill_rulecorpus")
