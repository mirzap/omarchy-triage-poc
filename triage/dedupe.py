"""Coarse file-and-hunk signatures used only as grouping metadata."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from triage.models import ChangedFile, PullRequest

_HUNK_RE = re.compile(r"^@@[^@]+@@", re.MULTILINE)


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
    if not parts:
        return ""
    payload = "\n".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def file_set_signature(paths: Iterable[str]) -> str:
    """Hash of sorted unique file paths (for rule matching)."""
    joined = "\n".join(sorted(set(paths)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]


def apply_fingerprints(prs: list[PullRequest]) -> list[PullRequest]:
    for pr in prs:
        pr.fingerprint = compute_fingerprint(pr)
    return prs
