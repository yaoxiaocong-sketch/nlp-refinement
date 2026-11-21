from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nlp_refinement.state_store import LeaseConflict, StateStore

RUN_ID = "run_aaaaaaaaaaaa"
SNAPSHOT = "b" * 64


def run_payload() -> dict[str, object]:
    return {
        "schema_version": "tui-ledger/v1", "run_id": RUN_ID,
        "project_key": "a" * 64, "target_snapshot_hash": SNAPSHOT,
        "current_snapshot_hash": SNAPSHOT, "state": "DRAFT",
        "outcome": "IN_PROGRESS", "revision": 0,
        "retention_policy_version": "tui-ledger/v1",
    }


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temporary_directory.name) / "state.db")
        self.store.initialize()
        self.run = self.store.create_run(run_payload(), {"goal": "test"}, now=1_000)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_current_ledger_accepts_a_repair_free_run_shape(self) -> None:
        self.assertEqual(self.run["schema_version"], "tui-ledger/v1")
        self.assertNotIn("authorization_mode", self.run)
        self.assertNotIn("repair_cycle_count", self.run)

    def test_current_ledger_rejects_repair_fields(self) -> None:
        duplicate = run_payload() | {"run_id": "run_bbbbbbbbbbbb", "repair_cycle_count": 0}
        with self.assertRaises(ValueError):
            self.store.create_run(duplicate, {"goal": "test"}, now=1_001)

    def test_lease_excludes_a_competing_owner(self) -> None:
        first = self.store.acquire_lease(RUN_ID, "owner-one", now=1_000, ttl_seconds=60)
        with self.assertRaises(LeaseConflict):
            self.store.acquire_lease(RUN_ID, "owner-two", now=1_001, ttl_seconds=60)
        self.store.release_lease(RUN_ID, first["token"], now=1_002)

    def test_terminal_human_handoff_is_retained_then_purged(self) -> None:
        lease = self.store.acquire_lease(RUN_ID, "owner", now=1_000, ttl_seconds=60)
        ready = self.store.transition(RUN_ID, 0, lease["token"], "READY", "IN_PROGRESS", SNAPSHOT, now=1_001)
        triaged = self.store.transition(RUN_ID, ready["revision"], lease["token"], "TRIAGING", "IN_PROGRESS", SNAPSHOT, now=1_002)
        done = self.store.transition(RUN_ID, triaged["revision"], lease["token"], "AWAITING_HUMAN", "AWAITING_HUMAN", SNAPSHOT, now=1_003)
        self.store.release_lease(RUN_ID, lease["token"], now=1_004)
        preview = self.store.retention_preview(1_003 + 31 * 24 * 60 * 60)
        self.assertEqual(preview["actions"], [{"action": "purge-run", "run_id": RUN_ID}])
        self.assertEqual(done["state"], "AWAITING_HUMAN")
