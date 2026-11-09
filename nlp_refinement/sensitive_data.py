"""Narrow secret guards and model-input redaction for review-only flows."""

from __future__ import annotations

from dataclasses import dataclass
import re


REDACTION_DETECTOR_VERSION = "1"


class RedactionRequiredError(ValueError):
    """A likely secret cannot be safely transformed without user input."""


@dataclass(frozen=True)
class ModelInputRedaction:
    """An in-memory model view and content-free replacement summary."""

    text: str
    replacement_counts: dict[str, int]


_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.DOTALL,
)
_INCOMPLETE_PRIVATE_KEY = re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----")
_REDACTABLE_PATTERNS = (
    ("private-key", _PRIVATE_KEY_BLOCK),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("api-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
)
_FUZZY_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:export\s+)?[\"']?(?:password|passwd|secret|token|api[_-]?key|apikey|authorization)[\"']?\s*[:=]\s*(?P<value>[^#\r\n]+?)\s*$"
)


def redact_for_model(value: str) -> ModelInputRedaction:
    """Return a safe-to-send view or require a user-produced redacted copy.

    Only complete, high-confidence values are replaced. The placeholder map is
    local to this call and is deliberately not returned or persisted.
    """
    text = value
    replacement_counts: dict[str, int] = {}
    placeholder_ids: dict[tuple[str, str], int] = {}

    def replace(label: str, match: re.Match[str]) -> str:
        original = match.group(0)
        key = (label, original)
        placeholder_id = placeholder_ids.setdefault(key, sum(1 for known_label, _ in placeholder_ids if known_label == label) + 1)
        replacement_counts[label] = replacement_counts.get(label, 0) + 1
        return f"<REDACTED:{label}:{placeholder_id}>"

    for label, pattern in _REDACTABLE_PATTERNS:
        text = pattern.sub(lambda match, label=label: replace(label, match), text)

    if _INCOMPLETE_PRIVATE_KEY.search(text):
        raise RedactionRequiredError("incomplete-private-key")
    if _fuzzy_assignment_present(text):
        raise RedactionRequiredError("ambiguous-secret-assignment")
    return ModelInputRedaction(text=text, replacement_counts=dict(sorted(replacement_counts.items())))


def likely_secret_kind(value: str) -> str | None:
    """Return a stable category, never echo the matched value.

    This is a narrow rejection guard for persistence, not proof that other
    content is safe. Unlike model-input redaction, it treats incomplete and
    ambiguous values as unsafe to persist too.
    """
    for label, pattern in _REDACTABLE_PATTERNS:
        if pattern.search(value):
            return label
    if _INCOMPLETE_PRIVATE_KEY.search(value):
        return "incomplete-private-key"
    if _fuzzy_assignment_present(value):
        return "ambiguous-secret-assignment"
    return None


def _fuzzy_assignment_present(value: str) -> bool:
    for match in _FUZZY_ASSIGNMENT.finditer(value):
        candidate = match.group("value").strip().rstrip(",").strip().strip("\"'")
        normalized = candidate.lower()
        if candidate.startswith("<REDACTED:"):
            continue
        if normalized.startswith("bearer <redacted:"):
            continue
        if candidate.startswith("${") and candidate.endswith("}"):
            continue
        if re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", candidate):
            continue
        return True
    return False
