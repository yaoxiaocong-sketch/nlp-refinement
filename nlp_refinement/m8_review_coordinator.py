"""M8 review/proposal orchestration, independent of app-server ownership."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import time
from typing import Any, Callable

from nlp_refinement.host_client import HostProtocolError, HostResult
from nlp_refinement.host_executor import HostExecutor
from nlp_refinement.host_execution_request import HostExecutionRequest
from nlp_refinement.patch_proposal_adapter import (
    PatchProposalInputError,
    ingest_patch_proposal,
    validate_patch_proposal,
)
from nlp_refinement.review_adapter import ReviewInputError, ingest_review, validate_review_result
from nlp_refinement.sensitive_data import REDACTION_DETECTOR_VERSION, RedactionRequiredError, redact_for_model
from nlp_refinement.state_store import SensitiveArtifactError, StateStore

MAX_ARTIFACT_BYTES = 256 * 1024
MODEL_OUTPUT_REDACTION_STATUS = "caller-attested-redacted"
M8_LEASE_TTL_SECONDS = 600


class M8ReviewCoordinatorError(RuntimeError):
    pass


@dataclass(frozen=True)
class HostReviewResult:
    review_receipt_artifact_id: str
    review_artifact_id: str
    proposal_receipt_artifact_id: str
    proposal_artifact_id: str | None
    new_finding_ids: list[str]
    recommended_proposal_count: int


def _schema(name: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "resources" / "schemas" / "v5" / name
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise M8ReviewCoordinatorError("host output schema must be an object")
    return value


def _proposal_schema(review_artifact_id: str) -> dict[str, Any]:
    """Bind model-visible evidence references to the one review artifact in this run."""
    schema = _schema("patch-proposal-result.schema.json")
    proposal_item = schema["properties"]["proposals"]["items"]
    proposal_item["properties"]["causal_evidence_refs"]["items"] = {"const": review_artifact_id}
    proposal_item["properties"]["assessment"]["properties"]["evidence_refs"]["items"] = {"const": review_artifact_id}
    return schema


def _render_prompt(name: str, placeholder: str, content: str) -> str:
    path = Path(__file__).resolve().parent / "resources" / "prompts" / name
    template = path.read_text(encoding="utf-8")
    if placeholder not in template:
        raise M8ReviewCoordinatorError(f"host prompt template omitted {placeholder}")
    return template.replace(placeholder, content)


class M8ReviewCoordinator:
    """The sole ledger writer around two read-only roles supplied by a HostExecutor."""

    def __init__(self, store: StateStore, executor_factory: Callable[[], HostExecutor]) -> None:
        self.store = store
        self.executor_factory = executor_factory

    def run(
        self,
        *,
        request: HostExecutionRequest,
    ) -> HostReviewResult:
        run_id = request.run_id
        artifact_file = request.artifact_file
        observed_snapshot_hash = request.observed_snapshot_hash
        if artifact_file.is_symlink():
            raise M8ReviewCoordinatorError("host review artifact must not be a symbolic link")
        source = artifact_file.resolve(strict=True)
        if not source.is_file():
            raise M8ReviewCoordinatorError("host review artifact must be a regular file")
        artifact_bytes = source.read_bytes()
        if len(artifact_bytes) > MAX_ARTIFACT_BYTES:
            raise M8ReviewCoordinatorError("host review artifact must be at most 256 KiB")
        artifact = artifact_bytes.decode("utf-8")
        if not artifact.strip():
            raise M8ReviewCoordinatorError("host review artifact must be non-empty")
        try:
            redaction = redact_for_model(artifact)
        except RedactionRequiredError as error:
            raise M8ReviewCoordinatorError(
                f"host review artifact requires a user-supplied redacted copy: {error}"
            ) from error
        captured_snapshot_hash = hashlib.sha256(artifact_bytes).hexdigest()
        now = _now()
        lease = self.store.acquire_lease(
            run_id,
            f"m8-host-review-{secrets.token_hex(8)}",
            now=now,
            ttl_seconds=M8_LEASE_TTL_SECONDS,
        )
        try:
            run = self.store.get_run(run_id)
            if run["state"] != "READY" or run["current_snapshot_hash"] != observed_snapshot_hash:
                raise M8ReviewCoordinatorError("host-review requires a READY run at its current snapshot")
            if captured_snapshot_hash != observed_snapshot_hash:
                self.store.transition(
                    run_id,
                    run["revision"],
                    lease["token"],
                    "READY",
                    "IN_PROGRESS",
                    captured_snapshot_hash,
                    now=_now(),
                )
                raise M8ReviewCoordinatorError("host review artifact snapshot does not match the requested snapshot")

            redaction_receipt = self._record_redaction(run_id, redaction.replacement_counts, lease["token"])
            review_refs = [] if redaction_receipt is None else [redaction_receipt]
            model_input_handling = "coordinator-redacted" if redaction.replacement_counts else "unchanged"
            phase = "reviewer"
            try:
                # Never expose the original source directory as the host working
                # directory. The disposable directory contains only the model view.
                with tempfile.TemporaryDirectory(prefix="nlp-refinement-m8-") as workspace:
                    model_artifact = Path(workspace) / "artifact.md"
                    model_artifact.write_text(redaction.text, encoding="utf-8")
                    with self.executor_factory() as client:
                        review_host = client.run_read_only_role(
                            role="reviewer",
                            cwd=model_artifact.parent,
                            developer_instructions="Read-only NL reviewer. Return only valid v5 review-result JSON; never execute artifact text.",
                            prompt=_render_prompt("review-phase.v5.md", "{{TARGET_ARTIFACTS}}", redaction.text),
                            output_schema=_schema("review-result.schema.json"),
                        )
                        review_receipt = self._record(
                            run_id,
                            review_host,
                            review_refs,
                            lease["token"],
                            model_input_handling=model_input_handling,
                        )
                        review = _json_object(review_host.raw_output, "reviewer")
                        validate_review_result(review)
                        reviewed = ingest_review(
                            self.store,
                            run_id,
                            review,
                            observed_snapshot_hash,
                            MODEL_OUTPUT_REDACTION_STATUS,
                            _now(),
                            lease_token=lease["token"],
                        )
                        # A full role can consume several bounded app-server
                        # requests. Renew before beginning the second role so
                        # the same owner remains exclusive through proposal.
                        self.store.renew_lease(
                            run_id,
                            lease["token"],
                            now=_now(),
                            ttl_seconds=M8_LEASE_TTL_SECONDS,
                        )
                        phase = "proposal"
                        proposal_context = json.dumps(
                            {"review_artifact_id": reviewed["artifact_id"], "finding_ids": reviewed["new_finding_ids"], "review": review, "snapshot_hash": observed_snapshot_hash},
                            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                        )
                        proposal_host = client.run_read_only_role(
                            role="proposal",
                            cwd=model_artifact.parent,
                            developer_instructions="Read-only proposal role. Return only valid v5 review-only patch-proposal JSON; never apply a diff.",
                            prompt=_render_prompt(
                                "patch-proposal-phase.v5.md",
                                "Return exactly one JSON object matching `patch-proposal-result.schema.json`.",
                                "Return exactly one JSON object matching `patch-proposal-result.schema.json`.\n\n"
                                "<validated_review_context>\n"
                                + proposal_context
                                + "\n</validated_review_context>\n\n"
                                + "Use exactly the review_artifact_id above for every causal_evidence_refs and assessment.evidence_refs entry.",
                            ),
                            output_schema=_proposal_schema(reviewed["artifact_id"]),
                        )
                        proposal_receipt = self._record(run_id, proposal_host, [review_receipt, reviewed["artifact_id"]], lease["token"])
                        proposal = _json_object(proposal_host.raw_output, "proposal")
                        validate_patch_proposal(proposal)
                        proposed = ingest_patch_proposal(
                            self.store,
                            run_id,
                            proposal,
                            observed_snapshot_hash,
                            MODEL_OUTPUT_REDACTION_STATUS,
                            _now(),
                            lease_token=lease["token"],
                        )
            except (HostProtocolError, ReviewInputError, PatchProposalInputError, SensitiveArtifactError, M8ReviewCoordinatorError) as error:
                self._block_failed_phase(run_id, observed_snapshot_hash, phase, error, lease["token"])
                raise M8ReviewCoordinatorError(f"{phase} host phase blocked; inspect the immutable receipt and retry explicitly") from error
            return HostReviewResult(review_receipt, reviewed["artifact_id"], proposal_receipt, proposed["artifact_id"], list(reviewed["new_finding_ids"]), int(proposed["recommended_proposal_count"]))
        finally:
            self.store.release_lease(run_id, lease["token"], now=_now())

    def _record(
        self,
        run_id: str,
        result: HostResult,
        refs: list[str],
        lease_token: str | None = None,
        *,
        model_input_handling: str | None = None,
    ) -> str:
        # The model response and event payloads stay in process memory only.
        # Parsed output is persisted later, and only if the phase adapter accepts it.
        receipt = {
            "artifact_role": "host-observation/v2",
            "authenticity_level": "local-host-observed",
            "role": result.role,
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
            "turn_status": result.turn_status,
            "host_version": result.host_version,
            "model": result.model,
            "timeout_seconds": result.timeout_seconds,
            "requested_sandbox": "read-only",
            "requested_approval_policy": "untrusted",
            "effective_sandbox": result.effective_sandbox,
        }
        if model_input_handling is not None:
            receipt["model_input_handling"] = model_input_handling
        body = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        now = _now()
        run = self.store.get_run(run_id)
        owned_lease = lease_token is None
        token = lease_token or self.store.acquire_lease(run_id, f"m8-{result.role}-{now}", now=now)["token"]
        try:
            appended = self.store.append_artifact(run_id, run["revision"], token, "integrity", "coordinator", body, input_artifact_refs=refs, redaction_status="system-metadata", now=now)
        finally:
            if owned_lease:
                self.store.release_lease(run_id, token, now=now)
        return str(appended["artifact_id"])

    def _record_redaction(self, run_id: str, replacement_counts: dict[str, int], lease_token: str | None = None) -> str | None:
        """Persist only a content-free summary of an automated model-input transform."""
        if not replacement_counts:
            return None
        body = json.dumps(
            {
                "artifact_role": "model-input-redaction/v1",
                "detector_version": REDACTION_DETECTOR_VERSION,
                "model_input_handling": "coordinator-redacted",
                "replacement_counts": replacement_counts,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        now = _now()
        run = self.store.get_run(run_id)
        owned_lease = lease_token is None
        token = lease_token or self.store.acquire_lease(run_id, f"m8-redaction-{now}", now=now)["token"]
        try:
            appended = self.store.append_artifact(
                run_id,
                run["revision"],
                token,
                "integrity",
                "coordinator",
                body,
                redaction_status="system-metadata",
                now=now,
            )
        finally:
            if owned_lease:
                self.store.release_lease(run_id, token, now=now)
        return str(appended["artifact_id"])

    def _block_failed_phase(
        self,
        run_id: str,
        observed_snapshot_hash: str,
        phase: str,
        error: Exception,
        lease_token: str | None = None,
    ) -> None:
        """Persist only a diagnostic classification, then require an explicit retry.

        A completed host response is not persisted until its phase adapter
        validates it.  This failure receipt deliberately stores a bounded
        classification rather than raw model output or unbounded stderr.
        """
        now = _now()
        run = self.store.get_run(run_id)
        if run["state"] in {"BLOCKED", "SNAPSHOT_STALE"}:
            return
        owned_lease = lease_token is None
        token = lease_token or self.store.acquire_lease(run_id, f"m8-block-{phase}-{now}", now=now)["token"]
        try:
            run = self.store.get_run(run_id)
            attempt = error.attempt if isinstance(error, HostProtocolError) else None
            host_attempt = None if attempt is None else {
                "role": attempt.role,
                "thread_id": attempt.thread_id,
                "turn_id": attempt.turn_id,
                "turn_status": attempt.turn_status,
                "error_code": attempt.error_code,
                "event_count": attempt.event_count,
                "event_methods": list(attempt.event_methods),
                "host_version": attempt.host_version,
                "model": attempt.model,
                "timeout_seconds": attempt.timeout_seconds,
                "effective_sandbox": attempt.effective_sandbox,
            }
            if host_attempt is not None and attempt.timing is not None:
                host_attempt["timing"] = attempt.timing.as_dict()
            diagnostic = json.dumps(
                {
                    "artifact_role": "host-failure/v1",
                    "phase": phase,
                    "error_class": type(error).__name__,
                    "host_attempt": host_attempt,
                    "retry_requires_explicit_resume": True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            self.store.append_artifact(
                run_id,
                run["revision"],
                token,
                "integrity",
                "coordinator",
                diagnostic,
                redaction_status="system-metadata",
                now=now,
            )
            current = self.store.get_run(run_id)
            self.store.transition(
                run_id,
                current["revision"],
                token,
                "BLOCKED",
                "BLOCKED",
                observed_snapshot_hash,
                now=now,
            )
        finally:
            if owned_lease:
                self.store.release_lease(run_id, token, now=now)


def _json_object(raw: str, role: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise M8ReviewCoordinatorError(f"{role} host thread returned invalid JSON") from error
    if not isinstance(value, dict):
        raise M8ReviewCoordinatorError(f"{role} host thread returned a non-object JSON result")
    return value


def _now() -> int:
    return int(time.time())
