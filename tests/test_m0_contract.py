from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("m0_validator", ROOT / "scripts" / "validate_m0_contract.py")
assert SPEC and SPEC.loader
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


class M0ContractTests(unittest.TestCase):
    def test_repository_contract_is_valid(self) -> None:
        self.assertEqual(VALIDATOR.validate_repository(), [])

    def test_validator_rejects_missing_required_run_field(self) -> None:
        schema = json.loads((ROOT / "nlp_refinement" / "resources" / "schemas" / "v4" / "run.schema.json").read_text())
        run = json.loads((ROOT / "fixtures" / "v4" / "green-run.json").read_text())["run"]
        del run["target_snapshot_hash"]
        errors = VALIDATOR.validate_instance(run, schema)
        self.assertTrue(any("target_snapshot_hash" in error for error in errors))

    def test_schema_rejects_more_than_five_repair_cycles(self) -> None:
        schema = json.loads((ROOT / "nlp_refinement" / "resources" / "schemas" / "v4" / "run.schema.json").read_text())
        run = json.loads((ROOT / "fixtures" / "v4" / "green-run.json").read_text())["run"]
        run["max_repair_cycles"] = 6
        errors = VALIDATOR.validate_instance(run, schema)
        self.assertTrue(any("above maximum" in error for error in errors))

    def test_validator_rejects_open_p1_as_pass(self) -> None:
        bundle = json.loads((ROOT / "fixtures" / "v4" / "yellow-run.json").read_text())
        bundle["run"]["state"] = "PASS"
        bundle["run"]["outcome"] = "PASS"
        schemas, errors = VALIDATOR.validate_schema_documents()
        self.assertEqual(errors, [])
        outcome_errors = VALIDATOR.validate_run_bundle(bundle, schemas, "mutated-yellow")
        self.assertTrue(any("verified or retracted" in error for error in outcome_errors))

    def test_validator_rejects_unapproved_p1_risk(self) -> None:
        bundle = json.loads((ROOT / "fixtures" / "v4" / "accepted-risk-run.json").read_text())
        bundle["findings"][0]["origin_severity"] = "P1"
        schemas, errors = VALIDATOR.validate_schema_documents()
        self.assertEqual(errors, [])
        outcome_errors = VALIDATOR.validate_run_bundle(bundle, schemas, "mutated-risk")
        self.assertTrue(any("P2/P3" in error for error in outcome_errors))

    def test_validator_rejects_non_user_risk_decision(self) -> None:
        bundle = json.loads((ROOT / "fixtures" / "v4" / "accepted-risk-run.json").read_text())
        bundle["artifacts"][1]["actor"] = "reviewer"
        schemas, errors = VALIDATOR.validate_schema_documents()
        self.assertEqual(errors, [])
        outcome_errors = VALIDATOR.validate_run_bundle(bundle, schemas, "non-user-decision")
        self.assertTrue(any("user decision artifact" in error for error in outcome_errors))

    def test_validator_rejects_high_risk_assumption_as_pass(self) -> None:
        bundle = json.loads((ROOT / "fixtures" / "v4" / "green-run.json").read_text())
        bundle["run"]["open_high_risk_assumptions"] = 1
        schemas, errors = VALIDATOR.validate_schema_documents()
        self.assertEqual(errors, [])
        outcome_errors = VALIDATOR.validate_run_bundle(bundle, schemas, "high-risk-pass")
        self.assertTrue(any("high-risk assumptions" in error for error in outcome_errors))

    def test_validator_rejects_missing_evidence_artifact(self) -> None:
        bundle = json.loads((ROOT / "fixtures" / "v4" / "green-run.json").read_text())
        bundle["artifacts"] = []
        schemas, errors = VALIDATOR.validate_schema_documents()
        self.assertEqual(errors, [])
        outcome_errors = VALIDATOR.validate_run_bundle(bundle, schemas, "missing-evidence")
        self.assertTrue(any("does not resolve" in error for error in outcome_errors))

    def test_validator_rejects_duplicate_artifact_id(self) -> None:
        bundle = json.loads((ROOT / "fixtures" / "v4" / "green-run.json").read_text())
        bundle["artifacts"].append(bundle["artifacts"][0].copy())
        schemas, errors = VALIDATOR.validate_schema_documents()
        self.assertEqual(errors, [])
        outcome_errors = VALIDATOR.validate_run_bundle(bundle, schemas, "duplicate-artifact")
        self.assertTrue(any("artifact IDs must be unique" in error for error in outcome_errors))

    def test_snapshot_stale_requires_a_different_snapshot(self) -> None:
        bundle = json.loads((ROOT / "fixtures" / "v4" / "snapshot-stale-run.json").read_text())
        bundle["run"]["current_snapshot_hash"] = bundle["run"]["target_snapshot_hash"]
        schemas, errors = VALIDATOR.validate_schema_documents()
        self.assertEqual(errors, [])
        outcome_errors = VALIDATOR.validate_run_bundle(bundle, schemas, "not-stale")
        self.assertTrue(any("distinct target and current" in error for error in outcome_errors))

    def test_validator_rejects_unresolved_acceptance_trace_as_pass(self) -> None:
        bundle = json.loads((ROOT / "fixtures" / "v4" / "green-run.json").read_text())
        bundle["acceptance_traces"][0]["result"] = "not-run"
        schemas, errors = VALIDATOR.validate_schema_documents()
        self.assertEqual(errors, [])
        outcome_errors = VALIDATOR.validate_run_bundle(bundle, schemas, "unverified-trace")
        self.assertTrue(any("passed acceptance traces" in error for error in outcome_errors))

    def test_review_result_schema_rejects_unknown_field(self) -> None:
        result = json.loads((ROOT / "fixtures" / "v4" / "review-result.json").read_text())
        result["findings"][0]["priority_override"] = "PASS"
        schemas, errors = VALIDATOR.validate_schema_documents()
        self.assertEqual(errors, [])
        outcome_errors = VALIDATOR.validate_instance(result, schemas["review-result"], "mutated-review")
        self.assertTrue(any("unexpected key" in error for error in outcome_errors))

    def test_validator_rejects_runtime_category_with_static_evidence(self) -> None:
        result = json.loads((ROOT / "fixtures" / "v4" / "review-result.json").read_text())
        result["findings"][0].update(
            {
                "subject_kind": "nl_host_boundary",
                "evidence_plane": "static",
                "category": "instruction_adherence",
            }
        )
        self.assertTrue(any("requires runtime" in error for error in VALIDATOR.validate_review_result(result)))

    def test_patch_proposal_schema_rejects_missing_human_only_attestation(self) -> None:
        result = json.loads((ROOT / "fixtures" / "v4" / "patch-proposal-result.json").read_text())
        del result["no_target_write_attestation"]
        schemas, errors = VALIDATOR.validate_schema_documents()
        self.assertEqual(errors, [])
        outcome_errors = VALIDATOR.validate_instance(result, schemas["patch-proposal-result"], "mutated-patch-proposal")
        self.assertTrue(any("no_target_write_attestation" in error for error in outcome_errors))

    def test_patch_proposal_scorecard_is_closed_and_required_for_response_format(self) -> None:
        schema = json.loads((ROOT / "nlp_refinement" / "resources" / "schemas" / "v4" / "patch-proposal-result.schema.json").read_text())
        scores = schema["properties"]["proposals"]["items"]["properties"]["assessment"]["properties"]["scores"]
        expected_maxima = {
            "coverage": 20,
            "causal": 20,
            "scope": 15,
            "verification": 15,
            "regression": 10,
            "evidence": 10,
            "alternatives": 5,
            "clarity": 5,
        }
        self.assertEqual(scores["type"], "object")
        self.assertEqual(set(scores["properties"]), set(expected_maxima))
        self.assertEqual(set(scores["required"]), set(expected_maxima))
        self.assertFalse(scores["additionalProperties"])
        self.assertEqual({key: value["maximum"] for key, value in scores["properties"].items()}, expected_maxima)


if __name__ == "__main__":
    unittest.main()
