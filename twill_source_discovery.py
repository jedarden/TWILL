"""Private, resumable inventory for explicitly enrolled native transcript globs.

The controller's StateLock serializes writers. SQLite holds locators, fingerprints,
parser cursors and counters, never source text; it is a rebuildable local index,
not an alternative source of human authority.
"""
from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

DEFAULT_LIMITS = {"max_files_per_cycle": 128, "scan_seconds": 30, "inventory_per_glob": 512}
_BOUNDS = {"max_files_per_cycle": (8, 1024), "scan_seconds": (1, 120),
           "inventory_per_glob": (8, 8192)}


def validate_limits(policy: dict) -> dict:
    limits = policy.get("discovery_limits", {})
    if not isinstance(limits, dict) or set(limits) - set(DEFAULT_LIMITS):
        raise ValueError("discovery_limits accepts max_files_per_cycle, scan_seconds and inventory_per_glob")
    result = dict(DEFAULT_LIMITS, **limits)
    for key, value in result.items():
        low, high = _BOUNDS[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"discovery_limits {key} must be an integer in {low}..{high}")
    return result


def _provider(pattern: str) -> str:
    parts = Path(pattern).parts
    if any(part in {".codex", "codex"} for part in parts):
        return "codex"
    if any(part in {".claude", "claude", "claude-code"} for part in parts):
        return "claude"
    return "unknown"


def _db_path(root: Path) -> Path:
    directory = Path(root) / "learning"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / ".source-discovery.sqlite3"
    if directory.absolute() != directory.resolve() or path.is_symlink():
        raise ValueError("source discovery database requires a canonical private path")
    if path.exists() and not path.is_file():
        raise ValueError("source discovery database must be regular")
    return path


def _safe_cursor(cursor: dict) -> dict:
    # The workflow state machine needs reference identities, not captured prose.
    value = {key: item for key, item in cursor.items() if key in {
        "fingerprint", "offset", "line", "episode", "done", "tools", "pending", "question_pending"}}
    value = json.loads(json.dumps(value))
    episode = value.get("episode", {})
    if episode:
        value["episode"] = {"stage": episode["stage"], "refs": [
            {key: item for key, item in ref.items() if key in {"source_path", "source_line", "sha256", "role"}}
            for ref in episode["refs"]]}
    return value


