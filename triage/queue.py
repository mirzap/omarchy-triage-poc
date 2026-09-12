"""Work-queue piles: needs you / known shape / junk / file hotspot."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from triage.models import Group, PullRequest, TrustedRule

HOTSPOT_MIN = 20


def _repo_key(repo: str) -> str:
    return (repo or "").strip().strip("/").lower()


def rule_applies_to_group(rule: TrustedRule, group: Group, repo: str = "") -> bool:
    """A decision applies only to its repository and reviewed membership."""
    active_repo = _repo_key(repo or group.repo)
    if not active_repo or _repo_key(group.repo) != active_repo:
        return False
    if _repo_key(rule.repo) != active_repo or rule.group_id != group.group_id:
        return False
    reviewed = rule.reviewed_members
    current = {int(n) for n in group.pr_numbers}
    return bool(current) and current.issubset(reviewed)


def build_queue(
    groups: list[Group],
    prs: list[PullRequest],
    rules: list[TrustedRule],
    new_pr_numbers: list[int] | None = None,
    hotspot_min: int = HOTSPOT_MIN,
    repo: str = "",
) -> dict[str, Any]:
    by_num = {p.number: p for p in prs}
    new_set = set(new_pr_numbers or [])
    active_repo = _repo_key(repo)
    if not active_repo:
        recorded_repos = {_repo_key(g.repo) for g in groups if _repo_key(g.repo)}
        if len(recorded_repos) == 1:
            active_repo = recorded_repos.pop()
    rule_by_gid = {
        r.group_id: r
        for r in rules
        if _repo_key(r.repo) == active_repo
    }

    needs: list[str] = []
    known: list[str] = []
    junk: list[str] = []
    hardware: list[str] = []
    upgrade: list[str] = []

    for g in groups:
        members = [by_num[n] for n in g.pr_numbers if n in by_num]
        candidate = rule_by_gid.get(g.group_id)
        rule = (
            candidate
            if candidate and rule_applies_to_group(candidate, g, active_repo)
            else None
        )
        if rule and rule.decision == "approve":
            known.append(g.group_id)
            continue
        if rule and rule.decision == "reject":
            junk.append(g.group_id)
            continue
        if rule and rule.decision == "hardware":
            hardware.append(g.group_id)
            continue
        if rule and rule.decision == "upgrade":
            upgrade.append(g.group_id)
            continue
        # Recency is a badge/filter, not queue eligibility. Every group without
        # a trusted or automatic decision remains in the durable Needs you pile.
        needs.append(g.group_id)

    by_path: dict[str, list[int]] = defaultdict(list)
    for p in prs:
        for path in p.paths:
            by_path[path].append(p.number)
    hotspots = []
    for path, nums in by_path.items():
        uniq = sorted(set(nums), reverse=True)
        if len(uniq) >= hotspot_min:
            hotspots.append({"path": path, "pr_count": len(uniq), "pr_numbers": uniq})
    hotspots.sort(key=lambda h: -h["pr_count"])

    return {
        "needs_you": needs,
        "known": known,
        "junk": junk,
        "hardware": hardware,
        "upgrade": upgrade,
        "hotspots": hotspots,
        "new_pr_numbers": sorted(new_set, reverse=True),
        "counts": {
            "needs_you": len(needs),
            "known": len(known),
            "junk": len(junk),
            "hardware": len(hardware),
            "upgrade": len(upgrade),
            "hotspots": len(hotspots),
            "new": len(new_set),
        },
    }
