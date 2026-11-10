#!/usr/bin/env python3
"""Prepare the pinned Plugin release records for one already-built wheel.

This script deliberately does not build a distribution.  The publisher builds
one wheel first, then this script reads those exact bytes and writes the two
non-package records consumed by the Plugin bootstrap: the release manifest and
its hash-pinning configuration.  It refuses to replace an existing versioned
release manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile
import tomllib
import zipfile


PACKAGE_NAME = "nlp-refinement-cli"
PLUGIN_NAME = "nlp-refinement-cli"
REPOSITORY = "yaoxiaocong-sketch/nlp-refinement"
RELEASE_SCHEMA = "nlp-refinement-runtime-release/v1"
BOOTSTRAP_SCHEMA = "nlp-refinement-runtime-bootstrap/v1"
REQUIRED_COMMANDS = (
    "nlp-refinement",
    "nlp-refinement-host",
    "nlp-refinement-doe-prune",
)
VERSION_PATTERN = re.compile(r'^CLI_VERSION\s*=\s*"(?P<version>[^"]+)"\s*$', re.MULTILINE)


def _blocked(code: str, **details: object) -> dict[str, object]:
    return {"status": "BLOCKED", "code": code, **details}


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _source_version(root: Path) -> str | None:
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            package = tomllib.load(handle)
        pyproject_version = package["project"]["version"]
        plugin = _read_json(root / "plugin.json")
        codex_plugin = _read_json(root / ".codex-plugin" / "plugin.json")
        version_source = (root / "nlp_refinement" / "version.py").read_text(encoding="utf-8")
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return None
    match = VERSION_PATTERN.search(version_source)
    values = (
        pyproject_version,
        plugin.get("version") if plugin else None,
        codex_plugin.get("version") if codex_plugin else None,
        match.group("version") if match else None,
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    return str(values[0]) if len(set(values)) == 1 else None


def _wheel_metadata(path: Path) -> tuple[str, str] | None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                return None
            metadata = archive.read(names[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
        return None
    fields: dict[str, str] = {}
    for line in metadata.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    name, version = fields.get("Name"), fields.get("Version")
    return (name, version) if isinstance(name, str) and isinstance(version, str) else None


def _release_bytes(*, version: str, wheel: Path) -> bytes:
    wheel_name = wheel.name
    payload = {
        "commands": list(REQUIRED_COMMANDS),
        "package_name": PACKAGE_NAME,
        "plugin": {"name": PLUGIN_NAME, "version": version},
        "requires_python": ">=3.11",
        "schema_version": RELEASE_SCHEMA,
        "version": version,
        "wheel": {
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "size_bytes": wheel.stat().st_size,
            "url": f"https://github.com/{REPOSITORY}/releases/download/v{version}/{wheel_name}",
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _config_bytes(*, version: str, manifest_name: str, release_bytes: bytes) -> bytes:
    payload = {
        "schema_version": BOOTSTRAP_SCHEMA,
        "plugin_name": PLUGIN_NAME,
        "plugin_version": version,
        "release_manifest": {
            "url": f"https://github.com/{REPOSITORY}/releases/download/v{version}/{manifest_name}",
            "sha256": hashlib.sha256(release_bytes).hexdigest(),
            "source_path": f"releases/{manifest_name}",
        },
    }
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run(*, root: Path, wheel: Path, apply: bool) -> dict[str, object]:
    if not root.is_absolute() or not root.is_dir() or not wheel.is_absolute() or not wheel.is_file():
        return _blocked("RELEASE_INPUT_UNAVAILABLE")
    version = _source_version(root)
    if version is None:
        return _blocked("RELEASE_VERSION_MISMATCH")
    metadata = _wheel_metadata(wheel)
    if metadata != (PACKAGE_NAME, version):
        return _blocked("WHEEL_METADATA_INCOMPATIBLE")

    manifest_name = f"nlp-refinement-runtime-{version}.json"
    manifest_path = root / "releases" / manifest_name
    config_path = root / "runtime-bootstrap-config.json"
    release_bytes = _release_bytes(version=version, wheel=wheel)
    if manifest_path.exists():
        try:
            existing_manifest = manifest_path.read_bytes()
        except OSError:
            return _blocked("RELEASE_MANIFEST_UNREADABLE", manifest_path=str(manifest_path))
        if existing_manifest != release_bytes:
            return _blocked("RELEASE_MANIFEST_ALREADY_EXISTS", manifest_path=str(manifest_path))
    config_bytes = _config_bytes(version=version, manifest_name=manifest_name, release_bytes=release_bytes)
    plan = {
        "status": "PLAN_READY",
        "version": version,
        "wheel": {
            "path": str(wheel),
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "size_bytes": wheel.stat().st_size,
        },
        "release_manifest": {
            "path": str(manifest_path),
            "sha256": hashlib.sha256(release_bytes).hexdigest(),
        },
        "writes": ([] if manifest_path.exists() else [str(manifest_path)]) + [str(config_path)],
        "next_action": "Rerun with --apply only after reviewing this exact built wheel and generated release records.",
    }
    if not apply:
        return plan

    if not manifest_path.exists():
        _write_new(manifest_path, release_bytes)
    _write_new(config_path, config_bytes)
    return {**plan, "status": "PREPARED", "next_action": "Commit these records, upload this exact wheel and manifest to the GitHub Release, then run the manual PyPI workflow."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare pinned Plugin release records for one existing wheel.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Write the planned release manifest and bootstrap configuration.")
    args = parser.parse_args(argv)
    result = run(root=args.root, wheel=args.wheel, apply=args.apply)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"PLAN_READY", "PREPARED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
