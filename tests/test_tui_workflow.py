from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from nlp_refinement.refine import project_identity, project_key
from nlp_refinement.review_adapter import ingest_review
from nlp_refinement.patch_proposal_adapter import (
    PatchProposalInputError,
    ingest_patch_proposal,
    validate_patch_proposal,
)
from nlp_refinement.state_store import StateStore
from nlp_refinement.tui_workflow import (
    SelectionInputError,
    SnapshotStaleError,
    build_proposal_handoff,
    find_resumable_batches,
    parse_selection,
    workflow_disposition,
)


ROOT = Path(__file__).resolve().parents[1]


def _run(run_id: str, key: str, snapshot: str) -> dict[str, object]:
    return {
        "schema_version": "5.0",
        "run_id": run_id,
        "project_key": key,
        "target_snapshot_hash": snapshot,
        "current_snapshot_hash": snapshot,
        "authorization_mode": "review-only",
        "state": "DRAFT",
        "outcome": "IN_PROGRESS",
        "revision": 0,
        "repair_cycle_count": 0,
        "max_repair_cycles": 3,
        "open_high_risk_assumptions": 0,
        "retention_policy_version": "5.0",
    }


class TuiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "project"
        self.root.mkdir()
        self.artifact = self.root / "AGENTS.md"
        self.artifact.write_text("# fixture artifact\n", encoding="utf-8")
        self.snapshot = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        self.store = StateStore(Path(self.temporary_directory.name) / "state.db")
        self.store.initialize()
        identity, _ = project_identity(self.root, None)
        self.project_key = project_key(identity)
        self.run_id = "run_aaaaaaaaaaaa"
        self.batch_id = "batch_aaaaaaaaaaaa"
        self.store.create_batch_with_ready_children(
            {"batch_id": self.batch_id, "project_key": self.project_key, "inventory_hash": "b" * 64},
            [
                (
                    _run(self.run_id, self.project_key, self.snapshot),
                    {
                        "batch_id": self.batch_id,
                        "relative_path": "AGENTS.md",
                        "project_identity": identity,
                        "identity_mode": "resolved-path",
                        "review_intent": "system-default-nl-artifact-review",
                    },
                    "AGENTS.md",
                )
            ],
            now=1_000,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _accept_review(self) -> dict[str, object]:
        review = json.loads((ROOT / "fixtures/v5/review-result.json").read_text(encoding="utf-8"))
        return ingest_review(
            self.store,
            self.run_id,
            review,
            self.snapshot,
            "caller-attested-redacted",
            now=1_001,
        )

    def test_parse_selection_accepts_all_indexes_ranges_and_cancel(self) -> None:
        candidates = ["AGENTS.md", "plugin.json", "workflow.yaml"]
        self.assertEqual(parse_selection("all", candidates), ("AGENTS.md", "plugin.json", "workflow.yaml"))
        self.assertEqual(parse_selection("1, 3", candidates), ("AGENTS.md", "workflow.yaml"))
        self.assertEqual(parse_selection("2-3", candidates), ("plugin.json", "workflow.yaml"))
        self.assertIsNone(parse_selection("cancel", candidates))
        with self.assertRaises(SelectionInputError):
            parse_selection("1,4", candidates)

    def test_ready_run_needs_review(self) -> None:
        run = self.store.get_run(self.run_id)
        self.assertEqual(workflow_disposition(run, [], []), "NEEDS_REVIEW")

    def test_accepted_review_builds_a_v6_proposal_handoff_from_open_findings(self) -> None:
        accepted = self._accept_review()

        handoff = build_proposal_handoff(self.store, self.root, self.run_id)

        self.assertEqual(handoff["run_id"], self.run_id)
        self.assertEqual(handoff["snapshot_hash"], self.snapshot)
        self.assertEqual(handoff["artifact_relative_path"], "AGENTS.md")
        self.assertEqual(handoff["review_artifact_id"], accepted["artifact_id"])
        self.assertEqual(handoff["open_finding_ids"], ["F-001"])
        self.assertEqual(handoff["proposal_schema"], "patch-proposal-result/v6")
        self.assertEqual(handoff["artifact_model_view"], "# fixture artifact\n")
        self.assertEqual([finding["finding_id"] for finding in handoff["validated_findings"]], ["F-001"])

    def test_handoff_stops_when_artifact_snapshot_has_changed(self) -> None:
        self._accept_review()
        self.artifact.write_text("# changed artifact\n", encoding="utf-8")

        with self.assertRaises(SnapshotStaleError):
            build_proposal_handoff(self.store, self.root, self.run_id)

    def test_v6_proposal_requires_exact_review_artifact_binding(self) -> None:
        proposal = json.loads((ROOT / "fixtures/v6/patch-proposal-result.json").read_text(encoding="utf-8"))
        validate_patch_proposal(proposal)
        proposal["proposals"][0]["causal_evidence_refs"] = ["r99999999-review"]
        with self.assertRaises(PatchProposalInputError):
            validate_patch_proposal(proposal)

    def test_v6_proposal_records_only_when_its_handoff_review_is_finding_evidence(self) -> None:
        accepted = self._accept_review()
        handoff = build_proposal_handoff(self.store, self.root, self.run_id)
        proposal = json.loads((ROOT / "fixtures/v6/patch-proposal-result.json").read_text(encoding="utf-8"))
        proposal["base_snapshot_hash"] = self.snapshot
        proposal["review_artifact_id"] = handoff["review_artifact_id"]
        proposal["proposals"][0]["causal_evidence_refs"] = [handoff["review_artifact_id"]]
        proposal["proposals"][0]["assessment"]["evidence_refs"] = [handoff["review_artifact_id"]]

        forged = json.loads(json.dumps(proposal))
        forged["review_artifact_id"] = "r99999999-review"
        forged["proposals"][0]["causal_evidence_refs"] = ["r99999999-review"]
        forged["proposals"][0]["assessment"]["evidence_refs"] = ["r99999999-review"]
        with self.assertRaises(PatchProposalInputError):
            ingest_patch_proposal(
                self.store,
                self.run_id,
                forged,
                self.snapshot,
                "caller-attested-redacted",
                now=1_002,
            )
        self.assertEqual(
            [artifact["artifact_id"] for artifact in self.store.list_artifacts(self.run_id)],
            [accepted["artifact_id"]],
        )

        recorded = ingest_patch_proposal(
            self.store,
            self.run_id,
            proposal,
            self.snapshot,
            "caller-attested-redacted",
            now=1_003,
        )
        self.assertIsNotNone(recorded["artifact_id"])
        self.assertEqual(workflow_disposition(
            self.store.get_run(self.run_id),
            self.store.list_findings(self.run_id),
            self.store.list_artifacts(self.run_id),
        ), "AWAITING_HUMAN")

    def test_resumable_lookup_is_project_scoped_and_requires_a_choice_when_ambiguous(self) -> None:
        self.assertEqual(find_resumable_batches(self.store, self.root), [self.batch_id])
        other_root = Path(self.temporary_directory.name) / "other-project"
        other_root.mkdir()
        other_identity, _ = project_identity(other_root, None)
        other_key = project_key(other_identity)
        self.store.create_batch_with_ready_children(
            {"batch_id": "batch_bbbbbbbbbbbb", "project_key": other_key, "inventory_hash": "c" * 64},
            [
                (
                    _run("run_bbbbbbbbbbbb", other_key, "d" * 64),
                    {
                        "batch_id": "batch_bbbbbbbbbbbb",
                        "relative_path": "OTHER.md",
                        "project_identity": other_identity,
                        "identity_mode": "resolved-path",
                        "review_intent": "system-default-nl-artifact-review",
                    },
                    "OTHER.md",
                )
            ],
            now=1_002,
        )
        self.assertEqual(find_resumable_batches(self.store, self.root), [self.batch_id])


if __name__ == "__main__":
    unittest.main()
