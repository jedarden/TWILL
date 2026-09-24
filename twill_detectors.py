"""The versioned detector registry and its per-detector isolation runner.

Plan §4 defines a detector as a named, versioned SQL query over observations;
§8.1 EC-12 and §8.2 decide the two mechanics this module owns:

* **Versioning (EC-12).**  A detector's identity is ``D-01@N`` — name plus
  integer version.  The registry holds one active version per name, and the
  semantics of a version (its SQL) are stamped into ``detector_run`` the first
  time that version commits clusters.  Re-registering the same version with
  different SQL is *refused*: changing what a detector means requires a new
  version, so a lesson's before/after measurement series is never silently
  redefined.  Measurements and run records cite the full versioned id.
* **Isolation (§8.2).**  Each detector runs inside its own ``BEGIN IMMEDIATE``
  transaction: a detector whose SQL errors (or whose output breaks the column
  contract below) is rolled back, skipped and reported, while every other
  detector still runs and commits.  No partial cluster writes survive a
  failure, and ``twill detect`` exits non-zero so timers and the operator see
  it.

Detector SQL contract
---------------------
``cluster_sql`` is a single read-only ``SELECT`` (a ``WITH`` CTE is fine) over
the corpus tables.  The runner binds two named parameters:

``:window_start_utc``
    ISO-8601 UTC timestamp of the window's start; observations at or after it
    are in scope.
``:window_days``
    The window's length in days, as an integer.

The query must emit these columns (any order; extras are ignored):

``key``
    The cluster's normalized key — the grouping identity of §4.
``sessions``
    Distinct sessions contributing (integer >= 0).
``events``
    Observations contributing (integer >= 0).
``first_seen`` / ``last_seen``
    ISO-8601 timestamps of the earliest and latest contributing observation.

The runner does all the writing.  Keys are redacted and truncated to 240
chars (§8.3); rows are upserted into ``cluster`` keyed by ``(detector_id,
key)`` using the *base* id, which stays stable across version bumps because
the cluster is the real-world problem while the version is recorded per run
in ``detector_run`` and per measurement; and ``state = 'open'`` clusters the
detector no longer emits are deleted (cluster output is refreshed per run,
§7.1).  Review state (``state`` other than open, ``covered_by``) is never
overwritten by a refresh — suppression and coverage outlive re-runs.

Exit-code mapping (§14): a refused detector is a validation failure (4, the
detector self-test); a detector SQL error is a runtime error (1).  Both are
per-detector: the healthy detectors' clusters are committed either way.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Sequence

from twill_contract import (
    EXIT_RUNTIME_ERROR,
    EXIT_SUCCESS,
    EXIT_VALIDATION_FAILURE,
)
from twill_redactor import redact_text


# §4: "D-01 …" — two or more digits so the catalog can grow past D-99.
DETECTOR_ID_PATTERN = re.compile(r"D-\d{2,}$")
# §8.3: every cluster.key is post-redaction and <= 240 chars.
MAX_KEY_LENGTH = 240
# §8.2: the failure text a run records is bounded like every other stored text.
MAX_ERROR_LENGTH = 500

# The columns a detector's SQL must emit, and the parameters the runner binds.
CLUSTER_COLUMNS = ("key", "sessions", "events", "first_seen", "last_seen")
SQL_PARAMETERS = ("window_start_utc", "window_days")

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_REFUSED = "refused"

# A detector query is a single statement that must read: anything that is not
# a SELECT (or a WITH ... SELECT, the only shape SQLite CTEs take) is refused
# at registration, so detector SQL can never write.  Python's sqlite3 already
# rejects multiple statements per execute(); the shape check keeps a write
# statement from ever being attempted.
_QUERY_SHAPE = re.compile(r"(?:SELECT|WITH)\b", re.IGNORECASE)


class DetectorContractError(ValueError):
    """A detector broke the SQL contract; isolates like a SQL error (§8.2)."""


@dataclasses.dataclass(frozen=True)
class Detector:
    """One registered detector: a name, a version, and the SQL that defines it.

    ``semantics_sha`` fingerprints exactly what a version means — its id, its
    version and its SQL with whitespace normalized — which is what EC-12
    compares against the stamp recorded when that version last committed
    clusters.  Prose (``description``) is deliberately outside the hash:
    rewording a detector does not redefine it.
    """

    detector_id: str
    version: int
    description: str
    cluster_sql: str

    @property
    def full_id(self) -> str:
        """The versioned identity measurements and run records cite (EC-12)."""

        return f"{self.detector_id}@{self.version}"

    @property
    def semantics_sha(self) -> str:
        canonical = "\n".join(
            (
                f"detector={self.detector_id}",
                f"version={self.version}",
                f"sql={' '.join(self.cluster_sql.split())}",
            )
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_registry(*detectors: Detector) -> tuple[Detector, ...]:
    """Validate a detector set; the registry's discipline is enforced here.

    One *active* version per name: a registry carrying ``D-01@1`` and
    ``D-01@2`` together is the exact confusion EC-12 exists to prevent, so it
    is a construction error rather than a runtime surprise.  Order is kept as
    given — it is the deterministic run order.
    """

    seen: dict[str, int] = {}
    for detector in detectors:
        if not DETECTOR_ID_PATTERN.fullmatch(detector.detector_id):
            raise ValueError(
                f"detector id {detector.detector_id!r} must match "
                f"'D-NN' (e.g. D-01, D-10)"
            )
        if detector.version < 1:
            raise ValueError(
                f"detector {detector.detector_id} has version "
                f"{detector.version}: versions start at 1"
            )
        if not detector.description.strip():
            raise ValueError(
                f"detector {detector.full_id} has an empty description"
            )
        statement = detector.cluster_sql.strip().rstrip(";").strip()
        if not statement:
            raise ValueError(
                f"detector {detector.full_id} has empty cluster SQL"
            )
        if ";" in statement:
            raise ValueError(
                f"detector {detector.full_id} cluster SQL must be one "
                f"statement: {detector.cluster_sql!r}"
            )
        if not _QUERY_SHAPE.match(statement):
            raise ValueError(
                f"detector {detector.full_id} cluster SQL must be a single "
                f"read-only SELECT (or WITH ... SELECT): "
                f"{detector.cluster_sql!r}"
            )
        previous = seen.get(detector.detector_id)
        if previous is not None:
            raise ValueError(
                f"detector {detector.detector_id} is registered twice "
                f"(versions {previous} and {detector.version}): one active "
                "version per name — change semantics by bumping the version "
                "and retiring the old one, never by registering both (EC-12)"
            )
        seen[detector.detector_id] = detector.version
    return detectors


def select_detectors(
    registry: Sequence[Detector], names: Sequence[str]
) -> tuple[Detector, ...]:
    """Pick registry entries by base id; an unknown name is an error."""

    by_id = {detector.detector_id: detector for detector in registry}
    unknown = sorted({name for name in names if name not in by_id})
    if unknown:
        raise ValueError(
            f"unknown detector id(s): {', '.join(unknown)}; registered: "
            f"{', '.join(by_id) if by_id else '(none)'}"
        )
    return tuple(by_id[name] for name in dict.fromkeys(names))


MISSING_BINARY_SQL = """
    SELECT 'command-not-found:' || program AS key,
           count(DISTINCT session_id) AS sessions,
           count(*) AS events,
           min(ts_utc) AS first_seen,
           max(ts_utc) AS last_seen
    FROM observation
    WHERE ts_utc >= :window_start_utc
      AND kind = 'run_failed'
      AND program IS NOT NULL
      AND trim(program) <> ''
      AND signature IS NOT NULL
      AND trim(signature) <> ''
      AND sig_hash IS NOT NULL
      AND lower(signature) LIKE '%command not found%'
    GROUP BY program
    HAVING count(DISTINCT session_id) >= 2
    ORDER BY sessions DESC, last_seen DESC, key ASC
