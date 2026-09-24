"""The secret-fixture test at the persistence boundary (plan §5 Scenario 4, §9 Phase 1).

Scenario 4 drives the secret-bearing fixture corpus through the shipped verbs
— ``twill ingest && twill detect && twill digest`` — and greps the resulting
state for every fixture credential.  This module owns the state-DB half of
that scenario: no fixture credential value may appear anywhere in the
persisted state, and the error signature must still be captured.  The Explain
prompt assertion lands with ``twill explain --dry-run``; the digest- and
lesson-file assertions have their own bead.

The credential values are never spelled in this source file: they are
discovered in the fixture corpus itself (tests/fixtures/transcripts/README.md
— all fixture credentials are inert synthetic values), so the forbidden set is
exactly what the corpus holds and no token-shaped literal sits in the
repository for a secret scanner to reject.

Non-skippability (plan §10.2): a skipped or xfail'd redaction test fails the
build.  The :class:`NonSkippable` metaclass makes that mechanically true —
skip markers are rejected at class creation (including
``unittest.expectedFailure``, whose marker survives the guard's own wrapping
and would otherwise turn a redaction failure into a green build), a skip
raised at runtime is converted into a failure, and a module-bottom audit
re-checks the shipped classes after decorators and rebinding.
:class:`NonSkipGuardTests` exercises every route a skip could take and
asserts each one lands as a failure.
"""

import functools
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import pytest
except ImportError:  # pragma: no cover - §10.2 requires pytest; guarded below
    pytest = None

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "transcripts"
CLI = ROOT / "twill"
sys.path.insert(0, str(ROOT))

from twill_redactor import CREDENTIAL_PATTERNS, Redactor  # noqa: E402
import twill_schema  # noqa: E402


# Credential kinds the secret fixtures must exercise (plan §5 Scenario 4: a
# realistic gh token, an AWS key, and a Bearer header).  A fixture that loses
# one of these shapes makes the absence assertions below vacuous, so the
# precondition is enforced, not assumed.
REQUIRED_CREDENTIAL_KINDS = frozenset(
    {"github-token", "aws-access-key", "bearer-token"}
)

# pytest markers that would soften a redaction test into a non-test.
_SKIP_MARKS = frozenset({"skip", "skipif", "xfail"})

_GUARD_ATTR = "__nonskip_guard__"


def _skip_exceptions():
    """Every exception a runner can raise to mean "skipped"."""

    exceptions = [unittest.SkipTest]
    if pytest is not None:
        for outcome in (pytest.skip, pytest.xfail, pytest.importorskip):
            exception = getattr(outcome, "Exception", None)
            if isinstance(exception, type):
                exceptions.append(exception)
    return tuple(dict.fromkeys(exceptions))


def _describe_skip(skipped):
    reason = str(skipped) or skipped.__class__.__name__
    return (
        f"a redaction test attempted to skip ({reason}); redaction tests are "
        "non-skippable -- a skipped or xfail'd redaction test fails the build "
        "(plan §10.2)"
    )


def _fail_on_skip(function, description):
    """Wrap one test method so every skip route becomes a failure."""

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except _skip_exceptions() as skipped:
            raise AssertionError(f"{description}: {_describe_skip(skipped)}") from skipped

    setattr(wrapper, _GUARD_ATTR, True)
    return wrapper


def _reject_skip_marker(member, description):
    """Refuse a statically marked skip, xfail, or expected failure."""

    if getattr(member, "__unittest_skip__", False):
        raise AssertionError(
            f"{description} is decorated with a unittest skip: "
            f"{getattr(member, '__unittest_skip_why__', '')!r}; "
            "redaction tests are non-skippable (plan §10.2)"
        )
    if any(
        getattr(member, attribute, False)
        for attribute in (
            "__unittest_expecting_failure__",  # the marker CPython reads
            "__unittest_expected_failure__",  # defensive: plausible misspelling
        )
    ):
        # expectedFailure is the unittest spelling of xfail, and its marker
        # is a plain attribute: functools.wraps copies it onto the guard's
        # own wrapper, so without this check a failing redaction test would
        # be recorded as an expected failure and the build would stay green.
        raise AssertionError(
            f"{description} is decorated with unittest.expectedFailure; "
            "redaction tests are non-skippable (plan §10.2)"
        )
    for mark in getattr(member, "pytestmark", ()) or ():
        if getattr(mark, "name", None) in _SKIP_MARKS:
            raise AssertionError(
                f"{description} is marked pytest.mark.{mark.name}; "
                "redaction tests are non-skippable (plan §10.2)"
            )


