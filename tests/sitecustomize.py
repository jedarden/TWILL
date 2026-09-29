"""Interpreter-startup hook for the open-path audit gate (plan §10.2).

``site`` imports this module in any interpreter that has this directory on
its path: the suite's own process under ``make test`` (which prepends the
directory to ``PYTHONPATH``), and every Python child the suite spawns --
``tests/openpath.install`` adds the directory to the inherited
``PYTHONPATH``.  The hook is therefore live before unittest or pytest loads
a module and before any CLI verb opens a file.

An interpreter that reaches here without the gate installing cleanly is not
silently ungated: ``tests/test_openpath.py`` asserts the hook is installed
in this process and in a spawned child, so a broken startup fails the run.
"""

import os
from pathlib import Path


def _canonicalize_temp_environment():
    """Keep tempfile paths stable when the configured temp root is a symlink."""

    for name in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            os.environ[name] = str(Path(value).expanduser().resolve())


_canonicalize_temp_environment()

import openpath  # noqa: E402

openpath.install()
