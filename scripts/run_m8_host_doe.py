"""Run a bounded, disposable M8 host-coordinator acceptance experiment.

This script creates an isolated fixture and an isolated SQLite ledger under a
fresh temporary root. It does not read or modify a user project, apply a patch,
or preserve the temporary ledger after the result summary has been checked.

One failed DOE attempt is retried once in a new temporary root. Failure
evidence is intentionally much smaller than the temporary ledger: it contains
only a redacted classification receipt and expires after 24 hours. This retry
policy belongs to the isolated DOE only; it does not change production
``host-review`` blocking or explicit-resume behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMP_REPORT_ROOT = Path("/private/tmp").resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nlp_refinement.host_executor import AppServerHostExecutor, HostExecutor
from nlp_refinement.doe_receipt_retention import (
    DEFAULT_FAILURE_EVIDENCE_ROOT,
    FAILURE_EVIDENCE_RETENTION_SECONDS,
    prepare_failure_evidence_root as _prepare_evidence_root,
    purge_expired_failure_receipts as _purge_expired_failure_evidence,
)
from nlp_refinement.host_execution_request import HostExecutionRequest
from nlp_refinement.m8_review_coordinator import M8ReviewCoordinator, HostReviewResult
from nlp_refinement.host_diagnostics import (
    PROPOSAL_INGESTION_ERROR_CODES,
    PROPOSAL_LOCAL_VALIDATION_ERROR_CODES,
    ProposalValidationCapture,
    ProposalValidationDiagnosticCoordinatorMixin,
    ReviewQualityCapture,
    ReviewerQualityDiagnosticCoordinatorMixin,
)
from nlp_refinement.m8_doe_fixtures import fixture_for
from nlp_refinement.state_store import (
    ARTIFACT_CATEGORIES,
    FINDING_GATES,
    FINDING_SEVERITIES,
    HOST_BOUNDARY_CATEGORIES,
    StateStore,
)


DOE_KINDS = {"protocol", "quality"}
_REVIEWER_QUALITY_RESULTS = frozenset({
    "accepted",
    "host-failed-before-local-classification",
    "invalid-json",
    "non-object-json",
    "rejected",
})
_REVIEWER_QUALITY_VALIDATOR_CODES = frozenset({
    "unexpected-or-missing-fields",
    "unsupported-schema-or-phase",
    "findings-not-array",
    "finding-unexpected-or-missing-fields",
    "reviewer-id-invalid",
    "summary-invalid",
    "finding-fingerprint-invalid",
    "finding-severity-or-gate-invalid",
    "duplicate-fingerprint",
    "review-input-rejected",
})
_REVIEWER_QUALITY_CATEGORIES = tuple(sorted(ARTIFACT_CATEGORIES | HOST_BOUNDARY_CATEGORIES))
_MAX_REVIEWER_QUALITY_FINDINGS = 1_000
_HOST_TIMING_STAGES = frozenset({
    "initialize",
    "thread_start",
    "turn_start",
    "awaiting_first_agent_message",
    "awaiting_terminal",
    "transport_read",
})
_HOST_TIMING_KEYS = (
    "failure_stage",
    "deadline_ms",
    "initialized_ms",
    "role_started_ms",
    "thread_started_ms",
    "turn_started_ms",
    "first_agent_message_ms",
    "terminal_ms",
)
_MAX_HOST_TIMING_MS = 3_600_000


class _QualityEvidenceCoordinator(
    ReviewerQualityDiagnosticCoordinatorMixin,
    ProposalValidationDiagnosticCoordinatorMixin,
    M8ReviewCoordinator,
):
    """DOE-only Coordinator that exposes a fixed-label reviewer summary on failure."""

    def __init__(
        self,
        store: StateStore,
        reviewer_capture: ReviewQualityCapture,
        proposal_capture: ProposalValidationCapture,
        executor_factory: Callable[[], HostExecutor] = AppServerHostExecutor,
    ) -> None:
        self._review_quality_capture = reviewer_capture
        self._proposal_validation_capture = proposal_capture
        super().__init__(store, executor_factory=executor_factory)


def _production_coordinator(store: StateStore) -> M8ReviewCoordinator:
    return M8ReviewCoordinator(store, executor_factory=AppServerHostExecutor)


def _snapshot(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ready_run(store: StateStore, snapshot: str) -> str:
    run_id = "run_m8hostdoe0001"
    store.create_run(
        {
            "schema_version": "4.0",
            "run_id": run_id,
            "project_key": hashlib.sha256(b"nlp-refinement/m8-doe").hexdigest(),
            "target_snapshot_hash": snapshot,
            "current_snapshot_hash": snapshot,
            "authorization_mode": "review-only",
            "state": "DRAFT",
            "outcome": "IN_PROGRESS",
            "revision": 0,
            "repair_cycle_count": 0,
            "max_repair_cycles": 3,
            "open_high_risk_assumptions": 0,
            "retention_policy_version": "4.0",
        },
        {"redacted_brief": "Run the isolated M8 host-dispatch DOE."},
        now=1,
    )
    lease = store.acquire_lease(run_id, "m8-doe-setup", now=1)
    try:
        store.transition(run_id, 0, lease["token"], "READY", "IN_PROGRESS", snapshot, now=1)
    finally:
        store.release_lease(run_id, lease["token"], now=1)
    return run_id


def _assert_quality_link(
    resolved_findings: list[dict[str, Any]],
    new_finding_ids: list[str],
    proposal_artifact: dict[str, Any],
) -> None:
    """Require a proposal to bind to this review's StateStore-resolved safety P0."""
    current_ids = set(new_finding_ids)
    p0_ids = {
        finding.get("finding_id")
        for finding in resolved_findings
        if isinstance(finding, dict)
        and finding.get("finding_id") in current_ids
        and finding.get("origin_severity") == "P0"
        and finding.get("effective_gate") == "blocking"
        and finding.get("category") == "safety_and_governance"
        and finding.get("subject_kind") == "nl_artifact"
        and isinstance(finding.get("finding_id"), str)
    }
    if not p0_ids:
        raise AssertionError("quality DOE did not identify the fixture's P0 safety finding")
    proposal_result = proposal_artifact.get("proposal_result")
    proposals = proposal_result.get("proposals") if isinstance(proposal_result, dict) else None
    if not isinstance(proposals, list) or not any(
        isinstance(proposal, dict) and proposal.get("finding_id") in p0_ids
        for proposal in proposals
    ):
        raise AssertionError("quality DOE did not create a proposal linked to the fixture's P0 safety finding")


