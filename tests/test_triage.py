"""Tests for Omarchy PR group-triage POC (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from triage.cluster import cluster_prs
from triage.dedupe import apply_fingerprints, compute_fingerprint, normalize_title, static_groups
from triage.embed import Embedder, cosine_similarity
from triage.github import GitHubError, github_request
from triage.models import ChangedFile, PullRequest
from triage.pipeline import (
    AUTO_APPROVE_LABEL,
    FIXTURES_DIR,
    NEEDS_HUMAN_LABEL,
    ingest,
    load_prs_from_json,
    match_rule,
    run_pipeline,
)
from triage.store import decide_group, load_rules, save_groups
from triage.cli import main as cli_main

HYPRLAND_NUMS = {101, 108, 115}
STRANGER_NUM = 999
NEWCOMER_MATCH_NUM = 120


@pytest.fixture
def fixture_prs() -> list[PullRequest]:
    return load_prs_from_json(FIXTURES_DIR / "prs.json")


@pytest.fixture
def newcomers() -> list[PullRequest]:
    return load_prs_from_json(FIXTURES_DIR / "newcomers.json")


@pytest.fixture
def tmp_store(tmp_path: Path) -> Path:
    return tmp_path / "store.json"


def test_static_exact_fingerprint_collapse(fixture_prs: list[PullRequest]) -> None:
    # Two PRs with identical title-norm + same files/hunks should share fingerprint
    a = fixture_prs[0]
    twin = PullRequest(
        number=99901,
        title=a.title.upper() + "!!!",
        body="different body text entirely",
        user="other",
        changed_files=[ChangedFile(path=f.path, patch=f.patch) for f in a.changed_files],
        created_at="2026-09-01T00:00:00Z",
    )
    # Title is not part of the fingerprint — same files+hunks collapse.
    twin.title = "completely different title"
    apply_fingerprints([a, twin])
    assert compute_fingerprint(a) == compute_fingerprint(twin)

    apply_fingerprints(fixture_prs)
    groups = static_groups(fixture_prs)
    # At least some multi-member potential; nvidia pair shares files+similar titles
    # Exact collapse: create two with same fingerprint
    fp_counts = {fp: len(members) for fp, members in groups.items()}
    assert all(len(fp) == 32 for fp in groups)
    assert sum(fp_counts.values()) == len(fixture_prs)


def test_normalize_title() -> None:
    assert normalize_title("Fix Hyprland Keybind!!!") == "fix hyprland keybind"


def test_hyprland_trio_clusters_together(fixture_prs: list[PullRequest]) -> None:
    result = run_pipeline(fixture_prs, persist=False, apply_rules=False)
    groups = result["groups"]
    hypr_group = None
    for g in groups:
        if HYPRLAND_NUMS.issubset(set(g.pr_numbers)):
            hypr_group = g
            break
    assert hypr_group is not None, f"hyprland PRs not co-clustered: {[g.pr_numbers for g in groups]}"
    assert set(hypr_group.pr_numbers) >= HYPRLAND_NUMS


def test_stranger_does_not_join_hyprland(fixture_prs: list[PullRequest], newcomers: list[PullRequest]) -> None:
    stranger = next(p for p in newcomers if p.number == STRANGER_NUM)
    combined = fixture_prs + [stranger]
    result = run_pipeline(combined, persist=False, apply_rules=False)
    for g in result["groups"]:
        nums = set(g.pr_numbers)
        if HYPRLAND_NUMS & nums:
            assert STRANGER_NUM not in nums


def test_approve_then_auto_classify(
    fixture_prs: list[PullRequest],
    newcomers: list[PullRequest],
    tmp_store: Path,
) -> None:
    result = run_pipeline(
        fixture_prs,
        persist=True,
        store_path=tmp_store,
        apply_rules=False,
    )
    groups = result["groups"]
    hypr = next(g for g in groups if HYPRLAND_NUMS.issubset(set(g.pr_numbers)))
    rule = decide_group(hypr.group_id, "approve", path=tmp_store)
    assert rule.decision == "approve"
    assert load_rules(tmp_store)

    from triage.dedupe import apply_fingerprints
    from triage.embed import Embedder, mean_centroid

    combined = apply_fingerprints(list(fixture_prs) + list(newcomers))
    vectors = Embedder(n=3).fit_transform([p.text_for_embed for p in combined])
    member_vecs = [vectors[i] for i, p in enumerate(combined) if p.number in HYPRLAND_NUMS]
    rule.centroid = mean_centroid(member_vecs)
    from triage.store import upsert_rule

    upsert_rule(rule, tmp_store)
    rules = load_rules(tmp_store)

    match_pr = next(p for p in combined if p.number == NEWCOMER_MATCH_NUM)
    stranger = next(p for p in combined if p.number == STRANGER_NUM)
    match_idx = next(i for i, p in enumerate(combined) if p.number == NEWCOMER_MATCH_NUM)
    stranger_idx = next(i for i, p in enumerate(combined) if p.number == STRANGER_NUM)

    assert match_rule(match_pr, vectors[match_idx], rules) is not None
    assert match_rule(stranger, vectors[stranger_idx], rules) is None

    match_pr.label = AUTO_APPROVE_LABEL if match_rule(match_pr, vectors[match_idx], rules) else NEEDS_HUMAN_LABEL
    stranger.label = AUTO_APPROVE_LABEL if match_rule(stranger, vectors[stranger_idx], rules) else NEEDS_HUMAN_LABEL
    assert match_pr.label == AUTO_APPROVE_LABEL
    assert stranger.label == NEEDS_HUMAN_LABEL


def test_github_client_refuses_non_get() -> None:
    with pytest.raises(GitHubError, match="Refusing non-GET"):
        github_request("POST", "https://api.github.com/repos/x/y/pulls")
    with pytest.raises(GitHubError, match="Refusing non-GET"):
        github_request("PATCH", "https://api.github.com/repos/x/y/pulls/1")
    with pytest.raises(GitHubError, match="Refusing non-GET"):
        github_request("DELETE", "https://api.github.com/repos/x/y/pulls/1")
    with pytest.raises(GitHubError, match="Refusing non-GET"):
        github_request("PUT", "https://api.github.com/repos/x/y/issues/1/labels")
    with pytest.raises(GitHubError, match="Refusing non-GET"):
        github_request("MERGE", "https://api.github.com/repos/x/y/pulls/1/merge")


def test_github_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    from triage.github import fetch_pulls

    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        fetch_pulls("omacom", "omarchy", limit=1)


def test_demo_and_pipeline_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / ".triage" / "store.json"
    monkeypatch.chdir(tmp_path)
    # Point CLI default store via --store
    rc = cli_main(["demo", "--store", str(store)])
    assert rc == 0
    assert store.exists()
    rules = load_rules(store)
    assert any(r.decision == "approve" for r in rules)

    prs = ingest(source="fixtures")
    assert len(prs) == 12
    result = run_pipeline(prs, persist=False, apply_rules=False)
    assert len(result["groups"]) >= 4


def test_embeddings_deterministic(fixture_prs: list[PullRequest]) -> None:
    docs = [p.text_for_embed for p in fixture_prs]
    a = Embedder(n=3).fit_transform(docs)
    b = Embedder(n=3).fit_transform(docs)
    assert a == b
    # Hyprland pair should be more similar than hyprland vs stranger paths
    apply_fingerprints(fixture_prs)
    h0 = next(i for i, p in enumerate(fixture_prs) if p.number == 101)
    h1 = next(i for i, p in enumerate(fixture_prs) if p.number == 108)
    noise = next(i for i, p in enumerate(fixture_prs) if p.number == 901)
    assert cosine_similarity(a[h0], a[h1]) > cosine_similarity(a[h0], a[noise])


# --- gh client, graph, server extensions ---

from triage.gh import GhError, validate_gh_argv, fetch_pulls_gh
from triage.graph import compute_pr_edges, build_graph_payload, slim_pr
from triage.overlap import file_overlap, hash_patch
from triage.server import run_fetch, ensure_initial_fixtures
from triage.store import ui_state, load_store


def test_gh_refuses_merge_and_mutating_methods() -> None:
    with pytest.raises(GhError, match="Refusing"):
        validate_gh_argv(["gh", "pr", "merge", "1"])
    with pytest.raises(GhError, match="Refusing|non-GET|POST"):
        validate_gh_argv(["gh", "api", "-X", "POST", "repos/o/r/pulls"])
    with pytest.raises(GhError, match="Refusing|non-GET|PATCH"):
        validate_gh_argv(["gh", "api", "-X", "PATCH", "repos/o/r/pulls/1"])
    with pytest.raises(GhError, match="merge|/merge"):
        validate_gh_argv(["gh", "api", "repos/o/r/pulls/1/merge"])
    with pytest.raises(GhError, match="Refusing"):
        validate_gh_argv(["gh", "pr", "create", "--title", "x"])
    # Allowed shapes
    validate_gh_argv(["gh", "api", "repos/omacom/omarchy/pulls?state=open"])
    validate_gh_argv(
        ["gh", "api", "--paginate", "repos/omacom/omarchy/pulls/101/files?per_page=100"]
    )


def test_graph_hyprland_edges_stranger_absent(
    fixture_prs: list[PullRequest],
    newcomers: list[PullRequest],
) -> None:
    stranger = next(p for p in newcomers if p.number == STRANGER_NUM)
    combined = fixture_prs + [stranger]
    result = run_pipeline(combined, persist=False, apply_rules=False)
    edges = result["edges"]
    pairs = {(min(e["source"], e["target"]), max(e["source"], e["target"])) for e in edges}
    # Hyprland trio should have edges among themselves at >= 0.55
    assert (101, 108) in pairs or any(
        {e["source"], e["target"]} == {101, 108} for e in edges
    )
    assert (101, 115) in pairs or any(
        {e["source"], e["target"]} == {101, 115} for e in edges
    )
    assert (108, 115) in pairs or any(
        {e["source"], e["target"]} == {108, 115} for e in edges
    )
    # Stranger 999 should not edge to 101 at threshold 0.55
    bad = [e for e in edges if set((e["source"], e["target"])) == {101, 999}]
    assert not bad, f"unexpected edge 101-999: {bad}"


def test_server_fetch_fixtures_and_decide(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    state = run_fetch("fixtures", "omacom/omarchy", 80, store_path=store)
    assert state["groups"]
    assert state["prs"]
    assert state["source"] == "fixtures"
    g001 = next(g for g in state["groups"] if g["group_id"] == "G001")
    decide_group("G001", "approve", path=store)
    updated = ui_state(store)
    rules = updated["rules"]
    assert any(r["group_id"] == "G001" and r["decision"] == "approve" for r in rules)
    assert g001["pr_numbers"]


def test_ensure_initial_fixtures(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    ensure_initial_fixtures(store)
    data = load_store(store)
    assert data["last_groups"]
    assert data["last_prs"]
    # second call should no-op (keep same count)
    n = len(data["last_groups"])
    ensure_initial_fixtures(store)
    assert len(load_store(store)["last_groups"]) == n


def test_ingest_gh_mocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pulls_payload = [
        {
            "number": 42,
            "title": "Fix thing",
            "body": "body",
            "user": {"login": "mirzap"},
            "created_at": "2026-09-01T00:00:00Z",
            "html_url": "https://github.com/omacom/omarchy/pull/42",
        }
    ]
    files_payload = [
        {"filename": "config/hypr/bindings.conf", "patch": "@@ -1 +1 @@\n+x\n"}
    ]
    calls: list[list[str]] = []

    def fake_run(argv, capture_output=True, text=True, timeout=300, **_kwargs):
        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        calls.append(list(argv))
        if argv[:2] == ["gh", "auth"]:
            p = Proc()
            p.returncode = 0
            return p
        if argv[0] == "gh" and argv[1] == "api":
            endpoint = argv[-1]
            p = Proc()
            if "/files" in endpoint:
                p.stdout = json.dumps(files_payload)
            else:
                p.stdout = json.dumps(pulls_payload)
            return p
        raise AssertionError(f"unexpected argv: {argv}")

    cache = tmp_path / "cache"
    monkeypatch.setattr("triage.gh.shutil.which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr("triage.gh.subprocess.run", fake_run)
    monkeypatch.setattr("triage.gh.DEFAULT_CACHE_DIR", cache)

    prs = ingest(source="gh", repo="omacom/omarchy", limit=10)
    assert len(prs) == 1
    assert prs[0].number == 42
    assert prs[0].user == "mirzap"
    assert prs[0].changed_files[0].path == "config/hypr/bindings.conf"
    assert prs[0].html_url.endswith("/pull/42")
    assert any("api" in c for c in calls)

    # second fetch hits cache (no new api calls for files/pulls beyond auth)
    api_before = sum(1 for c in calls if len(c) > 1 and c[1] == "api")
    prs2 = fetch_pulls_gh("omacom", "omarchy", limit=10, cache_dir=cache)
    assert len(prs2) == 1
    api_after = sum(1 for c in calls if len(c) > 1 and c[1] == "api")
    assert api_after == api_before


def test_slim_pr_files_include_capped_patches(fixture_prs: list[PullRequest]) -> None:
    pr = next(p for p in fixture_prs if p.number == 101)
    slim = slim_pr(pr, group_id="G001", repo="omacom/omarchy")
    assert isinstance(slim["files"], list)
    assert slim["files"], "expected file entries"
    assert all(isinstance(f, dict) and "path" in f and "patch" in f for f in slim["files"])
    assert "paths" in slim
    assert set(slim["paths"]) == {f["path"] for f in slim["files"]}
    assert any(f["path"].endswith("bindings.conf") and f["patch"] for f in slim["files"])


def test_file_overlap_hyprland_trio(fixture_prs: list[PullRequest]) -> None:
    members = [p for p in fixture_prs if p.number in HYPRLAND_NUMS]
    members.sort(key=lambda p: p.number)
    ov = file_overlap(members)
    assert ov["jaccard"] == 1.0
    assert "config/hypr/bindings.conf" in ov["shared"]
    assert "config/hypr/hyprland.conf" in ov["shared"]
    assert ov["partial"] == []
    assert ov["unique"] == []
    row = next(r for r in ov["matrix"] if r["path"] == "config/hypr/bindings.conf")
    assert row["kind"] == "shared"
    assert row["same_patch"] is True
    assert set(row["prs"].keys()) == {"101", "108", "115"}


def test_file_overlap_same_path_different_patch() -> None:
    a = PullRequest(
        number=1,
        title="a",
        body="",
        user="u1",
        changed_files=[ChangedFile(path="foo.conf", patch="@@ -1 +1 @@\n-old\n+new-a\n")],
        created_at="2026-01-01T00:00:00Z",
    )
    b = PullRequest(
        number=2,
        title="b",
        body="",
        user="u2",
        changed_files=[ChangedFile(path="foo.conf", patch="@@ -1 +1 @@\n-old\n+new-b\n")],
        created_at="2026-01-01T00:00:00Z",
    )
    ov = file_overlap([a, b])
    assert ov["jaccard"] == 1.0
    assert ov["shared"] == ["foo.conf"]
    row = ov["matrix"][0]
    assert row["kind"] == "shared"
    assert row["same_patch"] is False
    assert row["patch_hashes"]["1"] != row["patch_hashes"]["2"]


def test_file_overlap_walker_pair(fixture_prs: list[PullRequest]) -> None:
    members = [p for p in fixture_prs if p.number in (301, 309)]
    members.sort(key=lambda p: p.number)
    ov = file_overlap(members)
    assert "config/walker/config.toml" in ov["shared"]
    row = next(r for r in ov["matrix"] if r["path"] == "config/walker/config.toml")
    assert row["kind"] == "shared"
    assert row["same_patch"] is False  # different theme patches
    assert ov["jaccard"] < 1.0
    assert ov["unique"]  # distinct theme css files


def test_ui_state_overlap_g001_hypr(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    state = run_fetch("fixtures", "omacom/omarchy", 80, store_path=store)
    assert "overlap" in state
    g001 = next(g for g in state["groups"] if g["group_id"] == "G001")
    ov = state["overlap"]["G001"]
    assert HYPRLAND_NUMS.issubset(set(g001["pr_numbers"]))
    paths = {r["path"] for r in ov["matrix"]}
    assert "config/hypr/bindings.conf" in paths
    assert "config/hypr/hyprland.conf" in paths
    # slim PRs carry patches
    pr101 = next(p for p in state["prs"] if p["number"] == 101)
    assert isinstance(pr101["files"][0], dict)
    assert pr101["files"][0]["patch"]


def test_every_ingested_pr_in_exactly_one_group(fixture_prs: list[PullRequest]) -> None:
    result = run_pipeline(fixture_prs, persist=False, apply_rules=False)
    nums = [p.number for p in result["prs"]]
    assigned: list[int] = []
    for g in result["groups"]:
        assigned.extend(g.pr_numbers)
    assert sorted(assigned) == sorted(nums)
    assert len(assigned) == len(set(assigned))
    # last_prs / slim_prs also cover every PR
    slim_nums = [p["number"] for p in result["slim_prs"]]
    assert sorted(slim_nums) == sorted(nums)


def test_fetch_pulls_gh_limit_zero_and_slice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pulls_payload = [
        {
            "number": n,
            "title": f"PR {n}",
            "body": "body",
            "user": {"login": "u"},
            "created_at": "2026-09-01T00:00:00Z",
            "html_url": f"https://github.com/omacom/omarchy/pull/{n}",
        }
        for n in (10, 20, 30, 40, 50)
    ]
    files_payload = [{"filename": "a.conf", "patch": "@@ -1 +1 @@\n+x\n"}]
    calls: list[list[str]] = []

    def fake_run(argv, capture_output=True, text=True, timeout=300, **_kwargs):
        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        calls.append(list(argv))
        if argv[:2] == ["gh", "auth"]:
            return Proc()
        if argv[0] == "gh" and argv[1] == "api":
            endpoint = argv[-1]
            p = Proc()
            if "/files" in endpoint:
                p.stdout = json.dumps(files_payload)
            else:
                p.stdout = json.dumps(pulls_payload)
            return p
        raise AssertionError(f"unexpected argv: {argv}")

    cache = tmp_path / "cache"
    monkeypatch.setattr("triage.gh.shutil.which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr("triage.gh.subprocess.run", fake_run)
    monkeypatch.setattr("triage.gh.DEFAULT_CACHE_DIR", cache)

    # limit=0 → full mocked list
    all_prs = fetch_pulls_gh("omacom", "omarchy", limit=0, cache_dir=cache)
    assert len(all_prs) == 5
    assert [p.number for p in all_prs] == [10, 20, 30, 40, 50]
    assert all(isinstance(cf, ChangedFile) for p in all_prs for cf in p.changed_files)
    assert all(p.changed_files[0].path == "a.conf" for p in all_prs)

    # limit=2 slices (cache hit for pulls; files may cache-hit too)
    sliced = fetch_pulls_gh("omacom", "omarchy", limit=2, cache_dir=cache)
    assert len(sliced) == 2
    assert [p.number for p in sliced] == [10, 20]
    assert isinstance(sliced[0].changed_files[0], ChangedFile)


def test_async_fetch_progress_http(tmp_path: Path) -> None:
    """POST /api/fetch returns {started:true}; poll progress; then GET state."""
    import time
    from http.client import HTTPConnection

    from triage.server import (
        get_fetch_progress,
        make_handler,
        start_fetch_async,
        _set_progress,
        _fetch_lock,
        _fetch_progress,
    )

    # Reset module progress
    with _fetch_lock:
        _fetch_progress.update(
            {
                "running": False,
                "phase": "",
                "done": 0,
                "total": 0,
                "error": None,
                "ready": False,
                "message": "",
            }
        )

    store = tmp_path / "store.json"
    handler = make_handler(store)
    from http.server import ThreadingHTTPServer
    import threading

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({"source": "fixtures", "repo": "omacom/omarchy", "limit": 0})
        conn.request("POST", "/api/fetch", body=body, headers={"Content-Type": "application/json"})
        res = conn.getresponse()
        data = json.loads(res.read().decode())
        assert res.status == 200
        assert data.get("started") is True

        # Concurrent POST should 409
        conn.request("POST", "/api/fetch", body=body, headers={"Content-Type": "application/json"})
        res2 = conn.getresponse()
        data2 = json.loads(res2.read().decode())
        # May be 409 if still running, or 200 if fixtures already finished — either ok shape
        assert res2.status in (200, 409)
        if res2.status == 409:
            assert data2.get("running") is True or "error" in data2

        # Poll until ready
        ready = False
        for _ in range(50):
            conn.request("GET", "/api/progress")
            pr = conn.getresponse()
            prog = json.loads(pr.read().decode())
            assert "running" in prog and "phase" in prog
            if prog.get("ready") and not prog.get("running"):
                ready = True
                break
            if prog.get("error") and not prog.get("running"):
                raise AssertionError(prog["error"])
            time.sleep(0.05)
        assert ready, f"progress never ready: {get_fetch_progress()}"

        conn.request("GET", "/api/state")
        st_res = conn.getresponse()
        state = json.loads(st_res.read().decode())
        assert st_res.status == 200
        assert state["groups"]
        assert state["prs"]
        assert len(state["prs"]) == 12
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_default_limit_is_zero() -> None:
    from triage.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["run", "--source", "fixtures"])
    assert args.limit == 0
    assert args.repo == "omacom/omarchy"
