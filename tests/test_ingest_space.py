import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import twill_app  # noqa: E402
import twill_doctor  # noqa: E402
from twill_config import TwillConfig  # noqa: E402
from twill_contract import CliError  # noqa: E402


class IngestFreeSpaceTests(unittest.TestCase):
    def test_low_space_is_rejected_before_source_or_database_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            args = SimpleNamespace(
                state_dir=str(state),
                settle=0,
                source=None,
                file=str(root / "missing.jsonl"),
                limit=1,
                json=False,
            )
            config = TwillConfig(artifacts_root=root / "artifacts")
            with patch.object(twill_app, "load_config", return_value=config):
                with self.assertRaises(CliError) as raised:
                    twill_app.ingest_command(
                        args,
                        disk_usage=lambda _: SimpleNamespace(
                            free=twill_doctor.FREE_DISK_INGEST_FLOOR_BYTES - 1
                        ),
                    )

            self.assertEqual(raised.exception.code, 1)
            self.assertIn("ingest refused", raised.exception.message)
            self.assertFalse((state / "twill.db").exists())
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
