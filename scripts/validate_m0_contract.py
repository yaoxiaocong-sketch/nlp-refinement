#!/usr/bin/env python3
"""Validate the deliberately small M0 protocol subset without third-party packages."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nlp_refinement.format_contracts import validate_format_contract_registry, validate_state_schema_parity

SCHEMA_DIR = ROOT / "nlp_refinement" / "resources" / "schemas" / "v4"
FIXTURE_DIR = ROOT / "fixtures" / "v4"
SUPPORTED_KEYWORDS = {
    "$schema",
    "title",
    "type",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "pattern",
    "minLength",
    "minimum",
    "maximum",
    "minItems",
}
STATIC_ONLY_CATEGORIES = {
    "format_and_encoding",
    "schema_contract",
    "layout_and_discovery",
    "reference_integrity",
}
ARTIFACT_CATEGORIES = STATIC_ONLY_CATEGORIES | {
    "semantic_workflow",
    "safety_and_governance",
    "verification_design",
}
HOST_BOUNDARY_CATEGORIES = {
    "load_and_discovery",
    "instruction_adherence",
    "state_behavior",
    "outcome_verification",
    "safety_behavior",
}
EVIDENCE_CONFIDENCE = {"E1-inferred", "E2-direct", "E3-corroborated"}
IMPACT_SCOPES = {"single-artifact", "cross-artifact", "core-workflow", "durable-state", "security-boundary"}
CLAIM_STATUSES = {"inferred", "direct-static", "observed-runtime", "corroborated"}


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
    }.get(expected, False)


def validate_instance(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    unsupported = set(schema) - SUPPORTED_KEYWORDS
    if unsupported:
        return [f"{path}: unsupported schema keywords {sorted(unsupported)}"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']!r}")
    if "type" in schema and not type_matches(value, schema["type"]):
        errors.append(f"{path}: expected {schema['type']}")
        return errors
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: string is shorter than minLength")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            errors.append(f"{path}: does not match pattern")
    if isinstance(value, int):
        if value < schema.get("minimum", value):
            errors.append(f"{path}: is below minimum")
        if value > schema.get("maximum", value):
            errors.append(f"{path}: is above maximum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: has fewer than minItems")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(validate_instance(item, schema["items"], f"{path}[{index}]"))
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing required key {key}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: unexpected key {key}")
        for key, child_schema in properties.items():
            if key in value:
                errors.extend(validate_instance(value[key], child_schema, f"{path}.{key}"))
    return errors


def validate_finding_classification(finding: dict[str, Any], path: str) -> list[str]:
    subject_kind = finding.get("subject_kind")
    evidence_plane = finding.get("evidence_plane")
    category = finding.get("category")
    if subject_kind == "nl_artifact":
        if category not in ARTIFACT_CATEGORIES:
            return [f"{path}: category is invalid for nl_artifact"]
        if category in STATIC_ONLY_CATEGORIES and evidence_plane != "static":
            return [f"{path}: static category requires static evidence"]
        if category not in STATIC_ONLY_CATEGORIES and evidence_plane not in {"static", "hybrid"}:
            return [f"{path}: semantic NL artifact category requires static or hybrid evidence"]
        return []
    if subject_kind == "nl_host_boundary":
        if category not in HOST_BOUNDARY_CATEGORIES:
            return [f"{path}: category is invalid for nl_host_boundary"]
        if evidence_plane not in {"runtime", "hybrid"}:
            return [f"{path}: nl_host_boundary category requires runtime or hybrid evidence"]
        return []
    return [f"{path}: subject_kind is invalid"]


def validate_finding_assessment(finding: dict[str, Any], path: str) -> list[str]:
    severity = finding.get("origin_severity")
    gate = finding.get("effective_gate")
    plane = finding.get("evidence_plane")
    confidence = finding.get("evidence_confidence")
    scope = finding.get("impact_scope")
    claim_status = finding.get("claim_status")
    if confidence not in EVIDENCE_CONFIDENCE:
        return [f"{path}: evidence_confidence is invalid"]
    if scope not in IMPACT_SCOPES:
        return [f"{path}: impact_scope is invalid"]
    if claim_status not in CLAIM_STATUSES:
        return [f"{path}: claim_status is invalid"]
    if confidence == "E1-inferred" and claim_status != "inferred":
        return [f"{path}: E1-inferred requires inferred claim_status"]
    if confidence == "E2-direct":
        if claim_status == "direct-static" and plane not in {"static", "hybrid"}:
            return [f"{path}: direct-static requires static or hybrid evidence"]
        if claim_status == "observed-runtime" and plane not in {"runtime", "hybrid"}:
            return [f"{path}: observed-runtime requires runtime or hybrid evidence"]
        if claim_status not in {"direct-static", "observed-runtime"}:
            return [f"{path}: E2-direct requires a direct claim_status"]
    if confidence == "E3-corroborated" and (claim_status != "corroborated" or plane != "hybrid"):
        return [f"{path}: E3-corroborated requires corroborated claim_status and hybrid evidence"]
    if severity == "P0" and (gate != "blocking" or confidence == "E1-inferred"):
        return [f"{path}: P0 requires blocking gate and direct or corroborated evidence"]
    if severity == "P1" and gate == "recordable-risk":
        return [f"{path}: P1 cannot use recordable-risk gate"]
    if severity == "P2" and gate == "blocking":
        return [f"{path}: P2 cannot use blocking gate"]
    if severity == "P3" and gate != "recordable-risk":
        return [f"{path}: P3 requires recordable-risk gate"]
    return []


def validate_review_result(result: dict[str, Any]) -> list[str]:
    findings = result.get("findings")
    if not isinstance(findings, list):
        return ["review-result: findings must be an array"]
    errors: list[str] = []
    for index, finding in enumerate(findings):
        if isinstance(finding, dict):
            errors.extend(validate_finding_classification(finding, f"review-result.findings[{index}]"))
            errors.extend(validate_finding_assessment(finding, f"review-result.findings[{index}]"))
    return errors


def validate_schema_documents() -> tuple[dict[str, dict[str, Any]], list[str]]:
    schemas: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schema = load_json(path)
        schemas[path.stem.removesuffix(".schema")] = schema
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            errors.append(f"{path.name}: missing expected $schema")
        if schema.get("type") != "object":
            errors.append(f"{path.name}: root must be an object")
        unsupported = set(schema) - SUPPORTED_KEYWORDS
        if unsupported:
            errors.append(f"{path.name}: unsupported root keywords {sorted(unsupported)}")
    expected = {"run", "finding", "artifact", "phase-envelope", "retention-policy", "acceptance-trace", "review-result", "patch-proposal-result"}
    missing = expected - set(schemas)
    if missing:
        errors.append(f"missing schemas: {sorted(missing)}")
    return schemas, errors


def validate_run_bundle(bundle: dict[str, Any], schemas: dict[str, dict[str, Any]], source: str) -> list[str]:
    errors: list[str] = []
    run = bundle.get("run")
    if not isinstance(run, dict):
        return [f"{source}: missing run"]
    errors.extend(validate_instance(run, schemas["run"], f"{source}.run"))
    if run.get("repair_cycle_count", 0) > run.get("max_repair_cycles", 0):
        errors.append(f"{source}: repair cycle count exceeds configured maximum")
    for index, finding in enumerate(bundle.get("findings", [])):
        errors.extend(validate_instance(finding, schemas["finding"], f"{source}.findings[{index}]"))
        if isinstance(finding, dict):
            errors.extend(validate_finding_classification(finding, f"{source}.findings[{index}]"))
            errors.extend(validate_finding_assessment(finding, f"{source}.findings[{index}]"))
        if finding.get("run_id") != run.get("run_id"):
            errors.append(f"{source}: finding run_id does not match run")
    finding_ids = [finding.get("finding_id") for finding in bundle.get("findings", [])]
    finding_fingerprints = [finding.get("fingerprint") for finding in bundle.get("findings", [])]
    if len(finding_ids) != len(set(finding_ids)):
        errors.append(f"{source}: finding IDs must be unique within a run")
    if len(finding_fingerprints) != len(set(finding_fingerprints)):
        errors.append(f"{source}: finding fingerprints must be deduplicated within a run")
    for index, artifact in enumerate(bundle.get("artifacts", [])):
        errors.extend(validate_instance(artifact, schemas["artifact"], f"{source}.artifacts[{index}]"))
        if artifact.get("run_id") != run.get("run_id"):
            errors.append(f"{source}: artifact run_id does not match run")
    artifacts_by_id = {artifact.get("artifact_id"): artifact for artifact in bundle.get("artifacts", [])}
    artifact_ids = set(artifacts_by_id)
    if len(artifact_ids) != len(bundle.get("artifacts", [])):
        errors.append(f"{source}: artifact IDs must be unique within a run")
    for artifact in bundle.get("artifacts", []):
        for input_ref in artifact.get("input_artifact_refs", []):
            if input_ref not in artifact_ids:
                errors.append(f"{source}: artifact input ref does not resolve: {input_ref}")
    for finding in bundle.get("findings", []):
        for evidence_ref in finding.get("evidence_refs", []):
            if evidence_ref not in artifact_ids:
                errors.append(f"{source}: finding evidence ref does not resolve: {evidence_ref}")
        decision_ref = finding.get("accepted_risk_decision_ref")
        if decision_ref != "none" and decision_ref not in artifact_ids:
            errors.append(f"{source}: accepted-risk decision ref does not resolve: {decision_ref}")
        if finding.get("status") == "accepted-risk" and decision_ref in artifacts_by_id:
            decision_artifact = artifacts_by_id[decision_ref]
            if decision_artifact.get("kind") != "decision" or decision_artifact.get("actor") != "user":
                errors.append(f"{source}: accepted risk requires a user decision artifact")
    for index, envelope in enumerate(bundle.get("phase_envelopes", [])):
        errors.extend(validate_instance(envelope, schemas["phase-envelope"], f"{source}.phase_envelopes[{index}]"))
        if envelope.get("run_id") != run.get("run_id"):
            errors.append(f"{source}: phase envelope run_id does not match run")
        for input_ref in envelope.get("input_artifact_refs", []):
            if input_ref not in artifact_ids:
                errors.append(f"{source}: phase envelope input ref does not resolve: {input_ref}")
    traces = bundle.get("acceptance_traces", [])
    trace_ids = [trace.get("trace_id") for trace in traces]
    if len(trace_ids) != len(set(trace_ids)):
        errors.append(f"{source}: acceptance trace IDs must be unique within a run")
    for index, trace in enumerate(traces):
        errors.extend(validate_instance(trace, schemas["acceptance-trace"], f"{source}.acceptance_traces[{index}]"))
        if trace.get("run_id") != run.get("run_id"):
            errors.append(f"{source}: acceptance trace run_id does not match run")
        for evidence_ref in trace.get("evidence_refs", []):
            if evidence_ref not in artifact_ids:
                errors.append(f"{source}: acceptance trace evidence ref does not resolve: {evidence_ref}")
    policy = bundle.get("retention_policy")
    if not isinstance(policy, dict):
        errors.append(f"{source}: missing retention_policy")
    else:
        errors.extend(validate_instance(policy, schemas["retention-policy"], f"{source}.retention_policy"))

    closed_statuses = {"verified", "retracted-with-evidence"}
    terminal_outcomes = {"PASS", "PASS_WITH_ACCEPTED_RISK", "NEEDS_USER_DECISION", "NO_MATERIAL_DELTA", "SNAPSHOT_STALE", "BLOCKED", "CANCELLED", "STALE"}
    if run.get("outcome") in terminal_outcomes and run.get("state") != run.get("outcome"):
        errors.append(f"{source}: terminal state and outcome must match")
    if run.get("outcome") == "IN_PROGRESS" and run.get("state") in terminal_outcomes:
        errors.append(f"{source}: terminal state cannot have IN_PROGRESS outcome")
    if run.get("outcome") == "PASS":
        for finding in bundle.get("findings", []):
            if finding.get("status") not in closed_statuses:
                errors.append(f"{source}: PASS requires every finding to be verified or retracted")
        if run.get("open_high_risk_assumptions") != 0:
            errors.append(f"{source}: PASS requires no open high-risk assumptions")
        if not traces or any(trace.get("result") != "passed" for trace in traces):
            errors.append(f"{source}: PASS requires passed acceptance traces")
    if run.get("outcome") == "PASS_WITH_ACCEPTED_RISK":
        accepted_risk_count = 0
        for finding in bundle.get("findings", []):
            if finding.get("status") == "accepted-risk" and finding.get("origin_severity") not in {"P2", "P3"}:
                errors.append(f"{source}: accepted risk must originate as P2/P3")
            if finding.get("status") == "accepted-risk" and finding.get("accepted_risk_decision_ref") == "none":
                errors.append(f"{source}: accepted risk requires a decision reference")
            if finding.get("status") == "accepted-risk":
                accepted_risk_count += 1
            if finding.get("status") not in closed_statuses | {"accepted-risk"}:
                errors.append(f"{source}: PASS_WITH_ACCEPTED_RISK cannot retain unresolved findings")
        if accepted_risk_count == 0:
            errors.append(f"{source}: PASS_WITH_ACCEPTED_RISK requires an accepted risk")
        if run.get("open_high_risk_assumptions") != 0:
            errors.append(f"{source}: PASS_WITH_ACCEPTED_RISK requires no open high-risk assumptions")
        if not traces or any(trace.get("result") != "passed" for trace in traces):
            errors.append(f"{source}: PASS_WITH_ACCEPTED_RISK requires passed acceptance traces")
    if run.get("outcome") == "NO_MATERIAL_DELTA":
        if not any(f.get("repeat_count", 0) >= 2 for f in bundle.get("findings", [])):
            errors.append(f"{source}: NO_MATERIAL_DELTA requires a repeated finding")
    if run.get("outcome") == "SNAPSHOT_STALE" and run.get("target_snapshot_hash") == run.get("current_snapshot_hash"):
        errors.append(f"{source}: SNAPSHOT_STALE requires distinct target and current hashes")
    return errors


def validate_policy_fixtures() -> list[str]:
    errors: list[str] = []
    lease = load_json(FIXTURE_DIR / "lease-cases.json")
    expected_lease = {"LEASE-01": "LEASE_CONFLICT", "LEASE-02": "LEASE_ACQUIRED"}
    for case in lease.get("cases", []):
        if case.get("expected") != expected_lease.get(case.get("case_id")):
            errors.append(f"lease fixture has an invalid oracle: {case.get('case_id')}")
    redaction = load_json(FIXTURE_DIR / "redaction-cases.json")
    for case in redaction.get("cases", []):
        if "secret" in case.get("expected_persisted_value", "").lower() or case.get("expected_status") != "caller-attested-redacted":
            errors.append(f"redaction fixture persists secret-like content: {case.get('case_id')}")
    injection = load_json(FIXTURE_DIR / "injection-cases.json")
    for case in injection.get("cases", []):
        if case.get("expected_authority") != "immutable-anchor" or case.get("expected_action") != "record-as-data-no-command":
            errors.append(f"injection fixture has an unsafe oracle: {case.get('case_id')}")
    return errors


def validate_repository() -> list[str]:
    schemas, errors = validate_schema_documents()
    if errors:
        return errors
    for path in sorted(FIXTURE_DIR.glob("*-run.json")):
        errors.extend(validate_run_bundle(load_json(path), schemas, path.name))
    review_result = load_json(FIXTURE_DIR / "review-result.json")
    errors.extend(validate_instance(review_result, schemas["review-result"], "review-result.json"))
    if isinstance(review_result, dict):
        errors.extend(validate_review_result(review_result))
    errors.extend(validate_instance(load_json(FIXTURE_DIR / "patch-proposal-result.json"), schemas["patch-proposal-result"], "patch-proposal-result.json"))
    errors.extend(validate_policy_fixtures())
    errors.extend(validate_format_contract_registry(ROOT))
    errors.extend(validate_state_schema_parity(ROOT))
    return errors


def main() -> int:
    errors = validate_repository()
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
