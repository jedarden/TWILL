import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

from twill_config import (  # noqa: E402
    DEFAULT_CONTENT_FENCES,
    DEFAULT_MODEL,
    DEFAULT_RETENTION_SECONDS,
    DEFAULT_SETTLE_WINDOW_SECONDS,
    DEFAULT_SOURCE_GLOBS,
    DEFAULT_TOP_K,
    ConfigError,
    TwillConfig,
    load_config,
    parse_duration,
)


def write_config(home: Path, body: str) -> Path:
    config_dir = home / ".config" / "twill"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = config_dir / "config.toml"
    config.write_text(body)
    return config


def missing_path(directory: Path) -> Path:
    return directory / "does" / "not" / "exist.toml"


class DefaultValueTests(unittest.TestCase):
    def test_every_key_but_artifacts_root_defaults_when_the_file_omits_it(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory) / "artifacts"
            config = load_config(
                write_config(Path(directory), f'artifacts_root = "{artifacts}"\n'),
                repo_root=Path(directory) / "repo",
            )
            self.assertEqual(config.settle_window, DEFAULT_SETTLE_WINDOW_SECONDS)
            self.assertEqual(config.settle_window, 7200.0)
            self.assertEqual(config.retention, DEFAULT_RETENTION_SECONDS)
            self.assertEqual(config.retention, 180 * 86400.0)
            self.assertEqual(config.top_k, DEFAULT_TOP_K)
            self.assertEqual(config.top_k, 10)
            self.assertEqual(config.source_globs, DEFAULT_SOURCE_GLOBS)
            self.assertEqual(
                config.source_globs,
                (
                    "~/.claude/projects/**/*.jsonl",
                    "~/.codex/sessions/**/*.jsonl",
                ),
            )
            self.assertEqual(config.content_fences, DEFAULT_CONTENT_FENCES)
            self.assertEqual(config.content_fences, ())
            self.assertEqual(config.model, DEFAULT_MODEL)
            self.assertEqual(config.model, "claude-haiku-4-5")
            self.assertEqual(config.artifacts_root, artifacts.resolve())


class UnsetArtifactsRootTests(unittest.TestCase):
    """Unset is the same startup error as in-tree (plan §3, §13.1)."""

    def test_missing_config_file_is_a_load_error_because_artifacts_root_has_no_default(self):
        with tempfile.TemporaryDirectory() as directory:
            # Explicit missing path: the suite must not read a real
            # ~/.config/twill/config.toml if the host happens to have one.
            with self.assertRaises(ConfigError) as caught:
                load_config(missing_path(Path(directory)), repo_root=Path(directory))
            self.assertIn("artifacts_root", caught.exception.message)
            self.assertIn("no default", caught.exception.message)
            self.assertTrue(caught.exception.hint)

    def test_present_config_omitting_artifacts_root_is_a_load_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError) as caught:
                load_config(
                    write_config(Path(directory), 'settle_window = "30m"\n'),
                    repo_root=Path(directory),
                )
            self.assertIn("artifacts_root", caught.exception.message)
            self.assertIn("no default", caught.exception.message)
            self.assertTrue(caught.exception.hint)

    def test_config_built_in_code_without_a_destination_is_refused_at_the_gate(self):
        # The field is required and typed Path, so None can only arrive by
        # constructing the dataclass directly; the writer gate still refuses
        # it rather than picking a destination of its own.
        config = TwillConfig(artifacts_root=None)
        with self.assertRaises(ConfigError) as caught:
            config.require_artifacts_root(Path("/repo"))
        self.assertIn("no default", caught.exception.message)


