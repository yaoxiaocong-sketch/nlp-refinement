"""M2 command adapter for durable-local, review-only refinement runs.

This module is deliberately not an MCP server or a Codex command-registration
mechanism. It supplies a testable local command that a later host adapter may
call after capability verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from nlp_refinement.patch_proposal_adapter import ingest_patch_proposal, load_patch_proposal
from nlp_refinement.host_execution_request import HostExecutionRequest
from nlp_refinement.review_adapter import ingest_review, load_review_result
from nlp_refinement.sensitive_data import likely_secret_kind
from nlp_refinement.state_store import FINAL_STATES, StateStore, StateStoreError


PROJECT_KEY_DOMAIN = "nlp-refinement/project-key/v4\\0"


def default_state_root() -> Path:
    configured = os.environ.get("NLP_REFINEMENT_STATE_ROOT")
    return Path(configured).expanduser() if configured else Path("~/.codex/refinement-loop").expanduser()


def project_identity(project_dir: Path, explicit_project_id: str | None) -> tuple[str, str]:
    """Return a stable-enough local identity and its disclosure-safe mode.

    The path mode intentionally changes if a non-Git project is moved. A caller
    may use a user-managed project ID to preserve identity across that move.
    """
    if explicit_project_id:
        return f"id:{explicit_project_id}", "explicit-id"
    return f"path:{project_dir.resolve(strict=True)}", "resolved-path"


def project_key(identity: str) -> str:
    return hashlib.sha256(f"{PROJECT_KEY_DOMAIN}{identity}".encode("utf-8")).hexdigest()


def snapshot_hash(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("snapshot hash must be a lowercase SHA-256 hex string")
    return value


def now_epoch() -> int:
    return int(time.time())


def output(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nlp-refinement")
    parser.add_argument("--state-root", type=Path, default=default_state_root())
    subcommands = parser.add_subparsers(dest="command", required=True)

    start = subcommands.add_parser("start", help="create a review-only run")
    start.add_argument("--project-dir", type=Path, required=True)
    start.add_argument("--redacted-brief-file", type=Path, required=True)
    start.add_argument("--snapshot-hash", required=True)
    start.add_argument("--project-id", help="stable user-managed identity for a moved project")

    status = subcommands.add_parser("status", help="read a run without changing it")
    status.add_argument("--run-id", required=True)

    resume = subcommands.add_parser("resume", help="reopen a run only to check its snapshot")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--observed-snapshot-hash", required=True)
    resume.add_argument(
        "--retry-blocked",
        action="store_true",
        help="explicitly return a BLOCKED run to READY after its snapshot check",
    )

    review = subcommands.add_parser("review", help="ingest an external structured review result")
    review.add_argument("--run-id", required=True)
    review.add_argument("--review-result-file", type=Path, required=True)
    review.add_argument("--observed-snapshot-hash", required=True)
    review.add_argument(
        "--redaction-status",
        choices=("caller-attested-redacted",),
        required=True,
        help="caller attests that this content is already redacted; this is not an automatic safety proof",
    )

    proposal = subcommands.add_parser("patch-proposal", help="record a human-only patch proposal without applying it")
    proposal.add_argument("--run-id", required=True)
    proposal.add_argument("--patch-proposal-file", type=Path, required=True)
    proposal.add_argument("--observed-snapshot-hash", required=True)
    proposal.add_argument(
        "--redaction-status",
        choices=("caller-attested-redacted",),
        required=True,
        help="caller attests that this content is already redacted; this is not an automatic safety proof",
    )
    host_request = subcommands.add_parser(
        "host-execution-request",
        help="prepare a no-model request for the external host executor",
    )
    host_request.add_argument("--run-id", required=True)
    host_request.add_argument("--artifact-file", type=Path, required=True)
    host_request.add_argument("--observed-snapshot-hash", required=True)
    retention_preview = subcommands.add_parser("retention-preview", help="show a content-free retention plan without changing the ledger")
    retention_preview.add_argument("--now", type=int, help=argparse.SUPPRESS)
    retention_sweep = subcommands.add_parser("retention-sweep", help="apply one exact, previously previewed retention plan")
    retention_sweep.add_argument("--confirm-plan-hash", required=True)
    retention_sweep.add_argument("--now", type=int, help=argparse.SUPPRESS)
    return parser


def public_status(run: dict[str, Any], event_count: int) -> dict[str, Any]:
    """Never expose the immutable anchor because it carries the redacted brief."""
    return {
        "run_id": run["run_id"],
        "project_key": run["project_key"],
        "state": run["state"],
        "outcome": run["outcome"],
        "revision": run["revision"],
        "target_snapshot_hash": run["target_snapshot_hash"],
        "current_snapshot_hash": run["current_snapshot_hash"],
        "event_count": event_count,
    }


def start(args: argparse.Namespace, store: StateStore) -> int:
    if not args.project_dir.is_dir():
        raise ValueError("project directory must exist and be a directory")
    brief = args.redacted_brief_file.read_text(encoding="utf-8")
    if not brief.strip():
        raise ValueError("redacted brief file must not be empty")
    secret_kind = likely_secret_kind(brief)
    if secret_kind is not None:
        raise ValueError(f"redacted brief rejected by likely-secret guard: {secret_kind}")
    observed_snapshot = snapshot_hash(args.snapshot_hash)
    identity, identity_mode = project_identity(args.project_dir, args.project_id)
    run_id = f"run_{secrets.token_hex(12)}"
    run = {
        "schema_version": "4.0",
        "run_id": run_id,
        "project_key": project_key(identity),
        "target_snapshot_hash": observed_snapshot,
        "current_snapshot_hash": observed_snapshot,
        "authorization_mode": "review-only",
        "state": "DRAFT",
        "outcome": "IN_PROGRESS",
        "revision": 0,
        "repair_cycle_count": 0,
        "max_repair_cycles": 3,
        "open_high_risk_assumptions": 0,
        "retention_policy_version": "4.0",
    }
    created_at = now_epoch()
    store.create_run(
        run,
        immutable_anchor={
            "redacted_brief": brief,
            "project_identity": identity,
            "identity_mode": identity_mode,
            "redaction_attestation": "caller-supplied-redacted-brief",
        },
        now=created_at,
    )
    lease = store.acquire_lease(run_id, f"m2-start-{secrets.token_hex(8)}", now=created_at)
    ready = store.transition(
        run_id,
        expected_revision=0,
        lease_token=lease["token"],
        next_state="READY",
        next_outcome="IN_PROGRESS",
        observed_snapshot_hash=observed_snapshot,
        now=created_at,
    )
    store.release_lease(run_id, lease["token"], now=created_at)
    result = public_status(ready, len(store.list_events(run_id)))
    result.update({"command": "start", "identity_mode": identity_mode, "next_action": "submit M3 review result"})
    output(result)
    return 0


def status(args: argparse.Namespace, store: StateStore) -> int:
    run = store.get_run(args.run_id)
    result = public_status(run, len(store.list_events(args.run_id)))
    result["command"] = "status"
    output(result)
    return 0


def resume(args: argparse.Namespace, store: StateStore) -> int:
    observed_snapshot = snapshot_hash(args.observed_snapshot_hash)
    run = store.get_run(args.run_id)
    if run["state"] in FINAL_STATES:
        result = public_status(run, len(store.list_events(args.run_id)))
        result.update({"command": "resume", "resume_disposition": "terminal-run-not-resumable"})
        output(result)
        return 0

    checked_at = now_epoch()
    lease = store.acquire_lease(args.run_id, f"m2-resume-{secrets.token_hex(8)}", now=checked_at)
    try:
        if observed_snapshot != run["current_snapshot_hash"]:
            run = store.transition(
                args.run_id,
                expected_revision=run["revision"],
                lease_token=lease["token"],
                next_state=run["state"],
                next_outcome=run["outcome"],
                observed_snapshot_hash=observed_snapshot,
                now=checked_at,
            )
            disposition = "snapshot-stale-new-run-required"
        elif run["state"] == "READY":
            disposition = "ready-for-review"
        elif run["state"] == "NEEDS_USER_DECISION":
            disposition = "awaiting-user-decision"
        elif run["state"] == "BLOCKED":
            if args.retry_blocked:
                run = store.transition(
                    args.run_id,
                    expected_revision=run["revision"],
                    lease_token=lease["token"],
                    next_state="READY",
                    next_outcome="IN_PROGRESS",
                    observed_snapshot_hash=observed_snapshot,
                    now=checked_at,
                )
                disposition = "explicit-retry-authorized"
            else:
                disposition = "blocked-explicit-retry-required"
        else:
            disposition = "phase-adapter-required"
    finally:
        store.release_lease(args.run_id, lease["token"], now=checked_at)
    result = public_status(run, len(store.list_events(args.run_id)))
    result.update({"command": "resume", "resume_disposition": disposition})
    output(result)
    return 0


def review(args: argparse.Namespace, store: StateStore) -> int:
    review_result = load_review_result(args.review_result_file)
    result = ingest_review(
        store,
        args.run_id,
        review_result,
        snapshot_hash(args.observed_snapshot_hash),
        args.redaction_status,
        now_epoch(),
    )
    public = public_status(result["run"], len(store.list_events(args.run_id)))
    public.update(
        {
            "command": "review",
            "review_artifact_id": result["artifact_id"],
            "new_finding_ids": result["new_finding_ids"],
            "duplicate_finding_ids": result["duplicate_finding_ids"],
            "no_material_delta_candidates": result["no_material_delta_candidates"],
        }
    )
    output(public)
    return 0


def patch_proposal(args: argparse.Namespace, store: StateStore) -> int:
    proposal_result = load_patch_proposal(args.patch_proposal_file)
    result = ingest_patch_proposal(
        store,
        args.run_id,
        proposal_result,
        snapshot_hash(args.observed_snapshot_hash),
        args.redaction_status,
        now_epoch(),
    )
    public = public_status(result["run"], len(store.list_events(args.run_id)))
    public.update(
        {
            "command": "patch-proposal",
            "proposal_artifact_id": result["artifact_id"],
            "proposal_count": result["proposal_count"],
            "recommended_proposal_count": result["recommended_proposal_count"],
            "no_target_write_attestation": True,
        }
    )
    output(public)
    return 0


def host_execution_request(args: argparse.Namespace, store: StateStore) -> int:
    observed_snapshot = snapshot_hash(args.observed_snapshot_hash)
    request = HostExecutionRequest(args.run_id, args.artifact_file, observed_snapshot)
    run = store.get_run(request.run_id)
    if run["state"] != "READY" or run["current_snapshot_hash"] != request.observed_snapshot_hash:
        raise ValueError("host execution request requires a READY run at its current snapshot")
    public = public_status(run, len(store.list_events(request.run_id)))
    public.update(
        {
            "command": "host-execution-request",
            "disposition": "HOST_EXECUTOR_REQUIRED",
            "execution_request": {
                "run_id": request.run_id,
                "artifact_file": str(request.artifact_file),
                "observed_snapshot_hash": request.observed_snapshot_hash,
            },
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
            "external_command": "nlp-refinement-host review --allow-model-inference --run-id <run-id> --artifact-file <artifact.md-or-json> --observed-snapshot-hash <sha256>",
        }
    )
    output(public)
    return 0


def retention_preview(args: argparse.Namespace, store: StateStore) -> int:
    now = args.now if args.now is not None else now_epoch()
    plan = store.retention_preview(now)
    output({"command": "retention-preview", **plan})
    return 0


def retention_sweep(args: argparse.Namespace, store: StateStore) -> int:
    now = args.now if args.now is not None else now_epoch()
    applied = store.apply_retention(args.confirm_plan_hash, now)
    output({"command": "retention-sweep", **applied})
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = StateStore(args.state_root / "state.db")
    if args.command == "retention-preview" and not store.database_path.exists():
        now = args.now if args.now is not None else now_epoch()
        output({"command": "retention-preview", **store.empty_retention_preview(now)})
        return 0
    store.initialize()
    try:
        if args.command == "start":
            return start(args, store)
        if args.command == "status":
            return status(args, store)
        if args.command == "resume":
            return resume(args, store)
        if args.command == "review":
            return review(args, store)
        if args.command == "patch-proposal":
            return patch_proposal(args, store)
        if args.command == "host-execution-request":
            return host_execution_request(args, store)
        if args.command == "retention-preview":
            return retention_preview(args, store)
        if args.command == "retention-sweep":
            return retention_sweep(args, store)
        raise AssertionError(f"unsupported normal command: {args.command}")
    except (KeyError, OSError, StateStoreError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
