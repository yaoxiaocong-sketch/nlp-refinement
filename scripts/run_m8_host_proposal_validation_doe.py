"""Diagnose local patch-proposal validation after a completed host proposal turn."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nlp_refinement.diagnostic_doe_support import run_isolated_diagnostic
from nlp_refinement.m8_review_coordinator import M8ReviewCoordinator
from nlp_refinement.host_diagnostics import (
    ProposalValidationCapture,
    ProposalValidationDiagnosticCoordinatorMixin,
)
from nlp_refinement.host_executor import DiagnosticAppServerHostExecutor
from nlp_refinement.state_store import StateStore


class ProposalValidationDiagnosticCoordinator(ProposalValidationDiagnosticCoordinatorMixin, M8ReviewCoordinator):
    def __init__(self, store: StateStore, capture: ProposalValidationCapture) -> None:
        self._proposal_validation_capture = capture
        super().__init__(store, executor_factory=lambda: DiagnosticAppServerHostExecutor(capture.record_failure))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one isolated M8 proposal-validation diagnostic DOE; output is terminal-only."
    )
    parser.add_argument("--doe-kind", choices=("protocol", "quality"), default="protocol")
    args = parser.parse_args()
    capture = ProposalValidationCapture()
    code, report = run_isolated_diagnostic(
        capture,
        "proposal-local-validation/v1",
        coordinator_factory=lambda store, _capture: ProposalValidationDiagnosticCoordinator(store, capture),
        doe_kind=args.doe_kind,
    )
    report["temporary_root_deleted_after_exit"] = True
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
