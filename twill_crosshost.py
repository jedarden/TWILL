"""D-11: systemic signatures from accepted, host-local aggregate reports.

The importer owns validation and replacement of reports.  These table shapes
are its storage boundary; this module reads only accepted snapshots.  An
empty accepted report is still a live input, while an old state database
without these tables has no cross-host input yet.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from twill_detectors import Detector


DETECTOR_ID = "D-11"
SIGNATURE_SCHEME = "twill-normalized-signature-sha256/v1"
KEY_PREFIX = f"{SIGNATURE_SCHEME}:"
STALE_AFTER = timedelta(hours=72)

# The Phase 7 importer creates these tables in an additive schema migration.
# The accepted row is distinct from buckets: a healthy empty report contains
# no buckets, and must still make D-11 eligible for a run.
HOST_REPORT_DDL = (
    "CREATE TABLE IF NOT EXISTS host_report("
    "host_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, "
    "last_ingest_success_at TEXT NOT NULL, window_start TEXT NOT NULL, "
    "window_end TEXT NOT NULL, signature_scheme TEXT NOT NULL, "
    "report_sha256 TEXT NOT NULL)"
)
HOST_REPORT_BUCKET_DDL = (
    "CREATE TABLE IF NOT EXISTS host_report_bucket("
    "host_id TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, "
    "signature_scheme TEXT NOT NULL, signature_sha256 TEXT NOT NULL, "
    "sessions INTEGER NOT NULL, events INTEGER NOT NULL, "
    "first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, "
    "PRIMARY KEY(host_id, day, kind, signature_scheme, signature_sha256))"
)


def _has_tables(connection: sqlite3.Connection) -> bool:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('host_report', 'host_report_bucket')"
    ).fetchall()
    return {str(row[0]) for row in rows} == {"host_report", "host_report_bucket"}


def host_reports_imported(connection: sqlite3.Connection) -> bool:
    """Whether at least one compatible report was accepted, even if empty."""

    if not _has_tables(connection):
        return False
    return connection.execute(
        "SELECT 1 FROM host_report WHERE signature_scheme = ? LIMIT 1",
        (SIGNATURE_SCHEME,),
    ).fetchone() is not None


# A bucket is the smallest count the report can disclose.  The final day at
# the edge of a detect window cannot be divided into events before/after its
# start, so a day that overlaps the window contributes its whole capped count.
# A host is counted once across days and kinds.  Joining accepted snapshots
# prevents orphaned bucket rows from becoming findings.
CROSS_HOST_SIGNATURE_SQL = f"""
    SELECT '{KEY_PREFIX}' || b.signature_sha256 AS key,
           count(DISTINCT b.host_id) AS sessions,
           sum(b.events) AS events,
           min(b.first_seen) AS first_seen,
           max(b.last_seen) AS last_seen
    FROM host_report_bucket AS b
    JOIN host_report AS r ON r.host_id = b.host_id
      AND r.signature_scheme = b.signature_scheme
    WHERE b.signature_scheme = '{SIGNATURE_SCHEME}'
      AND b.day >= date(:window_start_utc)
      AND b.day <= date(:window_start_utc, '+' || :window_days || ' days')
      AND b.events > 0
    GROUP BY b.signature_scheme, b.signature_sha256
    HAVING count(DISTINCT b.host_id) >= 2
    ORDER BY sessions DESC, last_seen DESC, key ASC
