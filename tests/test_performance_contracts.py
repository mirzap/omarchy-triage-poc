"""Deterministic file-set and retired-runtime contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

import triage.cli as cli_module
from scripts.benchmark_pipeline import (
    EXPECTED_MEMBERSHIP_SHA256,
    corpus,
    hotspot_corpus,
)
from triage import gh
from triage.assign import assign_incremental
from triage.cli import main as cli_main
from triage.cluster import _build_group, cluster_prs
from triage.dedupe import apply_fingerprints
from triage.models import ChangedFile, PullRequest
from triage.pipeline import NEEDS_HUMAN_LABEL, ingest, run_pipeline


def _pr(number: int, paths: list[str]) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"PR {number}",
        body="",
        user="fixture",
        created_at="2026-01-01",
        changed_files=[
            ChangedFile(path, "@@ -1 +1 @@\n-old\n+new") for path in paths
        ],
    )


def _members(groups) -> list[list[int]]:
    return sorted(sorted(group.pr_numbers) for group in groups)


def test_file_set_output_and_ids_are_deterministic() -> None:
    prs = apply_fingerprints(
        [
            _pr(10, ["a", "b", "c", "d"]),
            _pr(20, ["a", "b", "c", "d", "e"]),
            _pr(30, ["a", "b", "x", "y"]),
            _pr(40, []),
            _pr(50, []),
        ]
    )
    first = cluster_prs(prs)
    second = cluster_prs(prs)
    assert _members(first) == [[10, 20], [30], [40], [50]]
    assert [(group.group_id, group.pr_numbers) for group in first] == [
        (group.group_id, group.pr_numbers) for group in second
    ]
    assert [group.group_id for group in first] == ["G001", "G002", "G003", "G004"]
    reversed_groups = cluster_prs(list(reversed(prs)))
    assert [(group.group_id, group.pr_numbers) for group in reversed_groups] == [
        (group.group_id, group.pr_numbers) for group in first
    ]


def test_incremental_tie_breaks_by_stable_group_id_not_pr_input_order() -> None:
    first = _pr(1, ["a", "b", "c", "d"])
    second = _pr(2, ["a", "b", "c", "e"])
    newcomer = _pr(3, ["a", "b", "c"])
    groups = [_build_group("G001", [first]), _build_group("G002", [second])]
    for group, member in zip(groups, (first, second)):
        group.repo = "acme/widgets"
        group.bind_revisions([member])

    normal = assign_incremental(
        [first, second, newcomer], groups, repo="acme/widgets"
    )
    reversed_input = assign_incremental(
        [second, first, newcomer], list(reversed(groups)), repo="acme/widgets"
    )
    assert [(group.group_id, group.pr_numbers) for group in normal] == [
        ("G001", [1, 3]),
        ("G002", [2]),
    ]
    assert [(group.group_id, group.pr_numbers) for group in reversed_input] == [
        (group.group_id, group.pr_numbers) for group in normal
    ]


def test_synthetic_membership_regression_hash() -> None:
    result = run_pipeline(
        corpus(),
        persist=False,
        incremental=False,
        repo="synthetic/benchmark",
    )
    digest = hashlib.sha256(json.dumps(_members(result["groups"])).encode()).hexdigest()
    assert digest == EXPECTED_MEMBERSHIP_SHA256
    assert len(result["groups"]) == 200
    assert set(result).isdisjoint({"vectors", "embedder", "edges", "group_edges"})
    assert all(pr.label == NEEDS_HUMAN_LABEL for pr in result["prs"])
    assert all(not pr.fingerprint for pr in result["prs"])


def test_giant_shared_path_uses_bounded_candidate_work() -> None:
    stats: dict[str, int] = {}
    groups = cluster_prs(apply_fingerprints(hotspot_corpus()), stats=stats)
    assert len(groups) == 2_200
    assert stats["candidate_checks"] == 0
    assert stats["nonempty_path_buckets"] == 2_200


def test_retired_runtime_modules_are_absent() -> None:
    for name in ("triage.embed", "triage.graph", "triage.minhash", "triage.simhash"):
        assert importlib.util.find_spec(name) is None


def test_cached_mode_never_promotes_missing_cache_to_network(tmp_path: Path) -> None:
    calls = 0

    def forbidden_transport(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("network transport called")

    with pytest.raises(gh.GhError, match="No cached GitHub snapshot"):
        gh.fetch_pulls_with_transport(
            "acme",
            "widgets",
            transport=forbidden_transport,
            cache_dir=tmp_path,
            refresh=False,
        )
    assert calls == 0


def test_github_cached_ingest_does_not_probe_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = _pr(1, ["cached"])

    def cached(owner, repo, **kwargs):
        assert (owner, repo) == ("acme", "widgets")
        assert kwargs["refresh"] is False
        return [marker]

    monkeypatch.setattr(gh, "fetch_pulls_gh", cached)
    monkeypatch.setattr(
        gh,
        "gh_available",
        lambda: (_ for _ in ()).throw(AssertionError("auth probe called")),
    )
    assert ingest(source="github", repo="acme/widgets", refresh=False) == [marker]


def test_demo_reset_is_isolated_and_recoverable(tmp_path: Path) -> None:
    store = tmp_path / "demo.json"
    assert cli_main(["demo", "--store", str(store)]) == 0
    original = store.read_bytes()
    assert cli_main(["demo", "--store", str(store)]) == 2
    assert store.read_bytes() == original
    assert cli_main(["demo", "--store", str(store), "--reset"]) == 0
    assert store.with_name("demo.json.bak").exists()


def test_cli_rejects_source_mismatch_before_ingest_and_preserves_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "workspace.json"
    run_pipeline(
        [_pr(1, ["a"])],
        persist=True,
        store_path=store,
        source="github",
        repo="acme/widgets",
    )
    before = store.read_bytes()
    monkeypatch.setattr(
        cli_module,
        "ingest",
        lambda **_kwargs: pytest.fail("source mismatch reached ingest"),
    )
    assert cli_main(
        [
            "run",
            "--source",
            "fixtures",
            "--repo",
            "acme/widgets",
            "--store",
            str(store),
        ]
    ) == 2
    assert cli_main(["replay", "--store", str(store)]) == 2
    assert cli_main(["demo", "--store", str(store), "--reset"]) == 2
    assert store.read_bytes() == before