def _is_test_method(name, member):
    return name.startswith("test") and callable(member)


class NonSkippable(type):
    """Metaclass for redaction tests: skipping is refused or converted.

    At class creation every ``test*`` method is (a) checked for static skip
    markers — ``unittest.skip``, ``unittest.expectedFailure`` or
    ``pytest.mark.skip/skipif/xfail`` — and rejected, and (b) wrapped so a
    skip raised at runtime (``self.skipTest``, ``pytest.skip``,
    ``pytest.xfail``) becomes a hard failure instead.  The
    same wrapping covers ``setUp``, ``setUpClass`` and ``tearDown``, whose
    skips would otherwise soften whole classes.  Pair the class with a
    module-bottom :func:`_assert_unskippable` call so decorators applied
    after creation, or a rebound test method, fail the import.
    """

    # Lifecyle hooks a skip can escape from besides the test method itself.
    _GUARDED_HOOKS = ("setUpClass", "setUp", "tearDown")

    def __new__(mcs, name, bases, namespace):
        for key, member in list(namespace.items()):
            unwrapped = member.__func__ if isinstance(member, classmethod) else member
            if not (
                _is_test_method(key, unwrapped) or key in mcs._GUARDED_HOOKS
            ):
                continue
            description = f"{name}.{key}"
            _reject_skip_marker(unwrapped, description)
            guarded = _fail_on_skip(unwrapped, description)
            namespace[key] = classmethod(guarded) if isinstance(member, classmethod) else guarded
        cls = super().__new__(mcs, name, bases, namespace)
        _reject_skip_marker(cls, name)
        return cls


def _assert_unskippable(test_class):
    """Audit one shipped test class after decorators and rebinding.

    Re-runs the creation-time marker checks — a ``@unittest.skip`` above the
    ``class`` line or a ``pytestmark`` assignment lands after the metaclass
    saw the namespace — and asserts every ``test*`` method still carries the
    runtime guard, so a plain-function rebinding cannot silently drop it.
    Called at this module's bottom, an AssertionError here fails collection
    under both runners: the build fails, as §10.2 demands.
    """

    _reject_skip_marker(test_class, test_class.__name__)
    for name in dir(test_class):
        member = getattr(test_class, name)
        if not _is_test_method(name, member):
            continue
        description = f"{test_class.__name__}.{name}"
        _reject_skip_marker(member, description)
        if not getattr(member, _GUARD_ATTR, False):
            raise AssertionError(
                f"{description} lost its non-skip guard; redaction tests are "
                "non-skippable (plan §10.2)"
            )
    return test_class


