"""Refresh cluster coverage and score, then expose the review paths.

The rule index is read through FTS5, while a successful move is resolved to
its live path using the indexed content hash. Coverage is a marker on the
cluster, not a new review state: a covered cluster remains available for the
later measurement/escalation path but is excluded from new-lesson candidates.
The persisted score combines distinct-session breadth, event volume and
window-relative recency; coverage and review state decide which review lane a
scored row belongs to.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite, log1p
from typing import Mapping, Sequence

from twill_rulecorpus import IndexReport, index_corpus


_SCORE_SESSION_WEIGHT = 4.0
_SCORE_EVENT_WEIGHT = 1.0
_SCORE_RECENCY_WEIGHT = 1.0
_SECONDS_PER_DAY = 86400.0


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
class EstimatedWaste:
    """Whole-session usage proportionally attributed to one cluster."""

    input_tokens: float | None
    output_tokens: float | None
    cache_read_tokens: float | None
    waste_usd: float | None

    @property
    def tokens(self) -> float | None:
        if (
            self.input_tokens is None
            or self.output_tokens is None
            or self.cache_read_tokens is None
        ):
            return None
        return self.input_tokens + self.output_tokens + self.cache_read_tokens

    def as_dict(self) -> dict[str, object]:
        return {
            "estimated_input_tokens": self.input_tokens,
            "estimated_output_tokens": self.output_tokens,
            "estimated_cache_read_tokens": self.cache_read_tokens,
            "estimated_tokens": self.tokens,
            "estimated_waste_usd": self.waste_usd,
            "waste_attribution_method": "equal_split_across_distinct_cluster_hits",
        }


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
    estimated_waste: EstimatedWaste | None = None

    @property
    def covered(self) -> bool:
        return self.covered_by is not None

    @property
    def new_lesson_candidate(self) -> bool:
        return (
            self.state == "open"
            and self.covered_by is None
            and self.sessions > 0
        )

    @property
    def escalation_candidate(self) -> bool:
        return self.state == "escalation" or (
            self.state == "open" and self.covered and not self.rule_stale
        )

    def as_dict(self) -> dict[str, object]:
        estimate = self.estimated_waste or EstimatedWaste(None, None, None, None)
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
            **estimate.as_dict(),
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


def _timestamp(value: object) -> datetime | None:
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
    return parsed.astimezone(timezone.utc)


def _clock(as_of: str | datetime | None) -> datetime:
    if as_of is None:
        return datetime.now(timezone.utc)
    resolved = _timestamp(as_of)
    if resolved is None:
        raise ValueError("as_of must be an ISO-8601 timestamp")
    return resolved


def _resolve_clock(
    as_of: str | datetime | None,
    now: str | datetime | None,
) -> datetime:
    if as_of is not None and now is not None:
        raise ValueError("pass only one of as_of and now")
    return _clock(as_of if as_of is not None else now)


def score_cluster(
    sessions: int,
    events: int,
    last_seen: str,
    window_days: int,
    *,
    as_of: str | datetime | None = None,
    now: str | datetime | None = None,
) -> float:
    """Return the deterministic priority score for one cluster.

    Breadth is the strongest signal because a recurrence across independent
    sessions is more actionable than repeated events in one session. Log
    scaling keeps either count from overwhelming the other, while a window-
    relative half-life lets a newer recurrence rise without erasing evidence
    from older sessions. A malformed timestamp contributes no recency rather
    than making the whole rank pass fail; detector timestamps are text, while
    the score is a prioritization hint.
    """

    if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 0:
        raise ValueError("sessions must be a non-negative integer")
    if isinstance(events, bool) or not isinstance(events, int) or events < 0:
        raise ValueError("events must be a non-negative integer")
    if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 1:
        raise ValueError("window_days must be a positive integer")
    reference = _resolve_clock(as_of, now)
    observed = _timestamp(last_seen)
    if observed is None:
        recency = 0.0
    else:
        age_days = max(
            0.0,
            (reference - observed).total_seconds() / _SECONDS_PER_DAY,
        )
        recency = 2.0 ** (-age_days / window_days)
    return (
        _SCORE_SESSION_WEIGHT * log1p(sessions)
        + _SCORE_EVENT_WEIGHT * log1p(events)
        + _SCORE_RECENCY_WEIGHT * recency
    )


def _score_updates(
    connection: sqlite3.Connection,
    *,
    as_of: datetime,
) -> tuple[tuple[float, str, str], ...]:
    rows = connection.execute(
        "SELECT detector_id, key, sessions, events, last_seen, window_days, score "
        "FROM cluster ORDER BY detector_id, key"
    ).fetchall()
    updates: list[tuple[float, str, str]] = []
    for detector_id, key, sessions, events, last_seen, window_days, old_score in rows:
        score = score_cluster(
            int(sessions),
            int(events),
            str(last_seen),
            int(window_days),
            as_of=as_of,
        )
        if float(old_score) != score:
            updates.append((score, str(detector_id), str(key)))
    return tuple(updates)


def _persist_score_updates(
    connection: sqlite3.Connection,
    updates: Sequence[tuple[float, str, str]],
    *,
    manage_transaction: bool,
) -> None:
    if not updates:
        return
    statement = "UPDATE cluster SET score = ? WHERE detector_id = ? AND key = ?"
    if manage_transaction:
        with connection:
            connection.executemany(statement, updates)
    else:
        connection.executemany(statement, updates)


def refresh_scores(
    connection: sqlite3.Connection,
    *,
    as_of: str | datetime | None = None,
    now: str | datetime | None = None,
    manage_transaction: bool = True,
) -> None:
    """Refresh persisted scores for every cluster without changing review state."""

    reference = _resolve_clock(as_of, now)
    updates = _score_updates(connection, as_of=reference)
    _persist_score_updates(connection, updates, manage_transaction=manage_transaction)


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


def _known_token(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _known_cost(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    resolved = float(value)
    if resolved < 0 or not isfinite(resolved):
        return None
    return resolved


def attribute_waste(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], EstimatedWaste]:
    rows = connection.execute(
        "WITH session_cluster_counts AS ("
        "  SELECT session_id, count(*) AS cluster_hits "
        "  FROM cluster_session GROUP BY session_id"
        ") "
        "SELECT cs.detector_id, cs.key, counts.cluster_hits, "
        "       u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cost_usd "
        "FROM cluster_session AS cs "
        "JOIN session_cluster_counts AS counts ON counts.session_id = cs.session_id "
        "LEFT JOIN session_usage AS u ON u.session_id = cs.session_id "
        "ORDER BY cs.detector_id, cs.key, cs.session_id"
    ).fetchall()
    hit_counts: dict[tuple[str, str], int] = {}
    token_values: dict[tuple[str, str], dict[str, float]] = {}
    token_counts: dict[tuple[str, str], dict[str, int]] = {}
    cost_values: dict[tuple[str, str], float] = {}
    cost_counts: dict[tuple[str, str], int] = {}
    token_columns = ("input_tokens", "output_tokens", "cache_read_tokens")
    for detector_id, key, cluster_hits, *usage in rows:
        cluster = (str(detector_id), str(key))
        hits = int(cluster_hits)
        hit_counts[cluster] = hit_counts.get(cluster, 0) + 1
        values = token_values.setdefault(
            cluster, {column: 0.0 for column in token_columns}
        )
        counts = token_counts.setdefault(cluster, {column: 0 for column in token_columns})
        for index, column in enumerate(token_columns):
            token = _known_token(usage[index])
            if token is None:
                continue
            values[column] += token / hits
            counts[column] += 1
        cost = _known_cost(usage[3])
        if cost is not None:
            cost_values[cluster] = cost_values.get(cluster, 0.0) + cost / hits
            cost_counts[cluster] = cost_counts.get(cluster, 0) + 1

    estimates: dict[tuple[str, str], EstimatedWaste] = {}
    for cluster, hits in hit_counts.items():
        values = token_values[cluster]
        counts = token_counts[cluster]
        components = {
            column: values[column] if counts[column] == hits else None
            for column in token_columns
        }
        cost = (
            cost_values[cluster]
            if cost_counts.get(cluster, 0) == hits
            else None
        )
        estimates[cluster] = EstimatedWaste(
            components["input_tokens"],
            components["output_tokens"],
            components["cache_read_tokens"],
            cost,
        )
    return estimates


def _ranked_row(
    row: tuple[object, ...],
    coverage: ClusterCoverage | None = None,
    estimated_waste: EstimatedWaste | None = None,
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
        estimated_waste=estimated_waste,
    )


def _cluster_rows(
    connection: sqlite3.Connection,
    coverage_by_cluster: dict[tuple[str, str], ClusterCoverage],
    estimates: Mapping[tuple[str, str], EstimatedWaste],
) -> tuple[RankedCluster, ...]:
    rows = connection.execute(
        "SELECT detector_id, key, window_days, sessions, events, first_seen, "
        "last_seen, score, covered_by, state FROM cluster "
        "ORDER BY score DESC, sessions DESC, events DESC, last_seen DESC, "
        "detector_id ASC, key ASC"
    ).fetchall()
    return tuple(
        _ranked_row(
            row,
            coverage_by_cluster.get((str(row[0]), str(row[1]))),
            estimates.get((str(row[0]), str(row[1]))),
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
    as_of: str | datetime | None = None,
    now: str | datetime | None = None,
) -> RankReport:
    """Refresh coverage and scores, then return the separate review paths."""

    _validate_top_k(top_k)
    reference = _resolve_clock(as_of, now)
    if manage_transaction:
        with connection:
            coverage = refresh_coverage(
                connection,
                preferred_shas=preferred_shas,
                manage_transaction=False,
            )
            updates = _score_updates(connection, as_of=reference)
            _persist_score_updates(connection, updates, manage_transaction=False)
    else:
        coverage = refresh_coverage(
            connection,
            preferred_shas=preferred_shas,
            manage_transaction=False,
        )
        updates = _score_updates(connection, as_of=reference)
        _persist_score_updates(connection, updates, manage_transaction=False)
    coverage_by_cluster = {
        (row.detector_id, row.key): row for row in coverage.rows
    }
    estimates = attribute_waste(connection)
    all_rows = _cluster_rows(connection, coverage_by_cluster, estimates)
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
    as_of: str | datetime | None = None,
    now: str | datetime | None = None,
) -> RankRun:
    """Index the configured corpus and rank the persisted clusters."""

    _validate_top_k(top_k)
    reference = _resolve_clock(as_of, now)
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
            as_of=reference,
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
