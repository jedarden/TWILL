# Rule corpus indexing semantics

The reviewable decision record for `twill_rulecorpus.py` (plan Phase 3, §7.1,
§8.1 EC-11). The plan fixes what the corpus is — MEMORY.md and its leaves,
CLAUDE.md, repo `AGENTS.md` files, skills — and that coverage matching is by
content hash + FTS, never path identity. This note pins the refinements the
edge case forced, each exercised in `tests/test_rulecorpus.py`.

## Identity is the whole file, not a prefix

`content_sha` is sha256 over the file's entire bytes. The cursor's
first-4-KiB identity hash (see `cursor-semantics.md`) is tuned for a growing
transcript, where the prefix answers "did the bytes I already read change";
a rule file is small and static, and an edit *past* any prefix boundary is
exactly the edit coverage must notice. The corpus therefore pays the full
hash, and `rule_doc.sha` means the same thing for every layer.

## The corpus is configured, not discovered

Each source is a `rule_globs` entry written `<layer>:<glob>` — the layer is
declared because path shape is precisely the identity this module must not
depend on: `~/**/AGENTS.md` and a moved memory leaf can both be `agents_md`
or `memory` tomorrow. Patterns apply in order and a file matched twice keeps
the first layer that claimed it, so overlapping globs resolve
deterministically instead of by map order. Config validates the grammar at
load (`twill_config._rule_glob_list`), because a typo must fail at startup,
not at the weekly rank pass that first consumes the corpus.

## A move is a stale row plus a hash match, never a rename

When a stored path vanishes (EC-11), its row is flagged `stale` — never
deleted, its FTS text kept — so coverage degrades to a visible,
doctor-reportable state instead of silently dropping a rule. The report
joins the stale row to any path discovered this run carrying the same sha:
that pair *is* the rename, detected by content, not by path.

Two consequences worth stating:

- **`last_read_by_agent` follows the hash.** A new path whose content
  matches an existing row adopts that row's newest read time. Identical
  content is the same rule, so its read history crosses a rename instead of
  feeding D-09 a false "never read". Two live copies of one file each keep
  their own row and share the hash; reads reported against either count for
  the content.
- **`stale` is a flag, not a tombstone.** The path reappearing clears it
  (`restored`), with or without new content; `doctor` (a later Phase 3 bead)
  is what reports stale rows to a human. Nothing here deletes.

## Existence is checked per stored path, not per discovery

The staleness sweep tests each stored path's existence directly — not
membership in this run's discovery — so a narrowed glob is never mistaken
for a vanished file, and the sweep runs even over a run that discovered
nothing, because an emptied corpus is exactly when staleness matters. This
is the cursor sweep's EC-05 discipline applied to rules.

## One transaction, idempotent when nothing changed

The whole index lands in one transaction: a crash part-way keeps the
previous index, and the next run recomputes it from the files. A document
whose sha and layer already match its row writes nothing, so re-indexing an
unchanged corpus is idempotent down to `indexed_at` — the content's first
indexing time, not the last walk over it.
