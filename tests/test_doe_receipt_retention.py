from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nlp_refinement.doe_receipt_retention import main, purge_expired_failure_receipts


class DoeReceiptRetentionTests(unittest.TestCase):
    def test_purge_removes_only_expired_well_formed_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expired = root / "failure-0000000001-0123456789abcdef.json"
            expired.write_text(json.dumps({"expires_at": 1}), encoding="utf-8")
            unexpired = root / "failure-0000000002-fedcba9876543210.json"
            unexpired.write_text(json.dumps({"expires_at": 101}), encoding="utf-8")
            unrelated = root / "notes.json"
            unrelated.write_text(json.dumps({"expires_at": 1}), encoding="utf-8")

            removed = purge_expired_failure_receipts(root, 100)

            self.assertEqual(removed, [expired.name])
            self.assertFalse(expired.exists())
            self.assertTrue(unexpired.exists())
            self.assertTrue(unrelated.exists())

    def test_purge_missing_root_is_noop_and_does_not_create_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "missing"

            self.assertEqual(purge_expired_failure_receipts(root, 100), [])
            self.assertFalse(root.exists())

    def test_user_command_reports_only_a_bounded_deletion_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "failure-0000000001-0123456789abcdef.json"
            receipt.write_text(json.dumps({"expires_at": 1}), encoding="utf-8")
            rendered = io.StringIO()
            with redirect_stdout(rendered), patch(
                "nlp_refinement.doe_receipt_retention.time.time", return_value=100
            ):
                self.assertEqual(main(["--failure-evidence-root", str(root)]), 0)

            self.assertEqual(
                json.loads(rendered.getvalue()),
                {
                    "command": "doe-receipt-prune",
                    "purge_trigger": "user-command",
                    "removed": [receipt.name],
                    "removed_count": 1,
                    "retention_seconds": 86400,
                },
            )


if __name__ == "__main__":
    unittest.main()
