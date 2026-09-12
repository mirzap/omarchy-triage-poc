"""Stage 1 safety, identity, cache, and concurrency regressions."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from triage import gh, rank, store
from triage.cli import main as cli_main
from triage.dedupe import compute_fingerprint, file_set_signature
from triage.models import ChangedFile, Group, PullRequest, TrustedRule
from triage.pipeline import NEEDS_HUMAN_LABEL, auto_classify, match_rule, run_pipeline
from triage.queue import build_queue
from triage.server import make_handler
from triage.store import (
    decide_group,
    load_rules,
    load_store,
    save_groups,
    save_run_state,
    save_store,
    ui_state,
    upsert_rule,
)


def _pr(
    number: int,
    path: str = "config/example.conf",
    patch: str = "@@ -1 +1 @@\n-old\n+new\n",
    *,
    label: str = NEEDS_HUMAN_LABEL,
) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"change {number}",
        body="",
        user="fixture",
        changed_files=[ChangedFile(path=path, patch=patch)],
        created_at="2026-09-01T00:00:00Z",
        label=label,
    )


def _group(group_id: str, numbers: list[int], repo: str) -> Group:
    return Group(
        group_id=group_id,
        pr_numbers=numbers,
        fingerprints=[f"fp-{n}" for n in numbers],
        shared_files=["config/example.conf"],
        file_set_signature="filesig",
        repo=repo,
    )


def _rule(group_id: str, numbers: list[int], repo: str, decision: str = "approve") -> TrustedRule:
    return TrustedRule(
        rule_id=f"rule-{group_id}-{decision}",
        group_id=group_id,
        decision=decision,
        fingerprints=[f"fp-{n}" for n in numbers],
        centroid=[],
        file_set_signature="filesig",
        shared_files=["config/example.conf"],
        created_from_prs=numbers,
        repo=repo,
        reviewed_pr_numbers=numbers,
    )


def _slim(pr: PullRequest, group_id: str) -> dict[str, Any]:
    return {
        "number": pr.number,
        "title": pr.title,
        "user": pr.user,
        "label": pr.label,
        "paths": pr.paths,
        "group_id": group_id,
        "html_url": "",
        "created_at": pr.created_at,
    }


def _pull_item(number: int, revision: str) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "user": {"login": "fixture"},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": f"2026-09-0{revision}T00:00:00Z",
        "head": {"sha": f"sha-{number}-{revision}"},
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
    }


def test_legacy_fingerprint_is_advisory_and_all_labels_reset(tmp_path: Path) -> None:
    added = _pr(1, patch="@@ -8,1 +8,1 @@\n-old\n+safe\n")
    opposite = _pr(2, patch="@@ -8,1 +8,1 @@\n-safe\n+old\n")
    different_hunk = _pr(3, patch="@@ -80,1 +80,1 @@\n-old\n+safe\n")
    added.fingerprint = compute_fingerprint(added)
    opposite.fingerprint = compute_fingerprint(opposite)
    different_hunk.fingerprint = compute_fingerprint(different_hunk)
    assert added.fingerprint == opposite.fingerprint
    assert different_hunk.fingerprint != added.fingerprint

    result = run_pipeline(
        [added, opposite],
        persist=False,
        store_path=tmp_path / "store.json",
        apply_rules=False,
    )
    assert result["groups"][0].suggested_decision == "related-theme"

    approve = _rule("G001", [1], "acme/widgets", "approve")
    approve.fingerprints = [added.fingerprint]
    approve.file_set_signature = file_set_signature(added.paths)
    reject = _rule("G002", [2], "acme/widgets", "reject")
    reject.fingerprints = [opposite.fingerprint]
    reject.file_set_signature = file_set_signature(opposite.paths)
    rules = [approve, reject]
    assert match_rule(added, [], [approve]) is None
    assert match_rule(opposite, [], [approve]) is None
    assert match_rule(different_hunk, [], [approve]) is None
    assert match_rule(opposite, [], [reject]) is None
    added.label = "auto:approved-shape"
    opposite.label = "already-reviewed"
    different_hunk.label = "auto:approved-shape"
    reset = run_pipeline(
        [added, opposite, different_hunk],
        persist=False,
        store_path=tmp_path / "reset.json",
        apply_rules=False,
    )
    assert [p.label for p in reset["prs"]] == [
        NEEDS_HUMAN_LABEL,
        NEEDS_HUMAN_LABEL,
        NEEDS_HUMAN_LABEL,
    ]
    assert [p.label for p in auto_classify(reset["prs"], [], rules)] == [
        NEEDS_HUMAN_LABEL,
        NEEDS_HUMAN_LABEL,
        NEEDS_HUMAN_LABEL,
    ]


@pytest.mark.parametrize("unsafe_patch", ["", "@@ -1 +1 @@\n+x\n… truncated"])
def test_missing_or_truncated_patches_are_never_duplicate_advice(
    tmp_path: Path, unsafe_patch: str
) -> None:
    result = run_pipeline(
        [_pr(1, patch=unsafe_patch), _pr(2, patch=unsafe_patch)],
        persist=False,
        store_path=tmp_path / "store.json",
        apply_rules=False,
    )
    assert result["groups"][0].suggested_decision != "duplicate"


def test_pipeline_preserves_unrelated_store_data(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    data = load_store(path)
    data["sentinel"] = {"never": "wipe"}
    data["trusted_rules"] = [_rule("G099", [99], "other/repo").to_dict()]
    save_store(data, path)

    run_pipeline([_pr(1)], persist=True, store_path=path, repo="acme/widgets")
    after = load_store(path)
    assert after["sentinel"] == {"never": "wipe"}
    assert any(r["group_id"] == "G099" for r in after["trusted_rules"])


def test_repository_scope_blocks_foreign_known_rule_and_reserves_ids(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    first = run_pipeline([_pr(1)], persist=True, store_path=path, repo="omacom/omarchy")
    assert first["groups"][0].group_id == "G001"
    decide_group("G001", "approve", path=path)

    second = run_pipeline([_pr(2)], persist=True, store_path=path, repo="acme/widgets")
    assert second["groups"][0].group_id != "G001"
    assert second["groups"][0].group_id in second["queue"]["needs_you"]
    assert second["queue"]["known"] == []
    assert load_rules(path, repo="acme/widgets") == []


def test_disappeared_and_rule_only_group_ids_stay_reserved(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    run_pipeline([_pr(1)], persist=True, store_path=path, repo="acme/widgets")
    run_pipeline([], persist=True, store_path=path, repo="acme/widgets")
    upsert_rule(_rule("G007", [7], "other/repo"), path)

    result = run_pipeline(
        [_pr(8, "totally/new.path")],
        persist=True,
        store_path=path,
        repo="acme/widgets",
    )
    assert result["groups"][0].group_id not in {"G001", "G007"}
    assert {"G001", "G007"}.issubset(set(load_store(path)["reserved_group_ids"]))


def test_unknown_legacy_scope_remains_explicitly_unscoped(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    legacy_group = _group("G001", [1], "").to_dict()
    legacy_rule = _rule("G001", [1], "").to_dict()
    legacy_group.pop("repo")
    legacy_rule.pop("repo")
    raw = {
        "repo": "",
        "last_groups": [legacy_group],
        "last_pr_numbers": [1],
        "last_prs": [_slim(_pr(1), "G001")],
        "trusted_rules": [legacy_rule],
    }
    path.write_text(json.dumps(raw), encoding="utf-8")

    run_pipeline([_pr(2)], persist=True, store_path=path, repo="new/repo")
    upsert_rule(_rule("G001", [2], "new/repo"), path)
    rules = load_store(path)["trusted_rules"]
    assert sorted(r["repo"] for r in rules) == ["", "new/repo"]
    assert next(r for r in rules if r["repo"] == "")["group_id"] == "G001"


def test_known_legacy_records_bind_only_to_original_omarchy_repo(tmp_path: Path) -> None:
    path = tmp_path / "legacy-omarchy.json"
    legacy_group = _group("G001", [1], "").to_dict()
    legacy_rule = _rule("G001", [1], "").to_dict()
    legacy_group.pop("repo")
    legacy_rule.pop("repo")
    path.write_text(
        json.dumps(
            {
                "repo": "omacom/omarchy",
                "last_groups": [legacy_group],
                "last_pr_numbers": [1],
                "last_prs": [_slim(_pr(1), "G001")],
                "trusted_rules": [legacy_rule],
            }
        ),
        encoding="utf-8",
    )
    materialized = load_store(path)
    assert materialized["last_groups"][0]["repo"] == "omacom/omarchy"
    assert materialized["trusted_rules"][0]["repo"] == "omacom/omarchy"
    assert [r.group_id for r in load_rules(path, repo="omacom/omarchy")] == ["G001"]
    assert load_rules(path, repo="other/repo") == []


def test_save_groups_inherits_active_repo_and_decision_becomes_known(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    first = _pr(1)
    save_run_state(
        [_group("G001", [1], "a/r")],
        [1],
        last_prs=[_slim(first, "G001")],
        repo="a/r",
        path=path,
    )
    second_group = _group("G002", [2], "")
    save_groups([second_group], [2], path)
    assert load_store(path)["last_groups"][0]["repo"] == "a/r"

    # Supply the current slim member as a normal run would before deciding.
    second = _pr(2)
    data = load_store(path)
    data["last_prs"] = [_slim(second, "G002")]
    save_store(data, path)
    decide_group("G002", "approve", path=path)
    assert ui_state(path)["queue"]["known"] == ["G002"]


def test_new_member_returns_reviewed_group_to_needs_you_and_ui_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.json"
    old = _pr(1)
    run_pipeline([old], persist=True, store_path=path, repo="acme/widgets")
    decide_group("G001", "approve", path=path)
    new = _pr(2)
    result = run_pipeline([old, new], persist=True, store_path=path, repo="acme/widgets")
    assert result["groups"][0].pr_numbers == [1, 2]
    assert result["queue"]["known"] == []
    assert result["queue"]["needs_you"] == ["G001"]

    stale = load_store(path)
    stale["last_queue"] = {"known": ["G001"], "needs_you": []}
    save_store(stale, path)
    visible = ui_state(path)
    assert visible["queue"]["known"] == []
    assert visible["queue"]["needs_you"] == ["G001"]
    assert visible["rules"] == []


def test_cache_namespaces_that_previously_collided_are_distinct(tmp_path: Path) -> None:
    assert gh._cache_root("a-b", "c", tmp_path) != gh._cache_root("a", "b-c", tmp_path)
    assert gh._cache_root("a-b", "c", tmp_path) == tmp_path / "a-b" / "c"


def test_every_undecided_singleton_is_durable_including_lockfiles() -> None:
    prs = [
        _pr(1, "package-lock.json"),
        _pr(2, ".github/workflows/security.yml"),
        _pr(3, "ordinary.txt"),
    ]
    groups = [_group(f"G00{i}", [i], "acme/widgets") for i in range(1, 4)]
    queue = build_queue(groups, prs, [], new_pr_numbers=[], repo="acme/widgets")
    assert queue["needs_you"] == ["G001", "G002", "G003"]
    assert queue["counts"]["needs_you"] == 3


def test_hotspots_keep_full_counts_and_lists_beyond_ui_caps() -> None:
    paths = [f"hot/{i}.conf" for i in range(41)]
    prs = [
        PullRequest(
            number=n,
            title=str(n),
            body="",
            user="fixture",
            changed_files=[ChangedFile(path=p) for p in paths],
            created_at="",
        )
        for n in range(1, 42)
    ]
    queue = build_queue([], prs, [], hotspot_min=20)
    assert len(queue["hotspots"]) == 41
    assert queue["counts"]["hotspots"] == 41
    assert all(h["pr_count"] == 41 and len(h["pr_numbers"]) == 41 for h in queue["hotspots"])


def test_demo_default_and_fixture_commands_never_touch_live_store(
    tmp_path: Path,
) -> None:
    live = tmp_path / ".triage" / "store.json"
    live.parent.mkdir()
    live.write_text("SENTINEL", encoding="utf-8")

    assert cli_main(["demo"]) == 0
    demo = tmp_path / ".triage" / "demo-store.json"
    assert demo.exists()
    assert live.read_text(encoding="utf-8") == "SENTINEL"

    # Fixture run and replay may reuse their isolated demo store, never live.
    assert cli_main(["run", "--source", "fixtures", "--store", str(tmp_path / "run.json")]) == 0
    assert cli_main(["replay", "--store", str(tmp_path / "replay.json")]) == 0
    assert live.read_text(encoding="utf-8") == "SENTINEL"


def test_demo_existing_store_reset_rules_and_live_alias_guard(tmp_path: Path) -> None:
    named = tmp_path / "named.json"
    named.write_text("KEEP", encoding="utf-8")
    assert cli_main(["demo", "--store", str(named)]) == 2
    assert named.read_text(encoding="utf-8") == "KEEP"
    assert cli_main(["demo", "--reset"]) == 2
    assert cli_main(["demo", "--store", str(named), "--reset"]) == 0

    live = tmp_path / ".triage" / "store.json"
    live.parent.mkdir(exist_ok=True)
    live.write_text("LIVE", encoding="utf-8")
    alias = tmp_path / "live-alias.json"
    alias.symlink_to(live)
    assert cli_main(["demo", "--store", str(alias), "--reset"]) == 2
    assert live.read_text(encoding="utf-8") == "LIVE"


def test_concurrent_decisions_and_upserts_are_full_read_modify_write(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    p1, p2 = _pr(1), _pr(2, "config/other.conf")
    save_run_state(
        [_group("G001", [1], "acme/widgets"), _group("G002", [2], "acme/widgets")],
        [1, 2],
        last_prs=[_slim(p1, "G001"), _slim(p2, "G002")],
        repo="acme/widgets",
        path=path,
    )
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def decide(group_id: str) -> None:
        try:
            barrier.wait()
            decide_group(group_id, "approve", path=path)
        except BaseException as exc:  # pragma: no cover - assertion reports worker failures
            errors.append(exc)

    threads = [threading.Thread(target=decide, args=(gid,)) for gid in ("G001", "G002")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert errors == []
    assert {r.group_id for r in load_rules(path)} == {"G001", "G002"}

    upsert_barrier = threading.Barrier(3)
    threads = [
        threading.Thread(
            target=lambda r=rule: (upsert_barrier.wait(), upsert_rule(r, path)),
        )
        for rule in (_rule("G003", [3], "acme/widgets"), _rule("G004", [4], "acme/widgets"))
    ]
    for thread in threads:
        thread.start()
    upsert_barrier.wait()
    for thread in threads:
        thread.join()
    assert {r.group_id for r in load_rules(path)} == {"G001", "G002", "G003", "G004"}
    json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("operation", ["decide", "upsert"])
def test_rule_mutations_hold_outer_lock_across_read_modify_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A forced first mutation cannot be interleaved by a second mutation."""
    path = tmp_path / "store.json"
    p1, p2 = _pr(1), _pr(2, "config/other.conf")
    save_run_state(
        [_group("G001", [1], "acme/widgets"), _group("G002", [2], "acme/widgets")],
        [1, 2],
        last_prs=[_slim(p1, "G001"), _slim(p2, "G002")],
        repo="acme/widgets",
        path=path,
    )

    real_mutate = store._upsert_rule_in_data
    first_inside = threading.Event()
    release_first = threading.Event()
    second_inside = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def controlled_mutate(data: dict[str, Any], rule: TrustedRule) -> None:
        nonlocal call_count
        with call_lock:
            call_count += 1
            ordinal = call_count
        if ordinal == 1:
            first_inside.set()
            assert release_first.wait(2), "test did not release first mutation"
        else:
            second_inside.set()
        real_mutate(data, rule)

    monkeypatch.setattr(store, "_upsert_rule_in_data", controlled_mutate)
    errors: list[BaseException] = []

    def invoke(group_id: str) -> None:
        try:
            if operation == "decide":
                decide_group(group_id, "approve", path=path)
            else:
                upsert_rule(_rule(group_id, [int(group_id[1:])], "acme/widgets"), path)
        except BaseException as exc:  # pragma: no cover - assertion reports worker failures
            errors.append(exc)

    first = threading.Thread(target=invoke, args=("G001",))
    second = threading.Thread(target=invoke, args=("G002",))
    first.start()
    assert first_inside.wait(2), "first mutation did not reach controlled interleave"
    second.start()
    assert not second_inside.wait(0.1), "second mutation entered while first owned outer lock"
    release_first.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert second_inside.is_set()
    assert {r.group_id for r in load_rules(path)} == {"G001", "G002"}


