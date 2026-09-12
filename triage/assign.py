"""Incremental assignment: new PRs join an existing file-set group or start one."""

from __future__ import annotations

from triage.cluster import FILE_JACCARD_CONFIRM, _build_group, _file_jaccard, cluster_prs
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


def _best_join(
    members: list[PullRequest],
    attached: dict[str, list[PullRequest]],
) -> tuple[str | None, float]:
    best_gid: str | None = None
    best_j = 0.0
    for gid, old_members in attached.items():
        for a in members:
            ap = set(a.paths)
            afp = a.fingerprint
            for b in old_members:
                if afp and afp == b.fingerprint:
                    return gid, 1.0
                j = _file_jaccard(ap, set(b.paths))
                if j > best_j:
                    best_j = j
                    best_gid = gid
    return best_gid, best_j


def assign_incremental(
    prs: list[PullRequest],
    existing_groups: list[Group],
) -> list[Group]:
    """
    Keep open PRs in their current groups. Cluster brand-new PRs among
    themselves, then attach a new cluster to an existing group only when
    file Jaccard >= FILE_JACCARD_CONFIRM (or exact fingerprint).
    """
    if not existing_groups:
        return cluster_prs(prs, [[0.0] for _ in prs])

    by_num = {p.number: p for p in prs}
    old_of: dict[int, str] = {}
    for g in existing_groups:
        for n in g.pr_numbers:
            old_of[n] = g.group_id

    attached: dict[str, list[PullRequest]] = {}
    new_prs: list[PullRequest] = []
    for p in prs:
        gid = old_of.get(p.number)
        if gid:
            attached.setdefault(gid, []).append(p)
        else:
            new_prs.append(p)

    if new_prs:
        fresh_clusters = cluster_prs(new_prs, [[0.0] for _ in new_prs])
    else:
        fresh_clusters = []

    leftovers: list[list[PullRequest]] = []
    for ng in fresh_clusters:
        members = [by_num[n] for n in ng.pr_numbers if n in by_num]
        if not members:
            continue
        gid, j = _best_join(members, attached)
        if gid and j >= FILE_JACCARD_CONFIRM:
            attached[gid].extend(members)
        else:
            leftovers.append(members)

    groups: list[Group] = []
    used_ids = list(attached.keys())
    for gid, members in attached.items():
        if members:
            groups.append(_build_group(gid, members, [0.0]))
    new_ids = _next_group_ids(used_ids, len(leftovers))
    for gid, members in zip(new_ids, leftovers):
        groups.append(_build_group(gid, members, [0.0]))
    groups.sort(key=lambda g: (min(g.pr_numbers) if g.pr_numbers else 0))
    return groups
