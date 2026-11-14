"""Read-only private evidence ownership and exact completed-scan receipts.

This adapter grants no deletion authority. The producer alone constructs owned
cache paths; unknown or unreadable ownership state makes retention fail closed.
"""
from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
import re

MAX_METADATA = 64 * 1024 * 1024
TERMINAL = {"archived", "rejected", "superseded", "invalidated", "revoked"}


class RetentionUnresolved(ValueError):
    pass


def _regular(path: Path) -> None:
    if path != path.resolve():
        raise RetentionUnresolved("noncanonical metadata")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise RetentionUnresolved("unsafe metadata")


def _read(path: Path) -> dict:
    _regular(path)
    if path.stat().st_size > MAX_METADATA:
        raise RetentionUnresolved("metadata bound")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise RetentionUnresolved("invalid metadata")
    return value


def _paths(value, result: set[str]) -> None:
    if isinstance(value, str) and value.startswith("/"):
        path = Path(value)
        # Missing source files still own their locator; symlink resolution must
        # never silently change the identity of a pin.
        if str(path) != value or path != path.resolve():
            raise RetentionUnresolved("noncanonical evidence reference")
        result.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            _paths(item, result)
    elif isinstance(value, list):
        for item in value:
            _paths(item, result)


def pinned_sources(root: Path) -> set[str]:
    """Conservatively retain source references of every live learning owner."""
    root = Path(root)
    if not root.is_absolute() or root != root.resolve():
        raise RetentionUnresolved("noncanonical learning root")
    result: set[str] = set()
    for name in ("records", "sources"):
        directory = root / "learning" / name
        if not directory.exists():
            continue
        if directory != directory.resolve() or not directory.is_dir():
            raise RetentionUnresolved("unsafe evidence directory")
        for path in directory.glob("*.json"):
            if path.name.startswith("."):
                continue
            value = _read(path)
            if value.get("state") not in TERMINAL:
                _paths(value, result)
    from twill_intervention_mining import pinned_sources as episode_pins
    result.update(episode_pins(root))
    return result


def consumed_sources(root: Path) -> dict:
    path = Path(root) / "learning" / ".intervention-consumption.json"
    if not path.exists():
        return {}
    value = _read(path)
    if value.get("schema") != "twill-intervention-consumption/v1" or not isinstance(value.get("sources"), dict):
        raise RetentionUnresolved("invalid consumption receipts")
    from twill_intervention_mining import SCANNER_VERSION
    for receipt in value["sources"].values():
        if not isinstance(receipt, dict):
            raise RetentionUnresolved("invalid source receipt")
        if receipt.get("scanner_version") != SCANNER_VERSION:
            receipt.update(complete=False, compact_durable=False)
    return value["sources"]


def verified_receipt(root: Path, path: Path, receipt: dict) -> bool:
    """Require a committed full parser cursor independently of its receipt."""
    from twill_intervention_mining import SCANNER_VERSION
    if (not isinstance(receipt, dict) or receipt.get("complete") is not True
            or receipt.get("compact_durable") is not True or receipt.get("error") is not None
            or receipt.get("scanner_version") != SCANNER_VERSION
            or not isinstance(receipt.get("episode_ids"), list)):
        return False
    fp = receipt.get("fingerprint")
    if (not isinstance(fp, list) or len(fp) != 3
            or any(type(v) is not int or v < 0 for v in fp)
            or receipt.get("offset") != fp[0]):
        return False
    # A boolean receipt cannot stand in for the compact evidence it names.
    for ident in receipt["episode_ids"]:
        if not isinstance(ident, str) or not re.fullmatch(r"IE-[0-9a-f]{24}", ident):
            return False
        episode = _read(Path(root) / "learning" / "interventions" / (ident + ".json"))
        if episode.get("schema") != "twill-intervention-episode/v1" or episode.get("id") != ident:
            return False
    database = Path(root) / "learning" / ".source-discovery.sqlite3"
    if not database.exists():
        return False
    _regular(database)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT fingerprint,cursor,done,error FROM source WHERE consumer=? AND path=?",
                         ("intervention-episodes", str(path))).fetchone()
    if not row or row[2] != 1 or row[3] != 0:
        return False
    cursor = json.loads(row[1])
    return (json.loads(row[0]) == fp and cursor.get("fingerprint") == fp
            and cursor.get("offset") == fp[0] and cursor.get("done") is True)
