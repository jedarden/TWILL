# The test-suite audit harness

The mechanical enforcement of the §3 and §8.3 invariants that the test suite
can observe — no open for writing outside the allowed write roots ("The
write boundary" below), "no path under `~/agent-transcript-archive` is ever
opened at all", no third-party Python import in the core path, and no Python
network call except the local `claude -p` child — plus §10.2's stop-ship
gate. The harness is `tests/openpath.py`; it is exercised by
`tests/test_openpath.py` and `tests/test_audit.py`. Ideas
ledger #77 rejected auditing in the engine's hot path; this is the selected
alternative: the whole *test suite* runs under the hook, so every change to
the engine is audited at development time, and the running timers pay
nothing.

## The write boundary

§8.3 words the write invariant tersely — "no file outside `~/TWILL` and
`~/.local/state/twill` is ever opened for writing" — naming the engine's own
two trees and leaving `artifacts_root` implicit; §3 and §10.2 state the same
contract in full ("read-only with respect to everything outside its own
repository, its state directory … and `artifacts_root`"; "any write outside
this repository + the state dir + `artifacts_root` … fails the run"). The
harness enforces the full form, and `artifacts_root` is not a relaxation of
it: §3, §7.2 and the README require every distilled artifact to live under
`artifacts_root` — the separate private repository; "nothing TWILL produces
lives here" — so it is the product's destination, not an exception granted
to the tests. The exact allowed write roots, in the order
`is_allowed_write` decides them:

1. **This repository** — except its top-level `lessons/`, `digests/`,
   `measurements/` and `guards/` names, which are denied outright ("no
   lesson, digest, measurement or guard artifact is ever written inside this
   repository's tree", §8.3 again — guard two of §10.2's three
   artifact-containment guards). Top-level names only, mirroring the
   `.gitignore` block: an artifact writer names `artifacts_root`, so a
   directory of the same name nested elsewhere in the tree is ordinary
   content, not a leak.
2. **The state directory** — `TWILL_STATE_DIR` when set, else
   `~/.local/state/twill`, the same resolution as the CLI's `--state-dir`
   default, so the gate and the engine cannot disagree about where the
   state tree is.
3. **`artifacts_root`** — from the operator config when one is loadable; a
   missing or unloadable config yields no allowance, which only narrows the
   gate, never widens it.
4. **The temp scratch root** — `tempfile.gettempdir()`, the one root the
   harness adds on its own beyond §10.2's three ("The temp root is the one
   broadening" below).

`os.devnull` is allowed alongside those (a write to it persists nothing),
and an fd-anchored open (`open(3, "w")`) addresses no path and is not
policed.

## Import and network gates

The same startup hook rejects an import whose top-level name is neither in
Python's standard library nor a module in this checkout. The runtime check
catches dynamic imports and is inherited by Python children through
`tests/sitecustomize.py`; `tests/test_audit.py` also walks every production
source file so a runner cannot hide a dependency by preloading it. `pytest`
and `ruff` remain development tools: the pytest runner has loaded before its
`conftest.py` installs the hook, and the production import walk excludes
`tests/`.

Python-level network audit events are refused at socket and stdlib client
boundaries. Explain's local `claude -p` invocation is the sole exception in
the design; it is an external process, so its own network traffic is outside
Python's audit-hook boundary. No other production code is permitted to spawn
a network-capable child.

## One audit event covers both entry points

`open()` and `os.open()` raise the same PEP 578 audit event, `open`, with
`(path, mode, flags)`: `io.open` fills in the mode word, `os.open` reports
`mode=None` and its intent through the flags. One hook therefore polices
both, plus `io.open_code` (mode `"r"`). Write intent is `w`/`a`/`x`/`+` in
the mode, or any of `O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND` in the flags
— `O_CREAT|O_RDONLY` creates the file on Linux, so it counts as a write.

## The gate is a hook, a startup, and an inheritance

- `tests/openpath.py` — the policy ("The write boundary" above). The
  repository is decided before the temp root, so the artifact names are
  denied even in a checkout that itself lives under the temp root — a clean
  extraction or a CI checkout in TMPDIR; the first extraction run caught the
  original ordering letting those through. A read must simply be
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
public tree) lives there.

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
