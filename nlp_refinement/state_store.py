"""Transactional, local-only state storage for formal refinement runs."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from nlp_refinement.sensitive_data import likely_secret_kind


class StateStoreError(RuntimeError):
    """Base class for durable-local StateStore failures."""


class LeaseConflict(StateStoreError):
    """Raised when another active owner holds a run lease."""


class LeaseInvalid(StateStoreError):
    """Raised when a write lacks a matching, unexpired lease token."""


class RevisionConflict(StateStoreError):
    """Raised when a write was prepared from a stale run revision."""


class ImmutableFieldError(StateStoreError):
    """Raised when a finding tries to alter an immutable identity field."""


class InvalidTransition(StateStoreError):
    """Raised when a requested state transition is not in the M0 state graph."""


class RetentionPlanStale(StateStoreError):
    """Raised when a confirmation does not match the current retention plan."""


class SensitiveArtifactError(StateStoreError):
    """Raised when a narrow secret guard blocks durable caller content."""

DAY_SECONDS = 24 * 60 * 60
STALE_AFTER_DAYS = 14
HOT_ARTIFACT_DAYS = 30
ARCHIVE_SUMMARY_DAYS = 180
PURGE_AFTER_DAYS = 181

TERMINAL_OUTCOMES = {
    "PASS",
    "PASS_WITH_ACCEPTED_RISK",
    "NEEDS_USER_DECISION",
    "NO_MATERIAL_DELTA",
    "SNAPSHOT_STALE",
    "BLOCKED",
    "CANCELLED",
    "STALE",
}

FINAL_STATES = {
    "PASS",
    "PASS_WITH_ACCEPTED_RISK",
    "NO_MATERIAL_DELTA",
    "SNAPSHOT_STALE",
    "CANCELLED",
    "STALE",
}

ARTIFACT_KINDS = {"review", "repair", "verification", "decision", "integrity", "reclassification"}
ARTIFACT_ACTORS = {"coordinator", "reviewer", "fixer", "verifier", "integrity-gate", "user"}
REDACTION_STATUSES = {"system-metadata", "caller-attested-redacted"}
FINDING_SEVERITIES = {"P0", "P1", "P2", "P3"}
FINDING_GATES = {"blocking", "needs-user-decision", "recordable-risk"}
FINDING_STATUSES = {"open", "fixed", "verified", "retracted-with-evidence", "accepted-risk"}
FINDING_SUBJECT_KINDS = {"nl_artifact", "nl_host_boundary"}
FINDING_EVIDENCE_PLANES = {"static", "runtime", "hybrid"}
FINDING_EVIDENCE_CONFIDENCE = {"E1-inferred", "E2-direct", "E3-corroborated"}
FINDING_IMPACT_SCOPES = {
    "single-artifact", "cross-artifact", "core-workflow", "durable-state", "security-boundary",
}
FINDING_CLAIM_STATUSES = {"inferred", "direct-static", "observed-runtime", "corroborated"}
STATIC_ONLY_CATEGORIES = {
    "format_and_encoding",
    "schema_contract",
    "layout_and_discovery",
    "reference_integrity",
}
ARTIFACT_CATEGORIES = STATIC_ONLY_CATEGORIES | {
    "semantic_workflow",
    "safety_and_governance",
    "verification_design",
}
HOST_BOUNDARY_CATEGORIES = {
    "load_and_discovery",
    "instruction_adherence",
    "state_behavior",
    "outcome_verification",
    "safety_behavior",
}
RUN_STATES = {
    "DRAFT", "READY", "REVIEWING", "TRIAGING", "REPAIRING", "VERIFYING",
    "REREVIEWING", "INTEGRITY_CHECK", "ROLLING_BACK", "SNAPSHOT_STALE",
    "STALE", "PASS", "PASS_WITH_ACCEPTED_RISK", "NEEDS_USER_DECISION",
    "NO_MATERIAL_DELTA", "BLOCKED", "CANCELLED",
}
RUN_OUTCOMES = {"IN_PROGRESS"} | TERMINAL_OUTCOMES


def validate_finding_classification(subject_kind: str, evidence_plane: str, category: str) -> None:
    """Validate the v4 NL-review taxonomy without accepting support-code findings."""
    if subject_kind not in FINDING_SUBJECT_KINDS:
        raise ValueError("finding subject_kind is invalid")
    if evidence_plane not in FINDING_EVIDENCE_PLANES:
        raise ValueError("finding evidence_plane is invalid")
    if subject_kind == "nl_artifact":
        if category not in ARTIFACT_CATEGORIES:
            raise ValueError("finding category is invalid for nl_artifact")
        if category in STATIC_ONLY_CATEGORIES and evidence_plane != "static":
            raise ValueError("static NL artifact category requires static evidence")
        if category not in STATIC_ONLY_CATEGORIES and evidence_plane not in {"static", "hybrid"}:
            raise ValueError("NL artifact semantic category requires static or hybrid evidence")
        return
    if category not in HOST_BOUNDARY_CATEGORIES:
        raise ValueError("finding category is invalid for nl_host_boundary")
    if evidence_plane not in {"runtime", "hybrid"}:
        raise ValueError("nl_host_boundary category requires runtime or hybrid evidence")


def validate_finding_assessment(
    severity: str,
    effective_gate: str,
    evidence_plane: str,
    evidence_confidence: str,
    impact_scope: str,
    claim_status: str,
) -> None:
    """Keep impact severity, evidence confidence, and workflow gate independent but coherent."""
    if evidence_confidence not in FINDING_EVIDENCE_CONFIDENCE:
        raise ValueError("finding evidence_confidence is invalid")
    if impact_scope not in FINDING_IMPACT_SCOPES:
        raise ValueError("finding impact_scope is invalid")
    if claim_status not in FINDING_CLAIM_STATUSES:
        raise ValueError("finding claim_status is invalid")
    if evidence_confidence == "E1-inferred" and claim_status != "inferred":
        raise ValueError("E1-inferred evidence requires inferred claim_status")
    if evidence_confidence == "E2-direct":
        if claim_status == "direct-static" and evidence_plane not in {"static", "hybrid"}:
            raise ValueError("direct-static claim_status requires static or hybrid evidence")
        if claim_status == "observed-runtime" and evidence_plane not in {"runtime", "hybrid"}:
            raise ValueError("observed-runtime claim_status requires runtime or hybrid evidence")
        if claim_status not in {"direct-static", "observed-runtime"}:
            raise ValueError("E2-direct evidence requires a direct-static or observed-runtime claim_status")
    if evidence_confidence == "E3-corroborated" and (
        claim_status != "corroborated" or evidence_plane != "hybrid"
    ):
        raise ValueError("E3-corroborated evidence requires corroborated claim_status and hybrid evidence")
    if severity == "P0":
        if effective_gate != "blocking":
            raise ValueError("P0 requires blocking gate")
        if evidence_confidence == "E1-inferred":
            raise ValueError("P0 requires direct or corroborated evidence")
    elif severity == "P1":
        if effective_gate == "recordable-risk":
            raise ValueError("P1 cannot use recordable-risk gate")
    elif severity == "P2":
        if effective_gate == "blocking":
            raise ValueError("P2 cannot use blocking gate")
    elif severity == "P3":
        if effective_gate != "recordable-risk":
            raise ValueError("P3 requires recordable-risk gate")

ALLOWED_TRANSITIONS = {
    "DRAFT": {"READY"},
    "READY": {"REVIEWING"},
    "REVIEWING": {"TRIAGING"},
    "TRIAGING": {"REPAIRING", "NEEDS_USER_DECISION"},
    "REPAIRING": {"VERIFYING"},
    "VERIFYING": {"REREVIEWING", "ROLLING_BACK"},
    "REREVIEWING": {"INTEGRITY_CHECK"},
    "INTEGRITY_CHECK": {"PASS", "PASS_WITH_ACCEPTED_RISK", "NO_MATERIAL_DELTA", "NEEDS_USER_DECISION"},
    "ROLLING_BACK": {"REVIEWING"},
    "NEEDS_USER_DECISION": {"TRIAGING"},
    "BLOCKED": {"READY"},
}


class StateStore:
    """Own local durable state; target-worktree writes remain outside this class."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        state_root = self.database_path.parent
        if state_root.exists():
            if not state_root.is_dir():
                raise ValueError("database parent must be a directory")
            if stat.S_IMODE(state_root.stat().st_mode) & 0o077:
                raise PermissionError("database directory must be owner-only")
        else:
            state_root.mkdir(parents=True, mode=0o700)
        if self.database_path.exists():
            if not self.database_path.is_file():
                raise ValueError("database path must be a regular file")
            if stat.S_IMODE(self.database_path.stat().st_mode) & 0o077:
                raise PermissionError("database file must be owner-only")
        with self._connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "runs" in tables:
                for row in connection.execute("SELECT run_id, run_json FROM runs"):
                    try:
                        version = json.loads(row["run_json"]).get("schema_version")
                    except json.JSONDecodeError as error:
                        raise StateStoreError("stored run JSON is invalid") from error
                    if version != "4.0":
                        raise StateStoreError(
                            f"legacy ledger data for {row['run_id']} is unsupported; export or remove it explicitly"
                        )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    target_snapshot_hash TEXT NOT NULL,
                    current_snapshot_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    run_json TEXT NOT NULL,
                    immutable_anchor_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
                    owner_session_id TEXT NOT NULL,
                    token_digest TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    heartbeat_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_id TEXT UNIQUE,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    input_artifact_refs_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    body TEXT NOT NULL,
                    redaction_status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS findings (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    finding_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    origin_severity TEXT NOT NULL,
                    finding_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (run_id, finding_id),
                    UNIQUE (run_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS archived_runs (
                    run_id TEXT PRIMARY KEY,
                    archived_at INTEGER NOT NULL,
                    purge_at INTEGER NOT NULL,
                    archive_json TEXT NOT NULL
                );
                """
            )
        if not self.database_path.exists():
            raise RuntimeError("SQLite did not create the database file")
        self.database_path.chmod(0o600)

    def create_run(self, run: dict[str, Any], immutable_anchor: dict[str, Any], now: int) -> dict[str, Any]:
        self._validate_new_run(run)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, project_key, target_snapshot_hash, current_snapshot_hash,
                    state, outcome, revision, run_json, immutable_anchor_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["run_id"],
                    run["project_key"],
                    run["target_snapshot_hash"],
                    run["current_snapshot_hash"],
                    run["state"],
                    run["outcome"],
                    run["revision"],
                    self._dump(run),
                    self._dump(immutable_anchor),
                    now,
                    now,
                ),
            )
            self._record_event(connection, run["run_id"], 0, "run-created", {"immutable_anchor_hash": self._digest(self._dump(immutable_anchor))}, now)
        return self.get_run(str(run["run_id"]))

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        return self._run_from_row(row)

    def acquire_lease(self, run_id: str, owner_session_id: str, now: int, ttl_seconds: int = 600) -> dict[str, Any]:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        token = secrets.token_urlsafe(32)
        digest = self._digest(token)
        expires_at = now + ttl_seconds
        with self._transaction() as connection:
            self._require_run(connection, run_id)
            current = connection.execute("SELECT * FROM leases WHERE run_id = ?", (run_id,)).fetchone()
            if current is not None and current["expires_at"] > now:
                raise LeaseConflict(f"run {run_id} is leased by {current['owner_session_id']}")
            connection.execute(
                """
                INSERT INTO leases (run_id, owner_session_id, token_digest, expires_at, heartbeat_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    owner_session_id = excluded.owner_session_id,
                    token_digest = excluded.token_digest,
                    expires_at = excluded.expires_at,
                    heartbeat_at = excluded.heartbeat_at
                """,
                (run_id, owner_session_id, digest, expires_at, now),
            )
            self._record_event(connection, run_id, self._run_revision(connection, run_id), "lease-acquired", {"owner_session_id": owner_session_id, "expires_at": expires_at}, now)
        return {"run_id": run_id, "owner_session_id": owner_session_id, "token": token, "expires_at": expires_at}

    def renew_lease(self, run_id: str, lease_token: str, now: int, ttl_seconds: int = 600) -> dict[str, Any]:
        with self._transaction() as connection:
            lease = self._require_lease(connection, run_id, lease_token, now)
            expires_at = now + ttl_seconds
            connection.execute("UPDATE leases SET expires_at = ?, heartbeat_at = ? WHERE run_id = ?", (expires_at, now, run_id))
            self._record_event(connection, run_id, self._run_revision(connection, run_id), "lease-renewed", {"owner_session_id": lease["owner_session_id"], "expires_at": expires_at}, now)
        return {"run_id": run_id, "owner_session_id": lease["owner_session_id"], "token": lease_token, "expires_at": expires_at}

    def release_lease(self, run_id: str, lease_token: str, now: int) -> None:
        """Release a matching lease so a subsequent local session can continue immediately."""
        with self._transaction() as connection:
            lease = self._require_lease(connection, run_id, lease_token, now)
            connection.execute("DELETE FROM leases WHERE run_id = ?", (run_id,))
            self._record_event(
                connection,
                run_id,
                self._run_revision(connection, run_id),
                "lease-released",
                {"owner_session_id": lease["owner_session_id"]},
                now,
            )

    def append_artifact(
        self,
        run_id: str,
        expected_revision: int,
        lease_token: str,
        kind: str,
        actor: str,
        body: str,
        input_artifact_refs: list[str] | None = None,
        redaction_status: str = "caller-attested-redacted",
        now: int = 0,
    ) -> dict[str, Any]:
        if not body:
            raise ValueError("artifact body must not be empty")
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"unsupported artifact kind: {kind}")
        if actor not in ARTIFACT_ACTORS:
            raise ValueError(f"unsupported artifact actor: {actor}")
        if redaction_status not in REDACTION_STATUSES:
            raise ValueError("artifacts with rejected-sensitive status must not be persisted")
        if redaction_status == "system-metadata" and (kind != "integrity" or actor not in {"coordinator", "integrity-gate"}):
            raise ValueError("system-metadata is reserved for coordinator integrity receipts")
        self._require_no_likely_secret(body, redaction_status)
        refs = input_artifact_refs or []
        with self._transaction() as connection:
            run = self._require_run(connection, run_id)
            self._require_lease(connection, run_id, lease_token, now)
            self._require_revision(run, expected_revision)
            self._require_artifact_refs(connection, run_id, refs)
            cursor = connection.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, run_id, kind, actor, input_artifact_refs_json,
                    content_hash, body, redaction_status, revision, created_at
                ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, kind, actor, self._dump(refs), self._digest(body), body, redaction_status, expected_revision + 1, now),
            )
            sequence = int(cursor.lastrowid)
            artifact_id = f"r{sequence:08d}-{kind}"
            connection.execute("UPDATE artifacts SET artifact_id = ? WHERE sequence = ?", (artifact_id, sequence))
            updated = self._advance_run(connection, run, now)
            self._record_event(connection, run_id, updated["revision"], "artifact-appended", {"artifact_id": artifact_id, "kind": kind, "actor": actor}, now)
        return {"artifact_id": artifact_id, "revision": updated["revision"], "content_hash": self._digest(body), "kind": kind, "actor": actor}

    def record_finding(self, run_id: str, expected_revision: int, lease_token: str, finding: dict[str, Any], now: int) -> dict[str, Any]:
        with self._transaction() as connection:
            run = self._require_run(connection, run_id)
            self._require_lease(connection, run_id, lease_token, now)
            self._require_revision(run, expected_revision)
            self._validate_finding_identity(run_id, finding)
            self._require_artifact_refs(connection, run_id, list(finding["evidence_refs"]))
            self._require_accepted_risk_decision(connection, run_id, finding)
            connection.execute(
                """
                INSERT INTO findings (run_id, finding_id, fingerprint, origin_severity, finding_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, finding["finding_id"], finding["fingerprint"], finding["origin_severity"], self._dump(finding), now, now),
            )
            updated = self._advance_run(connection, run, now)
            self._record_event(connection, run_id, updated["revision"], "finding-recorded", {"finding_id": finding["finding_id"]}, now)
        return {"finding_id": finding["finding_id"], "revision": updated["revision"]}

    def update_finding(self, run_id: str, expected_revision: int, lease_token: str, finding: dict[str, Any], now: int) -> dict[str, Any]:
        with self._transaction() as connection:
            run = self._require_run(connection, run_id)
            self._require_lease(connection, run_id, lease_token, now)
            self._require_revision(run, expected_revision)
            self._validate_finding_identity(run_id, finding)
            existing = connection.execute("SELECT * FROM findings WHERE run_id = ? AND finding_id = ?", (run_id, finding["finding_id"])).fetchone()
            if existing is None:
                raise KeyError(f"finding not found: {finding['finding_id']}")
            existing_finding = json.loads(existing["finding_json"])
            if (
                finding["origin_severity"] != existing["origin_severity"]
                or finding["fingerprint"] != existing["fingerprint"]
                or finding["effective_gate"] != existing_finding["effective_gate"]
                or any(
                    finding[field] != existing_finding[field]
                    for field in (
                        "subject_kind", "evidence_plane", "category",
                        "evidence_confidence", "impact_scope", "claim_status",
                    )
                )
            ):
                raise ImmutableFieldError("finding identity and classification fields cannot change")
            self._require_artifact_refs(connection, run_id, list(finding["evidence_refs"]))
            self._require_accepted_risk_decision(connection, run_id, finding)
            connection.execute("UPDATE findings SET finding_json = ?, updated_at = ? WHERE run_id = ? AND finding_id = ?", (self._dump(finding), now, run_id, finding["finding_id"]))
            updated = self._advance_run(connection, run, now)
            self._record_event(connection, run_id, updated["revision"], "finding-updated", {"finding_id": finding["finding_id"]}, now)
        return {"finding_id": finding["finding_id"], "revision": updated["revision"]}

    def apply_review_phase(
        self,
        run_id: str,
        expected_revision: int,
        lease_token: str,
        observed_snapshot_hash: str,
        review_body: str,
        redaction_status: str,
        planned_findings: list[dict[str, Any]],
        now: int,
    ) -> dict[str, Any]:
        """Atomically persist one externally produced review and its findings.

        `planned_findings` contains coordinator-validated dictionaries with
        action `new` or `duplicate`; the review artifact evidence reference is
        allocated inside this transaction rather than supplied by the reviewer.
        """
        if redaction_status != "caller-attested-redacted":
            raise ValueError("review content requires caller-attested-redacted status")
        if not review_body:
            raise ValueError("review body must not be empty")
        self._require_no_likely_secret(review_body, redaction_status)
        with self._transaction() as connection:
            run = self._require_run(connection, run_id)
            self._require_lease(connection, run_id, lease_token, now)
            self._require_revision(run, expected_revision)
            if run["state"] != "READY":
                raise InvalidTransition(f"review requires READY state, found {run['state']}")
            if observed_snapshot_hash != run["current_snapshot_hash"]:
                run["state"] = "SNAPSHOT_STALE"
                run["outcome"] = "SNAPSHOT_STALE"
                run["current_snapshot_hash"] = observed_snapshot_hash
                stale = self._write_run(connection, run, now)
                self._record_event(connection, run_id, stale["revision"], "snapshot-stale", {"observed_snapshot_hash": observed_snapshot_hash}, now)
                return {"run": stale, "artifact_id": None, "new_finding_ids": [], "duplicate_finding_ids": []}

            run["state"] = "REVIEWING"
            reviewing = self._write_run(connection, run, now)
            self._record_event(connection, run_id, reviewing["revision"], "state-transition", {"state": "REVIEWING", "outcome": "IN_PROGRESS"}, now)
            cursor = connection.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, run_id, kind, actor, input_artifact_refs_json,
                    content_hash, body, redaction_status, revision, created_at
                ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, "review", "reviewer", self._dump([]), self._digest(review_body), review_body, redaction_status, reviewing["revision"] + 1, now),
            )
            artifact_id = f"r{int(cursor.lastrowid):08d}-review"
            connection.execute("UPDATE artifacts SET artifact_id = ? WHERE sequence = ?", (artifact_id, cursor.lastrowid))
            current = self._advance_run(connection, reviewing, now)
            self._record_event(connection, run_id, current["revision"], "artifact-appended", {"artifact_id": artifact_id, "kind": "review", "actor": "reviewer"}, now)

            new_finding_ids: list[str] = []
            duplicate_finding_ids: list[str] = []
            for planned in planned_findings:
                if set(planned) != {"action", "finding"} or planned["action"] not in {"new", "duplicate"}:
                    raise ValueError("invalid planned review finding")
                finding = dict(planned["finding"])
                finding["evidence_refs"] = [*finding["evidence_refs"], artifact_id]
                self._validate_finding_identity(run_id, finding)
                if planned["action"] == "new":
                    connection.execute(
                        """
                        INSERT INTO findings (run_id, finding_id, fingerprint, origin_severity, finding_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (run_id, finding["finding_id"], finding["fingerprint"], finding["origin_severity"], self._dump(finding), now, now),
                    )
                    new_finding_ids.append(finding["finding_id"])
                    event_type = "finding-recorded"
                else:
                    existing = connection.execute(
                        "SELECT * FROM findings WHERE run_id = ? AND finding_id = ?", (run_id, finding["finding_id"])
                    ).fetchone()
                    if existing is None:
                        raise KeyError(f"finding not found: {finding['finding_id']}")
                    existing_finding = json.loads(existing["finding_json"])
                    if (
                        finding["origin_severity"] != existing["origin_severity"]
                        or finding["fingerprint"] != existing["fingerprint"]
                        or finding["effective_gate"] != existing_finding["effective_gate"]
                        or any(
                            finding[field] != existing_finding[field]
                            for field in (
                                "subject_kind", "evidence_plane", "category",
                                "evidence_confidence", "impact_scope", "claim_status",
                            )
                        )
                    ):
                        raise ImmutableFieldError("finding identity and classification fields cannot change")
                    connection.execute(
                        "UPDATE findings SET finding_json = ?, updated_at = ? WHERE run_id = ? AND finding_id = ?",
                        (self._dump(finding), now, run_id, finding["finding_id"]),
                    )
                    duplicate_finding_ids.append(finding["finding_id"])
                    event_type = "finding-updated"
                current = self._advance_run(connection, current, now)
                self._record_event(connection, run_id, current["revision"], event_type, {"finding_id": finding["finding_id"]}, now)

            current["state"] = "TRIAGING"
            triaged = self._write_run(connection, current, now)
            self._record_event(connection, run_id, triaged["revision"], "state-transition", {"state": "TRIAGING", "outcome": "IN_PROGRESS"}, now)
            return {
                "run": triaged,
                "artifact_id": artifact_id,
                "new_finding_ids": new_finding_ids,
                "duplicate_finding_ids": duplicate_finding_ids,
            }

    def record_user_decision(self, run_id: str, expected_revision: int, lease_token: str, body: str, now: int) -> dict[str, Any]:
        return self.append_artifact(
            run_id,
            expected_revision,
            lease_token,
            "decision",
            "user",
            body,
            redaction_status="caller-attested-redacted",
            now=now,
        )

    def apply_patch_proposal_phase(
        self,
        run_id: str,
        expected_revision: int,
        lease_token: str,
        observed_snapshot_hash: str,
        proposal_body: str,
        redaction_status: str,
        finding_ids: list[str],
        input_artifact_refs: list[str],
        now: int,
    ) -> dict[str, Any]:
        """Atomically record a human-only proposal without touching its target.

        The proposal remains a review artifact and may only attach evidence to
        currently open findings.  A drifted snapshot is terminal and records no
        proposal artifact or finding mutation.
        """
        if redaction_status != "caller-attested-redacted":
            raise ValueError("patch proposal content requires caller-attested-redacted status")
        if not proposal_body or not finding_ids:
            raise ValueError("patch proposal body and finding IDs must not be empty")
        self._require_no_likely_secret(proposal_body, redaction_status)
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("patch proposal finding IDs must be unique")
        with self._transaction() as connection:
            run = self._require_run(connection, run_id)
            self._require_lease(connection, run_id, lease_token, now)
            self._require_revision(run, expected_revision)
            if run["state"] != "TRIAGING":
                raise InvalidTransition(f"patch proposal requires TRIAGING state, found {run['state']}")
            if observed_snapshot_hash != run["current_snapshot_hash"]:
                run["state"] = "SNAPSHOT_STALE"
                run["outcome"] = "SNAPSHOT_STALE"
                run["current_snapshot_hash"] = observed_snapshot_hash
                stale = self._write_run(connection, run, now)
                self._record_event(connection, run_id, stale["revision"], "snapshot-stale", {"observed_snapshot_hash": observed_snapshot_hash}, now)
                return {"run": stale, "artifact_id": None, "proposal_count": 0}
            self._require_artifact_refs(connection, run_id, input_artifact_refs)
            findings: list[tuple[str, dict[str, Any]]] = []
            for finding_id in finding_ids:
                row = connection.execute(
                    "SELECT finding_json FROM findings WHERE run_id = ? AND finding_id = ?", (run_id, finding_id)
                ).fetchone()
                if row is None:
                    raise KeyError(f"finding not found: {finding_id}")
                finding = json.loads(row["finding_json"])
                if finding["status"] != "open":
                    raise ValueError(f"patch proposal requires an open finding: {finding_id}")
                findings.append((finding_id, finding))
            cursor = connection.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, run_id, kind, actor, input_artifact_refs_json,
                    content_hash, body, redaction_status, revision, created_at
                ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, "review", "reviewer", self._dump(input_artifact_refs), self._digest(proposal_body), proposal_body, redaction_status, run["revision"] + 1, now),
            )
            artifact_id = f"r{int(cursor.lastrowid):08d}-review"
            connection.execute("UPDATE artifacts SET artifact_id = ? WHERE sequence = ?", (artifact_id, cursor.lastrowid))
            for finding_id, finding in findings:
                finding["evidence_refs"] = [*finding["evidence_refs"], artifact_id]
                connection.execute(
                    "UPDATE findings SET finding_json = ?, updated_at = ? WHERE run_id = ? AND finding_id = ?",
                    (self._dump(finding), now, run_id, finding_id),
                )
            updated = self._advance_run(connection, run, now)
            self._record_event(
                connection,
                run_id,
                updated["revision"],
                "patch-proposal-recorded",
                {"artifact_id": artifact_id, "finding_ids": finding_ids, "proposal_count": len(finding_ids)},
                now,
            )
        return {"run": updated, "artifact_id": artifact_id, "proposal_count": len(finding_ids)}

    def transition(
        self,
        run_id: str,
        expected_revision: int,
        lease_token: str,
        next_state: str,
        next_outcome: str,
        observed_snapshot_hash: str,
        now: int,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            run = self._require_run(connection, run_id)
            self._require_lease(connection, run_id, lease_token, now)
            self._require_revision(run, expected_revision)
            if run["state"] in FINAL_STATES:
                raise InvalidTransition(f"final run state cannot transition: {run['state']}")
            if observed_snapshot_hash != run["current_snapshot_hash"]:
                run["state"] = "SNAPSHOT_STALE"
                run["outcome"] = "SNAPSHOT_STALE"
                run["current_snapshot_hash"] = observed_snapshot_hash
                updated = self._write_run(connection, run, now)
                self._record_event(connection, run_id, updated["revision"], "snapshot-stale", {"observed_snapshot_hash": observed_snapshot_hash}, now)
                return updated
            allowed = ALLOWED_TRANSITIONS.get(run["state"], set())
            if next_state not in allowed and next_state not in {"BLOCKED", "CANCELLED", "STALE"}:
                raise InvalidTransition(f"{run['state']} cannot transition to {next_state}")
            if next_state in TERMINAL_OUTCOMES and next_outcome != next_state:
                raise InvalidTransition("terminal state and outcome must match")
            if next_state not in TERMINAL_OUTCOMES and next_outcome != "IN_PROGRESS":
                raise InvalidTransition("non-terminal states require IN_PROGRESS outcome")
            run["state"] = next_state
            run["outcome"] = next_outcome
            updated = self._write_run(connection, run, now)
            self._record_event(connection, run_id, updated["revision"], "state-transition", {"state": next_state, "outcome": next_outcome}, now)
        return updated

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM events WHERE run_id = ? ORDER BY sequence", (run_id,)).fetchall()
        return [
            {"sequence": row["sequence"], "revision": row["revision"], "event_type": row["event_type"], "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]}
            for row in rows
        ]

    def list_findings(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT finding_json FROM findings WHERE run_id = ? ORDER BY finding_id", (run_id,)
            ).fetchall()
        return [json.loads(row["finding_json"]) for row in rows]

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT artifact_id, kind, actor, input_artifact_refs_json, content_hash, body, redaction_status, revision, created_at "
                "FROM artifacts WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [
            {
                "artifact_id": row["artifact_id"],
                "kind": row["kind"],
                "actor": row["actor"],
                "input_artifact_refs": json.loads(row["input_artifact_refs_json"]),
                "content_hash": row["content_hash"],
                "body": row["body"],
                "redaction_status": row["redaction_status"],
                "revision": row["revision"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def retention_preview(self, now: int) -> dict[str, Any]:
        """Return an immutable, content-free plan; it never changes the ledger."""
        with self._connection() as connection:
            actions = self._retention_actions(connection, now)
        plan = {"policy_version": "4.0", "generated_at": now, "actions": actions}
        return {**plan, "plan_hash": self._retention_plan_hash(actions)}

    def empty_retention_preview(self, now: int) -> dict[str, Any]:
        """Build the read-only result for a state root that has no ledger yet."""
        return {
            "policy_version": "4.0",
            "generated_at": now,
            "actions": [],
            "plan_hash": self._retention_plan_hash([]),
        }

    def apply_retention(self, plan_hash: str, now: int) -> dict[str, Any]:
        """Apply only the exact previewed lifecycle plan in one transaction."""
        with self._transaction() as connection:
            actions = self._retention_actions(connection, now)
            actual_hash = self._retention_plan_hash(actions)
            if not secrets.compare_digest(plan_hash, actual_hash):
                raise RetentionPlanStale("retention plan changed; run retention-preview again")
            completed: list[dict[str, Any]] = []
            for action in actions:
                run_id = action["run_id"]
                kind = action["action"]
                if kind == "mark-stale":
                    run = self._require_run(connection, run_id)
                    run["state"] = "STALE"
                    run["outcome"] = "STALE"
                    updated = self._write_run(connection, run, now)
                    self._record_event(connection, run_id, updated["revision"], "retention-stale", {}, now)
                elif kind == "archive":
                    self._archive_run(connection, run_id, now)
                elif kind == "purge-archive":
                    connection.execute("DELETE FROM archived_runs WHERE run_id = ?", (run_id,))
                else:  # defensive: `_retention_actions` is the sole producer.
                    raise StateStoreError(f"unsupported retention action: {kind}")
                completed.append(action)
        return {"plan_hash": plan_hash, "applied_at": now, "actions": completed}

    def list_archived_runs(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT run_id, archived_at, purge_at, archive_json FROM archived_runs ORDER BY archived_at, run_id"
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "archived_at": row["archived_at"],
                "purge_at": row["purge_at"],
                "archive": json.loads(row["archive_json"]),
            }
            for row in rows
        ]

    def get_finding_by_fingerprint(self, run_id: str, fingerprint: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT finding_json FROM findings WHERE run_id = ? AND fingerprint = ?",
                (run_id, fingerprint),
            ).fetchone()
        return None if row is None else json.loads(row["finding_json"])

    def _retention_actions(self, connection: sqlite3.Connection, now: int) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        active_leases = {
            row["run_id"]
            for row in connection.execute("SELECT run_id FROM leases WHERE expires_at > ?", (now,))
        }
        for row in connection.execute("SELECT run_id, state, updated_at FROM runs ORDER BY run_id"):
            run_id = str(row["run_id"])
            age = now - int(row["updated_at"])
            if run_id in active_leases:
                continue
            if row["state"] not in FINAL_STATES and age >= STALE_AFTER_DAYS * DAY_SECONDS:
                actions.append({"action": "mark-stale", "run_id": run_id})
            elif row["state"] in FINAL_STATES and age >= HOT_ARTIFACT_DAYS * DAY_SECONDS:
                actions.append({"action": "archive", "run_id": run_id})
        for row in connection.execute("SELECT run_id, purge_at FROM archived_runs ORDER BY run_id"):
            if now >= int(row["purge_at"]):
                actions.append({"action": "purge-archive", "run_id": str(row["run_id"])})
        return actions

    def _retention_plan_hash(self, actions: list[dict[str, Any]]) -> str:
        return self._digest(self._dump({"policy_version": "4.0", "actions": actions}))

    def _archive_run(self, connection: sqlite3.Connection, run_id: str, now: int) -> None:
        run_row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run_row is None:
            raise KeyError(f"run not found: {run_id}")
        run = self._run_from_row(run_row)
        if run["state"] not in FINAL_STATES:
            raise InvalidTransition("only final runs may be archived")
        artifact_rows = connection.execute(
            "SELECT artifact_id, kind, actor, content_hash, redaction_status, revision, created_at FROM artifacts WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        finding_rows = connection.execute("SELECT origin_severity, finding_json FROM findings WHERE run_id = ?", (run_id,)).fetchall()
        event_rows = connection.execute("SELECT event_type FROM events WHERE run_id = ?", (run_id,)).fetchall()
        severity_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        for finding_row in finding_rows:
            finding = json.loads(finding_row["finding_json"])
            severity = str(finding_row["origin_severity"])
            status = str(finding.get("status"))
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
            status_counts[status] = status_counts.get(status, 0) + 1
        event_type_counts: dict[str, int] = {}
        for event_row in event_rows:
            event_type = str(event_row["event_type"])
            event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
        archive = {
            "archive_version": "1.0",
            "run": {
                key: run[key]
                for key in (
                    "schema_version", "run_id", "project_key", "target_snapshot_hash",
                    "current_snapshot_hash", "authorization_mode", "state", "outcome",
                    "revision", "retention_policy_version",
                )
            },
            "timestamps": {"created_at": run_row["created_at"], "terminal_or_stale_at": run_row["updated_at"], "archived_at": now},
            "artifacts": [dict(row) for row in artifact_rows],
            "finding_counts": {"by_severity": severity_counts, "by_status": status_counts},
            "event_counts": {"total": len(event_rows), "by_type": event_type_counts},
        }
        purge_at = int(run_row["updated_at"]) + PURGE_AFTER_DAYS * DAY_SECONDS
        connection.execute(
            "INSERT INTO archived_runs (run_id, archived_at, purge_at, archive_json) VALUES (?, ?, ?, ?)",
            (run_id, now, purge_at, self._dump(archive)),
        )
        connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _require_run(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        return self._run_from_row(row)

    @staticmethod
    def _run_revision(connection: sqlite3.Connection, run_id: str) -> int:
        row = connection.execute("SELECT revision FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        return int(row["revision"])

    def _run_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        run = json.loads(row["run_json"])
        run.update(
            {
                "state": row["state"],
                "outcome": row["outcome"],
                "revision": row["revision"],
                "target_snapshot_hash": row["target_snapshot_hash"],
                "current_snapshot_hash": row["current_snapshot_hash"],
                "immutable_anchor": json.loads(row["immutable_anchor_json"]),
            }
        )
        return run

    def _require_lease(self, connection: sqlite3.Connection, run_id: str, lease_token: str, now: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM leases WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or row["expires_at"] <= now or not secrets.compare_digest(row["token_digest"], self._digest(lease_token)):
            raise LeaseInvalid(f"run {run_id} has no matching active lease")
        return row

    @staticmethod
    def _require_revision(run: dict[str, Any], expected_revision: int) -> None:
        if run["revision"] != expected_revision:
            raise RevisionConflict(f"expected revision {expected_revision}, found {run['revision']}")

    @staticmethod
    def _require_no_likely_secret(body: str, redaction_status: str) -> None:
        if redaction_status != "caller-attested-redacted":
            return
        kind = likely_secret_kind(body)
        if kind is not None:
            raise SensitiveArtifactError(f"caller-attested content rejected by likely-secret guard: {kind}")

    @staticmethod
    def _validate_finding_identity(run_id: str, finding: dict[str, Any]) -> None:
        required = {
            "schema_version", "finding_id", "run_id", "fingerprint", "origin_severity",
            "effective_gate", "status", "invariant_id", "evidence_refs", "repeat_count",
            "last_probe", "accepted_risk_decision_ref", "subject_kind", "evidence_plane", "category",
            "evidence_confidence", "impact_scope", "claim_status",
        }
        missing = required - set(finding)
        if missing:
            raise ValueError(f"finding is missing required fields: {sorted(missing)}")
        unexpected = set(finding) - required
        if unexpected:
            raise ValueError(f"finding has unexpected fields: {sorted(unexpected)}")
        if finding["run_id"] != run_id:
            raise ValueError("finding run_id must match target run")
        if finding["origin_severity"] not in FINDING_SEVERITIES:
            raise ValueError("finding origin_severity is invalid")
        if finding["schema_version"] != "4.0":
            raise ValueError("finding schema_version is invalid")
        if finding["effective_gate"] not in FINDING_GATES:
            raise ValueError("finding effective_gate is invalid")
        if finding["status"] not in FINDING_STATUSES:
            raise ValueError("finding status is invalid")
        if re.fullmatch(r"F-[0-9]{3,8}", str(finding["finding_id"])) is None:
            raise ValueError("finding_id is invalid")
        if re.fullmatch(r"[a-f0-9]{64}", str(finding["fingerprint"])) is None:
            raise ValueError("finding fingerprint is invalid")
        if not isinstance(finding["evidence_refs"], list) or not finding["evidence_refs"]:
            raise ValueError("finding evidence_refs must be a non-empty list")
        if not isinstance(finding["repeat_count"], int) or isinstance(finding["repeat_count"], bool) or finding["repeat_count"] < 0:
            raise ValueError("finding repeat_count is invalid")
        if not isinstance(finding["invariant_id"], str) or not finding["invariant_id"]:
            raise ValueError("finding invariant_id is invalid")
        if not isinstance(finding["last_probe"], str) or not finding["last_probe"]:
            raise ValueError("finding last_probe is invalid")
        if not isinstance(finding["accepted_risk_decision_ref"], str) or not finding["accepted_risk_decision_ref"]:
            raise ValueError("finding accepted_risk_decision_ref is invalid")
        validate_finding_classification(
            str(finding["subject_kind"]),
            str(finding["evidence_plane"]),
            str(finding["category"]),
        )
        validate_finding_assessment(
            str(finding["origin_severity"]),
            str(finding["effective_gate"]),
            str(finding["evidence_plane"]),
            str(finding["evidence_confidence"]),
            str(finding["impact_scope"]),
            str(finding["claim_status"]),
        )

    @staticmethod
    def _validate_new_run(run: dict[str, Any]) -> None:
        required = {
            "schema_version", "run_id", "project_key", "target_snapshot_hash",
            "current_snapshot_hash", "authorization_mode", "state", "outcome",
            "revision", "repair_cycle_count", "max_repair_cycles",
            "open_high_risk_assumptions", "retention_policy_version",
        }
        missing = required - set(run)
        if missing:
            raise ValueError(f"run is missing required fields: {sorted(missing)}")
        if run["schema_version"] != "4.0" or run["retention_policy_version"] != "4.0":
            raise ValueError("run schema or retention policy version is invalid")
        if re.fullmatch(r"run_[a-z0-9]{12,64}", str(run["run_id"])) is None:
            raise ValueError("run_id is invalid")
        for field in ("project_key", "target_snapshot_hash", "current_snapshot_hash"):
            if re.fullmatch(r"[a-f0-9]{64}", str(run[field])) is None:
                raise ValueError(f"{field} is invalid")
        if run["authorization_mode"] not in {"review-only", "bounded-repair", "repair-and-add-tests"}:
            raise ValueError("authorization_mode is invalid")
        if run["state"] not in RUN_STATES or run["outcome"] not in RUN_OUTCOMES:
            raise ValueError("run state or outcome is invalid")
        for field in ("revision", "repair_cycle_count", "open_high_risk_assumptions"):
            if not isinstance(run[field], int) or isinstance(run[field], bool) or run[field] < 0:
                raise ValueError(f"{field} is invalid")
        if not isinstance(run["max_repair_cycles"], int) or isinstance(run["max_repair_cycles"], bool) or not 1 <= run["max_repair_cycles"] <= 5:
            raise ValueError("max_repair_cycles is invalid")
        if run["repair_cycle_count"] > run["max_repair_cycles"]:
            raise ValueError("repair_cycle_count exceeds max_repair_cycles")
        if run["state"] != "DRAFT" or run["outcome"] != "IN_PROGRESS" or run["revision"] != 0:
            raise ValueError("a new run must start at DRAFT revision 0 with IN_PROGRESS outcome")

    @staticmethod
    def _require_artifact_refs(connection: sqlite3.Connection, run_id: str, refs: list[str]) -> None:
        for artifact_id in refs:
            row = connection.execute("SELECT 1 FROM artifacts WHERE run_id = ? AND artifact_id = ?", (run_id, artifact_id)).fetchone()
            if row is None:
                raise ValueError(f"artifact reference does not resolve: {artifact_id}")

    @staticmethod
    def _require_accepted_risk_decision(connection: sqlite3.Connection, run_id: str, finding: dict[str, Any]) -> None:
        if finding["status"] != "accepted-risk":
            return
        if finding["origin_severity"] not in {"P2", "P3"}:
            raise ValueError("accepted risk must originate as P2 or P3")
        decision_ref = finding["accepted_risk_decision_ref"]
        if decision_ref == "none":
            raise ValueError("accepted risk requires a user decision artifact")
        row = connection.execute(
            "SELECT kind, actor FROM artifacts WHERE run_id = ? AND artifact_id = ?",
            (run_id, decision_ref),
        ).fetchone()
        if row is None or row["kind"] != "decision" or row["actor"] != "user":
            raise ValueError("accepted risk requires a user decision artifact")

    def _advance_run(self, connection: sqlite3.Connection, run: dict[str, Any], now: int) -> dict[str, Any]:
        run["revision"] += 1
        return self._write_run(connection, run, now, increment_revision=False)

    def _write_run(self, connection: sqlite3.Connection, run: dict[str, Any], now: int, increment_revision: bool = True) -> dict[str, Any]:
        if increment_revision:
            run["revision"] += 1
        connection.execute(
            """
            UPDATE runs SET target_snapshot_hash = ?, current_snapshot_hash = ?, state = ?,
                outcome = ?, revision = ?, run_json = ?, updated_at = ? WHERE run_id = ?
            """,
            (
                run["target_snapshot_hash"],
                run["current_snapshot_hash"],
                run["state"],
                run["outcome"],
                run["revision"],
                self._dump({key: value for key, value in run.items() if key != "immutable_anchor"}),
                now,
                run["run_id"],
            ),
        )
        return run

    @staticmethod
    def _record_event(connection: sqlite3.Connection, run_id: str, revision: int, event_type: str, payload: dict[str, Any], now: int) -> None:
        connection.execute(
            "INSERT INTO events (run_id, revision, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, revision, event_type, json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False), now),
        )
