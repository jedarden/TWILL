"""Integration: the Explain and Apply workflow never mutates rule sources.

README "What it is not" is the invariant under test: TWILL is not an
unsupervised writer -- it never edits CLAUDE.md, memory, hooks, skills, or
another repository; it proposes, and a human accepts.  Phase 5's completion
criteria already name the refusal to auto-accept at the state-machine level;
this module covers the same property end to end (plan §10.1's Integration
row): after a real Explain pass and the operator Apply verbs run over a
corpus whose rule sources TWILL has just indexed, every rule source comes
back byte for byte identical.

The open-path audit gate (§10.2) polices *where* writes may land, but a test
layout lives under the temporary root the gate deliberately allows as
scaffolding, so the gate alone cannot tell "wrote inside the fake home" from
"wrote inside the operator's real rule corpus".  This module therefore builds
the layout the default ``rule_globs`` actually read -- MEMORY.md and a leaf,
``~/CLAUDE.md``, an external git repository's ``AGENTS.md``, a user skill and
a project skill -- snapshots it, and byte-compares across the workflow.  The
permitted outputs are enumerated just as exactly: draft lesson files under
``artifacts_root/lessons``, the routing frontmatter inside them, derived rows
under the state directory, and the human-installable guard artifact under
``artifacts_root/guards``.  Anything else changing -- a touched MEMORY.md, a
new file in the external repository, a staged git edit -- fails by name.

Two clusters drive the workflow so both halves of Apply are exercised: the
``foo`` lesson is applied at the recommended ``environment`` layer, and the
``bar`` lesson at ``agents_md`` with ``--emit-guard`` -- the layer whose
guard proposal is an ``AGENTS.md`` paragraph, i.e. exactly the file an
unsupervised writer would have edited.  Explain runs in-process with the
``claude -p`` child mocked (the one sanctioned egress, §10.2) as in the
Scenario 1 lifecycle; accept and apply run through the real CLI so their
writes are policed by the child audit hook like any timer-driven run.
"""

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_explainer  # noqa: E402
import twill_lessons  # noqa: E402
import twill_schema  # noqa: E402
from twill_reader import ClaudeCodeLineParser  # noqa: E402

#: Two distinct missing binaries, so Explain drafts two independent lessons.
PROGRAMS = ("foo", "bar")
SESSIONS_PER_PROGRAM = 3
FAILURE_AT = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)

#: The bead ids the operator records for each applied lesson.
BEAD_IDS = {"foo": "twill-rulefix-env", "bar": "twill-rulefix-agents"}

#: Model-authored summaries, one per cluster: two sentences, one line, the
#: same shape the strict Explain output contract enforces.
LESSON_SUMMARIES = {
    "foo": (
        "The missing foo binary fails repeated preflight runs. "
        "Install foo on the host before retrying the command."
    ),
    "bar": (
        "The missing bar binary fails repeated release checks. "
        "Install bar on the host before retrying the command."
    ),
}

#: The first line of every rendered guard template; a marker that must never
#: appear in a rule source, because a guard is a proposal a human installs.
GUARD_MARKER = "<!-- TWILL guard"

LESSON_FILE_RE = re.compile(r"^L-[0-9a-f]{8}\.md$")
GUARD_FILE_RE = re.compile(r"^L-[0-9a-f]{8}\.agents\.md$")


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def claude_record(
    session_id: str,
    timestamp: datetime,
    content: object,
    record_type: str,
) -> str:
    return json.dumps(
        {
            "type": record_type,
            "sessionId": session_id,
            "cwd": "/workspace/atlas",
            "timestamp": isoformat(timestamp),
            "message": {
                "role": "assistant" if record_type == "assistant" else "user",
                "content": content,
            },
        },
        separators=(",", ":"),
    )


