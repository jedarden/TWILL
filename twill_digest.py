"""Render the weekly digest as a reproducible week-over-week report."""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import twill_detectors
from twill_detectors import (
    MAX_ERROR_LENGTH,
    STATUS_ERROR,
    STATUS_OK,
    Detector,
    DetectorContractError,
    read_clusters,
)
from twill_redactor import redact_text
from twill_schema import connect_read_only, state_db_path


MAX_LINE_LENGTH = 240
DIGEST_DIR_MODE = 0o700
DIGEST_FILE_MODE = 0o600
VERDICT_NEW = "new"
VERDICT_WORSENING = "worsening"
VERDICT_IMPROVING = "improving"
VERDICT_GONE = "gone"
VERDICT_ORDER = (
    VERDICT_NEW,
    VERDICT_WORSENING,
    VERDICT_IMPROVING,
    VERDICT_GONE,
)
STATUS_NO_DATABASE = "no-database"
_WEEK_PATTERN = re.compile(r"^(\d{4})-W(\d{2})$")
Week = tuple[int, int]
Counts = tuple[int, int, str, str]


class WeekError(ValueError):
    pass


def parse_week(value: str) -> Week:
    match = _WEEK_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise WeekError(f"invalid ISO week {value!r}; expected YYYY-Www")
    year, week = (int(part) for part in match.groups())
    try:
        date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise WeekError(f"invalid ISO week {value!r}; {exc}") from exc
    return year, week


def format_week(week: Week) -> str:
    return f"{week[0]:04d}-W{week[1]:02d}"


def week_bounds(week: Week) -> tuple[str, str]:
    start = date.fromisocalendar(week[0], week[1], 1)
    end = start + timedelta(days=7)
    return (
        datetime(start.year, start.month, start.day, tzinfo=timezone.utc).isoformat(),
        datetime(end.year, end.month, end.day, tzinfo=timezone.utc).isoformat(),
    )


def previous_week(week: Week) -> Week:
    start = date.fromisocalendar(week[0], week[1], 1)
    return (start - timedelta(days=7)).isocalendar()[:2]


def default_week(now: datetime | None = None) -> Week:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    instant = instant.astimezone(timezone.utc)
    year, week, _ = instant.isocalendar()
    monday = date.fromisocalendar(year, week, 1)
    return (monday - timedelta(days=7)).isocalendar()[:2]


def reproduction_command(state_dir: Path, week: Week) -> str:
    state_text = str(Path(state_dir).expanduser().resolve())
    if any(not character.isprintable() for character in state_text):
        raise ValueError("state directory path must contain only printable characters")
    state_argument = shlex.quote(state_text)
    command = (
        f"twill digest --week {format_week(week)} --stdout "
        f"--state-dir {state_argument}"
    )
    if (
        redact_text(state_text) != state_text
        or len(command) > MAX_LINE_LENGTH - len(" | $ ") - 1
    ):
        state_argument = (
            '"${TWILL_STATE_DIR:?set TWILL_STATE_DIR to the state directory '
            'used for this report}"'
        )
        command = (
            f"twill digest --week {format_week(week)} --stdout "
            f"--state-dir {state_argument}"
        )
    return command


def _one_line(value: object, limit: int) -> str:
    return " ".join(redact_text(value).split())[:limit]


def _display_key(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)[1:-1]


def _line(claim: str, command: str) -> str:
    suffix = f" | $ {command}"
    available = MAX_LINE_LENGTH - len(suffix)
    if available < 1:
        raise ValueError("digest reproduction command exceeds the line budget")
    safe_claim = _one_line(claim, MAX_LINE_LENGTH)
    if len(safe_claim) <= available:
        return safe_claim + suffix
    if available <= 3:
        return safe_claim[:available] + suffix
    return safe_claim[: available - 3] + "..." + suffix


@dataclass(frozen=True)
class _DetectorWindow:
    detector: Detector
    status: str
    clusters: dict[str, Counts]
    error: str | None


@dataclass(frozen=True)
class DetectorSummary:
    detector_id: str
    version: int
    current_status: str
    previous_status: str
    current_clusters: int
    previous_clusters: int
    current_error: str | None
    previous_error: str | None

    @property
    def full_id(self) -> str:
        return f"{self.detector_id}@{self.version}"


