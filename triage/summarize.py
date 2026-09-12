"""Template-based group summaries. Optional LLM stub."""

from __future__ import annotations

import os

from triage.models import Group, PullRequest


def suggest_decision(group: Group, prs_by_number: dict[int, PullRequest]) -> str:
    n = len(group.pr_numbers)
    if n == 1:
        return "unique"
    # Check how similar file sets are
    members = [prs_by_number[n_] for n_ in group.pr_numbers if n_ in prs_by_number]
    if not members:
        return "related-theme"
    path_sets = [set(m.paths) for m in members]
    shared = set.intersection(*path_sets) if len(path_sets) > 1 else path_sets[0]
    union = set.union(*path_sets)
    overlap = len(shared) / len(union) if union else 0.0
    # Same fingerprint = same hunks. Shared files alone is not a duplicate fix.
    if len(set(m.fingerprint for m in members if m.fingerprint)) == 1 and members:
        return "duplicate"
    if overlap >= 0.3:
        return "related-theme"
    return "related-theme"


def summarize_group(group: Group, prs_by_number: dict[int, PullRequest], use_llm: bool = False) -> Group:
    decision = suggest_decision(group, prs_by_number)
    group.suggested_decision = decision
    shared = ", ".join(group.shared_files[:8]) or "(none)"
    titles = " | ".join(group.title_variants[:5])
    llm_note = ""
    if use_llm:
        llm_note = " " + llm_stub_note()
    group.summary = (
        f"Group {group.group_id}: {len(group.pr_numbers)} PR(s); "
        f"suggested={decision}; shared_files=[{shared}]; "
        f"titles=[{titles}].{llm_note}"
    )
    return group


def summarize_all(
    groups: list[Group],
    prs: list[PullRequest],
    use_llm: bool = False,
) -> list[Group]:
    by_num = {p.number: p for p in prs}
    return [summarize_group(g, by_num, use_llm=use_llm) for g in groups]


def llm_stub_note() -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return "[LLM stub: key present but LLM summarization not implemented in POC]"
    return "[LLM disabled: set OPENAI_API_KEY to enable; stub only]"
