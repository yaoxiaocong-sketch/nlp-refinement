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

from nlp_refinement.artifact_inventory import scan_artifact_inventory
from nlp_refinement.development_runtime import (
    DevelopmentRuntimeError,
    resolve_development_runtime,
    runtime_identity as development_runtime_identity,
)
from nlp_refinement.patch_proposal_adapter import ingest_patch_proposal, load_patch_proposal
from nlp_refinement.review_adapter import ingest_review, load_review_result
from nlp_refinement.sensitive_data import likely_secret_kind
from nlp_refinement.state_store import FINAL_STATES, StateStore, StateStoreError
from nlp_refinement.version import CLI_VERSION


# Project identity is independent of the persisted protocol version.  Keep it
# stable while the project-key algorithm itself is unchanged.
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


def invoked_cli_path() -> Path:
    """Return the console-script path that launched this process.

    Development runtime resolution deliberately applies only to an installed
    console script.  Module execution is not an acceptable substitute for a
    missing CLI during TUI validation.
    """
    candidate = Path(sys.argv[0])
    if not candidate.is_absolute():
        raise DevelopmentRuntimeError("CLI_PATH_INVALID: runtime resolution requires an absolute console-script path")
    if candidate.name != "nlp-refinement":
        raise DevelopmentRuntimeError("CLI_PATH_INVALID: runtime resolution requires the nlp-refinement console script")
    return candidate


def runtime_identity_command() -> int:
    output({"command": "runtime-identity", **development_runtime_identity(invoked_cli_path())})
    return 0


