"""Performance measurements and hard budgets for the TWILL pipeline.

The ingest timer runs in a bounded systemd user service.  These checks are
deliberately strict: a value equal to a budget is a miss because the plan
defines every limit as a strict ``<`` limit.  A failed measurement is also a
miss; continuing without a measurement would make the guard advisory.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


# Plan §12: the hourly timer is expected to process a steady-state batch in
# less than two minutes, a single 10 MB transcript in less than five seconds,
# and never reach 500 MiB resident high-water mark.
HOURLY_INGEST_WALL_TIME_BUDGET_SECONDS = 120.0
SINGLE_FILE_PARSE_TIME_BUDGET_SECONDS = 5.0
PEAK_RSS_BUDGET_BYTES = 500 * 1024 * 1024
DETECT_PASS_WALL_TIME_BUDGET_SECONDS = 20.0
DB_SIZE_BUDGET_BYTES = 2 * 1024**3

# Short aliases keep call sites readable and make the public budget names
# discoverable to the test harness without duplicating the values.
INGEST_WALL_TIME_BUDGET_SECONDS = HOURLY_INGEST_WALL_TIME_BUDGET_SECONDS
SINGLE_FILE_PARSE_BUDGET_SECONDS = SINGLE_FILE_PARSE_TIME_BUDGET_SECONDS
DETECT_PASS_TIME_BUDGET_SECONDS = DETECT_PASS_WALL_TIME_BUDGET_SECONDS
DATABASE_SIZE_BUDGET_BYTES = DB_SIZE_BUDGET_BYTES

PERFORMANCE_BUDGET_KEYS = frozenset(
    {"wall_time_seconds", "single_file_parse_seconds", "peak_rss_bytes"}
)
DETECT_BUDGET_KEYS = frozenset({"wall_time_seconds"})
DB_SIZE_BUDGET_KEYS = frozenset({"db_bytes"})
_VMHWM_RE = re.compile(r"^VmHWM:\s+(?P<kilobytes>[0-9]+)\s+kB\s*$", re.MULTILINE)


class PerformanceMeasurementError(RuntimeError):
    """The kernel did not expose a trustworthy high-water mark."""


def read_peak_rss_bytes(status_path: Path = Path("/proc/self/status")) -> int:
    """Read Linux ``VmHWM`` and convert its KiB value to bytes.

    ``VmHWM`` is the process high-water mark, so it remains useful after the
    parser releases temporary allocations.  The explicit failure is safer
    than treating an unavailable procfs value as zero.
    """

    try:
        text = status_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise PerformanceMeasurementError(
            f"peak RSS cannot be read from {status_path}"
        ) from exc
    match = _VMHWM_RE.search(text)
    if match is None:
        raise PerformanceMeasurementError(
            f"VmHWM is missing from {status_path}"
        )
    return int(match.group("kilobytes")) * 1024


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return float(value)


def _budget_miss(name: str, value: float, budget: float) -> str | None:
    if value >= budget:
        return f"{name}={value:g} meets or exceeds budget {budget:g}"
    return None


def assess_ingest(
    wall_time_seconds: float,
    single_file_parse_seconds: float,
    peak_rss_bytes: int | None,
    *,
    checked_at: str | None = None,
) -> dict[str, object]:
    """Return a serialisable measurement and its hard-budget result."""

    wall = _number(wall_time_seconds, "wall_time_seconds")
    parse = _number(single_file_parse_seconds, "single_file_parse_seconds")
    if peak_rss_bytes is not None:
        if isinstance(peak_rss_bytes, bool) or not isinstance(peak_rss_bytes, int):
            raise ValueError("peak_rss_bytes must be an integer or null")
        if peak_rss_bytes < 0:
            raise ValueError("peak_rss_bytes must be non-negative")

    misses = [
        miss
        for miss in (
            _budget_miss(
                "wall_time_seconds",
                wall,
                HOURLY_INGEST_WALL_TIME_BUDGET_SECONDS,
            ),
            _budget_miss(
                "single_file_parse_seconds",
                parse,
                SINGLE_FILE_PARSE_TIME_BUDGET_SECONDS,
            ),
            (
                "peak_rss_bytes is unavailable; refusing to run without a VmHWM measurement"
                if peak_rss_bytes is None
                else _budget_miss(
                    "peak_rss_bytes",
                    float(peak_rss_bytes),
                    float(PEAK_RSS_BUDGET_BYTES),
                )
            ),
        )
        if miss is not None
    ]
    return {
        "checked_at": checked_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "wall_time_seconds": round(wall, 6),
        "single_file_parse_seconds": round(parse, 6),
        "peak_rss_bytes": peak_rss_bytes,
        "budgets": {
            "wall_time_seconds": HOURLY_INGEST_WALL_TIME_BUDGET_SECONDS,
            "single_file_parse_seconds": SINGLE_FILE_PARSE_TIME_BUDGET_SECONDS,
            "peak_rss_bytes": PEAK_RSS_BUDGET_BYTES,
        },
        "misses": misses,
    }


def assess_detect(
    wall_time_seconds: float,
    *,
    checked_at: str | None = None,
) -> dict[str, object]:
    """Return the all-detector pass measurement and hard-budget result."""

    wall = _number(wall_time_seconds, "wall_time_seconds")
    miss = _budget_miss(
        "wall_time_seconds", wall, DETECT_PASS_WALL_TIME_BUDGET_SECONDS
    )
    return {
        "checked_at": checked_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "wall_time_seconds": round(wall, 6),
        "budgets": {
            "wall_time_seconds": DETECT_PASS_WALL_TIME_BUDGET_SECONDS,
        },
        "misses": [miss] if miss is not None else [],
    }


def read_db_size_bytes(state_dir: Path) -> int:
    """Return the apparent bytes used by SQLite and its live sidecars.

    SQLite's WAL and shared-memory files are part of the database footprint
    while a writer is active.  Counting them with the main file is the
    ``du``-equivalent measurement the plan calls for, without spawning a
    shell command or including unrelated status files.
    """

    db_path = Path(state_dir) / "twill.db"
    total = 0
    for suffix in ("", "-wal", "-shm"):
        path = db_path.with_name(db_path.name + suffix)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PerformanceMeasurementError(
                f"database size cannot be read from {path}"
            ) from exc
        if size < 0:
            raise PerformanceMeasurementError(
                f"database size is negative for {path}"
            )
        total += size
    if total == 0:
        raise PerformanceMeasurementError(
            f"database size cannot be measured: {db_path} is missing"
        )
    return total


def assess_db_size(
    db_bytes: int,
    *,
    checked_at: str | None = None,
) -> dict[str, object]:
    """Return the retained database-size measurement and hard-budget result."""

    if isinstance(db_bytes, bool) or not isinstance(db_bytes, int):
        raise ValueError("db_bytes must be an integer")
    if db_bytes < 0:
        raise ValueError("db_bytes must be non-negative")
    miss = _budget_miss("db_bytes", float(db_bytes), float(DB_SIZE_BUDGET_BYTES))
    return {
        "checked_at": checked_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "db_bytes": db_bytes,
        "budgets": {"db_bytes": DB_SIZE_BUDGET_BYTES},
        "misses": [miss] if miss is not None else [],
    }


def has_miss(performance: Mapping[str, object]) -> bool:
    """Return whether a validated performance record contains a budget miss."""

    misses = performance.get("misses")
    return isinstance(misses, list) and bool(misses)
