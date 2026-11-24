from __future__ import annotations

import hashlib
import json
import os
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
    SnapshotStaleError,
    WorkflowStateError,
    build_child_bootstrap,
    build_child_dispatch_receipt,
    build_child_contract,
    build_proposal_handoff,
    build_proposal_assessment_handoff,
    build_reviewer_handoff,
    compose_assessed_v6_proposal,
    create_tui_transient_root,
    find_resumable_locators,
    find_resumable_batches,
    proposal_draft_hash,
    validate_proposal_assessment,
    validate_proposal_draft,
    validate_tui_transient_file,
    workflow_disposition,
)


ROOT = Path(__file__).resolve().parents[1]


def _run(run_id: str, key: str, snapshot: str) -> dict[str, object]:
    return {
        "schema_version": "tui-ledger/v1",
        "run_id": run_id,
        "project_key": key,
        "target_snapshot_hash": snapshot,
        "current_snapshot_hash": snapshot,
        "state": "DRAFT",
        "outcome": "IN_PROGRESS",
        "revision": 0,
        "retention_policy_version": "tui-ledger/v1",
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

    def test_child_contracts_are_closed_and_derived_from_the_phase_validators(self) -> None:
        proposer = build_child_contract("proposer")
        self.assertEqual(proposer["contract_version"], "tui-child-contract/v6")
        self.assertEqual(proposer["output_schema"], "proposal-draft/v1")
        self.assertEqual(
            set(proposer["top_level_fields"]),
            {
                "schema_version", "phase_name", "proposer_id", "summary",
                "base_snapshot_hash", "review_artifact_id", "no_target_write_attestation",
                "proposals",
            },
        )
        self.assertEqual(
            set(proposer["collection_item_fields"]["proposals"]),
            {
                "finding_id", "target_reference", "unified_diff", "rationale",
                "target_invariants", "causal_claim", "causal_evidence_refs",
                "authorization_basis", "verification_plan", "regression_plan",
                "known_risks", "alternatives_considered", "claims_not_yet_verified",
            },
        )
        self.assertIn("required_target_invariant_by_finding", proposer["semantic_requirements"][0])
        self.assertEqual(
            proposer["top_level_literal_fields"],
            {
                "schema_version": "proposal-draft/v1",
                "phase_name": "proposal-draft",
                "no_target_write_attestation": True,
            },
        )

        self.assertEqual(
            proposer["field_constraints"]["proposals"],
            {
                "string_fields": [
                    "alternatives_considered", "causal_claim", "claims_not_yet_verified",
                    "known_risks", "rationale", "regression_plan", "target_reference",
                    "unified_diff", "verification_plan",
                ],
                "nonempty_string_array_fields": ["causal_evidence_refs", "target_invariants"],
                "literal_fields": {"authorization_basis": "review-only-human-application"},
            },
        )
        self.assertEqual(
            proposer["unified_diff_requirements"],
            {
                "nul_free": True,
                "ends_with_newline": True,
                "paired_file_headers": True,
                "at_least_one_hunk_per_file": True,
                "hunk_line_count_rule": (
                    "For each @@ -old_start,old_count +new_start,new_count @@ header, "
                    "old_count equals the number of following hunk lines beginning with "
                    "a space or '-', and new_count equals the number beginning with a space or '+'."
                ),
            },
        )
        assessor = build_child_contract("proposal-assessor")
        self.assertEqual(assessor["output_schema"], "proposal-assessment/v1")
        self.assertEqual(
            assessor["top_level_literal_fields"],
            {"schema_version": "proposal-assessment/v1", "phase_name": "proposal-assessment"},
        )
        self.assertEqual(
            assessor["field_constraints"]["assessments"],
            {
                "nonempty_string_fields": ["finding_id", "assessor_id"],
                "nonempty_string_array_fields": ["evidence_refs"],
                "literal_fields": {"runtime_effectiveness": "unverified"},
                "enum_fields": {
                    "recommendation": ["recommend", "needs-user-decision", "not-recommended"],
                },
                "same_as_top_level_fields": {"assessor_id": "assessor_id"},
            },
        )
        self.assertIn("draft_sha256", " ".join(assessor["semantic_requirements"]))
        reviewer = build_child_contract("reviewer")
        self.assertEqual(reviewer["output_schema"], "review-result/v5")
        self.assertIn("findings", reviewer["collection_item_fields"])
        review_schema = json.loads(
            (ROOT / "nlp_refinement/resources/schemas/v5/review-result.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(reviewer["top_level_fields"]), set(review_schema["properties"]))
        self.assertEqual(
            set(reviewer["collection_item_fields"]["findings"]),
            set(review_schema["properties"]["findings"]["items"]["properties"]),
        )
        self.assertEqual(
            reviewer["top_level_literal_fields"],
            {"schema_version": "5.0", "phase_name": "review"},
        )
        constraints = reviewer["field_constraints"]["findings"]
        self.assertEqual(constraints["pattern_fields"]["fingerprint"], "[a-f0-9]{64}")
        self.assertEqual(
            set(constraints["enum_fields"]),
            {
                "origin_severity", "effective_gate", "subject_kind", "evidence_plane",
                "category", "evidence_confidence", "impact_scope", "claim_status",
            },
        )
        self.assertEqual(
            constraints["classification_by_subject_kind"]["nl_artifact"]["allowed_categories"],
            [
                "format_and_encoding",
                "layout_and_discovery",
                "reference_integrity",
                "safety_and_governance",
                "schema_contract",
                "semantic_workflow",
                "verification_design",
            ],
        )
        self.assertNotIn(
            "safety_behavior",
            constraints["classification_by_subject_kind"]["nl_artifact"]["allowed_categories"],
        )
        self.assertEqual(
            constraints["classification_by_subject_kind"]["nl_host_boundary"]["allowed_categories"],
            [
                "instruction_adherence",
                "load_and_discovery",
                "outcome_verification",
                "safety_behavior",
                "state_behavior",
            ],
        )
        self.assertIn("P0 requires effective_gate blocking", " ".join(reviewer["semantic_requirements"]))
        with self.assertRaisesRegex(WorkflowStateError, "unsupported child role"):
            build_child_contract("repairer")

    def test_patch_proposal_rejects_retired_v5_schema(self) -> None:
        proposal = json.loads((ROOT / "fixtures/v6/patch-proposal-result.json").read_text(encoding="utf-8"))
        proposal["schema_version"] = "5.0"
        del proposal["review_artifact_id"]
        with self.assertRaises(PatchProposalInputError):
            validate_patch_proposal(proposal)

    def test_bootstrap_is_bounded_and_retrieves_the_complete_contract_and_handoff_read_only(self) -> None:
        reviewer_handoff = build_reviewer_handoff(self.store, self.root, self.run_id)
        self.assertEqual(reviewer_handoff["artifact_relative_path"], "AGENTS.md")

        reviewer = build_child_bootstrap(
            role="reviewer",
            executable=Path("/bin/echo"),
            state_root=self.store.database_path.parent,
            project_root=self.root,
            run_id=self.run_id,
        )
        self.assertLessEqual(reviewer["prompt_utf8_bytes"], 1_000)
        self.assertEqual(reviewer["prompt_sha256"], hashlib.sha256(reviewer["prompt"].encode("utf-8")).hexdigest())
        self.assertIn("tui-child-contract --role reviewer", reviewer["prompt"])
        self.assertIn("tui-review-handoff", reviewer["prompt"])

        self._accept_review()
        proposer = build_child_bootstrap(
            role="proposer",
            executable=Path("/bin/echo"),
            state_root=self.store.database_path.parent,
            project_root=self.root,
            run_id=self.run_id,
        )
        self.assertLessEqual(proposer["prompt_utf8_bytes"], 1_000)
        self.assertIn("tui-proposal-handoff", proposer["prompt"])
        self.assertIn(build_child_contract("proposer")["contract_sha256"], proposer["prompt"])

        # The assessor adds an absolute transient-draft path to its second
        # read-only command. Exercise the long absolute executable, state,
        # target, and system-temporary paths used by isolated wheel acceptance.
        long_root = self.root.parent / "nlp-refinement-0.12.1-acceptance" / "fixture-project"
        long_root.mkdir(parents=True)
        long_state_root = self.root.parent / "nlp-refinement-0.12.1-acceptance" / "state"
        long_state_root.mkdir(parents=True)
        long_executable = self.root.parent / "nlp-refinement-0.12.1-acceptance" / "venv" / "bin" / "nlp-refinement"
        long_executable.parent.mkdir(parents=True)
        long_executable.write_text("fixture", encoding="utf-8")
        transient_root = create_tui_transient_root(long_root, "run_" + ("r" * 32))
        draft_file = transient_root / "proposal-draft.json"
        draft_file.write_text("{}", encoding="utf-8")
        assessor = build_child_bootstrap(
            role="proposal-assessor",
            executable=long_executable,
            state_root=long_state_root,
            project_root=long_root,
            run_id="run_" + ("r" * 32),
            proposal_draft_file=draft_file,
            transient_root=transient_root,
        )
        self.assertLessEqual(assessor["prompt_utf8_bytes"], 1_000)
        self.assertIn("tui-proposal-draft-handoff", assessor["prompt"])

        for bootstrap in (reviewer, proposer, assessor):
            receipt = build_child_dispatch_receipt(
                bootstrap, submitted_prompt=str(bootstrap["prompt"]), child_thread_id="thread-fixture",
            )
            self.assertEqual(receipt["role"], bootstrap["role"])
            self.assertEqual(receipt["prompt_sha256"], bootstrap["prompt_sha256"])
            self.assertEqual(receipt["observed_cwd"], "unverified")
            with self.assertRaisesRegex(WorkflowStateError, "does not match"):
                build_child_dispatch_receipt(
                    bootstrap, submitted_prompt=str(bootstrap["prompt"]) + " ",
                    child_thread_id="thread-fixture",
                )

    def test_tui_transient_files_must_be_private_system_temporary_files_outside_project_root(self) -> None:
        transient_root = create_tui_transient_root(self.root, self.run_id)
        accepted = transient_root / "review-result.json"
        accepted.write_text("{}", encoding="utf-8")
        self.assertEqual(
            validate_tui_transient_file(
                project_root=self.root,
                transient_root=transient_root,
                candidate=accepted,
                run_id=self.run_id,
                kind="review result",
            ),
            accepted.resolve(strict=True),
        )

        target_file = self.root / ".reviewer-output.json"
        target_file.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowStateError, "outside the project root"):
            validate_tui_transient_file(
                project_root=self.root,
                transient_root=self.root,
                candidate=target_file,
                run_id=self.run_id,
                kind="review result",
            )

        hard_link = transient_root / "hard-linked-review-result.json"
        os.link(target_file, hard_link)
        with self.assertRaisesRegex(WorkflowStateError, "hard-linked"):
            validate_tui_transient_file(
                project_root=self.root,
                transient_root=transient_root,
                candidate=hard_link,
                run_id=self.run_id,
                kind="review result",
            )

    def test_skill_persists_a_changed_snapshot_before_any_proposal_child(self) -> None:
        skill = (ROOT / ".agents/skills/nlp-review-propose/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("resume --run-id", skill)
        self.assertIn("SNAPSHOT_STALE", skill)
        self.assertIn("must not build ProposalHandoff", skill)
        self.assertIn("proposer for that run", skill)

    def test_skill_dispatches_generic_children_with_cli_generated_contracts(self) -> None:
        skill = (ROOT / ".agents/skills/nlp-review-propose/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("tui-child-contract --role reviewer", skill)
        self.assertIn("tui-child-contract --role proposer", skill)
        self.assertIn("tui-child-contract --role proposal-assessor", skill)
        self.assertIn("tui-child-bootstrap", skill)
        self.assertIn("prompt_sha256", skill)
        self.assertIn("tui-create-transient-root", skill)
        self.assertIn("tui-record-review", skill)
        self.assertIn("tui-cleanup-transient-root", skill)
        self.assertIn("Do not use root or child cwd as location authority", skill)
        self.assertIn("cwd: unverified", skill)
        self.assertIn("Do not block the workflow solely because cwd is unobservable", skill)
        self.assertIn("create_thread", skill)
        self.assertIn("tui-child-dispatch-receipt/v1", skill)
        self.assertIn("tui-record-child-dispatch-receipt", skill)
        self.assertIn("tui-child-dispatch-receipts", skill)
        self.assertIn("root-attested protocol receipt", skill)
        self.assertIn("do not accept the child output", skill)
        self.assertIn("not a\n   host-enforced interception", skill)
        self.assertIn("required_target_invariant_by_finding", skill)
        self.assertIn("generic TUI child context", skill)
        self.assertIn("`.toml` role file was loaded", skill)
        self.assertIn("policy-observe", skill)
        self.assertIn("capability-review status", skill)
        self.assertIn("$nlp-review-propose maintenance", skill)
        self.assertIn("no selection arguments", skill)
        self.assertIn("not decoded into a model view, truncated, transcoded, or modified", skill)
        self.assertIn("未发现可审查的 NL 工件。", skill)
        self.assertIn("Do not display EXCLUDED entries in the normal TUI flow", skill)
        self.assertNotIn("show the excluded summary", skill)
        self.assertIn("scan <project-root>", skill)
        self.assertIn("batch-create <project-root>", skill)
        self.assertIn("tui-resume-locators", skill)
        self.assertIn("tui-resume-candidates <project-root>", skill)
        self.assertIn("host/source worktree\n   is never target-location authority", skill)
        self.assertIn("Do not\n   use the unrelated legacy `start --project-dir`", skill)

    def test_ready_run_needs_review(self) -> None:
        run = self.store.get_run(self.run_id)
        self.assertEqual(workflow_disposition(run, [], []), "NEEDS_REVIEW")

    def test_accepted_review_builds_a_draft_and_final_proposal_handoff_from_open_findings(self) -> None:
        accepted = self._accept_review()

        handoff = build_proposal_handoff(self.store, self.root, self.run_id)

        self.assertEqual(handoff["run_id"], self.run_id)
        self.assertEqual(handoff["snapshot_hash"], self.snapshot)
        self.assertEqual(handoff["artifact_relative_path"], "AGENTS.md")
        self.assertEqual(handoff["review_artifact_id"], accepted["artifact_id"])
        self.assertEqual(handoff["open_finding_ids"], ["F-001"])
        self.assertEqual(handoff["proposal_draft_schema"], "proposal-draft/v1")
        self.assertEqual(handoff["final_proposal_schema"], "patch-proposal-result/v6")
        self.assertEqual(handoff["artifact_model_view"], "# fixture artifact\n")
        self.assertEqual([finding["finding_id"] for finding in handoff["validated_findings"]], ["F-001"])
        self.assertEqual(
            handoff["required_target_invariant_by_finding"],
            {"F-001": handoff["validated_findings"][0]["invariant_id"]},
        )

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

    def test_independent_assessment_composes_a_valid_v6_proposal(self) -> None:
        self._accept_review()
        handoff = build_proposal_handoff(self.store, self.root, self.run_id)
        fixture = json.loads((ROOT / "fixtures/v6/patch-proposal-result.json").read_text(encoding="utf-8"))
        draft = {
            "schema_version": "proposal-draft/v1",
            "phase_name": "proposal-draft",
            "proposer_id": "tui-native-proposer",
            "summary": fixture["summary"],
            "base_snapshot_hash": handoff["snapshot_hash"],
            "review_artifact_id": handoff["review_artifact_id"],
            "no_target_write_attestation": True,
            "proposals": [{key: value for key, value in fixture["proposals"][0].items() if key != "assessment"}],
        }
        draft["proposals"][0]["finding_id"] = handoff["open_finding_ids"][0]
        draft["proposals"][0]["causal_evidence_refs"] = [handoff["review_artifact_id"]]
        validate_proposal_draft(draft, handoff)
        array_risks = json.loads(json.dumps(draft))
        array_risks["proposals"][0]["known_risks"] = ["runtime remains unverified"]
        with self.assertRaisesRegex(WorkflowStateError, "known_risks must be a non-empty string"):
            validate_proposal_draft(array_risks, handoff)
        duplicate_evidence = json.loads(json.dumps(draft))
        duplicate_evidence["proposals"][0]["causal_evidence_refs"] = [
            handoff["review_artifact_id"], handoff["review_artifact_id"],
        ]
        with self.assertRaisesRegex(WorkflowStateError, "evidence must bind exactly"):
            validate_proposal_draft(duplicate_evidence, handoff)
        assessment = {
            "schema_version": "proposal-assessment/v1",
            "phase_name": "proposal-assessment",
            "assessor_id": "tui-native-proposal-assessor",
            "draft_sha256": proposal_draft_hash(draft),
            "base_snapshot_hash": handoff["snapshot_hash"],
            "review_artifact_id": handoff["review_artifact_id"],
            "assessments": [{
                "finding_id": handoff["open_finding_ids"][0],
                **fixture["proposals"][0]["assessment"],
            }],
        }
        assessment["assessments"][0]["assessor_id"] = "tui-native-proposal-assessor"
        assessment["assessments"][0]["evidence_refs"] = [handoff["review_artifact_id"]]
        validate_proposal_assessment(assessment, draft, handoff)

        final = compose_assessed_v6_proposal(draft, assessment, handoff)

        self.assertEqual(final["schema_version"], "6.0")
        self.assertEqual(final["proposer_id"], "tui-native-proposer")
        self.assertEqual(final["proposals"][0]["assessment"]["assessor_id"], "tui-native-proposal-assessor")
        validate_patch_proposal(final)

    def test_assessment_rejects_self_assessment_or_a_different_draft(self) -> None:
        self._accept_review()
        handoff = build_proposal_handoff(self.store, self.root, self.run_id)
        draft = {
            "schema_version": "proposal-draft/v1",
            "phase_name": "proposal-draft",
            "proposer_id": "tui-native-proposer",
            "summary": "fixture draft",
            "base_snapshot_hash": handoff["snapshot_hash"],
            "review_artifact_id": handoff["review_artifact_id"],
            "no_target_write_attestation": True,
            "proposals": [{
                "finding_id": handoff["open_finding_ids"][0],
                "target_reference": "AGENTS.md",
                "unified_diff": "--- a/AGENTS.md\n+++ b/AGENTS.md\n@@ -1 +1 @@\n-old\n+new\n",
                "rationale": "fixture rationale",
                "target_invariants": [handoff["validated_findings"][0]["invariant_id"]],
                "causal_claim": "fixture causal claim",
                "causal_evidence_refs": [handoff["review_artifact_id"]],
                "authorization_basis": "review-only-human-application",
                "verification_plan": "human checks the proposal",
                "regression_plan": "re-run the fixture",
                "known_risks": "runtime remains unverified",
                "alternatives_considered": "leave the finding open",
                "claims_not_yet_verified": "the diff is not applied",
            }],
        }
        assessment = {
            "schema_version": "proposal-assessment/v1",
            "phase_name": "proposal-assessment",
            "assessor_id": "tui-native-proposer",
            "draft_sha256": proposal_draft_hash(draft),
            "base_snapshot_hash": handoff["snapshot_hash"],
            "review_artifact_id": handoff["review_artifact_id"],
            "assessments": [{
                "finding_id": handoff["open_finding_ids"][0],
                "assessor_id": "tui-native-proposer",
                "evidence_refs": [handoff["review_artifact_id"]],
                "scores": {"coverage": 20, "causal": 20, "scope": 15, "verification": 15, "regression": 10, "evidence": 10, "alternatives": 5, "clarity": 5},
                "total_score": 100,
                "recommendation": "recommend",
                "runtime_effectiveness": "unverified",
            }],
        }
        with self.assertRaisesRegex(WorkflowStateError, "distinct from proposer"):
            validate_proposal_assessment(assessment, draft, handoff)
        assessment["assessor_id"] = "tui-native-proposal-assessor"
        assessment["assessments"][0]["assessor_id"] = "tui-native-proposal-assessor"
        assessment["draft_sha256"] = "0" * 64
        with self.assertRaisesRegex(WorkflowStateError, "draft"):
            validate_proposal_assessment(assessment, draft, handoff)
        assessment["draft_sha256"] = proposal_draft_hash(draft)
        assessment["assessments"][0]["evidence_refs"] = [
            handoff["review_artifact_id"], handoff["review_artifact_id"],
        ]
        with self.assertRaisesRegex(WorkflowStateError, "evidence must bind exactly"):
            validate_proposal_assessment(assessment, draft, handoff)

    def test_draft_rejects_a_malformed_unified_diff_before_assessment(self) -> None:
        self._accept_review()
        handoff = build_proposal_handoff(self.store, self.root, self.run_id)
        draft = {
            "schema_version": "proposal-draft/v1", "phase_name": "proposal-draft",
            "proposer_id": "tui-native-proposer", "summary": "fixture draft",
            "base_snapshot_hash": handoff["snapshot_hash"],
            "review_artifact_id": handoff["review_artifact_id"],
            "no_target_write_attestation": True,
            "proposals": [{
                "finding_id": handoff["open_finding_ids"][0], "target_reference": "AGENTS.md",
                "unified_diff": "--- a/AGENTS.md\n+++ b/AGENTS.md\n@@ -1 +1 @@\n-old\n+new",
                "rationale": "fixture rationale", "target_invariants": ["I-REVIEW"],
                "causal_claim": "fixture causal claim",
                "causal_evidence_refs": [handoff["review_artifact_id"]],
                "authorization_basis": "review-only-human-application",
                "verification_plan": "human checks the proposal",
                "regression_plan": "re-run the fixture", "known_risks": "runtime remains unverified",
                "alternatives_considered": "leave the finding open",
                "claims_not_yet_verified": "the diff is not applied",
            }],
        }
        with self.assertRaisesRegex(WorkflowStateError, "unified_diff"):
            validate_proposal_draft(draft, handoff)

    def test_draft_rejects_an_invariant_not_linked_to_its_finding_before_assessment(self) -> None:
        self._accept_review()
        handoff = build_proposal_handoff(self.store, self.root, self.run_id)
        draft = {
            "schema_version": "proposal-draft/v1", "phase_name": "proposal-draft",
            "proposer_id": "tui-native-proposer", "summary": "fixture draft",
            "base_snapshot_hash": handoff["snapshot_hash"],
            "review_artifact_id": handoff["review_artifact_id"],
            "no_target_write_attestation": True,
            "proposals": [{
                "finding_id": handoff["open_finding_ids"][0], "target_reference": "AGENTS.md",
                "unified_diff": "--- a/AGENTS.md\n+++ b/AGENTS.md\n@@ -1 +1 @@\n-old\n+new\n",
                "rationale": "fixture rationale", "target_invariants": ["I-NOT-LINKED"],
                "causal_claim": "fixture causal claim",
                "causal_evidence_refs": [handoff["review_artifact_id"]],
                "authorization_basis": "review-only-human-application",
                "verification_plan": "human checks the proposal",
                "regression_plan": "re-run the fixture", "known_risks": "runtime remains unverified",
                "alternatives_considered": "leave the finding open",
                "claims_not_yet_verified": "the diff is not applied",
            }],
        }

        with self.assertRaisesRegex(WorkflowStateError, "linked finding invariant"):
            build_proposal_assessment_handoff(draft, handoff)

    def test_draft_requires_the_human_only_authorization_constant(self) -> None:
        self._accept_review()
        handoff = build_proposal_handoff(self.store, self.root, self.run_id)
        fixture = json.loads((ROOT / "fixtures/v6/patch-proposal-result.json").read_text(encoding="utf-8"))
        draft = {
            "schema_version": "proposal-draft/v1", "phase_name": "proposal-draft",
            "proposer_id": "tui-native-proposer", "summary": fixture["summary"],
            "base_snapshot_hash": handoff["snapshot_hash"],
            "review_artifact_id": handoff["review_artifact_id"],
            "no_target_write_attestation": True,
            "proposals": [{key: value for key, value in fixture["proposals"][0].items() if key != "assessment"}],
        }
        draft["proposals"][0]["finding_id"] = handoff["open_finding_ids"][0]
        draft["proposals"][0]["causal_evidence_refs"] = [handoff["review_artifact_id"]]
        draft["proposals"][0]["authorization_basis"] = "human-only-but-not-protocol-value"
        with self.assertRaisesRegex(WorkflowStateError, "authorization_basis"):
            validate_proposal_draft(draft, handoff)

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

    def test_resume_locators_recover_the_target_root_without_using_the_host_root(self) -> None:
        located = find_resumable_locators(self.store)

        self.assertEqual(located["unavailable"], [])
        self.assertEqual(len(located["locators"]), 1)
        locator = located["locators"][0]
        self.assertEqual(locator["batch_id"], self.batch_id)
        self.assertEqual(locator["project_key"], self.project_key)
        self.assertEqual(locator["target_project_root"], str(self.root.resolve()))
        self.assertEqual(locator["status"], "READY")
        self.assertEqual(locator["summary"], {
            "selected_count": 1,
            "active_child_count": 1,
            "purged_child_count": 0,
            "by_state": {"READY": 1},
        })

    def test_resume_locators_return_multiple_targets_without_choosing_one(self) -> None:
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

        located = find_resumable_locators(self.store)

        self.assertEqual([item["batch_id"] for item in located["locators"]], [
            "batch_bbbbbbbbbbbb", self.batch_id,
        ])
        self.assertEqual({item["target_project_root"] for item in located["locators"]}, {
            str(self.root.resolve()), str(other_root.resolve()),
        })
        self.assertEqual(located["unavailable"], [])

    def test_resume_locator_rejects_a_missing_target_path(self) -> None:
        missing_root = Path(self.temporary_directory.name) / "missing-project"
        missing_identity = f"path:{missing_root}"
        missing_key = project_key(missing_identity)
        self.store.create_batch_with_ready_children(
            {"batch_id": "batch_cccccccccccc", "project_key": missing_key, "inventory_hash": "c" * 64},
            [
                (
                    _run("run_cccccccccccc", missing_key, "d" * 64),
                    {
                        "batch_id": "batch_cccccccccccc",
                        "relative_path": "MISSING.md",
                        "project_identity": missing_identity,
                        "identity_mode": "resolved-path",
                        "review_intent": "system-default-nl-artifact-review",
                    },
                    "MISSING.md",
                )
            ],
            now=1_002,
        )

        located = find_resumable_locators(self.store)

        self.assertEqual([item["batch_id"] for item in located["locators"]], [self.batch_id])
        self.assertEqual(located["unavailable"], [{
            "batch_id": "batch_cccccccccccc",
            "reason": "TARGET_ROOT_UNAVAILABLE",
        }])

    def test_resume_locator_rejects_an_anchor_whose_identity_disagrees_with_the_batch_key(self) -> None:
        other_root = Path(self.temporary_directory.name) / "other-project"
        other_root.mkdir()
        other_identity, _ = project_identity(other_root, None)
        self.store.create_batch_with_ready_children(
            {"batch_id": "batch_dddddddddddd", "project_key": self.project_key, "inventory_hash": "c" * 64},
            [
                (
                    _run("run_dddddddddddd", self.project_key, "d" * 64),
                    {
                        "batch_id": "batch_dddddddddddd",
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

        located = find_resumable_locators(self.store)

        self.assertEqual([item["batch_id"] for item in located["locators"]], [self.batch_id])
        self.assertEqual(located["unavailable"], [{
            "batch_id": "batch_dddddddddddd",
            "reason": "PROJECT_KEY_MISMATCH",
        }])


if __name__ == "__main__":
    unittest.main()
