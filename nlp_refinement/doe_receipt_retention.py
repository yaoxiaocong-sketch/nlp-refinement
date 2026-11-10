"""Owner-only lifecycle helpers for redacted M8 DOE failure receipts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import time

from nlp_refinement.version import CLI_VERSION


DEFAULT_FAILURE_EVIDENCE_ROOT = Path("~/.codex/refinement-loop/doe-failures").expanduser()
FAILURE_EVIDENCE_RETENTION_SECONDS = 24 * 60 * 60
_FAILURE_EVIDENCE_NAME = re.compile(r"failure-[0-9]{10}-[0-9a-f]{16}\.json\Z")


def prepare_failure_evidence_root(root: Path) -> Path:
    """Create or validate the owner-only root used by the DOE writer."""
    root = root.expanduser()
    if root.exists():
        _validate_existing_root(root)
        return root
    root.mkdir(parents=True, mode=0o700)
    return root


def purge_expired_failure_receipts(root: Path, now: int) -> list[str]:
    """Remove only expired, parseable, regular receipts; never create a root."""
    root = root.expanduser()
    if not root.exists():
        return []
    _validate_existing_root(root)
    removed: list[str] = []
    for candidate in root.iterdir():
        if not _FAILURE_EVIDENCE_NAME.fullmatch(candidate.name):
            continue
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode):
            continue
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("expires_at"), int) and value["expires_at"] <= now:
            candidate.unlink()
            removed.append(candidate.name)
    return removed


def _validate_existing_root(root: Path) -> None:
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("DOE failure evidence root must be a non-symlink directory")
    if info.st_uid != os.getuid():
        raise PermissionError("DOE failure evidence root must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError("DOE failure evidence root must be owner-only")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nlp-refinement-doe-prune",
        description="Remove expired, redacted M8 DOE failure receipts without invoking a model.",
    )
    parser.add_argument("--version", action="version", version=CLI_VERSION)
    parser.add_argument("--failure-evidence-root", type=Path, default=DEFAULT_FAILURE_EVIDENCE_ROOT)
    args = parser.parse_args(argv)
    removed = purge_expired_failure_receipts(args.failure_evidence_root, int(time.time()))
    print(json.dumps({
        "command": "doe-receipt-prune",
        "removed": removed,
        "removed_count": len(removed),
        "retention_seconds": FAILURE_EVIDENCE_RETENTION_SECONDS,
        "purge_trigger": "user-command",
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
