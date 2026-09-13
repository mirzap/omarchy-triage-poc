"""Work-queue piles: needs you / known shape / junk / file hotspot."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from triage.models import (
    DISPOSITIONS,
    Disposition,
    Group,
    PullRequest,
    RevisionEvidence,
    TrustedRule,
)

HOTSPOT_MIN = 20


def _repo_key(repo: str) -> str:
    return (repo or "").strip().strip("/").lower()


def rule_applies_to_group(rule: TrustedRule, group: Group, repo: str = "") -> bool:
    """A decision applies only to its exact repository/revision snapshot."""
    active_repo = _repo_key(repo or group.repo)
    if not active_repo or _repo_key(group.repo) != active_repo:
        return False
    if _repo_key(rule.repo) != active_repo or rule.group_id != group.group_id:
        return False
    # Lists in a persisted JSON store are untrusted input.  Do not let the
    # dictionaries below collapse duplicate members/revisions and thereby
    # make a malformed group appear to match an otherwise valid decision.
    if (
        not group.pr_numbers
        or any(type(number) is not int or number <= 0 for number in group.pr_numbers)
        or len(group.pr_numbers) != len(set(group.pr_numbers))
    ):
        return False
    if not isinstance(rule.reviewed_revisions, list) or not isinstance(
        group.member_revisions, list
    ):
        return False
    reviewed = {item.pr_number: item for item in rule.reviewed_revisions}
    current = {item.pr_number: item for item in group.member_revisions}
    members = set(group.pr_numbers)
    if (
        len(reviewed) != len(rule.reviewed_revisions)
        or len(current) != len(group.member_revisions)
        or set(reviewed) != members
        or set(current) != members
    ):
        return False
    reviewed_members = rule.reviewed_pr_numbers or rule.created_from_prs
    if (
        not isinstance(reviewed_members, list)
        or any(type(number) is not int or number <= 0 for number in reviewed_members)
        or len(reviewed_members) != len(set(reviewed_members))
        or set(reviewed_members) != members
    ):
        return False
    if rule.snapshot_digest != group.snapshot_digest or reviewed != current:
        return False
    # Approvals are fail-closed. Other explicit dispositions can be recorded
    # against incomplete evidence, but still apply only to the exact snapshot.
    if rule.decision == "approve":
        return bool(
            rule.evidence_complete
            and group.evidence_complete
            and all(item.evidence_complete for item in reviewed.values())
        )
    return True


def build_queue(
    groups: list[Group],
    prs: list[PullRequest],
    rules: list[TrustedRule],
    new_pr_numbers: list[int] | None = None,
    hotspot_min: int = HOTSPOT_MIN,
    repo: str = "",
    dispositions: list[Disposition | dict[str, Any]] | None = None,
    current_revisions: dict[int, Any] | None = None,
    evidence_source: str = "",
) -> dict[str, Any]:
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
    reviewed: list[str] = []

    current_by_pr = current_revisions or {p.number: p.revision_evidence() for p in prs}
    # Local dispositions are exact-revision records.  Validate the complete
    # active set before projecting any row: duplicate/malformed identities,
    # stale canonical refs, and cycles must leave the affected PR pending.
    local_by_pr = active_dispositions(
        dispositions or [], current_by_pr, active_repo, evidence_source=evidence_source
    )

    for g in groups:
        locals_for_group = {
            number: local_by_pr[number]
            for number in g.pr_numbers
            if number in local_by_pr
            and number in current_by_pr
            and _same_revision(local_by_pr[number].revision, current_by_pr[number])
        }
        pending_members = [
            number for number in g.pr_numbers
            if number not in locals_for_group
            or locals_for_group[number].disposition == "pending"
        ]
        if locals_for_group and not pending_members:
            # The local workflow is independent from TrustedRule.  A fully
            # triaged group is exposed as reviewed, while legacy ``known``
            # remains reserved for an explicit group approval rule.
            reviewed.append(g.group_id)
            continue
        if locals_for_group:
            # A partial local review must remain visible until every member is
            # explicitly triaged, even if an older group rule would otherwise
            # place the group in a terminal pile.
            needs.append(g.group_id)
            continue
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
        "reviewed": reviewed,
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
            "reviewed": len(reviewed),
        },
    }


def _same_revision(left: Any, right: Any) -> bool:
    return all(
        getattr(left, field, None) == getattr(right, field, None)
        for field in ("pr_number", "head_sha", "base_sha", "content_digest")
    ) and (
        ("github" if str(getattr(left, "source", "")).lower() in {"gh", "github"}
         else str(getattr(left, "source", "")).lower())
        ==
        ("github" if str(getattr(right, "source", "")).lower() in {"gh", "github"}
         else str(getattr(right, "source", "")).lower())
    )


def active_dispositions(
    rows: list[Disposition | dict[str, Any]],
    current_revisions: dict[int, Any],
    repo: str,
    *,
    evidence_source: str = "",
) -> dict[int, Disposition]:
    """Return only structurally valid dispositions for exact current PRs.

    Cache snapshot IDs are provenance, not revision identity, so an unchanged
    record remains active after a refetch or regroup.  Conversely, every
    semantic field that can hide pending work is checked from the raw JSON
    before permissive model parsing can normalize it.
    """
    active_repo = _repo_key(repo)
    if not active_repo:
        return {}
    active_source = _source_key(evidence_source)
    if not active_source:
        sources = {
            _source_key(getattr(revision, "source", ""))
            for revision in current_revisions.values()
            if _source_key(getattr(revision, "source", ""))
        }
        active_source = next(iter(sources)) if len(sources) == 1 else ""
    if active_source not in {"fixtures", "github"}:
        return {}
    current: dict[int, RevisionEvidence] = {}
    for number, revision in current_revisions.items():
        if (
            type(number) is int
            and number > 0
            and isinstance(revision, RevisionEvidence)
            and revision.pr_number == number
            and _source_key(revision.source) == active_source
        ):
            current[number] = revision

    identity_counts: dict[int, int] = defaultdict(int)
    for value in rows:
        raw = value.to_dict() if isinstance(value, Disposition) else value
        if not isinstance(raw, dict) or _repo_key(raw.get("repo")) != active_repo:
            continue
        number = raw.get("pr", raw.get("pr_number"))
        if type(number) is int and number > 0:
            identity_counts[number] += 1

    candidates: dict[int, Disposition] = {}
    for value in rows:
        raw = value.to_dict() if isinstance(value, Disposition) else value
        if not isinstance(raw, dict) or _repo_key(raw.get("repo")) != active_repo:
            continue
        number = raw.get("pr", raw.get("pr_number"))
        revision_raw = raw.get("revision")
        disposition_raw = raw.get("disposition", raw.get("decision"))
        if (
            type(number) is not int
            or number <= 0
            or identity_counts[number] != 1
            or not isinstance(revision_raw, dict)
            or disposition_raw not in DISPOSITIONS
            or not isinstance(raw.get("reason"), str)
            or not 1 <= len(raw["reason"].strip()) <= 1000
        ):
            continue
        try:
            item = Disposition.from_dict(raw)
        except (TypeError, ValueError, KeyError):
            continue
        revision = current.get(number)
        if (
            revision is None
            or item.pr != number
            or item.revision.pr_number != number
            or not _same_revision(item.revision, revision)
            or (
                item.disposition in {"keep", "duplicate"}
                and (
                    not item.revision.evidence_complete
                    or not revision.evidence_complete
                )
            )
        ):
            continue
        duplicate_raw = raw.get("duplicate_of")
        target_raw = raw.get("duplicate_of_revision", raw.get("canonical_revision"))
        if item.disposition != "duplicate":
            if duplicate_raw is not None or target_raw is not None:
                continue
        else:
            if (
                type(duplicate_raw) is not int
                or duplicate_raw <= 0
                or duplicate_raw == number
                or not isinstance(target_raw, dict)
            ):
                continue
            target = current.get(duplicate_raw)
            if target is None:
                continue
            try:
                target_revision = RevisionEvidence.from_dict(target_raw)
            except (TypeError, ValueError, KeyError):
                continue
            if (
                item.duplicate_of != duplicate_raw
                or item.duplicate_of_revision is None
                or target_revision.pr_number != duplicate_raw
                or item.duplicate_of_revision.pr_number != duplicate_raw
                or not _same_revision(target_revision, target)
                or not _same_revision(item.duplicate_of_revision, target)
                or not target_revision.evidence_complete
                or not item.duplicate_of_revision.evidence_complete
                or not target.evidence_complete
            ):
                continue
        candidates[number] = item

    # A duplicate persisted identity is ambiguous.  Ignore every row for that
    # PR rather than letting ordering or timestamps decide whether work hides.
    valid = dict(candidates)
    graph = {
        number: item.duplicate_of
        for number, item in valid.items()
        if item.disposition == "duplicate" and item.duplicate_of is not None
    }
    cyclic: set[int] = set()
    for start in graph:
        order: list[int] = []
        positions: dict[int, int] = {}
        cursor: int | None = start
        while cursor in graph:
            if cursor in positions:
                cyclic.update(order[positions[cursor]:])
                break
            if cursor in cyclic:
                break
            positions[cursor] = len(order)
            order.append(cursor)
            cursor = graph[cursor]
    for number in cyclic:
        valid.pop(number, None)
    return valid


def _source_key(source: Any) -> str:
    value = str(source or "").strip().lower()
    return "github" if value in {"gh", "github"} else value
