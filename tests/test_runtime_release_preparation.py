from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PREPARE_SCRIPT = ROOT / "scripts" / "prepare_runtime_release.py"
FETCH_SCRIPT = ROOT / "scripts" / "fetch_verified_runtime_release.py"


def load_module(name: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def wheel_bytes(version: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            f"nlp_refinement_cli-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: nlp-refinement-cli\nVersion: {version}\n",
        )
    return output.getvalue()


def source_root(root: Path, version: str) -> None:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "nlp_refinement").mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\nname = \"nlp-refinement-cli\"\nversion = \"%s\"\n" % version,
        encoding="utf-8",
    )
    for relative in ("plugin.json", ".codex-plugin/plugin.json"):
        (root / relative).write_text(
            json.dumps({"name": "nlp-refinement-cli", "version": version}), encoding="utf-8"
        )
    (root / "nlp_refinement/version.py").write_text(f'CLI_VERSION = "{version}"\n', encoding="utf-8")


class RuntimeReleasePreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prepare = load_module("prepare_runtime_release", PREPARE_SCRIPT)
        self.fetch = load_module("fetch_verified_runtime_release", FETCH_SCRIPT)

    def test_plan_reads_one_built_wheel_without_writing_release_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            source_root(root, "1.2.3")
            wheel = root / "nlp_refinement_cli-1.2.3-py3-none-any.whl"
            wheel.write_bytes(wheel_bytes("1.2.3"))

            result = self.prepare.run(root=root, wheel=wheel, apply=False)

            self.assertEqual(result["status"], "PLAN_READY")
            self.assertEqual(result["version"], "1.2.3")
            self.assertEqual(result["wheel"]["sha256"], hashlib.sha256(wheel.read_bytes()).hexdigest())
            self.assertFalse((root / "releases").exists())
            self.assertFalse((root / "runtime-bootstrap-config.json").exists())

    def test_apply_writes_a_manifest_and_config_for_the_same_wheel_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            source_root(root, "1.2.3")
            wheel = root / "nlp_refinement_cli-1.2.3-py3-none-any.whl"
            wheel.write_bytes(wheel_bytes("1.2.3"))

            result = self.prepare.run(root=root, wheel=wheel, apply=True)

            manifest_path = root / "releases/nlp-refinement-runtime-1.2.3.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            config = json.loads((root / "runtime-bootstrap-config.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "PREPARED")
            self.assertEqual(manifest["wheel"]["sha256"], hashlib.sha256(wheel.read_bytes()).hexdigest())
            self.assertEqual(manifest["wheel"]["size_bytes"], len(wheel.read_bytes()))
            self.assertEqual(config["plugin_version"], "1.2.3")
            self.assertEqual(config["release_manifest"]["sha256"], hashlib.sha256(manifest_path.read_bytes()).hexdigest())

    def test_refuses_a_wheel_with_different_embedded_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            source_root(root, "1.2.3")
            wheel = root / "nlp_refinement_cli-1.2.3-py3-none-any.whl"
            wheel.write_bytes(wheel_bytes("9.9.9"))

            result = self.prepare.run(root=root, wheel=wheel, apply=False)

            self.assertEqual(result["code"], "WHEEL_METADATA_INCOMPATIBLE")

    def test_refuses_to_replace_a_different_existing_versioned_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            source_root(root, "1.2.3")
            wheel = root / "nlp_refinement_cli-1.2.3-py3-none-any.whl"
            wheel.write_bytes(wheel_bytes("1.2.3"))
            existing = root / "releases/nlp-refinement-runtime-1.2.3.json"
            existing.parent.mkdir()
            existing.write_text("already public", encoding="utf-8")

            result = self.prepare.run(root=root, wheel=wheel, apply=True)

            self.assertEqual(result["code"], "RELEASE_MANIFEST_ALREADY_EXISTS")
            self.assertEqual(existing.read_text(encoding="utf-8"), "already public")

    def test_matching_existing_manifest_allows_recovery_of_the_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            source_root(root, "1.2.3")
            wheel = root / "nlp_refinement_cli-1.2.3-py3-none-any.whl"
            wheel.write_bytes(wheel_bytes("1.2.3"))
            release = self.prepare.run(root=root, wheel=wheel, apply=True)
            (root / "runtime-bootstrap-config.json").unlink()

            recovered = self.prepare.run(root=root, wheel=wheel, apply=True)

            self.assertEqual(recovered["status"], "PREPARED")
            self.assertEqual(recovered["writes"], [str(root / "runtime-bootstrap-config.json")])
            self.assertTrue((root / "runtime-bootstrap-config.json").is_file())

    def test_fetcher_stages_exactly_the_manifest_pinned_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            root.mkdir()
            version = "1.2.3"
            payload = wheel_bytes(version)
            release = {
                "schema_version": "nlp-refinement-runtime-release/v1",
                "package_name": "nlp-refinement-cli",
                "version": version,
                "requires_python": ">=3.11",
                "plugin": {"name": "nlp-refinement-cli", "version": version},
                "wheel": {
                    "url": "https://downloads.example.invalid/nlp_refinement_cli-1.2.3-py3-none-any.whl",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                },
                "commands": ["nlp-refinement", "nlp-refinement-host", "nlp-refinement-doe-prune"],
            }
            release_bytes = json.dumps(release, sort_keys=True).encode("utf-8")
            (root / "plugin.json").write_text(
                json.dumps({"name": "nlp-refinement-cli", "version": version}), encoding="utf-8"
            )
            (root / "runtime-bootstrap-config.json").write_text(
                json.dumps({
                    "schema_version": "nlp-refinement-runtime-bootstrap/v1",
                    "plugin_name": "nlp-refinement-cli",
                    "plugin_version": version,
                    "release_manifest": {
                        "url": "https://downloads.example.invalid/release.json",
                        "sha256": hashlib.sha256(release_bytes).hexdigest(),
                    },
                }),
                encoding="utf-8",
            )
            output = root / "dist"
            with patch.object(
                self.fetch.bootstrap,
                "_download",
                side_effect=lambda url, _: release_bytes if url.endswith(".json") else payload,
            ):
                result = self.fetch.run(plugin_root=root, output_dir=output)

            staged = output / "nlp_refinement_cli-1.2.3-py3-none-any.whl"
            self.assertEqual(result["status"], "VERIFIED")
            self.assertEqual(staged.read_bytes(), payload)

    def test_fetcher_refuses_a_wheel_hash_mismatch_without_staging_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            root.mkdir()
            version = "1.2.3"
            payload = wheel_bytes(version)
            release = {
                "schema_version": "nlp-refinement-runtime-release/v1",
                "package_name": "nlp-refinement-cli",
                "version": version,
                "requires_python": ">=3.11",
                "plugin": {"name": "nlp-refinement-cli", "version": version},
                "wheel": {
                    "url": "https://downloads.example.invalid/nlp_refinement_cli-1.2.3-py3-none-any.whl",
                    "sha256": "0" * 64,
                    "size_bytes": len(payload),
                },
                "commands": ["nlp-refinement", "nlp-refinement-host", "nlp-refinement-doe-prune"],
            }
            release_bytes = json.dumps(release, sort_keys=True).encode("utf-8")
            (root / "plugin.json").write_text(json.dumps({"name": "nlp-refinement-cli", "version": version}), encoding="utf-8")
            (root / "runtime-bootstrap-config.json").write_text(
                json.dumps({
                    "schema_version": "nlp-refinement-runtime-bootstrap/v1",
                    "plugin_name": "nlp-refinement-cli",
                    "plugin_version": version,
                    "release_manifest": {
                        "url": "https://downloads.example.invalid/release.json",
                        "sha256": hashlib.sha256(release_bytes).hexdigest(),
                    },
                }),
                encoding="utf-8",
            )
            output = root / "dist"
            with patch.object(
                self.fetch.bootstrap,
                "_download",
                side_effect=lambda url, _: release_bytes if url.endswith(".json") else payload,
            ):
                result = self.fetch.run(plugin_root=root, output_dir=output)

            self.assertEqual(result["code"], "WHEEL_HASH_MISMATCH")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
