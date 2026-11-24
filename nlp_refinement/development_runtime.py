"""Development-only identity checks for an explicitly selected CLI executable.

This module does not locate an executable through ``PATH``, create a virtual
environment, install a package, or invoke a model.  A developer supplies a
descriptor that names one absolute executable, then the already-selected CLI
uses these checks before a TUI workflow may call its lower-level commands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from nlp_refinement.version import CLI_VERSION


TUI_NATIVE_INTERFACE = "nlp-refinement-tui-native/v1"
TUI_NATIVE_REQUIRED_COMMANDS = (
    "runtime-identity",
    "scan",
    "policy-observe",
    "capability-review",
    "batch-create",
    "tui-resume-locators",
    "resume",
    "tui-resume-candidates",
    "tui-workflow-disposition",
    "tui-review-handoff",
    "tui-proposal-handoff",
    "tui-proposal-draft-handoff",
    "tui-compose-assessed-proposal",
    "tui-create-transient-root",
    "tui-record-review",
    "tui-record-child-dispatch-receipt",
    "tui-child-dispatch-receipts",
    "tui-cleanup-transient-root",
    "tui-child-contract",
    "tui-child-bootstrap",
    "patch-proposal",
)
_DESCRIPTOR_SCHEMA = "nlp-refinement-development-runtime/v1"
_IDENTITY_SCHEMA = "nlp-refinement-runtime-identity/v1"
_DESCRIPTOR_FIELDS = {"schema_version", "executable", "state_root", "wheel_sha256", "expected_identity"}
_EXPECTED_IDENTITY_FIELDS = {
    "distribution_name",
    "distribution_version",
    "interface_version",
    "commands",
}


class DevelopmentRuntimeError(ValueError):
    """A content-free failure in a developer-selected runtime contract."""


def _resolved_regular_file(path: Path, *, code: str) -> Path:
    if not path.is_absolute():
        raise DevelopmentRuntimeError(f"{code}: path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DevelopmentRuntimeError(f"{code}: executable path is unavailable") from error
    if not resolved.is_file():
        raise DevelopmentRuntimeError(f"{code}: executable path is not a regular file")
    return resolved


def _absolute_state_root(value: object) -> Path:
    if not isinstance(value, str):
        raise DevelopmentRuntimeError("DESCRIPTOR_STATE_ROOT_INVALID: state_root must be a string")
    path = Path(value)
    if not path.is_absolute():
        raise DevelopmentRuntimeError("DESCRIPTOR_STATE_ROOT_INVALID: state_root must be absolute")
    return path.resolve(strict=False)


def _sha256(value: object, *, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise DevelopmentRuntimeError(f"{code}: expected lowercase SHA-256")
    return value


def _command_list(value: object, *, code: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DevelopmentRuntimeError(f"{code}: commands must be a string list")
    commands = tuple(value)
    if len(commands) != len(set(commands)):
        raise DevelopmentRuntimeError(f"{code}: commands must not contain duplicates")
    return commands


def runtime_identity(invoked_executable: Path) -> dict[str, Any]:
    """Return the machine-readable TUI-native interface identity for one CLI."""
    executable = _resolved_regular_file(invoked_executable, code="CLI_PATH_INVALID")
    return {
        "schema_version": _IDENTITY_SCHEMA,
        "distribution_name": "nlp-refinement-cli",
        "distribution_version": CLI_VERSION,
        "interface_version": TUI_NATIVE_INTERFACE,
        "commands": list(TUI_NATIVE_REQUIRED_COMMANDS),
        "executable": str(executable),
        "no_model_call_attestation": True,
        "no_target_write_attestation": True,
    }


def _load_descriptor(descriptor_path: Path) -> dict[str, Any]:
    if not descriptor_path.is_absolute():
        raise DevelopmentRuntimeError("DESCRIPTOR_PATH_INVALID: path must be absolute")
    try:
        decoded = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DevelopmentRuntimeError("DESCRIPTOR_READ_FAILED: descriptor must be readable JSON") from error
    if not isinstance(decoded, dict):
        raise DevelopmentRuntimeError("DESCRIPTOR_FIELDS_INVALID: descriptor must be an object")
    if set(decoded) != _DESCRIPTOR_FIELDS:
        raise DevelopmentRuntimeError("DESCRIPTOR_FIELDS_INVALID: descriptor fields do not match v1")
    if decoded["schema_version"] != _DESCRIPTOR_SCHEMA:
        raise DevelopmentRuntimeError("DESCRIPTOR_SCHEMA_UNSUPPORTED: expected development-runtime/v1")
    if not isinstance(decoded["executable"], str):
        raise DevelopmentRuntimeError("DESCRIPTOR_EXECUTABLE_INVALID: executable must be a string")
    _absolute_state_root(decoded["state_root"])
    _sha256(decoded["wheel_sha256"], code="DESCRIPTOR_WHEEL_HASH_INVALID")
    expected = decoded["expected_identity"]
    if not isinstance(expected, dict) or set(expected) != _EXPECTED_IDENTITY_FIELDS:
        raise DevelopmentRuntimeError("DESCRIPTOR_IDENTITY_INVALID: expected_identity fields do not match v1")
    if not all(isinstance(expected[key], str) for key in (
        "distribution_name", "distribution_version", "interface_version",
    )):
        raise DevelopmentRuntimeError("DESCRIPTOR_IDENTITY_INVALID: identity strings are required")
    _command_list(expected["commands"], code="DESCRIPTOR_IDENTITY_INVALID")
    return decoded


def _validated_identity(identity: Mapping[str, Any]) -> tuple[str, str, str, tuple[str, ...], Path]:
    if identity.get("schema_version") != _IDENTITY_SCHEMA:
        raise DevelopmentRuntimeError("CLI_IDENTITY_MISMATCH: unsupported identity schema")
    distribution_name = identity.get("distribution_name")
    distribution_version = identity.get("distribution_version")
    interface_version = identity.get("interface_version")
    executable = identity.get("executable")
    if not all(isinstance(value, str) for value in (
        distribution_name, distribution_version, interface_version, executable,
    )):
        raise DevelopmentRuntimeError("CLI_IDENTITY_MISMATCH: required identity strings are missing")
    commands = _command_list(identity.get("commands"), code="CLI_IDENTITY_MISMATCH")
    return (
        distribution_name,
        distribution_version,
        interface_version,
        commands,
        _resolved_regular_file(Path(executable), code="CLI_IDENTITY_MISMATCH"),
    )


def resolve_development_runtime(
    descriptor_path: Path,
    *,
    invoked_executable: Path,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a developer descriptor to this exact CLI and its TUI interface.

    The descriptor is a developer-control artifact, not a security sandbox or
    a published release manifest.  It prevents accidental fallback to a
    similarly named CLI on PATH.
    """
    descriptor = _load_descriptor(descriptor_path)
    configured_executable = _resolved_regular_file(
        Path(str(descriptor["executable"])), code="DESCRIPTOR_EXECUTABLE_INVALID"
    )
    actual_executable = _resolved_regular_file(invoked_executable, code="CLI_PATH_INVALID")
    if configured_executable != actual_executable:
        raise DevelopmentRuntimeError("CLI_PATH_MISMATCH: descriptor does not name the invoked CLI")
    (
        distribution_name,
        distribution_version,
        interface_version,
        commands,
        identity_executable,
    ) = _validated_identity(identity)
    if identity_executable != actual_executable:
        raise DevelopmentRuntimeError("CLI_PATH_MISMATCH: identity does not name the invoked CLI")
    expected = descriptor["expected_identity"]
    assert isinstance(expected, dict)
    if (
        distribution_name != expected["distribution_name"]
        or distribution_version != expected["distribution_version"]
        or interface_version != expected["interface_version"]
    ):
        raise DevelopmentRuntimeError("CLI_IDENTITY_MISMATCH: CLI identity differs from descriptor")
    expected_commands = _command_list(expected["commands"], code="DESCRIPTOR_IDENTITY_INVALID")
    if (
        interface_version != TUI_NATIVE_INTERFACE
        or not set(TUI_NATIVE_REQUIRED_COMMANDS).issubset(commands)
        or not set(expected_commands).issuperset(TUI_NATIVE_REQUIRED_COMMANDS)
        or not set(expected_commands).issubset(commands)
    ):
        raise DevelopmentRuntimeError("CLI_CAPABILITY_MISMATCH: TUI-native command contract is incomplete")
    return {
        "schema_version": "nlp-refinement-resolved-development-runtime/v1",
        "executable": str(actual_executable),
        "identity": dict(identity),
        "wheel_sha256": descriptor["wheel_sha256"],
        "state_root": str(_absolute_state_root(descriptor["state_root"])),
        "no_model_call_attestation": True,
        "no_target_write_attestation": True,
    }
