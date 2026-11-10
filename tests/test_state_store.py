from __future__ import annotations

import tempfile
import unittest
import stat
import json
import sqlite3
from pathlib import Path

from nlp_refinement.state_store import (
    ImmutableFieldError,
    InvalidTransition,
    LeaseConflict,
    RetentionPlanStale,
    RevisionConflict,
    SensitiveArtifactError,
    StateStore,
)


RUN_ID = "run_aaaaaaaaaaaa"
PROJECT_KEY = "a" * 64
SNAPSHOT = "b" * 64


def run_payload() -> dict[str, object]:
    return {
        "schema_version": "5.0",
        "run_id": RUN_ID,
        "project_key": PROJECT_KEY,
        "target_snapshot_hash": SNAPSHOT,
        "current_snapshot_hash": SNAPSHOT,
        "authorization_mode": "bounded-repair",
        "state": "DRAFT",
        "outcome": "IN_PROGRESS",
        "revision": 0,
        "repair_cycle_count": 0,
        "max_repair_cycles": 3,
        "open_high_risk_assumptions": 0,
        "retention_policy_version": "5.0",
    }


def finding_payload() -> dict[str, object]:
    return {
        "schema_version": "5.0",
        "finding_id": "F-001",
        "run_id": RUN_ID,
        "fingerprint": "c" * 64,
        "origin_severity": "P1",
        "effective_gate": "blocking",
        "status": "open",
        "invariant_id": "I-LEASE",
        "evidence_refs": ["r00000001-review"],
        "repeat_count": 0,
        "last_probe": "lease-fixture",
        "subject_kind": "nl_artifact",
        "evidence_plane": "static",
        "category": "semantic_workflow",
                "evidence_confidence": "E2-direct", "impact_scope": "single-artifact", "claim_status": "direct-static",
        "accepted_risk_decision_ref": "none",
    }


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.db")
        self.store.initialize()
        self.run = self.store.create_run(run_payload(), immutable_anchor={"goal": "test"}, now=1_000)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def acquire(self, owner: str = "session-a") -> dict[str, object]:
        return self.store.acquire_lease(RUN_ID, owner, now=1_000, ttl_seconds=60)

    def test_create_run_persists_anchor_and_initial_revision(self) -> None:
        stored = self.store.get_run(RUN_ID)
        self.assertEqual(stored["revision"], 0)
        self.assertEqual(stored["state"], "DRAFT")
        self.assertEqual(stored["immutable_anchor"], {"goal": "test"})

    def test_new_store_instance_reads_persisted_run(self) -> None:
        reopened = StateStore(Path(self.temp_dir.name) / "state.db")
        stored = reopened.get_run(RUN_ID)
        self.assertEqual(stored["run_id"], RUN_ID)
        self.assertEqual(stored["immutable_anchor"], {"goal": "test"})

    def test_initialize_rejects_a_v4_ledger_without_rewriting_it(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-v4.db"
        legacy_run = run_payload() | {"schema_version": "4.0", "retention_policy_version": "4.0"}
        with sqlite3.connect(legacy_path) as connection:
            connection.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, run_json TEXT NOT NULL)")
            connection.execute(
                "INSERT INTO runs (run_id, run_json) VALUES (?, ?)",
                (RUN_ID, json.dumps(legacy_run, sort_keys=True)),
            )
        legacy_path.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "legacy ledger data"):
            StateStore(legacy_path).initialize()

        with sqlite3.connect(legacy_path) as connection:
            persisted = json.loads(connection.execute("SELECT run_json FROM runs WHERE run_id = ?", (RUN_ID,)).fetchone()[0])
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(persisted["schema_version"], "4.0")
        self.assertEqual(tables, {"runs"})

    def test_initialize_rejects_a_v4_archive_only_ledger_without_rewriting_it(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-v4-archive.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                "CREATE TABLE archived_runs (run_id TEXT PRIMARY KEY, archived_at INTEGER NOT NULL, purge_at INTEGER NOT NULL, archive_json TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO archived_runs VALUES (?, ?, ?, ?)",
                (RUN_ID, 1, 2, "{}"),
            )
        legacy_path.chmod(0o600)

        with self.assertRaisesRegex(RuntimeError, "legacy ledger data"):
            StateStore(legacy_path).initialize()

        with sqlite3.connect(legacy_path) as connection:
            rows = connection.execute("SELECT run_id FROM archived_runs").fetchall()
        self.assertEqual(rows, [(RUN_ID,)])

    def test_database_directory_and_file_are_owner_only(self) -> None:
        self.assertEqual(stat.S_IMODE(self.store.database_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.database_path.stat().st_mode), 0o600)

    def test_existing_non_private_state_root_is_rejected_without_changing_it(self) -> None:
        unsafe_root = Path(self.temp_dir.name) / "unsafe-state"
        unsafe_root.mkdir(mode=0o755)
        unsafe_root.chmod(0o755)
        with self.assertRaises(PermissionError):
            StateStore(unsafe_root / "state.db").initialize()
        self.assertEqual(stat.S_IMODE(unsafe_root.stat().st_mode), 0o755)

    def test_create_run_rejects_m0_invalid_repair_limit(self) -> None:
        invalid = run_payload() | {"run_id": "run_bbbbbbbbbbbb", "max_repair_cycles": 6}
        with self.assertRaises(ValueError):
            self.store.create_run(invalid, immutable_anchor={"goal": "invalid"}, now=1_001)

    def test_active_lease_rejects_second_owner_and_expired_lease_can_be_reclaimed(self) -> None:
        first = self.acquire()
        with self.assertRaises(LeaseConflict):
            self.store.acquire_lease(RUN_ID, "session-b", now=1_001, ttl_seconds=60)
        replacement = self.store.acquire_lease(RUN_ID, "session-b", now=1_061, ttl_seconds=60)
        self.assertNotEqual(first["token"], replacement["token"])
        self.assertEqual(replacement["owner_session_id"], "session-b")

    def test_artifact_ids_are_monotonic_and_append_advances_revision(self) -> None:
        lease = self.acquire()
        first = self.store.append_artifact(
            RUN_ID,
            expected_revision=0,
            lease_token=lease["token"],
            kind="review",
            actor="reviewer",
            body="first review",
            now=1_001,
        )
        second = self.store.append_artifact(
            RUN_ID,
            expected_revision=1,
            lease_token=lease["token"],
            kind="verification",
            actor="verifier",
            body="second review",
            input_artifact_refs=[first["artifact_id"]],
            now=1_002,
        )
        self.assertEqual(first["artifact_id"], "r00000001-review")
        self.assertEqual(second["artifact_id"], "r00000002-verification")
        self.assertEqual(self.store.get_run(RUN_ID)["revision"], 2)

    def test_stale_revision_cannot_append_artifact(self) -> None:
        lease = self.acquire()
        self.store.append_artifact(RUN_ID, 0, lease["token"], "review", "reviewer", "body", now=1_001)
        with self.assertRaises(RevisionConflict):
            self.store.append_artifact(RUN_ID, 0, lease["token"], "review", "reviewer", "body", now=1_002)

    def test_finding_origin_severity_cannot_change(self) -> None:
        lease = self.acquire()
        self.store.append_artifact(RUN_ID, 0, lease["token"], "review", "reviewer", "body", now=1_001)
        recorded = self.store.record_finding(RUN_ID, 1, lease["token"], finding_payload(), now=1_002)
        changed = finding_payload() | {"origin_severity": "P2", "effective_gate": "needs-user-decision", "status": "verified"}
        with self.assertRaises(ImmutableFieldError):
            self.store.update_finding(RUN_ID, recorded["revision"], lease["token"], changed, now=1_003)

    def test_finding_gate_cannot_be_silently_weakened(self) -> None:
        lease = self.acquire()
        self.store.append_artifact(RUN_ID, 0, lease["token"], "review", "reviewer", "body", now=1_001)
        recorded = self.store.record_finding(RUN_ID, 1, lease["token"], finding_payload(), now=1_002)
        weakened = finding_payload() | {"origin_severity": "P2", "effective_gate": "recordable-risk", "status": "verified"}
        with self.assertRaises(ImmutableFieldError):
            self.store.update_finding(RUN_ID, recorded["revision"], lease["token"], weakened, now=1_003)

    def test_accepted_risk_requires_a_user_decision_artifact(self) -> None:
        lease = self.acquire()
        review = self.store.append_artifact(RUN_ID, 0, lease["token"], "review", "reviewer", "body", now=1_001)
        accepted = finding_payload() | {
            "origin_severity": "P2",
            "effective_gate": "needs-user-decision",
            "status": "accepted-risk",
            "evidence_refs": [review["artifact_id"]],
            "accepted_risk_decision_ref": review["artifact_id"],
        }
        with self.assertRaises(ValueError):
            self.store.record_finding(RUN_ID, 1, lease["token"], accepted, now=1_002)

        decision = self.store.record_user_decision(RUN_ID, 1, lease["token"], "accept F-001", now=1_003)
        accepted["accepted_risk_decision_ref"] = decision["artifact_id"]
        recorded = self.store.record_finding(RUN_ID, 2, lease["token"], accepted, now=1_004)
        self.assertEqual(recorded["finding_id"], "F-001")

    def test_state_store_rejects_unknown_finding_status(self) -> None:
        lease = self.acquire()
        self.store.append_artifact(RUN_ID, 0, lease["token"], "review", "reviewer", "body", now=1_001)
        invalid = finding_payload() | {"status": "made-up-label"}
        with self.assertRaises(ValueError):
            self.store.record_finding(RUN_ID, 1, lease["token"], invalid, now=1_002)

    def test_state_store_rejects_unexpected_finding_fields(self) -> None:
        lease = self.acquire()
        artifact = self.store.append_artifact(RUN_ID, 0, lease["token"], "review", "reviewer", "body", now=1_001)
        invalid = finding_payload() | {
            "evidence_refs": [artifact["artifact_id"]],
            "not_in_v5_schema": "must not persist",
        }

        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            self.store.record_finding(RUN_ID, 1, lease["token"], invalid, now=1_002)

    def test_user_decision_creates_user_owned_decision_artifact(self) -> None:
        lease = self.acquire()
        decision = self.store.record_user_decision(RUN_ID, 0, lease["token"], "accept F-003", now=1_001)
        self.assertEqual(decision["kind"], "decision")
        self.assertEqual(decision["actor"], "user")

    def test_state_store_rejects_known_sensitive_artifact_status(self) -> None:
        lease = self.acquire()
        with self.assertRaises(ValueError):
            self.store.append_artifact(
                RUN_ID,
                0,
                lease["token"],
                "review",
                "reviewer",
                "secret body",
                redaction_status="rejected-sensitive",
                now=1_001,
            )

    def test_caller_attested_content_with_a_likely_secret_is_not_persisted(self) -> None:
        lease = self.acquire()
        with self.assertRaises(SensitiveArtifactError):
            self.store.append_artifact(
                RUN_ID,
                0,
                lease["token"],
                "review",
                "reviewer",
                "model output ghp_abcdefghijklmnopqrstuvwx must not persist",
                redaction_status="caller-attested-redacted",
                now=1_001,
            )
        self.assertEqual(self.store.list_artifacts(RUN_ID), [])
        self.assertEqual(self.store.get_run(RUN_ID)["revision"], 0)

    def test_retention_purges_complete_terminal_run_after_30_days(self) -> None:
        lease = self.acquire()
        self.store.append_artifact(
            RUN_ID, 0, lease["token"], "review", "reviewer", "private review body",
            redaction_status="caller-attested-redacted", now=1_001,
        )
        self.store.release_lease(RUN_ID, lease["token"], now=1_001)
        with sqlite3.connect(self.store.database_path) as connection:
            connection.execute(
                "UPDATE runs SET state = ?, outcome = ?, updated_at = ? WHERE run_id = ?",
                ("PASS", "PASS", 10, RUN_ID),
            )

        purge_at = 10 + 31 * 24 * 60 * 60
        preview = self.store.retention_preview(purge_at)
        self.assertEqual(preview["actions"], [{"action": "purge-run", "run_id": RUN_ID}])
        self.store.apply_retention(str(preview["plan_hash"]), purge_at + 5)
        with self.assertRaises(KeyError):
            self.store.get_run(RUN_ID)
        self.assertEqual(self.store.list_artifacts(RUN_ID), [])

    def test_retention_plan_requires_an_exact_current_confirmation(self) -> None:
        preview = self.store.retention_preview(1_000 + 14 * 24 * 60 * 60)
        self.store.create_run(run_payload() | {"run_id": "run_bbbbbbbbbbbb"}, immutable_anchor={"goal": "other"}, now=1_000)
        with self.assertRaises(RetentionPlanStale):
            self.store.apply_retention(str(preview["plan_hash"]), 1_000 + 14 * 24 * 60 * 60)

    def test_invalid_transition_is_rejected(self) -> None:
        lease = self.acquire()
        with self.assertRaises(InvalidTransition):
            self.store.transition(RUN_ID, 0, lease["token"], "PASS", "PASS", observed_snapshot_hash=SNAPSHOT, now=1_001)

    def test_review_phase_rolls_back_all_intermediate_writes_on_invalid_plan(self) -> None:
        lease = self.acquire()
        ready = self.store.transition(RUN_ID, 0, lease["token"], "READY", "IN_PROGRESS", SNAPSHOT, now=1_001)
        with self.assertRaises(ValueError):
            self.store.apply_review_phase(
                RUN_ID,
                expected_revision=ready["revision"],
                lease_token=lease["token"],
                observed_snapshot_hash=SNAPSHOT,
                review_body="review body",
                redaction_status="caller-attested-redacted",
                planned_findings=[{"action": "not-a-review-action", "finding": {}}],
                now=1_002,
            )
        recovered = self.store.get_run(RUN_ID)
        self.assertEqual((recovered["state"], recovered["revision"]), ("READY", 1))
        self.assertEqual(self.store.list_artifacts(RUN_ID), [])

    def test_user_decision_pause_can_resume_triage(self) -> None:
        lease = self.acquire()
        ready = self.store.transition(RUN_ID, 0, lease["token"], "READY", "IN_PROGRESS", SNAPSHOT, now=1_001)
        reviewing = self.store.transition(RUN_ID, ready["revision"], lease["token"], "REVIEWING", "IN_PROGRESS", SNAPSHOT, now=1_002)
        triaging = self.store.transition(RUN_ID, reviewing["revision"], lease["token"], "TRIAGING", "IN_PROGRESS", SNAPSHOT, now=1_003)
        paused = self.store.transition(RUN_ID, triaging["revision"], lease["token"], "NEEDS_USER_DECISION", "NEEDS_USER_DECISION", SNAPSHOT, now=1_004)
        resumed = self.store.transition(RUN_ID, paused["revision"], lease["token"], "TRIAGING", "IN_PROGRESS", SNAPSHOT, now=1_005)
        self.assertEqual(resumed["state"], "TRIAGING")
        self.assertEqual(resumed["outcome"], "IN_PROGRESS")

    def test_blocked_run_can_resume_at_ready(self) -> None:
        lease = self.acquire()
        blocked = self.store.transition(RUN_ID, 0, lease["token"], "BLOCKED", "BLOCKED", SNAPSHOT, now=1_001)
        resumed = self.store.transition(RUN_ID, blocked["revision"], lease["token"], "READY", "IN_PROGRESS", SNAPSHOT, now=1_002)
        self.assertEqual(resumed["state"], "READY")
        self.assertEqual(resumed["outcome"], "IN_PROGRESS")

    def test_snapshot_drift_transitions_to_snapshot_stale_without_merge(self) -> None:
        lease = self.acquire()
        ready = self.store.transition(RUN_ID, 0, lease["token"], "READY", "IN_PROGRESS", observed_snapshot_hash=SNAPSHOT, now=1_001)
        drifted = self.store.transition(
            RUN_ID,
            ready["revision"],
            lease["token"],
            "REVIEWING",
            "IN_PROGRESS",
            observed_snapshot_hash="d" * 64,
            now=1_002,
        )
        self.assertEqual(drifted["state"], "SNAPSHOT_STALE")
        self.assertEqual(drifted["outcome"], "SNAPSHOT_STALE")
        with self.assertRaises(InvalidTransition):
            self.store.transition(
                RUN_ID,
                drifted["revision"],
                lease["token"],
                "BLOCKED",
                "BLOCKED",
                observed_snapshot_hash="d" * 64,
                now=1_003,
            )

    def test_run_created_event_records_anchor_hash_not_anchor_body(self) -> None:
        events = self.store.list_events(RUN_ID)
        self.assertIn("immutable_anchor_hash", events[0]["payload"])
        self.assertNotIn("immutable_anchor", events[0]["payload"])


if __name__ == "__main__":
    unittest.main()
