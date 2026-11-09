"""Temporary-fixture probes for a future bounded-repair adapter.

This module is intentionally not a repair implementation.  It observes a
small set of filesystem primitives inside a disposable directory so that M4
policy can be based on local evidence instead of assumptions.  In particular,
an observed primitive is not evidence that a complete repair flow is safe.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import multiprocessing
import os
import stat
import tempfile
from enum import Enum
from pathlib import Path
from queue import Empty
from typing import Any


class ProbeStatus(str, Enum):
    OBSERVED = "observed"
    UNSUPPORTED = "unsupported"
    NOT_PROVEN = "not-proven"
    FAILED = "failed"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _probe(probe_id: str, status: ProbeStatus, observation: str, limit: str, **details: Any) -> dict[str, Any]:
    return {
        "id": probe_id,
        "status": status.value,
        "observation": observation,
        "limit": limit,
        "details": details,
    }


def _validated_leaf_name(name: str) -> str:
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError("target name must be a single non-special path component")
    return name


def open_anchored_regular_file(directory: Path, name: str) -> int:
    """Open one regular, non-hardlinked leaf below an already chosen directory.

    The caller owns the returned descriptor.  This proves only leaf-level
    protection for a directory descriptor it already trusts; it is not a
    recursive ``openat`` walker and therefore does not establish parent-path
    TOCTOU safety.
    """
    leaf = _validated_leaf_name(name)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(directory, directory_flags)
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError(errno.ENOTSUP, "O_NOFOLLOW is unavailable")
        try:
            target_fd = os.open(leaf, os.O_RDONLY | nofollow, dir_fd=directory_fd)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTSUP}:
                raise ValueError("target must not be a symbolic link") from error
            raise
    finally:
        os.close(directory_fd)

    target_stat = os.fstat(target_fd)
    if not stat.S_ISREG(target_stat.st_mode):
        os.close(target_fd)
        raise ValueError("target must be a regular file")
    if target_stat.st_nlink != 1:
        os.close(target_fd)
        raise ValueError("target must not have multiple hard links")
    return target_fd


def _try_nonblocking_flock(lock_path: str, result_queue: multiprocessing.queues.Queue[Any]) -> None:
    """Child-process helper: report whether a cooperating process can lock."""
    try:
        with open(lock_path, "a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result_queue.put({"acquired": False})
                return
            result_queue.put({"acquired": True})
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as error:
        result_queue.put({"error": f"{error.__class__.__name__}: {error}"})


def _locked_by_other_process(lock_path: Path) -> tuple[bool, str | None]:
    """Return whether a child was excluded by an advisory lock held here."""
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(target=_try_nonblocking_flock, args=(str(lock_path), result_queue))
    with open(lock_path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        process.start()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
            return False, "child lock probe timed out"
        try:
            child_result = result_queue.get(timeout=2)
        except Empty:
            return False, "child lock probe returned no result"
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    if "error" in child_result:
        return False, str(child_result["error"])
    return not bool(child_result.get("acquired")), None


def _atomic_replace_with_mode_observation(root: Path) -> dict[str, Any]:
    target = root / "replace-target.md"
    target.write_bytes(b"before")
    target.chmod(0o640)
    temporary = root / ".replace-target.m4-tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"after")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, target)
    directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    mode_after = stat.S_IMODE(target.stat().st_mode)
    return {
        "content_replaced": target.read_bytes() == b"after",
        "temporary_name_absent": not temporary.exists(),
        "mode_before": "0640",
        "mode_after": f"{mode_after:04o}",
        "mode_preserved": mode_after == 0o640,
    }


def _xattr_observation(root: Path) -> dict[str, Any]:
    target = root / "xattr-target.md"
    target.write_bytes(b"xattr fixture")
    if not all(hasattr(os, attribute) for attribute in ("getxattr", "setxattr", "listxattr")):
        return {"available": False, "reason": "Python xattr APIs are unavailable"}
    attribute = "user.nlp_refinement_doe"
    try:
        os.setxattr(target, attribute, b"fixture-value")
        return {
            "available": True,
            "read_back": os.getxattr(target, attribute) == b"fixture-value",
            "listed": attribute in os.listxattr(target),
        }
    except OSError as error:
        return {"available": False, "reason": f"{error.__class__.__name__}: {error}"}


def _matching_hash_rollback_observation(root: Path) -> dict[str, Any]:
    target = root / "rollback-target.md"
    preimage = b"original fixture"
    patch = b"system patch"
    target.write_bytes(preimage)
    post_patch_hash = _sha256(patch)
    target.write_bytes(patch)
    if _sha256(target.read_bytes()) == post_patch_hash:
        target.write_bytes(preimage)
        matching_rollback = target.read_bytes() == preimage
    else:
        matching_rollback = False

    target.write_bytes(patch)
    target.write_bytes(b"external change")
    drifted_value = target.read_bytes()
    rollback_skipped_on_drift = _sha256(drifted_value) != post_patch_hash
    return {
        "matching_rollback": matching_rollback,
        "rollback_skipped_on_drift": rollback_skipped_on_drift,
        "drifted_bytes_unchanged": target.read_bytes() == drifted_value,
    }


def run_isolated_repair_feasibility_doe() -> dict[str, Any]:
    """Observe M4-relevant primitives in a newly created temporary directory.

    The returned report deliberately includes unsupported and not-proven cases.
    A caller must not use it to authorize target-worktree modification.
    """
    probes: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="nlp-refinement-m4-doe-") as temporary_root:
        root = Path(temporary_root)
        regular = root / "regular.md"
        regular.write_bytes(b"safe fixture")
        leaf_link = root / "leaf-link.md"
        leaf_link.symlink_to(regular)
        hard_link = root / "hard-link.md"
        hard_link.hardlink_to(regular)

        try:
            open_anchored_regular_file(root, leaf_link.name)
        except ValueError:
            probes.append(_probe(
                "DOE-ANCHOR-LEAF-SYMLINK", ProbeStatus.OBSERVED,
                "A descriptor-relative open rejected a symbolic-link leaf.",
                "This does not prove safety against parent-directory replacement after selection.",
            ))
        except OSError as error:
            probes.append(_probe(
                "DOE-ANCHOR-LEAF-SYMLINK", ProbeStatus.UNSUPPORTED,
                "The host could not run the descriptor-relative symlink rejection probe.",
                "A bounded-repair adapter remains blocked until a leaf-link rejection primitive is available.",
                error=f"{error.__class__.__name__}: {error}",
            ))
        else:
            probes.append(_probe(
                "DOE-ANCHOR-LEAF-SYMLINK", ProbeStatus.FAILED,
                "A symbolic-link leaf was accepted.",
                "A bounded-repair adapter must not use this primitive on this host.",
            ))

        try:
            open_anchored_regular_file(root, hard_link.name)
        except ValueError:
            probes.append(_probe(
                "DOE-ANCHOR-HARDLINK", ProbeStatus.OBSERVED,
                "A descriptor-relative open rejected a multiply-linked regular file.",
                "This says nothing about links created after the descriptor was opened.",
            ))
        except OSError as error:
            probes.append(_probe(
                "DOE-ANCHOR-HARDLINK", ProbeStatus.UNSUPPORTED,
                "The host could not run the descriptor-relative hardlink rejection probe.",
                "A bounded-repair adapter remains blocked until a hardlink rejection primitive is available.",
                error=f"{error.__class__.__name__}: {error}",
            ))
        else:
            probes.append(_probe(
                "DOE-ANCHOR-HARDLINK", ProbeStatus.FAILED,
                "A multiply-linked regular file was accepted.",
                "A bounded-repair adapter must not use this primitive on this host.",
            ))

        lock_excluded, lock_error = _locked_by_other_process(root / ".nlp-refinement.lock")
        probes.append(_probe(
            "DOE-LOCK-COOPERATING-PROCESS",
            ProbeStatus.OBSERVED if lock_excluded else ProbeStatus.FAILED,
            "A second cooperating process was excluded by flock." if lock_excluded else "The cooperating lock probe did not exclude its child.",
            "flock is advisory: it does not stop an editor or non-cooperating process.",
            error=lock_error,
        ))

        try:
            replace = _atomic_replace_with_mode_observation(root)
            probes.append(_probe(
                "DOE-ATOMIC-REPLACE", ProbeStatus.OBSERVED,
                "A same-directory temporary file was fsynced, renamed, and directory-fsynced.",
                "The observation cannot simulate power loss or prove crash durability on every filesystem.",
                **replace,
            ))
        except OSError as error:
            probes.append(_probe(
                "DOE-ATOMIC-REPLACE", ProbeStatus.UNSUPPORTED,
                "The host could not complete the same-directory replace probe.",
                "A bounded-repair adapter remains blocked until this is understood.",
                error=f"{error.__class__.__name__}: {error}",
            ))

        xattr = _xattr_observation(root)
        probes.append(_probe(
            "DOE-XATTR-API",
            ProbeStatus.OBSERVED if xattr.get("available") and xattr.get("read_back") and xattr.get("listed") else ProbeStatus.UNSUPPORTED,
            "A temporary user xattr was written and read back." if xattr.get("available") else "The temporary fixture did not support this xattr probe.",
            "One synthetic xattr is not evidence that security labels, Finder metadata, or arbitrary production xattrs are safe to copy.",
            **xattr,
        ))

        rollback = _matching_hash_rollback_observation(root)
        probes.append(_probe(
            "DOE-MATCHING-HASH-ROLLBACK",
            ProbeStatus.OBSERVED if rollback["matching_rollback"] else ProbeStatus.FAILED,
            "A private fixture preimage was restored only after its system-patch hash matched." if rollback["matching_rollback"] else "The matching-hash rollback fixture failed.",
            "This only proves byte comparison in a temporary fixture; it does not implement backup retention or real repair rollback.",
            **rollback,
        ))
        probes.append(_probe(
            "DOE-DRIFT-SKIPS-ROLLBACK",
            ProbeStatus.OBSERVED if rollback["rollback_skipped_on_drift"] and rollback["drifted_bytes_unchanged"] else ProbeStatus.FAILED,
            "A changed post-patch fixture was left unchanged instead of being rolled back.",
            "A real adapter must persist and verify the post-patch hash before relying on this rule.",
            **rollback,
        ))

        probes.append(_probe(
            "DOE-PARENT-TOCTOU", ProbeStatus.NOT_PROVEN,
            "This DOE uses a trusted directory descriptor only for one leaf and does not race parent replacement.",
            "A recursive fd-anchored traversal or equivalent platform primitive must be designed and tested before real repair.",
        ))
        probes.append(_probe(
            "DOE-CRASH-RECOVERY", ProbeStatus.NOT_PROVEN,
            "No repair journal or crash-recovery implementation exists to exercise.",
            "A later implementation needs a crash-point test with a durable recovery record and explicit user recovery decision.",
        ))
        probes.append(_probe(
            "DOE-ACL-PRESERVATION", ProbeStatus.NOT_PROVEN,
            "The standard-library DOE does not inspect or preserve platform ACLs.",
            "ACL and security-label handling remain a blocking platform-policy decision for real repair.",
        ))

    return {
        "execution_boundary": "temporary-fixture-only",
        "fixture_root_removed": not root.exists(),
        "probes": probes,
    }


if __name__ == "__main__":
    print(json.dumps(run_isolated_repair_feasibility_doe(), ensure_ascii=False, sort_keys=True))
