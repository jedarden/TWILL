"""`make install` entry point (plan §13.1).

Covers the two things install lays down — the ``~/.local/bin`` symlink and the
config skeleton — plus the refusals that keep it from ever clobbering operator
state.  Every test redirects ``HOME`` at a throwaway directory, so the target
must resolve its paths from ``$(HOME)`` and never from anything global.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKELETON = ROOT / "config.toml.skeleton"
sys.path.insert(0, str(ROOT))

from twill_config import ConfigError, load_config  # noqa: E402


def run_install(home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "-C", str(ROOT), "install"],
        env={**os.environ, "HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )


def run_installed(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(home / ".local" / "bin" / "twill"), *args],
        cwd=home,
        env={**os.environ, "HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class InstallTests(unittest.TestCase):
    def setUp(self):
        self._home_dir = tempfile.TemporaryDirectory()
        self.home = Path(self._home_dir.name)

    def tearDown(self):
        self._home_dir.cleanup()

    def link(self) -> Path:
        return self.home / ".local" / "bin" / "twill"

    def config(self) -> Path:
        return self.home / ".config" / "twill" / "config.toml"

    def test_install_symlinks_twill_and_lays_down_the_skeleton(self):
        result = run_install(self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.link().is_symlink())
        self.assertEqual(os.path.realpath(self.link()), str(ROOT / "twill"))
        self.assertEqual(self.config().read_text(), SKELETON.read_text())
        self.assertEqual(
            self.config().stat().st_mode & 0o777, 0o600, "config holds operator paths"
        )

    def test_installed_entry_point_runs_from_a_foreign_cwd(self):
        self.assertEqual(run_install(self.home).returncode, 0)
        # A verb, not just --help: the symlink must resolve back to this tree
        # for the sibling-module imports, and digest is the read-only verb
        # that touches neither the config gate nor the state directory.
        result = run_installed(self.home, "digest", "--stdout")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TWILL digest", result.stdout)

    def test_skeleton_parses_but_still_gates_on_artifacts_root(self):
        self.assertEqual(run_install(self.home).returncode, 0)
        # Valid TOML (a typo'd skeleton would fail to parse here) that names
        # nothing: load_config raises the real startup error, so the skeleton
        # can never silently supply an artifacts_root the operator never chose.
        with self.assertRaises(ConfigError) as raised:
            load_config(self.config(), repo_root=ROOT)
        self.assertIn("artifacts_root", raised.exception.message)
        # And through the installed entry point: startup aborts, nothing runs.
        result = run_installed(self.home, "ingest", "--limit", "1", "--json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("artifacts_root", result.stdout)

    def test_reinstall_keeps_the_config_and_repairs_a_stale_symlink(self):
        self.assertEqual(run_install(self.home).returncode, 0)
        self.config().write_text("artifacts_root = '~/somewhere-else'\n")
        stale = self.home / "stale-twill-target"
        stale.write_text("# not twill\n")
        self.link().unlink()
        self.link().symlink_to(stale)

        result = run_install(self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("keeping existing", result.stdout)
        self.assertEqual(os.path.realpath(self.link()), str(ROOT / "twill"))
        self.assertIn("somewhere-else", self.config().read_text())

    def test_install_refuses_to_replace_a_real_binary(self):
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        existing = self.link()
        existing.write_text("#!/bin/sh\n# someone else's twill\n")

        result = run_install(self.home)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a symlink", result.stderr)
        self.assertEqual(
            existing.read_text(), "#!/bin/sh\n# someone else's twill\n"
        )
        self.assertFalse(self.config().exists(), "a refused install lays nothing down")


if __name__ == "__main__":
    unittest.main()
