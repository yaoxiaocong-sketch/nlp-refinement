from __future__ import annotations

import json
from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PluginPackagingTests(unittest.TestCase):
    def test_portable_and_codex_compatibility_manifests_share_one_skill_package(self) -> None:
        portable = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        compatibility = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        with (ROOT / "pyproject.toml").open("rb") as handle:
            distribution = tomllib.load(handle)

        self.assertEqual(portable["name"], "nlp-refinement-cli")
        self.assertEqual(portable["version"], compatibility["version"])
        self.assertEqual(portable["version"], distribution["project"]["version"])
        self.assertEqual(portable["description"], compatibility["description"])
        self.assertEqual(portable["$schema"], "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
        self.assertTrue(portable["extensions"]["com.openai"]["requires_local_executor"])
        self.assertTrue(compatibility["requires_local_executor"])
        self.assertEqual(
            portable["extensions"]["com.openai"]["interface"],
            compatibility["interface"],
        )
        self.assertEqual(compatibility["skills"], "./.agents/skills/")
        self.assertTrue((ROOT / ".agents" / "skills" / "nlp-review-propose" / "SKILL.md").is_file())
        self.assertFalse((ROOT / "skills" / "nlp-refine" / "SKILL.md").exists())
        self.assertEqual(
            portable["extensions"]["com.openai"]["onboardingSkill"],
            "./.agents/skills/nlp-refine-setup/SKILL.md",
        )
        self.assertEqual(compatibility["onboardingSkill"], "./.agents/skills/nlp-refine-setup/SKILL.md")
        self.assertTrue((ROOT / ".agents" / "skills" / "nlp-refine-setup" / "SKILL.md").is_file())
        self.assertTrue((ROOT / "runtime-bootstrap-config.json").is_file())
        self.assertTrue((ROOT / "scripts" / "nlp_refinement_runtime_setup.py").is_file())
        self.assertIn("$nlp-review-propose", compatibility["interface"]["defaultPrompt"][0])
        self.assertIn(
            "nlp_refinement_runtime_setup.py",
            (ROOT / ".agents" / "skills" / "nlp-refine-setup" / "SKILL.md").read_text(encoding="utf-8"),
        )
        self.assertFalse((ROOT / ".agents" / "skills" / "nlp-refine" / "SKILL.md").exists())

    def test_authoritative_documents_describe_the_tui_native_boundary(self) -> None:
        for path in [ROOT / "AGENTS.md", ROOT / "README.md"]:
            text = path.read_text(encoding="utf-8")
            self.assertIn("human-only", text, path)
            self.assertTrue("no target write" in text or "no_target_write" in text, path)
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertNotIn("host-execution-request", agents)
        self.assertIn("does not create child threads", agents)


if __name__ == "__main__":
    unittest.main()
