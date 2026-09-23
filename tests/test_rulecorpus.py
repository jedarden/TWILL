"""Contract tests for the rule-corpus index (plan §6.1 rulecorpus, §7.1, §8.1 EC-11).

Each test pins one behaviour of ``twill_rulecorpus.index_corpus``: the
hash-first identity (a move is detected by content, not by path), the
stale-not-delete rule for vanished paths, the FTS sync rules, and the
``<layer>:<glob>`` grammar the config file validates against.
"""

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_rulecorpus  # noqa: E402
import twill_schema  # noqa: E402
from twill_rulecorpus import (  # noqa: E402
    DEFAULT_RULE_GLOBS,
    MAX_RULE_DOC_BYTES,
    discover_docs,
    index_corpus,
    parse_rule_pattern,
)

NOW = "2026-09-23T12:00:00+00:00"
LATER = "2026-09-23T18:00:00+00:00"

MEMORY_TEXT = "# Memory\n\nNever force-push to Forgejo; push only to origin.\n"
SKILL_TEXT = "# Skill\n\nSerialize ADB access; concurrent taps produce gibberish.\n"


class CorpusTestCase(unittest.TestCase):
    """Base: a state database and a corpus directory to point globs at."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.state_dir = self.root / "state"
        self.connection = twill_schema.connect(self.state_dir)
        self.addCleanup(self.connection.close)

    def write_doc(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def rule_row(self, path: Path):
        return self.connection.execute(
            "SELECT layer, sha, indexed_at, last_read_by_agent, stale "
            "FROM rule_doc WHERE path = ?",
            (str(path),),
        ).fetchone()

    def fts_paths(self, match: str) -> list[str]:
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT path FROM rule_fts WHERE rule_fts MATCH ? ORDER BY path",
                (match,),
            )
        ]


class PatternGrammarTests(unittest.TestCase):
    def test_valid_pattern_parses_to_layer_and_glob(self):
        parsed = parse_rule_pattern("memory:~/.claude/projects/*/memory/*.md")
        self.assertEqual(parsed.layer, "memory")
        self.assertEqual(parsed.pattern, "~/.claude/projects/*/memory/*.md")

    def test_pattern_without_a_layer_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            parse_rule_pattern("~/.claude/CLAUDE.md")
        self.assertIn("<layer>:<glob>", str(caught.exception))

    def test_pattern_with_an_unknown_layer_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            parse_rule_pattern("essay:~/writings/*.md")
        self.assertIn("essay", str(caught.exception))
        self.assertIn("claude_md", str(caught.exception))

    def test_pattern_with_an_empty_glob_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_rule_pattern("skill:")

    def test_every_default_glob_parses(self):
        # The defaults are data too: a typo there would only fail at the
        # weekly rank pass, so pin the grammar now.
        for entry in DEFAULT_RULE_GLOBS:
            with self.subTest(entry=entry):
                parse_rule_pattern(entry)

    def test_default_globs_cover_the_planned_corpus(self):
        layers = {parse_rule_pattern(entry).layer for entry in DEFAULT_RULE_GLOBS}
        self.assertEqual(
            layers, {"memory", "claude_md", "agents_md", "skill"},
        )


class DiscoveryTests(CorpusTestCase):
    def test_discovers_and_hashes_files(self):
        self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        discovery = discover_docs(
            [f"memory:{self.root}/home/projects/*/memory/*.md"]
        )
        self.assertEqual(len(discovery.docs), 1)
        doc = discovery.docs[0]
        self.assertEqual(doc.layer, "memory")
        self.assertEqual(doc.sha, hashlib.sha256(MEMORY_TEXT.encode()).hexdigest())
        self.assertEqual(doc.text, MEMORY_TEXT)
        self.assertEqual(doc.path, (self.root / "home/projects/p/memory/MEMORY.md").resolve())

    def test_a_file_matched_twice_keeps_the_first_layer(self):
        self.write_doc("rules/both.md", MEMORY_TEXT)
        discovery = discover_docs(
            [
                f"memory:{self.root}/rules/*.md",
                f"skill:{self.root}/rules/*.md",
            ]
        )
        self.assertEqual([doc.layer for doc in discovery.docs], ["memory"])

    def test_missing_literal_and_broken_glob_are_not_errors(self):
        discovery = discover_docs(
            [f"claude_md:{self.root}/absent.md", f"skill:{self.root}/[].md"]
        )
        self.assertEqual(discovery.docs, ())
        self.assertEqual(discovery.skipped, ())

    def test_unreadable_file_is_skipped_with_a_reason(self):
        path = self.write_doc("rules/locked.md", MEMORY_TEXT)
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        discovery = discover_docs([f"memory:{self.root}/rules/*.md"])
        self.assertEqual(discovery.docs, ())
        self.assertEqual([skipped.path for skipped in discovery.skipped], [path.resolve()])

    def test_oversized_file_is_skipped_with_a_reason(self):
        self.write_doc("rules/huge.md", "x" * (MAX_RULE_DOC_BYTES + 1))
        discovery = discover_docs([f"memory:{self.root}/rules/*.md"])
        self.assertEqual(discovery.docs, ())
        self.assertIn("cap", discovery.skipped[0].reason)


class IndexTests(CorpusTestCase):
    GLOB = "memory:{root}/home/projects/*/memory/*.md"
    SKILL_GLOB = "skill:{root}/home/skills/*/SKILL.md"

    def index(self, *extra: str, now: str = NOW):
        return index_corpus(
            self.connection,
            [
                self.GLOB.format(root=self.root),
                self.SKILL_GLOB.format(root=self.root),
                *extra,
            ],
            now=now,
        )

    def test_new_corpus_is_indexed_into_rule_doc_and_rule_fts(self):
        memory = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        skill = self.write_doc("home/skills/adb/SKILL.md", SKILL_TEXT)
        report = self.index()

        self.assertEqual(report.docs, 2)
        self.assertEqual(report.indexed, (str(memory.resolve()), str(skill.resolve())))
        self.assertEqual(
            self.rule_row(memory),
            ("memory", hashlib.sha256(MEMORY_TEXT.encode()).hexdigest(), NOW, None, 0),
        )
        self.assertEqual(self.rule_row(skill)[0], "skill")
        # The text is searchable, keyed to its path.
        self.assertEqual(self.fts_paths('"force-push"'), [str(memory.resolve())])
        self.assertEqual(self.fts_paths("gibberish"), [str(skill.resolve())])

    def test_unchanged_corpus_is_idempotent(self):
        memory = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        second = self.index(now=LATER)

        self.assertEqual(second.docs, 1)
        self.assertEqual(second.indexed, ())
        self.assertEqual(second.reindexed, ())
        self.assertEqual(second.unchanged, 1)
        # indexed_at keeps the content's first indexing time.
        self.assertEqual(self.rule_row(memory)[2], NOW)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM rule_fts").fetchone()[0], 1
        )

    def test_content_edit_reindexes_and_replaces_the_fts_text(self):
        memory = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        self.write_doc("home/projects/p/memory/MEMORY.md", "# Memory\n\nUse sata, never ssd.\n")
        second = self.index(now=LATER)

        self.assertEqual(second.reindexed, (str(memory.resolve()),))
        self.assertEqual(second.unchanged, 0)
        layer, sha, indexed_at, _, stale = self.rule_row(memory)
        self.assertEqual(indexed_at, LATER)
        self.assertEqual(stale, 0)
        self.assertNotEqual(sha, hashlib.sha256(MEMORY_TEXT.encode()).hexdigest())
        self.assertEqual(self.fts_paths("sata"), [str(memory.resolve())])
        self.assertEqual(self.fts_paths('"force-push"'), [])

    def test_layer_reclassification_leaves_content_bookkeeping_alone(self):
        memory = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        second = index_corpus(
            self.connection,
            [f"agents_md:{self.root}/home/projects/*/memory/*.md"],
            now=LATER,
        )

        self.assertEqual(second.relayered, (str(memory.resolve()),))
        self.assertEqual(second.reindexed, ())
        self.assertEqual(self.rule_row(memory)[0], "agents_md")
        self.assertEqual(self.rule_row(memory)[2], NOW)

    def test_new_doc_adopts_the_read_history_of_the_same_content(self):
        # A rename must not turn a read rule into an unread one for D-09:
        # identical content is the same rule, so last_read_by_agent follows
        # the hash, not the path.
        old = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        self.connection.execute(
            "UPDATE rule_doc SET last_read_by_agent = '2026-09-20T10:00:00+00:00' WHERE path = ?",
            (str(old.resolve()),),
        )
        self.connection.commit()
        old.unlink()
        new = self.write_doc("home/projects/q/memory/MEMORY.md", MEMORY_TEXT)
        report = self.index(now=LATER)

        self.assertEqual(report.moves, ((str(old.resolve()), (str(new.resolve()),)),))
        self.assertEqual(self.rule_row(new)[3], "2026-09-20T10:00:00+00:00")

    def test_move_is_detected_by_hash_while_the_old_row_goes_stale(self):
        self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        old = self.root / "home/projects/p/memory/MEMORY.md"
        new = self.root / "home/projects/p/memory/REMEMBER.md"
        old.rename(new)
        report = self.index(now=LATER)

        # The old path is flagged, not deleted; the content is alive at the
        # new path; the hash joins them.
        self.assertEqual(report.vanished, (str(old.resolve()),))
        self.assertEqual(report.moves, ((str(old.resolve()), (str(new.resolve()),)),))
        self.assertEqual(self.rule_row(old)[4], 1)
        self.assertEqual(self.rule_row(new)[0], "memory")
        self.assertEqual(self.rule_row(new)[4], 0)
        self.assertEqual(
            self.rule_row(old)[1],
            self.rule_row(new)[1],
            "the move is detected by content hash, not by path",
        )
        # The stale row's text survives (EC-11: visible degradation).
        self.assertEqual(
            self.fts_paths('"force-push"'),
            sorted([str(old.resolve()), str(new.resolve())]),
        )

    def test_vanished_file_without_a_successor_is_stale_and_kept(self):
        memory = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        memory.unlink()
        report = self.index(now=LATER)

        self.assertEqual(report.vanished, (str(memory.resolve()),))
        self.assertEqual(report.moves, ())
        layer, sha, indexed_at, _, stale = self.rule_row(memory)
        self.assertEqual(stale, 1)
        self.assertEqual(sha, hashlib.sha256(MEMORY_TEXT.encode()).hexdigest())
        self.assertEqual(self.fts_paths('"force-push"'), [str(memory.resolve())])

    def test_already_stale_row_that_is_still_gone_is_counted_once(self):
        memory = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        memory.unlink()
        self.index(now=LATER)
        third = self.index(now="2026-09-23T23:00:00+00:00")
        self.assertEqual(third.vanished, ())
        self.assertEqual(third.still_stale, 1)
        self.assertEqual(self.rule_row(memory)[4], 1)

    def test_restored_path_clears_staleness_without_rewriting_content(self):
        memory = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        memory.unlink()
        self.index(now=LATER)
        memory.write_text(MEMORY_TEXT)
        third = self.index(now="2026-09-23T23:00:00+00:00")

        self.assertEqual(third.restored, (str(memory.resolve()),))
        self.assertEqual(self.rule_row(memory)[4], 0)
        self.assertEqual(self.rule_row(memory)[2], NOW)

    def test_restored_path_with_new_content_reindexes_and_clears_staleness(self):
        memory = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        memory.unlink()
        self.index(now=LATER)
        memory.write_text("# Memory\n\nUse bead, never bf.\n")
        third = self.index(now="2026-09-23T23:00:00+00:00")

        self.assertEqual(third.reindexed, (str(memory.resolve()),))
        self.assertEqual(self.rule_row(memory)[4], 0)
        self.assertEqual(self.fts_paths("bead"), [str(memory.resolve())])

    def test_present_but_unglobbed_file_is_not_marked_stale(self):
        # Existence is checked per stored path, not against this run's
        # discovery: a narrowed glob must never masquerade as a vanished
        # file (the cursor sweep's discipline, applied to rules).
        memory = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        narrowed = index_corpus(
            self.connection,
            [f"skill:{self.root}/home/skills/*/SKILL.md"],
            now=LATER,
        )
        self.assertEqual(narrowed.vanished, ())
        self.assertEqual(self.rule_row(memory)[4], 0)

    def test_empty_corpus_still_sweeps_vanished_rows(self):
        memory = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        memory.unlink()
        report = index_corpus(self.connection, [], now=LATER)
        self.assertEqual(report.docs, 0)
        self.assertEqual(report.vanished, (str(memory.resolve()),))

    def test_two_live_copies_of_one_content_are_indexed_separately(self):
        first = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        second = self.write_doc("home/projects/q/memory/MEMORY.md", MEMORY_TEXT)
        report = self.index()
        self.assertEqual(report.docs, 2)
        self.assertEqual(
            self.rule_row(first)[1],
            self.rule_row(second)[1],
            "shared content, shared hash",
        )

    def test_whole_index_lands_in_one_transaction(self):
        # A failure part-way through rolls the whole run back: the previous
        # index stays intact and the next run recomputes from the files.
        first = self.write_doc("home/projects/p/memory/MEMORY.md", MEMORY_TEXT)
        self.index()
        self.write_doc("home/skills/adb/SKILL.md", SKILL_TEXT)
        original = twill_rulecorpus._fts_replace

        def explode_after_the_first_doc(connection, path_text, text):
            if path_text.endswith("SKILL.md"):
                raise sqlite3.OperationalError("injected failure")
            original(connection, path_text, text)

        with mock.patch(
            "twill_rulecorpus._fts_replace", side_effect=explode_after_the_first_doc
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.index(now=LATER)

        self.assertEqual(self.rule_row(first)[4], 0)
        self.assertEqual(self.rule_row(first)[2], NOW)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM rule_doc").fetchone()[0], 1
        )
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM rule_fts").fetchone()[0], 1
        )

        self.assertEqual(self.index(now=LATER).indexed, (str(
            (self.root / "home/skills/adb/SKILL.md").resolve()
        ),))


if __name__ == "__main__":
    unittest.main()
