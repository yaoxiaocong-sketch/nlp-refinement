"""Shared disposable harness for opt-in M8 diagnostic DOE entrypoints only."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
from typing import Any, Callable

from nlp_refinement.host_execution_request import HostExecutionRequest
from nlp_refinement.m8_review_coordinator import M8ReviewCoordinator
from nlp_refinement.host_diagnostics import FailureCapture
from nlp_refinement.host_executor import DiagnosticAppServerHostExecutor
from nlp_refinement.m8_doe_fixtures import fixture_for
from nlp_refinement.state_store import StateStore


def run_isolated_diagnostic(
    capture: FailureCapture,
    diagnostic_mode: str,
    coordinator_factory: Callable[[StateStore, FailureCapture], M8ReviewCoordinator] | None = None,
    doe_kind: str = "protocol",
) -> tuple[int, dict[str, Any]]:
    """Run one shared Coordinator path in a temporary root; never persist it."""
    with tempfile.TemporaryDirectory(prefix="nlp-refinement-m8-diagnostic-") as directory:
        root = Path(directory)
        target = root / "fixture"
        target.mkdir(mode=0o700)
        fixture = fixture_for(doe_kind)
        artifact = target / f"{doe_kind}-fixture.md"
        artifact.write_text(fixture, encoding="utf-8")
        store = StateStore(root / "state" / "state.db")
        store.initialize()
        snapshot = _snapshot(fixture)
        run_id = _ready_run(store, snapshot)
        try:
            (coordinator_factory or _default_coordinator)(store, capture).run(
                request=HostExecutionRequest(run_id, artifact, snapshot)
            )
            status, code, error_class = "passed", 0, None
        except Exception as error:
            status, code, error_class = "failed", 1, type(error).__name__
        return code, {
            "status": status,
            "error_class": error_class,
            "diagnostic_mode": diagnostic_mode,
            "diagnostic_signals": capture.snapshot(),
            "artifact_kinds": [entry["kind"] for entry in store.list_artifacts(run_id)],
            "fixture_unchanged": artifact.read_text(encoding="utf-8") == fixture,
            "target_writes": [],
        }


def _default_coordinator(store: StateStore, capture: FailureCapture) -> M8ReviewCoordinator:
    return M8ReviewCoordinator(
        store,
        executor_factory=lambda: DiagnosticAppServerHostExecutor(capture.record_failure),
    )


def _snapshot(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ready_run(store: StateStore, snapshot: str) -> str:
    run_id = "run_m8diagnostic0001"
    store.create_run(
        {
            "schema_version": "5.0", "run_id": run_id,
            "project_key": hashlib.sha256(b"nlp-refinement/m8-diagnostic-doe").hexdigest(),
            "target_snapshot_hash": snapshot, "current_snapshot_hash": snapshot,
            "authorization_mode": "review-only", "state": "DRAFT", "outcome": "IN_PROGRESS",
            "revision": 0, "repair_cycle_count": 0, "max_repair_cycles": 3,
            "open_high_risk_assumptions": 0, "retention_policy_version": "5.0",
        },
        {"redacted_brief": "Run an isolated M8 host diagnostic DOE."},
        now=1,
    )
    lease = store.acquire_lease(run_id, "m8-diagnostic-setup", now=1)
    try:
        store.transition(run_id, 0, lease["token"], "READY", "IN_PROGRESS", snapshot, now=1)
    finally:
        store.release_lease(run_id, lease["token"], now=1)
    return run_id
