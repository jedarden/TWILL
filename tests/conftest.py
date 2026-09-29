"""pytest entry point for the open-path audit gate (plan §10.2).

``make test`` reaches the same hook through ``tests/sitecustomize.py``
before anything imports; pytest has no startup hook of its own, so the
conftest installs it here -- at collection time, before the first test
module runs.  :func:`openpath.install` is idempotent and also puts this
directory on ``PYTHONPATH`` so every spawned CLI verb self-installs.
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
