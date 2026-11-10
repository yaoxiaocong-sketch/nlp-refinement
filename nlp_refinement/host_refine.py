"""Explicit external-host entrypoint for optional M8 model execution."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nlp_refinement.host_executor import AppServerHostExecutor
from nlp_refinement.host_execution_request import HostExecutionRequest
from nlp_refinement.m8_review_coordinator import M8ReviewCoordinator, M8ReviewCoordinatorError
from nlp_refinement.refine import default_state_root, output, public_status, snapshot_hash
from nlp_refinement.state_store import StateStore, StateStoreError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nlp-refinement-host")
    parser.add_argument("--state-root", type=Path, default=default_state_root())
    subcommands = parser.add_subparsers(dest="command", required=True)
    review = subcommands.add_parser(
        "review",
        help="run the optional M8 host review from an external host process",
        description=(
            "Run M8 through the local Codex host. NLP-refinement retention covers "
            "only its SQLite ledger; Codex host session history follows Codex configuration."
        ),
    )
    review.add_argument("--run-id", required=True)
    review.add_argument("--artifact-file", type=Path, required=True)
    review.add_argument("--observed-snapshot-hash", required=True)
    review.add_argument(
        "--allow-model-inference",
        action="store_true",
        help="explicitly authorize this external host command to call the local Codex app-server",
    )
    return parser


def review(args: argparse.Namespace, store: StateStore) -> int:
    if not args.allow_model_inference:
        raise ValueError("external host review requires --allow-model-inference")
    request = HostExecutionRequest(
        run_id=args.run_id,
        artifact_file=args.artifact_file,
        observed_snapshot_hash=snapshot_hash(args.observed_snapshot_hash),
    )
    result = M8ReviewCoordinator(store, executor_factory=AppServerHostExecutor).run(
        request=request,
    )
    run = store.get_run(args.run_id)
    public = public_status(run, len(store.list_events(args.run_id)))
    public.update({
        "command": "external-host-review",
        "executor_kind": "external-host-cli",
        "review_receipt_artifact_id": result.review_receipt_artifact_id,
        "review_artifact_id": result.review_artifact_id,
        "proposal_receipt_artifact_id": result.proposal_receipt_artifact_id,
        "proposal_artifact_id": result.proposal_artifact_id,
        "new_finding_ids": result.new_finding_ids,
        "recommended_proposal_count": result.recommended_proposal_count,
        "no_target_write_attestation": True,
    })
    output(public)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.allow_model_inference:
        print("error: external host review requires --allow-model-inference", file=sys.stderr)
        return 2
    store = StateStore(args.state_root / "state.db")
    store.initialize()
    try:
        return review(args, store)
    except (KeyError, OSError, StateStoreError, ValueError, M8ReviewCoordinatorError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
