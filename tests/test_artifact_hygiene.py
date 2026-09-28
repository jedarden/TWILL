"""Repository hygiene checks for TWILL's private artifact directories."""

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIRS = ("lessons", "digests", "measurements", "guards")


class ArtifactDirectoryHygieneTests(unittest.TestCase):
    """Keep private artifact paths out of the published repository."""

    def test_artifact_directories_are_ignored(self):
        """Git must ignore a candidate file below each artifact directory."""

        if (ROOT / ".git").exists():
            for artifact_dir in ARTIFACT_DIRS:
                with self.subTest(artifact_dir=artifact_dir):
                    result = subprocess.run(
                        [
                            "git",
                            "check-ignore",
                            "--no-index",
                            "--quiet",
                            "--",
                            f"{artifact_dir}/probe",
                        ],
                        cwd=ROOT,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        f"{artifact_dir}/ is not ignored by .gitignore",
                    )
            return

        # NEEDLE verifies committed state from a git archive, which has no
        # .git directory.  In that mode, assert the committed ignore entries
        # directly; the index check below is represented by the archive's
        # absence of these paths.
        ignored = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text().splitlines()
            if line.strip() in {f"{name}/" for name in ARTIFACT_DIRS}
        }
        self.assertEqual(ignored, {f"{name}/" for name in ARTIFACT_DIRS})

    def test_artifact_directories_are_not_tracked(self):
        """No committed path may live below a private artifact directory."""

        if (ROOT / ".git").exists():
            for artifact_dir in ARTIFACT_DIRS:
                with self.subTest(artifact_dir=artifact_dir):
                    result = subprocess.run(
                        ["git", "ls-files", "--", f"{artifact_dir}/**"],
                        cwd=ROOT,
                        check=False,
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
            return

        # A git archive contains tracked files only, so any artifact root in
        # this extraction would prove that the committed state leaked one.
        for artifact_dir in ARTIFACT_DIRS:
            with self.subTest(artifact_dir=artifact_dir):
                path = ROOT / artifact_dir
                self.assertFalse(path.exists() or path.is_symlink(), path)


if __name__ == "__main__":
    unittest.main()