"""

MISSING_BINARY = Detector(
    "D-01",
    1,
    "missing binaries reported as command-not-found across distinct sessions",
    MISSING_BINARY_SQL,
)


RECURRING_ERROR_SIGNATURE_SQL = """
    SELECT signature AS key,
           count(DISTINCT session_id) AS sessions,
           count(*) AS events,
           min(ts_utc) AS first_seen,
           max(ts_utc) AS last_seen
    FROM observation
    WHERE ts_utc >= :window_start_utc
      AND kind IN ('run_failed', 'tool_error')
      AND signature IS NOT NULL
      AND trim(signature) <> ''
      AND sig_hash IS NOT NULL
    GROUP BY sig_hash, signature
    HAVING count(DISTINCT session_id) >= 2
    ORDER BY sessions DESC, last_seen DESC, key ASC
"""

RECURRING_ERROR_SIGNATURE = Detector(
    "D-02",
    1,
    "recurring normalized error signatures across distinct sessions",
    RECURRING_ERROR_SIGNATURE_SQL,
)

# The shipped catalog.  Phase 2's detector beads (D-01 missing binary, D-02
# recurring error signature, ... D-10 ICG gate gap) append their entries here;
# an entry leaves this tuple when its successor version lands.
REGISTRY: tuple[Detector, ...] = build_registry(
    MISSING_BINARY, RECURRING_ERROR_SIGNATURE
)


@dataclasses.dataclass(frozen=True)
class DetectorOutcome:
    """What happened to one detector in one run (§8.2: skipped and reported)."""

    detector_id: str
    version: int
    status: str
    clusters: int = 0
    error: str | None = None

    @property
    def full_id(self) -> str:
        return f"{self.detector_id}@{self.version}"


@dataclasses.dataclass(frozen=True)
class DetectorRunReport:
    """The per-detector record of one ``twill detect`` run."""

    window_days: int
    window_start_utc: str
    ran_at: str
    outcomes: tuple[DetectorOutcome, ...]

    @property
    def refused(self) -> tuple[DetectorOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == STATUS_REFUSED)

    @property
    def errored(self) -> tuple[DetectorOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == STATUS_ERROR)

    @property
    def failed(self) -> tuple[DetectorOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status != STATUS_OK)

    @property
    def exit_code(self) -> int:
        # A refusal is the detector self-test failing (§14: exit 4): the
        # registry disagrees with what this database already ran.  A SQL error
        # is a runtime fault (exit 1).  Both leave the healthy detectors'
        # clusters committed.
        if self.refused:
            return EXIT_VALIDATION_FAILURE
        if self.errored:
            return EXIT_RUNTIME_ERROR
        return EXIT_SUCCESS


def _bounded(text: str, limit: int = MAX_ERROR_LENGTH) -> str:
    return redact_text(text)[:limit]


def _short_sha(sha: str) -> str:
    return sha[:12]


def _drift_message(detector: Detector, recorded_sha: str) -> str:
    return (
        f"semantics changed without a version bump (recorded "
        f"{_short_sha(recorded_sha)}, registered "
        f"{_short_sha(detector.semantics_sha)}); bump the version so the "
        "measurement series is redefined openly (EC-12)"
    )


def _emitted_key(detector: Detector, raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise DetectorContractError(
            f"{detector.full_id} emitted a non-text or empty key: {raw!r}"
        )
    key = redact_text(raw)[:MAX_KEY_LENGTH]
    if not key.strip():
        raise DetectorContractError(
            f"{detector.full_id} emitted a key that redacts to nothing"
        )
    return key


def _emitted_count(detector: Detector, column: str, raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise DetectorContractError(
            f"{detector.full_id} emitted non-integer {column}: {raw!r}"
        )
    if raw < 0:
        raise DetectorContractError(
            f"{detector.full_id} emitted negative {column}: {raw!r}"
        )
    return raw


def _emitted_timestamp(detector: Detector, column: str, raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise DetectorContractError(
            f"{detector.full_id} emitted non-text {column}: {raw!r}"
        )
    return raw


def _collect_clusters(
    detector: Detector, cursor: sqlite3.Cursor
) -> dict[str, tuple[int, int, str, str]]:
    """Validate a detector's emitted rows into upsert values, keyed by key.

    Column names come from the cursor description so emission order does not
    matter.  A contract violation raises :class:`DetectorContractError`, which
    the runner isolates exactly like a SQL error.
    """

    columns = [description[0] for description in cursor.description or ()]
    missing = [column for column in CLUSTER_COLUMNS if column not in columns]
    if missing:
        raise DetectorContractError(
            f"{detector.full_id} must emit the columns "
            f"{', '.join(CLUSTER_COLUMNS)}; missing {', '.join(missing)} "
            f"(got {', '.join(columns)})"
        )
    emitted: dict[str, tuple[int, int, str, str]] = {}
    for row in cursor.fetchall():
        value = dict(zip(columns, row))
        key = _emitted_key(detector, value["key"])
        emitted[key] = (
            _emitted_count(detector, "sessions", value["sessions"]),
            _emitted_count(detector, "events", value["events"]),
            _emitted_timestamp(detector, "first_seen", value["first_seen"]),
            _emitted_timestamp(detector, "last_seen", value["last_seen"]),
        )
    return emitted


_CLUSTER_UPSERT = """
INSERT INTO cluster(detector_id, key, window_days, sessions, events,
                    first_seen, last_seen, score, covered_by, state)
VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, NULL, 'open')
ON CONFLICT(detector_id, key) DO UPDATE SET
  window_days=excluded.window_days, sessions=excluded.sessions,
  events=excluded.events, first_seen=excluded.first_seen,
  last_seen=excluded.last_seen
"""

_RUN_RECORD_UPSERT = """
INSERT INTO detector_run(detector_id, version, full_id, semantics_sha,
                         first_run_at, last_run_at, last_status, last_error,
                         clusters, window_days)
VALUES (?, ?, ?, ?, ?, ?, 'ok', NULL, ?, ?)
ON CONFLICT(detector_id, version) DO UPDATE SET
  full_id=excluded.full_id, semantics_sha=excluded.semantics_sha,
  last_run_at=excluded.last_run_at, last_status='ok', last_error=NULL,
  clusters=excluded.clusters, window_days=excluded.window_days
"""


def _run_one(
    connection: sqlite3.Connection,
    detector: Detector,
    window_days: int,
    parameters: dict[str, object],
    ran_at: str,
) -> int:
    """Run one detector and commit its refresh in one transaction (§8.2).

    Everything a detector writes — its cluster refresh and its run record —
    lands or rolls back together, so a failure leaves no partial cluster
    writes and no semantics stamp for output that never committed.
    """

    connection.execute("BEGIN IMMEDIATE")
    try:
        cursor = connection.execute(detector.cluster_sql, parameters)
        emitted = _collect_clusters(detector, cursor)
        open_keys = {
            row[0]
            for row in connection.execute(
                "SELECT key FROM cluster "
                "WHERE detector_id = ? AND state = 'open'",
                (detector.detector_id,),
            )
        }
        connection.executemany(
            _CLUSTER_UPSERT,
            (
                # (detector_id, key, window_days, sessions, events,
                #  first_seen, last_seen) — the upsert's parameter order.
                (detector.detector_id, key, window_days, *values)
                for key, values in emitted.items()
            ),
        )
        stale = open_keys - set(emitted)
        if stale:
            connection.executemany(
                "DELETE FROM cluster WHERE detector_id = ? AND key = ?",
                ((detector.detector_id, key) for key in sorted(stale)),
            )
        connection.execute(
            _RUN_RECORD_UPSERT,
            (
                detector.detector_id,
                detector.version,
                detector.full_id,
                detector.semantics_sha,
                ran_at,
                ran_at,
                len(emitted),
                window_days,
            ),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return len(emitted)


def _record_failure(
    connection: sqlite3.Connection,
    detector: Detector,
    ran_at: str,
    error_text: str,
) -> None:
    """Note a failed run on a version that has succeeded before, if it has.

    No row is created for a never-successful version: ``semantics_sha`` only
    ever describes semantics that committed clusters, so fixing a detector
    that has only ever errored does not trip the drift check.  Best-effort by
    design — the run report is authoritative; this row is bookkeeping for a
    detector that already failed once.
    """

    try:
        with connection:
            connection.execute(
                "UPDATE detector_run SET last_run_at = ?, last_status = 'error', "
                "last_error = ? WHERE detector_id = ? AND version = ?",
                (
                    ran_at,
                    _bounded(error_text),
                    detector.detector_id,
                    detector.version,
                ),
            )
    except sqlite3.Error:
        pass


def run_detectors(
    connection: sqlite3.Connection,
    *,
    window_days: int,
    registry: Sequence[Detector] | None = None,
    only: Sequence[str] | None = None,
) -> DetectorRunReport:
    """Run the registry with per-detector isolation; never raises per-detector.

    Returns a report whatever happened: a detector that errors or breaks its
    contract is rolled back, recorded and reported while the rest still run
    (§8.2); a detector whose semantics drifted under the same version is
    refused outright (EC-12).  The caller decides the exit code from
    :attr:`DetectorRunReport.exit_code`.
    """

    if not isinstance(window_days, int) or isinstance(window_days, bool):
        raise ValueError("window_days must be an integer")
    if window_days < 1:
        raise ValueError(f"window_days must be at least 1: {window_days}")
    active = build_registry(*(registry if registry is not None else REGISTRY))
    if only:
        active = select_detectors(active, only)

    now = datetime.now(timezone.utc)
    parameters = {
        "window_start_utc": (now - timedelta(days=window_days)).isoformat(),
        "window_days": window_days,
    }
    ran_at = now.isoformat()

    outcomes: list[DetectorOutcome] = []
    for detector in active:
        row = connection.execute(
            "SELECT semantics_sha FROM detector_run "
            "WHERE detector_id = ? AND version = ?",
            (detector.detector_id, detector.version),
        ).fetchone()
        if row is not None and row[0] != detector.semantics_sha:
            outcomes.append(
                DetectorOutcome(
                    detector_id=detector.detector_id,
                    version=detector.version,
                    status=STATUS_REFUSED,
                    error=_drift_message(detector, str(row[0])),
                )
            )
            continue
        try:
            clusters = _run_one(
                connection, detector, window_days, parameters, ran_at
            )
        except (sqlite3.Error, DetectorContractError) as exc:
            _record_failure(connection, detector, ran_at, str(exc))
            outcomes.append(
                DetectorOutcome(
                    detector_id=detector.detector_id,
                    version=detector.version,
                    status=STATUS_ERROR,
                    error=_bounded(str(exc)),
                )
            )
            continue
        outcomes.append(
            DetectorOutcome(
                detector_id=detector.detector_id,
                version=detector.version,
                status=STATUS_OK,
                clusters=clusters,
            )
        )
    return DetectorRunReport(
        window_days=window_days,
        window_start_utc=str(parameters["window_start_utc"]),
        ran_at=ran_at,
        outcomes=tuple(outcomes),
    )
