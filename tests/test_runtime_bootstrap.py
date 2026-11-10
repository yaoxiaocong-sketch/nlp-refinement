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
SCRIPT = ROOT / "scripts" / "nlp_refinement_runtime_bootstrap.py"


def load_module() -> object:
    spec = importlib.util.spec_from_file_location("nlp_refinement_runtime_bootstrap", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RuntimeBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.wheel_bytes = self.make_wheel("0.9.0")
        self.wheel_sha256 = hashlib.sha256(self.wheel_bytes).hexdigest()
        self.release = {
            "schema_version": "nlp-refinement-runtime-release/v1",
            "package_name": "nlp-refinement-cli",
            "version": "0.9.0",
            "requires_python": ">=3.11",
            "plugin": {"name": "nlp-refinement-cli", "version": "0.9.0"},
            "wheel": {
                "url": "https://downloads.example.invalid/nlp_refinement_cli-0.9.0-py3-none-any.whl",
                "sha256": self.wheel_sha256,
                "size_bytes": len(self.wheel_bytes),
            },
            "commands": [
                "nlp-refinement",
                "nlp-refinement-host",
                "nlp-refinement-doe-prune",
            ],
        }

    @staticmethod
    def make_wheel(version: str) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(
                f"nlp_refinement_cli-{version}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: nlp-refinement-cli\nVersion: {version}\n",
            )
        return output.getvalue()

    def write_plugin_manifest(self, root: Path, *, version: str = "0.9.0") -> Path:
        manifest = root / "plugin.json"
        manifest.write_text(json.dumps({"name": "nlp-refinement-cli", "version": version}), encoding="utf-8")
        return manifest

    @staticmethod
    def write_script_dir(root: Path) -> Path:
        script_dir = root / "scripts"
        script_dir.mkdir()
        for command in ("nlp-refinement", "nlp-refinement-host", "nlp-refinement-doe-prune"):
            (script_dir / command).write_text("fixture", encoding="utf-8")
        return script_dir

    def test_plan_verifies_pinned_manifest_and_never_invokes_pip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.write_plugin_manifest(root)
            script_dir = self.write_script_dir(root)
            release_bytes = json.dumps(self.release, sort_keys=True).encode("utf-8")
            commands: list[list[str]] = []

            result = self.module.run(
                release_manifest_url="https://publisher.example.invalid/releases/0.9.0.json",
                release_manifest_sha256=hashlib.sha256(release_bytes).hexdigest(),
                plugin_manifest=manifest,
                apply=False,
                download=lambda url, limit: release_bytes,
                run_command=lambda command: commands.append(command),
                script_dir=script_dir,
            )

        self.assertEqual(result["status"], "PLAN_READY")
        self.assertEqual(result["cli_version"], "0.9.0")
        self.assertEqual(commands, [])

    def test_apply_refuses_a_release_manifest_with_the_wrong_pinned_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.write_plugin_manifest(root)
            script_dir = self.write_script_dir(root)
            commands: list[list[str]] = []

            result = self.module.run(
                release_manifest_url="https://publisher.example.invalid/releases/0.9.0.json",
                release_manifest_sha256="0" * 64,
                plugin_manifest=manifest,
                apply=True,
                download=lambda url, limit: json.dumps(self.release).encode("utf-8"),
                run_command=lambda command: commands.append(command),
                script_dir=script_dir,
            )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["code"], "RELEASE_MANIFEST_HASH_MISMATCH")
        self.assertEqual(commands, [])

    def test_apply_installs_only_a_verified_matching_release_then_checks_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.write_plugin_manifest(root)
            script_dir = self.write_script_dir(root)
            release_bytes = json.dumps(self.release, sort_keys=True).encode("utf-8")
            commands: list[list[str]] = []

            def download(url: str, limit: int) -> bytes:
                return release_bytes if url.endswith(".json") else self.wheel_bytes

            with patch.object(self.module.shutil, "which", side_effect=lambda name: str(script_dir / name)):
                result = self.module.run(
                    release_manifest_url="https://publisher.example.invalid/releases/0.9.0.json",
                    release_manifest_sha256=hashlib.sha256(release_bytes).hexdigest(),
                    plugin_manifest=manifest,
                    apply=True,
                    download=download,
                    run_command=lambda command: commands.append(command),
                    script_dir=script_dir,
                )

        self.assertEqual(result["status"], "INSTALLED")
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0][:8],
            [sys.executable, "-m", "pip", "install", "--user", "--no-deps", "--no-index", "--disable-pip-version-check"],
        )
        self.assertEqual(result["commands"], self.release["commands"])

    def test_apply_stops_after_wheel_hash_mismatch_before_pip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.write_plugin_manifest(root)
            script_dir = self.write_script_dir(root)
            release_bytes = json.dumps(self.release, sort_keys=True).encode("utf-8")
            commands: list[list[str]] = []

            def download(url: str, limit: int) -> bytes:
                return release_bytes if url.endswith(".json") else (b"x" * len(self.wheel_bytes))

            result = self.module.run(
                release_manifest_url="https://publisher.example.invalid/releases/0.9.0.json",
                release_manifest_sha256=hashlib.sha256(release_bytes).hexdigest(),
                plugin_manifest=manifest,
                apply=True,
                download=download,
                run_command=lambda command: commands.append(command),
                script_dir=script_dir,
            )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["code"], "WHEEL_HASH_MISMATCH")
        self.assertEqual(commands, [])

    def test_apply_checks_wheel_metadata_before_pip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.write_plugin_manifest(root)
            script_dir = self.write_script_dir(root)
            wrong_wheel = self.make_wheel("9.9.9")
            self.release["wheel"] = {
                "url": "https://downloads.example.invalid/nlp_refinement_cli-0.9.0-py3-none-any.whl",
                "sha256": hashlib.sha256(wrong_wheel).hexdigest(),
                "size_bytes": len(wrong_wheel),
            }
            release_bytes = json.dumps(self.release, sort_keys=True).encode("utf-8")
            commands: list[list[str]] = []

            result = self.module.run(
                release_manifest_url="https://publisher.example.invalid/releases/0.9.0.json",
                release_manifest_sha256=hashlib.sha256(release_bytes).hexdigest(),
                plugin_manifest=manifest,
                apply=True,
                download=lambda url, limit: release_bytes if url.endswith(".json") else wrong_wheel,
                run_command=lambda command: commands.append(command),
                script_dir=script_dir,
            )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["code"], "WHEEL_METADATA_INCOMPATIBLE")
        self.assertEqual(commands, [])

    def test_rejects_non_https_release_urls_before_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.write_plugin_manifest(Path(directory))
            script_dir = self.write_script_dir(Path(directory))
            called = False

            def download(_: str, limit: int) -> bytes:
                nonlocal called
                called = True
                return b""

            result = self.module.run(
                release_manifest_url="file:///tmp/release.json",
                release_manifest_sha256="0" * 64,
                plugin_manifest=manifest,
                apply=False,
                download=download,
                run_command=lambda command: None,
                script_dir=script_dir,
            )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["code"], "RELEASE_URL_NOT_HTTPS")
        self.assertFalse(called)

    def test_apply_rejects_a_stale_command_found_earlier_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.write_plugin_manifest(root)
            script_dir = self.write_script_dir(root)
            stale_dir = root / "stale"
            stale_dir.mkdir()
            for command in self.release["commands"]:
                (stale_dir / command).write_text("old", encoding="utf-8")
            release_bytes = json.dumps(self.release, sort_keys=True).encode("utf-8")

            with patch.object(self.module.shutil, "which", side_effect=lambda name: str(stale_dir / name)):
                result = self.module.run(
                    release_manifest_url="https://publisher.example.invalid/releases/0.9.0.json",
                    release_manifest_sha256=hashlib.sha256(release_bytes).hexdigest(),
                    plugin_manifest=manifest,
                    apply=True,
                    download=lambda url, limit: release_bytes if url.endswith(".json") else self.wheel_bytes,
                    run_command=lambda command: None,
                    script_dir=script_dir,
                )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["code"], "CLI_PATH_MISMATCH")


if __name__ == "__main__":
    unittest.main()
