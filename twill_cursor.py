"""Per-file identity and offset bookkeeping (plan §6.1 `cursor`, §6.2 steps 2-6).

The cursor table answers one question for every transcript file ever seen:
*which bytes have already been turned into observations, and can the rest be
read as an append of those?*  Three facts decide it — the sha256 of the
first 4 KiB (rewrite-in-place detection), the file size, and the committed
``last_offset``:

- **EC-02 (grew):** identity held and ``size >= last_offset`` → resume at
  ``last_offset``, parse complete lines only, advance to the last newline.
- **EC-03 (shrank / rewritten):** ``size < last_offset`` or an identity
  mismatch → full reparse from 0; the caller deletes the session's derived
  rows in the same transaction that rewrites this row.
- **EC-04 (torn final line):** bytes after the last newline are left for the
  next run — the offset never advances past a line that may still be growing.
- **EC-05 (vanished):** ``path_missing`` is flagged by :func:`mark_missing`;
  observations are never deleted on absence.

The identity comparison hashes ``min(4096, cursor.size)`` bytes of the
current file — the exact span the stored digest covered — so a file that
merely grew past the 4 KiB boundary (previously hashed whole, now longer)
still resumes instead of reparsing spuriously: the comparison asks "did any
byte I already hashed change", which is precisely the append/rewrite
question.  A rewritten file that happens to keep its first 4 KiB is
indistinguishable from an append by design; §7.1 scopes identity to the
first 4 KiB and accepts that limit.

``mtime_ns`` is bookkeeping (staleness, doctor), never a decision input: an
append bumps it just as a rewrite does, so deciding on it would reparse
every growing file.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

#: The identity prefix length: sha256 of the first 4 KiB (plan §7.1).
IDENTITY_PREFIX_BYTES = 4096

#: Chunk size for the byte scans; large enough to amortize syscalls, small
#: enough to stay a rounding error next to the parser's own allocations.
_SCAN_CHUNK_BYTES = 1 << 20

ACTION_PARSE = "parse"
ACTION_RESUME = "resume"
ACTION_REPARSE = "reparse"

REASON_IDENTITY_CHANGED = "identity-changed"
REASON_SHRANK = "shrank"


@dataclass(frozen=True)
class FileFacts:
    """The stat-level identity of a transcript file as it stands now."""

    size: int
    mtime_ns: int
    identity_sha: str


@dataclass(frozen=True)
class CursorRow:
    """One row of the ``cursor`` table (plan §7.1)."""

    path: str
    session_id: str
    source: str
    identity_sha: str
    size: int
    mtime_ns: int
    last_offset: int
    parse_errors: int
    first_seen: str
    last_indexed_at: str
    path_missing: bool


_CURSOR_COLUMNS = (
    "path",
    "session_id",
    "source",
    "identity_sha",
    "size",
    "mtime_ns",
    "last_offset",
    "parse_errors",
    "first_seen",
    "last_indexed_at",
    "path_missing",
)


@dataclass(frozen=True)
class IngestPlan:
    """What to do with a file this run, decided before any line is parsed."""

    action: str
    reason: str | None
    start_offset: int
    #: Derived rows for the previous session_id must be deleted before the
    #: new span is written (EC-03's same-transaction replace).
    replace_session: bool


@dataclass(frozen=True)
class LineScan:
    """The complete lines found in one file region.

    ``lines`` carries whole-file line numbers, so appended spans number
    contiguously with the base parse.  ``new_offset`` is the byte position
    after the last newline in the region — the furthest point the cursor may
    advance to (plan §8.3 invariant).  ``pending_tail`` is true when bytes
    remain beyond it: a line still being written, counted as a parse error
    by the caller and revisited next run (EC-04).
    """

    start_offset: int
    lines: tuple[tuple[int, str], ...]
    new_offset: int
    pending_tail: bool

    @property
    def region_empty(self) -> bool:
        """True when the file offered nothing at all beyond the cursor.

        Distinguishes "nothing new happened" from "a torn tail is waiting":
        an empty region leaves the stored row untouched (``parse_errors`` is
        sticky across idle passes so `doctor`'s three-consecutive-runs
        signal can fire), a pending tail is recounted every pass.
        """

        return self.new_offset == self.start_offset and not self.pending_tail


def identity_hash(path: Path, length: int = IDENTITY_PREFIX_BYTES) -> str:
    """sha256 of the first ``min(length, size)`` bytes of ``path``."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(_SCAN_CHUNK_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def file_facts(path: Path) -> FileFacts:
    """Stat the file and hash its identity prefix."""

    stat = path.stat()
    return FileFacts(
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        identity_sha=identity_hash(path),
    )


