import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_catalog  # noqa: E402
import twill_schema  # noqa: E402


def never_event(event_id="never-one", *, pack="pack-a"):
    return {
        "id": event_id,
        "pack": pack,
        "severity": "High",
        "tier": "tier1",
        "action": "deny",
        "destructive": True,
        "enabled": True,
        "check": "command_regex",
        "match": {"regex": rf"^{event_id}$"},
        "explanation": "the event must not execute",
        "sanctioned_alternative": {"reason": "use the safe path instead"},
    }


def always_event(event_id="safe-one", *, pack="pack-a"):
    return {
        "id": event_id,
        "pack": pack,
        "check": "predicate",
        "match": {"predicate": event_id},
    }


def document(*, never=None, always=None, catalog_format=twill_catalog.CATALOG_FORMAT):
    never = list(never or [never_event()])
    always = list(always or [always_event()])
    content = {
        "format": catalog_format,
        "never": never,
        "always": always,
    }
    digest = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    result = {
        "format": catalog_format,
        "catalog_digest": digest,
        "icg_version": "0.2.0-test",
        "pack_source": {"origin": "installed", "root": "/etc/icg/packs", "trusted_ref": None},
        "never": never,
        "always": always,
    }
    if catalog_format == twill_catalog.LEGACY_CATALOG_FORMAT:
        del result["pack_source"]
    return result


class CatalogParserTests(unittest.TestCase):
    def test_valid_v2_document_preserves_event_identity_and_digest(self):
        catalog = twill_catalog.parse_catalog(document())
        self.assertEqual(catalog.format, twill_catalog.CATALOG_FORMAT)
        self.assertEqual(catalog.never[0].identity, ("pack-a", "never-one"))
        self.assertEqual(catalog.always[0].identity, ("pack-a", "safe-one"))
        self.assertEqual(catalog.digest, twill_catalog.canonical_digest(catalog))

    def test_installed_v1_shape_is_accepted_without_a_pack_source(self):
        value = document(catalog_format=twill_catalog.LEGACY_CATALOG_FORMAT)
        catalog = twill_catalog.parse_catalog(value)
        self.assertEqual(catalog.format, twill_catalog.LEGACY_CATALOG_FORMAT)
        self.assertEqual(catalog.pack_source, {})
        self.assertEqual(catalog.digest, twill_catalog.canonical_digest(catalog))
        with self.assertRaisesRegex(twill_catalog.CatalogError, "fields do not match"):
            twill_catalog.parse_catalog(value | {"pack_source": {}})

    def test_digest_is_recomputed_without_provenance_fields(self):
        value = document()
        value["icg_version"] = "a-different-binary"
        value["pack_source"] = {"origin": "repository", "root": None, "trusted_ref": None}
        catalog = twill_catalog.parse_catalog(value)
        self.assertEqual(catalog.digest, value["catalog_digest"])

    def test_unknown_format_and_bad_digest_are_refused(self):
        with self.assertRaisesRegex(twill_catalog.CatalogError, "unsupported catalog format"):
            twill_catalog.parse_catalog(document() | {"format": "icg-catalog/v3"})
        bad = document()
        bad["catalog_digest"] = "0" * 64
        with self.assertRaisesRegex(twill_catalog.CatalogError, "does not match"):
            twill_catalog.parse_catalog(bad)

    def test_order_and_identity_are_part_of_the_contract(self):
        with self.assertRaisesRegex(twill_catalog.CatalogError, "not ordered"):
            twill_catalog.parse_catalog(
                document(never=[never_event("z"), never_event("a")])
            )
        with self.assertRaisesRegex(twill_catalog.CatalogError, "duplicate"):
            twill_catalog.parse_catalog(
                document(always=[always_event(), always_event()])
            )


class CatalogCommandTests(unittest.TestCase):
    def completed(self, value, returncode=0, stderr=""):
        return subprocess.CompletedProcess(
            ("fake",), returncode, json.dumps(value) if value is not None else "", stderr
        )

    def test_command_output_is_parsed_and_absence_is_non_fatal(self):
        value = document()
        result = twill_catalog.read_catalog_status(
            ("fake", "catalog"), runner=lambda *_args, **_kwargs: self.completed(value)
        )
        self.assertEqual(result.status, "available")
        missing = twill_catalog.read_catalog_status(
            ("fake",),
            runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
        )
        self.assertEqual(missing.status, "absent")
        self.assertFalse(missing.available)

    def test_future_and_malformed_exports_degrade_with_a_reason(self):
        future = document() | {"format": "icg-catalog/v3"}
        result = twill_catalog.read_catalog_status(
            runner=lambda *_args, **_kwargs: self.completed(future)
        )
        self.assertEqual(result.status, "unsupported")
        malformed = twill_catalog.read_catalog_status(
            runner=lambda *_args, **_kwargs: self.completed({"not": "a catalog"})
        )
        self.assertEqual(malformed.status, "invalid")


class CatalogDriftTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state"
        self.connection = twill_schema.connect(self.state)
        self.addCleanup(self.connection.close)

    def read(self, value):
        return twill_catalog.load_and_record(
            self.connection,
            runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                ("fake",), 0, json.dumps(value), ""
            ),
        )

    def test_first_same_and_changed_digest_transitions_are_recorded(self):
        first, first_observation = self.read(document())
        self.assertEqual(first.status, "available")
        self.assertEqual(first_observation.status, "first_seen")
        same, same_observation = self.read(document())
        self.assertEqual(same_observation.status, "unchanged")
        changed = document(never=[never_event("different")])
        _, drift = self.read(changed)
        self.assertEqual(drift.status, "drifted")
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?",
            (twill_catalog.CATALOG_DIGEST_META_KEY,),
        ).fetchone()
        self.assertEqual(row[0], changed["catalog_digest"])

    def test_absent_export_does_not_erase_last_seen_digest(self):
        self.read(document())
        result, observation = twill_catalog.load_and_record(
            self.connection,
            runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                ("fake",), 1, "", "icg unavailable"
            ),
        )
        self.assertEqual(result.status, "absent")
        self.assertIsNone(observation)
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?",
            (twill_catalog.CATALOG_DIGEST_META_KEY,),
        ).fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
