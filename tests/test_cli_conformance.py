"""CLI envelope and exit-code conformance over every public verb (plan §14).

§14 fixes the machine surface: every read verb accepts ``--json`` and emits a
single JSON object ``{schema_version, generated_at, data, warnings[]}``, and
the error contract is ``0`` success · ``1`` runtime error (message on stderr,
no partial commit) · ``2`` usage error · ``3`` lock held · ``4`` validation
failure, with ``--json`` errors emitted as ``{"error": {code, message, hint}}``
so a caller never parses prose.

This module is the systematic matrix over that contract: each public verb on
a successful and a failing path — invalid configuration, lock contention,
unhealthy doctor checks, detector refusals — asserting the envelope shape,
which stream carried it, and the exit code.  Verb-level behaviour (counts,
ordering, idempotency) belongs to the per-module suites; what is asserted
here is only the interface every caller shares.

Stream separation is part of the contract: ``--json`` writes the one envelope
object — success or error — to stdout and leaves stderr empty (warnings live
inside the envelope), while the human surface prints tables to stdout and
errors and warnings to stderr.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
FIXTURE = ROOT / "tests" / "fixtures" / "transcripts" / "claude" / "clean.jsonl"
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_schema  # noqa: E402
from twill_artifacts import PublicationResult  # noqa: E402
from twill_config import TwillConfig  # noqa: E402
from twill_explainer import (  # noqa: E402
    LessonDraft,
    PromptCluster,
    PromptExcerpt,
    write_lesson_files,
)
from twill_lock import StateLock  # noqa: E402
from twill_ranker import RankedCluster  # noqa: E402


SUCCESS_KEYS = {"schema_version", "generated_at", "data", "warnings"}
ERROR_KEYS = {"error"}
DOCTOR_EXIT_BY_STATUS = {"healthy": 0, "degraded": 1, "broken": 2}

#: Every verb whose handler loads the config before doing anything else
#: (plan §3: a wrong config is a startup error, never a mid-run surprise).
CONFIG_LOADING_VERBS = (
    ("ingest", ("ingest", "--file", "/nonexistent-session.jsonl", "--settle", "0")),
    ("detect", ("detect",)),
    ("rank", ("rank",)),
    ("explain", ("explain", "--dry-run")),
    ("measure", ("measure",)),
    ("prune", ("prune",)),
    ("lessons", ("lessons",)),
    ("brief", ("brief", "{target}")),
    ("accept", ("accept", "L-00000001")),
    ("apply", ("apply", "L-00000001", "--layer", "environment", "--bead", "twill-x")),
    ("unapply", ("unapply", "L-00000001")),
    ("dismiss", ("dismiss", "D-01:command-not-found:x", "--reason", "audited")),
    ("publish", ("publish",)),
)

#: The verbs main() runs behind the state lock: the mutating set, plus the
#: two doctor recovery flags (EC-10) and a file-writing digest.
LOCK_TAKING_VERBS = (
    ("ingest", ("ingest", "--json", "--file", "/nonexistent-session.jsonl", "--settle", "0")),
    ("detect", ("detect", "--json",)),
    ("rank", ("rank", "--json",)),
    ("explain", ("explain", "--json")),
    ("measure", ("measure", "--json",)),
    ("prune", ("prune", "--json",)),
    ("accept", ("accept", "L-00000001", "--json",)),
    ("apply", ("apply", "L-00000001", "--layer", "environment", "--bead", "twill-x", "--json",)),
    ("unapply", ("unapply", "L-00000001", "--json",)),
    ("un-apply alias", ("un-apply", "L-00000001", "--json",)),
    ("dismiss", ("dismiss", "D-01:command-not-found:x", "--reason", "audited", "--json",)),
    ("doctor --rebuild", ("doctor", "--rebuild", "--json",)),
    ("doctor --rescan-redaction", ("doctor", "--rescan-redaction", "--json",)),
    ("publish", ("publish", "--json",)),
)

#: Read verbs over primed state: one success envelope each, with a data key
#: that proves the payload is the verb's own and not a vacuous ``{}``.
READ_VERBS = (
    ("detect", ("detect", "--json"), ("detectors", "window_days")),
    ("rank", ("rank", "--json"), ("clusters", "coverage")),
    ("explain --dry-run", ("explain", "--dry-run", "--json"), ("clusters", "prompt")),
    ("rules", ("rules", "--json"), ("rules", "summary")),
    ("trend", ("trend", "--json"), ("findings", "summary")),
    ("measure", ("measure", "--json"), ("measurements",)),
    ("prune", ("prune", "--json"), ("retention_days",)),
    ("lessons", ("lessons", "--json"), ("lessons",)),
    ("brief", ("brief", "{target}", "--json"), ("target", "open_clusters")),
    ("status", ("status", "--json"), ("stages",)),
    ("digest", ("digest", "--json"), ("observations", "detectors")),
)

#: (argv, message substring) pairs that must be usage errors (exit 2).
USAGE_CASES = (
    (("--json",), "the following arguments are required: command"),
    (("nope", "--json"), "invalid choice"),
    (("detect", "--json", "--nope"), "unrecognized arguments: --nope"),
    (("ingest", "--json", "--limit", "0"), "--limit must be at least 1"),
    (("ingest", "--json", "--settle", "nope"), "invalid duration"),
    (("digest", "--json", "--week", "not-a-week"), "invalid ISO week"),
    (("accept", "--json"), "the following arguments are required: ID"),
    (("apply", "L-00000001", "--json"), "the following arguments are required: --layer"),
    (("apply", "L-00000001", "--layer", "environment", "--json"), "--bead is required"),
    (("dismiss", "D-01:x", "--json"), "the following arguments are required: --reason"),
    (("brief", "--json"), "the following arguments are required: REPO_OR_LAUNCH_DIR"),
    (("rank", "--json", "--top", "0"), "--top must be a positive integer"),
    (("explain", "--json", "--top", "0"), "--top must be a positive integer"),
    (("trend", "--json", "--weeks", "0"), "--weeks must be a positive integer"),
    (("rules", "--json", "--unread-days", "0"), "--unread-days must be a positive integer"),
    (("detect", "--json", "--detector", "D-99"), "unknown detector id"),
    (("detect", "--json", "--window", "12h"), "whole number of days"),
    (("unapply", "--json"), "the following arguments are required: ID"),
    (("lessons", "--json", "--state", "bogus"), "invalid choice"),
    (("measure", "--json", "--nope"), "unrecognized arguments: --nope"),
    (("prune", "--json", "--older-than", "nope"), "invalid duration"),
    (("publish", "--json", "--nope"), "unrecognized arguments: --nope"),
    (("doctor", "--json", "--settle", "nope"), "invalid duration"),
    (("status", "--json", "--nope"), "unrecognized arguments: --nope"),
)


def run_cli(home, *args, timeout=120.0):
    """Run the real CLI executable with HOME redirected to a temp root."""

    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        env={**os.environ, "HOME": str(home)},
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def install_config(home, text=None):
    """Write (or, with None, remove) the operator config under a temp HOME."""

    config_dir = Path(home) / ".config" / "twill"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.toml"
    if text is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(text)


def valid_config_text(home):
    home = Path(home)
    return f'artifacts_root = "{home}/artifacts"\n'


class ConformanceTestCase(unittest.TestCase):
    """Assertions shared by every conformance class below."""

    def make_home(self, config_text="valid"):
        """A temp HOME with an artifacts root and (by default) a valid config."""

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name)
        (home / "artifacts").mkdir()
        install_config(
            home,
            valid_config_text(home) if config_text == "valid" else config_text,
        )
        return home

    def run_main(self, home, *args):
        """Run one command in-process when its external boundary is mocked."""

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = twill_app.main(args)
        return SimpleNamespace(
            returncode=code,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )

    def assert_success_envelope(self, result):
        """Exit 0, one success envelope on stdout, stderr empty (§14)."""

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            len(result.stdout.splitlines()), 1, "a --json verb emits one object"
        )
        envelope = json.loads(result.stdout)
        self.assertEqual(set(envelope), SUCCESS_KEYS)
        self.assertEqual(envelope["schema_version"], 1)
        self.assertIsInstance(envelope["data"], dict)
        self.assertIsInstance(envelope["warnings"], list)
        self.assertTrue(
            all(isinstance(warning, str) for warning in envelope["warnings"])
        )
        self.assertTrue(envelope["generated_at"].endswith("Z"))
        stamp = datetime.fromisoformat(
            envelope["generated_at"].replace("Z", "+00:00")
        )
        self.assertEqual(stamp.utcoffset(), timedelta(0))
        return envelope

    def assert_doctor_envelope(self, result, status):
        """Doctor always emits a success envelope; the exit code is its verdict.

        Unlike every other verb, a non-zero doctor exit is not an error: the
        payload is the report, and the process exit code is the report's own
        ``exit_code`` field (§14 + the doctor contract).
        """

        self.assertEqual(result.stderr, "")
        self.assertEqual(
            len(result.stdout.splitlines()), 1, "a --json verb emits one object"
        )
        envelope = json.loads(result.stdout)
        self.assertEqual(set(envelope), SUCCESS_KEYS)
        data = envelope["data"]
        self.assertEqual(data["status"], status)
        self.assertEqual(data["exit_code"], DOCTOR_EXIT_BY_STATUS[status])
        self.assertEqual(result.returncode, data["exit_code"])
        self.assertTrue(data["checks"])
        return envelope

    def assert_error_envelope(self, result, code):
        """Exit == code, one error envelope on stdout, stderr empty (§14)."""

        self.assertNotEqual(code, 0)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            len(result.stdout.splitlines()), 1, "a --json error is one object"
        )
        envelope = json.loads(result.stdout)
        self.assertEqual(set(envelope), ERROR_KEYS)
        error = envelope["error"]
        self.assertEqual(set(error), {"code", "message", "hint"})
        self.assertEqual(error["code"], code)
        self.assertTrue(error["message"])
        self.assertIsInstance(error["hint"], str)
        return error

    def assert_human_error(self, result, code):
        """Exit == code, prose error on stderr, stdout untouched (§14)."""

        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.startswith("twill: error:"), result.stderr)

    def assert_human_success(self, result):
        """Exit 0 with human text on stdout that is not a JSON envelope."""

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout)
        self.assertFalse(result.stdout.startswith("{"))
        return result.stdout


class SuccessEnvelopeTests(ConformanceTestCase):
    """Successful verbs over a primed pipeline state."""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.home = Path(cls._directory.name)
        (cls.home / "artifacts").mkdir()
        install_config(cls.home, valid_config_text(cls.home))
        cls.state = cls.home / "state"
        for args in (
            ("ingest", "--json", "--file", str(FIXTURE), "--settle", "0"),
            ("detect", "--json"),
            ("rank", "--json"),
        ):
            result = run_cli(cls.home, *args, "--state-dir", str(cls.state))
            assert result.returncode == 0, result.stderr or result.stdout

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_ingest_success_envelope(self):
        result = run_cli(
            self.home,
            "ingest",
            "--json",
            "--file",
            str(FIXTURE),
            "--settle",
            "0",
            "--state-dir",
            str(self.state),
        )
        envelope = self.assert_success_envelope(result)
        self.assertLessEqual(
            set(("sessions", "events", "observations", "performance")),
            set(envelope["data"]),
        )

    def test_publish_success_envelope(self):
        home = self.make_home()
        published = PublicationResult(
            commit="a" * 40,
            branch="main",
            changed_paths=("manifest.json", "digests/2025-W01.txt"),
            created_commit=True,
            pushed=True,
        )
        with mock.patch.object(
            twill_app.twill_publisher,
            "publish_snapshot",
            return_value=published,
        ):
            result = self.run_main(
                home,
                "publish",
                "--json",
                "--state-dir",
                str(Path(home) / "state"),
            )
        # The publisher's git transport is exercised by
        # test_artifact_publisher; this assertion isolates the public CLI
        # envelope from child-process policy while running the real handler.
        envelope = self.assert_success_envelope(result)
        self.assertEqual(envelope["data"]["branch"], "main")
        self.assertTrue(envelope["data"]["created_commit"])
        self.assertTrue(envelope["data"]["pushed"])
        self.assertEqual(
            set(envelope["data"]["changed_paths"]),
            {"manifest.json", "digests/2025-W01.txt"},
        )

    def test_every_read_verb_emits_one_success_envelope(self):
        for label, args, data_keys in READ_VERBS:
            with self.subTest(verb=label):
                result = run_cli(
                    self.home,
                    *(arg.replace("{target}", str(self.home)) for arg in args),
                    "--state-dir",
                    str(self.state),
                )
                envelope = self.assert_success_envelope(result)
                for key in data_keys:
                    self.assertIn(key, envelope["data"])

    def test_doctor_envelope_reports_its_own_exit_code(self):
        result = run_cli(self.home, "doctor", "--json", "--state-dir", str(self.state))
        # A fully primed, freshly run pipeline is healthy: exit 0.  The
        # non-zero verdicts are exercised in DoctorHealthTests.
        self.assert_doctor_envelope(result, "healthy")

    def test_digest_stdout_human_mode_is_text(self):
        result = run_cli(
            self.home, "digest", "--stdout", "--state-dir", str(self.state)
        )
        output = self.assert_human_success(result)
        self.assertTrue(output.startswith("TWILL digest"))

    def test_digest_file_mode_writes_the_artifact_and_names_it(self):
        result = run_cli(self.home, "digest", "--state-dir", str(self.state))
        output = self.assert_human_success(result)
        self.assertIn("written to", output)
        written = [path for path in output.split() if path.startswith(str(self.home))]
        self.assertTrue(written, output)
        self.assertTrue(Path(written[0]).is_file(), output)

    def test_human_warnings_go_to_stderr_not_stdout(self):
        # A fresh state has no successful stage, so status always warns:
        # JSON mode must carry the warning inside the envelope with an empty
        # stderr, the human mode must print it as a warning line on stderr.
        state = self.home / "warning-state"
        json_result = run_cli(self.home, "status", "--json", "--state-dir", str(state))
        envelope = self.assert_success_envelope(json_result)
        self.assertTrue(envelope["warnings"])
        human_result = run_cli(self.home, "status", "--state-dir", str(state))
        self.assert_human_success(human_result)
        self.assertTrue(human_result.stderr.startswith("warning:"), human_result.stderr)

    def test_help_exits_zero(self):
        result = run_cli(self.home, "doctor", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout)
        self.assertEqual(result.stderr, "")


class LessonVerbTests(ConformanceTestCase):
    """accept/apply/un-apply and dismiss on real operator actions."""

    def write_draft(self, home, key="command-not-found:cli"):
        artifacts = Path(home) / "artifacts"
        cluster = RankedCluster(
            "D-01",
            key,
            30,
            3,
            7,
            "2026-09-01T00:00:00+00:00",
            "2026-09-24T00:00:00+00:00",
            1.0,
            None,
            "open",
        )
        candidate = PromptCluster(
            cluster,
            (PromptExcerpt(1, "session-cli", "safe evidence"),),
        )
        path = write_lesson_files(
            (
                LessonDraft(
                    f"D-01:{key}",
                    "A command fails repeatedly. Install the command before retrying.",
                ),
            ),
            (candidate,),
            TwillConfig(artifacts_root=artifacts),
        )[0]
        path.write_text(path.read_text() + "\nOperator body for the owner repo.\n")
        return path.stem

    def test_accept_apply_unapply_lifecycle_envelopes(self):
        home = self.make_home()
        state = Path(home) / "state"
        lesson_id = self.write_draft(home)

        accepted = run_cli(
            home,
            "accept",
            lesson_id,
            "--json",
            "--state-dir",
            str(state),
        )
        envelope = self.assert_success_envelope(accepted)
        self.assertEqual(envelope["data"]["lesson"]["state"], "accepted")

        applied = run_cli(
            home,
            "apply",
            lesson_id,
            "--layer",
            "environment",
            "--bead",
            "twill-x",
            "--json",
            "--state-dir",
            str(state),
        )
        envelope = self.assert_success_envelope(applied)
        self.assertEqual(envelope["data"]["lesson"]["state"], "applied:environment")
        self.assertTrue(envelope["data"]["bead_create_command"].startswith("bead create"))

        unapplied = run_cli(
            home,
            "un-apply",
            lesson_id,
            "--json",
            "--state-dir",
            str(state),
        )
        envelope = self.assert_success_envelope(unapplied)
        self.assertEqual(envelope["data"]["lesson"]["state"], "accepted")

    def test_apply_emits_environment_skill_and_retrieval_outcomes(self):
        home = self.make_home()
        state = Path(home) / "state-all-layers"
        cases = (
            ("command-not-found:environment", "environment", "twill-env"),
            ("command-not-found:skill", "skill", "twill-skill"),
            ("recurrence:retrieval", "retrieval_only", None),
        )
        for key, layer, bead in cases:
            with self.subTest(layer=layer):
                lesson_id = self.write_draft(home, key)
                accepted = run_cli(
                    home, "accept", lesson_id, "--json", "--state-dir", str(state)
                )
                self.assert_success_envelope(accepted)
                args = [
                    "apply",
                    lesson_id,
                    "--layer",
                    layer,
                    "--emit-guard",
                    "--json",
                    "--state-dir",
                    str(state),
                ]
                if bead is not None:
                    args[4:4] = ["--bead", bead]
                applied = run_cli(home, *args)
                envelope = self.assert_success_envelope(applied)
                data = envelope["data"]
                self.assertEqual(data["direct_change"], bead is not None)
                self.assertIn(f"guards/{lesson_id}.", data["guard_artifact"])
                artifact = home / "artifacts" / data["guard_artifact"]
                if bead is None:
                    self.assertNotIn("bead_create_command", data)
                    self.assertIn("direct_change: false", artifact.read_text())
                else:
                    self.assertTrue(data["bead_create_command"].startswith("bead create"))

    def test_dismiss_known_cluster_envelope(self):
        home = self.make_home()
        state = Path(home) / "state"
        connection = twill_schema.connect(state)
        connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score, covered_by, state) "
            "VALUES ('D-01', 'command-not-found:sqlite3', 30, 3, 7, ?, ?, 1.0, NULL, 'open')",
            ("2026-09-01T00:00:00+00:00", "2026-09-24T00:00:00+00:00"),
        )
        connection.commit()
        connection.close()

        result = run_cli(
            home,
            "dismiss",
            "D-01:command-not-found:sqlite3",
            "--reason",
            "not actionable for this environment",
            "--json",
            "--state-dir",
            str(state),
        )
        envelope = self.assert_success_envelope(result)
        self.assertEqual(envelope["data"]["cluster"]["state"], "dismissed")
        self.assertEqual(
            envelope["data"]["cluster"]["reason"],
            "not actionable for this environment",
        )


class InvalidConfigTests(ConformanceTestCase):
    """Wrong operator state is a startup error: exit 1 before any file opens."""

    def run_config_verb(self, home, args):
        state = Path(home) / "never-created"
        result = run_cli(home, *args, "--json", "--state-dir", str(state))
        return result, state

    def assert_no_partial_commit(self, state):
        """§14 runtime errors leave no committed state behind.

        main() takes the state lock for mutating verbs before the handler
        loads the config (EC-10), and the lock creates the state directory
        with its ``lock`` file — that is lock machinery, not state.  The
        database is what a partial commit would leave, and it must not exist.
        """

        self.assertFalse(
            (state / "twill.db").exists(), "no state database may be created"
        )

    def test_malformed_toml_is_a_runtime_error_in_every_config_verb(self):
        for label, args in CONFIG_LOADING_VERBS:
            with self.subTest(verb=label):
                home = self.make_home()
                install_config(home, 'artifacts_root = "')
                result, state = self.run_config_verb(
                    home,
                    tuple(arg.replace("{target}", str(home)) for arg in args),
                )
                error = self.assert_error_envelope(result, 1)
                self.assertIn("invalid TOML", error["message"])
                self.assert_no_partial_commit(state)

    def test_missing_artifacts_root_is_a_startup_error_in_every_config_verb(self):
        for label, args in CONFIG_LOADING_VERBS:
            with self.subTest(verb=label):
                home = self.make_home()
                install_config(home, "# present, but artifacts_root is unset\n")
                result, state = self.run_config_verb(
                    home,
                    tuple(arg.replace("{target}", str(home)) for arg in args),
                )
                error = self.assert_error_envelope(result, 1)
                self.assertIn("artifacts_root is not set", error["message"])
                self.assert_no_partial_commit(state)

    def test_artifacts_root_inside_the_repository_is_refused(self):
        home = self.make_home()
        install_config(home, f'artifacts_root = "{ROOT}"\n')
        result = run_cli(home, "lessons", "--json")
        error = self.assert_error_envelope(result, 1)
        self.assertIn("inside the TWILL repository tree", error["message"])
        self.assertIn("public", error["hint"])

    def test_unknown_config_key_is_a_runtime_error(self):
        home = self.make_home()
        install_config(home, valid_config_text(home) + 'no_such_key = true\n')
        result = run_cli(home, "detect", "--json")
        error = self.assert_error_envelope(result, 1)
        self.assertIn("unknown key", error["message"])

    def test_human_config_error_is_stderr_only(self):
        home = self.make_home()
        install_config(home, 'artifacts_root = "')
        result = run_cli(home, "detect")
        self.assert_human_error(result, 1)
        self.assertIn("invalid TOML", result.stderr)

    def test_doctor_fails_closed_on_a_present_but_invalid_config(self):
        home = self.make_home()
        install_config(home, 'artifacts_root = "')
        result = run_cli(home, "doctor", "--json")
        error = self.assert_error_envelope(result, 1)
        self.assertIn("invalid TOML", error["message"])

    def test_doctor_tolerates_an_absent_config_as_first_install(self):
        home = self.make_home(config_text=None)
        result = run_cli(home, "doctor", "--json")
        # No config and no state: doctor still reports (broken: nothing has
        # ever run) rather than refusing with a startup error — the absent
        # config is a first install, not operator mistakes.
        self.assert_doctor_envelope(result, "broken")

    def test_digest_read_modes_survive_bad_config_file_mode_fails_closed(self):
        home = self.make_home()
        install_config(home, 'artifacts_root = "')
        reading = run_cli(home, "digest", "--json")
        self.assert_success_envelope(reading)
        streaming = run_cli(home, "digest", "--stdout")
        self.assert_human_success(streaming)
        writing = run_cli(home, "digest")
        self.assert_human_error(writing, 1)

    def test_config_free_read_verbs_need_no_config(self):
        home = self.make_home(config_text=None)
        for verb in ("status", "rules", "trend"):
            with self.subTest(verb=verb):
                result = run_cli(home, verb, "--json")
                self.assert_success_envelope(result)


class LockContentionTests(ConformanceTestCase):
    """A held state lock exits 3 with the documented owner message."""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.home = Path(cls._directory.name)
        (cls.home / "artifacts").mkdir()
        install_config(cls.home, valid_config_text(cls.home))
        cls.state = cls.home / "state"
        # explain --dry-run reads the database read-only, so it needs one to
        # exist before the under-the-lock assertions below.
        primed = run_cli(cls.home, "detect", "--json", "--state-dir", str(cls.state))
        assert primed.returncode == 0, primed.stderr or primed.stdout

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_lock_taking_verbs_exit_three_with_the_owner_in_the_envelope(self):
        with StateLock(self.state):
            for label, args in LOCK_TAKING_VERBS:
                with self.subTest(verb=label):
                    result = run_cli(self.home, *args, "--state-dir", str(self.state))
                    error = self.assert_error_envelope(result, 3)
                    self.assertRegex(
                        error["message"], rf"^lock held by pid {os.getpid()} since \S"
                    )
                    self.assertTrue(error["hint"])

    def test_file_writing_digest_takes_the_lock_and_reports_it_in_prose(self):
        # --json is exactly what makes digest read-only, so the lock error
        # can only be observed on the human surface here.
        with StateLock(self.state):
            result = run_cli(self.home, "digest", "--state-dir", str(self.state))
            self.assert_human_error(result, 3)
            self.assertIn(f"pid {os.getpid()}", result.stderr)

    def test_read_verbs_are_not_blocked_by_the_lock(self):
        with StateLock(self.state):
            for args in (
                ("status", "--json"),
                ("rules", "--json"),
                ("trend", "--json"),
                ("digest", "--json"),
                ("explain", "--dry-run", "--json"),
            ):
                with self.subTest(verb=args[0]):
                    result = run_cli(self.home, *args, "--state-dir", str(self.state))
                    self.assert_success_envelope(result)


class DoctorHealthTests(ConformanceTestCase):
    """Unhealthy doctor checks change the exit code, not the envelope shape."""

    def test_missing_state_is_broken_and_exits_two(self):
        home = self.make_home(config_text=None)
        result = run_cli(home, "doctor", "--json")
        envelope = self.assert_doctor_envelope(result, "broken")
        self.assertTrue(envelope["warnings"])

    def test_database_without_any_run_is_degraded_and_exits_one(self):
        home = self.make_home()
        state = Path(home) / "state"
        twill_schema.connect(state).close()
        result = run_cli(home, "doctor", "--json", "--state-dir", str(state))
        self.assert_doctor_envelope(result, "degraded")

    def test_human_doctor_prints_checks_on_stdout_and_warnings_on_stderr(self):
        home = self.make_home(config_text=None)
        result = run_cli(home, "doctor")
        self.assertEqual(result.returncode, 2)
        self.assertIn("TWILL doctor", result.stdout)
        self.assertIn("status: broken", result.stdout)
        self.assertIn("- db_integrity:", result.stdout)
        self.assertTrue(result.stderr.startswith("warning:"), result.stderr)


class DetectorFailureTests(ConformanceTestCase):
    """Detector refusals: exit 4, named detector, others still committed."""

    def setUp(self):
        self.home = self.make_home()
        self.state = Path(self.home) / "state"
        healthy = run_cli(self.home, "detect", "--json", "--state-dir", str(self.state))
        self.assert_success_envelope(healthy)
        # Simulate EC-12 drift: the registry's semantics no longer match the
        # sha this database stamped for D-01 during the healthy run above.
        connection = twill_schema.connect(self.state)
        try:
            cursor = connection.execute(
                "UPDATE detector_run SET semantics_sha = ? WHERE detector_id = ?",
                ("0" * 64, "D-01"),
            )
            self.assertEqual(cursor.rowcount, 1, "D-01 must have stamped a run")
            connection.commit()
        finally:
            connection.close()

    def test_semantics_drift_is_a_validation_failure(self):
        result = run_cli(self.home, "detect", "--json", "--state-dir", str(self.state))
        error = self.assert_error_envelope(result, 4)
        self.assertIn("detector(s) failed", error["message"])
        self.assertIn("D-01@1", error["message"])
        self.assertIn("semantics changed without a version bump", error["message"])
        self.assertIn("the rest ran", error["message"])
        self.assertIn("version", error["hint"])

    def test_isolated_run_names_only_the_refused_detector(self):
        result = run_cli(
            self.home,
            "detect",
            "--json",
            "--detector",
            "D-01",
            "--state-dir",
            str(self.state),
        )
        error = self.assert_error_envelope(result, 4)
        self.assertIn("1 of 1 detector(s) failed", error["message"])
        self.assertIn("D-01@1", error["message"])

    def test_human_refusal_is_reported_on_stderr(self):
        result = run_cli(self.home, "detect", "--state-dir", str(self.state))
        self.assert_human_error(result, 4)
        self.assertIn("semantics changed without a version bump", result.stderr)
        self.assertIn("hint:", result.stderr)

    def test_unknown_detector_id_is_a_usage_error(self):
        result = run_cli(self.home, "detect", "--json", "--detector", "D-99")
        error = self.assert_error_envelope(result, 2)
        self.assertIn("unknown detector id", error["message"])

    def test_sub_day_window_is_a_usage_error(self):
        result = run_cli(self.home, "detect", "--json", "--window", "12h")
        error = self.assert_error_envelope(result, 2)
        self.assertIn("whole number of days", error["message"])


class UsageErrorTests(ConformanceTestCase):
    """Exit 2 for every misuse of a verb's flags, envelope in JSON mode."""

    def test_usage_errors_exit_two_with_the_error_envelope(self):
        home = self.make_home()
        for args, fragment in USAGE_CASES:
            with self.subTest(args=" ".join(args)):
                result = run_cli(home, *args)
                error = self.assert_error_envelope(result, 2)
                self.assertIn(fragment, error["message"])
                # §14 requires the hint key on every error envelope; a few
                # checks (``--limit``) legitimately raise without one, so
                # only its presence and type are universal here.

    def test_parse_level_usage_errors_precede_any_config_read(self):
        home = self.make_home(config_text=None)
        for args, fragment in (
            (("nope", "--json"), "invalid choice"),
            (("detect", "--json", "--nope"), "unrecognized arguments"),
            (("ingest", "--json", "--settle", "xyz"), "invalid duration"),
        ):
            with self.subTest(args=" ".join(args)):
                result = run_cli(home, *args)
                error = self.assert_error_envelope(result, 2)
                self.assertIn(fragment, error["message"])

    def test_human_usage_error_is_stderr_only(self):
        home = self.make_home()
        result = run_cli(home, "rank", "--top", "0")
        self.assert_human_error(result, 2)
        self.assertIn("--top must be a positive integer", result.stderr)
        self.assertIn("hint:", result.stderr)


