"""Content-free request contract shared by normal and external M8 entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HostExecutionRequest:
    """One caller-verified request that only an external host may execute."""

    run_id: str
    artifact_file: Path
    observed_snapshot_hash: str