@dataclass(frozen=True)
class Finding:
    n: int
    verdict: str
    detector: str
    key: str
    current: Counts | None
    previous: Counts | None
    week: str
    reproduce: str


@dataclass(frozen=True)
class DigestReport:
    state_dir: Path
    week: Week
    prior_week: Week
    command: str
    database: bool
    observations: int
    observations_in_week: int
    observations_in_previous_week: int
    detectors: tuple[DetectorSummary, ...]
    findings: tuple[Finding, ...]
    warnings: tuple[str, ...]

    @property
    def week_id(self) -> str:
        return format_week(self.week)

    @property
    def previous_week_id(self) -> str:
        return format_week(self.prior_week)

    @property
    def clean(self) -> bool:
        return (
            self.database
            and not self.findings
            and all(
                detector.current_status == STATUS_OK
                and detector.previous_status == STATUS_OK
                for detector in self.detectors
            )
        )


def _read_window(
    source: sqlite3.Connection,
    detectors: Sequence[Detector],
    start: str,
    end: str,
) -> tuple[_DetectorWindow, ...]:
    columns = tuple(
        (str(row[1]), str(row[2] or "TEXT"))
        for row in source.execute("PRAGMA table_info(observation)")
    )
    if not columns:
        raise sqlite3.DatabaseError("observation table has no columns")
    quoted_columns = ", ".join(
        '"' + name.replace('"', '""') + '"' for name, _ in columns
    )
    declarations = ", ".join(
        '"' + name.replace('"', '""') + '" ' + declaration
        for name, declaration in columns
    )
    placeholders = ", ".join("?" for _ in columns)
    memory = sqlite3.connect(":memory:")
    try:
        memory.execute(f"CREATE TABLE observation ({declarations})")
        memory.executemany(
            f"INSERT INTO observation ({quoted_columns}) VALUES ({placeholders})",
            source.execute(
                f"SELECT {quoted_columns} FROM observation "
                "WHERE ts_utc >= ? AND ts_utc < ?",
                (start, end),
            ),
        )
        memory.execute("PRAGMA query_only = ON")
        results: list[_DetectorWindow] = []
        for detector in detectors:
            try:
                twill_detectors.validate_detector_semantics(source, detector)
                clusters = read_clusters(
                    memory,
                    detector,
                    window_start_utc=start,
                    window_days=7,
                )
            except (sqlite3.Error, DetectorContractError) as exc:
                results.append(
                    _DetectorWindow(
                        detector=detector,
                        status=STATUS_ERROR,
                        clusters={},
                        error=_one_line(exc, MAX_ERROR_LENGTH),
                    )
                )
            else:
                results.append(
                    _DetectorWindow(
                        detector=detector,
                        status=STATUS_OK,
                        clusters=clusters,
                        error=None,
                    )
                )
        return tuple(results)
    finally:
        memory.close()


def _verdict(current: Counts | None, previous: Counts | None) -> str | None:
    if current is not None and previous is None:
        return VERDICT_NEW
    if current is None and previous is not None:
        return VERDICT_GONE
    if current is None or previous is None:
        return None
    if current[0] > previous[0] or current[1] > previous[1]:
        return VERDICT_WORSENING
    if current[0] < previous[0] or current[1] < previous[1]:
        return VERDICT_IMPROVING
    return None


def _finding_data(finding: Finding) -> dict[str, object]:
    current = finding.current
    previous = finding.previous
    return {
        "n": finding.n,
        "verdict": finding.verdict,
        "detector": finding.detector,
        "key": finding.key,
        "sessions": current[0] if current is not None else None,
        "events": current[1] if current is not None else None,
        "first_seen": current[2] if current is not None else None,
        "last_seen": current[3] if current is not None else None,
        "previous": (
            {
                "sessions": previous[0],
                "events": previous[1],
                "first_seen": previous[2],
                "last_seen": previous[3],
            }
            if previous is not None
            else None
        ),
        "week": finding.week,
        "reproduce": finding.reproduce,
    }


