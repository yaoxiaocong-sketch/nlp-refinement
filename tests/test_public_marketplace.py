from __future__ import annotations

import json
from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicMarketplaceTests(unittest.TestCase):
    def test_public_marketplace_pins_the_root_plugin_to_its_release_tag(self) -> None:
        marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
        with (ROOT / "pyproject.toml").open("rb") as handle:
            version = tomllib.load(handle)["project"]["version"]

        self.assertEqual(marketplace["name"], "nlp-refinement")
        self.assertEqual(len(marketplace["plugins"]), 1)
        plugin = marketplace["plugins"][0]
        self.assertEqual(plugin["name"], "nlp-refinement-cli")
        self.assertEqual(plugin["source"], {
            "source": "url",
            "url": "https://github.com/yaoxiaocong-sketch/nlp-refinement.git",
            "ref": f"v{version}",
        })
        self.assertEqual(plugin["policy"], {"installation": "AVAILABLE", "authentication": "ON_INSTALL"})
        self.assertEqual(plugin["category"], "Productivity")


if __name__ == "__main__":
    unittest.main()
