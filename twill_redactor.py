"""Redaction for untrusted transcript text.

The persistence boundary is the last point at which transcript content is
available in its original form. Keep the credential patterns in one place,
apply configured content fences there as well, and only then truncate excerpt
fields. The pattern inventory is documented in ``docs/notes``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


MAX_EXCERPT_LENGTH = 240
CONTENT_FENCE_MARKER = "<redacted:content-fence>"


# Keep this tuple as the code counterpart to docs/notes/redaction-patterns.md.
# Patterns are intentionally conservative about the surrounding text: only
# the credential-shaped value is replaced where the format makes that safe.
CREDENTIAL_PATTERNS = (
    (
        re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_-]{12,}"),
        "<redacted:github-token>",
    ),
    (
        re.compile(r"(?i)\bgithub_pat_[A-Za-z0-9_]{12,}"),
        "<redacted:github-token>",
    ),
    (
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "<redacted:aws-access-key>",
    ),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        "Bearer <redacted:bearer-token>",
    ),
    (
        re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}"),
        "<redacted:api-key>",
    ),
    (
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
        "<redacted:slack-token>",
    ),
    (
        re.compile(
            r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|token)\s*[:=]\s*)(['\"]?)[^\s,'\"]+"
        ),
        r"\1\2<redacted:secret>",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z ]+ PRIVATE KEY-----.*?-----END [A-Z ]+ PRIVATE KEY-----",
            re.DOTALL,
        ),
        "<redacted:private-key>",
    ),
)


class Redactor:
    """Apply credential and content-fence redaction consistently.

    ``redact_text`` does not impose a length limit because identifiers and
    paths are not excerpts. Callers storing excerpts must use
    ``redact_excerpt`` so redaction always happens before truncation.
    """

    def __init__(self, content_fences: Iterable[str] = ()) -> None:
        fences = tuple(dict.fromkeys(fence for fence in content_fences if fence))
        self.content_fences = fences
        if fences:
            # Longest first prevents a shorter configured fence from exposing
            # the remainder of a longer entity in the same input.
            alternatives = "|".join(
                re.escape(fence) for fence in sorted(fences, key=len, reverse=True)
            )
            self._content_fence_pattern: re.Pattern[str] | None = re.compile(
                alternatives, re.IGNORECASE
            )
        else:
            self._content_fence_pattern = None

    def redact_text(self, text: object | None) -> str:
        """Return safe text without truncating non-excerpt fields."""

        if text is None:
            return ""
        redacted = str(text).replace("\x00", "")
        for pattern, replacement in CREDENTIAL_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        if self._content_fence_pattern is not None:
            redacted = self._content_fence_pattern.sub(CONTENT_FENCE_MARKER, redacted)
        return redacted.strip()

    def redact_excerpt(self, text: object | None) -> str:
        """Redact first, then cap an excerpt at the persistence limit."""

        return self.redact_text(text)[:MAX_EXCERPT_LENGTH]


_DEFAULT_REDACTOR = Redactor()


def redact(text: object | None, content_fences: Iterable[str] = ()) -> str:
    """Compatibility helper returning a safe, bounded excerpt."""

    if content_fences:
        return Redactor(content_fences).redact_excerpt(text)
    return _DEFAULT_REDACTOR.redact_excerpt(text)


def redact_text(text: object | None, content_fences: Iterable[str] = ()) -> str:
    """Return safe text without applying the excerpt limit."""

    if content_fences:
        return Redactor(content_fences).redact_text(text)
    return _DEFAULT_REDACTOR.redact_text(text)
