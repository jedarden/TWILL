"""Import and network gates for the plan §3 forbidden dependencies.

The runtime audit hook catches dynamic imports and Python-level egress in the
suite and in its Python children.  The source-level import walk is the second
leg: it checks the production tree even when a test runner happened to load a
third-party module before the startup hook was installed.
"""

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT))

import openpath  # noqa: E402


def production_files():
    """Yield production Python sources, including the extensionless CLI."""

    yield from sorted(ROOT.glob("*.py"))
    cli = ROOT / "twill"
    if cli.is_file():
        yield cli


def imported_modules(path: Path):
    """Yield absolute top-level names imported by one production source."""

    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


class ImportAuditTests(unittest.TestCase):
    """The core path remains stdlib-only and the runtime gate is live."""

    def test_every_production_import_is_stdlib_or_repository_local(self):
        violations = []
        for path in production_files():
            for module in imported_modules(path):
                try:
                    openpath.check_import(module)
                except openpath.ImportViolation as violation:
                    violations.append(f"{path.name}: {violation}")
        self.assertEqual(violations, [])

    def test_runtime_import_policy_allows_stdlib_and_rejects_third_party(self):
        openpath.check_import("json.decoder")
        openpath.check_import("twill_config")
        with self.assertRaises(openpath.ImportViolation):
            openpath.check_import("requests")

    def test_spawned_python_child_rejects_a_third_party_import(self):
        child = subprocess.run(
            [sys.executable, "-c", "import requests"],
            cwd=ROOT,
            env=dict(os.environ),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("ImportViolation", child.stderr)


class NetworkAuditTests(unittest.TestCase):
    """Python network activity is stopped before it can leave the host."""

    def test_network_events_are_rejected(self):
        with self.assertRaises(openpath.NetworkViolation):
            openpath.audit_hook("socket.connect", (None, ("example.invalid", 443)))
        with self.assertRaises(openpath.NetworkViolation):
            openpath.audit_hook("socket.getaddrinfo", ("example.invalid", 443))

    def test_spawned_python_child_cannot_create_a_socket(self):
        child = subprocess.run(
            [sys.executable, "-c", "import socket; socket.socket()"],
            cwd=ROOT,
            env=dict(os.environ),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("NetworkViolation", child.stderr)

    def test_explain_claude_print_invocation_is_the_allowlisted_child(self):
        self.assertIsNone(
            openpath.audit_hook(
                "subprocess.Popen",
                ("claude", ["claude", "-p", "--model", "claude-haiku-4-5"], None, None),
            )
        )

    def test_other_child_processes_are_rejected(self):
        with self.assertRaises(openpath.SubprocessViolation):
            openpath.check_subprocess("git", ["git", "status"])
        with self.assertRaises(openpath.SubprocessViolation):
            openpath.check_subprocess("claude", ["claude", "--version"])

    def test_production_child_cannot_spawn_another_process(self):
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                "from twill_artifacts import _git; "
                "_git(Path.cwd(), ('status',))",
            ],
            cwd=ROOT,
            env=dict(os.environ),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("SubprocessViolation", child.stderr)

    def test_non_network_events_are_ignored(self):
        self.assertIsNone(openpath.audit_hook("os.stat", (str(ROOT),)))


if __name__ == "__main__":
    unittest.main()
