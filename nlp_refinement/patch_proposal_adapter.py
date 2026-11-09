"""M5 adapter for auditable, human-only patch proposals.

Patch text is untrusted evidence.  This module validates and records it; it
never opens, reads, writes, or applies the referenced target artifact.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path, PurePosixPath
from typing import Any

from nlp_refinement.state_store import StateStore


HASH = re.compile(r"[a-f0-9]{64}")
HUNK_HEADER = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?: .*)?$")
REQUIRED_RESULT = {
    "schema_version", "phase_name", "proposer_id", "summary",
    "base_snapshot_hash", "no_target_write_attestation", "proposals",
}
REQUIRED_PROPOSAL = {
    "finding_id", "target_reference", "unified_diff", "rationale", "target_invariants",
    "causal_claim", "causal_evidence_refs", "authorization_basis", "verification_plan",
    "regression_plan", "known_risks", "alternatives_considered", "claims_not_yet_verified",
    "assessment",
}
REQUIRED_ASSESSMENT = {
    "assessor_id", "evidence_refs", "scores", "total_score", "recommendation", "runtime_effectiveness",
}
SCORE_LIMITS = {
    "coverage": 20, "causal": 20, "scope": 15, "verification": 15,
    "regression": 10, "evidence": 10, "alternatives": 5, "clarity": 5,
}
MAX_DIFF_BYTES = 64 * 1024


class PatchProposalInputError(ValueError):
    """Raised when a submitted human-only patch proposal is invalid."""


def load_patch_proposal(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PatchProposalInputError(f"patch proposal is not valid JSON: {error.msg}") from error
    if not isinstance(loaded, dict):
        raise PatchProposalInputError("patch proposal must be a JSON object")
    validate_patch_proposal(loaded)
    return loaded


def _validate_target_reference(value: str) -> None:
    candidate = PurePosixPath(value)
    parts = value.split("/")
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or "\\" in value
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise PatchProposalInputError("target_reference must be a relative path without traversal")


def _hunk_count(match: re.Match[str], name: str) -> int:
    value = match.group(name)
    return 1 if value is None else int(value)


def _validate_hunk(lines: list[str], start: int) -> int:
    header = HUNK_HEADER.fullmatch(lines[start])
    if header is None:
        raise PatchProposalInputError("unified_diff has a malformed hunk header")
    old_remaining = _hunk_count(header, "old_count")
    new_remaining = _hunk_count(header, "new_count")
    index = start + 1
    saw_content = False
    just_saw_no_newline_marker = False
    while index < len(lines):
        line = lines[index]
        if line.startswith("@@ "):
            break
        if line.startswith("--- ") and old_remaining == 0 and new_remaining == 0:
            break
        if line == "\\ No newline at end of file":
            if not saw_content or just_saw_no_newline_marker:
                raise PatchProposalInputError("unified_diff has an invalid no-newline marker")
            just_saw_no_newline_marker = True
            index += 1
            continue
        if not line or line[0] not in {" ", "+", "-"}:
            raise PatchProposalInputError("unified_diff has an invalid hunk content line")
        if line[0] in {" ", "-"}:
            old_remaining -= 1
        if line[0] in {" ", "+"}:
            new_remaining -= 1
        if old_remaining < 0 or new_remaining < 0:
            raise PatchProposalInputError("unified_diff hunk line counts exceed its header")
        saw_content = True
        just_saw_no_newline_marker = False
        index += 1
    if old_remaining != 0 or new_remaining != 0:
        raise PatchProposalInputError("unified_diff hunk line counts do not match its header")
    return index


def _validate_unified_diff(value: str) -> None:
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_DIFF_BYTES:
        raise PatchProposalInputError("unified_diff exceeds the 64 KiB proposal limit")
    if "\x00" in value or not value.endswith("\n"):
        raise PatchProposalInputError("unified_diff must be NUL-free and end with a newline")
    lines = value.splitlines()
    index = 0
    file_blocks = 0
    while index < len(lines):
        if (
            index + 2 >= len(lines)
            or not lines[index].startswith("--- ")
            or not lines[index][4:].strip()
            or not lines[index + 1].startswith("+++ ")
            or not lines[index + 1][4:].strip()
        ):
            raise PatchProposalInputError("unified_diff must contain paired --- and +++ file headers")
        index += 2
        hunk_count = 0
        while index < len(lines) and not (lines[index].startswith("--- ") and hunk_count > 0):
            index = _validate_hunk(lines, index)
            hunk_count += 1
        if hunk_count == 0:
            raise PatchProposalInputError("unified_diff file block must contain a hunk")
        file_blocks += 1
    if file_blocks == 0:
        raise PatchProposalInputError("unified_diff must contain a file block")


def _validate_assessment(value: Any, proposer_id: str) -> None:
    if not isinstance(value, dict) or set(value) != REQUIRED_ASSESSMENT:
        raise PatchProposalInputError("proposal assessment has unexpected or missing fields")
    if not isinstance(value["assessor_id"], str) or not value["assessor_id"].strip():
        raise PatchProposalInputError("proposal assessment assessor_id must be a non-empty string")
    if value["assessor_id"] == proposer_id:
        raise PatchProposalInputError("proposal assessment must use a distinct assessor_id")
    if not isinstance(value["evidence_refs"], list) or not value["evidence_refs"] or not all(
        isinstance(reference, str) and reference for reference in value["evidence_refs"]
    ):
        raise PatchProposalInputError("proposal assessment evidence_refs must be a non-empty string array")
    if not isinstance(value["scores"], dict) or set(value["scores"]) != set(SCORE_LIMITS):
        raise PatchProposalInputError("proposal assessment scores must contain the complete scorecard")
    for dimension, maximum in SCORE_LIMITS.items():
        score = value["scores"][dimension]
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= maximum:
            raise PatchProposalInputError(f"proposal assessment {dimension} score is invalid")
    total = sum(value["scores"].values())
    if value["total_score"] != total:
        raise PatchProposalInputError("proposal assessment total_score must equal scorecard sum")
    if value["recommendation"] not in {"recommend", "needs-user-decision", "not-recommended"}:
        raise PatchProposalInputError("proposal assessment recommendation is invalid")
    if value["runtime_effectiveness"] != "unverified":
        raise PatchProposalInputError("proposal assessment runtime_effectiveness must remain unverified")
    if value["recommendation"] == "recommend" and (
        total < 70
        or value["scores"]["causal"] < 12
        or value["scores"]["verification"] < 10
        or value["scores"]["evidence"] < 7
    ):
        raise PatchProposalInputError("recommendation requires the minimum evidence-backed score thresholds")


def validate_patch_proposal(result: dict[str, Any]) -> None:
    if set(result) != REQUIRED_RESULT:
        raise PatchProposalInputError("patch proposal has unexpected or missing fields")
    if result["schema_version"] != "4.0" or result["phase_name"] != "patch-proposal":
        raise PatchProposalInputError("patch proposal has an unsupported schema or phase")
    for field in ("proposer_id", "summary", "base_snapshot_hash"):
        if not isinstance(result[field], str) or not result[field].strip():
            raise PatchProposalInputError(f"patch proposal {field} must be a non-empty string")
    if HASH.fullmatch(result["base_snapshot_hash"]) is None:
        raise PatchProposalInputError("patch proposal base_snapshot_hash must be lowercase SHA-256 hex")
    if result["no_target_write_attestation"] is not True:
        raise PatchProposalInputError("patch proposal must attest that no target write occurred")
    if not isinstance(result["proposals"], list) or not result["proposals"]:
        raise PatchProposalInputError("patch proposal proposals must be a non-empty array")
    if len(result["proposals"]) > 50:
        raise PatchProposalInputError("patch proposal may contain at most 50 proposals")
    finding_ids: set[str] = set()
    for proposal in result["proposals"]:
        if not isinstance(proposal, dict) or set(proposal) != REQUIRED_PROPOSAL:
            raise PatchProposalInputError("patch proposal item has unexpected or missing fields")
        for field in ("finding_id", "target_reference", "unified_diff", "rationale", "causal_claim", "authorization_basis", "verification_plan", "regression_plan", "known_risks", "alternatives_considered", "claims_not_yet_verified"):
            if not isinstance(proposal[field], str) or not proposal[field].strip():
                raise PatchProposalInputError(f"patch proposal {field} must be a non-empty string")
        if proposal["authorization_basis"] != "review-only-human-application":
            raise PatchProposalInputError("patch proposal authorization_basis is invalid")
        for field in ("target_invariants", "causal_evidence_refs"):
            if not isinstance(proposal[field], list) or not proposal[field] or not all(
                isinstance(value, str) and value for value in proposal[field]
            ):
                raise PatchProposalInputError(f"patch proposal {field} must be a non-empty string array")
        _validate_assessment(proposal["assessment"], result["proposer_id"])
        if proposal["finding_id"] in finding_ids:
            raise PatchProposalInputError("patch proposal may contain one item per finding")
        finding_ids.add(proposal["finding_id"])
        _validate_target_reference(proposal["target_reference"])
        _validate_unified_diff(proposal["unified_diff"])


def ingest_patch_proposal(
    store: StateStore,
    run_id: str,
    proposal_result: dict[str, Any],
    observed_snapshot_hash: str,
    redaction_status: str,
    now: int,
    lease_token: str | None = None,
) -> dict[str, Any]:
    """Record a proposal and attach its evidence to existing open findings."""
    validate_patch_proposal(proposal_result)
    run = store.get_run(run_id)
    if run["authorization_mode"] != "review-only":
        raise PatchProposalInputError("patch proposal accepts only review-only runs")
    if run["state"] != "TRIAGING":
        raise PatchProposalInputError(f"patch proposal requires TRIAGING state, found {run['state']}")

    owned_lease = lease_token is None
    token = lease_token or store.acquire_lease(run_id, f"m5-patch-proposal-{secrets.token_hex(8)}", now=now)["token"]
    try:
        run = store.get_run(run_id)
        if observed_snapshot_hash != run["current_snapshot_hash"]:
            stale = store.transition(
                run_id,
                expected_revision=run["revision"],
                lease_token=token,
                next_state="TRIAGING",
                next_outcome="IN_PROGRESS",
                observed_snapshot_hash=observed_snapshot_hash,
                now=now,
            )
            return {"run": stale, "artifact_id": None, "proposal_count": 0, "recommended_proposal_count": 0}
        if proposal_result["base_snapshot_hash"] != observed_snapshot_hash:
            raise PatchProposalInputError("patch proposal base snapshot must equal observed snapshot")
        findings = {finding["finding_id"]: finding for finding in store.list_findings(run_id)}
        proposal_finding_ids = [proposal["finding_id"] for proposal in proposal_result["proposals"]]
        for finding_id in proposal_finding_ids:
            finding = findings.get(finding_id)
            if finding is None:
                raise PatchProposalInputError(f"patch proposal references unknown finding: {finding_id}")
            if finding["status"] != "open":
                raise PatchProposalInputError(f"patch proposal requires an open finding: {finding_id}")
        for proposal in proposal_result["proposals"]:
            finding = findings[proposal["finding_id"]]
            if finding["invariant_id"] not in proposal["target_invariants"]:
                raise PatchProposalInputError("patch proposal target_invariants must include the linked finding invariant")
            permitted_evidence = set(finding["evidence_refs"])
            for field in ("causal_evidence_refs",):
                if not set(proposal[field]).issubset(permitted_evidence):
                    raise PatchProposalInputError("patch proposal causal evidence must reference linked finding evidence")
            if not set(proposal["assessment"]["evidence_refs"]).issubset(permitted_evidence):
                raise PatchProposalInputError("proposal assessment evidence must reference linked finding evidence")
            if proposal["assessment"]["recommendation"] == "recommend" and run["open_high_risk_assumptions"]:
                raise PatchProposalInputError("cannot recommend a proposal while high-risk assumptions remain open")

        source_refs = sorted({
            reference
            for finding_id in proposal_finding_ids
            for reference in findings.get(finding_id, {}).get("evidence_refs", [])
        })
        proposal_body = json.dumps(
            {
                "artifact_role": "patch-proposal",
                "no_target_write_attestation": True,
                "proposal_result": proposal_result,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        applied = store.apply_patch_proposal_phase(
            run_id=run_id,
            expected_revision=run["revision"],
            lease_token=token,
            observed_snapshot_hash=observed_snapshot_hash,
            proposal_body=proposal_body,
            redaction_status=redaction_status,
            finding_ids=proposal_finding_ids,
            input_artifact_refs=source_refs,
            now=now,
        )
        return {
            **applied,
            "recommended_proposal_count": sum(
                proposal["assessment"]["recommendation"] == "recommend"
                for proposal in proposal_result["proposals"]
            ),
        }
    finally:
        if owned_lease:
            store.release_lease(run_id, token, now=now)
