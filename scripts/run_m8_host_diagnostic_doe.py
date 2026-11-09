"""Run one disposable M8 diagnosis without changing normal host-review policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nlp_refinement.host_diagnostics import DiagnosticCapture
from nlp_refinement.diagnostic_doe_support import run_isolated_diagnostic


def run() -> tuple[int, dict[str, object]]:
    """Run exactly once; this diagnostic neither retries nor archives evidence."""
    return run_isolated_diagnostic(DiagnosticCapture(), "whitelisted-protocol-signals/v1")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one isolated M8 diagnostic DOE with no durable diagnostic capture."
    )
    parser.parse_args()
    code, report = run()
    report["temporary_root_deleted_after_exit"] = True
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
