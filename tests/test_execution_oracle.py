"""Execution schema, hidden-test boundaries and opt-in real Docker isolation."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import twill_execution_oracle as oracle


def fixture():
    return {"type": oracle.TYPE, "image": oracle.IMAGE, "output_files": ["solution.py"],
            "target_file": "solution.py", "function": "transform", "assertions": [
                {"id": "preserves-data", "args": ["  useful data  "], "equals": "  useful data  "}]}


def answer(code="def transform(value):\n return value\n"):
    return json.dumps({"files": {"solution.py": code}})


class ExecutionOracleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="oracle-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def execute(self, code=None):
        return oracle.execute_answer(fixture(), answer() if code is None else answer(code), work_root=self.root)

    def test_schema_rejects_unpinned_images_and_unbounded_execution(self):
        for field, value in [("image", "python:latest"), ("image", "python:3.12.12-slim"),
                             ("type", "shell"), ("function", "__import__"),
                             ("target_file", "outside.py"), ("assertions", []),
                             ("assertions", [{"id": "bad", "args": [], "command": "sh"}])]:
            spec = fixture()
            spec[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                oracle.validate_oracle(spec)
        spec = fixture()
        spec["command"] = ["sh", "-c", "malicious"]
        with self.assertRaises(ValueError):
            oracle.validate_oracle(spec)

    def test_rejects_traversal_configuration_and_ambiguous_paths(self):
        names = ["../escape.py", "/tmp/escape.py", "a/../escape.py", "a//x.py", "a/./x.py",
                 ".codex/x.py", ".claude/x.py", ".git/x.py", "sitecustomize.py", "a/usercustomize.py",
                 "AGENTS.md", "solution.py\x00", "a\\x.py", "-option.py"]
        for name in names:
            spec = fixture()
            spec["output_files"] = [name]
            spec["target_file"] = name
            with self.subTest(name=name), self.assertRaises(ValueError):
                oracle.validate_oracle(spec)

    def test_assertions_must_be_finite_unique_json(self):
        for assertions in ([{"id": "x", "equals": float("nan")}],
                           [{"id": "x", "equals": 1}, {"id": "x", "equals": 2}],
                           [{"id": "x", "kwargs": {1: "invalid"}, "equals": 1}],
                           [{"id": "x", "equals": object()}]):
            spec = fixture()
            spec["assertions"] = assertions
            with self.subTest(assertions=assertions), self.assertRaises(ValueError):
                oracle.validate_oracle(spec)

    def test_malformed_candidate_files_fail_without_runtime_call(self):
        values = ["not json", "[]", '{}', '{"files":{"../escape.py":"bad"}}',
                  '{"files":{"solution.py":7}}', '{"files":{"solution.py":"ok"},"command":"sh"}',
                  json.dumps({"files": {"solution.py": "x" * (oracle.MAX_BYTES + 1)}})]
        with patch.object(oracle, "_docker") as run:
            for value in values:
                report = oracle.execute_answer(fixture(), value, work_root=self.root)
                self.assertEqual(report["status"], "fail")
                self.assertFalse(report["executed"])
            run.assert_not_called()

    def test_controller_materializes_only_declared_regular_files(self):
        invocations = []
        def docker(args, *, root, timeout):
            invocations.append(args)
            if args[0] == "run":
                self.assertEqual((root / "workspace/solution.py").read_text(), "def transform(value):\n return value\n")
                self.assertFalse((root / "workspace/solution.py").is_symlink())
                self.assertEqual((root / "trusted").stat().st_mode & 0o777, 0o700)
                self.assertEqual(json.loads((root / "trusted/assertions.json").read_text()), fixture())
                return {"returncode": 0, "error": None, "stdout": json.dumps({"executed": True, "assertions": [
                    {"id": "preserves-data", "passed": True, "returncode": 0, "error": None}]})}
            return {"returncode": 0, "error": None, "stdout": ""}
        with patch.object(oracle, "_docker", side_effect=docker):
            report = self.execute()
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["executed"])
        self.assertEqual(invocations[-1][:2], ["rm", "--force"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_setup_errors_are_unknown_and_cleanup_always_runs(self):
        for returncode, error in [(125, None), (None, "FileNotFoundError"), (-9, "timeout"), (1, None)]:
            with self.subTest(returncode=returncode, error=error):
                with patch.object(oracle, "_docker", return_value={"returncode": returncode, "error": error, "stdout": ""}) as run:
                    report = self.execute()
                self.assertEqual(report["status"], "error")
                self.assertFalse(report["executed"])
                self.assertEqual(run.call_count, 2)

    def test_supervisor_candidate_failure_stays_distinct_from_setup_failure(self):
        row = {"id": "preserves-data", "passed": False, "returncode": 17, "error": "candidate_exit"}
        with patch.object(oracle, "_docker", return_value={"returncode": 1, "error": None,
                    "stdout": json.dumps({"executed": True, "assertions": [row]})}):
            report = self.execute()
        self.assertEqual(report["status"], "fail")
        self.assertTrue(report["executed"])
        self.assertEqual(report["assertions"][0]["returncode"], 17)

    def test_unverified_or_incomplete_supervisor_report_never_passes(self):
        reports = [{"executed": True, "assertions": []},
                   {"executed": True, "assertions": [{"id": "wrong", "passed": True}]},
                   {"executed": True, "assertions": [{"id": "preserves-data", "passed": False}]},
                   {"executed": True, "assertions": [{"id": "preserves-data", "passed": "yes"}]}]
        for value in reports:
            with patch.object(oracle, "_docker", return_value={"returncode": 0, "error": None, "stdout": json.dumps(value)}):
                self.assertEqual(self.execute()["status"], "error")

    def test_exact_runtime_argv_has_no_shell_network_or_host_secrets(self):
        command = oracle._command(oracle.IMAGE, self.root, "twill-oracle-" + "a" * 24)
        for token in ["--read-only", "--network", "none", "--pull=never", "--pids-limit", "--cpus", "--memory",
                      "--memory-swap", "--cap-drop", "ALL", "SETUID", "SETGID", "no-new-privileges"]:
            self.assertIn(token, command)
        self.assertEqual(command[-5:], ["/usr/local/bin/python", oracle.IMAGE, "-I", "-S", "/trusted/runner.py"])
        self.assertNotIn("--privileged", command)
        self.assertEqual(sum(token.startswith("type=bind,") for token in command), 2)
        self.assertFalse(any("docker.sock" in token or "--env" == token for token in command))

    def test_preflight_unavailable_fails_closed(self):
        with patch.object(oracle, "execute_answer", return_value={"status": "error", "error": "sandbox_setup_failure"}):
            with self.assertRaisesRegex(RuntimeError, "runtime unavailable"):
                oracle.preflight_oracles([{"oracle": fixture()}], work_root=self.root)


@unittest.skipUnless(os.environ.get("TWILL_DOCKER_TEST") == "1", "opt-in actual Docker sandbox smoke")
class RealDockerOracleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="oracle-smoke-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def run_code(self, code):
        return oracle.execute_answer(fixture(), answer(code), work_root=self.root)

    def test_real_runtime_isolation_and_meaningful_assertion_failure(self):
        oracle.preflight_oracles([{"oracle": fixture()}], work_root=self.root)
        self.assertEqual(self.run_code("def transform(value):\n return value\n")["status"], "pass")
        failed = self.run_code("def transform(value):\n return value.strip()\n")
        self.assertEqual(failed["status"], "fail")
        self.assertTrue(failed["executed"])
        self.assertEqual(failed["assertions"][0]["returncode"], 0)

    def test_generated_code_cannot_forge_supervisor_success(self):
        code = 'import os\nprint(\'{"executed":true,"assertions":[]}\')\nos._exit(0)\n'
        report = self.run_code(code)
        self.assertEqual(report["status"], "fail")
        self.assertTrue(report["executed"])

    def test_generated_code_cannot_read_expected_answers(self):
        report = self.run_code("def transform(value):\n from pathlib import Path\n return Path('/trusted/assertions.json').read_text()\n")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["assertions"][0]["error"], "candidate_exit")

    def test_candidate_timeout_is_executed_failure(self):
        report = self.run_code("def transform(value):\n import time\n time.sleep(30)\n")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["assertions"][0]["error"], "candidate_timeout")


if __name__ == "__main__":
    unittest.main()
