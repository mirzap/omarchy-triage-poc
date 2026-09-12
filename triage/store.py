"""JSON persistence for trusted rules and last-run groups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from triage.classify import classify_group
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
        "last_new_pr_numbers": [],
        "last_queue": {},
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
    data.setdefault("last_new_pr_numbers", [])
    data.setdefault("last_queue", {})
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
    last_new_pr_numbers: list[int] | None = None,
    last_queue: dict[str, Any] | None = None,
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
    if last_new_pr_numbers is not None:
        data["last_new_pr_numbers"] = last_new_pr_numbers
    if last_queue is not None:
        data["last_queue"] = last_queue
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
        simhash=int(getattr(match, "simhash", 0) or 0),
    )
    upsert_rule(rule, path)
    _refresh_queue(path)
    return rule


def _refresh_queue(path: Path = DEFAULT_STORE_PATH) -> None:
    """Rebuild last_queue after a bless/reject so the Queue tab moves."""
    from triage.overlap import slim_to_pr
    from triage.queue import build_queue

    data = load_store(path)
    groups = [Group.from_dict(g) for g in (data.get("last_groups") or [])]
    prs = [slim_to_pr(p) for p in (data.get("last_prs") or [])]
    rules = [TrustedRule.from_dict(r) for r in (data.get("trusted_rules") or [])]
    data["last_queue"] = build_queue(
        groups, prs, rules, new_pr_numbers=data.get("last_new_pr_numbers") or []
    )
    save_store(data, path)


MAX_STATE_TITLES = 4


def _slim_group_for_ui(g: dict[str, Any]) -> dict[str, Any]:
    out = dict(g)
    out["centroid"] = []
    titles = out.get("title_variants") or []
    card = classify_group(titles, out.get("shared_files") or [])
    out["card_class"] = card["card_class"]
    out["card_note"] = card["card_note"]
    if len(titles) > MAX_STATE_TITLES:
        out["title_variants"] = titles[:MAX_STATE_TITLES]
    return out


def _overlap_summary(ov: dict[str, Any]) -> dict[str, Any]:
    shared = ov.get("shared") or []
    unique = ov.get("unique") or []
    matrix = ov.get("matrix") or []
    return {
        "jaccard": ov.get("jaccard"),
        "shared": shared[:8],
        "partial": [],
        "unique": [],
        "matrix": [],
        "shared_n": len(shared),
        "unique_n": len(unique),
        "matrix_n": len(matrix),
        "lazy": True,
    }


def overlap_for_group(
    group_id: str,
    path: Path = DEFAULT_STORE_PATH,
    max_prs: int = 24,
    max_rows: int = 80,
) -> dict[str, Any]:
    """Capped overlap payload for one group. Avoids million-cell matrices."""
    data = load_store(path)
    groups = data.get("last_groups") or []
    g = next((x for x in groups if x.get("group_id") == group_id), None)
    if g is None:
        raise KeyError(f"group not found: {group_id}")
    ov = (data.get("last_overlap") or {}).get(group_id) or {}
    pr_numbers = list(g.get("pr_numbers") or [])
    shown = pr_numbers[:max_prs]
    shown_set = {str(n) for n in shown}
    matrix_in = ov.get("matrix") or []
    matrix = []
    for row in matrix_in[:max_rows]:
        prs = row.get("prs") or {}
        matrix.append(
            {
                "path": row.get("path", ""),
                "kind": row.get("kind", ""),
                "same_patch": bool(row.get("same_patch")),
                "prs": {k: v for k, v in prs.items() if k in shown_set},
            }
        )
    return {
        "group_id": group_id,
        "jaccard": ov.get("jaccard"),
        "shared": (ov.get("shared") or [])[:40],
        "partial": (ov.get("partial") or [])[:40],
        "unique": (ov.get("unique") or [])[:40],
        "matrix": matrix,
        "shared_n": len(ov.get("shared") or []),
        "unique_n": len(ov.get("unique") or []),
        "matrix_n": len(matrix_in),
        "pr_numbers": shown,
        "pr_truncated": max(0, len(pr_numbers) - len(shown)),
        "row_truncated": max(0, len(matrix_in) - len(matrix)),
        "lazy": False,
    }



def _slim_pr_for_ui(pr: dict[str, Any]) -> dict[str, Any]:
    paths = pr.get("paths") or []
    if not paths:
        files = pr.get("files") or []
        if files and isinstance(files[0], dict):
            paths = [f.get("path", "") for f in files if f.get("path")]
        elif files and isinstance(files[0], str):
            paths = list(files)
    return {
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "user": pr.get("user") or "",
        "label": pr.get("label") or "needs-human",
        "group_id": pr.get("group_id") or "",
        "html_url": pr.get("html_url") or "",
        "paths": paths[:40],
        "created_at": pr.get("created_at") or "",
    }


def prs_for_path(file_path: str, path: Path = DEFAULT_STORE_PATH, limit: int = 40) -> dict:
    """Open PRs that touch file_path (from persisted slim PRs)."""
    data = load_store(path)
    hits = []
    for pr in data.get("last_prs") or []:
        paths = pr.get("paths") or []
        if file_path in paths:
            hits.append({
                "number": pr.get("number"),
                "title": pr.get("title") or "",
                "user": pr.get("user") or "",
                "group_id": pr.get("group_id") or "",
                "html_url": pr.get("html_url") or "",
                "label": pr.get("label") or "needs-human",
            })
    hits.sort(key=lambda x: -(x["number"] or 0))
    return {
        "path": file_path,
        "pr_count": len(hits),
        "prs": hits[:limit],
        "truncated": max(0, len(hits) - limit),
    }


def ui_state(path: Path = DEFAULT_STORE_PATH) -> dict[str, Any]:
    """Slim dashboard payload. Full overlap matrices are lazy via overlap_for_group."""
    data = load_store(path)
    raw_ov = data.get("last_overlap") or {}
    overlap = {gid: _overlap_summary(ov) for gid, ov in raw_ov.items()}
    return {
        "groups": [_slim_group_for_ui(g) for g in data.get("last_groups", [])],
        "prs": [_slim_pr_for_ui(pr) for pr in data.get("last_prs", [])],
        "edges": [],
        "group_edges": [],
        "rules": data.get("trusted_rules", []),
        "overlap": overlap,
        "source": data.get("source", ""),
        "repo": data.get("repo", ""),
        "queue": data.get("last_queue") or {},
        "new_pr_numbers": data.get("last_new_pr_numbers") or [],
    }
