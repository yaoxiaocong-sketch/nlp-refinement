from __future__ import annotations

import tempfile
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from nlp_refinement.repair_feasibility import (
    ProbeStatus,
    open_anchored_regular_file,
    run_isolated_repair_feasibility_doe,
)


class RepairFeasibilityDoeTests(unittest.TestCase):
    def test_module_emits_a_machine_readable_temporary_fixture_report(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "nlp_refinement.repair_feasibility"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["execution_boundary"], "temporary-fixture-only")
        self.assertTrue(report["fixture_root_removed"])

    def test_isolated_doe_returns_observations_and_explicit_limits(self) -> None:
        result = run_isolated_repair_feasibility_doe()

        self.assertEqual(result["execution_boundary"], "temporary-fixture-only")
        self.assertTrue(result["fixture_root_removed"])
        probes = {probe["id"]: probe for probe in result["probes"]}
        self.assertEqual(probes["DOE-ANCHOR-LEAF-SYMLINK"]["status"], ProbeStatus.OBSERVED.value)
        self.assertEqual(probes["DOE-ANCHOR-HARDLINK"]["status"], ProbeStatus.OBSERVED.value)
        self.assertEqual(probes["DOE-LOCK-COOPERATING-PROCESS"]["status"], ProbeStatus.OBSERVED.value)
        self.assertEqual(probes["DOE-ATOMIC-REPLACE"]["status"], ProbeStatus.OBSERVED.value)
        self.assertEqual(probes["DOE-MATCHING-HASH-ROLLBACK"]["status"], ProbeStatus.OBSERVED.value)
        self.assertEqual(probes["DOE-DRIFT-SKIPS-ROLLBACK"]["status"], ProbeStatus.OBSERVED.value)
        self.assertEqual(probes["DOE-PARENT-TOCTOU"]["status"], ProbeStatus.NOT_PROVEN.value)
        self.assertEqual(probes["DOE-CRASH-RECOVERY"]["status"], ProbeStatus.NOT_PROVEN.value)
        self.assertEqual(probes["DOE-ACL-PRESERVATION"]["status"], ProbeStatus.NOT_PROVEN.value)

    def test_anchored_open_rejects_a_leaf_symlink_and_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as fixture_root:
            root = Path(fixture_root)
            regular = root / "regular.md"
            regular.write_text("safe", encoding="utf-8")
            link = root / "link.md"
            link.symlink_to(regular)
            hard_link = root / "hard-link.md"
            hard_link.hardlink_to(regular)

            with self.assertRaises(ValueError):
                open_anchored_regular_file(root, "link.md")
            with self.assertRaises(ValueError):
                open_anchored_regular_file(root, "hard-link.md")

    def test_anchored_open_rejects_path_traversal_names(self) -> None:
        with tempfile.TemporaryDirectory() as fixture_root:
            root = Path(fixture_root)
            (root / "regular.md").write_text("safe", encoding="utf-8")

            with self.assertRaises(ValueError):
                open_anchored_regular_file(root, "nested/regular.md")
            with self.assertRaises(ValueError):
                open_anchored_regular_file(root, "../regular.md")

    def test_missing_descriptor_primitive_is_reported_as_unsupported(self) -> None:
        with patch(
            "nlp_refinement.repair_feasibility.open_anchored_regular_file",
            side_effect=OSError("O_NOFOLLOW unavailable"),
        ):
            result = run_isolated_repair_feasibility_doe()

        probes = {probe["id"]: probe for probe in result["probes"]}
        self.assertEqual(probes["DOE-ANCHOR-LEAF-SYMLINK"]["status"], ProbeStatus.UNSUPPORTED.value)
        self.assertEqual(probes["DOE-ANCHOR-HARDLINK"]["status"], ProbeStatus.UNSUPPORTED.value)
