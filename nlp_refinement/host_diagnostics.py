"""Diagnostic-only, whitelisted host-failure observations.

This module is intentionally not used by normal ``host-review``. It supports
separate, opt-in DOE capture levels that distinguish a host-reported failure
from a transport failure. The default level retains no raw JSONL, model output,
prompts, or free-form error text; the explicitly authorized raw-fragment level
has a stricter, bounded, terminal-only contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Protocol

from nlp_refinement.host_client import HostResult
from nlp_refinement.patch_proposal_adapter import PatchProposalInputError, validate_patch_proposal
from nlp_refinement.review_adapter import ReviewInputError, validate_review_result
from nlp_refinement.state_store import (
    ARTIFACT_CATEGORIES,
    FINDING_GATES,
    FINDING_SEVERITIES,
    HOST_BOUNDARY_CATEGORIES,
)


_SAFE_DIAGNOSTIC_LABEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
_ALLOWED_FIELD_NAMES = frozenset({"code", "type", "kind", "status", "category"})
_MAX_DIAGNOSTIC_SIGNALS = 8
_RAW_DIAGNOSTIC_METHODS = frozenset({"error", "turn/completed"})
_MAX_RAW_DIAGNOSTIC_EVENTS = 2
_MAX_RAW_DIAGNOSTIC_BYTES = 4 * 1024
_MAX_RAW_DIAGNOSTIC_STRING = 1024
_SENSITIVE_FIELD_NAME = re.compile(r"(?:authorization|cookie|input|password|prompt|secret|token|api[_-]?key|content)", re.IGNORECASE)
_LIKELY_SECRET_VALUE = re.compile(r"(?:\b(?:bearer|sk)-[A-Za-z0-9._-]{12,}|\b(?:api[_-]?key|token|password)\s*[:=]\s*\S+)", re.IGNORECASE)
PROPOSAL_LOCAL_VALIDATION_ERROR_CODES = frozenset({
    "result-unexpected-or-missing-fields",
    "unsupported-schema-or-phase",
    "required-field-invalid",
    "base-snapshot-hash-invalid",
    "no-target-write-attestation-missing",
    "proposals-invalid-or-empty",
    "proposal-count-exceeded",
    "proposal-item-unexpected-or-missing-fields",
    "proposal-item-required-field-invalid",
    "authorization-basis-invalid",
    "proposal-list-field-invalid",
    "assessment-unexpected-or-missing-fields",
    "assessment-invalid",
    "duplicate-finding-id",
    "target-reference-invalid",
    "unified-diff-invalid",
    "proposal-validation-rejected",
})
PROPOSAL_INGESTION_ERROR_CODES = frozenset({
    "references-unknown-finding",
    "requires-open-finding",
    "snapshot-mismatch",
    "missing-linked-invariant",
    "causal-evidence-mismatch",
    "assessment-evidence-mismatch",
    "open-high-risk-assumptions",
    "patch-proposal-input-rejected",
})


class FailureCapture(Protocol):
    def record_failure(self, role: str, events: list[dict[str, Any]]) -> None: ...

    def snapshot(self) -> list[dict[str, object]]: ...


@dataclass
class DiagnosticCapture:
    """Ephemeral, whitelisted failure facts for one diagnostic DOE process."""

    _signals: list[dict[str, object]] = field(default_factory=list)

    def record_failure(self, role: str, events: list[dict[str, Any]]) -> None:
        for event in events:
            if len(self._signals) >= _MAX_DIAGNOSTIC_SIGNALS:
                return
            method = _safe_label(event.get("method"))
            params = event.get("params")
            if method is None or not isinstance(params, dict) or not _is_failure_event(method, params):
                continue
            self._signals.append(
                {
                    "role": role,
                    "method": method,
                    "fields": _whitelisted_fields(params),
                }
            )

    def snapshot(self) -> list[dict[str, object]]:
        """Return copies so callers cannot mutate the in-memory evidence."""
        return [
            {"role": item["role"], "method": item["method"], "fields": dict(item["fields"])}
            for item in self._signals
        ]


@dataclass
class RawDiagnosticCapture:
    """One opt-in, ephemeral, redacted fragment capture for a known failed role."""

    _fragments: list[dict[str, object]] = field(default_factory=list)

    def record_failure(self, role: str, events: list[dict[str, Any]]) -> None:
        if role != "proposal":
            return
        for event in events:
            if len(self._fragments) >= _MAX_RAW_DIAGNOSTIC_EVENTS:
                return
            method = _safe_label(event.get("method"))
            if method not in _RAW_DIAGNOSTIC_METHODS:
                continue
            fragment = {"role": role, "method": method, "params": _bounded_redaction(event.get("params"))}
            if len(json.dumps(fragment, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_RAW_DIAGNOSTIC_BYTES:
                fragment = {"role": role, "method": method, "params": {"capture_truncated": True}}
            self._fragments.append(fragment)

    def snapshot(self) -> list[dict[str, object]]:
        return json.loads(json.dumps(self._fragments, ensure_ascii=False))


@dataclass
class ProposalValidationCapture:
    """Ephemeral local validation evidence; raw proposal output never leaves memory."""

    _signals: list[dict[str, object]] = field(default_factory=list)

    def record_failure(self, role: str, events: list[dict[str, Any]]) -> None:
        if role == "proposal" and not self._signals:
            self._signals.append({"stage": "host", "result": "failed-before-local-validation"})

    def record_proposal_result(self, raw_output: str) -> None:
        if self._signals:
            return
        self._signals.append(classify_proposal_result(raw_output))

    def record_coordinator_failure(self, error: Exception) -> None:
        """Classify trusted local validation/ingestion failures without model values."""
        if not self._signals or self._signals[-1] != {"stage": "local-validation", "result": "accepted"}:
            return
        self._signals.append(classify_proposal_coordinator_failure(error))

    def snapshot(self) -> list[dict[str, object]]:
        return [dict(signal) for signal in self._signals]


@dataclass
class ReviewQualityCapture:
    """Ephemeral reviewer-classification summary; it never exports finding text."""

    _signals: list[dict[str, object]] = field(default_factory=list)

    def record_failure(self, role: str, events: list[dict[str, Any]]) -> None:
        if role == "reviewer" and not self._signals:
            self._signals.append({"stage": "review-quality", "result": "host-failed-before-local-classification"})

    def record_review_result(self, raw_output: str) -> None:
        if self._signals:
            return
        try:
            value = json.loads(raw_output)
        except json.JSONDecodeError:
            self._signals.append({"stage": "review-quality", "result": "invalid-json"})
            return
        if not isinstance(value, dict):
            self._signals.append({"stage": "review-quality", "result": "non-object-json"})
            return
        try:
            validate_review_result(value)
        except ReviewInputError as error:
            self._signals.append(
                {
                    "stage": "review-quality",
                    "result": "rejected",
                    "validator_error_code": _review_validation_error_code(str(error)),
                }
            )
            return

        categories = tuple(sorted(ARTIFACT_CATEGORIES | HOST_BOUNDARY_CATEGORIES))
        severity_counts = {severity: 0 for severity in sorted(FINDING_SEVERITIES)}
        gate_counts = {gate: 0 for gate in sorted(FINDING_GATES)}
        category_counts = {category: 0 for category in categories}
        has_safety_p0 = False
        for finding in value["findings"]:
            severity_counts[finding["origin_severity"]] += 1
            gate_counts[finding["effective_gate"]] += 1
            category_counts[finding["category"]] += 1
            has_safety_p0 = has_safety_p0 or (
                finding["origin_severity"] == "P0"
                and finding["effective_gate"] == "blocking"
                and finding["subject_kind"] == "nl_artifact"
                and finding["category"] == "safety_and_governance"
            )
        self._signals.append(
            {
                "stage": "review-quality",
                "result": "accepted",
                "finding_count": len(value["findings"]),
                "severity_counts": severity_counts,
                "gate_counts": gate_counts,
                "category_counts": category_counts,
                "has_safety_p0": has_safety_p0,
            }
        )

    def snapshot(self) -> list[dict[str, object]]:
        return json.loads(json.dumps(self._signals, ensure_ascii=False))


class ProposalValidationDiagnosticCoordinatorMixin:
    """Diagnostic-only observation point; it leaves the normal record path intact."""

    _proposal_validation_capture: ProposalValidationCapture

    def _record(
        self,
        run_id: str,
        result: HostResult,
        refs: list[str],
        lease_token: str | None = None,
        *,
        model_input_handling: str | None = None,
    ) -> str:
        if result.role == "proposal":
            self._proposal_validation_capture.record_proposal_result(result.raw_output)
        return super()._record(  # type: ignore[misc]
            run_id,
            result,
            refs,
            lease_token,
            model_input_handling=model_input_handling,
        )

    def run(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        try:
            return super().run(*args, **kwargs)  # type: ignore[misc]
        except Exception as error:
            self._proposal_validation_capture.record_coordinator_failure(error)
            raise


class ReviewerQualityDiagnosticCoordinatorMixin:
    """Diagnostic-only reviewer classification observation before normal ingestion."""

    _review_quality_capture: ReviewQualityCapture

    def _record(
        self,
        run_id: str,
        result: HostResult,
        refs: list[str],
        lease_token: str | None = None,
        *,
        model_input_handling: str | None = None,
    ) -> str:
        if result.role == "reviewer":
            self._review_quality_capture.record_review_result(result.raw_output)
        return super()._record(  # type: ignore[misc]
            run_id,
            result,
            refs,
            lease_token,
            model_input_handling=model_input_handling,
        )


def _is_failure_event(method: str, params: dict[str, Any]) -> bool:
    if method == "error":
        return True
    if method != "turn/completed":
        return False
    turn = params.get("turn")
    return isinstance(turn, dict) and turn.get("status") != "completed"


def _whitelisted_fields(value: dict[str, Any], prefix: str = "", depth: int = 0) -> dict[str, object]:
    """Collect only bounded labels from code-like fields; never collect text."""
    fields: dict[str, object] = {}
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if key in _ALLOWED_FIELD_NAMES:
            scalar = _safe_scalar(child)
            if scalar is not None:
                fields[path] = scalar
        if depth < 2 and isinstance(child, dict):
            fields.update(_whitelisted_fields(child, path, depth + 1))
    return fields


def _safe_label(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_DIAGNOSTIC_LABEL.fullmatch(value) else None


def _safe_scalar(value: object) -> str | int | bool | None:
    if isinstance(value, str):
        return value if _SAFE_DIAGNOSTIC_LABEL.fullmatch(value) else None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and -999_999 <= value <= 999_999:
        return value
    return None


def _bounded_redaction(value: object, key: str = "", depth: int = 0) -> object:
    """Retain a tiny error fragment while blocking known secret-bearing fields."""
    if _SENSITIVE_FIELD_NAME.search(key):
        return "[redacted]"
    if depth >= 5:
        return "[truncated-depth]"
    if isinstance(value, dict):
        output: dict[str, object] = {}
        for index, (child_key, child) in enumerate(value.items()):
            if index >= 24:
                output["_truncated_fields"] = True
                break
            safe_key = child_key if isinstance(child_key, str) and _SAFE_DIAGNOSTIC_LABEL.fullmatch(child_key) else "_unsafe_key"
            output[safe_key] = _bounded_redaction(child, safe_key, depth + 1)
        return output
    if isinstance(value, list):
        output = [_bounded_redaction(child, key, depth + 1) for child in value[:12]]
        if len(value) > 12:
            output.append("[truncated-items]")
        return output
    if isinstance(value, str):
        compact = _LIKELY_SECRET_VALUE.sub("[redacted-secret]", value)
        return compact if len(compact) <= _MAX_RAW_DIAGNOSTIC_STRING else compact[:_MAX_RAW_DIAGNOSTIC_STRING] + "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[unsupported-value]"


def _patch_proposal_error_code(message: str) -> str:
    """Map static adapter messages to stable codes; omit all model-originated values."""
    prefixes = {
        "patch proposal references unknown finding:": "references-unknown-finding",
        "patch proposal requires an open finding:": "requires-open-finding",
        "patch proposal base snapshot must equal observed snapshot": "snapshot-mismatch",
        "patch proposal target_invariants must include": "missing-linked-invariant",
        "patch proposal causal evidence must reference": "causal-evidence-mismatch",
        "proposal assessment evidence must reference": "assessment-evidence-mismatch",
        "cannot recommend a proposal while high-risk assumptions remain open": "open-high-risk-assumptions",
    }
    for prefix, code in prefixes.items():
        if message.startswith(prefix):
            return code
    return "patch-proposal-input-rejected"


def classify_proposal_result(raw_output: str) -> dict[str, object]:
    """Classify one raw proposal using fixed local codes without retaining its text."""
    try:
        value = json.loads(raw_output)
    except json.JSONDecodeError:
        return {"stage": "local-validation", "result": "invalid-json"}
    if not isinstance(value, dict):
        return {"stage": "local-validation", "result": "non-object-json"}
    try:
        validate_patch_proposal(value)
    except PatchProposalInputError as error:
        return {
            "stage": "local-validation",
            "result": "rejected",
            "error_code": _proposal_local_validation_error_code(str(error)),
        }
    return {"stage": "local-validation", "result": "accepted"}


def classify_proposal_coordinator_failure(error: Exception) -> dict[str, object]:
    """Classify only trusted local follow-up errors after accepted proposal syntax."""
    cause = error.__cause__
    if isinstance(cause, PatchProposalInputError):
        return {
            "stage": "ledger-ingestion",
            "result": "rejected",
            "error_code": _patch_proposal_error_code(str(cause)),
        }
    return {"stage": "coordinator", "result": "failed", "error_class": type(cause or error).__name__}


def _proposal_local_validation_error_code(message: str) -> str:
    prefixes = {
        "patch proposal has unexpected or missing fields": "result-unexpected-or-missing-fields",
        "patch proposal has an unsupported schema or phase": "unsupported-schema-or-phase",
        "patch proposal base_snapshot_hash must be lowercase SHA-256 hex": "base-snapshot-hash-invalid",
        "patch proposal must attest that no target write occurred": "no-target-write-attestation-missing",
        "patch proposal proposals must be a non-empty array": "proposals-invalid-or-empty",
        "patch proposal may contain at most 50 proposals": "proposal-count-exceeded",
        "patch proposal item has unexpected or missing fields": "proposal-item-unexpected-or-missing-fields",
        "patch proposal authorization_basis is invalid": "authorization-basis-invalid",
        "proposal assessment has unexpected or missing fields": "assessment-unexpected-or-missing-fields",
        "proposal assessment ": "assessment-invalid",
        "recommendation requires the minimum evidence-backed score thresholds": "assessment-invalid",
        "patch proposal may contain one item per finding": "duplicate-finding-id",
        "target_reference must be a relative path without traversal": "target-reference-invalid",
        "unified_diff ": "unified-diff-invalid",
    }
    for prefix, code in prefixes.items():
        if message.startswith(prefix):
            return code
    if message.startswith("patch proposal ") and message.endswith("must be a non-empty string"):
        return "required-field-invalid"
    if message.startswith("patch proposal ") and message.endswith("must be a non-empty string array"):
        return "proposal-list-field-invalid"
    return "proposal-validation-rejected"


def _review_validation_error_code(message: str) -> str:
    """Map local validator messages to fixed labels; never expose model values."""
    prefixes = {
        "review result has unexpected or missing fields": "unexpected-or-missing-fields",
        "review result has an unsupported schema or phase": "unsupported-schema-or-phase",
        "review result findings must be an array": "findings-not-array",
        "review finding has unexpected or missing fields": "finding-unexpected-or-missing-fields",
        "review result reviewer_id must be a non-empty string": "reviewer-id-invalid",
        "review result summary must be a non-empty string": "summary-invalid",
        "review finding fingerprint must be lowercase SHA-256 hex": "finding-fingerprint-invalid",
        "review finding has an unsupported severity or gate": "finding-severity-or-gate-invalid",
        "review result contains a duplicate fingerprint": "duplicate-fingerprint",
    }
    for prefix, code in prefixes.items():
        if message.startswith(prefix):
            return code
    return "review-input-rejected"
