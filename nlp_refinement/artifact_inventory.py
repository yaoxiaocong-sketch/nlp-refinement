"""Deterministic v1 policy discovery for NL-programming artifacts.

The scanner classifies files from path and filesystem facts only. It never
executes artifact text and never promotes a file because its contents merely
look instruction-like.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tomllib
from dataclasses import dataclass
from typing import Any, Callable


INVENTORY_SCHEMA_VERSION = "3.0"
POLICY_ID = "nl-artifact-policy/v1"
_SELECTION_DOMAIN = "nlp-refinement/auto-inventory/v3\0"
_MAX_AUTO_BYTES = 1_000_000
_MAX_DECLARATION_BYTES = 64 * 1024
_HARD_EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", "build", "dist",
})
_SUPPRESSED_DIRECTORIES = frozenset({
    "test", "tests", "__tests__", "fixture", "fixtures", "example", "examples",
    "sample", "samples", "doc", "docs",
})
_HARD_EXCLUDED_FILENAMES = frozenset({
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock",
    "Pipfile.lock", "Cargo.lock", "composer.lock", "Gemfile.lock",
})
_DECLARATION_PATH = ".nlp-refinement/nl-artifacts.json"
_DECLARATION_VERSION = "nl-artifact-declaration/v1"
_ALLOWED_EXTENSIONS = {".md": "markdown", ".json": "json", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml"}


def _policy_material() -> dict[str, Any]:
    return {
        "policy_id": POLICY_ID,
        "providers": ["codex/v1"],
        "auto_max_bytes": _MAX_AUTO_BYTES,
        "hard_excluded_directories": sorted(_HARD_EXCLUDED_DIRECTORIES),
        "suppressed_directories": sorted(_SUPPRESSED_DIRECTORIES),
        "hard_excluded_filenames": sorted(_HARD_EXCLUDED_FILENAMES),
        "rules": [
            "**/AGENTS.md", "**/AGENTS.override.md", ".agents/skills/<name>/SKILL.md",
            ".agents/skills/<name>/agents/openai.yaml|yml", ".agents/plugins/marketplace.json",
            ".codex-plugin/plugin.json", ".codex/config.toml", ".codex/hooks.json",
            ".codex/agents/<name>.toml", ".codex/prompts/<name>.md",
        ],
    }


POLICY_SHA256 = hashlib.sha256(
    json.dumps(_policy_material(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def _rule(provider_id: str, provider_version: str, rule_id: str, artifact_kind: str) -> dict[str, str]:
    return {"provider_id": provider_id, "provider_version": provider_version, "rule_id": rule_id, "artifact_kind": artifact_kind}


def _valid_name(name: str) -> bool:
    return bool(name) and len(name) <= 128 and name not in {".", ".."} and "/" not in name and "\\" not in name and "\x00" not in name


def _canonical_relative(value: str) -> bool:
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _load_declaration(root: Path) -> tuple[dict[str, dict[str, str]], str]:
    path = root / _DECLARATION_PATH
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("POLICY_DECLARATION_INVALID: declaration must be a regular non-symlink file")
    if not path.exists():
        return {}, ""
    raw = path.read_bytes()
    if len(raw) > _MAX_DECLARATION_BYTES:
        raise ValueError("POLICY_DECLARATION_INVALID: declaration is too large")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("POLICY_DECLARATION_INVALID: declaration must be strict UTF-8 JSON") from error
    if not isinstance(parsed, dict) or set(parsed) != {"schema_version", "policy_id", "entries"}:
        raise ValueError("POLICY_DECLARATION_INVALID: declaration fields are invalid")
    if parsed["schema_version"] != _DECLARATION_VERSION or parsed["policy_id"] != POLICY_ID:
        raise ValueError("POLICY_DECLARATION_INVALID: declaration version or policy is invalid")
    entries = parsed["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 128:
        raise ValueError("POLICY_DECLARATION_INVALID: declaration entries are invalid")
    declared: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"relative_path", "artifact_kind", "expected_extension", "reason"}:
            raise ValueError("POLICY_DECLARATION_INVALID: declaration entry fields are invalid")
        relative, kind, extension, reason = (entry["relative_path"], entry["artifact_kind"], entry["expected_extension"], entry["reason"])
        if not isinstance(relative, str) or not _canonical_relative(relative):
            raise ValueError("POLICY_DECLARATION_INVALID: declared path is invalid")
        if relative == _DECLARATION_PATH or any(part in _HARD_EXCLUDED_DIRECTORIES for part in relative.split("/")):
            raise ValueError("POLICY_DECLARATION_INVALID: declared path is not permitted")
        if not isinstance(kind, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind) is None:
            raise ValueError("POLICY_DECLARATION_INVALID: declared artifact kind is invalid")
        if extension not in _ALLOWED_EXTENSIONS or not relative.endswith(extension):
            raise ValueError("POLICY_DECLARATION_INVALID: declared extension is invalid")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 200:
            raise ValueError("POLICY_DECLARATION_INVALID: declared reason is invalid")
        if relative in declared:
            raise ValueError("POLICY_DECLARATION_INVALID: duplicate declared path")
        if not (root / relative).is_file() or (root / relative).is_symlink():
            raise ValueError("POLICY_DECLARATION_INVALID: declared file is missing or unsafe")
        declared[relative] = _rule("project-declaration", "1", "declared-exact-path", kind)
    return declared, hashlib.sha256(raw).hexdigest()


def _codex_rule(relative: str, root: Path) -> dict[str, str] | None:
    parts = relative.split("/")
    name = parts[-1]
    if name in {"AGENTS.md", "AGENTS.override.md"}:
        return _rule("codex", "1", "codex-agents-md" if name == "AGENTS.md" else "codex-agents-override", "agent_instructions")
    if relative == ".agents/plugins/marketplace.json":
        return _rule("codex", "1", "codex-marketplace", "plugin_marketplace")
    if relative == ".codex-plugin/plugin.json":
        return _rule("codex", "1", "codex-plugin-manifest", "plugin_manifest")
    if relative == ".codex/config.toml":
        return _rule("codex", "1", "codex-config", "codex_config")
    if relative == ".codex/hooks.json":
        return _rule("codex", "1", "codex-hooks", "codex_hooks")
    if len(parts) == 4 and parts[:2] == [".agents", "skills"] and _valid_name(parts[2]) and name == "SKILL.md":
        return _rule("codex", "1", "codex-skill", "skill")
    if len(parts) == 5 and parts[:2] == [".agents", "skills"] and _valid_name(parts[2]) and parts[3] == "agents" and name in {"openai.yaml", "openai.yml"}:
        parent_skill = root / ".agents" / "skills" / parts[2] / "SKILL.md"
        if parent_skill.is_file() and not parent_skill.is_symlink():
            return _rule("codex", "1", "codex-skill-sidecar", "skill_sidecar")
        return None
    if len(parts) == 3 and parts[:2] == [".codex", "agents"] and name.endswith(".toml") and _valid_name(name.removesuffix(".toml")):
        return _rule("codex", "1", "codex-agent-definition", "agent_definition")
    if len(parts) == 3 and parts[:2] == [".codex", "prompts"] and name.endswith(".md") and _valid_name(name.removesuffix(".md")):
        return _rule("codex", "1", "codex-prompt", "codex_prompt")
    return None


@dataclass(frozen=True)
class ArtifactProvider:
    """One independently versioned path-rule provider.

    Adding a future provider (for example Claude or AGY) requires a new
    matcher and an explicit registry entry.  It cannot alter Codex v1 rules by
    accident through a generic extension or content heuristic.
    """

    provider_id: str
    provider_version: str
    match: Callable[[str, Path], dict[str, str] | None]


_PROVIDERS = (ArtifactProvider("codex", "1", _codex_rule),)


def _provider_rule(relative: str, root: Path) -> dict[str, str] | None:
    matched: list[dict[str, str]] = []
    for provider in _PROVIDERS:
        rule = provider.match(relative, root)
        if rule is not None:
            if rule["provider_id"] != provider.provider_id or rule["provider_version"] != provider.provider_version:
                raise RuntimeError("provider returned an inconsistent rule identity")
            matched.append(rule)
    if len(matched) > 1:
        raise RuntimeError("POLICY_PROVIDER_CONFLICT: multiple providers matched one path")
    return matched[0] if matched else None


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


def _read_auto_file(path: Path) -> tuple[bytes | None, int, str | None]:
    try:
        expected = os.lstat(path)
    except OSError:
        return None, 0, "unreadable-file"
    if stat.S_ISLNK(expected.st_mode):
        return None, 0, "symbolic-link"
    if not stat.S_ISREG(expected.st_mode):
        return None, 0, "not-regular-file"
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None, 0, "unreadable-file"
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None, 0, "not-regular-file"
        if (metadata.st_dev, metadata.st_ino) != (expected.st_dev, expected.st_ino):
            return None, 0, "entry-changed"
        byte_count = int(metadata.st_size)
        if byte_count > _MAX_AUTO_BYTES:
            return None, byte_count, "file-too-large"
        data = bytearray()
        while len(data) <= _MAX_AUTO_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_AUTO_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > _MAX_AUTO_BYTES:
            return None, len(data), "file-too-large"
        return bytes(data), len(data), None
    finally:
        os.close(descriptor)


def scan_artifact_inventory(root: Path) -> dict[str, Any]:
    """Return a deterministic policy inventory without writing to ``root``."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("scan root must be a non-symlink directory")
    resolved_root = root.resolve(strict=True)
    declared, declaration_sha256 = _load_declaration(resolved_root)
    auto: list[dict[str, Any]] = []
    restricted: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    blocked: list[dict[str, Any]] = []

    def exclude(relative_path: str, reason: str) -> None:
        excluded.append({"relative_path": relative_path, "reason": reason})

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            relative = directory.relative_to(resolved_root).as_posix()
            blocked.append({"relative_path": "./" if relative == "." else relative + "/", "reason": "unreadable-directory"})
            return
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(resolved_root).as_posix()
            try:
                if entry.is_symlink():
                    exclude(relative, "symbolic-link")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _HARD_EXCLUDED_DIRECTORIES:
                        exclude(relative + "/", "excluded-directory")
                    else:
                        visit(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    exclude(relative, "not-regular-file")
                    continue
            except OSError:
                blocked.append({"relative_path": relative, "reason": "unreadable-entry"})
                continue
            if relative == _DECLARATION_PATH:
                exclude(relative, "policy-control-file")
                continue
            if entry.name in _HARD_EXCLUDED_FILENAMES:
                exclude(relative, "excluded-filename")
                continue
            rule = declared.get(relative) or _provider_rule(relative, resolved_root)
            if rule is None:
                exclude(relative, "no-policy-rule")
                continue
            if any(part in _SUPPRESSED_DIRECTORIES for part in relative.split("/")[:-1]) and relative not in declared:
                exclude(relative, "suppressed-directory")
                continue
            artifact_type = _ALLOWED_EXTENSIONS.get(path.suffix.lower())
            if artifact_type is None:
                exclude(relative, "unsupported-extension")
                continue
            data, byte_count, reason = _read_auto_file(path)
            base = {"relative_path": relative, "artifact_kind": rule["artifact_kind"], "primary_rule": rule, "byte_count": byte_count}
            if reason == "file-too-large":
                restricted.append(base | {"restriction_type": "exceeds_default_size_limit", "utf8_status": "not-read", "format_status": "not-applicable"})
                continue
            if reason is not None:
                blocked.append({"relative_path": relative, "reason": reason})
                continue
            if data is None:
                raise RuntimeError("scanner returned neither bytes nor a reason")
            try:
                text = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                restricted.append(base | {"restriction_type": "not_utf8", "utf8_status": "invalid", "format_status": "not-applicable"})
                continue
            auto.append(base | {"artifact_type": artifact_type, "sha256": hashlib.sha256(data).hexdigest(), "utf8_status": "valid", "format_status": _format_status(artifact_type, text)})

    visit(resolved_root)
    for collection in (auto, restricted, excluded, blocked):
        collection.sort(key=lambda entry: str(entry["relative_path"]))
    selection = {"schema_version": INVENTORY_SCHEMA_VERSION, "policy_id": POLICY_ID, "policy_sha256": POLICY_SHA256, "declaration_sha256": declaration_sha256, "auto": auto}
    inventory_hash = hashlib.sha256((_SELECTION_DOMAIN + json.dumps(selection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))).encode("utf-8")).hexdigest()
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "policy_id": POLICY_ID,
        "policy_sha256": POLICY_SHA256,
        "declaration_sha256": declaration_sha256,
        "scan_status": "complete",
        "inventory_id": f"inv_{inventory_hash[:24]}",
        "inventory_hash": inventory_hash,
        "auto": auto,
        "restricted": restricted,
        "blocked": blocked,
        "excluded": excluded,
    }
