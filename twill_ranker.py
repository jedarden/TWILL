"""Refresh cluster coverage and expose the new-lesson candidate path.

The rule index is read through FTS5, while a successful move is resolved to
its live path using the indexed content hash. Coverage is a marker on the
cluster, not a new review state: a covered cluster remains available for the
later measurement/escalation path but is excluded from new-lesson candidates.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Mapping, Sequence

from twill_rulecorpus import IndexReport, index_corpus


@dataclass(frozen=True)
class RuleMatch:
    """The rule selected for one cluster."""

    path: str
    sha: str
    stale: bool


@dataclass(frozen=True)
class ClusterCoverage:
    """The coverage result for one persisted cluster."""

    detector_id: str
    key: str
    covered_by: str | None
    rule_stale: bool = False
    changed: bool = False
    rule_sha: str | None = None


@dataclass(frozen=True)
class CoverageReport:
    """The complete result of one coverage refresh."""

    rows: tuple[ClusterCoverage, ...]

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def covered(self) -> int:
        return sum(row.covered_by is not None for row in self.rows)

    @property
    def uncovered(self) -> int:
        return self.total - self.covered

    @property
    def changed(self) -> int:
        return sum(row.changed for row in self.rows)

    @property
    def covered_rows(self) -> tuple[ClusterCoverage, ...]:
        return tuple(row for row in self.rows if row.covered_by is not None)


@dataclass(frozen=True)
class RankedCluster:
    """A cluster row rendered for the rank output."""

    detector_id: str
    key: str
    window_days: int
    sessions: int
    events: int
    first_seen: str
    last_seen: str
    score: float
    covered_by: str | None
    state: str
    rule_sha: str | None = None
    rule_stale: bool = False

    @property
    def covered(self) -> bool:
        return self.covered_by is not None

    @property
    def new_lesson_candidate(self) -> bool:
        return self.state == "open" and self.covered_by is None

    @property
    def escalation_candidate(self) -> bool:
        return self.state == "escalation" or (
            self.state == "open" and self.covered and not self.rule_stale
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "detector_id": self.detector_id,
            "key": self.key,
            "window_days": self.window_days,
            "sessions": self.sessions,
            "events": self.events,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "score": self.score,
            "covered_by": self.covered_by,
            "state": self.state,
            "rule_sha": self.rule_sha,
            "rule_stale": self.rule_stale,
            "covered": self.covered,
            "new_lesson_candidate": self.new_lesson_candidate,
            "escalation_candidate": self.escalation_candidate,
        }


@dataclass(frozen=True)
class RankReport:
    """The new-lesson, coverage, and escalation review paths."""

    top_k: int
    clusters: tuple[RankedCluster, ...]
    covered_clusters: tuple[RankedCluster, ...]
    escalations: tuple[RankedCluster, ...]
    degraded_clusters: tuple[RankedCluster, ...]
    coverage: CoverageReport
    all_clusters: tuple[RankedCluster, ...]

    @property
    def suppressed_clusters(self) -> tuple[RankedCluster, ...]:
        return tuple(row for row in self.all_clusters if not row.new_lesson_candidate)


@dataclass(frozen=True)
class RankRun:
    """The corpus index and rank results from one command run."""

    index: IndexReport
    ranking: RankReport


def _quote_fts(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _phrase_query(key: str) -> str | None:
    normalized = " ".join(key.replace("\x00", " ").split())
    if not normalized:
        return None
    return _quote_fts(normalized)


def _candidates(connection: sqlite3.Connection, query: str) -> tuple[RuleMatch, ...]:
    rows = connection.execute(
        "SELECT d.path, d.sha, d.stale "
        "FROM rule_fts "
        "JOIN rule_doc AS d ON d.path = rule_fts.path "
        "WHERE rule_fts MATCH ? "
        "ORDER BY d.stale ASC, d.path ASC",
        (query,),
    ).fetchall()
    return tuple(RuleMatch(str(path), str(sha), bool(stale)) for path, sha, stale in rows)


def match_rule(
    connection: sqlite3.Connection,
    key: str,
    *,
    preferred_path: str | None = None,
    preferred_sha: str | None = None,
) -> RuleMatch | None:
    """Return the deterministic rule match for a normalized cluster key.

    The key is matched as one quoted FTS phrase. Keeping the normalized key as
    one phrase avoids suppressing a cluster because a rule happens to mention
    one generic token from it. Candidate paths are ordered live before stale
    and then by path. When a cluster already names a rule, a live candidate
    with that rule's content hash is preferred, which follows a move even if
    another live rule contains the same phrase.
    """

    text = str(key).strip()
    query = _phrase_query(text)
    if query is None:
        return None
    candidates = _candidates(connection, query)
    if not candidates:
        return None
    previous_sha = preferred_sha
    if previous_sha is None and preferred_path is not None:
        previous = connection.execute(
            "SELECT sha FROM rule_doc WHERE path = ?", (str(preferred_path),)
        ).fetchone()
        if previous is not None:
            previous_sha = str(previous[0])
    if previous_sha is not None:
        for candidate in candidates:
            if not candidate.stale and candidate.sha == previous_sha:
                return candidate
        if preferred_path is not None:
            for candidate in candidates:
                if candidate.path == str(preferred_path):
                    return candidate
    return candidates[0]


def refresh_coverage(
    connection: sqlite3.Connection,
    *,
    preferred_shas: Mapping[tuple[str, str], str] | None = None,
    manage_transaction: bool = True,
) -> CoverageReport:
    """Refresh every cluster's coverage marker in one transaction.

    A no-match result clears an obsolete marker. Existing ``state`` values are
    never changed, so a later measurement pass can distinguish an enacted
    rule's recurrence from a new-lesson candidate without this pass making an
    escalation decision. ``manage_transaction`` is used by the rank command
    when the corpus index and this refresh share one outer transaction.
    """

    rows = connection.execute(
        "SELECT detector_id, key, covered_by FROM cluster "
        "ORDER BY detector_id, key"
    ).fetchall()
    results: list[ClusterCoverage] = []
    updates: list[tuple[str | None, str, str]] = []
    for detector_id, key, old_covered_by in rows:
        cluster_key = (str(detector_id), str(key))
        match = match_rule(
            connection,
            str(key),
            preferred_path=None if old_covered_by is None else str(old_covered_by),
            preferred_sha=None if preferred_shas is None else preferred_shas.get(cluster_key),
        )
        covered_by = match.path if match is not None else None
        changed = covered_by != old_covered_by
        if changed:
            updates.append((covered_by, str(detector_id), str(key)))
        results.append(
            ClusterCoverage(
                detector_id=str(detector_id),
                key=str(key),
                covered_by=covered_by,
                rule_sha=match.sha if match is not None else None,
                rule_stale=match.stale if match is not None else False,
                changed=changed,
            )
        )

    if updates:
        statement = (
            "UPDATE cluster SET covered_by = ? "
            "WHERE detector_id = ? AND key = ?"
        )
        if manage_transaction:
            with connection:
                connection.executemany(statement, updates)
        else:
            connection.executemany(statement, updates)
    return CoverageReport(tuple(results))


def _ranked_row(
    row: tuple[object, ...],
    coverage: ClusterCoverage | None = None,
) -> RankedCluster:
    return RankedCluster(
        detector_id=str(row[0]),
        key=str(row[1]),
        window_days=int(row[2]),
        sessions=int(row[3]),
        events=int(row[4]),
        first_seen=str(row[5]),
        last_seen=str(row[6]),
        score=float(row[7]),
        covered_by=None if row[8] is None else str(row[8]),
        state=str(row[9]),
        rule_sha=coverage.rule_sha if coverage is not None else None,
        rule_stale=coverage.rule_stale if coverage is not None else False,
    )


def _cluster_rows(
    connection: sqlite3.Connection,
    coverage_by_cluster: dict[tuple[str, str], ClusterCoverage],
) -> tuple[RankedCluster, ...]:
    rows = connection.execute(
        "SELECT detector_id, key, window_days, sessions, events, first_seen, "
        "last_seen, score, covered_by, state FROM cluster "
        "ORDER BY sessions DESC, last_seen DESC, detector_id ASC, key ASC"
    ).fetchall()
    return tuple(
        _ranked_row(
            row,
            coverage_by_cluster.get((str(row[0]), str(row[1]))),
        )
        for row in rows
    )


def _validate_top_k(top_k: int) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError("top_k must be a positive integer")


def rank_clusters(
    connection: sqlite3.Connection,
    top_k: int = 10,
    *,
    preferred_shas: Mapping[tuple[str, str], str] | None = None,
    manage_transaction: bool = True,
) -> RankReport:
    """Refresh coverage and return the separate review paths."""

    _validate_top_k(top_k)
    coverage = refresh_coverage(
        connection,
        preferred_shas=preferred_shas,
        manage_transaction=manage_transaction,
    )
    coverage_by_cluster = {
        (row.detector_id, row.key): row for row in coverage.rows
    }
    all_rows = _cluster_rows(connection, coverage_by_cluster)
    candidates = tuple(
        row for row in all_rows if row.new_lesson_candidate
    )[:top_k]
    covered = tuple(row for row in all_rows if row.covered)
    escalations = tuple(row for row in all_rows if row.escalation_candidate)
    degraded = tuple(row for row in covered if row.rule_stale)
    return RankReport(
        top_k,
        candidates,
        covered,
        escalations,
        degraded,
        coverage,
        all_rows,
    )


def _covered_rule_hashes(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    rows = connection.execute(
        "SELECT c.detector_id, c.key, d.sha "
        "FROM cluster AS c JOIN rule_doc AS d ON d.path = c.covered_by "
        "WHERE c.covered_by IS NOT NULL"
    ).fetchall()
    return {
        (str(detector_id), str(key)): str(sha)
        for detector_id, key, sha in rows
    }


def run_rank(
    connection: sqlite3.Connection,
    patterns: Sequence[str],
    *,
    top_k: int = 10,
) -> RankRun:
    """Index the configured corpus and rank the persisted clusters."""

    _validate_top_k(top_k)
    connection.execute("BEGIN IMMEDIATE")
    try:
        preferred_shas = _covered_rule_hashes(connection)
        index_report = index_corpus(
            connection,
            patterns,
            manage_transaction=False,
        )
        ranking = rank_clusters(
            connection,
            top_k=top_k,
            preferred_shas=preferred_shas,
            manage_transaction=False,
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return RankRun(index_report, ranking)


def match_clusters(connection: sqlite3.Connection) -> CoverageReport:
    """Compatibility name for the coverage refresh operation."""

    return refresh_coverage(connection)


def flag_coverage(connection: sqlite3.Connection) -> CoverageReport:
    """Compatibility name for callers that describe the operation as flagging."""

    return refresh_coverage(connection)