def failed_run_lines(
    session_id: str, timestamp: datetime, index: int, program: str
) -> tuple[str, str]:
    tool_id = f"toolu-{session_id}-{index}"
    return (
        claude_record(
            session_id,
            timestamp,
            [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Bash",
                    "input": {"command": f"{program} --version"},
                }
            ],
            "assistant",
        ),
        claude_record(
            session_id,
            timestamp + timedelta(seconds=1),
            [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Exit code 127\n/bin/sh: {program}: command not found",
                    "is_error": True,
                }
            ],
            "user",
        ),
    )


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(root: Path, *, skip_git: bool = False) -> dict[str, str]:
    """One content-addressed snapshot of a tree: relative path -> digest.

    Directories map to a constant so an emptied or newly created directory is
    itself a visible change; symlinks map to their target.  ``skip_git``
    excludes ``.git`` from the digest -- git's own cleanliness checks police
    that directory better than a content hash polices its volatile index.
    """

    entries: dict[str, str] = {}
    if not root.is_dir():
        return entries
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if skip_git and (relative == ".git" or relative.startswith(".git/")):
            continue
        if path.is_symlink():
            entries[relative] = f"symlink:{os.readlink(path)}"
        elif path.is_dir():
            entries[relative] = "dir"
        elif path.is_file():
            entries[relative] = file_digest(path)
    return entries


def diff_manifests(
    before: dict[str, str], after: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """Return (added, removed, changed) relative paths between snapshots."""

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        key for key in set(before) & set(after) if before[key] != after[key]
    )
    return added, removed, changed


def permitted_artifact_path(relative: str) -> bool:
    """Whether one artifacts_root path is a sanctioned workflow output.

    Lesson drafts, guard templates, their directories, and the v1 interchange
    manifest qualify -- any other artifact-side change is a surprise the
    accounting assertion names.
    """

    if relative in {
        "artifacts",
        "artifacts/manifest.json",
        "artifacts/lessons",
        "artifacts/guards",
    }:
        return True
    parts = relative.split("/")
    return len(parts) == 3 and parts[0] == "artifacts" and (
        (parts[1] == "lessons" and LESSON_FILE_RE.fullmatch(parts[2]))
        or (parts[1] == "guards" and GUARD_FILE_RE.fullmatch(parts[2]))
    )


