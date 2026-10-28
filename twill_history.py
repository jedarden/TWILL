"""Strict read-only adapter for Codex's local thread-history SQLite shape.

The operator may point ``TWILL_HISTORY_DB`` at a Codex
``thread_history_1.sqlite`` file. Its ``thread_turns`` records name the final
agent item of a completed turn; ``thread_items`` verifies that item belongs
to the same thread and turn. An unrecognized schema is ignored, leaving JSONL
terminality unknown. No transcript content is read from this database.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

from twill_contract import EXIT_RUNTIME_ERROR, CliError

HISTORY_CONTRACT = "codex-thread-history/v1"
HISTORY_DB_ENV = "TWILL_HISTORY_DB"
TURN_COLUMNS = frozenset(
    {"thread_id", "turn_id", "status", "completed_at", "final_agent_item_id"}
)
ITEM_COLUMNS = frozenset({"thread_id", "turn_id", "item_id"})


class HistorySchemaError(CliError):
    """The configured file is not a readable supported Codex history source."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(
            EXIT_RUNTIME_ERROR,
            f"{path} is not a readable {HISTORY_CONTRACT} source: {detail}",
            "remove TWILL_HISTORY_DB or point it at a supported Codex history file",
        )


def configured_path(environ: dict[str, str] | None = None) -> Path | None:
    environment = environ if environ is not None else os.environ
    value = environment.get(HISTORY_DB_ENV, "").strip()
    return Path(value) if value else None


def _validate_schema(connection: sqlite3.Connection, path: Path) -> None:
    for table, required in (
        ("thread_turns", TURN_COLUMNS),
        ("thread_items", ITEM_COLUMNS),
    ):
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise HistorySchemaError(path, f"missing {table} table")
        present = {
            str(column[1])
            for column in connection.execute(f"PRAGMA table_info({table})")
        }
        missing = required - present
        if missing:
            raise HistorySchemaError(path, f"{table} missing {', '.join(sorted(missing))}")


class HistorySnapshot:
    """One consistent, read-only view of the source for an ingest run."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._connection = connection
        self.path = path
        self._completed: dict[str, frozenset[str]] = {}

    @classmethod
    def open(cls, path: Path) -> HistorySnapshot:
        if not path.is_file():
            raise HistorySchemaError(path, "file is absent")
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except (OSError, sqlite3.Error) as exc:
            raise HistorySchemaError(path, "cannot open read-only") from exc
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN DEFERRED")
            _validate_schema(connection, path)
        except (OSError, sqlite3.Error) as exc:
            connection.close()
            raise HistorySchemaError(path, "schema cannot be read") from exc
        except BaseException:
            connection.close()
            raise
        return cls(connection, path)

    def completed_message_ids(self, session_id: str) -> frozenset[str]:
        if session_id not in self._completed:
            try:
                rows = self._connection.execute(
                    "SELECT DISTINCT t.final_agent_item_id "
                    "FROM thread_turns AS t JOIN thread_items AS i "
                    "ON i.thread_id=t.thread_id AND i.turn_id=t.turn_id "
                    "AND i.item_id=t.final_agent_item_id "
                    "WHERE t.thread_id=? AND t.status='completed' "
                    "AND t.completed_at IS NOT NULL "
                    "AND t.final_agent_item_id IS NOT NULL",
                    (session_id,),
                ).fetchall()
            except sqlite3.Error as exc:
                raise HistorySchemaError(self.path, "turn lookup failed") from exc
            self._completed[session_id] = frozenset(
                row[0] for row in rows if isinstance(row[0], str) and row[0]
            )
        return self._completed[session_id]

    def close(self) -> None:
        try:
            if self._connection.in_transaction:
                self._connection.rollback()
        finally:
            self._connection.close()

    def __enter__(self) -> HistorySnapshot:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def completed_keys_for_session(
    snapshot: HistorySnapshot | None, session_id: str
) -> frozenset[tuple[str, str]]:
    if snapshot is None or not session_id:
        return frozenset()
    return frozenset(
        (session_id, item_id)
        for item_id in snapshot.completed_message_ids(session_id)
    )
