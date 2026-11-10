from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "nlp_refinement_runtime_setup.py"


def load_module() -> object:
    spec = importlib.util.spec_from_file_location("nlp_refinement_runtime_setup", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RuntimeSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    @staticmethod
    def write_plugin_root(root: Path) -> Path:
        root.mkdir()
        (root / "plugin.json").write_text(
            json.dumps({"name": "nlp-refinement-cli", "version": "0.9.4"}), encoding="utf-8"
        )
        (root / "runtime-bootstrap-config.json").write_text(
            json.dumps({
                "schema_version": "nlp-refinement-runtime-bootstrap/v1",
                "plugin_name": "nlp-refinement-cli",
                "plugin_version": "0.9.4",
                "release_manifest": {
                    "url": "https://github.com/example/nlp-refinement/releases/download/v0.9.4/release.json",
                    "sha256": "a" * 64,
                },
            }),
            encoding="utf-8",
        )
        return root

    def test_matching_path_commands_finish_without_download_or_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.write_plugin_root(Path(directory) / "plugin")
            with patch.object(self.module, "_command_version", return_value="0.9.4"), \
                 patch.object(self.module.bootstrap, "run") as bootstrap_run:
                result = self.module.run(plugin_root=plugin_root, apply=False)

        self.assertEqual(result["status"], "ALREADY_INSTALLED")
        bootstrap_run.assert_not_called()

    def test_old_or_missing_commands_delegate_only_to_the_pinned_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.write_plugin_root(Path(directory) / "plugin")
            bootstrap_result = {"status": "PLAN_READY", "requires_confirmation": True}
            expected = {
                "status": "PLAN_READY",
                "requires_confirmation": True,
                "release_manifest_url": "https://github.com/example/nlp-refinement/releases/download/v0.9.4/release.json",
                "release_manifest_sha256": "a" * 64,
            }
            with patch.object(self.module, "_command_version", side_effect=["0.8.2", None, None]), \
                 patch.object(self.module.bootstrap, "run", return_value=bootstrap_result) as bootstrap_run:
                result = self.module.run(plugin_root=plugin_root, apply=False)

        self.assertEqual(result, expected)
        self.assertEqual(bootstrap_run.call_args.kwargs["release_manifest_url"], "https://github.com/example/nlp-refinement/releases/download/v0.9.4/release.json")
        self.assertEqual(bootstrap_run.call_args.kwargs["release_manifest_sha256"], "a" * 64)
        self.assertEqual(bootstrap_run.call_args.kwargs["plugin_manifest"], plugin_root / "plugin.json")
        self.assertFalse(bootstrap_run.call_args.kwargs["apply"])

    def test_relative_or_incomplete_plugin_configuration_stops_without_bootstrap(self) -> None:
        result = self.module.run(plugin_root=Path("relative-plugin-root"), apply=False)
        self.assertEqual(result["code"], "BOOTSTRAP_ROOT_OR_RELEASE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