class LoadedValueTests(unittest.TestCase):
    def test_every_key_is_honored_when_set(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = Path(directory) / "repo"
            repo.mkdir()
            artifacts = Path(directory) / "artifacts"
            write_config(
                home,
                "\n".join(
                    [
                        'settle_window = "30m"',
                        'retention = "90d"',
                        "top_k = 5",
                        'source_globs = ["~/transcripts/**/*.jsonl"]',
                        'content_fences = ["a fenced entity"]',
                        'model = "claude-opus-5"',
                        f'artifacts_root = "{artifacts}"',
                    ]
                ),
            )
            config = load_config(home / ".config" / "twill" / "config.toml", repo_root=repo)
            self.assertEqual(config.settle_window, 1800.0)
            self.assertEqual(config.retention, 90 * 86400.0)
            self.assertEqual(config.top_k, 5)
            self.assertEqual(config.source_globs, ("~/transcripts/**/*.jsonl",))
            self.assertEqual(config.content_fences, ("a fenced entity",))
            self.assertEqual(config.model, "claude-opus-5")
            self.assertEqual(config.artifacts_root, artifacts.resolve())
            self.assertEqual(config.require_artifacts_root(repo), artifacts.resolve())

    def test_bare_seconds_are_accepted_for_durations(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(
                write_config(
                    Path(directory),
                    "settle_window = 7200\nretention = 15552000\n"
                    f'artifacts_root = "{Path(directory) / "artifacts"}"\n',
                ),
                repo_root=Path(directory) / "repo",
            )
            self.assertEqual(config.settle_window, 7200.0)
            self.assertEqual(config.retention, 15552000.0)

    def test_sibling_directory_with_a_prefix_name_is_outside_the_tree(self):
        # guards against a string-prefix containment check: repo-lessons is a
        # string prefix of nothing here, but sits beside the repo, and a naive
        # startswith() on the repo path would misjudge exactly this shape.
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "twill"
            artifacts = Path(directory) / "twill-lessons"
            config = load_config(
                write_config(Path(directory), f'artifacts_root = "{artifacts}"\n'),
                repo_root=repo,
            )
            self.assertEqual(config.artifacts_root, artifacts.resolve())


class ContainmentRejectionTests(unittest.TestCase):
    def test_artifacts_root_equal_to_the_repo_tree_is_a_load_error(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            with self.assertRaises(ConfigError) as caught:
                load_config(
                    write_config(Path(directory), f'artifacts_root = "{repo}"\n'),
                    repo_root=repo,
                )
            self.assertIn("inside the TWILL repository tree", caught.exception.message)

    def test_artifacts_root_under_the_repo_tree_is_a_load_error(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            inside = repo / "lessons"
            with self.assertRaises(ConfigError) as caught:
                load_config(
                    write_config(Path(directory), f'artifacts_root = "{inside}"\n'),
                    repo_root=repo,
                )
            self.assertIn("inside the TWILL repository tree", caught.exception.message)

    def test_require_artifacts_root_rechecks_a_directly_built_config(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            config = TwillConfig(artifacts_root=repo / "lessons")
            with self.assertRaises(ConfigError):
                config.require_artifacts_root(repo)


class RejectionTests(unittest.TestCase):
    def test_invalid_toml_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError) as caught:
                load_config(
                    write_config(Path(directory), "settle_window =\n"),
                    repo_root=Path(directory),
                )
            self.assertIn("invalid TOML", caught.exception.message)

    def test_unknown_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError) as caught:
                load_config(
                    write_config(Path(directory), "setle_window = 5\n"),
                    repo_root=Path(directory),
                )
            self.assertIn("unknown key", caught.exception.message)
            self.assertIn("setle_window", caught.exception.message)

    def test_bad_duration_string_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError) as caught:
                load_config(
                    write_config(Path(directory), 'settle_window = "2x"\n'),
                    repo_root=Path(directory),
                )
            self.assertIn("settle_window", caught.exception.message)

    def test_negative_duration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                load_config(
                    write_config(Path(directory), "settle_window = -5\n"),
                    repo_root=Path(directory),
                )

    def test_boolean_duration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                load_config(
                    write_config(Path(directory), "retention = true\n"),
                    repo_root=Path(directory),
                )

    def test_top_k_must_be_a_positive_integer(self):
        for body in ("top_k = 0\n", 'top_k = "ten"\n', "top_k = true\n", "top_k = 1.5\n"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ConfigError):
                    load_config(write_config(Path(directory), body), repo_root=Path(directory))

    def test_source_globs_must_be_a_list_of_non_empty_strings(self):
        for body in (
            'source_globs = "~/x/**/*.jsonl"\n',
            "source_globs = [42]\n",
            'source_globs = [""]\n',
        ):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ConfigError):
                    load_config(write_config(Path(directory), body), repo_root=Path(directory))

    def test_empty_source_globs_are_a_valid_explicit_choice(self):
        # Nothing to walk is a working (if quiet) configuration: ingest
        # reports "no settled sessions" rather than failing to load.
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(
                write_config(
                    Path(directory),
                    "source_globs = []\n"
                    f'artifacts_root = "{Path(directory) / "artifacts"}"\n',
                ),
                repo_root=Path(directory) / "repo",
            )
            self.assertEqual(config.source_globs, ())

    def test_content_fences_must_be_a_list_of_non_empty_strings(self):
        for body in (
            'content_fences = "a name"\n',
            "content_fences = [7]\n",
        ):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ConfigError):
                    load_config(write_config(Path(directory), body), repo_root=Path(directory))

    def test_model_must_be_a_non_empty_string(self):
        for body in ("model = 5\n", 'model = ""\n'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ConfigError):
                    load_config(write_config(Path(directory), body), repo_root=Path(directory))

    def test_artifacts_root_must_be_a_path_string(self):
        for body in ("artifacts_root = 5\n", 'artifacts_root = ""\n'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ConfigError):
                    load_config(write_config(Path(directory), body), repo_root=Path(directory))


class ParseDurationTests(unittest.TestCase):
    def test_accepted_forms(self):
        for value, expected in [
            ("0", 0.0),
            ("90", 90.0),
            ("45s", 45.0),
            ("30m", 1800.0),
            ("2h", 7200.0),
            ("180d", 15552000.0),
            ("1.5h", 5400.0),
            ("2H", 7200.0),
            (7200, 7200.0),
        ]:
            with self.subTest(value=value):
                self.assertEqual(parse_duration(value), expected)

    def test_rejected_forms(self):
        for value in ["2x", "h", "", "1y", -1, True, None, []]:
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    parse_duration(value)