def _iter_record_strings(value):
    """Every string in a parsed JSON fixture record, at any depth."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_record_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_record_strings(item)


def _credential_atoms(text):
    """Yield ``(kind, atom)`` for every credential-shaped span in ``text``.

    The atom is the credential value itself, not the surrounding formatting:
    for a pattern whose replacement is a constant prefix plus one
    ``<redacted:kind>`` marker, the prefix (e.g. ``"Bearer "``) stays part of
    the redacted text and is stripped here.  Patterns with backreference
    replacements have no constant prefix, so their full match is forbidden
    whole — redaction removes the full match either way.
    """

    for pattern, replacement in CREDENTIAL_PATTERNS:
        marker_index = replacement.find("<redacted:")
        if marker_index < 0:
            continue
        prefix = replacement[:marker_index]
        kind = replacement[marker_index + len("<redacted:") :].rstrip(">")
        for match in pattern.finditer(text):
            matched = match.group(0)
            atom = matched[len(prefix) :] if matched.startswith(prefix) else matched
            yield kind or "credential", atom


class SecretFixturePipelineTests(unittest.TestCase, metaclass=NonSkippable):
    """Scenario 4 (§5) at the persistence boundary, through the real verbs.

    ``setUpClass`` runs the pipeline once per class: both secret-bearing
    fixtures are copied into a source tree (under ``.claude/`` and ``.codex/``
    so each takes its real parser), ingested, detected and digested through
    the shipped CLI, then the state directory is snapshotted — raw bytes of
    every state file, and every string value in every table and column of the
    database, opened read-only.  The individual tests assert against that
    snapshot.
    """

    @classmethod
    def setUpClass(cls):
        cls._home = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._home.cleanup)
        cls._work = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._work.cleanup)
        home = Path(cls._home.name)
        work = Path(cls._work.name)

        # ingest and detect load config at startup and artifacts_root has no
        # default (plan §13.1): every CLI run needs a config that sets it,
        # outside this repository tree.
        config_dir = home / ".config" / "twill"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            f'artifacts_root = "{home / "artifacts"}"\n'
        )

        cls.state_dir = work / "state"
        cls.fixtures = {}
        manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text())
        for source, source_data in manifest["sources"].items():
            fixture = cls._fixture_path(manifest, source)
            discovered = cls._discover_credentials(fixture)
            cls.fixtures[source] = discovered
            if not REQUIRED_CREDENTIAL_KINDS <= set(discovered["kinds"]):
                raise AssertionError(
                    f"{fixture} no longer carries a credential of every "
                    f"required kind {sorted(REQUIRED_CREDENTIAL_KINDS)} "
                    f"(found {sorted(discovered['kinds'])}); the Scenario 4 "
                    "fixture must stay secret-bearing or the absence "
                    "assertions below are vacuous"
                )
            session_dir = work / "sources" / f".{source}"
            session_dir.mkdir(parents=True)
            target = session_dir / fixture.name
            shutil.copyfile(fixture, target)
            cls.run_cli(
                "ingest",
                "--file",
                str(target),
                "--settle",
                "0",
                "--state-dir",
                str(cls.state_dir),
            )

        cls.run_cli("detect", "--state-dir", str(cls.state_dir))
        cls.run_cli("digest", "--stdout", "--state-dir", str(cls.state_dir))

        # Snapshot the persisted state before anything else can touch it:
        # every byte of every state file (the plan's grep across twill.db,
        # including the -wal sibling WAL writes land in), and every string
        # value in every table and column.
        cls.state_blobs = {
            path.name: path.read_bytes()
            for path in sorted(cls.state_dir.iterdir())
            if path.is_file()
        }
        cls.stored_texts = tuple(cls._dump_state_text(cls.state_dir))

    @classmethod
    def _fixture_path(cls, manifest, source):
        relative = manifest["sources"][source]["cases"]["secret-bearing"]["files"][0]
        return FIXTURE_ROOT / relative

    @classmethod
    def _discover_credentials(cls, fixture):
        """Pull the credential atoms and the clean text out of one fixture."""

        records = [
            json.loads(line)
            for line in fixture.read_text().splitlines()
            if line.strip()
        ]
        atoms = []
        kinds = set()
        credential_lines = []
        plain_lines = []
        for record in records:
            for text in _iter_record_strings(record):
                found = list(_credential_atoms(text))
                if found:
                    credential_lines.append(text)
                    for kind, atom in found:
                        kinds.add(kind)
                        atoms.append(atom)
                else:
                    plain_lines.append(text)
        error_lines = [text for text in plain_lines if "error" in text.lower()]
        if not error_lines:
            raise AssertionError(
                f"{fixture} carries no error text beside its credentials; "
                "Scenario 4 needs a failing command with a genuine error "
                "signature to assert on"
            )
        return {
            "path": fixture,
            "atoms": tuple(dict.fromkeys(atoms)),
            "kinds": kinds,
            "credential_lines": tuple(credential_lines),
            "error_line": error_lines[0],
        }

    @classmethod
    def _dump_state_text(cls, state_dir):
        """Yield ``(table, column, value)`` for every string in the state DB."""

        connection = twill_schema.connect_read_only(state_dir)
        try:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ]
            for table in tables:
                columns = [
                    row[1]
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                ]
                for column in columns:
                    for (value,) in connection.execute(
                        f'SELECT "{column}" FROM "{table}"'
                    ):
                        if isinstance(value, str):
                            yield table, column, value
                        elif isinstance(value, bytes):
                            yield table, column, value.decode("utf-8", "replace")
        finally:
            connection.close()

    @classmethod
    def run_cli(cls, *args):
        result = subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=str(ROOT),
            env={**os.environ, "HOME": str(Path(cls._home.name))},
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"twill {' '.join(args)} failed with exit "
                f"{result.returncode}: {result.stderr.strip()}"
            )
        return result

    @classmethod
    def forbidden_values(cls):
        """Every value that must not appear in the persisted state."""

        values = set()
        for fixture in cls.fixtures.values():
            # The credential atoms themselves, plus each full unredacted
            # line: redaction replaces the whole match, so the original
            # sentence must never persist in any form.
            values.update(fixture["atoms"])
            values.update(fixture["credential_lines"])
        return sorted(values)

    def test_no_fixture_credential_appears_in_the_raw_state_files(self):
        # The plan's pass criterion, literally: a grep for each fixture value
        # across the state database returns nothing.  Every file in the state
        # directory is scanned so a credential sitting in the WAL is caught.
        offenders = []
        for value in self.forbidden_values():
            encoded = value.encode("utf-8")
            for name, blob in self.state_blobs.items():
                if encoded in blob:
                    offenders.append(f"{value[:24]!r}… in {name}")
        self.assertEqual(offenders, [], "fixture credentials leaked to disk")

    def test_no_fixture_credential_appears_in_any_table_or_column(self):
        # The structured counterpart to the raw grep: every table, every
        # column, every row.  A leak is reported with its provenance instead
        # of only "somewhere in the file".
        offenders = []
        for table, column, value in self.stored_texts:
            for forbidden in self.forbidden_values():
                if forbidden in value:
                    offenders.append(f"{table}.{column}: {value[:80]!r}")
        self.assertEqual(offenders, [], "fixture credentials leaked into the state DB")

    def test_every_credential_kind_lands_as_a_redaction_marker(self):
        # Positive control for the absence assertions: the pipeline actually
        # ingested the secret fixture and redaction engaged — every kind the
        # fixtures carry is present in the state DB as its marker.
        stored = "\n".join(value for _, _, value in self.stored_texts)
        for source, fixture in self.fixtures.items():
            for kind in sorted(fixture["kinds"]):
                marker = f"<redacted:{kind}>"
                self.assertIn(marker, stored, f"{source}: {marker} never reached the DB")

    def test_the_redacted_credential_line_is_what_persisted(self):
        # The exact string the Store's redactor produces for the credential
        # line — computed with the same Redactor, not hardcoded — must be
        # present verbatim, so the fixture provably went through this
        # boundary rather than the assertions passing on an empty database.
        stored = {value for _, _, value in self.stored_texts}
        for source, fixture in self.fixtures.items():
            for line in fixture["credential_lines"]:
                expected = Redactor().redact_text(line)
                self.assertIn(
                    expected,
                    stored,
                    f"{source}: the redacted form of the credential line did "
                    "not persist unchanged",
                )

    def test_the_error_line_is_captured_and_signatured(self):
        # Scenario 4's other half: the genuine error signature is captured
        # while the credentials beside it are gone.  The clean error line
        # persists verbatim, and the persisted row carries the engine's
        # compact signature hash.
        stored = {value for _, _, value in self.stored_texts}
        connection = twill_schema.connect_read_only(self.state_dir)
        try:
            for source, fixture in self.fixtures.items():
                error_line = fixture["error_line"]
                self.assertIn(
                    error_line,
                    stored,
                    f"{source}: the error line beside the credentials was "
                    "not captured",
                )
                rows = connection.execute(
                    "SELECT signature, sig_hash FROM transcript_event "
                    "WHERE text = ?",
                    (error_line,),
                ).fetchall()
                self.assertTrue(
                    rows, f"{source}: the error line was stored but not signatured"
                )
                for signature, sig_hash in rows:
                    self.assertTrue(signature, f"{source}: empty signature")
                    self.assertRegex(
                        sig_hash or "",
                        r"^[0-9a-f]{12}$",
                        f"{source}: sig_hash is not the 12-hex fingerprint",
                    )
            observations = connection.execute(
                "SELECT count(*) FROM observation"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertGreaterEqual(
            observations, 1, "no observation was derived from the secret fixtures"
        )


class NonSkipGuardTests(unittest.TestCase, metaclass=NonSkippable):
    """The §10.2 guard itself: every skip route a redaction test could take
    must land as a failure, never as a skipped outcome."""

    def run_one(self, case):
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(
            unittest.TestSuite((case,))
        )
        return result

    def test_a_dynamically_skipped_test_fails_instead(self):
        class Attempt(unittest.TestCase, metaclass=NonSkippable):
            def test_would_be_skipped(self):
                raise unittest.SkipTest("sabotage")

        result = self.run_one(Attempt("test_would_be_skipped"))
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.skipped, [], "the skip went through")
        self.assertEqual(len(result.failures), 1, result.errors)
        self.assertIn("non-skippable", result.failures[0][1])

    def test_a_skip_in_setUp_fails_instead(self):
        class Attempt(unittest.TestCase, metaclass=NonSkippable):
            def setUp(self):
                raise unittest.SkipTest("sabotage")

            def test_would_be_skipped(self):
                pass

        result = self.run_one(Attempt("test_would_be_skipped"))
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.skipped, [], "the setUp skip went through")
        self.assertTrue(result.failures or result.errors)

    def test_a_skip_in_setUpClass_fails_the_class(self):
        class Attempt(unittest.TestCase, metaclass=NonSkippable):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest("sabotage")

            def test_would_be_skipped(self):
                pass

        result = self.run_one(Attempt("test_would_be_skipped"))
        # A failed class setup reports as a class-level error and the test
        # never runs (testsRun stays 0) — the build fails either way, which
        # is the outcome §10.2 wants; it must not be a skipped outcome.
        self.assertEqual(result.skipped, [], "the setUpClass skip went through")
        self.assertTrue(result.failures or result.errors, "the class error was swallowed")

    def test_a_pytest_skip_call_fails_instead(self):
        if pytest is None:
            raise AssertionError("pytest is a §10.2 gate; the guard cannot be verified")

        class Attempt(unittest.TestCase, metaclass=NonSkippable):
            def test_would_be_skipped(self):
                pytest.skip("sabotage")

        result = self.run_one(Attempt("test_would_be_skipped"))
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.skipped, [], "the pytest skip went through")
        self.assertEqual(len(result.failures), 1, result.errors)

    def test_a_pytest_marked_skip_is_rejected_at_the_module_audit(self):
        if pytest is None:
            raise AssertionError("pytest is a §10.2 gate; the guard cannot be verified")

        class Marked(unittest.TestCase, metaclass=NonSkippable):
            def test_marked(self):
                pass

        Marked.test_marked = pytest.mark.skip("sabotage")(Marked.test_marked)
        with self.assertRaises(AssertionError):
            _assert_unskippable(Marked)

    def test_a_pytest_marked_xfail_is_rejected_at_the_module_audit(self):
        if pytest is None:
            raise AssertionError("pytest is a §10.2 gate; the guard cannot be verified")

        class Marked(unittest.TestCase, metaclass=NonSkippable):
            def test_marked(self):
                pass

        Marked.test_marked = pytest.mark.xfail(reason="sabotage")(Marked.test_marked)
        with self.assertRaises(AssertionError):
            _assert_unskippable(Marked)

    def test_a_unittest_skipped_class_is_rejected_at_the_module_audit(self):
        class Marked(unittest.TestCase, metaclass=NonSkippable):
            def test_marked(self):
                pass

        Marked = unittest.skip("sabotage")(Marked)
        with self.assertRaises(AssertionError):
            _assert_unskippable(Marked)

    def test_a_unittest_expected_failure_method_is_rejected_at_creation(self):
        # expectedFailure is xfail's unittest spelling: without the check, a
        # failing redaction test would be recorded as an expected failure and
        # the build would stay green.
        with self.assertRaises(AssertionError):
            class Marked(unittest.TestCase, metaclass=NonSkippable):
                @unittest.expectedFailure
                def test_marked(self):
                    self.fail("sabotage")

    def test_a_post_hoc_expected_failure_is_rejected_at_the_module_audit(self):
        class Marked(unittest.TestCase, metaclass=NonSkippable):
            def test_marked(self):
                self.fail("sabotage")

        Marked.test_marked = unittest.expectedFailure(Marked.test_marked)
        with self.assertRaises(AssertionError):
            _assert_unskippable(Marked)

    def test_a_rebound_unguarded_test_method_is_rejected_at_the_module_audit(self):
        class Attempt(unittest.TestCase, metaclass=NonSkippable):
            def test_guarded(self):
                pass

        Attempt.test_guarded = lambda self: None
        with self.assertRaises(AssertionError):
            _assert_unskippable(Attempt)


# §10.2, enforced at import: the shipped classes are audited after decorators
# and rebinding, so any skip marker added above a class or on a method fails
# collection under unittest and pytest alike.  A module-level pytestmark or a
# setUpModule that could skip the whole module is equally refused.
for _class in (SecretFixturePipelineTests, NonSkipGuardTests):
    _assert_unskippable(_class)
if globals().get("setUpModule") is not None:
    raise AssertionError(
        "test_secret_fixture must not define setUpModule: redaction tests "
        "are non-skippable (plan §10.2)"
    )
_module_marks = globals().get("pytestmark") or ()
for _mark in _module_marks:
    if getattr(_mark, "name", None) in _SKIP_MARKS:
        raise AssertionError(
            f"test_secret_fixture is marked pytest.mark.{_mark.name} at module "
            "level; redaction tests are non-skippable (plan §10.2)"
        )


if __name__ == "__main__":
    unittest.main()
