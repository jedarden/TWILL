"""Retention pruning for the derived TWILL corpus."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

import twill_detectors
from twill_config import DEFAULT_RETENTION_SECONDS
from twill_contract import EXIT_SUCCESS


DEFAULT_RETENTION_DAYS = 180
DEFAULT_ANALYSIS_WINDOW_DAYS = 30
_PRUNE_SAVEPOINT = "twill_prune"


@dataclass(frozen=True)
class PruneReport:
    """The result of one retention pass."""

    cutoff_utc: str
    retention_seconds: float
    retention_days: int
    pruned_observations: int
    remaining_observations: int
    clusters: int
    detector_report: twill_detectors.DetectorRunReport
    analysis_window_days: dict[str, int]
    committed: bool = True
    detector_refresh_committed: bool = True

    @property
    def deleted_observations(self) -> int:
        return self.pruned_observations

    @property
    def exit_code(self) -> int:
        return self.detector_report.exit_code

    def as_dict(self) -> dict[str, object]:
        return {
            "cutoff_utc": self.cutoff_utc,
            "retention_seconds": self.retention_seconds,
            "retention_days": self.retention_days,
            "pruned_observations": self.pruned_observations,
            "remaining_observations": self.remaining_observations,
            "clusters": self.clusters,
            "analysis_window_days": dict(sorted(self.analysis_window_days.items())),
            "committed": self.committed,
            "detector_refresh_committed": self.detector_refresh_committed,
            "detectors": [
                {
                    "detector_id": outcome.detector_id,
                    "version": outcome.version,
                    "full_id": outcome.full_id,
                    "status": outcome.status,
                    "clusters": outcome.clusters,
                    "error": outcome.error,
                }
                for outcome in self.detector_report.outcomes
            ],
        }


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


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = text.replace(",", ".")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _delete_expired(connection: sqlite3.Connection, cutoff: datetime) -> None:
    coarse_cutoff = cutoff - timedelta(days=1)
    connection.execute(
        "DELETE FROM observation WHERE julianday(ts_utc) IS NOT NULL "
        "AND julianday(ts_utc) < julianday(?)",
        (coarse_cutoff.isoformat(),),
    )
    candidates = connection.execute(
        "SELECT obs_id, ts_utc FROM observation "
        "WHERE julianday(ts_utc) IS NULL "
        "OR julianday(ts_utc) >= julianday(?)",
        (coarse_cutoff.isoformat(),),
    ).fetchall()
    expired = [
        (row[0],)
        for row in candidates
        if (parsed := _timestamp(row[1])) is not None and parsed < cutoff
    ]
    if expired:
        connection.executemany("DELETE FROM observation WHERE obs_id = ?", expired)


def _seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("retention must be a positive duration")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0:
        raise ValueError("retention must be a positive duration")
    try:
        duration = timedelta(seconds=resolved)
    except (OverflowError, ValueError) as exc:
        raise ValueError("retention is outside the supported range") from exc
    if duration <= timedelta(0):
        raise ValueError("retention is too small to represent")
    return resolved


def validate_retention(value: object) -> float:
    """Validate and normalize a retention duration in seconds."""

    return _seconds(value)


def _positive_window(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _prior_window(
    connection: sqlite3.Connection,
    detector: twill_detectors.Detector,
) -> int:
    try:
        row = connection.execute(
            "SELECT window_days FROM detector_run "
            "WHERE detector_id = ? AND version = ?",
            (detector.detector_id, detector.version),
        ).fetchone()
    except sqlite3.Error:
        row = None
    window = _positive_window(row[0]) if row is not None else None
    if window is not None:
        return window
    try:
        row = connection.execute(
            "SELECT window_days FROM cluster WHERE detector_id = ? "
            "ORDER BY window_days DESC LIMIT 1",
            (detector.detector_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    window = _positive_window(row[0]) if row is not None else None
    return window if window is not None else DEFAULT_ANALYSIS_WINDOW_DAYS


def _begin_scope(connection: sqlite3.Connection) -> bool:
    if connection.in_transaction:
        connection.execute(f"SAVEPOINT {_PRUNE_SAVEPOINT}")
        return False
    connection.execute("BEGIN IMMEDIATE")
    return True


def _commit_scope(connection: sqlite3.Connection, owns_transaction: bool) -> None:
    if owns_transaction:
        connection.commit()
    else:
        connection.execute(f"RELEASE SAVEPOINT {_PRUNE_SAVEPOINT}")


def _rollback_scope(connection: sqlite3.Connection, owns_transaction: bool) -> None:
    if owns_transaction:
        connection.rollback()
    else:
        connection.execute(f"ROLLBACK TO SAVEPOINT {_PRUNE_SAVEPOINT}")
        connection.execute(f"RELEASE SAVEPOINT {_PRUNE_SAVEPOINT}")


def prune_observations(
    connection: sqlite3.Connection,
    retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    *,
    retention_days: int | None = None,
    older_than_seconds: float | None = None,
    now: str | datetime | None = None,
    registry: Sequence[twill_detectors.Detector] | None = None,
    only: Sequence[str] | None = None,
) -> PruneReport:
    """Delete expired observations and refresh detector-derived clusters.

    The retention period controls the deletion boundary.  It is not used as a
    new analysis window: each detector keeps the window from its last run, or
    the window recorded on its existing cluster, with the normal 30-day window
    used for a detector that has never run.  The deletion is committed before
    detector refresh so a broken detector cannot defeat daily retention.  Each
    detector then keeps its normal per-detector isolation and its result is
    returned through the detector report.
    """

    if older_than_seconds is not None:
        if retention_days is not None:
            raise ValueError("pass only one of retention_days and older_than_seconds")
        retention_seconds = older_than_seconds
    if retention_days is not None:
        if isinstance(retention_days, bool) or not isinstance(retention_days, int):
            raise ValueError("retention_days must be a positive integer")
        retention_seconds = retention_days * 86400
    seconds = _seconds(retention_seconds)
    current = _clock(now)
    try:
        cutoff = current - timedelta(seconds=seconds)
    except (OverflowError, ValueError) as exc:
        raise ValueError("retention is outside the supported range") from exc
    cutoff_utc = cutoff.isoformat()
    active = twill_detectors.build_registry(
        *(tuple(registry) if registry is not None else twill_detectors.REGISTRY)
    )
    if only:
        active = twill_detectors.select_detectors(active, only)
    analysis_window_days = {
        detector.detector_id: _prior_window(connection, detector)
        for detector in active
    }
    retention_day_count = max(1, math.ceil(seconds / 86400))
    owns_transaction = _begin_scope(connection)
    try:
        before = int(connection.execute("SELECT count(*) FROM observation").fetchone()[0])
        _delete_expired(connection, cutoff)
        after = int(connection.execute("SELECT count(*) FROM observation").fetchone()[0])
        _commit_scope(connection, owns_transaction)
    except BaseException:
        if connection.in_transaction:
            _rollback_scope(connection, owns_transaction)
        raise

    detector_report = twill_detectors.run_detectors(
        connection,
        window_days=DEFAULT_ANALYSIS_WINDOW_DAYS,
        registry=active,
        window_days_by_detector=analysis_window_days,
        now=current,
    )
    return PruneReport(
        cutoff_utc=cutoff_utc,
        retention_seconds=seconds,
        retention_days=retention_day_count,
        pruned_observations=before - after,
        remaining_observations=after,
        clusters=sum(outcome.clusters for outcome in detector_report.outcomes),
        detector_report=detector_report,
        analysis_window_days=analysis_window_days,
        committed=True,
        detector_refresh_committed=detector_report.exit_code == EXIT_SUCCESS,
    )


def prune(
    connection: sqlite3.Connection,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    *,
    retention_seconds: float | None = None,
    **kwargs: object,
) -> PruneReport:
    """Compatibility wrapper for callers that express retention in days."""

    if retention_seconds is not None:
        return prune_observations(
            connection,
            retention_seconds=retention_seconds,
            **kwargs,
        )
    return prune_observations(
        connection,
        retention_days=retention_days,
        **kwargs,
    )


run_prune = prune_observations


__all__ = [
    "DEFAULT_ANALYSIS_WINDOW_DAYS",
    "DEFAULT_RETENTION_DAYS",
    "PruneReport",
    "prune",
    "prune_observations",
    "validate_retention",
    "run_prune",
]
