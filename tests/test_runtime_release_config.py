from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "runtime-bootstrap-config.json"


class RuntimeReleaseConfigTests(unittest.TestCase):
    def test_plugin_config_pins_the_checked_in_matching_release_manifest(self) -> None:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        with (ROOT / "pyproject.toml").open("rb") as handle:
            package = tomllib.load(handle)
        version = package["project"]["version"]

        self.assertEqual(config["schema_version"], "nlp-refinement-runtime-bootstrap/v1")
        self.assertEqual(config["plugin_name"], "nlp-refinement-cli")
        self.assertEqual(config["plugin_version"], version)
        manifest_path = ROOT / config["release_manifest"]["source_path"]
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(
            config["release_manifest"]["sha256"],
            hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )
        release = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(release["version"], version)
        self.assertEqual(release["plugin"], {"name": "nlp-refinement-cli", "version": version})
        self.assertEqual(release["schema_version"], "nlp-refinement-runtime-release/v3")
        self.assertEqual(release["commands"], ["nlp-refinement"])
        self.assertEqual(release["tui_native_capability"]["interface_version"], "nlp-refinement-tui-native/v1")
        self.assertIn("runtime-identity", release["tui_native_capability"]["required_subcommands"])

    def test_historical_v2_manifest_retains_its_original_three_command_meaning(self) -> None:
        historical = json.loads(
            (ROOT / "releases" / "nlp-refinement-runtime-0.11.0.json").read_text(encoding="utf-8")
        )

        self.assertEqual(historical["schema_version"], "nlp-refinement-runtime-release/v2")
        self.assertEqual(
            historical["commands"],
            ["nlp-refinement", "nlp-refinement-host", "nlp-refinement-doe-prune"],
        )


if __name__ == "__main__":
    unittest.main()
