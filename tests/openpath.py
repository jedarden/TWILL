"""The open-path audit harness (plan §8.3, §10.2).

Mechanical enforcement of the two pre-flight invariants the plan lists as
"must always hold":

- No file outside this repository and the state directory is ever opened for
  writing.
- No path under ``~/agent-transcript-archive`` is ever opened at all.

:func:`install` puts an audit hook on the ``open`` event -- the single event
both :func:`open` and ``os.open`` raise (PEP 578, verified: ``os.open``
reports its intent through ``flags`` with ``mode=None``) -- into the
interpreter running the test suite.  A write that has no allowed home, or any
open under the transcript archive, raises :class:`OpenPathViolation` at the
open itself, which fails the test that attempted it and therefore the run.
There is no warning mode and no suppression switch: §10.2 makes this a
stop-ship gate, and a gate with an opt-out is a convention, not a mechanism.

Every Python child the suite spawns -- each CLI verb run through
``subprocess`` -- installs the same hook before its first open, so the engine
is policed exactly as the in-process tests are and the Scenario 2 assertion
("no archive path is opened") covers the real ingest/detect/digest runs, not
just library calls.  The propagation has two halves:

- :func:`install` puts this directory on ``PYTHONPATH`` in ``os.environ``,
  which spawned interpreters inherit;
- ``tests/sitecustomize.py`` (imported by ``site`` in any interpreter that
  has this directory on its path) calls :func:`install` at startup, so a
  child is hooked before the verb runs.

The suite's own entry points keep that directory importable from the start:
``make test`` prepends it to ``PYTHONPATH`` (which also hooks the parent
before ``unittest`` imports anything) and ``tests/conftest.py`` installs the
hook for pytest.  An entry point that skips both -- a bare
``python3 -m unittest discover`` -- runs unhooked, which
``tests/test_openpath.py`` detects and fails; running the suite ungated is
supposed to be loud.

Allowed write trees, and why the harness is allowed to be broader than the
invariant it enforces:

- **this repository**, except ``lessons/``, ``digests/``, ``measurements/``
  and ``guards/`` inside it.  The in-tree artifact names are denied outright
  even though the repository is otherwise writable: that is guard two of the
  three artifact-containment guards (§10.2), and it is what makes "no lesson,
  digest, measurement or guard artifact is ever written inside this
  repository's tree" (§8.3) a property of every open instead of a review
  note.  The repository is decided *before* the temp-root allowance below,
  so the artifact denial holds even in a checkout that itself lives under
  the temp root (a clean extraction, a CI checkout in TMPDIR).  The names
  mirror the ``.gitignore`` block and are checked against it by a test.
- **the state directory** (``TWILL_STATE_DIR`` or ``~/.local/state/twill``,
  the same resolution as the engine's ``--state-dir`` default).
- **``artifacts_root``**, read from the operator config when one is loadable.
  Distilled artifacts belong there (§7.2); a suite that cannot write a
  fixture digest through it would be narrower than the gate §10.2 describes.
- **the temporary-directory root** (``tempfile.gettempdir()``).  This is the
  one deliberate broadening, and it is scaffolding-shaped rather than
  engine-shaped: 78 sites in this suite build their fixtures and state
  directories under a ``TemporaryDirectory``, and pytest builds its own tmp
  roots there too.  The invariants protect operator context, other
  repositories and the public tree -- none of which live in the temp root --
  so a write there cannot breach them.  Tracking every ``TemporaryDirectory``
  root instead would make child processes untraceable (the registrations do
  not cross a process boundary), which is a worse hole than a tree nothing
  on this host depends on.
- **``os.devnull``**, which ``subprocess`` opens read-write for
  ``stdin=DEVNULL``; it addresses no file.

Reads are unrestricted except under ``~/agent-transcript-archive``: TWILL
reads real transcripts elsewhere by design, and the archive is the one tree
whose very index it must not lean on (§2, §5 Scenario 2).

Paths are resolved with :meth:`pathlib.Path.resolve` before any comparison,
so a symlink planted in a fixture cannot reach a denied tree through its
target.  An open anchored to a file descriptor (``open(3, "w")``) addresses
no path and is not policed.  Violation messages carry the path and the
intent, never file contents -- the corpus is the thing being kept out of
here.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

#: The repository under audit; every module here hangs off ``tests/``.
REPO_TREE = Path(__file__).resolve().parents[1]

#: The separate archive pipeline's tree (§5 Scenario 2). Nothing under it is
#: ever opened -- not for reads, which would couple TWILL to the archive's
#: derived index (§2), and not for writes, which the write policy refuses
#: anyway because the archive is not an allowed write tree.
ARCHIVE_DIRNAME = "agent-transcript-archive"

#: The in-repo artifact directories denied outright (§7.2, §10.2 guard two).
#: Top-level names only, mirroring the ``.gitignore`` block: an artifact
#: writer names ``artifacts_root``, so a nested directory of the same name
#: elsewhere in the tree is ordinary content, not a leak.
ARTIFACT_DIRS = ("lessons", "digests", "measurements", "guards")

#: Mode characters that intend mutation (§ of the ``open`` docs: any of
#: w/a/x/+ writes or positions for writing; b/t/U are encoding flags).
_WRITE_MODE_CHARS = frozenset("wax+")

#: ``os.open`` flags that can create or mutate a file.  ``O_CREAT`` counts
#: even paired with ``O_RDONLY`` -- on Linux that combination creates the
#: file when missing, so it is write intent, not a read.
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

_installed = False
#: Snapshot of the temp root (and, below, the artifacts root) taken in
#: :func:`install` before the hook goes live.  Both must never be computed
#: from inside the hook: ``tempfile.gettempdir()`` probes its candidate
#: directories with real ``os.open`` calls, and :func:`load_config` opens
#: the config file -- either re-enters the hook mid-open and recurses
#: forever (found the hard way: the first gated run hung in
#: ``_get_default_tempdir``).  Env-derived roots are safe to resolve per
#: check -- they do syscalls the hook ignores (stat/readlink), never
#: ``open`` -- so they stay live and honest.
_temp_root: Path | None = None
_artifacts_root_cache: tuple[Path, ...] | None = None


class OpenPathViolation(AssertionError):
    """An ``open``/``os.open`` broke a §8.3 invariant.

    An ``AssertionError`` subclass so an unchecked violation lands as a test
    failure with the path in the message, wherever in the suite it happened.
    """


def installed() -> bool:
    """Whether the audit hook is live in this interpreter."""

    return _installed


def install() -> None:
    """Install the hook once, and arrange for children to do the same.

    Idempotent: the hook cannot be removed once added, so a second call is a
    no-op rather than a second identical hook on every open.  Everything the
    policy would otherwise compute by opening a file -- the temp root, the
    operator config -- is snapshotted before the hook exists (see the
    comment at :data:`_temp_root`).
    """

    global _installed, _temp_root, _artifacts_root_cache
    if _installed:
        return
    _temp_root = Path(tempfile.gettempdir()).resolve()
    _prepare_child_interpreters()
    if _artifacts_root_cache is None:
        _artifacts_root_cache = _load_artifacts_root()
    sys.addaudithook(audit_hook)
    _installed = True


def audit_hook(event: str, args: tuple) -> None:
    """The audit hook itself; public so the policy is testable directly."""

    if event != "open":
        return
    path, mode, flags = args
    check_open(path, mode, flags)


def check_open(path: object, mode: str | None, flags: int) -> None:
    """The §8.3 policy for one open; raise :class:`OpenPathViolation` or pass."""

    if isinstance(path, int):
        # An open anchored to a file descriptor addresses no path on disk.
        return
    if isinstance(path, bytes):
        text = os.fsdecode(path)
    elif isinstance(path, str):
        text = path
    else:
        text = os.fsdecode(os.fspath(path))
    # Resolve through symlinks and ``..`` before any comparison; resolve()
    # is non-strict, so a not-yet-created file resolves against its existing
    # prefix, which is exactly the O_CREAT case.
    resolved = Path(text).expanduser().resolve()
    if is_write(mode, flags):
        if not is_allowed_write(resolved):
            raise OpenPathViolation(
                f"open for writing outside every allowed tree: {resolved} "
                f"(mode={mode!r}, flags={flags}); plan §8.3 allows writes only "
                "inside this repository (never its artifact directories), the "
                "state directory, artifacts_root, and test scratch space"
            )
        return
    if is_forbidden_read(resolved):
        raise OpenPathViolation(
            f"open under the transcript archive: {resolved} (mode={mode!r}); "
            "plan §8.3: no path under ~/agent-transcript-archive is ever "
            "opened at all (§5 Scenario 2, §10.2)"
        )


def is_write(mode: str | None, flags: int) -> bool:
    """Whether one ``open``-event (mode, flags) pair intends mutation."""

    if mode and not _WRITE_MODE_CHARS.isdisjoint(mode):
        return True
    return bool(flags & _WRITE_FLAGS)


def is_allowed_write(resolved: Path) -> bool:
    """Whether a resolved absolute path may be opened for writing.

    The repository is decided first and definitively, before the temp-root
    allowance: an artifact name inside the tree is denied even in a
    checkout that itself lives under the temp root (a clean extraction or
    a CI checkout in TMPDIR -- the extraction run caught the first version
    of this function letting those through).
    """

    if resolved == Path(os.devnull):
        return True
    if _under(resolved, REPO_TREE):
        # The repository is writable except for the artifact names inside
        # it: guard two of the artifact-containment guards (§10.2).
        for name in ARTIFACT_DIRS:
            if _under(resolved, REPO_TREE / name):
                return False
        return True
    if _under(resolved, state_dir()):
        return True
    for root in artifacts_root():
        if _under(resolved, root):
            return True
    if _under(resolved, temp_root()):
        return True
    return False


def is_forbidden_read(resolved: Path) -> bool:
    """Whether a resolved absolute path may not be opened for reading."""

    return _under(resolved, archive_root())


def repo_tree() -> Path:
    """The repository under audit (constant for this checkout)."""

    return REPO_TREE


def archive_root() -> Path:
    """The archive tree no open may touch, under the effective ``HOME``.

    Derived per process from ``HOME`` rather than hardcoded, so a child
    running with a redirected home polices its own layout the same way the
    parent polices the real one.
    """

    return (Path.home() / ARCHIVE_DIRNAME).resolve()


def state_dir() -> Path:
    """The state directory, resolved as the engine resolves it.

    ``TWILL_STATE_DIR`` then ``~/.local/state/twill`` -- the same two steps
    as the CLI's ``--state-dir`` default (``twill_app._state_dir``), so the
    gate and the engine cannot disagree about where the state tree is.
    """

    configured = os.environ.get("TWILL_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".local" / "state" / "twill").resolve()


def artifacts_root() -> tuple[Path, ...]:
    """``artifacts_root`` from the operator config, or nothing.

    Best effort by design: the config has no default (§3) and may be absent
    or refuse to load in a test's redirected home.  A missing value only
    narrows the allowance -- the writes the suite actually makes land under
    the temp root -- it never widens one.  Snapshotted in :func:`install`
    before the hook exists, because :func:`load_config` opens the config
    file; without an install it is loaded on first use.
    """

    global _artifacts_root_cache
    if _artifacts_root_cache is None:
        _artifacts_root_cache = _load_artifacts_root()
    return _artifacts_root_cache


def temp_root() -> Path:
    """The scratch root writes are allowed under (see the module docstring).

    Snapshotted in :func:`install` -- ``tempfile.gettempdir()`` probes its
    candidate directories with real opens, which would re-enter the hook if
    first called from inside it.
    """

    if _temp_root is None:
        return Path(tempfile.gettempdir()).resolve()
    return _temp_root


def _load_artifacts_root() -> tuple[Path, ...]:
    try:
        from twill_config import load_config

        return (load_config().artifacts_root,)
    except Exception:
        # ImportError on a path without the engine, ConfigError for absent
        # or invalid operator config: no allowance either way.
        return ()


def _under(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or somewhere inside it."""

    return path == root or root in path.parents


def _prepare_child_interpreters() -> None:
    """Put this directory on ``PYTHONPATH`` for spawned interpreters.

    Tests spawn CLI verbs with ``env={**os.environ, ...}``, so an entry made
    here reaches every child, where ``tests/sitecustomize.py`` imports this
    module and calls :func:`install` before the verb's first open.
    """

    here = str(Path(__file__).resolve().parent)
    parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if here not in parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([here, *parts])
