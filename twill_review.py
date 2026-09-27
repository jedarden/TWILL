"""Operator-owned review transitions for detector clusters."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from twill_contract import ValidationError
from twill_redactor import MAX_EXCERPT_LENGTH, Redactor


_CLUSTER_ID_RE = re.compile(r"(?P<detector>D-\d{2,}):(?P<key>.+)")


@dataclass(frozen=True)
class DismissedCluster:
    """The durable state recorded by an explicit dismiss operation."""

    detector_id: str
    key: str
    state: str
    reason: str
    dismissed_at: str

    @property
    def cluster_id(self) -> str:
        return f"{self.detector_id}:{self.key}"

    def as_dict(self) -> dict[str, str]:
        return {
            "cluster_id": self.cluster_id,
            "detector_id": self.detector_id,
            "key": self.key,
            "state": self.state,
            "reason": self.reason,
            "dismissed_at": self.dismissed_at,
        }


def _cluster_parts(cluster_id: object) -> tuple[str, str]:
    if not isinstance(cluster_id, str):
        raise ValidationError("cluster id must be text")
    match = _CLUSTER_ID_RE.fullmatch(cluster_id.strip())
    if match is None:
        raise ValidationError(
            "cluster id must have the form D-01:key",
            "copy the cluster id from 'twill rank --json' or 'twill explain --json'",
        )
    return match.group("detector"), match.group("key")


def _reason(value: object, content_fences: Iterable[str]) -> str:
    safe = Redactor(content_fences).redact_text(value)
    if not safe:
        raise ValidationError("dismiss reason must not be empty")
    if len(safe) > MAX_EXCERPT_LENGTH:
        raise ValidationError(
            f"dismiss reason must be at most {MAX_EXCERPT_LENGTH} characters"
        )
    return safe


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(row: tuple[object, ...]) -> DismissedCluster:
    detector_id, key, state, reason, dismissed_at = row
    if str(state) != "dismissed":
        raise ValidationError("cluster dismissal did not persist")
    if not isinstance(reason, str) or not reason:
        raise ValidationError("dismissed cluster has no recorded reason")
    if not isinstance(dismissed_at, str) or not dismissed_at:
        raise ValidationError("dismissed cluster has no recorded timestamp")
    return DismissedCluster(
        str(detector_id),
        str(key),
        str(state),
        reason,
        dismissed_at,
    )


def dismiss_cluster(
    connection: sqlite3.Connection,
    cluster_id: object,
    reason: object,
    *,
    content_fences: Iterable[str] = (),
    dismissed_at: str | None = None,
) -> DismissedCluster:
    """Permanently suppress one cluster and preserve its first audit record.

    Repeating the command is idempotent: an already dismissed cluster keeps its
    original reason and timestamp. A legacy dismissed row without audit fields
    is completed by the first explicit dismiss command that names it.
    """

    detector_id, key = _cluster_parts(cluster_id)
    safe_reason = _reason(reason, content_fences)
    timestamp = dismissed_at or _now()
    with connection:
        row = connection.execute(
            "SELECT detector_id, key, state, dismiss_reason, dismissed_at "
            "FROM cluster WHERE detector_id = ? AND key = ?",
            (detector_id, key),
        ).fetchone()
        if row is None:
            raise ValidationError(
                f"cluster not found: {detector_id}:{key}",
                "run 'twill detect' and 'twill rank' before dismissing a cluster",
            )

        if str(row[2]) == "dismissed" and row[3] and row[4]:
            return _record(row)

        updated = connection.execute(
            "UPDATE cluster SET state = 'dismissed', dismiss_reason = ?, "
            "dismissed_at = ? WHERE detector_id = ? AND key = ?",
            (safe_reason, timestamp, detector_id, key),
        ).rowcount
        if updated != 1:
            raise ValidationError("cluster dismissal did not update exactly one cluster")
        persisted = connection.execute(
            "SELECT detector_id, key, state, dismiss_reason, dismissed_at "
            "FROM cluster WHERE detector_id = ? AND key = ?",
            (detector_id, key),
        ).fetchone()
        if persisted is None:
            raise ValidationError("cluster dismissal disappeared before commit")
        return _record(persisted)


__all__ = ["DismissedCluster", "dismiss_cluster"]