def runtime_resolve_command(args: argparse.Namespace) -> int:
    identity = development_runtime_identity(invoked_cli_path())
    resolved = resolve_development_runtime(
        args.descriptor,
        invoked_executable=invoked_cli_path(),
        identity=identity,
    )
    output({"command": "runtime-resolve", "disposition": "RESOLVED", "resolved_cli": resolved})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nlp-refinement")
    parser.add_argument("--version", action="version", version=CLI_VERSION)
    parser.add_argument("--state-root", type=Path, default=default_state_root())
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser(
        "runtime-identity",
        help="report the machine-readable local CLI interface without creating state",
    )
    runtime_resolve = subcommands.add_parser(
        "runtime-resolve",
        help="verify an explicit development-runtime descriptor without creating state",
    )
    runtime_resolve.add_argument("--descriptor", type=Path, required=True)

    start = subcommands.add_parser("start", help="create a review-only run")
    start.add_argument("--project-dir", type=Path, required=True)
    start.add_argument("--redacted-brief-file", type=Path, required=True)
    start.add_argument("--snapshot-hash", required=True)
    start.add_argument("--project-id", help="stable user-managed identity for a moved project")

    scan = subcommands.add_parser("scan", help="list candidate NL artifacts without creating a run")
    scan.add_argument("root", type=Path)
    batch = subcommands.add_parser("batch-create", help="create review-ready child runs from an inventory")
    batch.add_argument("root", type=Path)
    batch.add_argument(
        "--expected-inventory-hash",
        required=True,
        help="inventory_hash returned by the scan that the user selected from",
    )
    selection = batch.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--select", action="append", metavar="RELATIVE_PATH")
    batch_status = subcommands.add_parser("batch-status", help="read one batch and its child-run states")
    batch_status.add_argument("--batch-id", required=True)
    batch_events = subcommands.add_parser("batch-events", help="read one batch's content-free audit events")
    batch_events.add_argument("--batch-id", required=True)
    tui_batches = subcommands.add_parser(
        "tui-resume-candidates",
        help="read project-scoped resumable Batches for the native TUI workflow",
    )
    tui_batches.add_argument("root", type=Path)
    tui_locators = subcommands.add_parser(
        "tui-resume-locators",
        help="read ledger-derived target roots for resumable native TUI Batches",
    )
    tui_disposition = subcommands.add_parser(
        "tui-workflow-disposition",
        help="derive the native TUI workflow disposition for one current-project run",
    )
    tui_disposition.add_argument("root", type=Path)
    tui_disposition.add_argument("--run-id", required=True)
    tui_handoff = subcommands.add_parser(
        "tui-proposal-handoff",
        help="build a snapshot-bound proposer handoff from accepted ledger facts",
    )
    tui_handoff.add_argument("root", type=Path)
    tui_handoff.add_argument("--run-id", required=True)
    tui_review_handoff = subcommands.add_parser(
        "tui-review-handoff",
        help="build a snapshot-bound reviewer handoff from one ready ledger run",
    )
    tui_review_handoff.add_argument("root", type=Path)
    tui_review_handoff.add_argument("--run-id", required=True)
    tui_transient = subcommands.add_parser(
        "tui-create-transient-root",
        help="create one private run-scoped system-temporary directory for TUI child JSON",
    )
    tui_transient.add_argument("root", type=Path)
    tui_transient.add_argument("--run-id", required=True)
    tui_cleanup = subcommands.add_parser(
        "tui-cleanup-transient-root",
        help="remove one verified private run-scoped system-temporary directory",
    )
    tui_cleanup.add_argument("root", type=Path)
    tui_cleanup.add_argument("--run-id", required=True)
    tui_cleanup.add_argument("--transient-root", type=Path, required=True)
    tui_review_record = subcommands.add_parser(
        "tui-record-review",
        help="record one reviewer result only from a private TUI transient directory",
    )
    tui_review_record.add_argument("root", type=Path)
    tui_review_record.add_argument("--run-id", required=True)
    tui_review_record.add_argument("--review-result-file", type=Path, required=True)
    tui_review_record.add_argument("--transient-root", type=Path, required=True)
    tui_review_record.add_argument("--observed-snapshot-hash", required=True)
    tui_review_record.add_argument(
        "--redaction-status", choices=("caller-attested-redacted",), required=True,
    )
    tui_receipt = subcommands.add_parser(
        "tui-record-child-dispatch-receipt",
        help="persist one content-free root-attested native-child dispatch receipt",
    )
    tui_receipt.add_argument("root", type=Path)
    tui_receipt.add_argument("--run-id", required=True)
    tui_receipt.add_argument("--receipt-file", type=Path, required=True)
    tui_receipt.add_argument("--transient-root", type=Path, required=True)
    tui_receipts = subcommands.add_parser(
        "tui-child-dispatch-receipts",
        help="read content-free persisted native-child dispatch receipts for one run",
    )
    tui_receipts.add_argument("root", type=Path)
    tui_receipts.add_argument("--run-id", required=True)
    tui_draft = subcommands.add_parser(
        "tui-proposal-draft-handoff",
        help="validate one untrusted proposer draft and build the assessor handoff without writing state",
    )
    tui_draft.add_argument("root", type=Path)
    tui_draft.add_argument("--run-id", required=True)
    tui_draft.add_argument("--proposal-draft-file", type=Path, required=True)
    tui_draft.add_argument("--transient-root", type=Path)
    tui_compose = subcommands.add_parser(
        "tui-compose-assessed-proposal",
        help="validate one draft and independent assessment, then render an unpersisted v6 proposal",
    )
    tui_compose.add_argument("root", type=Path)
    tui_compose.add_argument("--run-id", required=True)
    tui_compose.add_argument("--proposal-draft-file", type=Path, required=True)
    tui_compose.add_argument("--proposal-assessment-file", type=Path, required=True)
    tui_compose.add_argument("--transient-root", type=Path, required=True)
    tui_contract = subcommands.add_parser(
        "tui-child-contract",
        help="render one versioned output contract for a generic native TUI child without creating state",
    )
    tui_contract.add_argument(
        "--role", choices=("reviewer", "proposer", "proposal-assessor"), required=True,
    )
    tui_bootstrap = subcommands.add_parser(
        "tui-child-bootstrap",
        help="render a bounded native-child bootstrap prompt without creating a child",
    )
    tui_bootstrap.add_argument("root", type=Path)
    tui_bootstrap.add_argument("--run-id", required=True)
    tui_bootstrap.add_argument(
        "--role", choices=("reviewer", "proposer", "proposal-assessor"), required=True,
    )
    tui_bootstrap.add_argument("--proposal-draft-file", type=Path)
    tui_bootstrap.add_argument("--transient-root", type=Path)

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
        "schema_version": "5.0",
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
        "retention_policy_version": "5.0",
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


def scan(args: argparse.Namespace) -> int:
    inventory = scan_artifact_inventory(args.root)
    output({"command": "scan", "root": str(args.root.resolve(strict=True)), "inventory": inventory})
    return 0


