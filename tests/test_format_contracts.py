from __future__ import annotations

import json
import unittest
from pathlib import Path

from nlp_refinement.format_contracts import (
    load_contract_cases,
    load_format_contract_registry,
    validate_format_contract_registry,
    validate_state_schema_parity,
)
from nlp_refinement.patch_proposal_adapter import PatchProposalInputError, validate_patch_proposal
from nlp_refinement.review_adapter import ReviewInputError, validate_review_result


ROOT = Path(__file__).resolve().parents[1]


def _replace_path(value: object, path: str, replacement: object) -> None:
    """Replace a JSON-pointer-like path used only by controlled fixtures."""
    parts = path.removeprefix("/").split("/")
    target = value
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]  # type: ignore[index]
    leaf = parts[-1]
    if isinstance(target, list):
        target[int(leaf)] = replacement
    else:
        target[leaf] = replacement  # type: ignore[index]


def _apply_mutation(value: object, mutation: dict[str, object]) -> None:
    if mutation["kind"] == "replace":
        _replace_path(value, str(mutation["path"]), mutation["value"])
        return
    target = value
    for part in str(mutation["path"]).removeprefix("/").split("/"):
        target = target[int(part)] if isinstance(target, list) else target[part]  # type: ignore[index]
    if mutation["kind"] == "duplicate-first-array-item" and isinstance(target, list) and target:
        target.extend([json.loads(json.dumps(target[0]))] * (int(mutation["count"]) - 1))
        return
    if mutation["kind"] == "replace-fields" and isinstance(target, dict) and isinstance(mutation["values"], dict):
        target.update(mutation["values"])
        return
    raise AssertionError(f"unsupported controlled mutation: {mutation}")


class FormatContractTests(unittest.TestCase):
    def test_registry_and_persisted_schema_parity_are_valid(self) -> None:
        self.assertEqual(validate_format_contract_registry(ROOT), [])
        self.assertEqual(validate_state_schema_parity(ROOT), [])

    def test_each_registered_valid_fixture_is_accepted_by_its_owner(self) -> None:
        registry = load_format_contract_registry(ROOT)
        for contract in registry["contracts"]:
            value = json.loads((ROOT / contract["valid_fixture_path"]).read_text(encoding="utf-8"))
            owners = {rule["owner"] for rule in contract["semantic_rules"]}
            with self.subTest(contract_id=contract["contract_id"]):
                self.assertEqual(len(owners), 1)
                if owners == {"review_adapter"}:
                    validate_review_result(value)
                elif owners == {"patch_proposal_adapter"}:
                    validate_patch_proposal(value)
                else:
                    self.fail(f"unsupported contract owner: {owners}")

    def test_each_negative_contract_case_is_rejected_by_its_owner(self) -> None:
        registry = load_format_contract_registry(ROOT)
        case_ids = {
            case_id
            for contract in registry["contracts"]
            for rule in contract["semantic_rules"]
            for case_id in rule["negative_case_ids"]
        }
        cases = {case["case_id"]: case for case in load_contract_cases(ROOT)["cases"]}
        self.assertEqual(case_ids, set(cases))

        for case_id in sorted(case_ids):
            case = cases[case_id]
            value = json.loads((ROOT / case["base_fixture"]).read_text(encoding="utf-8"))
            _apply_mutation(value, case["mutation"])
            with self.subTest(case_id=case_id):
                if case["owner"] == "review_adapter":
                    with self.assertRaises(ReviewInputError):
                        validate_review_result(value)
                elif case["owner"] == "patch_proposal_adapter":
                    with self.assertRaises(PatchProposalInputError):
                        validate_patch_proposal(value)
                else:
                    self.fail(f"unsupported contract owner: {case['owner']}")

    def test_malformed_hunk_is_rejected(self) -> None:
        proposal = json.loads((ROOT / "fixtures/v5/patch-proposal-result.json").read_text(encoding="utf-8"))
        proposal["proposals"][0]["unified_diff"] = "--- a/file.md\n+++ b/file.md\n@@ not-a-hunk\n"
        with self.assertRaises(PatchProposalInputError):
            validate_patch_proposal(proposal)


if __name__ == "__main__":
    unittest.main()