def plan_ingest(row: CursorRow | None, path: Path, facts: FileFacts) -> IngestPlan:
    """Decide parse/resume/reparse from the stored row and current facts."""

    if row is None:
        return IngestPlan(ACTION_PARSE, None, 0, replace_session=False)
    if facts.size < row.last_offset:
        return IngestPlan(ACTION_REPARSE, REASON_SHRANK, 0, replace_session=True)
    # Compare over the span the stored digest covered, which for a file that
    # has since grown past 4 KiB is shorter than the current prefix.
    compared_bytes = min(IDENTITY_PREFIX_BYTES, row.size)
    if compared_bytes == min(IDENTITY_PREFIX_BYTES, facts.size):
        current_sha = facts.identity_sha
    else:
        current_sha = identity_hash(path, compared_bytes)
    if current_sha != row.identity_sha:
        return IngestPlan(
            ACTION_REPARSE, REASON_IDENTITY_CHANGED, 0, replace_session=True
        )
    return IngestPlan(ACTION_RESUME, None, row.last_offset, replace_session=False)


def scan_lines(path: Path, start_offset: int) -> LineScan:
    """Read complete lines from ``start_offset``; never consume a torn tail.

    ``start_offset`` must sit on a line boundary (the ``last_offset``
    invariant guarantees stored offsets do), so the lines before it are
    counted, not parsed, and the yielded numbers are whole-file line numbers.
    """

    lines: list[tuple[int, str]] = []
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if start_offset > size:
            raise ValueError(
                f"scan start {start_offset} is past end of file {path} ({size})"
            )
        line_number = _count_newlines(handle, start_offset)
        handle.seek(start_offset)
        carry = b""
        while True:
            chunk = handle.read(_SCAN_CHUNK_BYTES)
            if not chunk:
                break
            carry += chunk
            *complete, carry = carry.split(b"\n")
            for raw in complete:
                line_number += 1
                lines.append((line_number, raw.decode("utf-8", errors="replace")))
        pending_tail = bool(carry)
    return LineScan(
        start_offset=start_offset,
        lines=tuple(lines),
        new_offset=size - len(carry) if pending_tail else size,
        pending_tail=pending_tail,
    )


def _count_newlines(handle: BinaryIO, limit: int) -> int:
    count = 0
    remaining = limit
    while remaining > 0:
        chunk = handle.read(min(_SCAN_CHUNK_BYTES, remaining))
        if not chunk:
            break
        count += chunk.count(b"\n")
        remaining -= len(chunk)
    return count


def load_cursor(connection: sqlite3.Connection, path: str) -> CursorRow | None:
    """Fetch the cursor row for ``path``, or ``None`` when never seen."""

    row = connection.execute(
        f"SELECT {', '.join(_CURSOR_COLUMNS)} FROM cursor WHERE path = ?", (path,)
    ).fetchone()
    if row is None:
        return None
    return CursorRow(
        path=row[0],
        session_id=row[1],
        source=row[2],
        identity_sha=row[3],
        size=row[4],
        mtime_ns=row[5],
        last_offset=row[6],
        parse_errors=row[7],
        first_seen=row[8],
        last_indexed_at=row[9],
        path_missing=bool(row[10]),
    )


def upsert_cursor(
    connection: sqlite3.Connection,
    *,
    path: str,
    session_id: str,
    source: str,
    facts: FileFacts,
    last_offset: int,
    parse_errors: int,
    now: str,
) -> None:
    """Write the cursor row after a parsed span, inside the caller's transaction.

    ``first_seen`` is set only on insert; every later write updates the
    resume position and clears ``path_missing`` — the file demonstrably
    exists again.
    """

    connection.execute(
        "INSERT INTO cursor(path, session_id, source, identity_sha, size, mtime_ns, "
        "last_offset, parse_errors, first_seen, last_indexed_at, path_missing) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(path) DO UPDATE SET session_id=excluded.session_id, "
        "source=excluded.source, identity_sha=excluded.identity_sha, "
        "size=excluded.size, mtime_ns=excluded.mtime_ns, "
        "last_offset=excluded.last_offset, parse_errors=excluded.parse_errors, "
        "last_indexed_at=excluded.last_indexed_at, path_missing=0",
        (
            path,
            session_id,
            source,
            facts.identity_sha,
            facts.size,
            facts.mtime_ns,
            last_offset,
            parse_errors,
            now,
            now,
        ),
    )


def mark_missing(connection: sqlite3.Connection) -> int:
    """Flag vanished files ``path_missing`` (EC-05); return rows changed.

    Existence is checked per stored path — not against this run's candidate
    list — so a file skipped by the settle window or a narrowed glob is never
    mistaken for a vanished one.  Only the flag moves: observations and the
    resume position survive untouched, because absence is never a reason to
    delete evidence.
    """

    changed = 0
    rows = connection.execute(
        "SELECT path, path_missing FROM cursor", ()
    ).fetchall()
    for path_text, currently_missing in rows:
        missing = 0 if Path(path_text).is_file() else 1
        if bool(currently_missing) != bool(missing):
            connection.execute(
                "UPDATE cursor SET path_missing = ? WHERE path = ?",
                (missing, path_text),
            )
            changed += 1
    return changed
