"""End-to-end triage pipeline: ingest -> dedupe -> embed -> cluster -> summarize -> classify."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from triage.cluster import DEFAULT_THRESHOLD, cluster_prs
from triage.dedupe import apply_fingerprints, file_set_signature
from triage.embed import Embedder, cosine_similarity
from triage.github import fetch_pulls, parse_repo
from triage.graph import build_graph_payload
from triage.models import Group, PullRequest, TrustedRule
from triage.overlap import overlap_for_groups
from triage.store import load_rules, save_run_state
from triage.summarize import summarize_all

AUTO_APPROVE_LABEL = "auto:approved-shape"
NEEDS_HUMAN_LABEL = "needs-human"
AUTO_MATCH_THRESHOLD = 0.85

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def load_prs_from_json(path: Path) -> list[PullRequest]:
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        raise ValueError(f"expected list of PRs in {path}")
    return [PullRequest.from_dict(item) for item in raw]


def ingest(
    source: str = "fixtures",
    repo: str = "omacom/omarchy",
    limit: int = 0,
    fixtures_path: Path | None = None,
    progress: dict | None = None,
    on_progress=None,
) -> list[PullRequest]:
    if source == "fixtures":
        path = fixtures_path or (FIXTURES_DIR / "prs.json")
        return load_prs_from_json(path)
    if source == "gh":
        from triage.gh import fetch_pulls_gh

        owner, name = parse_repo(repo)
        return fetch_pulls_gh(
            owner,
            name,
            limit=limit,
            progress=progress,
            on_progress=on_progress,
        )
    if source == "github":
        owner, name = parse_repo(repo)
        # Prefer local gh CLI; if unavailable, fall back to GITHUB_TOKEN urllib client
        from triage.gh import gh_available, fetch_pulls_gh

        if gh_available():
            return fetch_pulls_gh(
                owner,
                name,
                limit=limit,
                progress=progress,
                on_progress=on_progress,
            )
        return fetch_pulls(owner, name, limit=limit)
    raise ValueError(f"unknown source: {source!r} (use fixtures|gh|github)")


def files_overlap(pr_paths: list[str], rule_files: list[str]) -> bool:
    """True if PR shares at least one path with the rule's shared/file set."""
    if not rule_files:
        # Fall back to signature-only: empty shared means check any overlap via signature
        return False
    return bool(set(pr_paths) & set(rule_files))


def match_rule(
    pr: PullRequest,
    vector: list[float],
    rules: list[TrustedRule],
    auto_threshold: float = AUTO_MATCH_THRESHOLD,
) -> TrustedRule | None:
    """
    Match APPROVED rules: exact fingerprint OR
    (cosine >= auto_threshold AND overlapping file-set).
    Rejected rules are remembered so they do not auto-approve.
    """
    approved = [r for r in rules if r.decision == "approve"]
    rejected = [r for r in rules if r.decision == "reject"]

    # Exact fingerprint on a rejected rule: stay needs-human
    for r in rejected:
        if pr.fingerprint and pr.fingerprint in r.fingerprints:
            return None

    for r in approved:
        if pr.fingerprint and pr.fingerprint in r.fingerprints:
            return r
        sim = cosine_similarity(vector, r.centroid) if r.centroid and vector else 0.0
        overlap = files_overlap(pr.paths, r.shared_files)
        # Also accept matching file-set signature as overlap signal
        if not overlap and r.file_set_signature:
            overlap = file_set_signature(pr.paths) == r.file_set_signature
        if sim >= auto_threshold and overlap:
            return r
    return None


def auto_classify(
    prs: list[PullRequest],
    vectors: list[list[float]],
    rules: list[TrustedRule],
    auto_threshold: float = AUTO_MATCH_THRESHOLD,
) -> list[PullRequest]:
    for pr, vec in zip(prs, vectors):
        rule = match_rule(pr, vec, rules, auto_threshold=auto_threshold)
        if rule is not None:
            pr.label = AUTO_APPROVE_LABEL
        else:
            pr.label = NEEDS_HUMAN_LABEL
    return prs


def run_pipeline(
    prs: list[PullRequest],
    threshold: float = DEFAULT_THRESHOLD,
    use_llm: bool = False,
    persist: bool = True,
    store_path: Path | None = None,
    apply_rules: bool = True,
    source: str = "",
    repo: str = "",
) -> dict[str, Any]:
    prs = apply_fingerprints(list(prs))
    docs = [p.text_for_embed for p in prs]
    embedder = Embedder(n=3)
    vectors = embedder.fit_transform(docs)
    groups = cluster_prs(prs, vectors, threshold=threshold)
    groups = summarize_all(groups, prs, use_llm=use_llm)

    rules: list[TrustedRule] = []
    if apply_rules:
        from triage.store import DEFAULT_STORE_PATH

        path = store_path or DEFAULT_STORE_PATH
        rules = load_rules(path)
        # Re-embed against same vocab for rule centroids already stored;
        # match_rule uses stored centroids directly with current vectors.
        auto_classify(prs, vectors, rules)

    graph = build_graph_payload(prs, groups, vectors, repo=repo or None)
    overlap = overlap_for_groups(groups, prs)

    if persist:
        from triage.store import DEFAULT_STORE_PATH

        path = store_path or DEFAULT_STORE_PATH
        save_run_state(
            groups,
            [p.number for p in prs],
            last_prs=graph["prs"],
            last_edges=graph["edges"],
            last_group_edges=graph["group_edges"],
            last_overlap=overlap,
            source=source or None,
            repo=repo or None,
            path=path,
        )

    return {
        "prs": prs,
        "groups": groups,
        "vectors": vectors,
        "embedder": embedder,
        "rules": rules,
        "edges": graph["edges"],
        "group_edges": graph["group_edges"],
        "slim_prs": graph["prs"],
        "overlap": overlap,
    }


def format_groups(groups: list[Group], prs: list[PullRequest] | None = None) -> str:
    by_num = {p.number: p for p in (prs or [])}
    lines: list[str] = []
    for g in groups:
        lines.append(g.summary or f"Group {g.group_id}")
        labels = []
        for num in g.pr_numbers:
            pr = by_num.get(num)
            lab = pr.label if pr else NEEDS_HUMAN_LABEL
            labels.append(f"#{num}[{lab}]")
        lines.append(f"  PRs: {', '.join(labels)}")
        lines.append(f"  fingerprints: {len(g.fingerprints)}; filesig={g.file_set_signature}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
