import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check-published-tree.sh"


class PublishedTreeArtifactTests(unittest.TestCase):
    def run_check(self, tree):
        return subprocess.run(
            ["sh", str(CHECK), str(tree)],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_clean_tree_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_check(Path(directory))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_each_private_artifact_tree_fails(self):
        for artifact_dir in ("lessons", "digests", "measurements", "guards"):
            with self.subTest(artifact_dir=artifact_dir):
                with tempfile.TemporaryDirectory() as directory:
                    artifact = Path(directory) / artifact_dir / "private.md"
                    artifact.parent.mkdir()
                    artifact.write_text("private\n")

                    result = self.run_check(Path(directory))

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f"{artifact_dir}/private.md", result.stderr)

    def test_nested_artifact_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "guards" / "nested" / "guard.txt"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("private\n")

            result = self.run_check(Path(directory))

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("guards/nested/guard.txt", result.stderr)


if __name__ == "__main__":
    unittest.main()