def batch_create(args: argparse.Namespace, store: StateStore) -> int:
    expected_inventory_hash = snapshot_hash(args.expected_inventory_hash)
    inventory = scan_artifact_inventory(args.root)
    observed_inventory_hash = inventory["inventory_hash"]
    if expected_inventory_hash != observed_inventory_hash:
        output(
            {
                "command": "batch-create",
                "disposition": "INVENTORY_STALE",
                "expected_inventory_hash": expected_inventory_hash,
                "observed_inventory_hash": observed_inventory_hash,
                "no_model_call_attestation": True,
                "no_target_write_attestation": True,
            }
        )
        return 0
    candidates = inventory["candidates"]
    requested = {candidate["relative_path"] for candidate in candidates} if args.all else set(args.select or [])
    selected = [candidate for candidate in candidates if candidate["relative_path"] in requested]
    if not selected or requested != {candidate["relative_path"] for candidate in selected}:
        raise ValueError("batch selection must name one or more current candidate relative paths")
    identity, identity_mode = project_identity(args.root, None)
    key = project_key(identity)
    batch_id = f"batch_{secrets.token_hex(12)}"
    children: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for candidate in selected:
        run_id = f"run_{secrets.token_hex(12)}"
        snapshot = str(candidate["sha256"])
        run = {"schema_version": "5.0", "run_id": run_id, "project_key": key, "target_snapshot_hash": snapshot, "current_snapshot_hash": snapshot, "authorization_mode": "review-only", "state": "DRAFT", "outcome": "IN_PROGRESS", "revision": 0, "repair_cycle_count": 0, "max_repair_cycles": 3, "open_high_risk_assumptions": 0, "retention_policy_version": "5.0"}
        anchor = {"batch_id": batch_id, "relative_path": candidate["relative_path"], "project_identity": identity, "identity_mode": identity_mode, "review_intent": "system-default-nl-artifact-review"}
        children.append((run, anchor, str(candidate["relative_path"])))
    store.initialize()
    result = store.create_batch_with_ready_children(
        {"batch_id": batch_id, "project_key": key, "inventory_hash": inventory["inventory_hash"]},
        children,
        now=now_epoch(),
    )
    output({"command": "batch-create", "batch": result, "no_model_call_attestation": True, "no_target_write_attestation": True})
    return 0


def batch_status(args: argparse.Namespace, store: StateStore) -> int:
    output({"command": "batch-status", "batch": store.get_batch(args.batch_id)})
    return 0


def batch_events(args: argparse.Namespace, store: StateStore) -> int:
    output(
        {
            "command": "batch-events",
            "batch_id": args.batch_id,
            "events": store.list_batch_events(args.batch_id),
        }
    )
    return 0


