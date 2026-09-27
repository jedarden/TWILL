"""Build the durable weekly cluster series used by trend analysis."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Sequence

import twill_detectors
from twill_detectors import Detector, read_clusters, read_weekly_counts


WEEKLY_HISTORY_DAYS = 180
_REFERENCE_TABLES = ("observation", "rule_doc", "session_usage")
_WEEKLY_UPSERT = """
INSERT INTO cluster_week(detector_id, key, week, sessions, events)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(detector_id, key, week) DO UPDATE SET
  sessions=excluded.sessions, events=excluded.events
"""


def _clock(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("now must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError("now must be an ISO-8601 timestamp or datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _positive_days(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("history_days must be a positive integer")
    return value


def _monday(value: datetime) -> datetime:
    return (value - timedelta(days=value.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _week_label(value: datetime) -> str:
    iso_year, iso_week, _ = value.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _copy_reference_table(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> None:
    columns = tuple(source.execute(f"PRAGMA table_info({_quote_identifier(table)})"))
    if not columns:
        return
    names = tuple(str(row[1]) for row in columns)
    declarations = ", ".join(
        f"{_quote_identifier(str(row[1]))} {str(row[2] or 'TEXT')}"
        for row in columns
    )
    target.execute(f"CREATE TABLE {_quote_identifier(table)} ({declarations})")
    quoted = ", ".join(_quote_identifier(name) for name in names)
    if table == "observation" and start is not None and end is not None:
        rows = source.execute(
            f"SELECT {quoted} FROM {_quote_identifier(table)} "
            "WHERE ts_utc >= ? AND ts_utc < ?",
            (start, end),
        )
    else:
        rows = source.execute(
            f"SELECT {quoted} FROM {_quote_identifier(table)}"
        )
    placeholders = ", ".join("?" for _ in names)
    target.executemany(
        f"INSERT INTO {_quote_identifier(table)} ({quoted}) VALUES ({placeholders})",
        rows,
    )


def _filtered_connection(
    source: sqlite3.Connection,
    start: str,
    end: str,
) -> sqlite3.Connection:
    target = sqlite3.connect(":memory:")
    try:
        for table in _REFERENCE_TABLES:
            _copy_reference_table(source, target, table, start=start, end=end)
        target.execute("PRAGMA query_only = ON")
        return target
    except BaseException:
        target.close()
        raise


def _has_reference_data(connection: sqlite3.Connection) -> bool:
    for table in _REFERENCE_TABLES:
        columns = connection.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        ).fetchall()
        if not columns:
            continue
        if connection.execute(
            f"SELECT 1 FROM {_quote_identifier(table)} LIMIT 1"
        ).fetchone() is not None:
            return True
    return False


def _filtered_has_detector_data(
    connection: sqlite3.Connection, detector: Detector
) -> bool:
    if connection.execute("SELECT 1 FROM observation LIMIT 1").fetchone() is not None:
        return True
    for table in ("rule_doc", "session_usage"):
        if table in detector.cluster_sql and connection.execute(
            f"SELECT 1 FROM {table} LIMIT 1"
        ).fetchone() is not None:
            return True
    return False


def _replay_by_week(
    connection: sqlite3.Connection,
    detector: Detector,
    start: datetime,
    end: datetime,
) -> dict[tuple[str, str], tuple[int, int]]:
    counts: dict[tuple[str, str], tuple[int, int]] = {}
    if not _has_reference_data(connection):
        return counts
    first = _monday(start)
    last = _monday(end)
    while first <= last:
        week_start = max(first, start)
        week_end = min(first + timedelta(days=7), end)
        filtered = _filtered_connection(
            connection,
            week_start.isoformat(),
            (week_end + timedelta(microseconds=1)).isoformat(),
        )
        try:
            if not _filtered_has_detector_data(filtered, detector):
                first += timedelta(days=7)
                continue
            emitted = read_clusters(
                filtered,
                detector,
                window_start_utc=first.isoformat(),
                window_days=7,
            )
        finally:
            filtered.close()
        label = _week_label(first)
        for key, values in emitted.items():
            # A detector may emit non-observation findings with zero counts
            # (D-09 is one example).  They are valid clusters, but they are
            # not weekly activity and do not belong in the rate series.
            if values[0] == 0 and values[1] == 0:
                continue
            counts[(key, label)] = (values[0], values[1])
        first += timedelta(days=7)
    return counts


def _weekly_counts(
    connection: sqlite3.Connection,
    detector: Detector,
    *,
    start: datetime,
    end: datetime,
    history_days: int,
) -> dict[tuple[str, str], tuple[int, int]]:
    if detector.weekly_hits_sql is not None:
        return read_weekly_counts(
            connection,
            detector,
            window_start_utc=start.isoformat(),
            window_days=history_days,
            window_end_utc=end.isoformat(),
        )
    return _replay_by_week(connection, detector, start, end)


def _persist_counts(
    connection: sqlite3.Connection,
    detector: Detector,
    counts: dict[tuple[str, str], tuple[int, int]],
    *,
    manage_transaction: bool,
) -> int:
    savepoint = "twill_cluster_week"
    owns_transaction = False
    started_transaction = False
    if connection.in_transaction:
        connection.execute(f"SAVEPOINT {savepoint}")
    else:
        connection.execute("BEGIN IMMEDIATE")
        started_transaction = True
        owns_transaction = manage_transaction
    try:
        connection.executemany(
            _WEEKLY_UPSERT,
            (
                (detector.detector_id, key, week, sessions, events)
                for (key, week), (sessions, events) in sorted(counts.items())
            ),
        )
        if owns_transaction:
            connection.commit()
        elif not started_transaction:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        if started_transaction:
            connection.rollback()
        elif connection.in_transaction:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    return len(counts)


def aggregate_detector_weeks(
    connection: sqlite3.Connection,
    detector: Detector,
    *,
    now: str | datetime | None = None,
    history_days: int = WEEKLY_HISTORY_DAYS,
    manage_transaction: bool = True,
) -> int:
    """Aggregate one detector's weekly sessions and events into ``cluster_week``."""

    days = _positive_days(history_days)
    current = _clock(now)
    start = current - timedelta(days=days)
    end = current + timedelta(microseconds=1)
    counts = _weekly_counts(
        connection,
        detector,
        start=start,
        end=end,
        history_days=days,
    )
    return _persist_counts(
        connection,
        detector,
        counts,
        manage_transaction=manage_transaction,
    )


