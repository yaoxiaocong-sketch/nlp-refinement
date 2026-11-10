#!/usr/bin/env python3
"""Resolve a Plugin-owned pinned release plan before invoking the bootstrap."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = Path(__file__).with_name("nlp_refinement_runtime_bootstrap.py")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nlp_refinement.version import CLI_VERSION

_bootstrap_spec = importlib.util.spec_from_file_location("nlp_refinement_runtime_bootstrap", BOOTSTRAP_PATH)
assert _bootstrap_spec is not None and _bootstrap_spec.loader is not None
bootstrap = importlib.util.module_from_spec(_bootstrap_spec)
sys.modules[_bootstrap_spec.name] = bootstrap
_bootstrap_spec.loader.exec_module(bootstrap)


CONFIG_NAME = "runtime-bootstrap-config.json"


def _blocked(code: str) -> dict[str, object]:
    return {
        "status": "BLOCKED",
        "code": code,
        "completed_stages": [],
        "next_action": "Install a matching Plugin release with a valid pinned runtime configuration, then start a fresh setup plan.",
    }


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _is_https(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def _load_configuration(plugin_root: Path) -> tuple[Path, str, str] | None:
    if not plugin_root.is_absolute() or not plugin_root.is_dir():
        return None
    manifest = plugin_root / "plugin.json"
    configuration = plugin_root / CONFIG_NAME
    try:
        plugin = json.loads(manifest.read_text(encoding="utf-8"))
        config = json.loads(configuration.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(plugin, dict) or not isinstance(config, dict):
        return None
    release = config.get("release_manifest")
    if not isinstance(release, dict):
        return None
    if not (
        plugin.get("name") == "nlp-refinement-cli"
        and plugin.get("version") == CLI_VERSION
        and config.get("schema_version") == "nlp-refinement-runtime-bootstrap/v1"
        and config.get("plugin_name") == plugin["name"]
        and config.get("plugin_version") == plugin["version"]
        and _is_https(release.get("url"))
        and _is_sha256(release.get("sha256"))
    ):
        return None
    return manifest, str(release["url"]), str(release["sha256"])


def _command_version(command: str) -> str | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def run(*, plugin_root: Path, apply: bool) -> dict[str, object]:
    configuration = _load_configuration(plugin_root)
    if configuration is None:
        return _blocked("BOOTSTRAP_ROOT_OR_RELEASE_UNAVAILABLE")
    manifest, release_manifest_url, release_manifest_sha256 = configuration
    commands = tuple(bootstrap.REQUIRED_COMMANDS)
    observed_versions = {command: _command_version(command) for command in commands}
    if all(version == CLI_VERSION for version in observed_versions.values()):
        return {
            "status": "ALREADY_INSTALLED",
            "cli_version": CLI_VERSION,
            "plugin_version": CLI_VERSION,
            "commands": list(commands),
            "completed_stages": ["commands-visible"],
        }
    return bootstrap.run(
        release_manifest_url=release_manifest_url,
        release_manifest_sha256=release_manifest_sha256,
        plugin_manifest=manifest,
        apply=apply,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or apply the Plugin-owned pinned NLP-refinement CLI installation.")
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Install only after the user confirms the displayed plan.")
    args = parser.parse_args(argv)
    result = run(plugin_root=args.plugin_root, apply=args.apply)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"ALREADY_INSTALLED", "PLAN_READY", "INSTALLED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