def build_digest(
    state_dir: Path,
    week: Week,
    *,
    registry: Sequence[Detector] = twill_detectors.REGISTRY,
) -> DigestReport:
    state = Path(state_dir).expanduser().resolve()
    prior = previous_week(week)
    start, end = week_bounds(week)
    prior_start, prior_end = week_bounds(prior)
    command = reproduction_command(state, week)
    detectors = twill_detectors.build_registry(*registry)
    db_path = state_db_path(state)
    if not db_path.is_file():
        summaries = tuple(
            DetectorSummary(
                detector_id=detector.detector_id,
                version=detector.version,
                current_status=STATUS_NO_DATABASE,
                previous_status=STATUS_NO_DATABASE,
                current_clusters=0,
                previous_clusters=0,
                current_error=None,
                previous_error=None,
            )
            for detector in detectors
        )
        warning = _one_line(
            f"no state database at {db_path}; nothing has been ingested yet, "
            "so no detector ran",
            MAX_ERROR_LENGTH,
        )
        return DigestReport(
            state_dir=state,
            week=week,
            prior_week=prior,
            command=command,
            database=False,
            observations=0,
            observations_in_week=0,
            observations_in_previous_week=0,
            detectors=summaries,
            findings=(),
            warnings=(warning,),
        )

    source = connect_read_only(state)
    try:
        source.execute("BEGIN")
        observations = int(
            source.execute("SELECT count(*) FROM observation").fetchone()[0]
        )
        observations_in_week = int(
            source.execute(
                "SELECT count(*) FROM observation "
                "WHERE ts_utc >= ? AND ts_utc < ?",
                (start, end),
            ).fetchone()[0]
        )
        observations_in_previous_week = int(
            source.execute(
                "SELECT count(*) FROM observation "
                "WHERE ts_utc >= ? AND ts_utc < ?",
                (prior_start, prior_end),
            ).fetchone()[0]
        )
        current_results = _read_window(source, detectors, start, end)
        previous_results = _read_window(source, detectors, prior_start, prior_end)
    finally:
        source.close()

    summaries: list[DetectorSummary] = []
    warnings: list[str] = []
    changes: list[tuple[str, str, str, Counts | None, Counts | None]] = []
    for current_result, previous_result in zip(current_results, previous_results):
        full_id = current_result.detector.full_id
        summaries.append(
            DetectorSummary(
                detector_id=current_result.detector.detector_id,
                version=current_result.detector.version,
                current_status=current_result.status,
                previous_status=previous_result.status,
                current_clusters=len(current_result.clusters),
                previous_clusters=len(previous_result.clusters),
                current_error=current_result.error,
                previous_error=previous_result.error,
            )
        )
        if current_result.error is not None and previous_result.error is not None:
            warnings.append(
                f"{full_id} failed for {format_week(prior)} and {format_week(week)}: "
                f"{current_result.error}; {previous_result.error}"
            )
        elif current_result.error is not None:
            warnings.append(
                f"{full_id} failed for {format_week(week)}: {current_result.error}"
            )
        elif previous_result.error is not None:
            warnings.append(
                f"{full_id} failed for {format_week(prior)}; "
                f"{format_week(week)} is not comparable"
            )
        if current_result.status != STATUS_OK or previous_result.status != STATUS_OK:
            continue
        keys = current_result.clusters.keys() | previous_result.clusters.keys()
        for key in keys:
            verdict = _verdict(
                current_result.clusters.get(key),
                previous_result.clusters.get(key),
            )
            if verdict is not None:
                changes.append(
                    (
                        verdict,
                        full_id,
                        key,
                        current_result.clusters.get(key),
                        previous_result.clusters.get(key),
                    )
                )

    verdict_rank = {verdict: index for index, verdict in enumerate(VERDICT_ORDER)}
    changes.sort(key=lambda row: (verdict_rank[row[0]], row[1], row[2]))
    findings = tuple(
        Finding(
            n=index,
            verdict=verdict,
            detector=detector,
            key=key,
            current=current,
            previous=previous,
            week=format_week(week),
            reproduce=command,
        )
        for index, (verdict, detector, key, current, previous) in enumerate(
            changes,
            start=1,
        )
    )
    return DigestReport(
        state_dir=state,
        week=week,
        prior_week=prior,
        command=command,
        database=True,
        observations=observations,
        observations_in_week=observations_in_week,
        observations_in_previous_week=observations_in_previous_week,
        detectors=tuple(summaries),
        findings=findings,
        warnings=tuple(warnings),
    )


