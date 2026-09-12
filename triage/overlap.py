"""Per-group file overlap stats (jaccard, shared/partial/unique, matrix)."""

from __future__ import annotations

import hashlib
from typing import Any

from triage.models import ChangedFile, Group, PullRequest, unified_patch_line_counts

PATCH_HASH_LEN = 12


def hash_patch(patch: str) -> str:
    """SHA-256 hex digest of raw patch text (short prefix for the matrix)."""
    digest = hashlib.sha256((patch or "").encode("utf-8")).hexdigest()
    return digest[:PATCH_HASH_LEN]


def file_overlap(members: list[PullRequest]) -> dict[str, Any]:
    """
    Compute overlap across group members.

    Returns:
      jaccard, shared, partial, unique, matrix
    """
    if not members:
        return {
            "jaccard": 0.0,
            "shared": [],
            "partial": [],
            "unique": [],
            "matrix": [],
        }

    n = len(members)
    # path -> complete file records. Display hashes may describe incomplete
    # bytes, but equality claims below are made only from canonical evidence.
    by_path: dict[str, list[tuple[int, ChangedFile]]] = {}
    sets: list[set[str]] = []
    for pr in members:
        paths = set()
        for f in pr.changed_files:
            paths.add(f.path)
            by_path.setdefault(f.path, []).append((pr.number, f))
        sets.append(paths)

    union: set[str] = set()
    inter: set[str] = set(sets[0]) if sets else set()
    for s in sets:
        union |= s
        inter &= s

    if n == 1:
        jaccard = 1.0
    elif not union:
        jaccard = 1.0
    else:
        jaccard = len(inter) / len(union)

    shared: list[str] = []
    partial: list[str] = []
    unique: list[str] = []
    matrix: list[dict[str, Any]] = []

    for path in sorted(by_path.keys()):
        entries = by_path[path]
        # de-dupe by PR number (keep first patch)
        seen: dict[int, ChangedFile] = {}
        for num, changed in entries:
            if num not in seen:
                seen[num] = changed
        count = len(seen)
        if count == n and n >= 1:
            kind = "shared"
            shared.append(path)
        elif count >= 2:
            kind = "partial"
            partial.append(path)
        else:
            kind = "unique"
            unique.append(path)

        prs_flag = {str(num): True for num in seen}
        patch_hashes = {str(num): hash_patch(changed.patch) for num, changed in seen.items()}
        complete: dict[int, bool] = {}
        for num, changed in seen.items():
            counts = unified_patch_line_counts(changed.patch)
            complete[num] = bool(
                changed.patch_complete
                and counts[0] is not None
                and (changed.additions is None or changed.additions == counts[0])
                and (changed.deletions is None or changed.deletions == counts[1])
            )
        hashes = [patch_hashes[str(num)] for num in seen]
        evidence_complete = len(seen) >= 2 and all(complete.values())
        same_patch = evidence_complete and len(set(hashes)) == 1
        same_patch_status = (
            "same" if same_patch else "different" if evidence_complete else "unknown"
        )

        matrix.append(
            {
                "path": path,
                "prs": prs_flag,
                "kind": kind,
                "patch_hashes": patch_hashes,
                "same_patch": same_patch,
                "same_patch_status": same_patch_status,
                "patch_evidence_complete": {str(num): value for num, value in complete.items()},
            }
        )

    return {
        "jaccard": round(jaccard, 6),
        "shared": shared,
        "partial": partial,
        "unique": unique,
        "matrix": matrix,
    }


def slim_to_pr(slim: dict[str, Any]) -> PullRequest:
    """Hydrate a minimal PullRequest from a slim persist dict (with or without patches)."""
    files_raw = slim.get("files") or []
    changed: list[ChangedFile] = []
    if files_raw and isinstance(files_raw[0], str):
        # legacy: files was a list of path strings
        for p in files_raw:
            changed.append(ChangedFile(path=str(p), patch=""))
    else:
        for f in files_raw:
            if isinstance(f, dict):
                changed.append(ChangedFile.from_dict(f))
            elif isinstance(f, str):
                changed.append(ChangedFile(path=f, patch=""))
    if not changed:
        for p in slim.get("paths") or []:
            changed.append(ChangedFile(path=str(p), patch=""))
    return PullRequest(
        number=int(slim["number"]),
        title=slim.get("title", ""),
        body="",
        user=slim.get("user", ""),
        changed_files=changed,
        created_at=slim.get("created_at", ""),
        label=slim.get("label", "needs-human"),
        html_url=slim.get("html_url", "") or "",
    )


def overlap_for_groups(
    groups: list[Group],
    prs: list[PullRequest],
) -> dict[str, dict[str, Any]]:
    """Map group_id -> file_overlap(members)."""
    by_num = {p.number: p for p in prs}
    out: dict[str, dict[str, Any]] = {}
    for g in groups:
        members = [by_num[n] for n in g.pr_numbers if n in by_num]
        out[g.group_id] = file_overlap(members)
    return out


def overlap_from_store_payload(
    groups: list[dict[str, Any]] | list[Group],
    slim_prs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Recompute overlap from persisted groups + slim PRs (for ui_state fallback)."""
    group_objs: list[Group] = []
    for g in groups:
        if isinstance(g, Group):
            group_objs.append(g)
        else:
            group_objs.append(Group.from_dict(g))
    prs = [slim_to_pr(s) for s in slim_prs]
    return overlap_for_groups(group_objs, prs)
