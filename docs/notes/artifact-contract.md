# `artifacts_root` interchange contract

This is the v1 contract between TWILL and a downstream recall consumer. The
artifact repository is private and is separate from this public engine
repository. A consumer reads a committed checkout of that repository; it does
not read TWILL's state database or transcript sources.

## Snapshot layout

The root contains a machine-readable `manifest.json` and four reserved
namespaces. A namespace may be absent when it has no artifacts yet, but every
published artifact must use one of these paths:

| Path | Schema | Meaning |
| --- | --- | --- |
| `lessons/L-<8 lowercase hex>.md` | `twill-lesson/v1` | reviewed lesson lifecycle record |
| `digests/YYYY-Www.txt` | `twill-digest/v1` | completed ISO-week human report |
| `measurements/L-<8 lowercase hex>.jsonl` | `twill-measurement/v1` | durable daily measurements for one lesson |
| `guards/L-<8 lowercase hex>.<template>` | `twill-guard/v1` | human-installable guard proposal |

`<template>` is one of `environment.md`, `hook.json`, `wrapper.sh`,
`gate.txt`, `skill.md`, `agents.md`, `memory.md`, or `retrieval.md`. The
lesson id in a measurement or guard filename must name the corresponding
lesson. Unknown files outside these namespaces (for example `README.md` or
`.git` metadata) are not part of the interchange surface and must be ignored.

## Manifest metadata

`manifest.json` is written at the artifact root after every successful TWILL
artifact write. Its required v1 fields are:

```json
{
  "schema": "twill-artifacts/v1",
  "producer": "twill",
  "generated_at": "2026-09-28T12:00:00Z",
  "paths": {
    "lessons": {"pattern": "lessons/L-<8 lowercase hex>.md", "schema": "twill-lesson/v1"},
    "digests": {"pattern": "digests/YYYY-Www.txt", "schema": "twill-digest/v1"},
    "measurements": {"pattern": "measurements/L-<8 lowercase hex>.jsonl", "schema": "twill-measurement/v1"},
    "guards": {"pattern": "guards/L-<8 lowercase hex>.<template>", "schema": "twill-guard/v1"}
  },
  "artifacts": [
    {"path": "lessons/L-0123abcd.md", "schema": "twill-lesson/v1", "bytes": 512, "sha256": "<64 lowercase hex>"}
  ]
}
```

`artifacts` is the complete inventory of recognized artifact files in the
snapshot. `bytes` and `sha256` let a consumer detect a partial checkout or a
changed file before indexing it. The manifest does not contain absolute paths,
transcript text, evidence excerpts, credentials, or any secret value.

The v1 compatibility rule is deliberately small: a reader MUST accept the
required fields above and additive unknown fields, MUST ignore unknown files
outside the four namespaces, and MUST reject an unknown contract version,
malformed path, duplicate inventory entry, missing inventory entry, symlink,
or hash/size mismatch. A future incompatible layout uses a new major schema
(`twill-artifacts/v2`) and is not silently treated as v1.

## Artifact schemas

- **Lessons** are UTF-8 Markdown with the existing `---` front matter. The
  required fields are `id`, `summary`, `state`, `detector`, `key`, `evidence`,
  `routing`, and `backtest`; `guard` is nullable metadata. Evidence contains
  at least one session id. States and routing transitions are validated by
  TWILL; consumers should treat a lesson as retrieval-eligible only when its
  state is `accepted`, `applied:<layer>`, or a terminal state retaining its
  applied layer. Every body line and quoted field is redacted and at most 240
  characters.
- **Digests** are UTF-8 text named for a completed ISO week. Every line is
  redacted, bounded to 240 characters, and ends with the reproducible command
  suffix emitted by TWILL. Digests are human reports, not the recall index's
  lesson source of truth.
- **Measurements** are JSONL, one object per non-empty line, with exactly
  `lesson_id`, `detector_id`, `measured_at`, `window_days`, `sessions`, and
  `events`. `detector_id` includes its version (`D-NN@N`); there is at most one
  point per UTC day per lesson file. Covered clusters are reported in command
  output and their detector-owned history, not as synthetic lesson files.
- **Guards** are proposals, never installed policy. The hook JSON carries
  `schema: twill-guard/v1`, `lesson_id`, `detector`, `key`, and a human-only
  install block. Other templates retain their human-readable format and are
  identified by the manifest schema and filename suffix. Consumers MUST NOT
  install or inject a guard merely because it is present.

## Publication semantics

There are three owners and two snapshot boundaries:

1. **TWILL owns production.** It writes only below the configured
   `artifacts_root`, stages each file in that same directory, fsyncs it, and
   atomically renames it into place. It then regenerates `manifest.json` from
   the complete tree and atomically replaces the old manifest. The
   `manifest_after_write` boundary restores the touched files when manifest
   generation fails, so the previous valid local snapshot remains available
   for a retry. Temporary dot-files are never inventory entries. TWILL does
   not commit, push, or write an artifact into this public repository.
2. **The private-repository publisher owns transport.** The operator or
   publisher process validates the external tree with `read_manifest`, stages
   the changed artifact files and `manifest.json` together, and creates one
   normal commit. The commit is the immutable snapshot boundary: an artifact
   and its manifest are either both in the commit tree or neither is. The
   publisher pushes that commit to the configured private Forgejo `origin`
   without force-pushing. A working tree, index, or unpushed local commit is
   not visible to recall and is not a publication.
3. **The recall service owns consumption.** It fetches a commit, materializes
   that exact commit into a disposable staging directory, runs the v1 reader
   there, and builds its disposable index there. It atomically swaps the
   index only after the manifest, inventory, hashes, and payload validation
   all pass. It never indexes a live publisher worktree.

The publisher must use this sequence for every attempt:

```text
write/repair artifacts_root
  -> read_manifest(artifacts_root)
  -> stage manifest.json and changed artifact files
  -> validate the staged tree
  -> commit one snapshot
  -> push that commit to private origin
```

The second validation is important: validating the producer's worktree does
not prove that the staged index contains the same bytes. The publisher must
also ensure the staged diff contains no unrelated paths and that the commit
contains `manifest.json` with every inventory entry. A failed validation or
commit leaves the last remote commit unchanged. A transient push failure is
retried with the same local commit; it must not regenerate artifacts or amend
the commit. If the remote has advanced, stop and reconcile the private
repository before retrying—never force-push or silently discard either
snapshot.

The recall retry is similarly fail-closed: fetch failures, incomplete
checkouts, manifest errors, hash mismatches, and index-build failures discard
only the temporary candidate and retain the last valid index. The next poll
retries the same remote commit (or a newly fetched one). A worktree with a
changed artifact but an old manifest, or a manifest advertising a missing or
partially written artifact, is therefore rejected rather than indexed.

Git's commit tree is the cross-process publication boundary; the atomic file
renames are only the local producer boundary. A consumer reading the external
worktree during the short interval between those renames may see an old
manifest beside new bytes, but `read_manifest` rejects that mixed state. Only
the validated tree of one committed snapshot may replace the recall index.

The existing containment boundary remains unchanged: `artifacts_root` is
required, must resolve outside the public TWILL tree, and is the only place
these four artifact namespaces may be written. The public checkout continues
to gitignore all four namespaces, audit writes, reject secrets before
persistence, and fail the published-tree check if any artifact leaks into it.