def tui_resume_candidates(args: argparse.Namespace, store: StateStore) -> int:
    from nlp_refinement.tui_workflow import find_resumable_batches

    batch_ids = find_resumable_batches(store, args.root)
    output(
        {
            "command": "tui-resume-candidates",
            "batches": [store.get_batch(batch_id) for batch_id in batch_ids],
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def tui_resume_locators(store: StateStore) -> int:
    from nlp_refinement.tui_workflow import find_resumable_locators

    output(
        {
            "command": "tui-resume-locators",
            **find_resumable_locators(store),
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def tui_workflow_disposition(args: argparse.Namespace, store: StateStore) -> int:
    from nlp_refinement.tui_workflow import _batch_item_for_run, workflow_disposition

    run = store.get_run(args.run_id)
    _batch_item_for_run(store, args.root, args.run_id)
    output(
        {
            "command": "tui-workflow-disposition",
            "run_id": args.run_id,
            "disposition": workflow_disposition(
                run, store.list_findings(args.run_id), store.list_artifacts(args.run_id)
            ),
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def tui_proposal_handoff(args: argparse.Namespace, store: StateStore) -> int:
    from nlp_refinement.tui_workflow import build_proposal_handoff

    output(
        {
            "command": "tui-proposal-handoff",
            "proposal_handoff": build_proposal_handoff(store, args.root, args.run_id),
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def tui_review_handoff(args: argparse.Namespace, store: StateStore) -> int:
    from nlp_refinement.tui_workflow import build_reviewer_handoff

    output(
        {
            "command": "tui-review-handoff",
            "review_handoff": build_reviewer_handoff(store, args.root, args.run_id),
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def _verify_tui_project_run(args: argparse.Namespace, store: StateStore) -> None:
    from nlp_refinement.tui_workflow import _batch_item_for_run

    store.get_run(args.run_id)
    _batch_item_for_run(store, args.root, args.run_id)


def tui_create_transient_root(args: argparse.Namespace, store: StateStore) -> int:
    from nlp_refinement.tui_workflow import create_tui_transient_root

    _verify_tui_project_run(args, store)
    transient_root = create_tui_transient_root(args.root, args.run_id)
    output(
        {
            "command": "tui-create-transient-root",
            "transient_root": str(transient_root),
            "run_id": args.run_id,
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def tui_cleanup_transient_root(args: argparse.Namespace, store: StateStore) -> int:
    from nlp_refinement.tui_workflow import cleanup_tui_transient_root

    _verify_tui_project_run(args, store)
    cleanup_tui_transient_root(
        project_root=args.root, transient_root=args.transient_root, run_id=args.run_id,
    )
    output(
        {
            "command": "tui-cleanup-transient-root",
            "run_id": args.run_id,
            "transient_root_removed": True,
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def tui_record_review(args: argparse.Namespace, store: StateStore) -> int:
    from nlp_refinement.tui_workflow import validate_tui_transient_file

    _verify_tui_project_run(args, store)
    validated = validate_tui_transient_file(
        project_root=args.root,
        transient_root=args.transient_root,
        candidate=args.review_result_file,
        run_id=args.run_id,
        kind="review result",
    )
    args.review_result_file = validated
    return review(args, store)


def tui_record_child_dispatch_receipt(args: argparse.Namespace, store: StateStore) -> int:
    from nlp_refinement.tui_workflow import validate_tui_transient_file

    _verify_tui_project_run(args, store)
    receipt_path = validate_tui_transient_file(
        project_root=args.root,
        transient_root=args.transient_root,
        candidate=args.receipt_file,
        run_id=args.run_id,
        kind="child dispatch receipt",
    )
    receipt = _load_tui_json(receipt_path, kind="child dispatch receipt")
    persisted = store.record_tui_child_dispatch_receipt(args.run_id, receipt, now_epoch())
    output(
        {
            "command": "tui-record-child-dispatch-receipt",
            "run_id": args.run_id,
            "receipt": persisted,
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def tui_child_dispatch_receipts(args: argparse.Namespace, store: StateStore) -> int:
    _verify_tui_project_run(args, store)
    output(
        {
            "command": "tui-child-dispatch-receipts",
            "run_id": args.run_id,
            "receipts": store.list_tui_child_dispatch_receipts(args.run_id),
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def tui_child_contract(args: argparse.Namespace) -> int:
    from nlp_refinement.tui_workflow import build_child_contract

    output(
        {
            "command": "tui-child-contract",
            "child_contract": build_child_contract(args.role),
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def tui_child_bootstrap(args: argparse.Namespace, store: StateStore) -> int:
    from nlp_refinement.tui_workflow import (
        build_child_bootstrap,
        build_proposal_assessment_handoff,
        build_proposal_handoff,
        build_reviewer_handoff,
        validate_tui_transient_file,
    )

    transient_root = args.transient_root
    if args.role == "reviewer":
        build_reviewer_handoff(store, args.root, args.run_id)
    elif args.role == "proposer":
        build_proposal_handoff(store, args.root, args.run_id)
    else:
        if args.proposal_draft_file is None:
            raise ValueError("proposal assessor bootstrap requires --proposal-draft-file")
        transient_root = args.transient_root or args.proposal_draft_file.parent
        draft_path = validate_tui_transient_file(
            project_root=args.root,
            transient_root=transient_root,
            candidate=args.proposal_draft_file,
            run_id=args.run_id,
            kind="proposal draft",
        )
        draft = _load_tui_json(draft_path, kind="proposal draft")
        build_proposal_assessment_handoff(
            draft, build_proposal_handoff(store, args.root, args.run_id),
        )
    output(
        {
            "command": "tui-child-bootstrap",
            "child_bootstrap": build_child_bootstrap(
                role=args.role,
                executable=invoked_cli_path(),
                state_root=args.state_root,
                project_root=args.root,
                run_id=args.run_id,
                proposal_draft_file=args.proposal_draft_file,
                transient_root=transient_root,
            ),
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def _load_tui_json(path: Path, *, kind: str) -> dict[str, Any]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{kind} must be one readable JSON object") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"{kind} must be one JSON object")
    return decoded


def tui_proposal_draft_handoff(args: argparse.Namespace, store: StateStore) -> int:
    from nlp_refinement.tui_workflow import (
        build_proposal_assessment_handoff, build_proposal_handoff, validate_tui_transient_file,
    )

    draft = _load_tui_json(
        validate_tui_transient_file(
            project_root=args.root,
            transient_root=args.transient_root or args.proposal_draft_file.parent,
            candidate=args.proposal_draft_file,
            run_id=args.run_id,
            kind="proposal draft",
        ),
        kind="proposal draft",
    )
    handoff = build_proposal_handoff(store, args.root, args.run_id)
    output(
        {
            "command": "tui-proposal-draft-handoff",
            "proposal_assessment_handoff": build_proposal_assessment_handoff(draft, handoff),
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
    return 0


def tui_compose_assessed_proposal(args: argparse.Namespace, store: StateStore) -> int:
    from nlp_refinement.tui_workflow import (
        build_proposal_handoff, compose_assessed_v6_proposal, validate_tui_transient_file,
    )

    handoff = build_proposal_handoff(store, args.root, args.run_id)
    draft = _load_tui_json(validate_tui_transient_file(
        project_root=args.root, transient_root=args.transient_root,
        candidate=args.proposal_draft_file, run_id=args.run_id, kind="proposal draft",
    ), kind="proposal draft")
    assessment = _load_tui_json(validate_tui_transient_file(
        project_root=args.root, transient_root=args.transient_root,
        candidate=args.proposal_assessment_file, run_id=args.run_id, kind="proposal assessment",
    ), kind="proposal assessment")
    output(
        {
            "command": "tui-compose-assessed-proposal",
            "proposal_result": compose_assessed_v6_proposal(draft, assessment, handoff),
            "no_model_call_attestation": True,
            "no_target_write_attestation": True,
        }
    )
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
    if args.command in {"runtime-identity", "runtime-resolve"}:
        try:
            if args.command == "runtime-identity":
                return runtime_identity_command()
            return runtime_resolve_command(args)
        except (DevelopmentRuntimeError, OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    if args.command == "tui-child-contract":
        try:
            return tui_child_contract(args)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    if args.command == "scan":
        try:
            return scan(args)
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    store = StateStore(args.state_root / "state.db")
    if args.command == "batch-create":
        try:
            return batch_create(args, store)
        except (KeyError, OSError, StateStoreError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    if args.command == "retention-preview" and not store.database_path.exists():
        now = args.now if args.now is not None else now_epoch()
        output({"command": "retention-preview", **store.empty_retention_preview(now)})
        return 0
    store.initialize()
    try:
        if args.command == "start":
            return start(args, store)
        if args.command == "batch-status":
            return batch_status(args, store)
        if args.command == "batch-events":
            return batch_events(args, store)
        if args.command == "tui-resume-candidates":
            return tui_resume_candidates(args, store)
        if args.command == "tui-resume-locators":
            return tui_resume_locators(store)
        if args.command == "tui-workflow-disposition":
            return tui_workflow_disposition(args, store)
        if args.command == "tui-proposal-handoff":
            return tui_proposal_handoff(args, store)
        if args.command == "tui-review-handoff":
            return tui_review_handoff(args, store)
        if args.command == "tui-create-transient-root":
            return tui_create_transient_root(args, store)
        if args.command == "tui-cleanup-transient-root":
            return tui_cleanup_transient_root(args, store)
        if args.command == "tui-record-review":
            return tui_record_review(args, store)
        if args.command == "tui-record-child-dispatch-receipt":
            return tui_record_child_dispatch_receipt(args, store)
        if args.command == "tui-child-dispatch-receipts":
            return tui_child_dispatch_receipts(args, store)
        if args.command == "tui-child-bootstrap":
            return tui_child_bootstrap(args, store)
        if args.command == "tui-proposal-draft-handoff":
            return tui_proposal_draft_handoff(args, store)
        if args.command == "tui-compose-assessed-proposal":
            return tui_compose_assessed_proposal(args, store)
        if args.command == "status":
            return status(args, store)
        if args.command == "resume":
            return resume(args, store)
        if args.command == "review":
            return review(args, store)
        if args.command == "patch-proposal":
            return patch_proposal(args, store)
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
