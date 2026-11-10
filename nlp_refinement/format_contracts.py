"""Static parity checks for externally produced phase-output contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_EXPECTED_CONTRACT_IDS = {"review-result/v5", "patch-proposal-result/v5"}
_VALID_OWNERS = {"review_adapter", "patch_proposal_adapter"}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalized_prompt_text(value: str) -> str:
    return " ".join(value.replace("`", "").split())


def load_format_contract_registry(root: Path) -> dict[str, Any]:
    value = _load_json(root / "contracts/v5/phase-output-contracts.json")
    if not isinstance(value, dict):
        raise ValueError("format contract registry must be a JSON object")
    return value


def load_contract_cases(root: Path) -> dict[str, Any]:
    value = _load_json(root / "fixtures/v5/format-contract-cases.json")
    if not isinstance(value, dict):
        raise ValueError("format contract cases must be a JSON object")
    return value


def validate_format_contract_registry(root: Path) -> list[str]:
    """Check traceability only; adapters remain the semantic authority."""
    errors: list[str] = []
    try:
        registry = load_format_contract_registry(root)
        cases = load_contract_cases(root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"format contract files are unreadable: {type(error).__name__}"]
    if set(registry) != {"registry_version", "contracts"} or registry.get("registry_version") != "1.0":
        return ["format contract registry has an unsupported shape or version"]
    contracts = registry.get("contracts")
    if not isinstance(contracts, list):
        return ["format contract registry contracts must be an array"]
    ids: set[str] = set()
    declared_case_ids: set[str] = set()
    declared_rules: dict[str, str] = {}
    for contract in contracts:
        if not isinstance(contract, dict) or set(contract) != {
            "contract_id", "schema_path", "prompt_path", "valid_fixture_path", "semantic_rules"
        }:
            errors.append("format contract has unexpected or missing fields")
            continue
        contract_id = contract["contract_id"]
        if not isinstance(contract_id, str) or contract_id in ids:
            errors.append("format contract IDs must be unique strings")
            continue
        ids.add(contract_id)
        for field in ("schema_path", "prompt_path", "valid_fixture_path"):
            relative = contract[field]
            if not isinstance(relative, str) or not relative or not (root / relative).is_file():
                errors.append(f"{contract_id}: {field} does not resolve")
        prompt_path = contract["prompt_path"]
        prompt = (root / prompt_path).read_text(encoding="utf-8") if isinstance(prompt_path, str) and (root / prompt_path).is_file() else ""
        rules = contract["semantic_rules"]
        if not isinstance(rules, list) or not rules:
            errors.append(f"{contract_id}: semantic_rules must be a non-empty array")
            continue
        rule_ids: set[str] = set()
        for rule in rules:
            if not isinstance(rule, dict) or set(rule) != {
                "rule_id", "owner", "prompt_requirement", "negative_case_ids"
            }:
                errors.append(f"{contract_id}: semantic rule has unexpected or missing fields")
                continue
            rule_id = rule["rule_id"]
            if not isinstance(rule_id, str) or rule_id in rule_ids:
                errors.append(f"{contract_id}: semantic rule IDs must be unique strings")
                continue
            rule_ids.add(rule_id)
            if rule["owner"] not in _VALID_OWNERS:
                errors.append(f"{contract_id}: semantic rule owner is invalid")
            elif rule_id in declared_rules:
                errors.append("format contract semantic rule IDs must be globally unique")
            else:
                declared_rules[rule_id] = rule["owner"]
            if f"<!-- format-rule: {rule_id} -->" not in prompt:
                errors.append(f"{contract_id}: prompt omits marker for {rule_id}")
            if (
                not isinstance(rule["prompt_requirement"], str)
                or _normalized_prompt_text(rule["prompt_requirement"])
                not in _normalized_prompt_text(prompt)
            ):
                errors.append(f"{contract_id}: prompt omits requirement for {rule_id}")
            negative = rule["negative_case_ids"]
            if not isinstance(negative, list) or not negative or not all(isinstance(case_id, str) for case_id in negative):
                errors.append(f"{contract_id}: semantic rule needs negative case IDs")
            else:
                declared_case_ids.update(negative)
    if ids != _EXPECTED_CONTRACT_IDS:
        errors.append("format contract registry must declare exactly the supported phase outputs")
    case_rows = cases.get("cases") if cases.get("schema_version") == "5.0" else None
    if not isinstance(case_rows, list):
        errors.append("format contract cases must declare schema version 5.0 and cases")
        return errors
    case_ids = {case.get("case_id") for case in case_rows if isinstance(case, dict)}
    if len(case_ids) != len(case_rows) or not all(isinstance(case_id, str) for case_id in case_ids):
        errors.append("format contract case IDs must be unique strings")
    if case_ids != declared_case_ids:
        errors.append("format contract negative cases and registry declarations differ")
    for case in case_rows:
        if not isinstance(case, dict) or set(case) != {"case_id", "rule_id", "owner", "base_fixture", "mutation"}:
            errors.append("format contract case has unexpected or missing fields")
            continue
        if (
            not isinstance(case["case_id"], str)
            or case["owner"] not in _VALID_OWNERS
            or not isinstance(case["rule_id"], str)
        ):
            errors.append("format contract case owner or rule ID is invalid")
        elif declared_rules.get(case["rule_id"]) != case["owner"]:
            errors.append("format contract case does not match its declared semantic rule")
        if not isinstance(case["base_fixture"], str) or not (root / case["base_fixture"]).is_file():
            errors.append("format contract case base fixture does not resolve")
    return errors


def validate_state_schema_parity(root: Path) -> list[str]:
    """Ensure persisted-v5 enum surfaces match their StateStore authority."""
    from nlp_refinement import state_store

    checks = (
        ("artifact", "kind", state_store.ARTIFACT_KINDS),
        ("artifact", "actor", state_store.ARTIFACT_ACTORS),
        ("artifact", "redaction_status", state_store.REDACTION_STATUSES),
        ("finding", "origin_severity", state_store.FINDING_SEVERITIES),
        ("finding", "effective_gate", state_store.FINDING_GATES),
        ("finding", "status", state_store.FINDING_STATUSES),
        ("finding", "subject_kind", state_store.FINDING_SUBJECT_KINDS),
        ("finding", "evidence_plane", state_store.FINDING_EVIDENCE_PLANES),
        ("finding", "evidence_confidence", state_store.FINDING_EVIDENCE_CONFIDENCE),
        ("finding", "impact_scope", state_store.FINDING_IMPACT_SCOPES),
        ("finding", "claim_status", state_store.FINDING_CLAIM_STATUSES),
        ("run", "state", state_store.RUN_STATES),
        ("run", "outcome", state_store.RUN_OUTCOMES),
    )
    errors: list[str] = []
    for schema_name, property_name, runtime_values in checks:
        schema = _load_json(root / "nlp_refinement/resources/schemas/v5" / f"{schema_name}.schema.json")
        values = schema.get("properties", {}).get(property_name, {}).get("enum")
        if not isinstance(values, list) or set(values) != set(runtime_values):
            errors.append(f"{schema_name}.{property_name} enum differs from StateStore")
    return errors
