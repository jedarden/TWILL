# The settle gate

The reviewable decision record for ingest eligibility (plan §4 "settled
session", §8.1 EC-01). The plan fixes the rule — a transcript file is
eligible only when `now − mtime` is at least the settle window (default 2h,
configurable via `settle_window` in config.toml or `--settle`) — and this
note pins the refinements the rule's wording decides. Each is exercised by
`tests/test_settle_gate.py`.

## The gate runs at enumeration, before any byte is read

Eligibility is decided once per file in `settled_files` (`twill_app.py`),
from the mtime, before any parse. A file younger than the window is skipped
whole: it is never partially parsed, never leaves a cursor row, never
contributes an observation. Reading "just the complete lines so far" of a
growing session would be safe in the cursor's terms (EC-02 handles appended
bytes after settlement) but wrong in the plan's: the settle window exists
because a session's meaning can turn on the turn that has not been written
yet — the interrupt, the correction, the failure the successful retry hides.

## The boundary is inclusive

"At least the settle window" — `now − mtime >= settle` — is inclusive: a
file exactly one window old is eligible. Anything at or past the boundary
parses this run; anything inside it waits for the next.

## An explicitly named file crosses the same gate

`twill ingest --file` selects one file; it does not bypass EC-01. A fresh
explicit file is skipped like any other, and `--settle 0` is the declared
escape hatch — which is how controlled fixtures and the §12 single-file
parse-budget measurement run.

## A future mtime is never eligible

For a clock-skewed or forward-dated file, `now − mtime` is negative, which
is less than any window — including zero. The literal reading is the wanted
one: the file becomes eligible when the clock reaches its mtime, and
`--settle 0` means "everything settled as of now", not "everything
regardless".

## Related, and out of scope here

When every candidate is young, ingest exits 1 with `no settled JSONL
sessions found` rather than silently doing nothing; whether an entirely
empty run should instead be EC-14's exit 0 `no work` (with a `status.json`
update) is that edge case's own decision, not this gate's.
