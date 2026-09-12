"""Incremental assignment: new PRs join an existing file-set group or start one."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import MutableMapping

from triage.cluster import (
    FILE_JACCARD_CONFIRM,
    _build_group,
    _file_jaccard,
    cluster_prs,
)
from triage.models import Group, PullRequest


def _next_group_ids(existing_ids: list[str], n: int) -> list[str]:
    nums: list[int] = []
    for gid in existing_ids:
        if gid.startswith("G"):
            tail = gid[1:]
            if tail.isdigit():
                nums.append(int(tail))
    start = (max(nums) + 1) if nums else 1
    return [f"G{start + i:03d}" for i in range(n)]


def _path_signature(pr: PullRequest) -> str:
    return hashlib.sha256(
        json.dumps(pr.paths, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def assign_incremental(
    prs: list[PullRequest],
    existing_groups: list[Group],
    *,
    repo: str = "",
    reserved_group_ids: list[str] | set[str] | None = None,
    stats: MutableMapping[str, int] | None = None,
) -> list[Group]:
    """
    Keep open PRs in their current groups. Cluster brand-new PRs among
    themselves, then attach a new cluster to an existing group only when
    file Jaccard >= FILE_JACCARD_CONFIRM (or exact fingerprint).
    """
    # Repository identity is part of group identity.  Foreign (and, for an
    # explicit repository, unscoped legacy) groups are never attachment
    # candidates, but all supplied IDs remain reserved globally.
    all_existing_ids = [g.group_id for g in existing_groups]
    repo_key = repo.strip().strip("/").lower()
    existing_groups = [
        g
        for g in existing_groups
        if g.repo.strip().strip("/").lower() == repo_key
    ]
    by_num = {p.number: p for p in prs}
    paths_by_num = {p.number: frozenset(p.paths) for p in prs}
    revisions_by_num = {p.number: p.revision_evidence() for p in prs}
    old_of: dict[int, tuple[str, Group]] = {}
    for g in existing_groups:
        for n in g.pr_numbers:
            old_of[n] = (g.group_id, g)

    attached: dict[str, list[PullRequest]] = {}
    new_prs: list[PullRequest] = []
    for p in prs:
        prior = old_of.get(p.number)
        if prior:
            gid, old_group = prior
            prior_revision = next(
                (item for item in old_group.member_revisions if item.pr_number == p.number),
                None,
            )
            current_revision = revisions_by_num[p.number]
            same_revision = prior_revision is not None and prior_revision == current_revision
            same_paths = old_group.member_path_signatures.get(str(p.number)) == _path_signature(p)
            # Complete evidence uses exact revision identity. Incomplete
            # evidence may preserve placement only while its full path set is
            # unchanged; it can never preserve an approval in queue.py.
            if same_revision or (
                prior_revision is not None
                and not prior_revision.evidence_complete
                and not current_revision.evidence_complete
                and same_paths
            ):
                attached.setdefault(gid, []).append(p)
            else:
                new_prs.append(p)
        else:
            new_prs.append(p)

    if new_prs:
        fresh_clusters = cluster_prs(new_prs)
    else:
        fresh_clusters = []

    group_path_sets: dict[str, list[frozenset[str]]] = {}
    path_to_groups: dict[str, set[str]] = defaultdict(set)
    for gid, members in attached.items():
        distinct = list(dict.fromkeys(paths_by_num[member.number] for member in members))
        group_path_sets[gid] = distinct
        for paths in distinct:
            for path in paths:
                path_to_groups[path].add(gid)

    join_checks = 0
    size_rejections = 0
    leftovers: list[list[PullRequest]] = []
    for ng in fresh_clusters:
        members = [by_num[n] for n in ng.pr_numbers if n in by_num]
        if not members:
            continue
        # Missing file evidence is not evidence that unrelated PRs share an
        # empty change shape. Keep each incomplete stub independently pending.
        if any(not member.paths for member in members):
            leftovers.extend([[member] for member in members])
            continue
        candidate_gids: set[str] = set()
        member_path_sets = [paths_by_num[member.number] for member in members]
        for paths in member_path_sets:
            for path in paths:
                candidate_gids.update(path_to_groups[path])
        best_gid: str | None = None
        best_jaccard = 0.0
        # Equal-scoring targets resolve by durable group identity, never by
        # caller/input order.
        for gid in sorted(candidate_gids):
            for new_paths in member_path_sets:
                for old_paths in group_path_sets[gid]:
                    if (
                        min(len(new_paths), len(old_paths))
                        / max(len(new_paths), len(old_paths))
                        < FILE_JACCARD_CONFIRM
                    ):
                        size_rejections += 1
                        continue
                    join_checks += 1
                    similarity = _file_jaccard(new_paths, old_paths)
                    if similarity > best_jaccard:
                        best_jaccard = similarity
                        best_gid = gid
        if best_gid and best_jaccard >= FILE_JACCARD_CONFIRM:
            attached[best_gid].extend(members)
            known_sets = group_path_sets[best_gid]
            for paths in member_path_sets:
                if paths not in known_sets:
                    known_sets.append(paths)
                    for path in paths:
                        path_to_groups[path].add(best_gid)
        else:
            leftovers.append(members)

    groups: list[Group] = []
    used_ids = (
        list(reserved_group_ids or [])
        + all_existing_ids
        + list(attached.keys())
    )
    for gid, members in attached.items():
        if members:
            group = _build_group(gid, members)
            group.repo = repo
            groups.append(group)
    new_ids = _next_group_ids(used_ids, len(leftovers))
    for gid, members in zip(new_ids, leftovers):
        group = _build_group(gid, members)
        group.repo = repo
        groups.append(group)
    groups.sort(key=lambda g: (min(g.pr_numbers) if g.pr_numbers else 0))
    if stats is not None:
        stats.update(
            existing_groups=len(attached),
            fresh_clusters=len(fresh_clusters),
            join_checks=join_checks,
            size_rejections=size_rejections,
        )
    return groups
