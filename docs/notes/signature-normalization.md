# Error signature normalization

Error observations carry a normalized `signature` and a compact `sig_hash`.
The implementation is in `twill_app.py`; the values are derived at the
persistence boundary, after transcript text has passed through the redactor.

## Derivation

1. Redact credential-shaped values and configured content fences.
2. Strip the text and keep at most 400 characters for normalization.
3. Replace volatile values in this order: UUIDs, 40-character hashes, other
   hexadecimal identifiers, paths, and bare numbers.
4. Collapse whitespace and strip the result.
5. Store the normalized text as `signature` and the first 12 hexadecimal
   characters of its SHA-256 digest as `sig_hash`.

The raw excerpt remains separately redacted and bounded to 240 characters.
The signature is a grouping identity, not a replacement for the excerpt and
not a security boundary. Redaction happens before normalization so a masked
value cannot re-enter a hash input through a later transformation.

## Known limit

Normalization deliberately does not discard the context before a failure. If
one failure is reported with two different context prefixes, the prefixes can
produce two signatures even when the rest of the failure is identical. The
resulting count is therefore a floor, not a total: it can undercount one
failure, and it is not evidence that two signatures are two different
failures. The limit is visible rather than hidden by an over-broad prefix
strip.