def _quality_failure_code(error: Exception) -> str | None:
    """Translate only this script's controlled quality assertions into safe codes."""
    if not isinstance(error, AssertionError):
        return None
    message = str(error)
    if message == "quality DOE did not identify the fixture's P0 safety finding":
        return "p0-not-detected"
    if message == "quality DOE did not create a proposal linked to the fixture's P0 safety finding":
        return "p0-proposal-not-linked"
    return "quality-gate-assertion-unclassified"


def _assert_success(
    store: StateStore, run_id: str, artifact: Path, result: HostReviewResult, fixture: str, doe_kind: str,
) -> dict[str, Any]:
    if artifact.read_text(encoding="utf-8") != fixture:
        raise AssertionError("fixture content changed")
    artifacts = store.list_artifacts(run_id)
    protocol_redaction = doe_kind == "protocol"
    expected_kinds = (
        ["integrity", "integrity", "review", "integrity", "review"]
        if protocol_redaction
        else ["integrity", "review", "integrity", "review"]
    )
    if [entry["kind"] for entry in artifacts] != expected_kinds:
        raise AssertionError("unexpected ledger artifact sequence")
    receipt_indexes = (1, 3) if protocol_redaction else (0, 2)
    receipts = [json.loads(artifacts[index]["body"]) for index in receipt_indexes]
    if protocol_redaction:
        redaction_receipt = json.loads(artifacts[0]["body"])
        if redaction_receipt != {
            "artifact_role": "model-input-redaction/v1",
            "detector_version": "1",
            "model_input_handling": "coordinator-redacted",
            "replacement_counts": {"api-key": 1},
        }:
            raise AssertionError("protocol DOE did not retain the expected content-free redaction receipt")
        if receipts[0].get("model_input_handling") != "coordinator-redacted":
            raise AssertionError("reviewer receipt did not record coordinator redaction")
    _assert_host_observation_receipts(receipts)
    if doe_kind == "quality":
        proposal_artifact = json.loads(artifacts[3]["body"])
        _assert_quality_link(
            store.list_findings(run_id),
            result.new_finding_ids,
            proposal_artifact,
        )
    return {
        "status": "passed",
        "doe_kind": doe_kind,
        "run_id": run_id,
        "review_receipt_artifact_id": result.review_receipt_artifact_id,
        "review_artifact_id": result.review_artifact_id,
        "proposal_receipt_artifact_id": result.proposal_receipt_artifact_id,
        "proposal_artifact_id": result.proposal_artifact_id,
        "reviewer_thread_id": receipts[0]["thread_id"],
        "proposal_thread_id": receipts[1]["thread_id"],
        "host_attempts": [
            {key: receipt[key] for key in ("role", "thread_id", "turn_id", "turn_status", "host_version", "model", "timeout_seconds")}
            for receipt in receipts
        ],
        "finding_ids": result.new_finding_ids,
        "recommended_proposal_count": result.recommended_proposal_count,
        "model_input_handling": receipts[0].get("model_input_handling"),
        "fixture_unchanged": True,
        "target_writes": [],
        "ledger_artifact_kinds": [entry["kind"] for entry in artifacts],
    }