def test_atomic_store_failure_preserves_old_bytes_and_cleans_same_dir_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "store.json"
    save_store({"version": "old"}, path)
    old = path.read_bytes()
    attempted: list[Path] = []

    def fail_replace(src: os.PathLike[str], _dst: os.PathLike[str]) -> None:
        attempted.append(Path(src))
        raise OSError("injected replace failure")

    monkeypatch.setattr(store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        store._atomic_write_json({"version": "new"}, path)
    assert path.read_bytes() == old
    assert len(attempted) == 1
    assert attempted[0].parent == path.parent
    assert not attempted[0].exists()


def test_atomic_store_serialization_failure_preserves_old_bytes_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "store.json"
    save_store({"version": "old"}, path)
    old = path.read_bytes()
    real_named_temp = store.tempfile.NamedTemporaryFile
    temps: list[Path] = []

    def recording_temp(*args: Any, **kwargs: Any):
        fh = real_named_temp(*args, **kwargs)
        temps.append(Path(fh.name))
        return fh

    def partial_dump(_data: Any, fh: Any, **_kwargs: Any) -> None:
        fh.write('{"partial":')
        raise TypeError("injected serialization failure")

    monkeypatch.setattr(store.tempfile, "NamedTemporaryFile", recording_temp)
    monkeypatch.setattr(store.json, "dump", partial_dump)
    with pytest.raises(TypeError, match="injected"):
        store._atomic_write_json({"version": "new"}, path)
    assert path.read_bytes() == old
    assert len(temps) == 1
    assert temps[0].parent == path.parent
    assert not temps[0].exists()


def test_atomic_store_and_rank_writers_use_unique_temps_and_leave_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    sources: list[Path] = []
    source_lock = threading.Lock()

    def recording_replace(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        with source_lock:
            sources.append(Path(src))
        real_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", recording_replace)
    path = tmp_path / "store.json"
    threads = [threading.Thread(target=save_store, args=({"writer": i}, path)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({p.name for p in sources}) == 4
    assert all(p.parent == path.parent for p in sources)
    json.loads(path.read_text(encoding="utf-8"))

    sources.clear()
    rank_path = tmp_path / "rank.json"
    threads = [
        threading.Thread(
            target=rank.save_cache,
            args=({"model": "m", "dim": 1, "items": {str(i): {"vec": [i]}}}, rank_path),
        )
        for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({p.name for p in sources}) == 4
    assert all(p.parent == rank_path.parent for p in sources)
    json.loads(rank_path.read_text(encoding="utf-8"))


def test_gh_refresh_revisions_status_and_partial_to_full_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh, "ensure_gh_auth", lambda: None)
    listing = [[_pull_item(1, "1"), _pull_item(2, "1")]]
    calls: list[str] = []

    def fake_api(endpoint: str, **_kwargs: Any) -> Any:
        calls.append(endpoint)
        if "/files" not in endpoint:
            return list(listing[-1])
        number = int(endpoint.split("/pulls/")[1].split("/")[0])
        return [{"filename": f"p{number}.txt", "patch": f"patch-{number}-{len(calls)}"}]

    monkeypatch.setattr(gh, "run_gh_api", fake_api)
    progress: dict[str, Any] = {}
    first = gh.fetch_pulls_gh("acme", "widgets", limit=1, cache_dir=tmp_path, progress=progress)
    assert [p.number for p in first] == [1]
    assert len(json.loads((tmp_path / "acme" / "widgets" / "pulls.json").read_text())) == 2
    first_call_count = len(calls)

    cached_progress: dict[str, Any] = {}
    gh.fetch_pulls_gh("acme", "widgets", limit=1, cache_dir=tmp_path, progress=cached_progress)
    assert len(calls) == first_call_count
    assert cached_progress["cache_status"] == "cached_stale"
    assert cached_progress["fetched_at"]

    listing.append([_pull_item(1, "2"), _pull_item(2, "1")])
    refreshed = gh.fetch_pulls_gh("acme", "widgets", limit=1, cache_dir=tmp_path, refresh=True)
    assert "patch-1" in refreshed[0].changed_files[0].patch
    assert sum("/files" not in call for call in calls) == 2
    assert sum("/pulls/1/files" in call for call in calls) == 2

    full = gh.fetch_pulls_gh("acme", "widgets", limit=0, cache_dir=tmp_path)
    assert [p.number for p in full] == [1, 2]
    assert sum("/pulls/2/files" in call for call in calls) == 1

    gh.fetch_pulls_gh("acme", "widgets", limit=1, cache_dir=tmp_path, force=True)
    assert sum("/files" not in call for call in calls) == 3


def test_failed_gh_refresh_leaves_cache_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh, "ensure_gh_auth", lambda: None)
    fail = False
    list_calls = 0

    def fake_api(endpoint: str, **_kwargs: Any) -> Any:
        nonlocal fail, list_calls
        if "/files" in endpoint:
            return [{"filename": "a.txt", "patch": "patch"}]
        list_calls += 1
        if fail:
            fail = False
            raise gh.GhError("temporary listing failure")
        return [_pull_item(1, "1")]

    monkeypatch.setattr(gh, "run_gh_api", fake_api)
    gh.fetch_pulls_gh("acme", "widgets", cache_dir=tmp_path)
    cached_bytes = (tmp_path / "acme" / "widgets" / "pulls.json").read_bytes()
    fail = True
    with pytest.raises(gh.GhError, match="temporary"):
        gh.fetch_pulls_gh("acme", "widgets", cache_dir=tmp_path, refresh=True)
    assert (tmp_path / "acme" / "widgets" / "pulls.json").read_bytes() == cached_bytes
    gh.fetch_pulls_gh("acme", "widgets", cache_dir=tmp_path, refresh=True)
    assert list_calls == 3


def test_cached_files_reject_unknown_legacy_patch_against_canonical_revision(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "omacom" / "omarchy"
    canonical.mkdir(parents=True)
    canonical.joinpath("pulls.json").write_text(
        json.dumps([_pull_item(1, "2")]), encoding="utf-8"
    )
    legacy = tmp_path / "omacom-omarchy" / "files"
    legacy.mkdir(parents=True)
    legacy.joinpath("1.json").write_text(
        json.dumps([{"filename": "a.txt", "patch": "OLDPATCH"}]), encoding="utf-8"
    )
    assert gh.cached_pr_files("omacom", "omarchy", 1, cache_dir=tmp_path) == []


def test_legacy_only_omarchy_cache_is_readable(tmp_path: Path) -> None:
    legacy = tmp_path / "omacom-omarchy"
    (legacy / "files").mkdir(parents=True)
    legacy.joinpath("pulls.json").write_text(json.dumps([{"number": 1, "title": "old"}]), encoding="utf-8")
    legacy.joinpath("files", "1.json").write_text(
        json.dumps([{"filename": "a.txt", "patch": "legacy"}]), encoding="utf-8"
    )
    assert gh.cached_pr_meta("omacom", "omarchy", 1, cache_dir=tmp_path)["title"] == "old"
    assert gh.cached_pr_files("omacom", "omarchy", 1, cache_dir=tmp_path) == [
        {"path": "a.txt", "patch": "legacy"}
    ]


def test_cache_indexes_invalidate_on_create_and_atomic_replace(tmp_path: Path) -> None:
    root = tmp_path / "acme" / "widgets"
    root.mkdir(parents=True)
    assert gh.cached_pr_meta("acme", "widgets", 1, cache_dir=tmp_path) is None
    pulls = root / "pulls.json"
    pulls.write_text(json.dumps([_pull_item(1, "1")]), encoding="utf-8")
    assert gh.cached_pr_meta("acme", "widgets", 1, cache_dir=tmp_path)["title"] == "PR 1"

    replacement = root / "replacement.json"
    replacement.write_text(json.dumps([_pull_item(2, "1")]), encoding="utf-8")
    os.replace(replacement, pulls)
    assert gh.cached_pr_meta("acme", "widgets", 1, cache_dir=tmp_path) is None
    assert gh.cached_pr_meta("acme", "widgets", 2, cache_dir=tmp_path)["title"] == "PR 2"


def test_related_endpoint_parses_numeric_pr_and_stays_lazy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[int, str | None]] = []

    def fake_related(number: int, *, file_path: str | None, store_path: Path) -> dict[str, Any]:
        seen.append((number, file_path))
        return {"enabled": False, "query": number, "related": []}

    monkeypatch.setattr("triage.server.related", fake_related)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path / "store.json"))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/api/related?pr=12&path=config%2Fx.conf")
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert payload["query"] == 12
        assert seen == [(12, "config/x.conf")]
    finally:
        httpd.shutdown()
        httpd.server_close()

    app_js = Path(__file__).parents[1] / "triage" / "web" / "app.js"
    source = app_js.read_text(encoding="utf-8")
    assert "function loadRelatedIfOpen" in source
    assert "if (!block || !block.open)" in source


def test_web_selected_item_helper_and_deferred_patch_race_nonbrowser() -> None:
    app_js = Path(__file__).parents[1] / "triage" / "web" / "app.js"
    probe = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const renderStart = source.indexOf("  async function renderDiffs(");
const helperStart = source.indexOf("  function takeWithSelected(", renderStart);
const helperEnd = source.indexOf("\n  function escapeHtml(", helperStart);
assert(renderStart >= 0 && helperStart > renderStart && helperEnd > helperStart);
const functions = source.slice(renderStart, helperEnd);
let resolvePatches;
const deferred = new Promise((resolve) => { resolvePatches = resolve; });
const root = { innerHTML: "initial", querySelector() { return null; } };
const context = {
  diffGen: 0,
  state: { selectedFile: "old.conf", selectedPr: 10,
    fileQueue: { prs: Array.from({length: 12}, (_, i) => ({number: i + 1})) },
    repo: "fixture/offline" },
  $: (id) => id === "diffPanels" ? root : { textContent: "" },
  api: () => deferred, escapeHtml: String, scrollToDiff: () => {},
  prByNumber: (number) => ({number, paths: ["old.conf"], user: "fixture"}),
  encodeURIComponent, Set,
};
vm.runInNewContext(
  `let diffGen = globalThis.diffGen; ${functions}; globalThis.renderDiffs = renderDiffs; globalThis.takeWithSelected = takeWithSelected;`,
  context
);
assert.deepStrictEqual(Array.from(context.takeWithSelected([1,2,3,4,5,6,7,8,9,10], 10, 8)), [1,2,3,4,5,6,7,10]);
(async () => {
  const stale = context.renderDiffs(null);
  context.state.selectedFile = null;
  await context.renderDiffs(null);
  assert.match(root.innerHTML, /Pick a file/);
  resolvePatches({ items: [{ number: 10, patch: "same", complete: true }] });
  await stale;
  assert.match(root.innerHTML, /Pick a file/);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", probe, str(app_js)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_web_pr_body_cache_is_repo_scoped_and_snapshot_clears_file_rows() -> None:
    app_js = Path(__file__).parents[1] / "triage" / "web" / "app.js"
    probe = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
function extract(startNeedle, endNeedle) {
  const start = source.indexOf(startNeedle);
  const end = source.indexOf(endNeedle, start);
  assert(start >= 0 && end > start);
  return source.slice(start, end);
}
const snapshotFn = extract("  function snapshotKey()", "\n  function invalidateSnapshotCaches");
const bodyFn = extract("  async function loadPrBody(", "\n  function renderPrBodies(");
const calls = [];
const context = {
  state: {source: "gh", repo: "owner/a"}, bodyCache: {}, snapshotGen: 1,
  encodeURIComponent,
  api: async (url) => { calls.push(url); return {url}; },
};
vm.runInNewContext(
  `${snapshotFn}\n${bodyFn}\nglobalThis.loadPrBody = loadPrBody;`, context
);
(async () => {
  const a = await context.loadPrBody(12);
  context.state.repo = "owner/b";
  const b = await context.loadPrBody(12);
  assert.notStrictEqual(a.url, b.url);
  assert.strictEqual(calls.length, 2);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", probe, str(app_js)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    source = app_js.read_text(encoding="utf-8")
    apply_start = source.index("  function applyState(data)")
    apply_end = source.index("\n  function ruleFor(", apply_start)
    apply_body = source[apply_start:apply_end]
    invalidate_start = source.index("  function invalidateSnapshotCaches()")
    invalidate_end = source.index("\n  function applyState(data)", invalidate_start)
    invalidate_body = source[invalidate_start:invalidate_end]
    assert "invalidateSnapshotCaches();" in apply_body
    assert "state.fileQueue = null;" in invalidate_body
    assert "fileQueueGen += 1;" in invalidate_body
