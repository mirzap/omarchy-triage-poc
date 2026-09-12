"""Work-queue piles: needs you / known shape / junk / file hotspot."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from triage.models import Group, PullRequest, TrustedRule

HOTSPOT_MIN = 20
JUNK_PATHS = {
    "readme.md",
    "package-lock.json",
    "package.json",
    "docs/community.md",
}
JUNK_TITLE_BITS = ("discord", "invite to", "typo in readme")


def _is_junk_pr(pr: PullRequest) -> bool:
    title = (pr.title or "").lower()
    if any(bit in title for bit in JUNK_TITLE_BITS):
        return True
    paths = [p.lower() for p in pr.paths]
    if paths and all(p in JUNK_PATHS or p.endswith("package-lock.json") for p in paths):
        return True
    return False


def build_queue(
    groups: list[Group],
    prs: list[PullRequest],
    rules: list[TrustedRule],
    new_pr_numbers: list[int] | None = None,
    hotspot_min: int = HOTSPOT_MIN,
) -> dict[str, Any]:
    by_num = {p.number: p for p in prs}
    new_set = set(new_pr_numbers or [])
    rule_by_gid = {r.group_id: r for r in rules}

    needs: list[str] = []
    known: list[str] = []
    junk: list[str] = []

    for g in groups:
        members = [by_num[n] for n in g.pr_numbers if n in by_num]
        rule = rule_by_gid.get(g.group_id)
        if rule and rule.decision == "approve":
            known.append(g.group_id)
            continue
        if rule and rule.decision == "reject":
            junk.append(g.group_id)
            continue
        if members and all(m.label == "auto:approved-shape" for m in members):
            known.append(g.group_id)
            continue
        if len(members) == 1 and _is_junk_pr(members[0]):
            junk.append(g.group_id)
            continue
        if len(g.pr_numbers) >= 2:
            needs.append(g.group_id)
            continue
        if any(n in new_set for n in g.pr_numbers):
            needs.append(g.group_id)
            continue
        # old singleton, no rule: backlog, not today's queue

    by_path: dict[str, list[int]] = defaultdict(list)
    for p in prs:
        for path in p.paths:
            by_path[path].append(p.number)
    hotspots = []
    for path, nums in by_path.items():
        uniq = sorted(set(nums), reverse=True)
        if len(uniq) >= hotspot_min:
            hotspots.append({"path": path, "pr_count": len(uniq), "pr_numbers": uniq[:40]})
    hotspots.sort(key=lambda h: -h["pr_count"])

    return {
        "needs_you": needs,
        "known": known,
        "junk": junk,
        "hotspots": hotspots[:40],
        "new_pr_numbers": sorted(new_set, reverse=True),
        "counts": {
            "needs_you": len(needs),
            "known": len(known),
            "junk": len(junk),
            "hotspots": len(hotspots),
            "new": len(new_set),
        },
    }
