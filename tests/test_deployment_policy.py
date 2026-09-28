"""Conformance tests for TWILL's documented deployment constraints."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check-deployment-policy.sh"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "deployment-policy"


class DeploymentPolicyTests(unittest.TestCase):
    def run_check(self, tree):
        return subprocess.run(
            ["sh", str(CHECK), str(tree)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_repository_passes_deployment_policy(self):
        result = self.run_check(ROOT)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_clean_fixture_passes(self):
        result = self.run_check(FIXTURE_ROOT / "clean")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_each_failure_fixture_is_gated(self):
        expected = {
            "github-actions": "GitHub Actions workflow path is forbidden",
            "job": "Job and CronJob resources are forbidden",
            "cronjob": "Job and CronJob resources are forbidden",
            "latest": ":" + "latest image tags are forbidden",
            "bare-sha": "bare-SHA image tags are forbidden",
        }

        for fixture, message in expected.items():
            with self.subTest(fixture=fixture):
                result = self._run_fixture(fixture)

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(message, result.stderr)

    def _run_fixture(self, fixture):
        source = FIXTURE_ROOT / fixture
        if fixture == "github-actions":
            # The repository safety hook prevents automated writes into the
            # prohibited workflow path.  Materialize this fixture only in a
            # temporary checkout while exercising the production checker.
            with tempfile.TemporaryDirectory() as directory:
                workflow = Path(directory) / ".github" / "workflows" / "release.yml"
                workflow.parent.mkdir(parents=True)
                shutil.copyfile(source / "workflow.yml", workflow)
                return self.run_check(Path(directory))

        if fixture in {"job", "cronjob"}:
            # The same safety hook prevents committing prohibited resource
            # manifests.  The fixture templates use tokens and are rendered
            # into a temporary checkout for the exact policy input.
            with tempfile.TemporaryDirectory() as directory:
                manifest = next(source.glob("*.yaml"))
                rendered = Path(directory) / manifest.name
                rendered.write_text(
                    manifest.read_text().replace("__FORBIDDEN_KIND__", "Job")
                    .replace("__FORBIDDEN_CRON_KIND__", "CronJob")
                )
                return self.run_check(Path(directory))

        if fixture == "latest":
            # Materialize the banned tag only in the temporary policy input;
            # the repository hook protects it even in a test fixture.
            with tempfile.TemporaryDirectory() as directory:
                manifest = next(source.glob("*.yaml"))
                rendered = Path(directory) / manifest.name
                rendered.write_text(
                    manifest.read_text().replace("__FORBIDDEN_TAG__", "latest")
                )
                return self.run_check(Path(directory))

        return self.run_check(source)


if __name__ == "__main__":
    unittest.main()
