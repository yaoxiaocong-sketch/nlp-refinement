from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

from nlp_refinement.artifact_inventory import scan_artifact_inventory


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_SPEC = importlib.util.spec_from_file_location("m0_validator", ROOT / "scripts" / "validate_m0_contract.py")
assert VALIDATOR_SPEC is not None and VALIDATOR_SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(VALIDATOR)


def tree_digest(root: Path) -> str:
    """A test-only tree digest that detects scanner writes to the fixture."""
    records: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            records.append((relative, "symlink:" + os.readlink(path)))
        elif path.is_file():
            records.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        elif path.is_dir():
            records.append((relative + "/", "directory"))
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode("utf-8")).hexdigest()


class ArtifactInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "project"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("# Project rules\n", encoding="utf-8")
        (self.root / "plugin.json").write_text('{"name":"fixture"}\n', encoding="utf-8")
        (self.root / "bad.json").write_text("{not json}\n", encoding="utf-8")
        (self.root / "config.toml").write_text('name = "fixture"\n', encoding="utf-8")
        (self.root / "workflow.yaml").write_text("steps:\n  - review\n", encoding="utf-8")
        (self.root / "binary.md").write_bytes(b"\xff\x00")
        (self.root / "notes.txt").write_text("not a supported NL artifact\n", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config.json").write_text("{}\n", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "ignored.md").write_text("ignored\n", encoding="utf-8")
        (self.root / "linked.md").symlink_to(self.root / "AGENTS.md")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_scan_is_deterministic_read_only_and_classifies_candidates(self) -> None:
        before = tree_digest(self.root)

        first = scan_artifact_inventory(self.root)
        second = scan_artifact_inventory(self.root)

        self.assertEqual(first, second)
        self.assertEqual(tree_digest(self.root), before)
        self.assertEqual(first["schema_version"], "1.0")
        self.assertEqual(first["scan_status"], "complete")
        schema = json.loads(
            (ROOT / "nlp_refinement/resources/schemas/inventory/v1/artifact-inventory.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(VALIDATOR.validate_instance(first, schema), [])
        self.assertEqual(
            [candidate["relative_path"] for candidate in first["candidates"]],
            ["AGENTS.md", "bad.json", "config.toml", "plugin.json", "workflow.yaml"],
        )
        candidates = {candidate["relative_path"]: candidate for candidate in first["candidates"]}
        self.assertEqual(candidates["AGENTS.md"]["artifact_type"], "markdown")
        self.assertEqual(candidates["plugin.json"]["format_status"], "valid")
        self.assertEqual(candidates["bad.json"]["format_status"], "invalid")
        self.assertEqual(candidates["config.toml"]["format_status"], "valid")
        self.assertEqual(candidates["workflow.yaml"]["format_status"], "unvalidated")
        self.assertTrue(all(candidate["utf8_status"] == "valid" for candidate in first["candidates"]))
        self.assertTrue(all(len(candidate["sha256"]) == 64 for candidate in first["candidates"]))
        self.assertEqual(
            first["excluded"],
            [
                {"relative_path": ".git/", "reason": "excluded-directory"},
                {"relative_path": "binary.md", "reason": "not-utf8"},
                {"relative_path": "linked.md", "reason": "symbolic-link"},
                {"relative_path": "node_modules/", "reason": "excluded-directory"},
                {"relative_path": "notes.txt", "reason": "unsupported-extension"},
            ],
        )
        self.assertNotIn("content", json.dumps(first))

    def test_scan_rejects_a_symlink_root(self) -> None:
        alias = self.root.parent / "project-alias"
        alias.symlink_to(self.root, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "non-symlink directory"):
            scan_artifact_inventory(alias)


if __name__ == "__main__":
    unittest.main()