def _detector_data(detector: DetectorSummary) -> dict[str, object]:
    return {
        "detector": detector.full_id,
        "current_status": detector.current_status,
        "previous_status": detector.previous_status,
        "current_clusters": detector.current_clusters,
        "previous_clusters": detector.previous_clusters,
        "current_error": detector.current_error,
        "previous_error": detector.previous_error,
    }


def render_data(report: DigestReport) -> dict[str, object]:
    start, end = week_bounds(report.week)
    prior_start, prior_end = week_bounds(report.prior_week)
    return {
        "week": report.week_id,
        "week_start": start,
        "week_end": end,
        "previous_week": report.previous_week_id,
        "previous_week_start": prior_start,
        "previous_week_end": prior_end,
        "clean": report.clean,
        "observations": report.observations,
        "observations_in_week": report.observations_in_week,
        "observations_in_previous_week": report.observations_in_previous_week,
        "detectors": [_detector_data(detector) for detector in report.detectors],
        "findings": [_finding_data(finding) for finding in report.findings],
        "reproduction_command": report.command,
    }


def _count_text(count: tuple[int, int] | None) -> str:
    if count is None:
        return "? sessions/? events"
    return f"{count[0]} sessions/{count[1]} events"


def render_text(report: DigestReport) -> str:
    start, end = week_bounds(report.week)
    prior_start, prior_end = week_bounds(report.prior_week)
    lines = [
        _line("TWILL digest", report.command),
        _line(f"week: {report.week_id} ({start} to {end})", report.command),
        _line(
            f"previous week: {report.previous_week_id} "
            f"({prior_start} to {prior_end})",
            report.command,
        ),
        _line(
            f"observations: {report.observations} total, "
            f"{report.observations_in_week} current, "
            f"{report.observations_in_previous_week} previous",
            report.command,
        ),
    ]
    detector_text = ", ".join(
        f"{detector.full_id} {detector.current_status} "
        f"{detector.current_clusters}/{detector.previous_clusters}"
        for detector in report.detectors
    )
    lines.append(
        _line(
            f"detectors current/previous: {detector_text or 'none registered'}",
            report.command,
        )
    )
    for detector in report.detectors:
        if detector.current_error is not None:
            lines.append(
                _line(
                    f"detector error: {detector.full_id} {report.week_id}: "
                    f"{detector.current_error}",
                    report.command,
                )
            )
        if detector.previous_error is not None:
            lines.append(
                _line(
                    f"detector error: {detector.full_id} {report.previous_week_id}: "
                    f"{detector.previous_error}",
                    report.command,
                )
            )
    for verdict in VERDICT_ORDER:
        selected = [
            finding for finding in report.findings if finding.verdict == verdict
        ]
        if not selected:
            continue
        lines.append(_line(f"{verdict}: {len(selected)}", report.command))
        for finding in selected:
            current = (
                (finding.current[0], finding.current[1])
                if finding.current is not None
                else None
            )
            previous = (
                (finding.previous[0], finding.previous[1])
                if finding.previous is not None
                else None
            )
            lines.append(
                _line(
                    f"- {finding.n} {finding.verdict} {finding.detector} "
                    f"{_display_key(finding.key)} "
                    f"{_count_text(previous)}->{_count_text(current)}",
                    report.command,
                )
            )
    ran = ", ".join(detector.full_id for detector in report.detectors) or "none registered"
    if report.clean:
        clean = f"clean week: no findings; detectors ran: {ran}"
    elif not report.database:
        clean = "clean week: no; no state database; no detector ran"
    else:
        failed = [
            detector.full_id
            for detector in report.detectors
            if detector.current_status != STATUS_OK
            or detector.previous_status != STATUS_OK
        ]
        if failed:
            clean = f"clean week: no; detector failures: {', '.join(failed)}"
        else:
            clean = f"clean week: no; {len(report.findings)} finding(s)"
    lines.append(_line(clean, report.command))
    return "\n".join(lines) + "\n"


def write_digest_file(text: str, artifacts_root: Path, week: Week) -> Path:
    directory = Path(artifacts_root).expanduser().resolve() / "digests"
    if directory.is_symlink():
        raise ValueError("digest directory may not be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True, mode=DIGEST_DIR_MODE)
    os.chmod(directory, DIGEST_DIR_MODE)
    path = directory / f"{format_week(week)}.txt"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("digest path is not a regular file")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory,
        prefix=f".{format_week(week)}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, DIGEST_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, DIGEST_FILE_MODE)
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return path
