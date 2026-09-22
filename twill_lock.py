"""Exclusive state-directory locking for mutating TWILL verbs."""

from __future__ import annotations

import errno
import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
from time import sleep

import twill_schema
from twill_contract import LockHeldError, generated_at


LOCK_FILENAME = "lock"
LOCK_MODE = 0o600
_OWNER_READ_ATTEMPTS = 5
_OWNER_READ_DELAY_SECONDS = 0.01


@dataclass(frozen=True)
class LockOwner:
    """The operator-facing identity recorded while a state lock is held."""

    pid: int
    since: str


def _owner_from_metadata(raw: bytes) -> LockOwner | None:
    try:
        metadata = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    pid = metadata.get("pid")
    since = metadata.get("since")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(since, str) or not since:
        return None
    return LockOwner(pid, since)


def _read_owner(lock_fd: int) -> LockOwner | None:
    for attempt in range(_OWNER_READ_ATTEMPTS):
        os.lseek(lock_fd, 0, os.SEEK_SET)
        owner = _owner_from_metadata(os.read(lock_fd, 4096))
        if owner is not None:
            return owner
        if attempt + 1 < _OWNER_READ_ATTEMPTS:
            sleep(_OWNER_READ_DELAY_SECONDS)
    return None


class StateLock:
    """Hold an advisory, process-exclusive lock for one state directory.

    The kernel flock is authoritative.  The small JSON payload is only
    operator metadata for the EC-10 diagnostic and is rewritten whenever a
    new process acquires the lock, so stale contents after a crash are safe.
    """

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.lock_path = state_dir / LOCK_FILENAME
        self._fd: int | None = None
        self.owner: LockOwner | None = None

    def __enter__(self) -> "StateLock":
        twill_schema.prepare_state_dir(self.state_dir)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, LOCK_MODE)
        self._fd = fd
        try:
            os.chmod(self.lock_path, LOCK_MODE)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                owner = _read_owner(fd)
                if owner is None:
                    owner = _proc_lock_owner(self.lock_path)
                if owner is None:
                    raise RuntimeError(
                        f"state lock {self.lock_path} is held but its owner metadata is unavailable"
                    ) from exc
                raise LockHeldError(owner.pid, owner.since) from exc

            owner = LockOwner(os.getpid(), generated_at())
            payload = json.dumps(
                {"pid": owner.pid, "since": owner.since},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.fsync(fd)
            self.owner = owner
            return self
        except BaseException:
            self._close_fd()
            raise

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)
            os.fsync(self._fd)
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            self._close_fd()

    def _close_fd(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.owner = None


def _proc_device_string(device_stat: os.stat_result) -> str:
    """Render a device the way ``/proc/locks`` prints it: ``%02x:%02x``.

    The kernel formats both numbers as lowercase hex padded to at least two
    digits (fs/locks.c), so a stat device of 259:4 must be matched as
    ``103:04`` — a decimal rendering matches nothing on major > 9.
    """

    return f"{os.major(device_stat.st_dev):02x}:{os.minor(device_stat.st_dev):02x}"


def _proc_lock_owner(lock_path: Path) -> LockOwner | None:
    """Recover the pid during the tiny metadata-write race after flock.

    Linux exposes the owner of an advisory flock in ``/proc/locks``.  The
    metadata remains the normal path because it also carries the start time;
    this fallback only supplies a pid and uses the current instant as the
    best available lower-bound timestamp.
    """

    try:
        stat = lock_path.stat()
        device = _proc_device_string(stat)
        inode = str(stat.st_ino)
        lines = Path("/proc/locks").read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return None
    for line in lines:
        fields = line.split()
        if len(fields) >= 6 and fields[1] == "FLOCK" and fields[4].isdigit():
            if fields[5] == f"{device}:{inode}":
                return LockOwner(int(fields[4]), generated_at())
    return None
