"""Greedy agglomerative clustering with static-exact forced merges."""

from __future__ import annotations

from triage.dedupe import file_set_signature
from triage.embed import cosine_similarity, mean_centroid
from triage.models import Group, PullRequest

DEFAULT_THRESHOLD = 0.72


def cluster_prs(
    prs: list[PullRequest],
    vectors: list[list[float]],
    threshold: float = DEFAULT_THRESHOLD,
) -> list[Group]:
    """
    Greedy clustering: if max similarity to a cluster centroid >= threshold, join;
    else start a new cluster. Static-exact fingerprint matches always share a cluster.
    """
    if len(prs) != len(vectors):
        raise ValueError("prs and vectors length mismatch")

    # Map fingerprint -> cluster index for forced merges
    fp_to_cluster: dict[str, int] = {}
    clusters: list[dict] = []  # {indices, centroid, fingerprints}

    for i, (pr, vec) in enumerate(zip(prs, vectors)):
        if pr.fingerprint and pr.fingerprint in fp_to_cluster:
            cidx = fp_to_cluster[pr.fingerprint]
            clusters[cidx]["indices"].append(i)
            member_vecs = [vectors[j] for j in clusters[cidx]["indices"]]
            clusters[cidx]["centroid"] = mean_centroid(member_vecs)
            continue

        best_idx = -1
        best_sim = -1.0
        for cidx, cluster in enumerate(clusters):
            sim = cosine_similarity(vec, cluster["centroid"])
            if sim > best_sim:
                best_sim = sim
                best_idx = cidx

        if best_idx >= 0 and best_sim >= threshold:
            clusters[best_idx]["indices"].append(i)
            member_vecs = [vectors[j] for j in clusters[best_idx]["indices"]]
            clusters[best_idx]["centroid"] = mean_centroid(member_vecs)
            if pr.fingerprint:
                fp_to_cluster[pr.fingerprint] = best_idx
                clusters[best_idx]["fingerprints"].add(pr.fingerprint)
        else:
            new_idx = len(clusters)
            fps = {pr.fingerprint} if pr.fingerprint else set()
            clusters.append(
                {
                    "indices": [i],
                    "centroid": list(vec),
                    "fingerprints": fps,
                }
            )
            if pr.fingerprint:
                fp_to_cluster[pr.fingerprint] = new_idx

    groups: list[Group] = []
    for idx, cluster in enumerate(clusters):
        members = [prs[i] for i in cluster["indices"]]
        groups.append(_build_group(f"G{idx + 1:03d}", members, cluster["centroid"]))
    return groups


def _build_group(group_id: str, members: list[PullRequest], centroid: list[float]) -> Group:
    path_sets = [set(m.paths) for m in members]
    if path_sets:
        shared = set.intersection(*path_sets) if len(path_sets) > 1 else path_sets[0]
        all_paths = set.union(*path_sets)
    else:
        shared = set()
        all_paths = set()

    titles = list(dict.fromkeys(m.title for m in members))
    fps = list(dict.fromkeys(m.fingerprint for m in members if m.fingerprint))

    return Group(
        group_id=group_id,
        pr_numbers=sorted(m.number for m in members),
        fingerprints=fps,
        shared_files=sorted(shared),
        title_variants=titles,
        suggested_decision="",  # filled by summarize
        centroid=centroid,
        file_set_signature=file_set_signature(all_paths),
    )
