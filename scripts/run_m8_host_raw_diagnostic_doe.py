"""Run one explicitly authorized, bounded raw-fragment M8 diagnostic DOE."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nlp_refinement.diagnostic_doe_support import run_isolated_diagnostic
from nlp_refinement.host_diagnostics import RawDiagnosticCapture


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one isolated M8 raw-fragment diagnostic DOE; output is terminal-only."
    )
    parser.parse_args()
    code, report = run_isolated_diagnostic(RawDiagnosticCapture(), "bounded-redacted-protocol-fragments/v1")
    report["temporary_root_deleted_after_exit"] = True
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
