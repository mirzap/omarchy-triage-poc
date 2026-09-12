"""Static de-dupe: normalize titles and fingerprint PRs by files + hunks."""

from __future__ import annotations

import hashlib
import re
import string
from typing import Iterable

from triage.models import ChangedFile, PullRequest

_PUNCT_TABLE = str.maketrans("", "", string.punctuation)
_HUNK_RE = re.compile(r"^@@[^@]+@@", re.MULTILINE)
_WS_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace."""
    lowered = title.lower()
    stripped = lowered.translate(_PUNCT_TABLE)
    return _WS_RE.sub(" ", stripped).strip()


def extract_hunk_headers(patch: str) -> list[str]:
    """Return normalized hunk headers from a unified diff patch."""
    if not patch:
        return []
    return [m.group(0).strip() for m in _HUNK_RE.finditer(patch)]


def file_fingerprint_parts(files: Iterable[ChangedFile]) -> list[str]:
    parts: list[str] = []
    for f in sorted(files, key=lambda x: x.path):
        hunks = extract_hunk_headers(f.patch)
        hunk_part = "|".join(hunks) if hunks else ""
        parts.append(f"{f.path}::{hunk_part}")
    return parts


def compute_fingerprint(pr: PullRequest) -> str:
    """
    Stable fingerprint = hash of sorted changed paths + normalized hunk headers.
    Title is excluded: agents write unique titles for the same shape.
    """
    parts = file_fingerprint_parts(pr.changed_files)
    payload = "\n".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def file_set_signature(paths: Iterable[str]) -> str:
    """Hash of sorted unique file paths (for rule matching)."""
    joined = "\n".join(sorted(set(paths)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]


def compute_simhash(pr: PullRequest) -> int:
    """64-bit SimHash over the same shingles used for MinHash."""
    from triage.minhash import shingles_for_pr
    from triage.simhash import simhash

    return simhash(shingles_for_pr(pr))


def apply_fingerprints(prs: list[PullRequest]) -> list[PullRequest]:
    for pr in prs:
        pr.fingerprint = compute_fingerprint(pr)
        pr.simhash = compute_simhash(pr)
    return prs


def static_groups(prs: list[PullRequest]) -> dict[str, list[PullRequest]]:
    """Collapse exact fingerprint matches into static groups."""
    groups: dict[str, list[PullRequest]] = {}
    for pr in prs:
        if not pr.fingerprint:
            pr.fingerprint = compute_fingerprint(pr)
        if not pr.simhash:
            pr.simhash = compute_simhash(pr)
        groups.setdefault(pr.fingerprint, []).append(pr)
    return groups
