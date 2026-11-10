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


if __name__ == "__main__":
    unittest.main()