class ResolutionOrderTests(unittest.TestCase):
    """CLI flag > TWILL_SOURCE_ROOTS env > config globs (plan §13.1)."""

    def test_env_override_wins_over_config_globs(self):
        with mock.patch.dict(os.environ, {"TWILL_SOURCE_ROOTS": "/env/first"}):
            from twill_app import _source_roots

            config = TwillConfig(
                artifacts_root=Path("/elsewhere/twill-lessons"),
                source_globs=("/config/second/**/*.jsonl",),
            )
            self.assertEqual(_source_roots(None, config), (Path("/env/first"),))

    def test_cli_flag_wins_over_env_and_config(self):
        with mock.patch.dict(os.environ, {"TWILL_SOURCE_ROOTS": "/env/first"}):
            from twill_app import _source_roots

            config = TwillConfig(
                artifacts_root=Path("/elsewhere/twill-lessons"),
                source_globs=("/config/second/**/*.jsonl",),
            )
            roots = _source_roots(["/cli/flag"], config)
            self.assertEqual(roots, (Path("/cli/flag"),))

    def test_glob_literal_prefix_becomes_the_walker_root(self):
        from twill_app import _glob_roots

        roots = _glob_roots(("~/.claude/projects/**/*.jsonl", "~/x/words/*.jsonl", "~/a/**/*.jsonl"))
        self.assertEqual(
            roots,
            (
                Path.home() / ".claude" / "projects",
                Path.home() / "x" / "words",
                Path.home() / "a",
            ),
        )


class CliWiringTests(unittest.TestCase):
    """The CLI reads settle window and source globs from config.toml alone."""

    def run_cli(self, *args, home):
        env = dict(os.environ)
        env["HOME"] = str(home)
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            env=env,
            check=False,
            text=True,
            capture_output=True,
        )

    def write_fixture(self, source_dir: Path) -> Path:
        source_dir.mkdir(parents=True, exist_ok=True)
        session = source_dir / "session.jsonl"
        session.write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "config-driven",
                    "timestamp": "2026-09-22T12:00:00Z",
                    "message": {"role": "user", "content": "config sourced session"},
                }
            )
            + "\n"
        )
        return session

    def test_ingest_reads_settle_and_source_globs_from_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            source_dir = root / "sources"
            self.write_fixture(source_dir)
            write_config(
                home,
                "\n".join(
                    [
                        'settle_window = "0"',
                        f'source_globs = ["{source_dir}/**/*.jsonl"]',
                        f'artifacts_root = "{root / "artifacts"}"',
                    ]
                )
                + "\n",
            )
            result = self.run_cli(
                "ingest", "--json", "--limit", "1", "--state-dir", str(root / "state"),
                home=home,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)["data"]
            self.assertEqual(data["sessions"], 1)
            self.assertEqual(data["events"], 1)

    def test_explicit_settle_flag_overrides_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            source_dir = root / "sources"
            self.write_fixture(source_dir)  # freshly written: unsettled under 2h
            write_config(
                home,
                f'settle_window = "2h"\nartifacts_root = "{root / "artifacts"}"\n',
            )
            result = self.run_cli(
                "ingest", "--json", "--settle", "0", "--limit", "1",
                "--source", str(source_dir), "--state-dir", str(root / "state"),
                home=home,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["data"]["sessions"], 1)

    def test_wrong_config_is_a_runtime_error_with_a_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            write_config(home, 'settle_window = "2x"\n')
            result = self.run_cli(
                "ingest", "--json", "--state-dir", str(root / "state"),
                home=home,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            error = json.loads(result.stdout)["error"]
            self.assertEqual(error["code"], 1)
            self.assertIn("settle_window", error["message"])
            self.assertTrue(error["hint"])

    def test_artifacts_root_inside_the_repo_tree_fails_ingest_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            write_config(home, f'artifacts_root = "{ROOT / "lessons"}"\n')
            result = self.run_cli(
                "ingest", "--json", "--state-dir", str(root / "state"),
                home=home,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            error = json.loads(result.stdout)["error"]
            self.assertEqual(error["code"], 1)
            self.assertIn("artifacts_root", error["message"])

    def test_unset_artifacts_root_fails_ingest_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            write_config(home, 'settle_window = "0"\n')
            result = self.run_cli(
                "ingest", "--json", "--state-dir", str(root / "state"),
                home=home,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            error = json.loads(result.stdout)["error"]
            self.assertEqual(error["code"], 1)
            self.assertIn("artifacts_root", error["message"])
            self.assertIn("no default", error["message"])
            self.assertTrue(error["hint"])

    def test_missing_config_file_fails_ingest_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_cli(
                "ingest", "--json", "--state-dir", str(root / "state"),
                home=root,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            error = json.loads(result.stdout)["error"]
            self.assertIn("artifacts_root", error["message"])
            self.assertIn("no default", error["message"])

    def test_bad_settle_flag_is_still_a_usage_error(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(
                "ingest", "--settle", "2x", "--state-dir", str(Path(directory) / "state"),
                home=Path(directory),
            )
            self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
