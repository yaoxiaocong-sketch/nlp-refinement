#!/usr/bin/env python3
"""Download the one GitHub Release wheel after validating its pinned records."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


BOOTSTRAP_PATH = Path(__file__).with_name("nlp_refinement_runtime_bootstrap.py")
_spec = importlib.util.spec_from_file_location("nlp_refinement_runtime_bootstrap", BOOTSTRAP_PATH)
assert _spec is not None and _spec.loader is not None
bootstrap = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bootstrap
_spec.loader.exec_module(bootstrap)


def _blocked(code: str, *, completed: list[str] | None = None) -> dict[str, object]:
    return {"status": "BLOCKED", "code": code, "completed_stages": completed or []}


def _configuration(plugin_root: Path) -> tuple[Path, str, str] | None:
    try:
        plugin = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
        config = json.loads((plugin_root / "runtime-bootstrap-config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    release = config.get("release_manifest") if isinstance(config, dict) else None
    if not isinstance(plugin, dict) or not isinstance(release, dict):
        return None
    if not (
        config.get("schema_version") == "nlp-refinement-runtime-bootstrap/v1"
        and config.get("plugin_name") == plugin.get("name")
        and config.get("plugin_version") == plugin.get("version")
        and bootstrap._is_https(release.get("url"))
        and bootstrap._is_sha256(release.get("sha256"))
    ):
        return None
    return plugin_root / "plugin.json", str(release["url"]), str(release["sha256"])


def run(*, plugin_root: Path, output_dir: Path) -> dict[str, object]:
    configuration = _configuration(plugin_root)
    if configuration is None:
        return _blocked("BOOTSTRAP_ROOT_OR_RELEASE_UNAVAILABLE")
    plugin_manifest, manifest_url, manifest_sha256 = configuration
    plugin = bootstrap._read_plugin(plugin_manifest)
    assert plugin is not None
    try:
        manifest_bytes = bootstrap._download(manifest_url, bootstrap.MAX_RELEASE_MANIFEST_BYTES)
    except Exception:
        return _blocked("RELEASE_MANIFEST_DOWNLOAD_FAILED")
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
        return _blocked("RELEASE_MANIFEST_HASH_MISMATCH")
    release = bootstrap._parse_release(manifest_bytes)
    if release is None or not bootstrap._release_is_compatible(release, plugin):
        return _blocked("RELEASE_MANIFEST_INCOMPATIBLE", completed=["release-manifest-verified"])
    wheel = release["wheel"]
    assert isinstance(wheel, dict)
    wheel_url, wheel_sha256, wheel_size = wheel["url"], wheel["sha256"], wheel["size_bytes"]
    assert isinstance(wheel_url, str) and isinstance(wheel_sha256, str) and isinstance(wheel_size, int)
    try:
        wheel_bytes = bootstrap._download(wheel_url, wheel_size)
    except Exception:
        return _blocked("WHEEL_DOWNLOAD_FAILED", completed=["release-manifest-verified"])
    if len(wheel_bytes) != wheel_size:
        return _blocked("WHEEL_SIZE_MISMATCH", completed=["release-manifest-verified"])
    if hashlib.sha256(wheel_bytes).hexdigest() != wheel_sha256:
        return _blocked("WHEEL_HASH_MISMATCH", completed=["release-manifest-verified"])
    version = release["version"]
    assert isinstance(version, str)
    if not bootstrap._wheel_metadata_matches(wheel_bytes, expected_version=version):
        return _blocked("WHEEL_METADATA_INCOMPATIBLE", completed=["release-manifest-verified", "wheel-verified"])
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / Path(wheel_url).name
    if output.exists():
        return _blocked("VERIFIED_WHEEL_OUTPUT_ALREADY_EXISTS", completed=["release-manifest-verified", "wheel-verified"])
    output.write_bytes(wheel_bytes)
    return {
        "status": "VERIFIED",
        "version": version,
        "wheel_path": str(output),
        "wheel_sha256": wheel_sha256,
        "completed_stages": ["release-manifest-verified", "wheel-verified", "wheel-staged"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download the exact GitHub Release wheel verified by Plugin release records.")
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(plugin_root=args.plugin_root, output_dir=args.output_dir)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
