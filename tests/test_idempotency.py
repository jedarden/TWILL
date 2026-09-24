"""The idempotency property test for repeated ingest (plan §8.3, §10.1).

§8.3 states the invariant — re-running ingest over unchanged inputs produces
byte-identical derived rows — and §10.1 pairs it with its cursor half: a
cursor never moves backwards except on an identity change.  This module
drives both properties over the whole transcript fixture corpus (both
sources, all six scenarios each) through the real :class:`Store.ingest_path`
persistence boundary, from three angles:

1. **Double ingest.**  Ingest each case's final bytes twice into one state
   store.  The second pass is the idle-resume path; every derived row —
   including the rowids — must be byte-identical to the first pass, and the
   cursor must not move.
2. **Rebuild.**  Derive the same inputs into a second, independent state
   store.  Every row must match the first store's except the columns that
   record *when* and *where* the run happened (wall-clock stamps and the
   path-derived session key) — never in anything transcript-derived.
3. **Resume convergence.**  Drive each case through its scenario's real
   lifecycle (parse → idle → append or rewrite → idle) and require the
   result to converge on exactly one full parse of the same final bytes,
   with monotone cursor transitions recorded after every pass.  For codex
   this exercises the stateful prefix replay on the resumed span.

The comparison units are the full ``transcript_event`` and ``observation``
tables ordered by rowid, plus the ``session`` and ``cursor`` bookkeeping
rows — ``SELECT *``, so a schema column cannot silently escape the property.
"""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "transcripts"
sys.path.insert(0, str(ROOT))

import twill_cursor  # noqa: E402
from twill_app import Store  # noqa: E402
from twill_cursor import CursorRow  # noqa: E402

#: mtime_ns is cursor bookkeeping, never a decision input (twill_cursor's
#: module docstring).  Pinning it on every materialized transcript makes the
#: cross-store cursor comparison byte-exact instead of bookkeeping-blind.
PINNED_MTIME_NS = 1_700_000_000_000_000_000

#: Per table, the columns a cross-store comparison may drop: the session key
#: and source path are derived from *where* the checkout sits, the stamps
#: from *when* the run happened.  Nothing transcript-derived is droppable.
CROSS_STORE_DROPS = {
    "session": frozenset({"session_key", "source_path", "ingested_at"}),
    "transcript_event": frozenset({"session_key"}),
    "observation": frozenset(),
}

#: Cursor fields compared across stores; the omitted three are the path key
#: and the two wall-clock stamps.  ``mtime_ns`` stays because it is pinned.
CURSOR_COMPARED_FIELDS = tuple(
    field.name
    for field in fields(CursorRow)
    if field.name not in ("path", "first_seen", "last_indexed_at")
)


@dataclass(frozen=True)
class Case:
    """One manifest scenario for one source, with its fixture files."""

    source: str
    scenario: str
    operation: str
    files: tuple[Path, ...]

    @property
    def label(self) -> str:
        return f"{self.source}/{self.scenario}"

    def final_bytes(self) -> bytes:
        """The transcript bytes the scenario leaves on disk when it settles."""

        if self.operation == "append":
            return self.files[0].read_bytes() + self.files[1].read_bytes()
        if self.operation == "replace-at-same-path":
            return self.files[1].read_bytes()
        return self.files[0].read_bytes()

    def stages(self) -> tuple[tuple[bytes | None, str], ...]:
        """The lifecycle as ``(bytes_to_write_or_None, expected_action)``.

        ``None`` means idle: re-ingest over unchanged inputs, which must
        take the resume action and parse nothing.
        """

        if self.operation == "append":
            base = self.files[0].read_bytes()
            return (
                (base, twill_cursor.ACTION_PARSE),
                (None, twill_cursor.ACTION_RESUME),
                (base + self.files[1].read_bytes(), twill_cursor.ACTION_RESUME),
                (None, twill_cursor.ACTION_RESUME),
            )
        if self.operation == "replace-at-same-path":
            return (
                (self.files[0].read_bytes(), twill_cursor.ACTION_PARSE),
                (None, twill_cursor.ACTION_RESUME),
                (self.files[1].read_bytes(), twill_cursor.ACTION_REPARSE),
                (None, twill_cursor.ACTION_RESUME),
            )
        payload = self.files[0].read_bytes()
        return (
            (payload, twill_cursor.ACTION_PARSE),
            (None, twill_cursor.ACTION_RESUME),
        )


