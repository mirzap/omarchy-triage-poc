"""JSON persistence for trusted rules and last-run groups."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


from triage.classify import classify_group
from triage.models import Group, TrustedRule

DEFAULT_STORE_DIR = Path(".triage")
DEFAULT_STORE_PATH = DEFAULT_STORE_DIR / "store.json"
_STORE_LOCK = threading.RLock()


def _repo_key(repo: str | None) -> str:
    return (repo or "").strip().strip("/").lower()


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
        "groups_by_repo": {},
        "pr_numbers_by_repo": {},
        "reserved_group_ids": [],
        "reserved_rule_ids": [],
    }


def _materialize_identity(data: dict[str, Any]) -> dict[str, Any]:
    """Give legacy records their original repository before any repo switch."""
    data.setdefault("groups_by_repo", {})
    data.setdefault("pr_numbers_by_repo", {})
    data.setdefault("reserved_group_ids", [])
    data.setdefault("reserved_rule_ids", [])

    original_repo = _repo_key(data.get("repo"))

    normalized_groups: dict[str, list[dict[str, Any]]] = {}
    for stored_repo, records in data["groups_by_repo"].items():
        scoped_repo = _repo_key(stored_repo)
        if not scoped_repo:
            continue
        for raw in records:
            raw.setdefault("repo", scoped_repo)
        normalized_groups[scoped_repo] = records
    data["groups_by_repo"] = normalized_groups
    data["pr_numbers_by_repo"] = {
        _repo_key(stored_repo): numbers
        for stored_repo, numbers in data["pr_numbers_by_repo"].items()
        if _repo_key(stored_repo)
    }

    for raw in data.get("last_groups") or []:
        # A missing field is legacy data eligible for the store's original
        # repository context.  Once materialized, an explicit empty value means
        # provenance is unknown and must remain fail-closed across repo swaps.
        if "repo" not in raw:
            raw["repo"] = original_repo
    for raw in data.get("trusted_rules") or []:
        if "repo" not in raw:
            raw["repo"] = original_repo
        raw.setdefault("reviewed_pr_numbers", list(raw.get("created_from_prs") or []))

    if original_repo and data.get("last_groups"):
        data["groups_by_repo"][original_repo] = list(data["last_groups"])
        data["pr_numbers_by_repo"][original_repo] = list(data.get("last_pr_numbers") or [])

    group_ids = set(data.get("reserved_group_ids") or [])
    rule_ids = set(data.get("reserved_rule_ids") or [])
    group_ids.update(g.get("group_id", "") for g in data.get("last_groups") or [])
    for records in data["groups_by_repo"].values():
        group_ids.update(g.get("group_id", "") for g in records)
    for raw in data.get("trusted_rules") or []:
        group_ids.add(raw.get("group_id", ""))
        rule_ids.add(raw.get("rule_id", ""))
    data["reserved_group_ids"] = sorted(x for x in group_ids if x)
    data["reserved_rule_ids"] = sorted(x for x in rule_ids if x)
    return data


def ensure_store_dir(path: Path = DEFAULT_STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_store(path: Path = DEFAULT_STORE_PATH) -> dict[str, Any]:
    with _STORE_LOCK:
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
        return _materialize_identity(data)


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync after publishing a replacement file."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Some filesystems do not support syncing directory descriptors.  The
        # file itself was synced before the atomic replacement.
        pass
    finally:
        os.close(fd)


def _atomic_write_json(data: dict[str, Any], path: Path) -> None:
    ensure_store_dir(path)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            temp_path = Path(fh.name)
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
        temp_path = None
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def save_store(data: dict[str, Any], path: Path = DEFAULT_STORE_PATH) -> None:
    with _STORE_LOCK:
        _atomic_write_json(data, path)


def save_groups(groups: list[Group], pr_numbers: list[int], path: Path = DEFAULT_STORE_PATH) -> None:
    save_run_state(groups, pr_numbers, path=path)


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
    with _STORE_LOCK:
        data = load_store(path)
        active_repo = _repo_key(repo if repo is not None else data.get("repo"))
        for g in groups:
            g.repo = active_repo
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
            data["repo"] = active_repo
        if last_new_pr_numbers is not None:
            data["last_new_pr_numbers"] = last_new_pr_numbers
        if last_queue is not None:
            data["last_queue"] = last_queue
        if active_repo:
            data["groups_by_repo"][active_repo] = list(data["last_groups"])
            data["pr_numbers_by_repo"][active_repo] = list(pr_numbers)
        _materialize_identity(data)
        save_store(data, path)


def _groups_from_data(data: dict[str, Any], repo: str | None = None) -> list[Group]:
    wanted = _repo_key(repo)
    raw = data.get("last_groups", [])
    if wanted:
        raw = (data.get("groups_by_repo") or {}).get(wanted, [])
    return [Group.from_dict(g) for g in raw]


def load_groups(path: Path = DEFAULT_STORE_PATH, repo: str | None = None) -> list[Group]:
    return _groups_from_data(load_store(path), repo)


def _rules_from_data(
    data: dict[str, Any], repo: str | None = None
) -> list[TrustedRule]:
    rules = [TrustedRule.from_dict(r) for r in data.get("trusted_rules", [])]
    wanted = _repo_key(repo if repo is not None else data.get("repo"))
    return [r for r in rules if _repo_key(r.repo) == wanted]


def load_rules(path: Path = DEFAULT_STORE_PATH, repo: str | None = None) -> list[TrustedRule]:
    return _rules_from_data(load_store(path), repo)


def reserved_group_ids(path: Path = DEFAULT_STORE_PATH) -> set[str]:
    return set(load_store(path).get("reserved_group_ids") or [])


def _upsert_rule_in_data(data: dict[str, Any], rule: TrustedRule) -> None:
    if not rule.reviewed_pr_numbers:
        rule.reviewed_pr_numbers = list(rule.created_from_prs)
    rules = data.get("trusted_rules", [])
    # Replace existing rule for same group_id + decision type, or append
    replaced = False
    for i, existing in enumerate(rules):
        if (
            existing.get("group_id") == rule.group_id
            and _repo_key(existing.get("repo")) == _repo_key(rule.repo)
        ):
            rules[i] = rule.to_dict()
            replaced = True
            break
    if not replaced:
        rules.append(rule.to_dict())
    data["trusted_rules"] = rules
    _materialize_identity(data)


def upsert_rule(rule: TrustedRule, path: Path = DEFAULT_STORE_PATH) -> None:
    with _STORE_LOCK:
        data = load_store(path)
        _upsert_rule_in_data(data, rule)
        save_store(data, path)


def decide_group(
    group_id: str,
    decision: str,
    path: Path = DEFAULT_STORE_PATH,
) -> TrustedRule:
    if decision not in ("approve", "reject", "hardware", "upgrade"):
        raise ValueError(
            f"decision must be approve|reject|hardware|upgrade, got {decision!r}"
        )
    with _STORE_LOCK:
        data = load_store(path)
        repo = _repo_key(data.get("repo"))
        groups = _groups_from_data(data, repo=repo)
        match = next((g for g in groups if g.group_id == group_id), None)
        if match is None:
            raise KeyError(f"group not found: {group_id}")
        existing_rule = next(
            (
                raw
                for raw in data.get("trusted_rules") or []
                if raw.get("group_id") == group_id
                and _repo_key(raw.get("repo")) == repo
            ),
            None,
        )
        rule_id = (
            str(existing_rule.get("rule_id"))
            if existing_rule
            else f"rule-{group_id}-{decision}"
        )
        if not existing_rule and rule_id in set(data.get("reserved_rule_ids") or []):
            base = rule_id
            suffix = 2
            while rule_id in set(data.get("reserved_rule_ids") or []):
                rule_id = f"{base}-{suffix}"
                suffix += 1
        rule = TrustedRule(
            rule_id=rule_id,
            group_id=group_id,
            decision=decision,
            fingerprints=list(match.fingerprints),
            centroid=list(match.centroid),
            file_set_signature=match.file_set_signature,
            shared_files=list(match.shared_files),
            created_from_prs=list(match.pr_numbers),
            simhash=int(getattr(match, "simhash", 0) or 0),
            repo=repo,
            reviewed_pr_numbers=list(match.pr_numbers),
        )
        _upsert_rule_in_data(data, rule)
        _refresh_queue_in_data(data)
        save_store(data, path)
        return rule


def _refresh_queue_in_data(data: dict[str, Any]) -> None:
    from triage.overlap import slim_to_pr
    from triage.queue import build_queue

    groups = [Group.from_dict(g) for g in (data.get("last_groups") or [])]
    prs = [slim_to_pr(p) for p in (data.get("last_prs") or [])]
    repo = _repo_key(data.get("repo"))
    rules = _rules_from_data(data, repo=repo)
    data["last_queue"] = build_queue(
        groups,
        prs,
        rules,
        new_pr_numbers=data.get("last_new_pr_numbers") or [],
        repo=repo,
    )


def _refresh_queue(path: Path = DEFAULT_STORE_PATH) -> None:
    """Rebuild last_queue after a local decision so the Queue tab moves."""
    with _STORE_LOCK:
        data = load_store(path)
        _refresh_queue_in_data(data)
        save_store(data, path)


MAX_STATE_TITLES = 4


def _slim_group_for_ui(
    g: dict[str, Any],
    member_paths: list[str] | None = None,
) -> dict[str, Any]:
    out = dict(g)
    out["centroid"] = []
    titles = out.get("title_variants") or []
    paths = list(member_paths or out.get("shared_files") or [])
    card = classify_group(titles, paths)
    out["card_class"] = card["card_class"]
    out["card_note"] = card["card_note"]
    fps = [f for f in (out.get("fingerprints") or []) if f]
    if out.get("suggested_decision") == "duplicate" and len(set(fps)) > 1:
        out["suggested_decision"] = "related-theme"
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
    paths_by_gid: dict[str, list[str]] = {}
    for pr in data.get("last_prs") or []:
        gid = pr.get("group_id") or ""
        if not gid:
            continue
        bucket = paths_by_gid.setdefault(gid, [])
        for member_path in pr.get("paths") or []:
            if member_path and member_path not in bucket:
                bucket.append(member_path)
    repo = _repo_key(data.get("repo"))
    group_models = [Group.from_dict(g) for g in data.get("last_groups", [])]
    from triage.queue import rule_applies_to_group

    active_rules = [
        rule.to_dict()
        for rule in _rules_from_data(data, repo=repo)
        if any(rule_applies_to_group(rule, group, repo) for group in group_models)
    ]
    # Derive the visible queue from the scoped rules on every read.  This
    # prevents a legacy persisted queue (including stale auto labels) from
    # exposing a foreign or no-longer-applicable decision before the next run.
    from triage.overlap import slim_to_pr
    from triage.queue import build_queue

    queue = build_queue(
        group_models,
        [slim_to_pr(pr) for pr in data.get("last_prs") or []],
        [TrustedRule.from_dict(rule) for rule in active_rules],
        new_pr_numbers=data.get("last_new_pr_numbers") or [],
        repo=repo,
    )
    return {
        "groups": [
            _slim_group_for_ui(g, paths_by_gid.get(g.get("group_id") or ""))
            for g in data.get("last_groups", [])
        ],
        "prs": [_slim_pr_for_ui(pr) for pr in data.get("last_prs", [])],
        "edges": [],
        "group_edges": [],
        "rules": active_rules,
        "overlap": overlap,
        "source": data.get("source", ""),
        "repo": data.get("repo", ""),
        "queue": queue,
        "new_pr_numbers": data.get("last_new_pr_numbers") or [],
    }
