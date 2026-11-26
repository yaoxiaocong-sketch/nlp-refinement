from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "acceptance" / "verify_tui_release_candidate.py"
SPEC = importlib.util.spec_from_file_location("tui_release_candidate_verifier", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def receipt(role: str, bytes_count: int) -> dict[str, object]:
    return {
        "receipt_schema": "tui-child-dispatch-receipt/v2",
        "role": role,
        "child_thread_id": f"/root/{role}",
        "prompt_utf8_bytes": bytes_count,
        "observed_cwd": "unverified",
        "host_role_binding": "unverified",
        "child_sandbox": "unverified",
    }


class TuiReleaseCandidateVerifierTests(unittest.TestCase):
    def test_three_child_inline_receipts_are_accepted_without_claiming_host_isolation(self) -> None:
        rendered = VERIFIER.validate_completed_run(
            identity={"commands": list(VERIFIER.REQUIRED_RUNTIME_COMMANDS)},
            receipts={"receipts": [receipt("reviewer", 4_394), receipt("proposer", 3_966), receipt("proposal-assessor", 6_184)], "no_target_write_attestation": True},
            disposition={"disposition": "AWAITING_HUMAN", "no_target_write_attestation": True},
            expected_roles={"reviewer", "proposer", "proposal-assessor"},
            expected_disposition="AWAITING_HUMAN",
            minimum_prompt_bytes=1_001,
            actual_target_sha256="a" * 64,
            expected_target_sha256="a" * 64,
        )
        self.assertEqual(rendered["roles"], ["proposal-assessor", "proposer", "reviewer"])
        self.assertTrue(rendered["no_target_write_attestation"])

    def test_rejects_a_return_to_short_legacy_bootstrap_or_a_changed_target(self) -> None:
        arguments = {
            "identity": {"commands": list(VERIFIER.REQUIRED_RUNTIME_COMMANDS)},
            "receipts": {"receipts": [receipt("reviewer", 789)], "no_target_write_attestation": True},
            "disposition": {"disposition": "AWAITING_HUMAN", "no_target_write_attestation": True},
            "expected_roles": {"reviewer"},
            "expected_disposition": "AWAITING_HUMAN",
            "minimum_prompt_bytes": 1_001,
            "actual_target_sha256": "a" * 64,
            "expected_target_sha256": "a" * 64,
        }
        with self.assertRaisesRegex(ValueError, "inline-prompt size"):
            VERIFIER.validate_completed_run(**arguments)
        arguments["receipts"] = {"receipts": [receipt("reviewer", 4_394)]}
        arguments["actual_target_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "target artifact hash changed"):
            VERIFIER.validate_completed_run(**arguments)

    def test_rejects_a_missing_zero_target_write_attestation(self) -> None:
        with self.assertRaisesRegex(ValueError, "did not attest zero target writes"):
            VERIFIER.validate_completed_run(
                identity={"commands": list(VERIFIER.REQUIRED_RUNTIME_COMMANDS)},
                receipts={"receipts": [receipt("reviewer", 4_394)]},
                disposition={"disposition": "AWAITING_HUMAN", "no_target_write_attestation": True},
                expected_roles={"reviewer"},
                expected_disposition="AWAITING_HUMAN",
                minimum_prompt_bytes=1_001,
                actual_target_sha256="a" * 64,
                expected_target_sha256="a" * 64,
            )


if __name__ == "__main__":
    unittest.main()
