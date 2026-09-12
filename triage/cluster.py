"""Near-dup clustering: MinHash/LSH + union-find (default); greedy fallback for A/B."""

from __future__ import annotations

from triage.dedupe import file_set_signature, normalize_title
from triage.embed import cosine_similarity, mean_centroid
from triage.minhash import (
    DEFAULT_NUM_BANDS,
    DEFAULT_NUM_PERM,
    DEFAULT_ROWS_PER_BAND,
    estimated_jaccard,
    lsh_candidate_pairs,
    minhash_signature,
    shingles_for_pr,
)
from triage.models import Group, PullRequest
from triage.simhash import simhash
from triage.unionfind import UnionFind

# Cosine confirm threshold (kept for fixture compatibility / Embedder confirm)
DEFAULT_THRESHOLD = 0.72

# File-set confirm. Title never creates an edge.
FILE_JACCARD_CONFIRM = 0.72
CANOPY_JACCARD = 0.15  # unused by default path; kept for greedy/A-B notes
MINHASH_JACCARD_CONFIRM = 0.35
COSINE_CONFIRM = DEFAULT_THRESHOLD
TITLE_CLOSE_RATIO = 0.72
SMALL_N_SKIP_CANOPY = 40
LSH_NUM_PERM = DEFAULT_NUM_PERM
LSH_NUM_BANDS = DEFAULT_NUM_BANDS
LSH_ROWS = DEFAULT_ROWS_PER_BAND


def _file_jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / float(union) if union else 0.0


def _overlap_coefficient(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def _titles_very_close(ta: str, tb: str) -> bool:
    """Cheap token Jaccard on normalized titles."""
    sa = set(normalize_title(ta).split())
    sb = set(normalize_title(tb).split())
    if not sa or not sb:
        return False
    return _file_jaccard(sa, sb) >= TITLE_CLOSE_RATIO


def cluster_prs(
    prs: list[PullRequest],
    vectors: list[list[float]],
    threshold: float = DEFAULT_THRESHOLD,
    method: str = "lsh",
) -> list[Group]:
    """
    Default method='lsh': MinHash/LSH + union-find near-dup clustering.
    method='greedy': legacy centroid greedy (tests A/B only).
    """
    if len(prs) != len(vectors):
        raise ValueError("prs and vectors length mismatch")
    if method == "greedy":
        return _cluster_greedy(prs, vectors, threshold=threshold)
    return _cluster_lsh(prs, vectors, cosine_threshold=threshold)


def _cluster_greedy(
    prs: list[PullRequest],
    vectors: list[list[float]],
    threshold: float = DEFAULT_THRESHOLD,
) -> list[Group]:
    """Legacy greedy: max similarity to centroid >= threshold; fingerprint forced merges."""
    fp_to_cluster: dict[str, int] = {}
    clusters: list[dict] = []

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


def _cluster_lsh(
    prs: list[PullRequest],
    vectors: list[list[float]],
    cosine_threshold: float = COSINE_CONFIRM,
) -> list[Group]:
    """
    File-set clustering. Title never creates an edge.

    1. Forced unions: identical fingerprint (paths + hunk headers)
    2. Candidates: pairs that share at least one path (inverted index)
    3. Confirm: file-set Jaccard >= FILE_JACCARD_CONFIRM (0.72)
    4. Union-find confirmed pairs
    5. Build Group objects (singletons included)

    Cosine / MinHash / title-close are not used for edges. They built
    the 560-PR title-chained blob. vectors is unused here (greedy still uses it).
    """
    n = len(prs)
    uf = UnionFind()
    for i in range(n):
        uf.add(i)

    fp_members: dict[str, list[int]] = {}
    for i, pr in enumerate(prs):
        if pr.fingerprint:
            fp_members.setdefault(pr.fingerprint, []).append(i)
    for members in fp_members.values():
        for j in range(1, len(members)):
            uf.union(members[0], members[j])

    path_sets = [set(pr.paths) for pr in prs]
    by_path: dict[str, list[int]] = {}
    for i, ps in enumerate(path_sets):
        for path in ps:
            by_path.setdefault(path, []).append(i)

    seen: set[tuple[int, int]] = set()
    for indices in by_path.values():
        if len(indices) < 2:
            continue
        for ii in range(len(indices)):
            for jj in range(ii + 1, len(indices)):
                a, b = indices[ii], indices[jj]
                if a > b:
                    a, b = b, a
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                if uf.find(a) == uf.find(b):
                    continue
                if _file_jaccard(path_sets[a], path_sets[b]) >= FILE_JACCARD_CONFIRM:
                    uf.union(a, b)

    # 6. Build groups from components (order by min index for stable G001..)
    comps = uf.components()
    comps_sorted = sorted(comps, key=lambda c: min(int(x) for x in c))
    groups: list[Group] = []
    for idx, comp in enumerate(comps_sorted):
        indices = sorted(int(x) for x in comp)
        members = [prs[i] for i in indices]
        member_vecs = [vectors[i] for i in indices]
        centroid = mean_centroid(member_vecs)
        groups.append(_build_group(f"G{idx + 1:03d}", members, centroid))
    return groups


def _group_simhash(members: list[PullRequest]) -> int:
    """SimHash of shared_files + title variants + shared hunk headers (concat text)."""
    path_sets = [set(m.paths) for m in members]
    shared = set.intersection(*path_sets) if len(path_sets) > 1 else (path_sets[0] if path_sets else set())
    titles = list(dict.fromkeys(m.title for m in members))
    hunks: list[str] = []
    from triage.dedupe import extract_hunk_headers

    for m in members:
        for cf in m.changed_files:
            if cf.path in shared or not shared:
                hunks.extend(extract_hunk_headers(cf.patch))
    hunks = list(dict.fromkeys(hunks))
    text = "\n".join(sorted(shared)) + "\n" + "\n".join(titles) + "\n" + "\n".join(hunks)
    from triage.minhash import shingles_from_text

    return simhash(shingles_from_text(text, paths=shared, hunks=hunks))


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
    g_sim = _group_simhash(members)

    return Group(
        group_id=group_id,
        pr_numbers=sorted(m.number for m in members),
        fingerprints=fps,
        shared_files=sorted(shared),
        title_variants=titles,
        suggested_decision="",  # filled by summarize
        centroid=centroid,
        file_set_signature=file_set_signature(all_paths),
        simhash=g_sim,
    )
