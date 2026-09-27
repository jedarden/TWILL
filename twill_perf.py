"""Performance measurements and hard budgets for the ingest path.

The ingest timer runs in a bounded systemd user service.  These checks are
deliberately strict: a value equal to a budget is a miss because the plan
defines all three limits as strict ``<`` limits.  A failed measurement is
also a miss; continuing without knowing the process high-water mark would
make the memory guard advisory.
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

# Short aliases keep call sites readable and make the public budget names
# discoverable to the test harness without duplicating the values.
INGEST_WALL_TIME_BUDGET_SECONDS = HOURLY_INGEST_WALL_TIME_BUDGET_SECONDS
SINGLE_FILE_PARSE_BUDGET_SECONDS = SINGLE_FILE_PARSE_TIME_BUDGET_SECONDS

PERFORMANCE_BUDGET_KEYS = frozenset(
    {"wall_time_seconds", "single_file_parse_seconds", "peak_rss_bytes"}
)
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


def has_miss(performance: Mapping[str, object]) -> bool:
    """Return whether a validated performance record contains a budget miss."""

    misses = performance.get("misses")
    return isinstance(misses, list) and bool(misses)
