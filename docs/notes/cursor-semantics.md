# Cursor bookkeeping semantics

The reviewable decision record for `twill_cursor.py` (plan §7.1, §8.1
EC-02..EC-05). The plan fixes the arithmetic — identity hash of the first
4 KiB, resume at `last_offset`, reparse on shrink or identity change — and
this note pins the three refinements the edge cases forced, each of which the
fixture corpus exercises (`tests/fixtures/transcripts/*/`).

## Which bytes count as a complete line

A line is complete when a `\n` terminates it. Only complete lines are ever
parsed, and `last_offset` only ever advances to a position immediately after
a `\n` (plan §8.3 invariant) — with one deliberate split inside EC-04:

- **Unterminated tail** (bytes after the last `\n`): left entirely for the
  next run. The producer may still be extending that very line, so
  committing a parse of it now would either store half a record or, worse,
  store a "complete" record the producer then continues. `parse_errors`
  counts 1 for the pending tail and the counter stays at 1 across idle
  passes — that stability is what `doctor`'s "parse_errors > 0 on three
  consecutive runs" alarm (EC-04) keys on.
- **Newline-terminated but invalid JSON** (the corpus's
  `truncated-final-line.jsonl` shape: the record is torn but its newline was
  flushed): the producer will never repair this line, so the cursor advances
  past it and it is counted in `parse_errors` for the pass that consumed it.
  Stopping instead would let one corrupt flushed line block every later
  append to the file forever.

Consequence: `parse_errors` reads as "unusable lines in the most recent pass
that actually read bytes", not a lifetime total. A later clean append clears
it; an idle run leaves it untouched.

## Identity comparison re-hashes the stored span, not the current prefix

The stored `identity_sha` covered `min(4096, size_at_the_time)` bytes. A file
that grows *past* the 4 KiB boundary has a different first-4-KiB digest than
the whole-file digest stored when it was short, so a literal digest compare
would reparse every file exactly once in its life for no reason. The
comparison therefore hashes `min(4096, cursor.size)` bytes of the current
file — the exact span the stored digest covered — which answers precisely
"did any byte I already hashed change". Growth is an append; only a changed
prefix is a rewrite.

`mtime_ns` is recorded but never decides anything: an append bumps it just as
a rewrite does, so keying reparse on mtime would reparse every growing file.

## What a vanished file does and does not change

`path_missing` (an additive column, applied idempotently to pre-v1.1
databases by `twill_schema.ADDITIVE_COLUMNS`) is the only thing that moves
when an upstream transcript disappears (EC-05). The sweep checks each stored
path's existence directly — not membership in the current run's candidate
list — so a file skipped by the settle window or a narrowed source glob is
never mistaken for a vanished one, and the sweep runs even on a run that
finds no settled files at all, because a fully cleaned transcript tree is
exactly when the flag matters. Observations, events and the resume position
survive untouched: absence is never a reason to delete evidence.
