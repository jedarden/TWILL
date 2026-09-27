"""Deterministic routing recommendations for reviewed friction."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from twill_contract import ValidationError


ROUTING_LAYER_ORDER = (
    "environment",
    "hook",
    "wrapper",
    "skill",
    "agents_md",
    "memory",
    "retrieval_only",
)
MAX_ROUTING_REASON_LENGTH = 240
_DETECTOR_RE = re.compile(r"D-\d{2,}")


@dataclass(frozen=True)
class RoutingRecommendation:
    """The strongest justified layer and the reason it is recorded."""

    recommended: str
    reason: str


def rank_routing_layers(layers: Iterable[str]) -> tuple[str, ...]:
    """Return known routing layers in strongest-to-weakest order."""

    selected = set(layers)
    unknown = selected.difference(ROUTING_LAYER_ORDER)
    if unknown:
        choices = ", ".join(sorted(unknown))
        raise ValidationError(f"unknown routing layer(s): {choices}")
    return tuple(layer for layer in ROUTING_LAYER_ORDER if layer in selected)


def _validated_inputs(detector: str, key: str, sessions: int) -> str:
    if not isinstance(detector, str):
        raise ValidationError("routing detector must be text")
    base = detector.partition("@")[0]
    if _DETECTOR_RE.fullmatch(base) is None:
        raise ValidationError("routing detector must be a detector identifier")
    if not isinstance(key, str) or not key:
        raise ValidationError("routing key must be non-empty text")
    if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 1:
        raise ValidationError("routing sessions must be a positive integer")
    return base


def _recommendation(recommended: str, reason: str) -> RoutingRecommendation:
    rank_routing_layers((recommended,))
    if not reason or len(reason) > MAX_ROUTING_REASON_LENGTH:
        raise ValidationError("routing reason must contain 1 to 240 characters")
    return RoutingRecommendation(recommended=recommended, reason=reason)


def recommend_routing(
    *,
    detector: str,
    key: str,
    sessions: int,
) -> RoutingRecommendation:
    """Recommend the strongest intervention established by the cluster evidence."""

    base = _validated_inputs(detector, key, sessions)
    if base == "D-01" and key.startswith("command-not-found:"):
        return _recommendation(
            "environment",
            f"The command was missing across {sessions} sessions; installing or "
            "repairing it prevents the failure before an agent can invoke it.",
        )
    return _recommendation(
        "retrieval_only",
        f"Detector {base} shows recurrence across {sessions} sessions but proves "
        "no stronger prevention point; retrieval is the strongest justified layer "
        "until such a point is established.",
    )


def bead_create_command(*, title: str, body: str, detector: str) -> str:
    """Render the bead creation invocation for a human to run in the owner repo.

    This function only serializes arguments.  In particular, it must never
    invoke the bead CLI: creating work in another repository is an explicit
    human operation.
    """

    if not isinstance(title, str) or not title:
        raise ValidationError("bead title must be non-empty text")
    if not isinstance(body, str):
        raise ValidationError("lesson body must be text")
    if not isinstance(detector, str) or _DETECTOR_RE.fullmatch(detector) is None:
        raise ValidationError("bead detector must be a detector identifier")
    return shlex.join(
        (
            "bead",
            "create",
            "--title",
            title,
            "--description",
            body,
            "--label",
            f"detector:{detector}",
        )
    )


__all__ = [
    "MAX_ROUTING_REASON_LENGTH",
    "ROUTING_LAYER_ORDER",
    "RoutingRecommendation",
    "bead_create_command",
    "rank_routing_layers",
    "recommend_routing",
]