def aggregate_cluster_weeks(
    connection: sqlite3.Connection,
    detector: Detector,
    *,
    now: str | datetime | None = None,
    history_days: int = WEEKLY_HISTORY_DAYS,
    manage_transaction: bool = True,
) -> int:
    """Compatibility name for aggregating one detector's weekly series."""

    return aggregate_detector_weeks(
        connection,
        detector,
        now=now,
        history_days=history_days,
        manage_transaction=manage_transaction,
    )


def refresh_cluster_weeks(
    connection: sqlite3.Connection,
    *,
    registry: Sequence[Detector] | None = None,
    only: Sequence[str] | None = None,
    now: str | datetime | None = None,
    history_days: int = WEEKLY_HISTORY_DAYS,
) -> int:
    """Refresh the weekly series for the selected detector registry."""

    active = twill_detectors.build_registry(
        *(tuple(registry) if registry is not None else twill_detectors.REGISTRY)
    )
    if only:
        active = twill_detectors.select_detectors(active, only)
    return sum(
        aggregate_detector_weeks(
            connection,
            detector,
            now=now,
            history_days=history_days,
        )
        for detector in active
    )


run_cluster_week_aggregation = refresh_cluster_weeks
aggregate_all_cluster_weeks = refresh_cluster_weeks


__all__ = [
    "WEEKLY_HISTORY_DAYS",
    "aggregate_all_cluster_weeks",
    "aggregate_cluster_weeks",
    "aggregate_detector_weeks",
    "refresh_cluster_weeks",
    "run_cluster_week_aggregation",
]
