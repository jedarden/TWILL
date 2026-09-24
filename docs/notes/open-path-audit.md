# The open-path audit harness

The mechanical enforcement of the two §8.3 invariants that name paths —
"No file outside `~/TWILL` and `~/.local/state/twill` is ever opened for
writing" and "No path under `~/agent-transcript-archive` is ever opened at
all" — and of §10.2's open-path stop-ship gate. The harness is
`tests/openpath.py`; it is exercised by `tests/test_openpath.py`. Ideas
ledger #77 rejected auditing in the engine's hot path; this is the selected
alternative: the whole *test suite* runs under the hook, so every change to
the engine is audited at development time, and the running timers pay
nothing.

## One audit event covers both entry points

`open()` and `os.open()` raise the same PEP 578 audit event, `open`, with
`(path, mode, flags)`: `io.open` fills in the mode word, `os.open` reports
`mode=None` and its intent through the flags. One hook therefore polices
both, plus `io.open_code` (mode `"r"`). Write intent is `w`/`a`/`x`/`+` in
the mode, or any of `O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND` in the flags
— `O_CREAT|O_RDONLY` creates the file on Linux, so it counts as a write.

## The gate is a hook, a startup, and an inheritance

- `tests/openpath.py` — the policy. A write must land inside the
  repository (never inside its `lessons/`, `digests/`, `measurements/` or
  `guards/` names: §10.2's second artifact guard), the state directory
  (`TWILL_STATE_DIR` or `~/.local/state/twill`, the same resolution as the
  CLI), `artifacts_root`, or the temp scratch root. The repository is
  decided before the temp root, so the artifact names are denied even in a
  checkout that itself lives under the temp root — a clean extraction or a
  CI checkout in TMPDIR; the first extraction run caught the original
  ordering letting those through. A read must simply be
  outside `~/agent-transcript-archive` — any open under it fails, read or
  write, which is the stricter §8.3 wording. Violations raise
  `OpenPathViolation` (an `AssertionError`) at the open itself; the refused
  open never touches the disk.
- `tests/sitecustomize.py` — installs the hook at interpreter startup in
  any process that has `tests/` on its path. `make test` puts the directory
  there *before* `unittest` loads anything, so the parent is hooked from
  startup; `install()` also adds the directory to the inherited
  `PYTHONPATH`, so every Python child the suite spawns — each CLI verb —
  self-installs before its first open. That is what makes §5 Scenario 2's
  assertion ("no archive path is opened") cover the real
  `ingest → detect → digest` runs rather than only in-process calls.
- `tests/conftest.py` — the same install for pytest.

An entry point that skips both — a bare `python3 -m unittest discover -s
tests` — runs ungated, and `test_openpath.py` fails on purpose ("this
interpreter is gated"): a suite that is not audited does not pass.

## The temp root is the one broadening

§10.2 allows writes to "this repository + the state dir + `artifacts_root`";
the suite writes its fixtures and state directories under
`tempfile.gettempdir()` in 78 places, and pytest builds its tmp roots there.
So the harness also allows the temp root — scaffolding, not engine surface:
nothing the invariants protect (operator context, other repositories, the
public tree) lives there. It also allows `os.devnull` (a write to it
persists nothing) and ignores fd-anchored opens (`open(3, "w")`), which
address no path.

## No open happens inside the hook

The hook resolves every path with `Path.resolve()` — symlink-proof, and
made only of `stat`/`readlink`, the audit events the hook ignores. The two
policy inputs that *would* open a file are snapshotted in `install()`
before the hook goes live: `tempfile.gettempdir()` probes its candidates
with real opens, and `load_config()` opens the config file. Either called
from inside the hook re-enters it mid-open and recurses forever — the
first gated run hung in `_get_default_tempdir`, which is why
`install()` snapshots the temp root and the artifacts root up front.

## Known boundaries

The hook sees Python-level opens. SQLite's own C-level opens (WAL/SHM
sidecars, temp files) are invisible to it — the database *file* is created
through the engine's `os.open` and is visible — and non-Python children
(`make install`'s `install`/`ln`) are not audited. Those are covered by the
existing fixture tests, not by this gate; the gate's claim is exactly
§10.2's: every `open()` and `os.open()` the suite and its Python children
perform is policed.