class DismissValidationTests(ConformanceTestCase):
    """Exit 4: dismiss validates its cluster id against real state."""

    def setUp(self):
        self.home = self.make_home()
        self.state = Path(self.home) / "state"
        connection = twill_schema.connect(self.state)
        connection.execute(
            "INSERT INTO cluster(detector_id, key, window_days, sessions, events, "
            "first_seen, last_seen, score, covered_by, state) "
            "VALUES ('D-01', 'command-not-found:sqlite3', 30, 3, 7, ?, ?, 1.0, NULL, 'open')",
            ("2026-09-01T00:00:00+00:00", "2026-09-24T00:00:00+00:00"),
        )
        connection.commit()
        connection.close()

    def test_unknown_cluster_is_a_validation_failure(self):
        result = run_cli(
            self.home,
            "dismiss",
            "D-99:command-not-found:nope",
            "--reason",
            "audited",
            "--json",
            "--state-dir",
            str(self.state),
        )
        error = self.assert_error_envelope(result, 4)
        self.assertIn("cluster not found", error["message"])

    def test_malformed_cluster_id_is_a_validation_failure(self):
        result = run_cli(
            self.home,
            "dismiss",
            "garbage",
            "--reason",
            "audited",
            "--json",
            "--state-dir",
            str(self.state),
        )
        error = self.assert_error_envelope(result, 4)
        self.assertIn("must have the form D-01:key", error["message"])
        self.assertIn("twill rank --json", error["hint"])


if __name__ == "__main__":
    unittest.main()
