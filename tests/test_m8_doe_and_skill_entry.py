from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import runpy
import json
import os
from pathlib import Path

from nlp_refinement.host_client import HostResult
from nlp_refinement.host_execution_request import HostExecutionRequest
from nlp_refinement.m8_review_coordinator import HostReviewResult
from nlp_refinement.host_diagnostics import ProposalValidationCapture, ReviewQualityCapture
from nlp_refinement.m8_doe_fixtures import PROTOCOL_FIXTURE
from nlp_refinement.sensitive_data import redact_for_model
from nlp_refinement.state_store import StateStore


ROOT = Path(__file__).resolve().parents[1]


class M8DoeAndSkillEntryTests(unittest.TestCase):
    def test_protocol_fixture_exercises_a_synthetic_model_input_redaction(self) -> None:
        redaction = redact_for_model(PROTOCOL_FIXTURE)

        self.assertEqual(redaction.replacement_counts, {"api-key": 1})
        self.assertIn("<REDACTED:api-key:1>", redaction.text)
        self.assertNotIn("sk-proj-abcdefghijklmnopqrstuvwxyz", redaction.text)

    def test_protocol_success_requires_the_redaction_receipt_before_host_receipts(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        reviewer_receipt = {
            "artifact_role": "host-observation/v2", "role": "reviewer",
            "thread_id": "thread-review", "turn_id": "turn-review",
            "turn_status": "completed", "host_version": "host-1.0",
            "model": "model-1", "timeout_seconds": 120.0,
            "requested_sandbox": "read-only", "model_input_handling": "coordinator-redacted",
        }
        proposal_receipt = {
            "artifact_role": "host-observation/v2", "role": "proposal",
            "thread_id": "thread-proposal", "turn_id": "turn-proposal",
            "turn_status": "completed", "host_version": "host-1.0",
            "model": "model-1", "timeout_seconds": 120.0,
            "requested_sandbox": "read-only",
        }
        artifacts = [
            {"kind": "integrity", "body": json.dumps({"artifact_role": "model-input-redaction/v1", "detector_version": "1", "model_input_handling": "coordinator-redacted", "replacement_counts": {"api-key": 1}})},
            {"kind": "integrity", "body": json.dumps(reviewer_receipt)},
            {"kind": "review", "body": "{}"},
            {"kind": "integrity", "body": json.dumps(proposal_receipt)},
            {"kind": "review", "body": "{}"},
        ]

        class FixtureStore:
            def list_artifacts(self, _run_id: str) -> list[dict[str, str]]:
                return artifacts

        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "protocol.md"
            artifact.write_text(PROTOCOL_FIXTURE, encoding="utf-8")
            report = module["_assert_success"](
                FixtureStore(),
                "run_fixture",
                artifact,
                HostReviewResult("r1", "r2", "r3", "r4", ["F-001"], 1),
                PROTOCOL_FIXTURE,
                "protocol",
            )

        self.assertEqual(report["model_input_handling"], "coordinator-redacted")
        self.assertEqual(report["ledger_artifact_kinds"], ["integrity", "integrity", "review", "integrity", "review"])

    def test_isolated_host_doe_has_a_safe_command_entrypoint(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/run_m8_host_doe.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("isolated", completed.stdout)
        self.assertIn("model inference", completed.stdout)
        self.assertIn("--report-file", completed.stdout)
        self.assertIn("--failure-evidence-root", completed.stdout)
        self.assertIn("--doe-kind", completed.stdout)

    def test_doe_report_path_rejects_project_files(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        with self.assertRaisesRegex(ValueError, "/private/tmp"):
            module["_validated_report_path"](ROOT / "do-not-write.json")

    def test_doe_report_is_created_exclusively_and_is_private(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            report = module["_validated_report_path"](Path(directory) / "result.json")
            module["_write_new_private_report"](report, '{"status":"passed"}')
            self.assertEqual(report.read_text(encoding="utf-8"), '{"status":"passed"}\n')
            self.assertEqual(os.stat(report).st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                module["_write_new_private_report"](report, '{"status":"failed"}')

    def test_nlp_refine_is_the_only_skill_call_name(self) -> None:
        skill = ROOT / "skills" / "nlp-refine" / "SKILL.md"
        self.assertTrue(skill.is_file())
        self.assertFalse((ROOT / "skills" / "refine").exists())
        self.assertIn("name: nlp-refine", skill.read_text(encoding="utf-8"))

        for path in [
            skill,
            ROOT / "docs" / "M2_COMMAND.md",
            ROOT / "docs" / "M8_HOST_COORDINATOR.md",
        ]:
            self.assertNotIn("$refine", path.read_text(encoding="utf-8"), path)

    def test_user_facing_protocol_descriptions_are_v5(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        taxonomy = (ROOT / "docs" / "M6_FINDING_TAXONOMY.md").read_text(encoding="utf-8")

        self.assertIn("validates v5 NL-artifact reviews", manifest["interface"]["longDescription"])
        self.assertIn("V4 reviews natural-language-programming artifacts", taxonomy)

    def test_live_host_review_documents_its_execution_boundary(self) -> None:
        for path in [
            ROOT / "skills" / "nlp-refine" / "SKILL.md",
            ROOT / "docs" / "M8_HOST_COORDINATOR.md",
        ]:
            text = path.read_text(encoding="utf-8")
            self.assertIn("host-capable, non-sandboxed process", text, path)
            self.assertIn("does not grant privileges", text, path)

    def test_doe_success_does_not_retry_or_archive_a_receipt(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        attempts: list[None] = []

        def fake_attempt(_factory: object) -> tuple[int, dict[str, object]]:
            attempts.append(None)
            return 0, {"status": "passed"}

        with tempfile.TemporaryDirectory() as directory:
            code, report = module["run"](evidence_root=Path(directory), now_fn=lambda: 100, attempt_runner=fake_attempt)
        self.assertEqual((code, len(attempts), report["attempt_count"]), (0, 1, 1))
        self.assertNotIn("failure_evidence", report)

    def test_doe_retries_once_and_archives_only_sanitized_failure_receipts(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        attempts: list[None] = []
        failure = {
            "status": "failed",
            "error_class": "M8ReviewCoordinatorError",
            "run_state": "BLOCKED",
            "run_outcome": "BLOCKED",
            "artifact_kinds": ["integrity"],
            "host_diagnostics": [{"artifact_role": "host-failure/v1", "phase": "reviewer", "error_class": "HostProtocolError"}],
            "fixture_unchanged": True,
            "target_writes": [],
            "error": "secret error text must not persist",
            "raw_output": "private model output must not persist",
        }

        def fake_attempt(_factory: object) -> tuple[int, dict[str, object]]:
            attempts.append(None)
            return (1, failure) if len(attempts) == 1 else (0, {"status": "passed"})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, report = module["run"](evidence_root=root, now_fn=lambda: 100, attempt_runner=fake_attempt)
            evidence = json.loads((root / report["failure_evidence"]["receipt_files"][0]).read_text(encoding="utf-8"))
        self.assertEqual((code, len(attempts), report["attempt_count"]), (0, 2, 2))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(evidence["expires_at"], 100 + 24 * 60 * 60)
        self.assertEqual(evidence["failure_receipts"][0]["phase"], "reviewer")
        self.assertNotIn("secret error text", json.dumps(evidence))
        self.assertNotIn("private model output", json.dumps(evidence))
        self.assertNotIn("secret error text", json.dumps(report))
        self.assertNotIn("private model output", json.dumps(report))

    def test_doe_second_failure_stops_after_two_attempts_and_purges_only_expired_receipts(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        attempts: list[None] = []
        failure = {
            "status": "failed", "error_class": "M8ReviewCoordinatorError", "run_state": "BLOCKED", "run_outcome": "BLOCKED",
            "artifact_kinds": ["integrity"], "host_diagnostics": [], "fixture_unchanged": True, "target_writes": [],
        }

        def fake_attempt(_factory: object) -> tuple[int, dict[str, object]]:
            attempts.append(None)
            return 1, dict(failure)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expired = root / "failure-0000000001-0123456789abcdef.json"
            expired.write_text(json.dumps({"expires_at": 1}), encoding="utf-8")
            untouched = root / "notes.json"
            untouched.write_text("not a receipt", encoding="utf-8")
            code, report = module["run"](evidence_root=root, now_fn=lambda: 100, attempt_runner=fake_attempt)
            self.assertFalse(expired.exists())
            self.assertTrue(untouched.exists())
            self.assertEqual(len(report["failure_evidence"]["receipt_files"]), 2)
        self.assertEqual((code, len(attempts), report["attempt_count"]), (1, 2, 2))

    def test_doe_rejects_an_undefined_evidence_kind(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "unsupported DOE kind"):
                module["run"](evidence_root=Path(directory), doe_kind="made-up")

    def test_successful_protocol_receipts_require_content_free_v2(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        receipts = [
            {
                "artifact_role": "host-observation/v2", "role": role,
                "thread_id": f"thread-{role}", "turn_id": f"turn-{role}",
                "turn_status": "completed", "host_version": "host-1.0",
                "model": "model-1", "timeout_seconds": 120.0,
                "requested_sandbox": "read-only",
            }
            for role in ("reviewer", "proposal")
        ]
        module["_assert_host_observation_receipts"](receipts)

        receipts[0]["raw_output"] = "must-not-persist"
        with self.assertRaisesRegex(AssertionError, "prohibited"):
            module["_assert_host_observation_receipts"](receipts)

    def test_quality_gate_requires_a_proposal_linked_to_the_safety_p0(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        resolved_findings = [
            {
                "finding_id": "F-001",
                "origin_severity": "P0",
                "effective_gate": "blocking",
                "category": "safety_and_governance",
                "subject_kind": "nl_artifact",
            }
        ]
        unrelated = {"proposal_result": {"proposals": [{"finding_id": "F-002"}]}}
        linked = {"proposal_result": {"proposals": [{"finding_id": "F-001"}]}}

        with self.assertRaisesRegex(AssertionError, "proposal linked"):
            module["_assert_quality_link"](resolved_findings, ["F-001"], unrelated)
        module["_assert_quality_link"](resolved_findings, ["F-001"], linked)
        with self.assertRaisesRegex(AssertionError, "identify"):
            module["_assert_quality_link"](resolved_findings, [], linked)

    def test_quality_failure_code_is_specific_and_does_not_export_assertion_text(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))

        self.assertEqual(
            module["_quality_failure_code"](AssertionError("quality DOE did not identify the fixture's P0 safety finding")),
            "p0-not-detected",
        )
        self.assertEqual(
            module["_quality_failure_code"](AssertionError("quality DOE did not create a proposal linked to the fixture's P0 safety finding")),
            "p0-proposal-not-linked",
        )
        self.assertEqual(
            module["_quality_failure_code"](AssertionError("untrusted text must not leave the temporary run")),
            "quality-gate-assertion-unclassified",
        )

    def test_quality_failure_code_survives_redaction_and_receipt_without_error_text(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        summary = {
            "status": "failed", "error_class": "AssertionError", "run_state": "TRIAGING", "run_outcome": "IN_PROGRESS",
            "artifact_kinds": ["integrity", "review", "integrity", "review"], "host_diagnostics": [],
            "fixture_unchanged": True, "target_writes": [], "doe_kind": "quality",
            "quality_failure_code": "p0-not-detected", "error": "private assertion detail",
        }

        redacted = module["_redact_failure_summary"](summary)
        receipt = module["_failure_receipt"](1, redacted, 100)

        self.assertEqual(redacted["quality_failure_code"], "p0-not-detected")
        self.assertEqual(receipt["summary"]["quality_failure_code"], "p0-not-detected")
        self.assertNotIn("private assertion detail", json.dumps(receipt))

    def test_quality_failure_receipt_keeps_only_whitelisted_reviewer_summary(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        summary = {
            "status": "failed", "error_class": "AssertionError", "run_state": "TRIAGING", "run_outcome": "IN_PROGRESS",
            "artifact_kinds": ["integrity", "review", "integrity", "review"], "host_diagnostics": [],
            "fixture_unchanged": True, "target_writes": [], "doe_kind": "quality",
            "quality_failure_code": "p0-not-detected",
            "reviewer_quality": [
                {
                    "stage": "review-quality", "result": "accepted", "finding_count": 2,
                    "severity_counts": {"P0": 0, "P1": 1, "P2": 1, "P3": 0, "leak": 99},
                    "gate_counts": {"blocking": 1, "needs-user-decision": 0, "recordable-risk": 1},
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
                        "secret-category": 99,
                    },
                    "has_safety_p0": False,
                    "statement": "private model finding text must not persist",
                    "finding_id": "F-secret-model-value",
                }
            ],
        }

        redacted = module["_redact_failure_summary"](summary)
        receipt = module["_failure_receipt"](1, redacted, 100)

        self.assertEqual(
            receipt["summary"]["reviewer_quality"],
            [
                {
                    "stage": "review-quality", "result": "accepted", "finding_count": 2,
                    "severity_counts": {"P0": 0, "P1": 1, "P2": 1, "P3": 0},
                    "gate_counts": {"blocking": 1, "needs-user-decision": 0, "recordable-risk": 1},
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
                    "has_safety_p0": False,
                }
            ],
        )
        rendered = json.dumps(receipt)
        self.assertNotIn("private model finding", rendered)
        self.assertNotIn("F-secret-model-value", rendered)
        self.assertNotIn("secret-category", rendered)

    def test_quality_failure_receipt_keeps_only_whitelisted_proposal_classification(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        summary = {
            "status": "failed", "error_class": "M8ReviewCoordinatorError", "run_state": "BLOCKED", "run_outcome": "BLOCKED",
            "artifact_kinds": ["integrity", "review", "integrity", "integrity"], "host_diagnostics": [],
            "fixture_unchanged": True, "target_writes": [], "doe_kind": "quality",
            "proposal_validation": [
                {"stage": "local-validation", "result": "accepted", "raw_output": "private proposal"},
                {
                    "stage": "ledger-ingestion", "result": "rejected",
                    "error_code": "references-unknown-finding",
                    "finding_id": "F-secret-model-value",
                },
            ],
        }

        redacted = module["_redact_failure_summary"](summary)
        receipt = module["_failure_receipt"](1, redacted, 100)

        self.assertEqual(
            receipt["summary"]["proposal_validation"],
            [
                {"stage": "local-validation", "result": "accepted"},
                {"stage": "ledger-ingestion", "result": "rejected", "error_code": "references-unknown-finding"},
            ],
        )
        rendered = json.dumps(receipt)
        self.assertNotIn("private proposal", rendered)
        self.assertNotIn("F-secret-model-value", rendered)

    def test_failure_receipt_keeps_only_whitelisted_host_timing(self) -> None:
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        summary = {
            "status": "failed", "error_class": "M8ReviewCoordinatorError", "run_state": "BLOCKED", "run_outcome": "BLOCKED",
            "artifact_kinds": ["integrity"], "fixture_unchanged": True, "target_writes": [], "doe_kind": "quality",
            "host_diagnostics": [{
                "artifact_role": "host-failure/v1", "phase": "reviewer", "error_class": "HostProtocolError",
                "host_attempt": {
                    "role": "reviewer", "thread_id": "thread-safe", "turn_id": "turn-safe", "turn_status": "unobserved-timeout",
                    "error_code": "client_timeout", "event_count": 3, "event_methods": ["turn/started"],
                    "host_version": "codex-cli 0.165.0", "model": "fake-model", "timeout_seconds": 120.0,
                    "effective_sandbox": "read-only",
                    "timing": {
                        "failure_stage": "awaiting_first_agent_message", "deadline_ms": 120000,
                        "initialized_ms": 4, "role_started_ms": 8, "thread_started_ms": 12,
                        "turn_started_ms": 16, "first_agent_message_ms": None, "terminal_ms": None,
                        "private": "must-not-persist",
                    },
                },
            }],
        }

        receipt = module["_failure_receipt"](1, module["_redact_failure_summary"](summary), 100)

        timing = receipt["failure_receipts"][0]["host_attempt"]["timing"]
        self.assertEqual(timing["failure_stage"], "awaiting_first_agent_message")
        self.assertNotIn("private", timing)
        self.assertNotIn("must-not-persist", json.dumps(receipt))

    def test_simulated_proposal_validation_failure_reaches_same_attempt_quality_receipt(self) -> None:
        """Exercise Coordinator -> capture -> redaction -> receipt without a model call."""
        module = runpy.run_path(str(ROOT / "scripts" / "run_m8_host_doe.py"))
        review = (ROOT / "fixtures" / "v5" / "review-result.json").read_text(encoding="utf-8")
        secret = "proposal-secret-must-not-persist"

        class FakeClient:
            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def run_read_only_role(self, *, role: str, **_: object) -> HostResult:
                raw_output = review if role == "reviewer" else json.dumps({"summary": secret})
                return HostResult(
                    role=role,
                    thread_id=f"fake-{role}-thread",
                    turn_id=f"fake-{role}-turn",
                    raw_output=raw_output,
                    events=(),
                    effective_sandbox="read-only",
                    host_version="fake-host-1.0",
                    model="fake-model",
                    timeout_seconds=1.0,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "quality-fixture.md"
            fixture = "# simulated quality fixture\n"
            artifact.write_text(fixture, encoding="utf-8")
            store = StateStore(root / "state" / "state.db")
            store.initialize()
            snapshot = module["_snapshot"](fixture)
            run_id = module["_ready_run"](store, snapshot)
            reviewer_capture = ReviewQualityCapture()
            proposal_capture = ProposalValidationCapture()
            coordinator = module["_QualityEvidenceCoordinator"](store, reviewer_capture, proposal_capture)
            coordinator.executor_factory = FakeClient

            with self.assertRaisesRegex(RuntimeError, "proposal host phase blocked") as caught:
                coordinator.run(request=HostExecutionRequest(run_id, artifact, snapshot))

            summary = module["_failure_summary"](
                store,
                run_id,
                artifact,
                fixture,
                "quality",
                caught.exception,
                reviewer_capture.snapshot(),
                proposal_capture.snapshot(),
            )
            receipt_name = module["_archive_failure_receipt"](
                root,
                1,
                module["_redact_failure_summary"](summary),
                100,
            )
            receipt = json.loads((root / receipt_name).read_text(encoding="utf-8"))

        self.assertEqual(
            receipt["summary"]["proposal_validation"],
            [{
                "stage": "local-validation",
                "result": "rejected",
                "error_code": "result-unexpected-or-missing-fields",
            }],
        )
        self.assertEqual(receipt["summary"]["target_writes"], [])
        self.assertTrue(receipt["summary"]["fixture_unchanged"])
        self.assertNotIn(secret, json.dumps(receipt))
