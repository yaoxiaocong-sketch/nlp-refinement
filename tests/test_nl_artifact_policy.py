from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nlp_refinement.artifact_inventory import scan_artifact_inventory
from nlp_refinement.state_store import StateStore


class NLArtifactPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "project"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_codex_assets_are_auto_and_only_recognized_restricted_assets_are_reported(self) -> None:
        skill = self.root / ".agents/skills/reviewer"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Reviewer\n", encoding="utf-8")
        (skill / "agents").mkdir()
        (skill / "agents/openai.yaml").write_text("interface:\n  display_name: Reviewer\n", encoding="utf-8")
        (self.root / ".codex/agents").mkdir(parents=True)
        (self.root / ".codex/agents/legacy.toml").write_bytes(b"\xff\x00")
        prompt_directory = self.root / ".codex/prompts"
        prompt_directory.mkdir(parents=True)
        (prompt_directory / "large.md").write_bytes(b"x" * 1_000_001)
        (self.root / "README.md").write_text("ordinary documentation\n", encoding="utf-8")
        test_skill = self.root / "tests/.agents/skills/sample"
        test_skill.mkdir(parents=True)
        (test_skill / "SKILL.md").write_text("fixture\n", encoding="utf-8")

        inventory = scan_artifact_inventory(self.root)

        self.assertEqual(inventory["schema_version"], "3.0")
        self.assertEqual(
            [entry["relative_path"] for entry in inventory["auto"]],
            [
                ".agents/skills/reviewer/SKILL.md",
                ".agents/skills/reviewer/agents/openai.yaml",
            ],
        )
        self.assertEqual(
            [(entry["relative_path"], entry["restriction_type"]) for entry in inventory["restricted"]],
            [
                (".codex/agents/legacy.toml", "not_utf8"),
                (".codex/prompts/large.md", "exceeds_default_size_limit"),
            ],
        )
        self.assertNotIn("README.md", [entry["relative_path"] for entry in inventory["auto"]])
        self.assertNotIn("tests/.agents/skills/sample/SKILL.md", [entry["relative_path"] for entry in inventory["auto"]])
        self.assertIn("README.md", {entry["relative_path"] for entry in inventory["excluded"]})

    def test_restricted_changes_do_not_stale_auto_inventory(self) -> None:
        skill = self.root / ".agents/skills/reviewer"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Reviewer\n", encoding="utf-8")
        agent_dir = self.root / ".codex/agents"
        agent_dir.mkdir(parents=True)
        restricted = agent_dir / "legacy.toml"
        restricted.write_bytes(b"\xff\x00")

        first = scan_artifact_inventory(self.root)
        restricted.write_bytes(b"\xff\x01")
        second = scan_artifact_inventory(self.root)

        self.assertEqual(first["inventory_hash"], second["inventory_hash"])

    def test_project_declaration_admits_only_exactly_declared_nonstandard_assets(self) -> None:
        declaration_directory = self.root / ".nlp-refinement"
        declaration_directory.mkdir()
        workflow = self.root / "workflow/release-review.yaml"
        workflow.parent.mkdir()
        workflow.write_text("steps: []\n", encoding="utf-8")
        (self.root / "workflow/ordinary.yaml").write_text("steps: []\n", encoding="utf-8")
        (declaration_directory / "nl-artifacts.json").write_text(
            """{
  "schema_version": "nl-artifact-declaration/v1",
  "policy_id": "nl-artifact-policy/v1",
  "entries": [{
    "relative_path": "workflow/release-review.yaml",
    "artifact_kind": "release_workflow",
    "expected_extension": ".yaml",
    "reason": "release workflow reviewed by this project"
  }]
}
""",
            encoding="utf-8",
        )

        inventory = scan_artifact_inventory(self.root)

        self.assertEqual([item["relative_path"] for item in inventory["auto"]], ["workflow/release-review.yaml"])
        self.assertEqual(inventory["auto"][0]["primary_rule"]["provider_id"], "project-declaration")
        self.assertEqual(len(inventory["declaration_sha256"]), 64)
        self.assertIn(
            {"relative_path": "workflow/ordinary.yaml", "reason": "no-policy-rule"},
            inventory["excluded"],
        )


class CapabilityReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temporary_directory.name) / "state.db")
        self.store.initialize()
        self.cluster = {
            "policy_id": "nl-artifact-policy/v1",
            "provider_id": "codex",
            "provider_version": "1",
            "rule_id": "codex-skill",
            "artifact_kind": "skill",
            "restriction_type": "exceeds_default_size_limit",
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_first_occurrence_opens_once_and_first_new_path_after_acknowledgement_reopens(self) -> None:
        first = self.store.observe_capability_occurrence(
            self.cluster, project_key="a" * 64, relative_path=".agents/skills/a/SKILL.md",
            byte_count=1_000_001, utf8_status="not-read", now=1,
        )
        self.assertEqual(first["event"]["state"], "OPEN")
        self.assertTrue(first["event_created"])
        with self.store._connection() as connection:
            stored = connection.execute(
                "SELECT byte_count, utf8_status FROM capability_occurrences WHERE occurrence_id = ?",
                (first["occurrence_id"],),
            ).fetchone()
        self.assertEqual((stored["byte_count"], stored["utf8_status"]), (1_000_001, "not-read"))

        repeated = self.store.observe_capability_occurrence(
            self.cluster, project_key="a" * 64, relative_path=".agents/skills/a/SKILL.md",
            byte_count=1_000_002, utf8_status="not-read", now=2,
        )
        self.assertFalse(repeated["occurrence_created"])
        self.assertFalse(repeated["event_created"])
        with self.store._connection() as connection:
            stored = connection.execute(
                "SELECT byte_count, utf8_status, seen_count FROM capability_occurrences WHERE occurrence_id = ?",
                (first["occurrence_id"],),
            ).fetchone()
        self.assertEqual((stored["byte_count"], stored["utf8_status"], stored["seen_count"]), (1_000_002, "not-read", 2))
        self.assertEqual(len(self.store.list_capability_events()), 1)

        acknowledged = self.store.acknowledge_capability_event(
            first["event"]["event_id"], decision="defer", recorded_via="test", now=3
        )
        self.assertEqual(acknowledged["state"], "ACKNOWLEDGED")

        reopened = self.store.observe_capability_occurrence(
            self.cluster, project_key="a" * 64, relative_path=".agents/skills/b/SKILL.md",
            byte_count=1_000_001, utf8_status="not-read", now=4,
        )
        self.assertTrue(reopened["event_created"])
        self.assertEqual(reopened["event"]["state"], "OPEN")
        self.assertEqual(reopened["event"]["reopened_from_event_id"], first["event"]["event_id"])
        self.assertEqual(len(self.store.list_capability_events()), 2)


if __name__ == "__main__":
    unittest.main()
