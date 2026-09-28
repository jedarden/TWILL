"""Fresh-install and CLI coverage for private TWILL state permissions."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "twill"


def _permissive_umask() -> None:
    """Make the child prove that TWILL sets modes explicitly."""

    os.umask(0)


class StatePermissionsCliTests(unittest.TestCase):
    def run_install(self, home: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["make", "-C", str(ROOT), "install"],
            env={**os.environ, "HOME": str(home)},
            text=True,
            capture_output=True,
            check=False,
            preexec_fn=_permissive_umask,
        )

    def run_installed(
        self, home: Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(home / ".local" / "bin" / "twill"), *args],
            cwd=home,
            env={**os.environ, "HOME": str(home)},
            text=True,
            capture_output=True,
            check=False,
            preexec_fn=_permissive_umask,
        )

    def assert_private_state_tree(self, state: Path) -> None:
        self.assertTrue(state.is_dir())
        self.assertEqual(state.stat().st_mode & 0o777, 0o700, state)
        for path in sorted(state.rglob("*")):
            self.assertFalse(path.is_symlink(), path)
            mode = path.stat().st_mode & 0o777
            if path.is_dir():
                expected = 0o700
            else:
                self.assertTrue(path.is_file(), path)
                expected = 0o600
            self.assertEqual(mode, expected, path)

    def test_fresh_install_and_first_cli_run_keep_state_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            artifacts = root / "artifacts"

            installed = self.run_install(home)
            self.assertEqual(installed.returncode, 0, installed.stderr)

            config = home / ".config" / "twill" / "config.toml"
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            config.write_text(f"artifacts_root = {json.dumps(str(artifacts))}\n")
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

            source = root / "session.jsonl"
            source.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "permissions-session",
                        "timestamp": "2026-09-20T12:00:00Z",
                        "message": {
                            "role": "user",
                            "content": "permission fixture",
                        },
                    }
                )
                + "\n"
            )

            result = self.run_installed(
                home,
                "ingest",
                "--file",
                str(source),
                "--settle",
                "0",
                "--limit",
                "1",
                "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            state = home / ".local" / "state" / "twill"
            for filename in ("twill.db", "lock", "status.json"):
                self.assertTrue((state / filename).is_file(), filename)
            self.assert_private_state_tree(state)


if __name__ == "__main__":
    unittest.main()