def _assert_host_observation_receipts(receipts: list[dict[str, Any]]) -> None:
    """Check the content-free v2 receipt contract used by a successful DOE."""
    if [receipt["role"] for receipt in receipts] != ["reviewer", "proposal"]:
        raise AssertionError("host roles were not recorded in order")
    if any(receipt.get("artifact_role") != "host-observation/v2" for receipt in receipts):
        raise AssertionError("host receipt did not use the content-free v2 contract")
    if any(any(key in receipt for key in ("raw_output", "raw_output_sha256", "events")) for receipt in receipts):
        raise AssertionError("host receipt persisted prohibited model-output fields")
    if any(not receipt["thread_id"] for receipt in receipts):
        raise AssertionError("host receipt omitted a thread ID")
    if any(receipt["requested_sandbox"] != "read-only" for receipt in receipts):
        raise AssertionError("host receipt did not retain the requested read-only boundary")
    if any(receipt["turn_status"] != "completed" for receipt in receipts):
        raise AssertionError("host receipt omitted a completed turn status")
    if any(not isinstance(receipt["host_version"], str) or not isinstance(receipt["model"], str) for receipt in receipts):
        raise AssertionError("host receipt omitted host identity fields")
    if any(not isinstance(receipt["timeout_seconds"], (int, float)) or receipt["timeout_seconds"] <= 0 for receipt in receipts):
        raise AssertionError("host receipt omitted a positive timeout")


def _validated_report_path(candidate: Path) -> Path:
    report_file = candidate.resolve()
    try:
        report_file.relative_to(TEMP_REPORT_ROOT)
    except ValueError as error:
        raise ValueError("DOE report file must be below /private/tmp") from error
    if report_file.exists():
        raise ValueError("refusing to overwrite an existing DOE report file")
    if not report_file.parent.is_dir():
        raise ValueError("DOE report parent must already exist")
    return report_file


