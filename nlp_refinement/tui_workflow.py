"""Pure local workflow facts for the TUI-native review → proposal procedure.

This module neither creates Codex children nor calls a model.  It builds the
small, accepted-ledger handoff that a TUI root may provide to a proposal child.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from nlp_refinement.refine import project_identity, project_key
from nlp_refinement.state_store import FINAL_STATES, StateStore


class SelectionInputError(ValueError):
    """Raised when an inventory selection is outside the displayed choices."""


class WorkflowStateError(ValueError):
    """Raised when a requested workflow phase has no accepted prerequisite."""


class SnapshotStaleError(WorkflowStateError):
    """Raised when the current artifact bytes differ from the stored snapshot."""


_INDEX = re.compile(r"[1-9][0-9]*")
_RANGE = re.compile(r"([1-9][0-9]*)-([1-9][0-9]*)")


def parse_selection(value: str, candidates: list[str]) -> tuple[str, ...] | None:
    """Parse the explicit TUI selection grammar without choosing on the user's behalf."""
    normalized = value.strip().lower()
    if normalized == "cancel":
        return None
    if normalized == "all":
        return tuple(candidates)
    if not normalized or not candidates:
        raise SelectionInputError("selection must be all, cancel, indexes, or index ranges")
    indexes: set[int] = set()
    for token in (part.strip() for part in normalized.split(",")):
        match = _RANGE.fullmatch(token)
        if match is not None:
            start, end = (int(match.group(1)), int(match.group(2)))
            if start > end:
                raise SelectionInputError("selection range must ascend")
            indexes.update(range(start, end + 1))
            continue
        if _INDEX.fullmatch(token) is not None:
            indexes.add(int(token))
            continue
        raise SelectionInputError("selection contains an invalid token")
    if not indexes or min(indexes) < 1 or max(indexes) > len(candidates):
        raise SelectionInputError("selection index is outside the displayed inventory")
    return tuple(candidates[index - 1] for index in sorted(indexes))


def _proposal_artifact(artifact: dict[str, Any]) -> bool:
    try:
        value = json.loads(str(artifact["body"]))
    except (KeyError, TypeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("artifact_role") == "patch-proposal"


def _review_artifact(artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for artifact in artifacts:
        try:
            value = json.loads(str(artifact["body"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("phase_name") == "review":
            return artifact
    return None


def workflow_disposition(
    run: dict[str, Any], findings: list[dict[str, Any]], artifacts: list[dict[str, Any]],
) -> str:
    """Derive TUI work from ledger facts without adding a second run state machine."""
    state = str(run["state"])
    if state == "SNAPSHOT_STALE":
        return "SNAPSHOT_STALE"
    if state == "BLOCKED":
        return "BLOCKED"
    if state in FINAL_STATES:
        return state
    if any(_proposal_artifact(artifact) for artifact in artifacts):
        return "AWAITING_HUMAN"
    review = _review_artifact(artifacts)
    open_findings = [finding for finding in findings if finding.get("status") == "open"]
    if review is not None and open_findings:
        return "NEEDS_PROPOSAL"
    if review is not None:
        return "AWAITING_HUMAN"
    return "NEEDS_REVIEW"


def find_resumable_batches(store: StateStore, project_root: Path) -> list[str]:
    """Return project-scoped Batches that still contain a non-terminal child run."""
    identity, _ = project_identity(project_root, None)
    key = project_key(identity)
    batches = store.list_batches_for_project(key)
    return [
        str(batch["batch_id"])
        for batch in batches
        if any(
            item["member_status"] == "ACTIVE" and item["state"] not in FINAL_STATES
            for item in batch["items"]
        )
    ]


def _batch_item_for_run(store: StateStore, project_root: Path, run_id: str) -> dict[str, Any]:
    identity, _ = project_identity(project_root, None)
    key = project_key(identity)
    matches = [
        item
        for batch in store.list_batches_for_project(key)
        for item in batch["items"]
        if item["child_run_id"] == run_id and item["member_status"] == "ACTIVE"
    ]
    if len(matches) != 1:
        raise WorkflowStateError("run is not one active member of the current project's retained Batches")
    return matches[0]


def _read_current_artifact(project_root: Path, relative_path: str, expected_hash: str) -> tuple[Path, str]:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise WorkflowStateError("Batch member path is invalid")
    root = project_root.resolve(strict=True)
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise SnapshotStaleError("artifact is no longer a regular in-project file")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise SnapshotStaleError("artifact no longer resolves beneath the project root") from error
    raw = resolved.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise SnapshotStaleError("artifact snapshot differs from the accepted run")
    try:
        return resolved, raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SnapshotStaleError("artifact is no longer UTF-8") from error


def _validated_findings(
    findings: list[dict[str, Any]], review: dict[str, Any], review_artifact_id: str,
) -> list[dict[str, Any]]:
    try:
        review_result = json.loads(str(review["body"]))
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise WorkflowStateError("accepted review artifact is not valid JSON") from error
    incoming = {
        item["fingerprint"]: item
        for item in review_result.get("findings", [])
        if isinstance(item, dict) and isinstance(item.get("fingerprint"), str)
    }
    rendered: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("status") != "open":
            continue
        source = incoming.get(finding.get("fingerprint"))
        if source is None or review_artifact_id not in finding.get("evidence_refs", []):
            raise WorkflowStateError("open finding is not bound to the accepted review artifact")
        rendered.append(
            {
                "finding_id": finding["finding_id"],
                "invariant_id": finding["invariant_id"],
                "statement": source.get("statement"),
                "evidence": source.get("evidence"),
                "recommendation": source.get("recommendation"),
                "evidence_refs": [review_artifact_id],
            }
        )
    return rendered


def build_proposal_handoff(store: StateStore, project_root: Path, run_id: str) -> dict[str, Any]:
    """Build the proposer input from current bytes plus accepted ledger facts only."""
    run = store.get_run(run_id)
    item = _batch_item_for_run(store, project_root, run_id)
    snapshot = str(run["current_snapshot_hash"])
    if item["snapshot_hash"] != snapshot:
        raise WorkflowStateError("Batch member snapshot does not match its run")
    _, artifact_model_view = _read_current_artifact(project_root, str(item["relative_path"]), snapshot)
    findings = store.list_findings(run_id)
    artifacts = store.list_artifacts(run_id)
    if workflow_disposition(run, findings, artifacts) != "NEEDS_PROPOSAL":
        raise WorkflowStateError("run does not have an accepted review with open findings awaiting proposal")
    review = _review_artifact(artifacts)
    if review is None:
        raise WorkflowStateError("accepted review artifact is missing")
    review_artifact_id = str(review["artifact_id"])
    validated_findings = _validated_findings(findings, review, review_artifact_id)
    return {
        "run_id": run_id,
        "snapshot_hash": snapshot,
        "artifact_relative_path": item["relative_path"],
        "artifact_model_view": artifact_model_view,
        "review_artifact_id": review_artifact_id,
        "open_finding_ids": [finding["finding_id"] for finding in validated_findings],
        "validated_findings": validated_findings,
        "proposal_schema": "patch-proposal-result/v6",
    }
