"""Private-repository transport for validated TWILL artifact snapshots.

The contract implementation lives in :mod:`twill_artifacts` so producers and
the consumer-facing validator share one set of path and manifest rules.  This
module gives the transport its own import surface for operators and future
recall code.
"""

from twill_artifacts import (
    ArtifactPublicationError,
    PublicationResult,
    publish_artifacts,
    publish_snapshot,
    read_committed_manifest,
)

__all__ = [
    "ArtifactPublicationError",
    "PublicationResult",
    "publish_artifacts",
    "publish_snapshot",
    "read_committed_manifest",
]
