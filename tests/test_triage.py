"""Tests for Omarchy PR group-triage POC (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from triage.github import GitHubError, github_request
from triage.models import ChangedFile, PullRequest
from triage.pipeline import (
    FIXTURES_DIR,
    NEEDS_HUMAN_LABEL,
    ingest,
    load_prs_from_json,
    run_pipeline,
)
from triage.store import decide_group, load_rules
from triage.cli import main as cli_main

HYPRLAND_NUMS = {101, 108, 115}
STRANGER_NUM = 999


@pytest.fixture
def fixture_prs() -> list[PullRequest]:
    return load_prs_from_json(FIXTURES_DIR / "prs.json")


@pytest.fixture
def newcomers() -> list[PullRequest]:
    return load_prs_from_json(FIXTURES_DIR / "newcomers.json")


@pytest.fixture
def tmp_store(tmp_path: Path) -> Path:
    return tmp_path / "store.json"


def test_hyprland_trio_clusters_together(
    fixture_prs: list[PullRequest], tmp_store: Path
) -> None:
    result = run_pipeline(
        fixture_prs, persist=False, store_path=tmp_store, apply_rules=False
    )
    groups = result["groups"]
    hypr_group = None
    for g in groups:
        if HYPRLAND_NUMS.issubset(set(g.pr_numbers)):
            hypr_group = g
            break
    assert hypr_group is not None, f"hyprland PRs not co-clustered: {[g.pr_numbers for g in groups]}"
    assert set(hypr_group.pr_numbers) >= HYPRLAND_NUMS


def test_stranger_does_not_join_hyprland(
    fixture_prs: list[PullRequest], newcomers: list[PullRequest], tmp_store: Path
) -> None:
    stranger = next(p for p in newcomers if p.number == STRANGER_NUM)
    combined = fixture_prs + [stranger]
    result = run_pipeline(
        combined, persist=False, store_path=tmp_store, apply_rules=False
    )
    for g in result["groups"]:
        nums = set(g.pr_numbers)
        if HYPRLAND_NUMS & nums:
            assert STRANGER_NUM not in nums


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
        fetch_pulls("omacom", "omarchy", limit=1, refresh=True)


def test_demo_and_pipeline_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / ".triage" / "demo-store.json"
    monkeypatch.chdir(tmp_path)
    # Point CLI default store via --store
    rc = cli_main(["demo", "--store", str(store)])
    assert rc == 0
    assert store.exists()
    rules = load_rules(store)
    assert rules == []

    prs = ingest(source="fixtures")
    assert len(prs) == 12
    result = run_pipeline(prs, persist=False, apply_rules=False)
    assert len(result["groups"]) >= 4


# --- gh client and server extensions ---

from triage.gh import GhError, validate_gh_argv, fetch_pulls_gh
from triage.overlap import file_overlap
from triage.server import run_fetch
from triage.store import overlap_for_group, ui_state, load_store


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


def test_current_group_membership_excludes_unrelated_pr(
    fixture_prs: list[PullRequest],
    newcomers: list[PullRequest],
    tmp_store: Path,
) -> None:
    stranger = next(p for p in newcomers if p.number == STRANGER_NUM)
    combined = fixture_prs + [stranger]
    result = run_pipeline(
        combined, persist=False, store_path=tmp_store, apply_rules=False
    )
    hypr = next(g for g in result["groups"] if 101 in g.pr_numbers)
    assert HYPRLAND_NUMS.issubset(set(hypr.pr_numbers))
    assert STRANGER_NUM not in hypr.pr_numbers


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


def test_server_initial_state_remains_empty(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    data = load_store(store)
    assert data["last_groups"] == []
    assert data["last_prs"] == []
    assert not store.exists()


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

    prs = ingest(source="gh", repo="omacom/omarchy", limit=10, refresh=True)
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


def test_pipeline_slim_prs_keep_current_paths_without_eager_patches(
    fixture_prs: list[PullRequest], tmp_store: Path
) -> None:
    result = run_pipeline(
        fixture_prs, persist=False, store_path=tmp_store, apply_rules=False
    )
    slim = next(p for p in result["slim_prs"] if p["number"] == 101)
    assert "config/hypr/bindings.conf" in slim["paths"]
    assert "files" not in slim


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


def test_ui_state_overlap_is_summary_and_full_payload_is_lazy(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    state = run_fetch("fixtures", "omacom/omarchy", 80, store_path=store)
    assert "overlap" in state
    g001 = next(g for g in state["groups"] if g["group_id"] == "G001")
    assert HYPRLAND_NUMS.issubset(set(g001["pr_numbers"]))
    # The pipeline stores no eager overlap payload; derive bounded pages on demand.
    assert state["overlap"] == {}
    full = overlap_for_group("G001", path=store)
    assert full["lazy"] is False
    paths = {r["path"] for r in full["matrix"]}
    assert "config/hypr/bindings.conf" in paths
    assert "config/hypr/hyprland.conf" in paths
    # UI state carries paths only; patches are fetched lazily from cache.
    pr101 = next(p for p in state["prs"] if p["number"] == 101)
    assert "config/hypr/bindings.conf" in pr101["paths"]
    assert "files" not in pr101


def test_every_ingested_pr_in_exactly_one_group(
    fixture_prs: list[PullRequest], tmp_store: Path
) -> None:
    result = run_pipeline(
        fixture_prs, persist=False, store_path=tmp_store, apply_rules=False
    )
    nums = [p.number for p in result["prs"]]
    assigned: list[int] = []
    for g in result["groups"]:
        assigned.extend(g.pr_numbers)
    assert sorted(assigned) == sorted(nums)
    assert len(assigned) == len(set(assigned))
    # last_prs / slim_prs also cover every PR
    slim_nums = [p["number"] for p in result["slim_prs"]]
    assert sorted(slim_nums) == sorted(nums)


def test_fetch_pulls_gh_limit_zero_and_cached_listing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    all_prs = fetch_pulls_gh("omacom", "omarchy", limit=0, cache_dir=cache, refresh=True)
    assert len(all_prs) == 5
    assert [p.number for p in all_prs] == [10, 20, 30, 40, 50]
    assert all(isinstance(cf, ChangedFile) for p in all_prs for cf in p.changed_files)
    assert all(p.changed_files[0].path == "a.conf" for p in all_prs)

    # Browsing a cached snapshot keeps the complete listing; limit controls
    # file hydration during an explicit refresh.
    sliced = fetch_pulls_gh("omacom", "omarchy", limit=2, cache_dir=cache)
    assert len(sliced) == 5
    assert [p.number for p in sliced] == [10, 20, 30, 40, 50]


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
    handler = make_handler(
        store,
        allowed_hostnames=("127.0.0.1",),
        csrf_token="test-session-token",
    )
    from http.server import ThreadingHTTPServer
    import threading

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({
            "source": "fixtures", "repo": "omacom/omarchy", "limit": 0,
            "refresh": False,
        })
        mutation_headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{port}",
            "X-CSRF-Token": "test-session-token",
        }
        conn.request("POST", "/api/fetch", body=body, headers=mutation_headers)
        res = conn.getresponse()
        data = json.loads(res.read().decode())
        assert res.status == 202
        assert data.get("started") is True

        # Concurrent POST should 409
        conn.request("POST", "/api/fetch", body=body, headers=mutation_headers)
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
