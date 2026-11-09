from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

from nlp_refinement.diagnostic_doe_support import run_isolated_diagnostic
from nlp_refinement.host_client import HostProtocolError, HostResult
from nlp_refinement.host_executor import DiagnosticAppServerHostExecutor
from nlp_refinement.m8_review_coordinator import M8ReviewCoordinator
from nlp_refinement.host_diagnostics import (
    DiagnosticCapture,
    ProposalValidationCapture,
    RawDiagnosticCapture,
    ReviewQualityCapture,
)
from nlp_refinement.patch_proposal_adapter import PatchProposalInputError
from nlp_refinement.state_store import StateStore


ROOT = Path(__file__).resolve().parents[1]


class HostDiagnosticTests(unittest.TestCase):
    def test_shared_diagnostic_harness_reaches_the_current_coordinator_contract(self) -> None:
        capture = DiagnosticCapture()

        class FakeClient:
            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def run_read_only_role(self, *, role: str, **_: object) -> HostResult:
                return HostResult(
                    role=role,
                    thread_id="fixture-thread",
                    turn_id="fixture-turn",
                    raw_output="not-json",
                    events=(),
                    effective_sandbox="read-only",
                )

        def coordinator_factory(store: StateStore, _capture: DiagnosticCapture) -> M8ReviewCoordinator:
            return M8ReviewCoordinator(store, executor_factory=FakeClient)

        code, report = run_isolated_diagnostic(
            capture,
            "fixture-current-coordinator-contract/v1",
            coordinator_factory=coordinator_factory,
        )

        self.assertEqual(code, 1)
        self.assertEqual(report["error_class"], "M8ReviewCoordinatorError")
        self.assertEqual(report["artifact_kinds"], ["integrity", "integrity", "integrity"])
        self.assertEqual(report["diagnostic_signals"], [])

    def test_capture_retains_only_whitelisted_failure_fields(self) -> None:
        capture = DiagnosticCapture()
        secret = "this free-form host error must never be retained"
        capture.record_failure(
            "proposal",
            [
                {
                    "method": "error",
                    "params": {
                        "code": "host_rejected",
                        "message": secret,
                        "detail": {"text": secret, "category": "request"},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {
                            "status": "failed",
                            "error": {"code": "invalid_request", "message": secret},
                        }
                    },
                },
            ],
        )

        signals = capture.snapshot()

        self.assertEqual(signals[0]["method"], "error")
        self.assertEqual(signals[0]["fields"], {"code": "host_rejected", "detail.category": "request"})
        self.assertEqual(signals[1]["fields"], {"turn.status": "failed", "turn.error.code": "invalid_request"})
        self.assertNotIn(secret, json.dumps(signals))

    def test_diagnostic_cli_is_a_separate_single_attempt_entrypoint(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/run_m8_host_diagnostic_doe.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("diagnostic DOE", completed.stdout)
        self.assertIn("no durable diagnostic capture", completed.stdout)

    def test_raw_diagnostic_cli_is_a_distinct_terminal_only_entrypoint(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/run_m8_host_raw_diagnostic_doe.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("raw-fragment diagnostic DOE", completed.stdout)
        self.assertIn("terminal-only", completed.stdout)

    def test_proposal_validation_diagnostic_cli_is_a_distinct_terminal_only_entrypoint(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/run_m8_host_proposal_validation_doe.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        rendered_help = completed.stdout.replace("-\n", "-")
        self.assertIn("proposal-validation diagnostic DOE", rendered_help)
        self.assertIn("terminal-only", rendered_help)
        self.assertIn("--doe-kind", rendered_help)

    def test_reviewer_quality_diagnostic_cli_is_a_distinct_terminal_only_entrypoint(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/run_m8_host_reviewer_quality_diagnostic_doe.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        rendered_help = completed.stdout.replace("-\n", "-")
        self.assertIn("reviewer-quality diagnostic DOE", rendered_help)
        self.assertIn("terminal-only", rendered_help)
        self.assertIn("--doe-kind", rendered_help)

    def test_diagnostic_executor_captures_only_the_failing_role_events(self) -> None:
        capture = DiagnosticCapture()
        class FailingClient:
            def __init__(self) -> None:
                self.events: list[dict[str, object]] = []

            def __enter__(self) -> "FailingClient":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            @property
            def event_count(self) -> int:
                return len(self.events)

            def events_after(self, start: int) -> list[dict[str, object]]:
                return self.events[start:]

            def run_read_only_role(self, **_: object) -> HostResult:
                self.events.append(
                    {"method": "error", "params": {"code": "host_rejected", "message": "do not retain"}}
                )
                raise HostProtocolError("host failed")

        client = FailingClient()
        executor = DiagnosticAppServerHostExecutor(capture.record_failure, client_factory=lambda: client)  # type: ignore[arg-type]
        with executor:
            with self.assertRaises(HostProtocolError):
                executor.run_read_only_role(
                    role="proposal",
                    cwd=ROOT,
                    developer_instructions="fixture",
                    prompt="fixture",
                    output_schema={},
                )

        self.assertEqual(
            capture.snapshot(),
            [{"role": "proposal", "method": "error", "fields": {"code": "host_rejected"}}],
        )

    def test_raw_capture_is_limited_to_proposal_failure_events_and_redacts_sensitive_values(self) -> None:
        capture = RawDiagnosticCapture()
        capture.record_failure(
            "reviewer",
            [{"method": "error", "params": {"message": "do not retain reviewer"}}],
        )
        capture.record_failure(
            "proposal",
            [
                {"method": "thread/started", "params": {"message": "ignore"}},
                {
                    "method": "error",
                    "params": {
                        "message": "provider rejected request",
                        "authorization": "Bearer sk-should-not-appear",
                        "nested": {"token": "should-not-appear", "reason": "unsupported schema"},
                    },
                },
                {"method": "turn/completed", "params": {"turn": {"status": "failed", "error": {"message": "provider rejected"}}}},
            ],
        )

        fragments = capture.snapshot()

        self.assertEqual([entry["method"] for entry in fragments], ["error", "turn/completed"])
        self.assertEqual(fragments[0]["params"]["authorization"], "[redacted]")
        self.assertEqual(fragments[0]["params"]["nested"]["token"], "[redacted]")
        rendered = json.dumps(fragments)
        self.assertNotIn("should-not-appear", rendered)
        self.assertNotIn("do not retain reviewer", rendered)

    def test_raw_capture_replaces_an_oversized_fragment_with_a_bounded_marker(self) -> None:
        capture = RawDiagnosticCapture()
        capture.record_failure(
            "proposal",
            [
                {
                    "method": "error",
                    "params": {f"field_{index}": "x" * 1_024 for index in range(24)},
                }
            ],
        )

        self.assertEqual(
            capture.snapshot(),
            [{"role": "proposal", "method": "error", "params": {"capture_truncated": True}}],
        )

    def test_proposal_validation_capture_reports_only_local_validator_error(self) -> None:
        result = json.loads((ROOT / "fixtures" / "v4" / "patch-proposal-result.json").read_text(encoding="utf-8"))
        result["proposals"][0]["assessment"].pop("scores")
        result["summary"] = "must never appear in the diagnostic"
        capture = ProposalValidationCapture()

        capture.record_proposal_result(json.dumps(result))

        self.assertEqual(
            capture.snapshot(),
            [{
                "stage": "local-validation",
                "result": "rejected",
                "error_code": "assessment-unexpected-or-missing-fields",
            }],
        )
        self.assertNotIn("must never appear", json.dumps(capture.snapshot()))

    def test_proposal_validation_capture_uses_a_fixed_error_code_for_model_text(self) -> None:
        capture = ProposalValidationCapture()

        capture.record_proposal_result('{"summary":"secret model text"}')

        self.assertEqual(
            capture.snapshot(),
            [{"stage": "local-validation", "result": "rejected", "error_code": "result-unexpected-or-missing-fields"}],
        )
        self.assertNotIn("secret model text", json.dumps(capture.snapshot()))

    def test_proposal_validation_capture_classifies_ingestion_error_without_finding_value(self) -> None:
        capture = ProposalValidationCapture()
        valid_result = json.loads((ROOT / "fixtures" / "v4" / "patch-proposal-result.json").read_text(encoding="utf-8"))
        capture.record_proposal_result(json.dumps(valid_result))
        error = RuntimeError("coordinator failed")
        error.__cause__ = PatchProposalInputError(
            "patch proposal references unknown finding: F-secret-model-value"
        )

        capture.record_coordinator_failure(error)

        self.assertEqual(
            capture.snapshot(),
            [
                {"stage": "local-validation", "result": "accepted"},
                {"stage": "ledger-ingestion", "result": "rejected", "error_code": "references-unknown-finding"},
            ],
        )
        self.assertNotIn("F-secret-model-value", json.dumps(capture.snapshot()))

    def test_reviewer_quality_capture_summarizes_only_fixed_classification_labels(self) -> None:
        result = json.loads((ROOT / "fixtures" / "v4" / "review-result.json").read_text(encoding="utf-8"))
        result["findings"].append(
            {
                "fingerprint": "b" * 64,
                "origin_severity": "P0",
                "effective_gate": "blocking",
                "invariant_id": "I-SAFETY",
                "last_probe": "review-probe-002",
                "subject_kind": "nl_artifact",
                "evidence_plane": "static",
                "category": "safety_and_governance",
                "evidence_confidence": "E2-direct",
                "impact_scope": "security-boundary",
                "claim_status": "direct-static",
                "statement": "secret reviewer statement must not leave memory",
                "evidence": "secret reviewer evidence must not leave memory",
                "recommendation": "secret reviewer recommendation must not leave memory",
            }
        )
        capture = ReviewQualityCapture()

        capture.record_review_result(json.dumps(result))

        signal = capture.snapshot()
        self.assertEqual(
            signal,
            [
                {
                    "stage": "review-quality",
                    "result": "accepted",
                    "finding_count": 2,
                    "severity_counts": {"P0": 1, "P1": 1, "P2": 0, "P3": 0},
                    "gate_counts": {"blocking": 2, "needs-user-decision": 0, "recordable-risk": 0},
                    "category_counts": {
                        "format_and_encoding": 0,
                        "instruction_adherence": 0,
                        "layout_and_discovery": 0,
                        "load_and_discovery": 0,
                        "outcome_verification": 0,
                        "reference_integrity": 0,
                        "safety_and_governance": 1,
                        "safety_behavior": 0,
                        "schema_contract": 0,
                        "semantic_workflow": 1,
                        "state_behavior": 0,
                        "verification_design": 0,
                    },
                    "has_safety_p0": True,
                }
            ],
        )
        rendered = json.dumps(signal)
        self.assertNotIn("secret reviewer", rendered)
        self.assertNotIn("I-SAFETY", rendered)

    def test_reviewer_quality_capture_reports_local_rejection_without_model_text(self) -> None:
        capture = ReviewQualityCapture()

        capture.record_review_result('{"summary":"secret model text"}')

        self.assertEqual(
            capture.snapshot(),
            [{"stage": "review-quality", "result": "rejected", "validator_error_code": "unexpected-or-missing-fields"}],
        )
        self.assertNotIn("secret model text", json.dumps(capture.snapshot()))