@unittest.skipUnless(shutil.which("git"), "the external-repository fixture needs git")
class ExplainApplyNeverMutateRuleSourcesTests(unittest.TestCase):
    """One shared workflow run, three frozen checkpoints, named assertions."""

    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._temporary.cleanup)
        cls.root = Path(cls._temporary.name)
        cls.home = cls.root / "home"
        cls.artifacts = cls.root / "artifacts"
        cls.state = cls.root / "state"
        cls.project = cls.home / ".claude" / "projects" / "atlas"
        cls.memory = cls.project / "memory"
        cls.skills = cls.home / ".claude" / "skills"
        cls.repo = cls.home / "demo-repo"
        cls._write_config()
        cls._stage_rule_sources()
        cls._stage_transcripts()
        cls._drive_to_ranked_clusters()

        cls.before = cls._checkpoint()
        cls._run_explain()
        cls.after_explain = cls._checkpoint()
        cls._freeze_drafts()
        cls._run_operator_apply()
        cls.after_apply = cls._checkpoint()

    # -- fixture ----------------------------------------------------------

    @classmethod
    def _write_config(cls) -> None:
        config_dir = cls.home / ".config" / "twill"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            f'artifacts_root = "{cls.artifacts}"\nretention = "3650d"\n',
            encoding="utf-8",
        )
        # rule_globs stays at its defaults on purpose: the engine's own view
        # of the rule corpus must be what resolves against the fake home.

    @classmethod
    def _stage_rule_sources(cls) -> None:
        """Lay out every rule layer the default rule_globs discover."""

        (cls.home / "CLAUDE.md").write_text(
            "# Home instructions\n\n- Stage fixtures under scratch before editing.\n",
            encoding="utf-8",
        )
        cls.memory.mkdir(parents=True)
        (cls.memory / "MEMORY.md").write_text(
            "# Memory\n\n- Prefer precise staging paths.\n",
            encoding="utf-8",
        )
        (cls.memory / "tooling.md").write_text(
            "# Tooling notes\n\n- Preflight binaries belong to the host image, "
            "not to any repository.\n",
            encoding="utf-8",
        )
        cls.skills.mkdir(parents=True)
        (cls.skills / "deploy-check").mkdir()
        (cls.skills / "deploy-check" / "SKILL.md").write_text(
            "---\nname: deploy-check\n---\n\n"
            "Verify the release checklist before deploying anything.\n",
            encoding="utf-8",
        )
        # The external repository: a real git checkout TWILL neither owns nor
        # may touch, whose AGENTS.md and project skill are rule-corpus inputs.
        cls.repo.mkdir(parents=True)
        (cls.repo / "AGENTS.md").write_text(
            "# AGENTS.md\n\n- Run the preflight checklist before committing.\n",
            encoding="utf-8",
        )
        (cls.repo / "README.md").write_text(
            "# demo-repo\n\nA fixture repository owned outside TWILL.\n",
            encoding="utf-8",
        )
        scripts = cls.repo / "scripts"
        scripts.mkdir()
        (scripts / "preflight.sh").write_text(
            "#!/bin/sh\nset -eu\nexec make check\n",
            encoding="utf-8",
        )
        repo_skill = cls.repo / ".claude" / "skills" / "release"
        repo_skill.mkdir(parents=True)
        (repo_skill / "SKILL.md").write_text(
            "---\nname: release\n---\n\nWalk the release steps in order.\n",
            encoding="utf-8",
        )
        cls._git("init", "-q")
        cls._git("add", ".")
        cls._git(
            "-c",
            "user.name=jedarden",
            "-c",
            "user.email=github@jedarden.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "fixture baseline",
        )

    @classmethod
    def _stage_transcripts(cls) -> None:
        """Write sessions whose only friction is two recurring missing binaries."""

        cls.project.mkdir(parents=True, exist_ok=True)
        for program in PROGRAMS:
            for index in range(SESSIONS_PER_PROGRAM):
                session_id = f"atlas-{program}-{index:02d}"
                timestamp = FAILURE_AT + timedelta(minutes=10 * index)
                lines = failed_run_lines(session_id, timestamp, 0, program)
                (cls.project / f"{session_id}.jsonl").write_text(
                    "\n".join(lines) + "\n",
                    encoding="utf-8",
                )

    # -- driving the workflow ---------------------------------------------

    @classmethod
    def run_cli(cls, *args: str) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "HOME": str(cls.home)}
        environment.pop("TWILL_SOURCE_ROOTS", None)
        environment.pop("TWILL_STATE_DIR", None)
        return subprocess.run(
            [sys.executable, str(CLI), *args, "--state-dir", str(cls.state)],
            cwd=ROOT,
            env=environment,
            check=False,
            text=True,
            capture_output=True,
        )

    @classmethod
    def _run_json_cli(cls, *args: str) -> dict[str, object]:
        result = cls.run_cli(*args, "--json")
        if result.returncode != 0:
            raise AssertionError(f"{args} exited {result.returncode}: {result.stderr}")
        return json.loads(result.stdout)

    @classmethod
    def _git(cls, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(cls.repo), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"git {args} exited {completed.returncode}: {completed.stderr.strip()}"
            )
        return completed.stdout

    @classmethod
    def _seed_normalized_detector_rows(cls) -> None:
        """Adapt parser events to D-01's normalized detector input contract."""

        rows = []
        for path in sorted(cls.project.glob("*.jsonl")):
            program = path.stem.split("-")[1]
            parser = ClaudeCodeLineParser()
            events = []
            for source_line, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                events.extend(parser.parse_line(line, source_line))
            events.extend(parser.finish())
            if not events:
                raise AssertionError(f"no parser events for {path}")
            for event in events:
                if event.error_excerpt is None:
                    raise AssertionError(f"no error excerpt in {path}")
                rows.append(
                    (
                        event.session_id,
                        event.timestamp,
                        event.timestamp,
                        program,
                        event.command,
                        event.error_excerpt,
                        hashlib.sha256(event.error_excerpt.encode()).hexdigest()[:12],
                        event.error_excerpt,
                        event.cwd,
                    )
                )
        connection = twill_schema.connect(cls.state)
        try:
            connection.executemany(
                "INSERT INTO observation(session_id, ts_utc, ts_local, kind, "
                "program, command, signature, sig_hash, excerpt, cwd) "
                "VALUES (?, ?, ?, 'run_failed', ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()
        finally:
            connection.close()

    @classmethod
    def _drive_to_ranked_clusters(cls) -> None:
        """ingest, detect and rank until two open candidates are ready."""

        ingest = cls.run_cli("ingest", "--settle", "0", "--limit", "100", "--json")
        if ingest.returncode != 0:
            raise AssertionError(f"ingest exited {ingest.returncode}: {ingest.stderr}")
        cls._seed_normalized_detector_rows()

        detected = cls._run_json_cli("detect", "--detector", "D-01", "--window", "3650d")
        if detected["data"]["detectors"][0]["clusters"] != len(PROGRAMS):
            raise AssertionError(f"expected {len(PROGRAMS)} clusters: {detected}")

        ranked = cls._run_json_cli("rank", "--top", "5")
        corpus = ranked["data"]["corpus"]
        expected_docs = {
            "CLAUDE.md",
            "memory/MEMORY.md",
            "memory/tooling.md",
            "skills/deploy-check/SKILL.md",
            "demo-repo/AGENTS.md",
            "demo-repo/.claude/skills/release/SKILL.md",
        }
        if corpus["documents"] != len(expected_docs) or corpus["skipped"]:
            raise AssertionError(
                f"rule corpus did not index every staged source: {corpus}"
            )
        clusters = {cluster["key"]: cluster for cluster in ranked["data"]["clusters"]}
        if set(clusters) != {f"command-not-found:{program}" for program in PROGRAMS}:
            raise AssertionError(f"unexpected candidate clusters: {sorted(clusters)}")
        for key, cluster in clusters.items():
            if cluster["state"] != "open":
                raise AssertionError(f"cluster {key} is not an open candidate: {cluster}")
            if cluster["sessions"] != SESSIONS_PER_PROGRAM:
                raise AssertionError(f"cluster {key} session count drifted: {cluster}")

    @classmethod
    def _run_explain(cls) -> None:
        model_output = json.dumps(
            {
                "lessons": [
                    {
                        "cluster_id": f"D-01:command-not-found:{program}",
                        "summary": LESSON_SUMMARIES[program],
                    }
                    for program in PROGRAMS
                ]
            },
            separators=(",", ":"),
        )
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(cls.home)}), mock.patch(
            "sys.stdout", stdout
        ), mock.patch.object(
            twill_explainer, "invoke_claude", return_value=model_output
        ) as invoke:
            code = twill_app.main(
                ["explain", "--top", "5", "--json", "--state-dir", str(cls.state)]
            )
        if code != 0:
            raise AssertionError(f"explain exited {code}: {stdout.getvalue()}")
        invoke.assert_called_once()
        cls.explain_result = json.loads(stdout.getvalue())

    @classmethod
    def _freeze_drafts(cls) -> None:
        """Capture the drafted lessons exactly as Explain left them."""

        lesson_paths = sorted((cls.artifacts / "lessons").glob("L-*.md"))
        if len(lesson_paths) != len(PROGRAMS):
            raise AssertionError(f"expected drafted lesson files: {lesson_paths}")
        cls.lesson_ids: dict[str, str] = {}
        cls.drafts: dict[str, dict[str, object]] = {}
        for path in lesson_paths:
            record = twill_lessons.load_lesson(cls.artifacts, path.stem)
            program = record.key.partition(":")[2]
            cls.lesson_ids[program] = record.id
            cls.drafts[program] = record.as_dict()

    @classmethod
    def _run_operator_apply(cls) -> None:
        """The human half: accept both drafts, apply each at its own layer."""

        cls.apply_outputs: dict[str, dict[str, object]] = {}
        plan = (
            ("foo", "environment", ()),
            ("bar", "agents_md", ("--emit-guard",)),
        )
        for program, layer, extra in plan:
            lesson_id = cls.lesson_ids[program]
            accepted = cls.run_cli("accept", lesson_id, "--json")
            if accepted.returncode != 0:
                raise AssertionError(
                    f"accept {lesson_id} exited {accepted.returncode}: {accepted.stderr}"
                )
            applied = cls.run_cli(
                "apply",
                lesson_id,
                "--layer",
                layer,
                "--bead",
                BEAD_IDS[program],
                *extra,
                "--json",
            )
            if applied.returncode != 0:
                raise AssertionError(
                    f"apply {lesson_id} exited {applied.returncode}: {applied.stderr}"
                )
            cls.apply_outputs[program] = json.loads(applied.stdout)

    # -- snapshots ---------------------------------------------------------

    @classmethod
    def _checkpoint(cls) -> dict[str, object]:
        """Freeze every tree the workflow may not -- or may -- touch.

        The git queries run before the manifest is taken so the index refresh
        ``git status`` performs lands inside the snapshot rather than between
        two of them.
        """

        porcelain = cls._git("status", "--porcelain")
        head = cls._git("rev-parse", "HEAD").strip()
        return {
            "tree": manifest(cls.root),
            "rule_sources": {
                "~/CLAUDE.md": {"CLAUDE.md": file_digest(cls.home / "CLAUDE.md")},
                "memory leaves": manifest(cls.memory),
                "user skills": manifest(cls.skills),
                "external repository worktree": manifest(cls.repo, skip_git=True),
            },
            "porcelain": porcelain,
            "head": head,
        }

    # -- the invariant ------------------------------------------------------

    def test_rule_sources_are_byte_identical_across_the_workflow(self):
        for checkpoint in (self.after_explain, self.after_apply):
            for name, snapshot in self.before["rule_sources"].items():
                self.assertEqual(
                    checkpoint["rule_sources"][name],
                    snapshot,
                    f"{name} was mutated by the Explain/Apply workflow",
                )

    def test_external_repository_stays_git_clean(self):
        for checkpoint in (self.after_explain, self.after_apply):
            self.assertEqual(
                checkpoint["porcelain"],
                "",
                "the external repository has staged or untracked changes",
            )
            self.assertEqual(
                checkpoint["head"],
                self.before["head"],
                "the external repository gained or lost commits",
            )

    def test_every_change_lands_in_a_permitted_output(self):
        added, removed, changed = diff_manifests(
            self.before["tree"], self.after_apply["tree"]
        )
        violators = sorted(
            path
            for path in added + removed + changed
            if path != "artifacts"
            and not path.startswith(("artifacts/", "state/"))
        )
        self.assertEqual(
            violators,
            [],
            "the Explain/Apply workflow changed paths outside artifacts_root "
            "and the state directory",
        )
        for path in added + changed:
            if path == "artifacts" or path.startswith("artifacts/"):
                self.assertTrue(
                    permitted_artifact_path(path),
                    f"unexpected artifact-side change: {path}",
                )
        # A removal is never part of this workflow; the state directory's
        # transient WAL sidecars are the only files allowed to vanish.
        unexpected_removals = [
            path
            for path in removed
            if not (path.startswith("state/") and Path(path).name.startswith("twill.db"))
        ]
        self.assertEqual(unexpected_removals, [], "the workflow removed input files")
        self.assertIn(
            "state/twill.db",
            changed,
            "the derived state database should have advanced",
        )

    def test_explain_adds_exactly_the_draft_lesson_files(self):
        self.assertEqual(self.explain_result["data"]["drafts"], len(PROGRAMS))
        added, _, changed = diff_manifests(
            self.before["tree"], self.after_explain["tree"]
        )
        artifact_added = sorted(
            path for path in added if path == "artifacts" or path.startswith("artifacts/")
        )
        expected = sorted(
            ["artifacts", "artifacts/lessons"]
            + [
                f"artifacts/lessons/{lesson_id}.md"
                for lesson_id in self.lesson_ids.values()
            ]
            + ["artifacts/manifest.json"]
        )
        self.assertEqual(artifact_added, expected)
        artifact_changed = [
            path for path in changed if path.startswith("artifacts/")
        ]
        self.assertEqual(
            artifact_changed,
            [],
            "Explain must not modify existing artifacts, only add drafts",
        )

    def test_drafts_are_not_automatically_accepted_or_applied(self):
        for program, draft in self.drafts.items():
            self.assertEqual(
                draft["state"],
                "draft",
                f"the {program} lesson left Explain already accepted",
            )
            routing = draft["routing"]
            self.assertEqual(routing["recommended"], "environment")
            self.assertIsNone(routing["applied"])
            self.assertIsNone(routing["applied_at"])
            self.assertIsNone(routing["bead"])
            self.assertIsNone(draft["guard"]["artifact"])
            self.assertIsNone(draft["guard"]["layer"])
        # Derived state advanced exactly as far as "drafted": the clusters are
        # marked for review and the weekly invocation quota was consumed.
        connection = sqlite3.connect(
            f"file:{self.state / 'twill.db'}?mode=ro", uri=True
        )
        try:
            states = dict(
                connection.execute(
                    "SELECT key, state FROM cluster WHERE detector_id = 'D-01'"
                )
            )
            quota = connection.execute(
                "SELECT value FROM meta WHERE key = ?",
                (twill_explainer.EXPLAIN_QUOTA_META_KEY,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(
            set(states.values()),
            {"drafted"},
            f"clusters should be marked drafted, not resolved: {states}",
        )
        self.assertIsNotNone(quota, "the Explain invocation quota was not recorded")
        self.assertRegex(quota[0], r"^\d{4}-W\d{2}$")

    def test_operator_apply_records_routing_and_emits_a_guard_proposal(self):
        environment = twill_lessons.load_lesson(
            self.artifacts, self.lesson_ids["foo"]
        ).as_dict()
        self.assertEqual(environment["state"], "applied:environment")
        self.assertEqual(environment["routing"]["applied"], "environment")
        self.assertEqual(environment["routing"]["bead"], BEAD_IDS["foo"])
        self.assertTrue(environment["routing"]["applied_at"])

        agents = twill_lessons.load_lesson(
            self.artifacts, self.lesson_ids["bar"]
        ).as_dict()
        self.assertEqual(agents["state"], "applied:agents_md")
        self.assertEqual(agents["routing"]["applied"], "agents_md")
        self.assertEqual(agents["routing"]["bead"], BEAD_IDS["bar"])
        guard_name = f"guards/{self.lesson_ids['bar']}.agents.md"
        self.assertEqual(agents["guard"]["artifact"], guard_name)
        self.assertFalse(
            agents["guard"]["installed"],
            "a generated guard must never be marked installed",
        )
        guard_text = (self.artifacts / guard_name).read_text(encoding="utf-8")
        self.assertIn(GUARD_MARKER, guard_text)
        self.assertIn("command-not-found:bar", guard_text)
        # The emitted change is a command for a human, never an executed one.
        self.assertTrue(
            self.apply_outputs["bar"]["data"]["bead_create_command"].startswith(
                "bead create"
            )
        )

    def test_guard_proposals_never_reach_rule_source_files(self):
        offenders = []
        phrases = (GUARD_MARKER,) + tuple(
            f"Install {program} on the host" for program in PROGRAMS
        )
        for path in sorted(self.home.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            for phrase in phrases:
                if phrase in text:
                    offenders.append(f"{path} contains {phrase!r}")
        self.assertEqual(
            offenders,
            [],
            "lesson or guard content leaked into the rule corpus",
        )


if __name__ == "__main__":
    unittest.main()
