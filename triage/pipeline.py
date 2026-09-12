"""End-to-end triage pipeline: ingest -> dedupe -> embed -> cluster -> summarize -> classify."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from triage.assign import assign_incremental
from triage.cluster import DEFAULT_THRESHOLD, cluster_prs
from triage.dedupe import apply_fingerprints, file_set_signature
from triage.embed import cosine_similarity
from triage.github import fetch_pulls, parse_repo
from triage.models import Group, PullRequest, TrustedRule
from triage.overlap import overlap_for_groups
from triage.queue import build_queue
from triage.store import load_rules, load_store, save_run_state
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


def _overlap_coefficient(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(min(len(sa), len(sb)))


def match_rule(
    pr: PullRequest,
    vector: list[float],
    rules: list[TrustedRule],
    auto_threshold: float = AUTO_MATCH_THRESHOLD,
) -> TrustedRule | None:
    """
    Match APPROVED rules: exact fingerprint OR
    (cosine >= auto_threshold AND overlapping file-set) OR
    (SimHash Hamming <= 3 AND files overlap / overlap coeff > 0).
    Rejected fingerprints still block auto-approve.
    Missing rule.simhash skips the SimHash clause (back-compat).
    """
    from triage.simhash import SIMHASH_MAX_HAMMING, hamming

    approved = [r for r in rules if r.decision == "approve"]
    rejected = [r for r in rules if r.decision == "reject"]

    # Exact fingerprint on a rejected rule: stay needs-human
    for r in rejected:
        if pr.fingerprint and pr.fingerprint in r.fingerprints:
            return None

    for r in approved:
        if pr.fingerprint and pr.fingerprint in r.fingerprints:
            return r
        if r.file_set_signature and file_set_signature(pr.paths) == r.file_set_signature:
            return r
        overlap = files_overlap(pr.paths, r.shared_files)
        sim = cosine_similarity(vector, r.centroid) if r.centroid and vector else 0.0
        if overlap and sim >= auto_threshold:
            return r
        # SimHash near-dup + file overlap
        rule_sh = getattr(r, "simhash", 0) or 0
        pr_sh = getattr(pr, "simhash", 0) or 0
        if rule_sh and pr_sh:
            ov_coeff = _overlap_coefficient(pr.paths, r.shared_files)
            if hamming(pr_sh, rule_sh) <= SIMHASH_MAX_HAMMING and (overlap or ov_coeff > 0):
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


def _slim_prs(prs: list[PullRequest], groups: list[Group], repo: str) -> list[dict[str, Any]]:
    gid_of = {}
    for g in groups:
        for n in g.pr_numbers:
            gid_of[n] = g.group_id
    out = []
    for p in prs:
        html = p.html_url or (f"https://github.com/{repo}/pull/{p.number}" if repo else "")
        out.append({
            "number": p.number,
            "title": p.title,
            "user": p.user,
            "label": p.label,
            "paths": list(p.paths),
            "group_id": gid_of.get(p.number, ""),
            "html_url": html,
            "created_at": p.created_at,
        })
    return out


def run_pipeline(
    prs: list[PullRequest],
    threshold: float = DEFAULT_THRESHOLD,
    use_llm: bool = False,
    persist: bool = True,
    store_path: Path | None = None,
    apply_rules: bool = True,
    source: str = "",
    repo: str = "",
    incremental: bool | None = None,
) -> dict[str, Any]:
    """
    File-set pipeline. Skips TF-IDF (that hung 2.2k PRs on 8GB).
    incremental=True (default when persisting onto an existing store)
    assigns new PRs into current groups instead of reclustering.
    """
    from triage.store import DEFAULT_STORE_PATH

    path = store_path or DEFAULT_STORE_PATH
    prs = apply_fingerprints(list(prs))
    dummy = [[0.0] for _ in prs]

    existing: list[Group] = []
    prev_nums: set[int] = set()
    if incremental is None:
        incremental = persist
    if incremental:
        data = load_store(path)
        raw = data.get("last_groups") or []
        prev_nums = {int(n) for n in (data.get("last_pr_numbers") or []) if n}
        if raw:
            existing = [g if isinstance(g, Group) else Group.from_dict(g) for g in raw]

    if existing:
        groups = assign_incremental(prs, existing)
        mode = "incremental"
    else:
        groups = cluster_prs(prs, dummy, threshold=threshold)
        mode = "full"

    groups = summarize_all(groups, prs, use_llm=use_llm)

    rules: list[TrustedRule] = []
    if apply_rules:
        rules = load_rules(path)
        auto_classify(prs, dummy, rules)

    slim = _slim_prs(prs, groups, repo)
    overlap = overlap_for_groups(groups, prs)
    new_nums = [p.number for p in prs if p.number not in prev_nums]
    queue = build_queue(groups, prs, rules, new_pr_numbers=new_nums)

    if persist:
        save_run_state(
            groups,
            [p.number for p in prs],
            last_prs=slim,
            last_edges=[],
            last_group_edges=[],
            last_overlap=overlap,
            source=source or None,
            repo=repo or None,
            path=path,
            last_new_pr_numbers=new_nums,
            last_queue=queue,
        )

    return {
        "prs": prs,
        "groups": groups,
        "vectors": dummy,
        "embedder": None,
        "rules": rules,
        "edges": [],
        "group_edges": [],
        "slim_prs": slim,
        "overlap": overlap,
        "queue": queue,
        "new_pr_numbers": new_nums,
        "assign_mode": mode,
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
