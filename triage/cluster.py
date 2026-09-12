"""Deterministic file-set grouping for pull requests."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import MutableMapping

from triage.dedupe import file_set_signature
from triage.models import Group, PullRequest
from triage.unionfind import UnionFind

FILE_JACCARD_CONFIRM = 0.72


def _file_jaccard(a: set[str] | frozenset[str], b: set[str] | frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    return intersection / float(len(a) + len(b) - intersection)


def _can_reach_threshold(a_size: int, b_size: int) -> bool:
    """Cheap necessary size bound for Jaccard >= configured threshold."""
    if not a_size or not b_size:
        return False
    return min(a_size, b_size) / max(a_size, b_size) >= FILE_JACCARD_CONFIRM


def cluster_prs(
    prs: list[PullRequest],
    *,
    stats: MutableMapping[str, int] | None = None,
) -> list[Group]:
    """Group by strong file-set overlap; titles and patch shape never add edges.

    Equal path sets are bucketed first. Distinct buckets use standard Jaccard
    prefix filtering ordered by global path rarity, which avoids materializing
    every pair behind a giant shared-path hotspot while retaining every pair
    that can meet ``FILE_JACCARD_CONFIRM``. PRs with no known paths remain
    independent because missing evidence is not an identical change shape.
    """
    uf = UnionFind()
    for index in range(len(prs)):
        uf.add(index)

    path_sets = [frozenset(pr.paths) for pr in prs]
    buckets: dict[frozenset[str], list[int]] = {}
    empty_indices: list[int] = []
    for index, paths in enumerate(path_sets):
        if not paths:
            empty_indices.append(index)
            continue
        buckets.setdefault(paths, []).append(index)

    bucket_items = sorted(
        buckets.items(),
        key=lambda item: (
            min(prs[index].number for index in item[1]),
            tuple(sorted(item[0])),
        ),
    )
    for _paths, members in bucket_items:
        anchor = members[0]
        for member in members[1:]:
            uf.union(anchor, member)

    path_frequency = Counter(path for paths, _members in bucket_items for path in paths)
    prefix_index: dict[str, list[int]] = defaultdict(list)
    bucket_paths = [item[0] for item in bucket_items]
    candidate_checks = 0
    size_rejections = 0

    for bucket_id, paths in enumerate(bucket_paths):
        ordered = sorted(paths, key=lambda path: (path_frequency[path], path))
        prefix_length = len(paths) - math.ceil(FILE_JACCARD_CONFIRM * len(paths)) + 1
        candidates: set[int] = set()
        for path in ordered[:prefix_length]:
            candidates.update(prefix_index[path])

        for other_id in sorted(candidates):
            other = bucket_paths[other_id]
            if not _can_reach_threshold(len(paths), len(other)):
                size_rejections += 1
                continue
            candidate_checks += 1
            if _file_jaccard(paths, other) >= FILE_JACCARD_CONFIRM:
                uf.union(bucket_items[bucket_id][1][0], bucket_items[other_id][1][0])

        for path in ordered[:prefix_length]:
            prefix_index[path].append(bucket_id)

    components = sorted(
        uf.components(),
        key=lambda component: min(prs[int(index)].number for index in component),
    )
    groups = [
        _build_group(
            f"G{group_index + 1:03d}",
            [
                prs[int(index)]
                for index in sorted(
                    component, key=lambda value: prs[int(value)].number
                )
            ],
        )
        for group_index, component in enumerate(components)
    ]
    if stats is not None:
        stats.update(
            pr_count=len(prs),
            nonempty_path_buckets=len(bucket_items),
            empty_prs=len(empty_indices),
            candidate_checks=candidate_checks,
            size_rejections=size_rejections,
        )
    return groups


def _build_group(group_id: str, members: list[PullRequest]) -> Group:
    path_sets = [set(member.paths) for member in members]
    if path_sets:
        shared = set.intersection(*path_sets)
        all_paths = set.union(*path_sets)
    else:
        shared = set()
        all_paths = set()

    return Group(
        group_id=group_id,
        pr_numbers=sorted(member.number for member in members),
        fingerprints=list(
            dict.fromkeys(member.fingerprint for member in members if member.fingerprint)
        ),
        shared_files=sorted(shared),
        title_variants=list(dict.fromkeys(member.title for member in members)),
        suggested_decision="",
        file_set_signature=file_set_signature(all_paths),
    )