"""


CROSS_HOST_SIGNATURE_WEEKLY_SQL = f"""
    WITH qualifying AS (
      SELECT b.signature_sha256
      FROM host_report_bucket AS b
      JOIN host_report AS r ON r.host_id = b.host_id
        AND r.signature_scheme = b.signature_scheme
      WHERE b.signature_scheme = '{SIGNATURE_SCHEME}'
        AND b.day >= date(:window_start_utc)
        AND b.day < date(:window_end_utc)
        AND b.events > 0
      GROUP BY b.signature_sha256
      HAVING count(DISTINCT b.host_id) >= 2
    )
    SELECT '{KEY_PREFIX}' || b.signature_sha256 AS key,
           strftime('%G-W%V', b.day) AS week,
           count(DISTINCT b.host_id) AS sessions,
           sum(b.events) AS events
    FROM host_report_bucket AS b
    JOIN qualifying AS q ON q.signature_sha256 = b.signature_sha256
    JOIN host_report AS r ON r.host_id = b.host_id
      AND r.signature_scheme = b.signature_scheme
    WHERE b.signature_scheme = '{SIGNATURE_SCHEME}'
      AND b.day >= date(:window_start_utc)
      AND b.day < date(:window_end_utc)
      AND b.events > 0
    GROUP BY b.signature_scheme, b.signature_sha256, strftime('%G-W%V', b.day)
    ORDER BY key ASC, week ASC
"""


CROSS_HOST_SIGNATURE = Detector(
    DETECTOR_ID,
    1,
    "normalized error signatures observed on at least two distinct hosts",
    CROSS_HOST_SIGNATURE_SQL,
    weekly_hits_sql=CROSS_HOST_SIGNATURE_WEEKLY_SQL,
)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no UTC offset")
    return parsed.astimezone(timezone.utc)


def _freshness(
    generated_at: str, last_ingest_success_at: str, now: datetime
) -> str:
    # The importer validates both timestamps.  A malformed stored value is
    # conservatively stale, never a claim that the host is healthy.
    try:
        generated = _parse_timestamp(generated_at)
        ingested = _parse_timestamp(last_ingest_success_at)
    except ValueError:
        return "stale_host"
    if ingested > generated or generated > now + timedelta(minutes=5):
        return "stale_host"
    if now - generated > STALE_AFTER or now - ingested > STALE_AFTER:
        return "stale_host"
    return "current"


def findings(
    connection: sqlite3.Connection, *, window_start_utc: str,
    window_days: int, now: datetime | None = None,
) -> list[dict[str, object]]:
    """Show accepted host/day evidence for each systemic signature.

    The importer and doctor own complete coverage checks.  This view reports
    freshness from the two accepted heartbeat timestamps and never describes
    a missing host as zero occurrences or claims fleet coverage is complete.
    """

    if not host_reports_imported(connection):
        return []
    current = now or datetime.now(timezone.utc)
    clusters = connection.execute(
        CROSS_HOST_SIGNATURE_SQL,
        {"window_start_utc": window_start_utc, "window_days": window_days},
    ).fetchall()
    output: list[dict[str, object]] = []
    for key, hosts, events, first_seen, last_seen in clusters:
        signature_sha256 = str(key).removeprefix(KEY_PREFIX)
        rows = connection.execute(
            "SELECT b.host_id, b.day, r.generated_at, "
            "r.last_ingest_success_at FROM host_report_bucket AS b "
            "JOIN host_report AS r ON r.host_id = b.host_id "
            "AND r.signature_scheme = b.signature_scheme "
            "WHERE b.signature_scheme = ? AND b.signature_sha256 = ? "
            "AND b.day >= date(?) "
            "AND b.day <= date(?, '+' || ? || ' days') AND b.events > 0 "
            "GROUP BY b.host_id, b.day ORDER BY b.host_id, b.day",
            (SIGNATURE_SCHEME, signature_sha256, window_start_utc, window_start_utc, window_days),
        ).fetchall()
        contributors: list[dict[str, object]] = []
        for host_id, day, generated_at, ingested_at in rows:
            if not contributors or contributors[-1]["host_id"] != host_id:
                contributors.append(
                    {
                        "host_id": host_id,
                        "days": [],
                        "freshness": _freshness(generated_at, ingested_at, current),
                    }
                )
            contributors[-1]["days"].append(day)
        output.append(
            {
                "key": key,
                "signature_scheme": SIGNATURE_SCHEME,
                "signature_sha256": signature_sha256,
                "hosts": hosts,
                "events": events,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "contributors": contributors,
            }
        )
    return output
