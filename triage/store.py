"""JSON persistence for trusted rules and last-run groups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from triage.models import Group, TrustedRule

DEFAULT_STORE_DIR = Path(".triage")
DEFAULT_STORE_PATH = DEFAULT_STORE_DIR / "store.json"


def _empty_store() -> dict[str, Any]:
    return {
        "trusted_rules": [],
        "last_groups": [],
        "last_pr_numbers": [],
        "last_prs": [],
        "last_edges": [],
        "last_group_edges": [],
        "last_overlap": {},
        "source": "",
        "repo": "",
    }


def ensure_store_dir(path: Path = DEFAULT_STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_store(path: Path = DEFAULT_STORE_PATH) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("trusted_rules", [])
    data.setdefault("last_groups", [])
    data.setdefault("last_pr_numbers", [])
    data.setdefault("last_prs", [])
    data.setdefault("last_edges", [])
    data.setdefault("last_group_edges", [])
    data.setdefault("last_overlap", {})
    data.setdefault("source", "")
    data.setdefault("repo", "")
    return data


def save_store(data: dict[str, Any], path: Path = DEFAULT_STORE_PATH) -> None:
    ensure_store_dir(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def save_groups(groups: list[Group], pr_numbers: list[int], path: Path = DEFAULT_STORE_PATH) -> None:
    data = load_store(path)
    data["last_groups"] = [g.to_dict() for g in groups]
    data["last_pr_numbers"] = pr_numbers
    save_store(data, path)


def save_run_state(
    groups: list[Group],
    pr_numbers: list[int],
    *,
    last_prs: list[dict[str, Any]] | None = None,
    last_edges: list[dict[str, Any]] | None = None,
    last_group_edges: list[dict[str, Any]] | None = None,
    last_overlap: dict[str, Any] | None = None,
    source: str | None = None,
    repo: str | None = None,
    path: Path = DEFAULT_STORE_PATH,
) -> None:
    """Persist groups plus slim PR/graph payload for the UI."""
    data = load_store(path)
    data["last_groups"] = [g.to_dict() for g in groups]
    data["last_pr_numbers"] = pr_numbers
    if last_prs is not None:
        data["last_prs"] = last_prs
    if last_edges is not None:
        data["last_edges"] = last_edges
    if last_group_edges is not None:
        data["last_group_edges"] = last_group_edges
    if last_overlap is not None:
        data["last_overlap"] = last_overlap
    if source is not None:
        data["source"] = source
    if repo is not None:
        data["repo"] = repo
    save_store(data, path)


def load_groups(path: Path = DEFAULT_STORE_PATH) -> list[Group]:
    data = load_store(path)
    return [Group.from_dict(g) for g in data.get("last_groups", [])]


def load_rules(path: Path = DEFAULT_STORE_PATH) -> list[TrustedRule]:
    data = load_store(path)
    return [TrustedRule.from_dict(r) for r in data.get("trusted_rules", [])]


def upsert_rule(rule: TrustedRule, path: Path = DEFAULT_STORE_PATH) -> None:
    data = load_store(path)
    rules = data.get("trusted_rules", [])
    # Replace existing rule for same group_id + decision type, or append
    replaced = False
    for i, existing in enumerate(rules):
        if existing.get("group_id") == rule.group_id:
            rules[i] = rule.to_dict()
            replaced = True
            break
    if not replaced:
        rules.append(rule.to_dict())
    data["trusted_rules"] = rules
    save_store(data, path)


def decide_group(
    group_id: str,
    decision: str,
    path: Path = DEFAULT_STORE_PATH,
) -> TrustedRule:
    if decision not in ("approve", "reject"):
        raise ValueError(f"decision must be approve|reject, got {decision!r}")
    groups = load_groups(path)
    match = next((g for g in groups if g.group_id == group_id), None)
    if match is None:
        raise KeyError(f"group not found: {group_id}")
    rule = TrustedRule(
        rule_id=f"rule-{group_id}-{decision}",
        group_id=group_id,
        decision=decision,
        fingerprints=list(match.fingerprints),
        centroid=list(match.centroid),
        file_set_signature=match.file_set_signature,
        shared_files=list(match.shared_files),
        created_from_prs=list(match.pr_numbers),
    )
    upsert_rule(rule, path)
    return rule


def ui_state(path: Path = DEFAULT_STORE_PATH) -> dict[str, Any]:
    """State payload for the local dashboard API."""
    data = load_store(path)
    overlap = data.get("last_overlap") or {}
    if not overlap and data.get("last_groups") and data.get("last_prs"):
        from triage.overlap import overlap_from_store_payload

        overlap = overlap_from_store_payload(data["last_groups"], data["last_prs"])
    return {
        "groups": data.get("last_groups", []),
        "prs": data.get("last_prs", []),
        "edges": data.get("last_edges", []),
        "group_edges": data.get("last_group_edges", []),
        "rules": data.get("trusted_rules", []),
        "overlap": overlap,
        "source": data.get("source", ""),
        "repo": data.get("repo", ""),
    }
