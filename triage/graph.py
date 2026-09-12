"""Similarity graph edges for the triage dashboard (PR and group neighborhoods)."""

from __future__ import annotations

from typing import Any

from triage.embed import cosine_similarity
from triage.models import Group, PullRequest

PR_EDGE_THRESHOLD = 0.55
GROUP_EDGE_THRESHOLD = 0.45
MAX_PR_EDGES = 400
PATCH_CAP = 8000


def compute_pr_edges(
    prs: list[PullRequest],
    vectors: list[list[float]],
    threshold: float = PR_EDGE_THRESHOLD,
    max_edges: int = MAX_PR_EDGES,
) -> list[dict[str, Any]]:
    """Pairwise cosine edges between PRs with similarity >= threshold (strongest first)."""
    if len(prs) != len(vectors):
        raise ValueError("prs and vectors length mismatch")
    candidates: list[tuple[float, int, int]] = []
    n = len(prs)
    for i in range(n):
        for j in range(i + 1, n):
            w = cosine_similarity(vectors[i], vectors[j])
            if w >= threshold:
                candidates.append((w, prs[i].number, prs[j].number))
    candidates.sort(key=lambda t: t[0], reverse=True)
    edges: list[dict[str, Any]] = []
    for w, src, tgt in candidates[:max_edges]:
        edges.append({"source": src, "target": tgt, "weight": round(w, 6)})
    return edges


def compute_group_edges(
    groups: list[Group],
    threshold: float = GROUP_EDGE_THRESHOLD,
) -> list[dict[str, Any]]:
    """Edges between groups whose centroids have cosine >= threshold."""
    candidates: list[tuple[float, str, str]] = []
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            a, b = groups[i], groups[j]
            if not a.centroid or not b.centroid:
                continue
            w = cosine_similarity(a.centroid, b.centroid)
            if w >= threshold:
                candidates.append((w, a.group_id, b.group_id))
    candidates.sort(key=lambda t: t[0], reverse=True)
    return [
        {"source": src, "target": tgt, "weight": round(w, 6)}
        for w, src, tgt in candidates
    ]


def slim_pr(pr: PullRequest, group_id: str | None = None, repo: str | None = None) -> dict[str, Any]:
    """Persistable PR summary with capped patches for the overlap/diff UI."""
    html_url = pr.html_url
    if not html_url and repo:
        html_url = f"https://github.com/{repo}/pull/{pr.number}"
    files: list[dict[str, str]] = []
    for f in pr.changed_files:
        patch = f.patch or ""
        if len(patch) > PATCH_CAP:
            patch = patch[:PATCH_CAP]
        files.append({"path": f.path, "patch": patch})
    return {
        "number": pr.number,
        "title": pr.title,
        "user": pr.user,
        "label": pr.label,
        "files": files,
        "paths": list(pr.paths),
        "group_id": group_id,
        "html_url": html_url or "",
        "created_at": pr.created_at,
    }


def build_graph_payload(
    prs: list[PullRequest],
    groups: list[Group],
    vectors: list[list[float]],
    repo: str | None = None,
) -> dict[str, Any]:
    """Full graph payload for UI + store."""
    num_to_group: dict[int, str] = {}
    for g in groups:
        for n in g.pr_numbers:
            num_to_group[n] = g.group_id
    slim = [slim_pr(p, group_id=num_to_group.get(p.number), repo=repo) for p in prs]
    edges = compute_pr_edges(prs, vectors)
    group_edges = compute_group_edges(groups)
    return {
        "prs": slim,
        "edges": edges,
        "group_edges": group_edges,
    }