class IdempotencyPropertyTests(unittest.TestCase):
    """Plan §8.3 idempotency + §10.1 monotonicity over the whole corpus."""

    @classmethod
    def setUpClass(cls):
        manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text())
        cls.cases = tuple(
            Case(source, scenario, case["operation"], tuple(
                FIXTURE_ROOT / relative for relative in case["files"]
            ))
            for source, source_data in sorted(manifest["sources"].items())
            for scenario, case in sorted(source_data["cases"].items())
        )

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    # -- harness --------------------------------------------------------------

    def store(self, tag: str) -> Store:
        store = Store(self.root / tag / "state")
        self.addCleanup(store.close)
        return store

    def materialize(self, tag: str, name: str, payload: bytes) -> Path:
        directory = self.root / tag
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(payload)
        os.utime(path, ns=(PINNED_MTIME_NS, PINNED_MTIME_NS))
        return path

    def dump(self, store: Store) -> dict[str, list[tuple]]:
        """Every derived and bookkeeping row, ordered by rowid, as tuples."""

        dumps = {}
        for table, order_by in (
            ("session", "rowid"),
            ("transcript_event", "event_id"),
            ("observation", "obs_id"),
        ):
            cursor = store.connection.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
            columns = [description[0] for description in cursor.description]
            dumps[table] = (columns, cursor.fetchall())
        return dumps

    def drop_columns(self, columns, rows, drop):
        keep = [index for index, name in enumerate(columns) if name not in drop]
        self.assertTrue(keep, "comparison dropped every column of a table")
        return [tuple(row[index] for index in keep) for row in rows]

    def cursor_fields(self, row: CursorRow) -> tuple:
        return tuple(getattr(row, field) for field in CURSOR_COMPARED_FIELDS)

    def assert_cross_store_identical(
        self, work: Store, work_path: str, control: Store, control_path: str, label: str
    ) -> None:
        """Derived rows match across stores minus when/where bookkeeping."""

        work_dump, control_dump = self.dump(work), self.dump(control)
        for table, (columns, work_rows) in work_dump.items():
            self.assertEqual(
                self.drop_columns(columns, control_dump[table][1], CROSS_STORE_DROPS[table]),
                self.drop_columns(columns, work_rows, CROSS_STORE_DROPS[table]),
                f"{label}: {table} rows differ across an equivalent rebuild",
            )
        work_cursor = twill_cursor.load_cursor(work.connection, work_path)
        control_cursor = twill_cursor.load_cursor(control.connection, control_path)
        self.assertIsNotNone(work_cursor, label)
        self.assertIsNotNone(control_cursor, label)
        self.assertEqual(
            self.cursor_fields(control_cursor),
            self.cursor_fields(work_cursor),
            f"{label}: cursor rows differ across an equivalent rebuild",
        )

    def assert_offset_monotone(
        self, previous: CursorRow, current: CursorRow, label: str
    ) -> None:
        """Plan §10.1: a cursor never moves backwards except on an identity change."""

        if previous.identity_sha == current.identity_sha:
            self.assertGreaterEqual(
                current.last_offset,
                previous.last_offset,
                f"{label}: cursor moved backwards without an identity change",
            )
        self.assertGreaterEqual(current.last_offset, 0, label)
        self.assertLessEqual(current.last_offset, current.size, label)

    # -- 1: double ingest over unchanged inputs (§8.3, the idle-resume path) --

    def test_second_ingest_over_unchanged_inputs_is_byte_identical(self):
        for case in self.cases:
            with self.subTest(case=case.label):
                path = self.materialize(case.label.replace("/", "-"), "transcript.jsonl",
                                        case.final_bytes())
                store = self.store(f"double-{case.label.replace('/', '-')}")
                first = store.ingest_path(path)
                self.assertEqual(first["action"], twill_cursor.ACTION_PARSE, case.label)
                before = self.dump(store)
                before_cursor = twill_cursor.load_cursor(store.connection, str(path))

                second = store.ingest_path(path)

                # The second pass took the idle-resume path and re-derived
                # nothing at all.
                self.assertEqual(second["action"], twill_cursor.ACTION_RESUME, case.label)
                self.assertEqual(second["events"], 0, case.label)
                after = self.dump(store)
                for table, (columns, rows) in before.items():
                    self.assertEqual(rows, after[table][1],
                                     f"{case.label}: {table} changed on re-ingest")
                # Byte-identical includes the rowids, which the row tuples carry.
                self.assertEqual(
                    [row[0] for row in before["observation"][1]],
                    [row[0] for row in after["observation"][1]],
                    f"{case.label}: observation rowids moved on an idle pass",
                )
                after_cursor = twill_cursor.load_cursor(store.connection, str(path))
                self.assertEqual(
                    self.cursor_fields(after_cursor),
                    self.cursor_fields(before_cursor),
                    f"{case.label}: cursor bookkeeping changed beyond its stamps",
                )
                self.assert_offset_monotone(before_cursor, after_cursor, case.label)
                # Every fixture is newline-terminated, so each scenario — the
                # truncated one included — commits every byte it saw: the
                # invalid final line is counted as a parse error, not deferred.
                self.assertEqual(after_cursor.last_offset, len(case.final_bytes()), case.label)

    # -- 2: an independent rebuild derives the same rows ----------------------

    def test_rebuild_from_scratch_derives_the_same_rows(self):
        for case in self.cases:
            with self.subTest(case=case.label):
                tag = case.label.replace("/", "-")
                payload = case.final_bytes()
                first_path = self.materialize(f"rebuild-a-{tag}", "transcript.jsonl", payload)
                second_path = self.materialize(f"rebuild-b-{tag}", "transcript.jsonl", payload)
                first = self.store(f"rebuild-a-{tag}")
                second = self.store(f"rebuild-b-{tag}")
                first.ingest_path(first_path)
                second.ingest_path(second_path)
                self.assert_cross_store_identical(
                    first, str(first_path), second, str(second_path), case.label
                )

    # -- 3: the resume lifecycle converges on one full parse (§10.1) ----------

    def test_resume_lifecycle_converges_to_one_full_parse_with_a_monotone_cursor(self):
        for case in self.cases:
            with self.subTest(case=case.label):
                tag = case.label.replace("/", "-")
                work_path = self.materialize(f"lifecycle-{tag}", "transcript.jsonl",
                                             case.stages()[0][0])
                control_path = self.materialize(f"control-{tag}", "transcript.jsonl",
                                                case.final_bytes())
                work = self.store(f"lifecycle-{tag}")
                control = self.store(f"control-{tag}")

                previous = None
                for stage, (payload, expected_action) in enumerate(case.stages()):
                    if payload is not None:
                        work_path.write_bytes(payload)
                        os.utime(work_path, ns=(PINNED_MTIME_NS, PINNED_MTIME_NS))
                    summary = work.ingest_path(work_path)
                    self.assertEqual(
                        summary["action"], expected_action,
                        f"{case.label} stage {stage}: unexpected ingest action",
                    )
                    current = twill_cursor.load_cursor(work.connection, str(work_path))
                    self.assertIsNotNone(current, f"{case.label} stage {stage}")
                    if previous is not None:
                        self.assert_offset_monotone(
                            previous, current, f"{case.label} stage {stage}"
                        )
                    previous = current

                self.assertEqual(
                    previous.last_offset, len(case.final_bytes()),
                    f"{case.label}: lifecycle cursor stopped short of the final bytes",
                )
                control.ingest_path(control_path)
                self.assert_cross_store_identical(
                    work, str(work_path), control, str(control_path), case.label
                )


if __name__ == "__main__":
    unittest.main()
