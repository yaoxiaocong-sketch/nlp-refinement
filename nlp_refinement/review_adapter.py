"""M3 adapter for accepting an externally produced, structured review result."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

from nlp_refinement.state_store import (
    StateStore,
    validate_finding_assessment,
    validate_finding_classification,
)


FINGERPRINT = re.compile(r"[a-f0-9]{64}")
SEVERITIES = {"P0", "P1", "P2", "P3"}
GATES = {"blocking", "needs-user-decision", "recordable-risk"}
REQUIRED_RESULT = {"schema_version", "phase_name", "reviewer_id", "summary", "findings"}
REQUIRED_REVIEW_FINDING = {
    "fingerprint", "origin_severity", "effective_gate", "invariant_id", "last_probe",
    "statement", "evidence", "recommendation", "subject_kind", "evidence_plane", "category",
    "evidence_confidence", "impact_scope", "claim_status",
}


class ReviewInputError(ValueError):
    """Raised when external review data is not valid for the M3 protocol."""


def load_review_result(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReviewInputError(f"review result is not valid JSON: {error.msg}") from error
    if not isinstance(loaded, dict):
        raise ReviewInputError("review result must be a JSON object")
    validate_review_result(loaded)
    return loaded


def validate_review_result(result: dict[str, Any]) -> None:
    if set(result) != REQUIRED_RESULT:
        raise ReviewInputError("review result has unexpected or missing fields")
    if result["schema_version"] != "4.0" or result["phase_name"] != "review":
        raise ReviewInputError("review result has an unsupported schema or phase")
    for field in ("reviewer_id", "summary"):
        if not isinstance(result[field], str) or not result[field].strip():
            raise ReviewInputError(f"review result {field} must be a non-empty string")
    if not isinstance(result["findings"], list):
        raise ReviewInputError("review result findings must be an array")
    fingerprints: set[str] = set()
    for finding in result["findings"]:
        if not isinstance(finding, dict) or set(finding) != REQUIRED_REVIEW_FINDING:
            raise ReviewInputError("review finding has unexpected or missing fields")
        for field in (
            "fingerprint", "origin_severity", "effective_gate", "invariant_id", "last_probe",
            "statement", "evidence", "recommendation", "subject_kind", "evidence_plane", "category",
            "evidence_confidence", "impact_scope", "claim_status",
        ):
            if not isinstance(finding[field], str) or not finding[field].strip():
                raise ReviewInputError(f"review finding {field} must be a non-empty string")
        if FINGERPRINT.fullmatch(finding["fingerprint"]) is None:
            raise ReviewInputError("review finding fingerprint must be lowercase SHA-256 hex")
        if finding["origin_severity"] not in SEVERITIES or finding["effective_gate"] not in GATES:
            raise ReviewInputError("review finding has an unsupported severity or gate")
        try:
            validate_finding_classification(
                finding["subject_kind"], finding["evidence_plane"], finding["category"]
            )
            validate_finding_assessment(
                finding["origin_severity"],
                finding["effective_gate"],
                finding["evidence_plane"],
                finding["evidence_confidence"],
                finding["impact_scope"],
                finding["claim_status"],
            )
        except ValueError as error:
            raise ReviewInputError(str(error)) from error
        if finding["fingerprint"] in fingerprints:
            raise ReviewInputError("review result contains a duplicate fingerprint")
        fingerprints.add(finding["fingerprint"])


def _next_finding_id(findings: list[dict[str, Any]]) -> str:
    highest = max((int(finding["finding_id"].removeprefix("F-")) for finding in findings), default=0)
    return f"F-{highest + 1:03d}"


def _assert_compatible_duplicate(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    for field in ("origin_severity", "effective_gate", "invariant_id"):
        if existing[field] != incoming[field]:
            raise ReviewInputError(f"duplicate fingerprint conflicts with immutable finding fields: {field}")
    for field in (
        "subject_kind", "evidence_plane", "category",
        "evidence_confidence", "impact_scope", "claim_status",
    ):
        if existing[field] != incoming[field]:
            raise ReviewInputError(f"duplicate fingerprint conflicts with immutable classification: {field}")
    if existing["status"] != "open":
        raise ReviewInputError("M3 can deduplicate only an open finding")


def ingest_review(
    store: StateStore,
    run_id: str,
    review_result: dict[str, Any],
    observed_snapshot_hash: str,
    redaction_status: str,
    now: int,
    lease_token: str | None = None,
) -> dict[str, Any]:
    """Validate, append, and triage a review result without invoking a model."""
    validate_review_result(review_result)
    run = store.get_run(run_id)
    if run["authorization_mode"] != "review-only":
        raise ReviewInputError("M3 accepts only review-only runs")
    if run["state"] != "READY":
        raise ReviewInputError(f"M3 review requires READY state, found {run['state']}")

    owned_lease = lease_token is None
    token = lease_token or store.acquire_lease(run_id, f"m3-review-{secrets.token_hex(8)}", now=now)["token"]
    try:
        run = store.get_run(run_id)
        review_body = json.dumps(review_result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if observed_snapshot_hash != run["current_snapshot_hash"]:
            applied = store.apply_review_phase(
                run_id,
                expected_revision=run["revision"],
                lease_token=token,
                observed_snapshot_hash=observed_snapshot_hash,
                review_body=review_body,
                redaction_status=redaction_status,
                planned_findings=[],
                now=now,
            )
            return {**applied, "no_material_delta_candidates": []}
        existing_findings = store.list_findings(run_id)
        existing_by_fingerprint = {finding["fingerprint"]: finding for finding in existing_findings}
        for incoming in review_result["findings"]:
            existing = existing_by_fingerprint.get(incoming["fingerprint"])
            if existing is not None:
                _assert_compatible_duplicate(existing, incoming)

        planned_findings: list[dict[str, Any]] = []
        no_material_delta_candidates: list[str] = []
        for incoming in review_result["findings"]:
            existing = existing_by_fingerprint.get(incoming["fingerprint"])
            if existing is None:
                finding = {
                    "schema_version": "4.0",
                    "finding_id": _next_finding_id(existing_findings),
                    "run_id": run_id,
                    "fingerprint": incoming["fingerprint"],
                    "origin_severity": incoming["origin_severity"],
                    "effective_gate": incoming["effective_gate"],
                    "status": "open",
                    "invariant_id": incoming["invariant_id"],
                    "evidence_refs": [],
                    "repeat_count": 0,
                    "last_probe": incoming["last_probe"],
                    "accepted_risk_decision_ref": "none",
                    "subject_kind": incoming["subject_kind"],
                    "evidence_plane": incoming["evidence_plane"],
                    "category": incoming["category"],
                    "evidence_confidence": incoming["evidence_confidence"],
                    "impact_scope": incoming["impact_scope"],
                    "claim_status": incoming["claim_status"],
                }
                existing_findings.append(finding)
                existing_by_fingerprint[finding["fingerprint"]] = finding
                planned_findings.append({"action": "new", "finding": finding})
                continue

            same_probe = existing["last_probe"] == incoming["last_probe"]
            duplicate = dict(existing)
            duplicate["repeat_count"] = existing["repeat_count"] + 1
            duplicate["last_probe"] = incoming["last_probe"]
            existing_by_fingerprint[duplicate["fingerprint"]] = duplicate
            planned_findings.append({"action": "duplicate", "finding": duplicate})
            if same_probe and duplicate["repeat_count"] >= 2:
                no_material_delta_candidates.append(duplicate["finding_id"])

        applied = store.apply_review_phase(
            run_id,
            expected_revision=run["revision"],
            lease_token=token,
            observed_snapshot_hash=observed_snapshot_hash,
            review_body=review_body,
            redaction_status=redaction_status,
            planned_findings=planned_findings,
            now=now,
        )
        return {
            **applied,
            "no_material_delta_candidates": no_material_delta_candidates,
        }
    finally:
        if owned_lease:
            store.release_lease(run_id, token, now=now)
