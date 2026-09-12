"""End-to-end triage pipeline: ingest -> file-set group -> summarize -> queue."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from triage.assign import assign_incremental
from triage.cluster import cluster_prs
from triage.github import fetch_pulls, parse_repo
from triage.models import Group, PullRequest
from triage.queue import build_queue
from triage.store import (
    StoreConflictError,
    load_groups,
    load_rules,
    load_store,
    reserved_group_ids,
    save_run_state,
)
from triage.summarize import summarize_all

NEEDS_HUMAN_LABEL = "needs-human"

# Fixture data is package data so demo/replay work from an installed wheel,
# independently of the source checkout or current working directory.
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_prs_from_json(path: Path) -> list[PullRequest]:
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        raise TypeError(f"expected list of PRs in {path}")
    return [PullRequest.from_dict(item) for item in raw]


def ingest(
    source: str = "fixtures",
    repo: str = "omacom/omarchy",
    limit: int = 0,
    fixtures_path: Path | None = None,
    progress: dict | None = None,
    on_progress=None,
    refresh: bool = False,
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
            refresh=refresh,
        )
    if source == "github":
        owner, name = parse_repo(repo)
        # Cached reads never probe credentials or the network. The gh-backed
        # adapter's transport authenticates lazily and is not invoked by the
        # canonical cached path.
        from triage.gh import fetch_pulls_gh, gh_available

        if not refresh:
            return fetch_pulls_gh(
                owner,
                name,
                limit=limit,
                progress=progress,
                on_progress=on_progress,
                refresh=False,
            )
        # An explicit refresh prefers local gh and falls back to GITHUB_TOKEN.
        if gh_available():
            return fetch_pulls_gh(
                owner,
                name,
                limit=limit,
                progress=progress,
                on_progress=on_progress,
                refresh=refresh,
            )
        return fetch_pulls(owner, name, limit=limit, refresh=True)
    raise ValueError(f"unknown source: {source!r} (use fixtures|gh|github)")


def require_human_review(prs: list[PullRequest]) -> list[PullRequest]:
    """Reset every PR to the human-review gate; grouping never approves code."""
    for pr in prs:
        pr.label = NEEDS_HUMAN_LABEL
    return prs


def _slim_prs(
    prs: list[PullRequest],
    groups: list[Group],
    repo: str,
    source: str = "",
) -> list[dict[str, Any]]:
    gid_of = {}
    for g in groups:
        for n in g.pr_numbers:
            gid_of[n] = g.group_id
    out = []
    for p in prs:
        html = p.html_url or (f"https://github.com/{repo}/pull/{p.number}" if repo else "")
        revision = p.revision_evidence()
        item = {
            "number": p.number,
            "title": p.title,
            "user": p.user,
            "label": p.label,
            "paths": list(p.paths),
            "group_id": gid_of.get(p.number, ""),
            "html_url": html,
            "created_at": p.created_at,
            "head_sha": p.head_sha,
            "base_sha": p.base_sha,
            "updated_at": p.updated_at,
            "additions": revision.additions,
            "deletions": revision.deletions,
            "content_digest": revision.content_digest,
            "evidence_complete": revision.evidence_complete,
            "evidence_source": revision.source,
            "cache_snapshot_id": p.cache_snapshot_id,
        }
        if source.strip().lower() == "fixtures":
            # Fixture evidence has no separate immutable cache. Keep its local
            # body and exact file payload reachable for review and ranking.
            item["body"] = p.body
            item["files"] = [changed.to_dict() for changed in p.changed_files]
        out.append(item)
    return out


def run_pipeline(
    prs: list[PullRequest],
    persist: bool = True,
    store_path: Path | None = None,
    apply_rules: bool = True,
    source: str = "",
    repo: str = "",
    incremental: bool | None = None,
    _retry_count: int = 0,
) -> dict[str, Any]:
    """
    File-set pipeline. Skips TF-IDF (that hung 2.2k PRs on 8GB).
    incremental=True (default when persisting onto an existing store)
    assigns new PRs into current groups instead of reclustering.
    """
    from triage.store import DEFAULT_STORE_PATH

    path = store_path or DEFAULT_STORE_PATH
    # File-set grouping and revision evidence do not use the retired coarse
    # patch-position fingerprint. Legacy values remain readable on models.
    prs = list(prs)
    evidence_source = source.strip().lower()
    if evidence_source in {"gh", "github"}:
        evidence_source = "github"
    for pr in prs:
        if evidence_source:
            pr.evidence_source = evidence_source
    existing: list[Group] = []
    prev_nums: set[int] = set()
    data = load_store(path)
    expected_store_version = int(data.get("store_version", 0))
    expected_snapshot_version = int(data.get("snapshot_version", 0))
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
        groups = cluster_prs(prs)
        for group in groups:
            group.repo = active_repo
    if existing:
        mode = "incremental"
    else:
        mode = "full"

    groups = summarize_all(groups, prs)
    for group in groups:
        group.bind_revisions(prs)

    rules = []
    if apply_rules:
        rules = load_rules(path, repo=active_repo)
    # Classification is a safety invariant even when persisted rules are not
    # loaded: callers may reuse PR objects carrying stale auto-approval labels.
    require_human_review(prs)

    slim = _slim_prs(prs, groups, active_repo, source=source)
    overlap: dict[str, Any] = {}
    new_nums = [p.number for p in prs if p.number not in prev_nums]
    queue = build_queue(groups, prs, rules, new_pr_numbers=new_nums, repo=active_repo)

    if persist:
        try:
            save_run_state(
                groups,
                [p.number for p in prs],
                last_prs=slim,
                last_edges=[],
                last_group_edges=[],
                last_overlap={},
                source=source or None,
                repo=active_repo or None,
                path=path,
                last_new_pr_numbers=new_nums,
                last_queue=queue,
                expected_snapshot_version=expected_snapshot_version,
                expected_version=expected_store_version,
            )
        except StoreConflictError as exc:
            if (
                exc.code == "stale_store_version"
                and _retry_count < 3
            ):
                # A decision/upsert landed while this snapshot was computed.
                # Recompute against it; a newer published snapshot is never
                # retried because that could let an older fetch overwrite it.
                return run_pipeline(
                    prs,
                    persist=persist,
                    store_path=store_path,
                    apply_rules=apply_rules,
                    source=source,
                    repo=repo,
                    incremental=incremental,
                    _retry_count=_retry_count + 1,
                )
            raise

    return {
        "prs": prs,
        "groups": groups,
        "rules": rules,
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
