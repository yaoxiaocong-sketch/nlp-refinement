"""Read-only discovery of candidate natural-language-programming artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tomllib
from typing import Any


INVENTORY_SCHEMA_VERSION = "1.0"
_INVENTORY_DOMAIN = "nlp-refinement/artifact-inventory/v1\0"
_MAX_CANDIDATE_BYTES = 1_000_000
_EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", "build", "dist",
})
_ARTIFACT_TYPES = {
    ".md": "markdown",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def scan_artifact_inventory(root: Path) -> dict[str, Any]:
    """Return a deterministic, content-free inventory without writing to ``root``.

    The scanner intentionally recognizes an artifact by its filename only. It
    never executes contents, rejects observed root and leaf symbolic links, and
    does not claim protection against concurrent ancestor-directory replacement.
    It also does not claim YAML syntax was checked without a YAML parser.
    """
    if root.is_symlink() or not root.is_dir():
        raise ValueError("scan root must be a non-symlink directory")
    resolved_root = root.resolve(strict=True)
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []

    def exclude(relative_path: str, reason: str) -> None:
        excluded.append({"relative_path": relative_path, "reason": reason})

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            relative = directory.relative_to(resolved_root).as_posix()
            exclude((relative + "/") if relative != "." else "./", "unreadable-directory")
            return
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(resolved_root).as_posix()
            try:
                if entry.is_symlink():
                    exclude(relative, "symbolic-link")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _EXCLUDED_DIRECTORIES:
                        exclude(relative + "/", "excluded-directory")
                    else:
                        visit(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    exclude(relative, "not-regular-file")
                    continue
            except OSError:
                exclude(relative, "unreadable-entry")
                continue

            artifact_type = _ARTIFACT_TYPES.get(path.suffix.lower())
            if artifact_type is None:
                exclude(relative, "unsupported-extension")
                continue
            data, exclusion = _read_regular_file(path)
            if exclusion is not None:
                exclude(relative, exclusion)
                continue
            if data is None:
                raise RuntimeError("scanner returned neither data nor an exclusion")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                exclude(relative, "not-utf8")
                continue
            candidates.append(
                {
                    "relative_path": relative,
                    "artifact_type": artifact_type,
                    "byte_count": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "utf8_status": "valid",
                    "format_status": _format_status(artifact_type, text),
                }
            )

    visit(resolved_root)
    candidates.sort(key=lambda entry: str(entry["relative_path"]))
    excluded.sort(key=lambda entry: (entry["relative_path"], entry["reason"]))
    canonical = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "candidates": candidates,
        "excluded": excluded,
    }
    inventory_hash = hashlib.sha256(
        (_INVENTORY_DOMAIN + json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))).encode("utf-8")
    ).hexdigest()
    return {
        **canonical,
        "scan_status": "complete",
        "inventory_id": f"inv_{inventory_hash[:24]}",
        "inventory_hash": inventory_hash,
    }


def _read_regular_file(path: Path) -> tuple[bytes | None, str | None]:
    """Read one bounded regular file without following a final symlink."""
    try:
        expected = os.lstat(path)
    except OSError:
        return None, "unreadable-entry"
    if stat.S_ISLNK(expected.st_mode):
        return None, "symbolic-link"
    if not stat.S_ISREG(expected.st_mode):
        return None, "not-regular-file"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None, "unreadable-entry"
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None, "not-regular-file"
        if (metadata.st_dev, metadata.st_ino) != (expected.st_dev, expected.st_ino):
            return None, "entry-changed"
        if metadata.st_size > _MAX_CANDIDATE_BYTES:
            return None, "file-too-large"
        chunks: list[bytes] = []
        remaining = _MAX_CANDIDATE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_CANDIDATE_BYTES:
            return None, "file-too-large"
        return data, None
    finally:
        os.close(descriptor)


def _format_status(artifact_type: str, text: str) -> str:
    if artifact_type == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError:
            return "invalid"
        return "valid"
    if artifact_type == "toml":
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return "invalid"
        return "valid"
    if artifact_type == "yaml":
        return "unvalidated"
    return "not-applicable"
