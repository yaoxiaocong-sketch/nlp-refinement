from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from nlp_refinement.host_client import HostAttempt, HostAttemptTiming, HostProtocolError, HostResult
from nlp_refinement.host_execution_request import HostExecutionRequest
from nlp_refinement.m8_review_coordinator import (
    M8ReviewCoordinator,
    M8ReviewCoordinatorError,
)
from nlp_refinement.state_store import LeaseConflict, StateStore


SNAPSHOT = "b" * 64


def snapshot_for(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeHost:
    def __init__(self, results: list[HostResult]) -> None:
        self.results = results
        self.calls: list[dict[str, object]] = []
        self.working_directory_snapshots: list[tuple[Path, dict[str, str]]] = []

    def __enter__(self) -> "FakeHost":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def run_read_only_role(self, **kwargs: object) -> HostResult:
        self.calls.append(kwargs)
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        self.working_directory_snapshots.append(
            (cwd, {entry.name: entry.read_text(encoding="utf-8") for entry in cwd.iterdir()})
        )
        return self.results.pop(0)


class M8ReviewCoordinatorTests(unittest.TestCase):
    def test_requires_user_redacted_copy_for_ambiguous_secret_before_starting_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "state" / "state.db")
            store.initialize()
            artifact = root / "skill.md"
            artifact.write_text("password: hunter2\n", encoding="utf-8")
            snapshot = snapshot_for(artifact)
            run_id = "run_1234567890ab"
            store.create_run({"schema_version": "5.0", "run_id": run_id, "project_key": "d" * 64, "target_snapshot_hash": snapshot, "current_snapshot_hash": snapshot, "authorization_mode": "review-only", "state": "DRAFT", "outcome": "IN_PROGRESS", "revision": 0, "repair_cycle_count": 0, "max_repair_cycles": 3, "open_high_risk_assumptions": 0, "retention_policy_version": "5.0"}, {"redacted_brief": "x"}, now=1)
            lease = store.acquire_lease(run_id, "test", now=1)
            store.transition(run_id, 0, lease["token"], "READY", "IN_PROGRESS", snapshot, now=1)
            store.release_lease(run_id, lease["token"], now=1)
            fake = FakeHost([])

            with self.assertRaisesRegex(M8ReviewCoordinatorError, "ambiguous-secret-assignment"):
                M8ReviewCoordinator(store, lambda: fake).run(
                    request=HostExecutionRequest(run_id, artifact, snapshot)
                )

            self.assertEqual(fake.calls, [])
            self.assertEqual(store.get_run(run_id)["state"], "READY")
            self.assertEqual(store.list_artifacts(run_id), [])

    def test_records_host_receipts_before_review_and_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            target = root / "target"
            target.mkdir()
            artifact = target / "skill.md"
            api_key = "sk-proj-abcdefghijklmnopqrstuvwxyz"
            artifact.write_text(f"# fixture\napi_key: {api_key}\n", encoding="utf-8")
            snapshot = snapshot_for(artifact)
            store = StateStore(state_dir / "state.db")
            store.initialize()
            run_id = "run_1234567890ab"
            store.create_run({"schema_version": "5.0", "run_id": run_id, "project_key": "d" * 64, "target_snapshot_hash": snapshot, "current_snapshot_hash": snapshot, "authorization_mode": "review-only", "state": "DRAFT", "outcome": "IN_PROGRESS", "revision": 0, "repair_cycle_count": 0, "max_repair_cycles": 3, "open_high_risk_assumptions": 0, "retention_policy_version": "5.0"}, {"redacted_brief": "x"}, now=1)
            lease = store.acquire_lease(run_id, "test", now=1)
            store.transition(run_id, 0, lease["token"], "READY", "IN_PROGRESS", snapshot, now=1)
            store.release_lease(run_id, lease["token"], now=1)
            review = {"schema_version": "5.0", "phase_name": "review", "reviewer_id": "r", "summary": "x", "findings": [{"fingerprint": "a" * 64, "origin_severity": "P1", "effective_gate": "blocking", "invariant_id": "I", "last_probe": "p", "subject_kind": "nl_artifact", "evidence_plane": "static", "category": "semantic_workflow", "evidence_confidence": "E2-direct", "impact_scope": "single-artifact", "claim_status": "direct-static", "statement": "x", "evidence": "x", "recommendation": "x"}]}
            proposal = {"schema_version": "5.0", "phase_name": "patch-proposal", "proposer_id": "p", "summary": "x", "base_snapshot_hash": snapshot, "no_target_write_attestation": True, "proposals": [{"finding_id": "F-001", "target_reference": "skill.md", "unified_diff": "--- a/skill.md\n+++ b/skill.md\n@@ -1 +1 @@\n-old\n+new\n", "rationale": "x", "target_invariants": ["I"], "causal_claim": "x", "causal_evidence_refs": ["r00000003-review"], "authorization_basis": "review-only-human-application", "verification_plan": "x", "regression_plan": "x", "known_risks": "x", "alternatives_considered": "x", "claims_not_yet_verified": "x", "assessment": {"assessor_id": "a", "evidence_refs": ["r00000003-review"], "scores": {"coverage": 20, "causal": 20, "scope": 15, "verification": 15, "regression": 10, "evidence": 10, "alternatives": 5, "clarity": 5}, "total_score": 100, "recommendation": "recommend", "runtime_effectiveness": "unverified"}}]}
            fake = FakeHost([
                HostResult(
                    "reviewer", "thr-review", "turn-review", json.dumps(review),
                    ({"method": "turn/completed", "params": {"untrusted_payload": "must-not-persist"}},),
                    "read-only",
                ),
                HostResult(
                    "proposal", "thr-proposal", "turn-proposal", json.dumps(proposal),
                    ({"method": "turn/completed", "params": {"untrusted_payload": "must-not-persist"}},),
                    "read-only",
                ),
            ])
            result = M8ReviewCoordinator(store, lambda: fake).run(
                request=HostExecutionRequest(run_id, artifact, snapshot)
            )
            self.assertEqual(result.new_finding_ids, ["F-001"])
            self.assertEqual(result.recommended_proposal_count, 1)
            self.assertEqual([call["role"] for call in fake.calls], ["reviewer", "proposal"])
            self.assertIn("lease-renewed", [event["event_type"] for event in store.list_events(run_id)])
            self.assertTrue(all(call["cwd"] != target.resolve() for call in fake.calls))
            self.assertEqual(
                [contents for _, contents in fake.working_directory_snapshots],
                [{"artifact.md": "# fixture\napi_key: <REDACTED:api-key:1>\n"}] * 2,
            )
            self.assertTrue(all(not cwd.exists() for cwd, _ in fake.working_directory_snapshots))
            self.assertIn("Classification coherence is mandatory", str(fake.calls[0]["prompt"]))
            self.assertNotIn(api_key, str(fake.calls[0]["prompt"]))
            self.assertIn("<REDACTED:api-key:1>", str(fake.calls[0]["prompt"]))
            self.assertIn("validated_review_context", str(fake.calls[1]["prompt"]))
            proposal_schema = fake.calls[1]["output_schema"]
            assert isinstance(proposal_schema, dict)
            proposal_item = proposal_schema["properties"]["proposals"]["items"]
            self.assertEqual(proposal_item["properties"]["causal_evidence_refs"]["items"], {"const": "r00000003-review"})
            self.assertEqual(
                proposal_item["properties"]["assessment"]["properties"]["evidence_refs"]["items"],
                {"const": "r00000003-review"},
            )
            artifacts = store.list_artifacts(run_id)
            self.assertEqual([item["kind"] for item in artifacts], ["integrity", "integrity", "review", "integrity", "review"])
            self.assertEqual(
                [item["redaction_status"] for item in artifacts],
                ["system-metadata", "system-metadata", "caller-attested-redacted", "system-metadata", "caller-attested-redacted"],
            )
            redaction_receipt = json.loads(artifacts[0]["body"])
            self.assertEqual(redaction_receipt, {"artifact_role": "model-input-redaction/v1", "detector_version": "1", "model_input_handling": "coordinator-redacted", "replacement_counts": {"api-key": 1}})
            self.assertNotIn(api_key, artifacts[0]["body"])
            self.assertIn("thr-review", artifacts[1]["body"])
            self.assertEqual(artifacts[1]["input_artifact_refs"], [artifacts[0]["artifact_id"]])
            self.assertIn("thr-proposal", artifacts[3]["body"])
            self.assertIn('"requested_sandbox":"read-only"', artifacts[1]["body"])
            receipt = json.loads(artifacts[1]["body"])
            self.assertEqual(receipt["artifact_role"], "host-observation/v2")
            self.assertEqual(receipt["model_input_handling"], "coordinator-redacted")
            self.assertNotIn("raw_output", receipt)
            self.assertNotIn("raw_output_sha256", receipt)
            self.assertNotIn("events", receipt)
            self.assertNotIn("must-not-persist", artifacts[1]["body"])
            self.assertEqual(artifact.read_text(encoding="utf-8"), f"# fixture\napi_key: {api_key}\n")

    def test_invalid_host_output_is_audited_and_blocks_until_explicit_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            artifact = root / "skill.md"
            artifact.write_text("# fixture\n", encoding="utf-8")
            snapshot = snapshot_for(artifact)
            store = StateStore(state_dir / "state.db")
            store.initialize()
            run_id = "run_1234567890ab"
            store.create_run({"schema_version": "5.0", "run_id": run_id, "project_key": "d" * 64, "target_snapshot_hash": snapshot, "current_snapshot_hash": snapshot, "authorization_mode": "review-only", "state": "DRAFT", "outcome": "IN_PROGRESS", "revision": 0, "repair_cycle_count": 0, "max_repair_cycles": 3, "open_high_risk_assumptions": 0, "retention_policy_version": "5.0"}, {"redacted_brief": "x"}, now=1)
            lease = store.acquire_lease(run_id, "test", now=1)
            store.transition(run_id, 0, lease["token"], "READY", "IN_PROGRESS", snapshot, now=1)
            store.release_lease(run_id, lease["token"], now=1)
            fake = FakeHost([HostResult("reviewer", "thr-review", "turn-review", "not-json", (), "read-only")])

            with self.assertRaises(M8ReviewCoordinatorError):
                M8ReviewCoordinator(store, lambda: fake).run(
                    request=HostExecutionRequest(run_id, artifact, snapshot)
                )

            run = store.get_run(run_id)
            self.assertEqual((run["state"], run["outcome"]), ("BLOCKED", "BLOCKED"))
            artifacts = store.list_artifacts(run_id)
            self.assertEqual([item["kind"] for item in artifacts], ["integrity", "integrity"])
            self.assertEqual(json.loads(artifacts[0]["body"])["model_input_handling"], "unchanged")
            self.assertNotIn("not-json", artifacts[0]["body"])
            self.assertIn("host-failure/v1", artifacts[1]["body"])
            self.assertEqual(artifact.read_text(encoding="utf-8"), "# fixture\n")

    def test_changed_artifact_stales_run_without_starting_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "state" / "state.db")
            store.initialize()
            artifact = root / "skill.md"
            artifact.write_text("# original\n", encoding="utf-8")
            snapshot = snapshot_for(artifact)
            run_id = "run_1234567890ab"
            store.create_run({"schema_version": "5.0", "run_id": run_id, "project_key": "d" * 64, "target_snapshot_hash": snapshot, "current_snapshot_hash": snapshot, "authorization_mode": "review-only", "state": "DRAFT", "outcome": "IN_PROGRESS", "revision": 0, "repair_cycle_count": 0, "max_repair_cycles": 3, "open_high_risk_assumptions": 0, "retention_policy_version": "5.0"}, {"redacted_brief": "x"}, now=1)
            lease = store.acquire_lease(run_id, "test", now=1)
            store.transition(run_id, 0, lease["token"], "READY", "IN_PROGRESS", snapshot, now=1)
            store.release_lease(run_id, lease["token"], now=1)
            artifact.write_text("# changed\n", encoding="utf-8")
            fake = FakeHost([])

            with self.assertRaisesRegex(M8ReviewCoordinatorError, "snapshot"):
                M8ReviewCoordinator(store, lambda: fake).run(
                    request=HostExecutionRequest(run_id, artifact, snapshot)
                )

            self.assertEqual(fake.calls, [])
            self.assertEqual(store.get_run(run_id)["state"], "SNAPSHOT_STALE")

    def test_active_run_lease_prevents_any_host_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "state" / "state.db")
            store.initialize()
            artifact = root / "skill.md"
            artifact.write_text("# fixture\n", encoding="utf-8")
            snapshot = snapshot_for(artifact)
            run_id = "run_1234567890ab"
            store.create_run({"schema_version": "5.0", "run_id": run_id, "project_key": "d" * 64, "target_snapshot_hash": snapshot, "current_snapshot_hash": snapshot, "authorization_mode": "review-only", "state": "DRAFT", "outcome": "IN_PROGRESS", "revision": 0, "repair_cycle_count": 0, "max_repair_cycles": 3, "open_high_risk_assumptions": 0, "retention_policy_version": "5.0"}, {"redacted_brief": "x"}, now=1)
            setup_lease = store.acquire_lease(run_id, "setup", now=1)
            store.transition(run_id, 0, setup_lease["token"], "READY", "IN_PROGRESS", snapshot, now=1)
            store.release_lease(run_id, setup_lease["token"], now=1)
            held_now = int(time.time())
            held = store.acquire_lease(run_id, "other-host", now=held_now)
            fake = FakeHost([])

            with self.assertRaises(LeaseConflict):
                M8ReviewCoordinator(store, lambda: fake).run(
                    request=HostExecutionRequest(run_id, artifact, snapshot)
                )

            self.assertEqual(fake.calls, [])
            self.assertEqual(store.get_run(run_id)["state"], "READY")
            store.release_lease(run_id, held["token"], now=held_now)

    def test_failed_host_attempt_receipt_keeps_bounded_attempt_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "state" / "state.db")
            store.initialize()
            artifact = root / "skill.md"
            artifact.write_text("# fixture\n", encoding="utf-8")
            run_id = "run_1234567890ab"
            store.create_run({"schema_version": "5.0", "run_id": run_id, "project_key": "d" * 64, "target_snapshot_hash": SNAPSHOT, "current_snapshot_hash": SNAPSHOT, "authorization_mode": "review-only", "state": "DRAFT", "outcome": "IN_PROGRESS", "revision": 0, "repair_cycle_count": 0, "max_repair_cycles": 3, "open_high_risk_assumptions": 0, "retention_policy_version": "5.0"}, {"redacted_brief": "x"}, now=1)
            lease = store.acquire_lease(run_id, "test", now=1)
            store.transition(run_id, 0, lease["token"], "READY", "IN_PROGRESS", SNAPSHOT, now=1)
            store.release_lease(run_id, lease["token"], now=1)

            coordinator = M8ReviewCoordinator(store, lambda: FakeHost([]))
            coordinator._block_failed_phase(
                run_id, SNAPSHOT, "proposal",
                HostProtocolError(
                    "fixture transport failure",
                    attempt=HostAttempt(
                        role="proposal", thread_id="thread-proposal", turn_id="turn-proposal", turn_status="failed",
                        error_code="fixture_failure", event_count=2, event_methods=("turn/started", "turn/completed"),
                        host_version="codex 0.fixture", model="fixture-model", timeout_seconds=12.0, effective_sandbox="read-only",
                        timing=HostAttemptTiming(
                            failure_stage="awaiting_terminal", deadline_ms=12_000, initialized_ms=4,
                            role_started_ms=5, thread_started_ms=6, turn_started_ms=7,
                            first_agent_message_ms=8, terminal_ms=None,
                        ),
                    ),
                ),
            )

            body = json.loads(store.list_artifacts(run_id)[0]["body"])
            self.assertEqual(body["artifact_role"], "host-failure/v1")
            self.assertEqual(body["phase"], "proposal")
            self.assertEqual(body["host_attempt"], {"role": "proposal", "thread_id": "thread-proposal", "turn_id": "turn-proposal", "turn_status": "failed", "error_code": "fixture_failure", "event_count": 2, "event_methods": ["turn/started", "turn/completed"], "host_version": "codex 0.fixture", "model": "fixture-model", "timeout_seconds": 12.0, "effective_sandbox": "read-only", "timing": {"failure_stage": "awaiting_terminal", "deadline_ms": 12000, "initialized_ms": 4, "role_started_ms": 5, "thread_started_ms": 6, "turn_started_ms": 7, "first_agent_message_ms": 8, "terminal_ms": None}})
