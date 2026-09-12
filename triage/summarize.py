"""Template-based advisory group summaries."""

from __future__ import annotations

from triage.models import Group, PullRequest


def suggest_decision(group: Group, prs_by_number: dict[int, PullRequest]) -> str:
    n = len(group.pr_numbers)
    if n == 1:
        return "unique"
    members = [prs_by_number[n_] for n_ in group.pr_numbers if n_ in prs_by_number]
    if not members:
        return "related-theme"
    revisions = [member.revision_evidence() for member in members]
    if (
        len(members) == n
        and all(revision.evidence_complete for revision in revisions)
        and len({revision.content_digest for revision in revisions}) == 1
    ):
        return "duplicate"
    return "related-theme"


def summarize_group(group: Group, prs_by_number: dict[int, PullRequest]) -> Group:
    decision = suggest_decision(group, prs_by_number)
    group.suggested_decision = decision
    shared = ", ".join(group.shared_files[:8]) or "(none)"
    titles = " | ".join(group.title_variants[:5])
    group.summary = (
        f"Group {group.group_id}: {len(group.pr_numbers)} PR(s); "
        f"suggested={decision}; shared_files=[{shared}]; "
        f"titles=[{titles}]."
    )
    return group


def summarize_all(
    groups: list[Group],
    prs: list[PullRequest],
) -> list[Group]:
    by_num = {p.number: p for p in prs}
    return [summarize_group(g, by_num) for g in groups]
