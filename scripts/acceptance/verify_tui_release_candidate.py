#!/usr/bin/env python3
"""Verify one completed TUI release-candidate run without invoking a model.

This is a maintainer-only acceptance utility.  It is intentionally outside the
``nlp_refinement`` package and is not a published CLI command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REQUIRED_RUNTIME_COMMANDS = {
    "tui-inline-handoff",
    "tui-child-dispatch-receipts",
    "tui-workflow-disposition",
}
VALID_DISPOSITIONS = {"AWAITING_HUMAN", "SNAPSHOT_STALE"}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_absolute_file(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_absolute() or not resolved.is_file():
        raise ValueError(f"{label} must be an existing regular file")
    return resolved


def require_absolute_directory(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_absolute() or not resolved.is_dir():
        raise ValueError(f"{label} must be an existing directory")
    return resolved


def run_cli(cli: Path, state_root: Path, arguments: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [str(cli), "--state-root", str(state_root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError("release-candidate CLI query failed")
    try:
        rendered = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("release-candidate CLI returned non-JSON output") from error
    if not isinstance(rendered, dict):
        raise ValueError("release-candidate CLI returned a non-object result")
    return rendered


def validate_completed_run(
    *,
    identity: dict[str, Any],
    receipts: dict[str, Any],
    disposition: dict[str, Any],
    expected_roles: set[str],
    expected_disposition: str,
    minimum_prompt_bytes: int,
    actual_target_sha256: str,
    expected_target_sha256: str,
) -> dict[str, Any]:
    commands = set(identity.get("commands", []))
    missing = REQUIRED_RUNTIME_COMMANDS - commands
    if missing:
        raise ValueError(f"runtime identity lacks required commands: {sorted(missing)}")
    if expected_disposition not in VALID_DISPOSITIONS:
        raise ValueError("expected disposition is unsupported")
    if disposition.get("disposition") != expected_disposition:
        raise ValueError("run disposition does not match the expected release-candidate result")
    if actual_target_sha256 != expected_target_sha256:
        raise ValueError("target artifact hash changed during acceptance")
    if receipts.get("no_target_write_attestation") is not True or disposition.get("no_target_write_attestation") is not True:
        raise ValueError("release-candidate CLI did not attest zero target writes")

    received = receipts.get("receipts")
    if not isinstance(received, list):
        raise ValueError("dispatch-receipts response is invalid")
    roles = [item.get("role") for item in received if isinstance(item, dict)]
    if set(roles) != expected_roles or len(roles) != len(expected_roles):
        raise ValueError("dispatch receipt roles do not match the expected release-candidate phases")
    for receipt in received:
        if receipt.get("receipt_schema") != "tui-child-dispatch-receipt/v2":
            raise ValueError("dispatch receipt schema is invalid")
        if not isinstance(receipt.get("child_thread_id"), str) or not receipt["child_thread_id"]:
            raise ValueError("dispatch receipt has no child handle")
        if not isinstance(receipt.get("prompt_utf8_bytes"), int) or receipt["prompt_utf8_bytes"] < minimum_prompt_bytes:
            raise ValueError("dispatch receipt does not meet the required inline-prompt size")
        for field in ("observed_cwd", "host_role_binding", "child_sandbox"):
            if receipt.get(field) != "unverified":
                raise ValueError(f"dispatch receipt {field} exceeds the documented host evidence boundary")

    return {
        "receipt_schema": "tui-release-candidate-verification/v1",
        "disposition": expected_disposition,
        "roles": sorted(expected_roles),
        "prompt_utf8_bytes": {item["role"]: item["prompt_utf8_bytes"] for item in received},
        "target_sha256": actual_target_sha256,
        "no_target_write_attestation": True,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True, help="absolute staged-wheel CLI path")
    parser.add_argument("--state-root", type=Path, required=True, help="isolated acceptance state root")
    parser.add_argument("--target-root", type=Path, required=True, help="isolated fixture project root")
    parser.add_argument("--artifact-relative-path", required=True, help="bound AUTO artifact path")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-target-sha256", required=True)
    parser.add_argument("--expected-disposition", choices=sorted(VALID_DISPOSITIONS), required=True)
    parser.add_argument("--expected-roles", default="", help="comma-separated dispatched roles")
    parser.add_argument("--minimum-prompt-bytes", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cli = require_absolute_file(args.cli, "--cli")
    state_root = require_absolute_directory(args.state_root, "--state-root")
    target_root = require_absolute_directory(args.target_root, "--target-root")
    artifact = (target_root / args.artifact_relative_path).resolve(strict=True)
    if target_root not in artifact.parents or not artifact.is_file():
        raise ValueError("--artifact-relative-path must name a file below --target-root")
    expected_roles = {role for role in args.expected_roles.split(",") if role}
    if args.minimum_prompt_bytes < 0:
        raise ValueError("--minimum-prompt-bytes must be non-negative")

    identity = run_cli(cli, state_root, ["runtime-identity"])
    receipts = run_cli(cli, state_root, ["tui-child-dispatch-receipts", str(target_root), "--run-id", args.run_id])
    disposition = run_cli(cli, state_root, ["tui-workflow-disposition", str(target_root), "--run-id", args.run_id])
    rendered = validate_completed_run(
        identity=identity,
        receipts=receipts,
        disposition=disposition,
        expected_roles=expected_roles,
        expected_disposition=args.expected_disposition,
        minimum_prompt_bytes=args.minimum_prompt_bytes,
        actual_target_sha256=sha256_file(artifact),
        expected_target_sha256=args.expected_target_sha256,
    )
    rendered["run_id"] = args.run_id
    print(json.dumps(rendered, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)