def _write_new_private_report(path: Path, encoded: str) -> None:
    """Write a DOE summary once, without a check-then-write overwrite window."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(encoded + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _artifact_diagnostics(store: StateStore, run_id: str) -> list[dict[str, Any]]:
    """Return only classification-level data suitable for durable DOE evidence."""
    diagnostics: list[dict[str, Any]] = []
    for artifact in store.list_artifacts(run_id):
        body = json.loads(artifact["body"])
        if body.get("artifact_role") == "host-observation/v2":
            diagnostics.append(
                {
                    "artifact_role": body["artifact_role"],
                    "role": body["role"],
                    "thread_id": body["thread_id"],
                    "turn_id": body["turn_id"],
                    "turn_status": body["turn_status"],
                    "host_version": body["host_version"],
                    "model": body["model"],
                    "timeout_seconds": body["timeout_seconds"],
                }
            )
        elif body.get("artifact_role") == "host-failure/v1":
            diagnostics.append(
                {
                    "artifact_role": body["artifact_role"],
                    "phase": body["phase"],
                    "error_class": body["error_class"],
                    "host_attempt": body.get("host_attempt"),
                }
            )
    return diagnostics


def _failure_summary(
    store: StateStore,
    run_id: str,
    artifact: Path,
    fixture: str,
    doe_kind: str,
    error: Exception,
    reviewer_quality: list[dict[str, object]] | None = None,
    proposal_validation: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    """Build a redacted public result; never export exception text or host output."""
    run = store.get_run(run_id)
    summary = {
        "status": "failed",
        "error_class": type(error).__name__,
        "run_state": run["state"],
        "run_outcome": run["outcome"],
        "artifact_kinds": [entry["kind"] for entry in store.list_artifacts(run_id)],
        "host_diagnostics": _artifact_diagnostics(store, run_id),
        "fixture_unchanged": artifact.read_text(encoding="utf-8") == fixture,
        "target_writes": [],
        "doe_kind": doe_kind,
    }
    quality_failure_code = _quality_failure_code(error) if doe_kind == "quality" else None
    if quality_failure_code is not None:
        summary["quality_failure_code"] = quality_failure_code
    if doe_kind == "quality" and reviewer_quality is not None:
        summary["reviewer_quality"] = reviewer_quality
    if doe_kind == "quality" and proposal_validation is not None:
        summary["proposal_validation"] = proposal_validation
    return summary


def _redact_failure_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Whitelist public failure fields even if a future attempt runner adds more."""
    diagnostics: list[dict[str, Any]] = []
    for entry in summary.get("host_diagnostics", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("artifact_role") == "host-failure/v1":
            diagnostics.append(
                {
                    "artifact_role": "host-failure/v1",
                    "phase": entry.get("phase"),
                    "error_class": entry.get("error_class"),
                    "host_attempt": _redact_host_attempt(entry.get("host_attempt")),
                }
            )
        elif entry.get("artifact_role") == "host-observation/v2":
            diagnostics.append(
                {
                    "artifact_role": "host-observation/v2",
                    "role": entry.get("role"),
                    "thread_id": entry.get("thread_id"),
                    "turn_id": entry.get("turn_id"),
                    "turn_status": entry.get("turn_status"),
                    "host_version": entry.get("host_version"),
                    "model": entry.get("model"),
                    "timeout_seconds": entry.get("timeout_seconds"),
                }
            )
    artifact_kinds = summary.get("artifact_kinds")
    redacted = {
        "status": "failed",
        "error_class": summary.get("error_class"),
        "run_state": summary.get("run_state"),
        "run_outcome": summary.get("run_outcome"),
        "artifact_kinds": list(artifact_kinds) if isinstance(artifact_kinds, list) else [],
        "host_diagnostics": diagnostics,
        "fixture_unchanged": summary.get("fixture_unchanged") is True,
        "target_writes": [],
        "doe_kind": summary.get("doe_kind"),
    }
    quality_failure_code = summary.get("quality_failure_code")
    if quality_failure_code in {
        "p0-not-detected",
        "p0-proposal-not-linked",
        "quality-gate-assertion-unclassified",
    }:
        redacted["quality_failure_code"] = quality_failure_code
    reviewer_quality = _redact_reviewer_quality(summary.get("reviewer_quality"))
    if reviewer_quality:
        redacted["reviewer_quality"] = reviewer_quality
    proposal_validation = _redact_proposal_validation(summary.get("proposal_validation"))
    if proposal_validation:
        redacted["proposal_validation"] = proposal_validation
    return redacted


def _redact_proposal_validation(value: object) -> list[dict[str, object]]:
    """Keep only fixed codes from the same proposal attempt; never preserve proposal text."""
    if not isinstance(value, list) or not 1 <= len(value) <= 2:
        return []
    redacted: list[dict[str, object]] = []
    for signal in value:
        if not isinstance(signal, dict):
            return []
        stage, result = signal.get("stage"), signal.get("result")
        if stage == "local-validation" and result in {"accepted", "invalid-json", "non-object-json"}:
            redacted.append({"stage": stage, "result": result})
            continue
        if stage == "local-validation" and result == "rejected" and signal.get("error_code") in PROPOSAL_LOCAL_VALIDATION_ERROR_CODES:
            redacted.append({"stage": stage, "result": result, "error_code": signal["error_code"]})
            continue
        if stage == "ledger-ingestion" and result == "rejected" and signal.get("error_code") in PROPOSAL_INGESTION_ERROR_CODES:
            redacted.append({"stage": stage, "result": result, "error_code": signal["error_code"]})
            continue
        return []
    return redacted


def _redact_reviewer_quality(value: object) -> list[dict[str, object]]:
    """Retain only the fixed-label reviewer summary used to explain quality DOE failures."""
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return []
    signal = value[0]
    if signal.get("stage") != "review-quality" or signal.get("result") not in _REVIEWER_QUALITY_RESULTS:
        return []
    result = signal["result"]
    if result != "accepted":
        if result == "rejected" and signal.get("validator_error_code") in _REVIEWER_QUALITY_VALIDATOR_CODES:
            return [{"stage": "review-quality", "result": "rejected", "validator_error_code": signal["validator_error_code"]}]
        return [{"stage": "review-quality", "result": result}]
    finding_count = signal.get("finding_count")
    if not isinstance(finding_count, int) or isinstance(finding_count, bool) or not 0 <= finding_count <= _MAX_REVIEWER_QUALITY_FINDINGS:
        return []
    severity_counts = _fixed_count_map(signal.get("severity_counts"), tuple(sorted(FINDING_SEVERITIES)))
    gate_counts = _fixed_count_map(signal.get("gate_counts"), tuple(sorted(FINDING_GATES)))
    category_counts = _fixed_count_map(signal.get("category_counts"), _REVIEWER_QUALITY_CATEGORIES)
    has_safety_p0 = signal.get("has_safety_p0")
    if severity_counts is None or gate_counts is None or category_counts is None or not isinstance(has_safety_p0, bool):
        return []
    return [{
        "stage": "review-quality",
        "result": "accepted",
        "finding_count": finding_count,
        "severity_counts": severity_counts,
        "gate_counts": gate_counts,
        "category_counts": category_counts,
        "has_safety_p0": has_safety_p0,
    }]


def _fixed_count_map(value: object, keys: tuple[str, ...]) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    counts: dict[str, int] = {}
    for key in keys:
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= _MAX_REVIEWER_QUALITY_FINDINGS:
            return None
        counts[key] = item
    return counts


def _redact_host_attempt(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    redacted = {
        key: value.get(key)
        for key in ("role", "thread_id", "turn_id", "turn_status", "error_code", "event_count", "event_methods", "host_version", "model", "timeout_seconds", "effective_sandbox")
    }
    timing = _redact_host_timing(value.get("timing"))
    if timing is not None:
        redacted["timing"] = timing
    return redacted


def _redact_host_timing(value: object) -> dict[str, object] | None:
    """Pass only the fixed, local, content-free timing contract into a DOE receipt."""
    if not isinstance(value, dict) or not set(_HOST_TIMING_KEYS).issubset(value):
        return None
    stage = value.get("failure_stage")
    deadline = value.get("deadline_ms")
    if stage not in _HOST_TIMING_STAGES or not _valid_timing_ms(deadline):
        return None
    redacted: dict[str, object] = {"failure_stage": stage, "deadline_ms": deadline}
    for key in _HOST_TIMING_KEYS[2:]:
        item = value.get(key)
        if item is not None and not _valid_timing_ms(item):
            return None
        redacted[key] = item
    return redacted


def _valid_timing_ms(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_HOST_TIMING_MS


def _run_attempt(coordinator_factory: Callable[[StateStore], M8ReviewCoordinator], doe_kind: str) -> tuple[int, dict[str, Any]]:
    """Run one self-contained attempt. Its fixture and ledger always disappear."""
    with tempfile.TemporaryDirectory(prefix="nlp-refinement-m8-host-doe-") as directory:
        root = Path(directory)
        target = root / "fixture"
        target.mkdir(mode=0o700)
        fixture = fixture_for(doe_kind)
        artifact = target / f"{doe_kind}-fixture.md"
        artifact.write_text(fixture, encoding="utf-8")
        store = StateStore(root / "state" / "state.db")
        store.initialize()
        snapshot = _snapshot(fixture)
        run_id = _ready_run(store, snapshot)
        reviewer_capture = ReviewQualityCapture() if doe_kind == "quality" and coordinator_factory is _production_coordinator else None
        proposal_capture = ProposalValidationCapture() if reviewer_capture is not None else None
        coordinator = (
            _QualityEvidenceCoordinator(store, reviewer_capture, proposal_capture)
            if reviewer_capture is not None and proposal_capture is not None
            else coordinator_factory(store)
        )
        try:
            result = coordinator.run(request=HostExecutionRequest(run_id, artifact, snapshot))
            return 0, _assert_success(store, run_id, artifact, result, fixture, doe_kind)
        except Exception as error:
            return 1, _failure_summary(
                store,
                run_id,
                artifact,
                fixture,
                doe_kind,
                error,
                reviewer_capture.snapshot() if reviewer_capture is not None else None,
                proposal_capture.snapshot() if proposal_capture is not None else None,
            )


def _failure_receipt(attempt: int, summary: dict[str, Any], recorded_at: int) -> dict[str, Any]:
    """Make the only durable failure record, with no model text or error message."""
    return {
        "schema_version": "doe-failure-receipt/v1",
        "artifact_role": "doe-failure-receipt/v1",
        "recorded_at": recorded_at,
        "expires_at": recorded_at + FAILURE_EVIDENCE_RETENTION_SECONDS,
        "attempt": attempt,
        "summary": {
            "error_class": summary["error_class"],
            "run_state": summary["run_state"],
            "run_outcome": summary["run_outcome"],
            "artifact_kinds": list(summary["artifact_kinds"]),
            "fixture_unchanged": summary["fixture_unchanged"],
            "target_writes": [],
            **({"quality_failure_code": summary["quality_failure_code"]} if "quality_failure_code" in summary else {}),
            **({"reviewer_quality": summary["reviewer_quality"]} if "reviewer_quality" in summary else {}),
            **({"proposal_validation": summary["proposal_validation"]} if "proposal_validation" in summary else {}),
        },
        "failure_receipts": [
            entry for entry in summary["host_diagnostics"] if entry["artifact_role"] == "host-failure/v1"
        ],
    }


def _archive_failure_receipt(root: Path, attempt: int, summary: dict[str, Any], recorded_at: int) -> str:
    receipt = _failure_receipt(attempt, summary, recorded_at)
    for _ in range(8):
        name = f"failure-{recorded_at:010d}-{secrets.token_hex(8)}.json"
        path = root / name
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return name
    raise RuntimeError("could not allocate a unique DOE failure receipt name")


def run(
    *,
    coordinator_factory: Callable[[StateStore], M8ReviewCoordinator] = _production_coordinator,
    evidence_root: Path | None = None,
    now_fn: Callable[[], int] | None = None,
    attempt_runner: Callable[[Callable[[StateStore], M8ReviewCoordinator]], tuple[int, dict[str, Any]]] | None = None,
    doe_kind: str = "protocol",
) -> tuple[int, dict[str, Any]]:
    """Run at most two isolated attempts, retrying only after an initial failure."""
    now = now_fn or (lambda: int(time.time()))
    if doe_kind not in DOE_KINDS:
        raise ValueError("unsupported DOE kind")
    runner = attempt_runner or (lambda factory: _run_attempt(factory, doe_kind))
    root = _prepare_evidence_root(evidence_root or DEFAULT_FAILURE_EVIDENCE_ROOT)
    _purge_expired_failure_evidence(root, now())
    code, first = runner(coordinator_factory)
    if code == 0:
        first["attempt_count"] = 1
        return 0, first
    first = _redact_failure_summary(first)

    receipts: list[str] = []
    archive_error_class: str | None = None
    try:
        receipts.append(_archive_failure_receipt(root, 1, first, now()))
    except (OSError, PermissionError, ValueError, RuntimeError) as error:
        archive_error_class = type(error).__name__

    retry_code, second = runner(coordinator_factory)
    if retry_code != 0:
        second = _redact_failure_summary(second)
    if retry_code != 0:
        try:
            receipts.append(_archive_failure_receipt(root, 2, second, now()))
        except (OSError, PermissionError, ValueError, RuntimeError) as error:
            archive_error_class = archive_error_class or type(error).__name__

    report = second
    report["attempt_count"] = 2
    report["retry_performed"] = True
    report["failure_evidence"] = {
        "receipt_files": receipts,
        "retention_seconds": FAILURE_EVIDENCE_RETENTION_SECONDS,
        "purge_trigger": "next-doe-invocation",
    }
    if archive_error_class is not None:
        report["failure_evidence"]["archive_error_class"] = archive_error_class
    return retry_code, report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an isolated, disposable M8 app-server model inference DOE."
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        help="write the redacted DOE summary to a new file outside the disposable fixture root",
    )
    parser.add_argument(
        "--failure-evidence-root",
        type=Path,
        default=DEFAULT_FAILURE_EVIDENCE_ROOT,
        help="owner-only root for 24-hour redacted DOE failure receipts",
    )
    parser.add_argument(
        "--doe-kind",
        choices=sorted(DOE_KINDS),
        default="protocol",
        help="protocol validates dispatch/ledger; quality also checks the expected P0 finding",
    )
    args = parser.parse_args()
    report_file = _validated_report_path(args.report_file) if args.report_file is not None else None
    code, report = run(evidence_root=args.failure_evidence_root, doe_kind=args.doe_kind)
    report["temporary_root_deleted_after_exit"] = True
    encoded = json.dumps(report, sort_keys=True, ensure_ascii=False)
    if report_file is not None:
        _write_new_private_report(report_file, encoded)
    print(encoded)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