class DiscoveryQueue:
    """One independent consumer of a shared private, durable source inventory."""

    def __init__(self, root: Path, consumer: str, policy: dict, max_source_bytes: int):
        self.started = time.monotonic()
        self.limits = validate_limits(policy)
        self.consumer = consumer
        self.policy = policy
        self.max_source_bytes = max_source_bytes
        path = _db_path(root)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS consumer (
                name TEXT PRIMARY KEY, imported INTEGER NOT NULL DEFAULT 0,
                next_glob INTEGER NOT NULL DEFAULT 0, counter INTEGER NOT NULL DEFAULT 0,
                cycle TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS enrollment (
                consumer TEXT NOT NULL, pattern TEXT NOT NULL, provider TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, sweep INTEGER NOT NULL DEFAULT 0,
                seen INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (consumer,pattern));
            CREATE TABLE IF NOT EXISTS frontier (
                id INTEGER PRIMARY KEY, consumer TEXT NOT NULL, pattern TEXT NOT NULL,
                directory TEXT NOT NULL, remainder TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
                finished INTEGER NOT NULL DEFAULT 0,
                UNIQUE (consumer,pattern,directory,remainder));
            CREATE INDEX IF NOT EXISTS frontier_pending ON frontier(consumer,pattern,finished,offset,id);
            CREATE TABLE IF NOT EXISTS source (
                consumer TEXT NOT NULL, path TEXT NOT NULL, fingerprint TEXT NOT NULL,
                mtime REAL NOT NULL, cursor TEXT NOT NULL DEFAULT '{}', done INTEGER NOT NULL DEFAULT 0,
                error INTEGER NOT NULL DEFAULT 0, attempted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (consumer,path));
            CREATE INDEX IF NOT EXISTS source_pending ON source(consumer,done,attempted,mtime DESC,path);
            CREATE TABLE IF NOT EXISTS membership (
                consumer TEXT NOT NULL, pattern TEXT NOT NULL, path TEXT NOT NULL,
                PRIMARY KEY (consumer,pattern,path));
            CREATE TABLE IF NOT EXISTS output (
                id INTEGER PRIMARY KEY, consumer TEXT NOT NULL, name TEXT NOT NULL,
                UNIQUE (consumer,name));
        """)
        self.db.execute("INSERT OR IGNORE INTO consumer(name) VALUES (?)", (consumer,))
        self.patterns = list(dict.fromkeys(str(Path(p).expanduser()) for p in policy["intervention_sources"]))
        self.db.execute("UPDATE enrollment SET active=0 WHERE consumer=?", (consumer,))
        for pattern in self.patterns:
            exists = self.db.execute("SELECT 1 FROM enrollment WHERE consumer=? AND pattern=?",
                                     (consumer, pattern)).fetchone()
            self.db.execute("""INSERT INTO enrollment(consumer,pattern,provider) VALUES (?,?,?)
                ON CONFLICT(consumer,pattern) DO UPDATE SET active=1,provider=excluded.provider""", (consumer, pattern, _provider(pattern)))
            if not exists:
                self._restart(pattern)
        last_cycle = json.loads(self.db.execute("SELECT cycle FROM consumer WHERE name=?", (consumer,)).fetchone()[0])
        self.cycle = {"inventoried": 0, "visited": 0, "errors": 0, "budget_exhausted": False,
                      "budget_reasons": [], "limits": self.limits,
                      "next_inventory_glob": last_cycle.get("next_inventory_glob", 0)}
        self.selected = set()
        self.mirror = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.db.close()

    def _task(self, pattern: str, directory: str, parts: list[str]) -> None:
        self.db.execute("INSERT OR IGNORE INTO frontier(consumer,pattern,directory,remainder) VALUES (?,?,?,?)",
                        (self.consumer, pattern, directory, json.dumps(parts)))
        if parts and parts[0] == "**":
            self._task(pattern, directory, parts[1:])

    def _restart(self, pattern: str) -> None:
        self.db.execute("DELETE FROM frontier WHERE consumer=? AND pattern=?", (self.consumer, pattern))
        path = Path(pattern)
        parts = list(path.parts)
        directory = path.anchor or "."
        parts = parts[1:] if path.anchor else parts
        # Literal prefixes cost no directory enumeration. A final literal is
        # checked as a file, so absent optional sources remain harmless.
        while len(parts) > 1 and not any(c in parts[0] for c in "*?["):
            directory = str(Path(directory) / parts.pop(0))
        self._task(pattern, directory, parts)
        self.db.execute("UPDATE enrollment SET sweep=sweep+1 WHERE consumer=? AND pattern=?",
                        (self.consumer, pattern))

    def import_legacy(self, state: dict) -> None:
        if self.db.execute("SELECT imported FROM consumer WHERE name=?", (self.consumer,)).fetchone()[0]:
            return
        matches = {pattern: re.compile(glob.translate(pattern, recursive=True)) for pattern in self.patterns}
        for path, cursor in state["files"].items():
            fingerprint = cursor.get("fingerprint")
            if not isinstance(fingerprint, list) or len(fingerprint) != 3:
                raise ValueError("invalid legacy discovery fingerprint")
            self.db.execute("""INSERT OR IGNORE INTO source(consumer,path,fingerprint,mtime,cursor,done)
                VALUES (?,?,?,?,?,?)""", (self.consumer, path, json.dumps(fingerprint), fingerprint[1] / 1e9,
                                          json.dumps(_safe_cursor(cursor)), bool(cursor.get("done"))))
            for pattern, matcher in matches.items():
                if matcher.fullmatch(path):
                    self.db.execute("INSERT OR IGNORE INTO membership(consumer,pattern,path) VALUES (?,?,?)",
                                    (self.consumer, pattern, path))
        for name in state["outputs"]:
            self.add_output(name)
        self.db.execute("UPDATE consumer SET imported=1 WHERE name=?", (self.consumer,))

    def _observe(self, pattern: str, path: Path) -> None:
        try:
            info = path.stat(follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or not path.is_absolute() or
                    path.absolute() != path.resolve() or info.st_size > self.max_source_bytes):
                return
            fp = json.dumps([info.st_size, info.st_mtime_ns, info.st_ino])
            self.db.execute("""INSERT INTO source(consumer,path,fingerprint,mtime) VALUES (?,?,?,?)
                ON CONFLICT(consumer,path) DO UPDATE SET
                cursor=CASE WHEN fingerprint!=excluded.fingerprint THEN '{}' ELSE cursor END,
                done=CASE WHEN fingerprint!=excluded.fingerprint THEN 0 ELSE done END,
                error=CASE WHEN fingerprint!=excluded.fingerprint THEN 0 ELSE error END,
                attempted=CASE WHEN fingerprint!=excluded.fingerprint THEN 0 ELSE attempted END,
                fingerprint=excluded.fingerprint,mtime=excluded.mtime""",
                (self.consumer, str(path), fp, info.st_mtime))
            self.db.execute("INSERT OR IGNORE INTO membership(consumer,pattern,path) VALUES (?,?,?)",
                            (self.consumer, pattern, str(path)))
        except OSError:
            self._error(pattern)

    def _error(self, pattern: str) -> None:
        self.cycle["errors"] += 1
        self.db.execute("UPDATE enrollment SET errors=errors+1 WHERE consumer=? AND pattern=?",
                        (self.consumer, pattern))

    def _inventory_chunk(self, pattern: str, limit: int, deadline: float) -> tuple[int, bool]:
        task = self.db.execute("""SELECT * FROM frontier WHERE consumer=? AND pattern=? AND finished=0
            ORDER BY offset,id LIMIT 1""", (self.consumer, pattern)).fetchone()
        if task is None:
            return 0, True
        parts = json.loads(task["remainder"])
        used, offset, finished = 0, task["offset"], False
        try:
            directory = Path(task["directory"])
            if directory.is_symlink() or directory.absolute() != directory.resolve():
                finished = True
            elif not parts:
                self._observe(pattern, directory)
                used, finished = 1, True
            else:
                with os.scandir(directory) as entries:
                    # scandir offsets are deliberately replayed without stat.
                    # Directory tasks bound replay to one directory rather than
                    # repeatedly walking the prefix of the entire corpus.
                    for _ in range(offset):
                        if time.monotonic() >= deadline:
                            return 0, False
                        if next(entries, None) is None:
                            finished = True
                            break
                    while not finished and used < limit and time.monotonic() < deadline:
                        entry = next(entries, None)
                        if entry is None:
                            finished = True
                            break
                        offset += 1
                        used += 1
                        name = entry.name
                        token = parts[0]
                        if name.startswith(".") and not token.startswith("."):
                            continue
                        if entry.is_symlink():
                            continue
                        if token == "**":
                            if len(parts) == 1:
                                self._observe(pattern, Path(entry.path))
                            if entry.is_dir(follow_symlinks=False):
                                self._task(pattern, entry.path, parts)
                        elif fnmatch.fnmatchcase(name, token):
                            if len(parts) == 1:
                                self._observe(pattern, Path(entry.path))
                            elif entry.is_dir(follow_symlinks=False):
                                self._task(pattern, entry.path, parts[1:])
        except OSError:
            self._error(pattern)
            finished = True
        self.db.execute("UPDATE frontier SET offset=?,finished=? WHERE id=?", (offset, finished, task["id"]))
        self.db.execute("UPDATE enrollment SET seen=seen+? WHERE consumer=? AND pattern=?",
                        (used, self.consumer, pattern))
        self.cycle["inventoried"] += used
        return used, False

    def budget(self, reason: str) -> None:
        self.cycle["budget_exhausted"] = True
        if reason not in self.cycle["budget_reasons"]:
            self.cycle["budget_reasons"].append(reason)

    def inventory(self, deadline: float) -> None:
        # Reserve most of the cooperative budget for actual transcript reads.
        scan_end = min(deadline, time.monotonic() + self.limits["scan_seconds"] / 3)
        remaining = dict.fromkeys(self.patterns, self.limits["inventory_per_glob"])
        done = set()
        start = self.cycle["next_inventory_glob"] % max(1, len(self.patterns))
        order = self.patterns[start:] + self.patterns[:start]
        while len(done) < len(self.patterns) and time.monotonic() < scan_end:
            for pattern in order:
                if pattern in done:
                    continue
                # No single provider can spend the inventory budget first.
                quantum_end = min(scan_end, time.monotonic() + max(.005,
                                  self.limits["scan_seconds"] / (6 * max(1, len(self.patterns)))))
                used, exhausted = self._inventory_chunk(pattern, min(32, remaining[pattern]), quantum_end)
                self.cycle["next_inventory_glob"] = (self.patterns.index(pattern) + 1) % len(self.patterns)
                remaining[pattern] -= used
                if exhausted:
                    self._restart(pattern)
                if remaining[pattern] <= 0:
                    self.budget("inventory_limit")
                if exhausted or remaining[pattern] <= 0:
                    done.add(pattern)
                if time.monotonic() >= scan_end:
                    break
        if len(done) < len(self.patterns):
            self.budget("inventory_time")

    def pending(self, deadline: float):
        count = len(self.patterns)
        start = self.db.execute("SELECT next_glob FROM consumer WHERE name=?", (self.consumer,)).fetchone()[0]
        index, misses = start % max(1, count), 0
        while count and len(self.selected) < self.limits["max_files_per_cycle"] and misses < count:
            if time.monotonic() >= deadline:
                self.budget("scan_time")
                break
            pattern = self.patterns[index]
            index = (index + 1) % count
            self.db.execute("UPDATE consumer SET next_glob=? WHERE name=?", (index, self.consumer))
            rows = self.db.execute("""SELECT s.* FROM source s JOIN membership m
                ON s.consumer=m.consumer AND s.path=m.path
                WHERE s.consumer=? AND m.pattern=? AND s.done=0 AND s.mtime<=?
                ORDER BY s.attempted,s.mtime DESC,s.path LIMIT ?""",
                (self.consumer, pattern, time.time() - self.policy.get("intervention_settle_seconds", 7200),
                 self.limits["max_files_per_cycle"] + 1)).fetchall()
            row = next((r for r in rows if r["path"] not in self.selected), None)
            if row is None:
                misses += 1
                continue
            misses = 0
            value = row["path"]
            self.selected.add(value)
            try:
                path = Path(value)
                info = path.stat(follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or path.absolute() != path.resolve() or
                        info.st_size > self.max_source_bytes):
                    raise ValueError("source is no longer an enrolled regular file")
                current_fp = [info.st_size, info.st_mtime_ns, info.st_ino]
                if current_fp != json.loads(row["fingerprint"]):
                    self._observe(pattern, path)
                    continue
                if time.time() - info.st_mtime < self.policy.get("intervention_settle_seconds", 7200):
                    continue
            except (OSError, ValueError):
                self.db.execute("UPDATE source SET done=1,error=1 WHERE consumer=? AND path=?",
                                (self.consumer, value))
                self._error(pattern)
                continue
            self.db.execute("UPDATE consumer SET counter=counter+1 WHERE name=?", (self.consumer,))
            self.db.execute("""UPDATE source SET attempted=(SELECT counter FROM consumer WHERE name=?)
                WHERE consumer=? AND path=?""", (self.consumer, self.consumer, value))
            self.cycle["visited"] += 1
            yield value, json.loads(row["fingerprint"]), json.loads(row["cursor"])
        if len(self.selected) >= self.limits["max_files_per_cycle"]:
            self.budget("file_limit")

    def save(self, path: str, cursor: dict, error: bool = False) -> None:
        cursor = _safe_cursor(cursor)
        self.db.execute("UPDATE source SET cursor=?,done=?,error=? WHERE consumer=? AND path=?",
                        (json.dumps(cursor), bool(cursor.get("done")), error, self.consumer, path))
        self.mirror[path] = cursor
        if error:
            self.cycle["errors"] += 1

    def add_output(self, name: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO output(consumer,name) VALUES (?,?)", (self.consumer, name))

    def outputs(self) -> list[str]:
        return [row[0] for row in reversed(self.db.execute(
            "SELECT name FROM output WHERE consumer=? ORDER BY id DESC LIMIT 256", (self.consumer,)).fetchall())]

    def finish(self, state: dict) -> dict:
        # JSON is a bounded compatibility/diagnostic mirror. SQLite remains
        # authoritative for every completed and partial cursor, including those
        # too large to include in this mirror.
        state["files"] = {}
        used = 0
        for path, cursor in self.mirror.items():
            size = len(json.dumps({path: cursor}).encode())
            if used + size > 1024 * 1024:
                continue
            state["files"][path] = cursor
            used += size
        state["outputs"] = self.outputs()
        self.cycle["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.db.execute("UPDATE consumer SET cycle=? WHERE name=?", (json.dumps(self.cycle), self.consumer))
        self.db.commit()
        state["coverage"] = _coverage(self.db, self.consumer)
        return state

    def close(self) -> None:
        self.db.close()


def _coverage(db: sqlite3.Connection, consumer: str) -> dict:
    globs = []
    for row in db.execute("SELECT pattern,provider,sweep,seen,errors FROM enrollment WHERE consumer=? AND active=1",
                          (consumer,)):
        counts = db.execute("""SELECT COUNT(*),COALESCE(SUM(s.done),0),COALESCE(SUM(s.error),0),
            COALESCE(SUM(s.attempted>0 OR s.done),0) FROM source s JOIN membership m
            ON s.consumer=m.consumer AND s.path=m.path WHERE s.consumer=? AND m.pattern=?""",
            (consumer, row[0])).fetchone()
        globs.append({"glob": row[0], "provider": row[1], "sweeps": row[2], "inventory_entries": row[3],
                      "inventory_errors": row[4], "known": counts[0], "completed": counts[1],
                      "pending": counts[0] - counts[1], "errors": counts[2], "visited": counts[3]})
    cycle = db.execute("SELECT cycle FROM consumer WHERE name=?", (consumer,)).fetchone()
    providers = {}
    for provider in dict.fromkeys(item["provider"] for item in globs):
        counts = db.execute("""SELECT COUNT(*),COALESCE(SUM(done),0),COALESCE(SUM(error),0),
            COALESCE(SUM(attempted>0 OR done),0) FROM (SELECT DISTINCT s.path,s.done,s.error,s.attempted
            FROM source s JOIN membership m ON s.consumer=m.consumer AND s.path=m.path
            JOIN enrollment e ON e.consumer=m.consumer AND e.pattern=m.pattern
            WHERE s.consumer=? AND e.active=1 AND e.provider=?)""", (consumer, provider)).fetchone()
        providers[provider] = {"known": counts[0], "completed": counts[1], "pending": counts[0] - counts[1],
                               "errors": counts[2], "visited": counts[3]}
    return {"schema": "twill-source-discovery-coverage/v1", "globs": globs, "providers": providers,
            "last_cycle": json.loads(cycle[0]) if cycle else {}}


def discovery_coverage(artifacts_root: Path) -> dict:
    """Read private locator-only coverage, without requiring any archive feed."""
    path = Path(artifacts_root) / "learning" / ".source-discovery.sqlite3"
    if not path.exists():
        return {}
    if path.is_symlink() or path.absolute() != path.resolve() or not path.is_file():
        raise ValueError("source discovery coverage requires a canonical database")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return {row[0]: _coverage(db, row[0]) for row in db.execute("SELECT name FROM consumer")}
