"""End-to-end triage pipeline: ingest -> dedupe -> embed -> cluster -> summarize -> classify."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from triage.assign import assign_incremental
from triage.cluster import DEFAULT_THRESHOLD, cluster_prs
from triage.dedupe import apply_fingerprints
from triage.github import fetch_pulls, parse_repo
from triage.models import Group, PullRequest, TrustedRule
from triage.overlap import overlap_for_groups
from triage.queue import build_queue
from triage.store import (
    load_groups,
    load_rules,
    load_store,
    reserved_group_ids,
    save_run_state,
)
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
    """Return no approval match while Stage 1 auto-approval is disabled.

    The arguments and return type remain compatible with existing callers.  A
    fingerprint is only a hash of paths and hunk positions; it omits added and
    removed line content.  Consequently even an exact fingerprint match cannot
    safely inherit a prior approval.  File-set, embedding, and SimHash
    similarity are likewise advisory rather than approval evidence.
    """
    return None


def auto_classify(
    prs: list[PullRequest],
    vectors: list[list[float]],
    rules: list[TrustedRule],
    auto_threshold: float = AUTO_MATCH_THRESHOLD,
) -> list[PullRequest]:
    """Reset every PR to the human-review gate.

    Do not zip PRs with vectors here: vectors are optional legacy input and may
    be shorter than the PR list, while every PR must have a safe label.
    """
    for pr in prs:
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
    data = load_store(path)
    active_repo = (repo or data.get("repo") or "").strip().strip("/").lower()
    reserved_ids = reserved_group_ids(path)
    if incremental is None:
        incremental = persist
    if incremental:
        existing = load_groups(path, repo=active_repo) if active_repo else []
        prior = (data.get("pr_numbers_by_repo") or {}).get(active_repo, [])
        prev_nums = {int(n) for n in prior if n}

    if incremental or (persist and reserved_ids):
        groups = assign_incremental(
            prs,
            existing if incremental else [],
            repo=active_repo,
            reserved_group_ids=reserved_ids,
        )
    else:
        groups = cluster_prs(prs, dummy, threshold=threshold)
        for group in groups:
            group.repo = active_repo
    if existing:
        mode = "incremental"
    else:
        mode = "full"

    groups = summarize_all(groups, prs, use_llm=use_llm)

    rules: list[TrustedRule] = []
    if apply_rules:
        rules = load_rules(path, repo=active_repo)
    # Classification is a safety invariant even when persisted rules are not
    # loaded: callers may reuse PR objects carrying stale auto-approval labels.
    auto_classify(prs, dummy, rules)

    slim = _slim_prs(prs, groups, active_repo)
    overlap = overlap_for_groups(groups, prs)
    new_nums = [p.number for p in prs if p.number not in prev_nums]
    queue = build_queue(groups, prs, rules, new_pr_numbers=new_nums, repo=active_repo)

    if persist:
        save_run_state(
            groups,
            [p.number for p in prs],
            last_prs=slim,
            last_edges=[],
            last_group_edges=[],
            last_overlap=overlap,
            source=source or None,
            repo=active_repo or None,
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
