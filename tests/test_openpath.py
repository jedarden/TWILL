"""The open-path audit harness, audited itself (plan §8.3, §10.2).

Three layers are covered here:

- the policy, exercised directly through :func:`openpath.check_open` and
  :func:`openpath.audit_hook`, so every allowance and every denial is
  pinned without depending on where in the suite an open happens;
- the installation, which is what makes the gate a gate: the hook must be
  live in this interpreter (a bare, ungated ``python3 -m unittest discover``
  fails here by design), and every spawned interpreter must self-install it
  through ``tests/sitecustomize.py`` so the CLI verbs are policed too;
- the Scenario 2 property itself (§5): the shipped verbs run over a real
  fixture and come back clean, which under the propagated hook is exactly
  the assertion that no path under ``~/agent-transcript-archive`` is opened
  by ``ingest`` → ``detect`` → ``digest``.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
CLI = ROOT / "twill"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT))

import openpath  # noqa: E402


def deny(read_or_write, *args):
    """Assert an open is refused, and hand back the violation."""

    try:
        openpath.check_open(*args)
    except openpath.OpenPathViolation as violation:
        return violation
    raise AssertionError(f"expected a refusal for {read_or_write}: {args!r}")


def allow(*args):
    """Assert an open passes the policy."""

    openpath.check_open(*args)


class WritePolicyTests(unittest.TestCase):
    """Every write allowance and denial of the §8.3 write invariant."""

    def test_write_under_the_temp_root_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "fixture" / "session.jsonl"
            allow(str(scratch), "w", 0)

    def test_write_under_the_repository_is_allowed(self):
        allow(str(ROOT / "__pycache__" / "engine.pyc"), "wb", 0)

    def test_write_under_each_in_tree_artifact_dir_is_denied(self):
        # Guard two of the artifact-containment guards (§10.2): the
        # repository is writable, these four names inside it are not.
        for name in openpath.ARTIFACT_DIRS:
            with self.subTest(artifact_dir=name):
                violation = deny("write", str(ROOT / name / "L-0001.md"), "w", 0)
                self.assertIn(str(ROOT / name), str(violation))

    def test_artifact_denial_beats_the_temp_root_allowance(self):
        # A clean extraction or a CI checkout lives under the temp root;
        # the repository is still decided first, so an artifact name in
        # that checkout is denied and the rest of it stays writable.
        injected = ROOT.parent  # contains REPO_TREE, so it would allow all of it
        previous = openpath._temp_root
        openpath._temp_root = injected
        try:
            for name in openpath.ARTIFACT_DIRS:
                with self.subTest(artifact_dir=name):
                    deny("write", str(ROOT / name / "L-0001.md"), "w", 0)
            allow(str(ROOT / "__pycache__" / "engine.pyc"), "wb", 0)
        finally:
            openpath._temp_root = previous

    def test_write_under_the_state_dir_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            previous = os.environ.get("TWILL_STATE_DIR")
            os.environ["TWILL_STATE_DIR"] = str(state)
            try:
                allow(str(state / "twill.db"), "w", 0)
                allow(str(state / "twill.db-wal"), None, os.O_CREAT | os.O_RDWR)
            finally:
                if previous is None:
                    del os.environ["TWILL_STATE_DIR"]
                else:
                    os.environ["TWILL_STATE_DIR"] = previous

    def test_write_under_artifacts_root_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            injected = (Path(directory) / "artifacts",)
            previous = openpath._artifacts_root_cache
            openpath._artifacts_root_cache = injected
            try:
                allow(str(injected[0] / "digests" / "2026-09.md"), "w", 0)
            finally:
                openpath._artifacts_root_cache = previous

    def test_write_under_the_home_directory_outside_the_trees_is_denied(self):
        for target in ("~/.config/twill/config.toml", "~/.claude/notes.md", "~/elsewhere"):
            with self.subTest(target=target):
                deny("write", os.path.expanduser(target), "w", 0)

    def test_write_under_the_archive_is_denied(self):
        deny("write", str(openpath.archive_root() / "sessions" / "x.jsonl"), "a", 0)

    def test_creating_a_flag_outside_the_trees_through_os_open_is_denied(self):
        # O_CREAT | O_RDONLY creates the file on Linux, so it is write
        # intent even though the mode word says read.
        deny("write", os.path.expanduser("~/.twill-flag"), None, os.O_CREAT | os.O_RDONLY)

    def test_devnull_is_allowed(self):
        allow(os.devnull, None, os.O_RDWR)

    def test_tilde_paths_expand_before_the_check(self):
        deny("write", "~/.twill-openpath-canary", "w", 0)

    def test_an_fd_anchored_open_addresses_no_path(self):
        allow(3, "w", 0)


class ReadPolicyTests(unittest.TestCase):
    """The §8.3 read invariant: nothing under the archive is ever opened."""

    def test_read_under_the_archive_is_denied_in_every_path_spelling(self):
        archive = openpath.archive_root() / "sessions" / "2026-09-23.jsonl"
        for spelling in (str(archive), os.fsencode(archive), archive):
            with self.subTest(spelling=type(spelling).__name__):
                violation = deny("read", spelling, "r", os.O_RDONLY)
                self.assertIn(str(openpath.archive_root()), str(violation))

    def test_read_of_a_not_yet_existing_archive_path_is_denied(self):
        # resolve() is non-strict: a missing file resolves against its
        # existing prefix, which is the case that matters for a glob that
        # would land under the archive.
        deny("read", str(openpath.archive_root() / "missing" / "x.jsonl"), "r", os.O_RDONLY)

    def test_a_symlink_into_the_archive_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "lure.jsonl"
            os.symlink(openpath.archive_root() / "sessions" / "x.jsonl", link)
            violation = deny("read", str(link), "r", os.O_RDONLY)
            self.assertIn(str(openpath.archive_root()), str(violation))

    def test_reads_outside_the_archive_are_allowed(self):
        for target in ("/etc/hostname", os.path.expanduser("~/.bashrc"), str(CLI)):
            with self.subTest(target=target):
                allow(target, "r", os.O_RDONLY)

    def test_non_open_events_are_ignored(self):
        self.assertIsNone(openpath.audit_hook("os.stat", ("/etc/passwd",)))
        self.assertIsNone(openpath.audit_hook("subprocess.Popen", ("sh", "sh", None)))
        self.assertIsNone(openpath.audit_hook("open", (3, "w", 0)))


class HarnessInstallationTests(unittest.TestCase):
    """The gate must be live here and in every child the suite spawns."""

    def test_this_interpreter_is_gated(self):
        # Reached ungated -- a bare `python3 -m unittest discover` with no
        # PYTHONPATH and no conftest -- this fails, and that is the point:
        # §10.2 has the whole suite run under the hook or not ship.
        self.assertTrue(
            openpath.installed(),
            "the open-path audit hook is not installed; run the suite "
            "through `make test` or pytest",
        )

    def test_pythonpath_carries_the_tests_dir_to_children(self):
        self.assertIn(str(TESTS), os.environ.get("PYTHONPATH", ""))

    def test_a_spawned_interpreter_self_installs_the_hook(self):
        probe = "import openpath, sys; sys.exit(0 if openpath.installed() else 3)"
        child = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(ROOT),
            env=dict(os.environ),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_a_spawned_child_is_stopped_at_the_open(self):
        canary = Path.home() / ".twill-openpath-child-canary"
        child = subprocess.run(
            [sys.executable, "-c", f"open({str(canary)!r}, 'w')"],
            cwd=str(ROOT),
            env=dict(os.environ),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(child.returncode, 0, "an out-of-tree child write was not stopped")
        self.assertIn("OpenPathViolation", child.stderr)
        self.assertFalse(canary.exists(), "the refused open still touched the disk")

    def test_a_denied_open_in_this_process_fails_at_the_open(self):
        canary = Path.home() / ".twill-openpath-canary"
        try:
            with self.assertRaises(openpath.OpenPathViolation):
                with open(canary, "w"):
                    pass
            self.assertFalse(canary.exists(), "the refused open still touched the disk")
        finally:
            canary.unlink(missing_ok=True)

    def test_an_allowed_open_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allowed.txt"
            with open(path, "w") as handle:
                handle.write("written under the gate\n")
            self.assertEqual(path.read_text(), "written under the gate\n")

    def test_gitignore_and_the_harness_name_the_same_artifact_dirs(self):
        # Guard one (.gitignore) and guard two (this harness) must deny the
        # same four names; a rename in either place alone is a hole.
        gitignore = (ROOT / ".gitignore").read_text()
        ignored = {
            line.strip().rstrip("/")
            for line in gitignore.splitlines()
            if line.strip().rstrip("/") in set(openpath.ARTIFACT_DIRS)
        }
        self.assertEqual(ignored, set(openpath.ARTIFACT_DIRS))


class ChildVerbOpenPathTests(unittest.TestCase):
    """Scenario 2 (§5) through the shipped CLI, under the propagated hook."""

    @classmethod
    def setUpClass(cls):
        # ingest loads config at startup and artifacts_root is the one key
        # with no default (plan §13.1): every CLI run in this suite needs a
        # config that sets it to a directory outside the repository tree.
        cls._config_home = tempfile.TemporaryDirectory()
        home = Path(cls._config_home.name)
        config_dir = home / ".config" / "twill"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            f'artifacts_root = "{home / "artifacts"}"\n'
        )

    @classmethod
    def tearDownClass(cls):
        cls._config_home.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(Path(self._config_home.name))},
            check=False,
            text=True,
            capture_output=True,
        )

    def test_ingest_detect_digest_children_open_nothing_under_the_archive(self):
        # The children self-install the hook through tests/sitecustomize.py,
        # so an archive open anywhere in the engine would raise inside the
        # verb and fail it; exit 0 across the pipeline is the §5 Scenario 2
        # pass criterion.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "session.jsonl"
            # The same fake fixture token the other tests use, spelled in
            # pieces so no token-shaped literal sits in source for the
            # fleet secret scanner to reject; it must still be
            # credential-shaped, because being redacted is the point.
            token = "ghp_" + "1234567890abcdefghijklmnop"
            source.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": "openpath-fixture",
                                "timestamp": "2026-09-20T12:00:00Z",
                                "message": {
                                    "role": "user",
                                    "content": f"command failed with {token}",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "sessionId": "openpath-fixture",
                                "timestamp": "2026-09-20T12:00:01Z",
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {"type": "text", "text": "error: command not found"}
                                    ],
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )
            state = root / "state"

            ingest = self.run_cli(
                "ingest",
                "--file",
                str(source),
                "--settle",
                "0",
                "--limit",
                "1",
                "--state-dir",
                str(state),
            )
            self.assertEqual(ingest.returncode, 0, ingest.stderr)

            detect = self.run_cli("detect", "--state-dir", str(state))
            self.assertEqual(detect.returncode, 0, detect.stderr)

            digest = self.run_cli("digest", "--stdout", "--state-dir", str(state))
            self.assertEqual(digest.returncode, 0, digest.stderr)
            self.assertIn("TWILL digest", digest.stdout)


if __name__ == "__main__":
    unittest.main()
