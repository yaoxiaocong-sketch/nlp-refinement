#!/usr/bin/env python3
"""Explicit, hash-pinned bootstrap for the separately distributed local CLI.

This script is intentionally independent from the Python package it installs.
It performs no installation unless the caller passes ``--apply``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import site
import subprocess
import sys
import tempfile
from typing import Callable
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


RELEASE_SCHEMA = "nlp-refinement-runtime-release/v1"
PACKAGE_NAME = "nlp-refinement-cli"
REQUIRED_COMMANDS = (
    "nlp-refinement",
    "nlp-refinement-host",
    "nlp-refinement-doe-prune",
)
MAX_RELEASE_MANIFEST_BYTES = 64 * 1024


def _blocked(code: str, *, completed: list[str] | None = None) -> dict[str, object]:
    return {
        "status": "BLOCKED",
        "code": code,
        "completed_stages": completed or [],
        "next_action": "Correct the reported release or environment boundary, then start a fresh bootstrap plan.",
    }


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _is_https(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def _download(url: str, max_bytes: int) -> bytes:
    class _HttpsOnlyRedirect(HTTPRedirectHandler):
        def redirect_request(self, request: Request, fp: object, code: int, message: str, headers: object, newurl: str) -> Request | None:
            if not _is_https(newurl):
                raise OSError("redirect left HTTPS")
            return super().redirect_request(request, fp, code, message, headers, newurl)

    received = bytearray()
    with build_opener(_HttpsOnlyRedirect()).open(url, timeout=30) as response:
        while chunk := response.read(min(64 * 1024, max_bytes + 1 - len(received))):
            received.extend(chunk)
            if len(received) > max_bytes:
                raise ValueError("download exceeded declared limit")
    return bytes(received)


def _read_plugin(plugin_manifest: Path) -> tuple[str, str] | None:
    try:
        payload = json.loads(plugin_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name, version = payload.get("name"), payload.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        return None
    return name, version


def _parse_release(raw: bytes) -> dict[str, object] | None:
    try:
        release = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(release, dict):
        return None
    return release


def _wheel_metadata_matches(raw: bytes, *, expected_version: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                return False
            metadata = archive.read(metadata_names[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
        return False
    fields: dict[str, str] = {}
    for line in metadata.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key] = value.strip()
    return fields.get("Name") == PACKAGE_NAME and fields.get("Version") == expected_version


def _wheel_filename(url: str, *, expected_version: str) -> str | None:
    """Return the one published filename accepted for this pure-Python wheel."""
    name = Path(urlparse(url).path).name
    expected = f"{PACKAGE_NAME.replace('-', '_')}-{expected_version}-py3-none-any.whl"
    return name if name == expected else None


def _release_is_compatible(release: dict[str, object], plugin: tuple[str, str]) -> bool:
    plugin_record = release.get("plugin")
    wheel = release.get("wheel")
    commands = release.get("commands")
    if not isinstance(plugin_record, dict) or not isinstance(wheel, dict) or not isinstance(commands, list):
        return False
    return (
        release.get("schema_version") == RELEASE_SCHEMA
        and release.get("package_name") == PACKAGE_NAME
        and isinstance(release.get("version"), str)
        and release.get("requires_python") == ">=3.11"
        and plugin_record.get("name") == plugin[0]
        and plugin_record.get("version") == plugin[1]
        and release.get("version") == plugin[1]
        and _is_https(wheel.get("url"))
        and _is_sha256(wheel.get("sha256"))
        and isinstance(wheel.get("size_bytes"), int)
        and 0 < wheel["size_bytes"] <= 64 * 1024 * 1024
        and tuple(commands) == REQUIRED_COMMANDS
    )


def _run_command(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _user_scripts_dir() -> Path:
    return Path(site.USER_BASE) / ("Scripts" if os.name == "nt" else "bin")


def run(
    *,
    release_manifest_url: str,
    release_manifest_sha256: str,
    plugin_manifest: Path,
    apply: bool,
    download: Callable[[str, int], bytes] = _download,
    run_command: Callable[[list[str]], None] = _run_command,
    script_dir: Path | None = None,
) -> dict[str, object]:
    """Prepare or apply an installation without accepting unpinned sources."""
    if sys.version_info < (3, 11):
        return _blocked("PYTHON_TOO_OLD")
    if not _is_https(release_manifest_url):
        return _blocked("RELEASE_URL_NOT_HTTPS")
    if not _is_sha256(release_manifest_sha256):
        return _blocked("RELEASE_MANIFEST_HASH_INVALID")

    plugin = _read_plugin(plugin_manifest)
    if plugin is None:
        return _blocked("PLUGIN_MANIFEST_INVALID")

    try:
        release_bytes = download(release_manifest_url, MAX_RELEASE_MANIFEST_BYTES)
    except Exception:
        return _blocked("RELEASE_MANIFEST_DOWNLOAD_FAILED")
    if len(release_bytes) > MAX_RELEASE_MANIFEST_BYTES:
        return _blocked("RELEASE_MANIFEST_TOO_LARGE")
    if hashlib.sha256(release_bytes).hexdigest() != release_manifest_sha256:
        return _blocked("RELEASE_MANIFEST_HASH_MISMATCH")

    release = _parse_release(release_bytes)
    if release is None or not _release_is_compatible(release, plugin):
        return _blocked("RELEASE_MANIFEST_INCOMPATIBLE")

    version = release["version"]
    wheel = release["wheel"]
    assert isinstance(version, str) and isinstance(wheel, dict)
    wheel_url = wheel["url"]
    wheel_sha256 = wheel["sha256"]
    wheel_size = wheel["size_bytes"]
    assert isinstance(wheel_url, str) and isinstance(wheel_sha256, str) and isinstance(wheel_size, int)
    plan = {
        "status": "PLAN_READY",
        "cli_version": version,
        "plugin_version": plugin[1],
        "package_name": PACKAGE_NAME,
        "commands": list(REQUIRED_COMMANDS),
        "wheel_sha256": wheel_sha256,
        "requires_confirmation": True,
        "next_action": "Rerun this same request with --apply only after the user confirms this exact version and hash.",
    }
    if not apply:
        return plan

    try:
        wheel_bytes = download(wheel_url, wheel_size)
    except Exception:
        return _blocked("WHEEL_DOWNLOAD_FAILED", completed=["release-manifest-verified"])
    if len(wheel_bytes) != wheel_size:
        return _blocked("WHEEL_SIZE_MISMATCH", completed=["release-manifest-verified"])
    if hashlib.sha256(wheel_bytes).hexdigest() != wheel_sha256:
        return _blocked("WHEEL_HASH_MISMATCH", completed=["release-manifest-verified"])
    if not _wheel_metadata_matches(wheel_bytes, expected_version=version):
        return _blocked("WHEEL_METADATA_INCOMPATIBLE", completed=["release-manifest-verified", "wheel-verified"])
    wheel_filename = _wheel_filename(wheel_url, expected_version=version)
    if wheel_filename is None:
        return _blocked("WHEEL_FILENAME_INCOMPATIBLE", completed=["release-manifest-verified", "wheel-verified"])

    with tempfile.TemporaryDirectory(prefix="nlp-refinement-bootstrap-") as directory:
        wheel_path = Path(directory) / wheel_filename
        wheel_path.write_bytes(wheel_bytes)
        try:
            run_command([
                sys.executable,
                "-m",
                "pip",
                "install",
                "--user",
                "--no-deps",
                "--no-index",
                "--disable-pip-version-check",
                str(wheel_path),
            ])
        except Exception:
            return _blocked("PIP_INSTALL_FAILED", completed=["release-manifest-verified", "wheel-verified"])

    expected_dir = script_dir or _user_scripts_dir()
    missing_scripts = [command for command in REQUIRED_COMMANDS if not (expected_dir / command).is_file()]
    if missing_scripts:
        return {
            **_blocked("CLI_SCRIPT_MISSING", completed=["release-manifest-verified", "wheel-verified", "pip-installed"]),
            "missing_commands": missing_scripts,
            "next_action": "pip completed, but the expected user-site commands are absent. Inspect the Python user-site installation before retrying.",
        }
    mismatched = [
        command
        for command in REQUIRED_COMMANDS
        if shutil.which(command) is None or Path(str(shutil.which(command))).resolve() != (expected_dir / command).resolve()
    ]
    if mismatched:
        return {
            **_blocked("CLI_PATH_MISMATCH", completed=["release-manifest-verified", "wheel-verified", "pip-installed"]),
            "mismatched_commands": mismatched,
            "next_action": "The verified user-site CLI is not the command currently selected by PATH. Update PATH or start a new terminal/session, then verify again.",
        }
    return {
        "status": "INSTALLED",
        "cli_version": version,
        "plugin_version": plugin[1],
        "commands": list(REQUIRED_COMMANDS),
        "completed_stages": ["release-manifest-verified", "wheel-verified", "pip-installed", "commands-visible"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or explicitly apply a pinned NLP-refinement CLI installation.")
    parser.add_argument("--release-manifest-url", required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    parser.add_argument("--plugin-manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Install only after an explicit user confirmation.")
    args = parser.parse_args(argv)
    result = run(
        release_manifest_url=args.release_manifest_url,
        release_manifest_sha256=args.release_manifest_sha256,
        plugin_manifest=args.plugin_manifest,
        apply=args.apply,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"PLAN_READY", "INSTALLED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
