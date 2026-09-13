"""Focused offline contracts for request-routed repository workspaces."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Iterator

import pytest

from triage import server as server_module
from triage.models import ChangedFile, PullRequest
from triage.pipeline import run_pipeline
from triage.server import BoundedThreadingHTTPServer, make_handler
from triage.store import load_store
from triage.workspaces import (
    InvalidRepoError,
    WorkspaceConflictError,
    WorkspaceRouter,
    WorkspaceUnavailableError,
    canonical_repo,
)

TOKEN = "workspace-test-token"


def fixture_pr(repo: str, number: int = 1) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"fixture for {repo}",
        body="",
        user="local",
        changed_files=[
            ChangedFile(path="a.txt", patch=f"@@ -1 +1 @@\n-old\n+{repo}\n")
        ],
        created_at="2026-09-12T00:00:00Z",
    )


def seed_store(path: Path, repo: str) -> None:
    run_pipeline(
        [fixture_pr(repo)],
        persist=True,
        store_path=path,
        source="fixtures",
        repo=repo,
    )


@contextmanager
def multi_server(root: Path) -> Iterator[int]:
    handler = make_handler(
        None,
        workspace_root=root,
        csrf_token=TOKEN,
        allowed_hostnames=("127.0.0.1", "localhost"),
    )
    httpd = BoundedThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def json_request(
    connection: HTTPConnection,
    port: int,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Host": f"127.0.0.1:{port}"}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers.update(
            {
                "Origin": f"http://127.0.0.1:{port}",
                "X-CSRF-Token": TOKEN,
                "Content-Type": "application/json",
            }
        )
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    return response.status, json.loads(raw) if raw else {}


def test_unknown_lookup_is_empty_and_noncreating_legacy_stays_in_place(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspaces"
    router = WorkspaceRouter(root)
    binding = router.resolve("Acme/NewRepo")
    assert binding.repo == "acme/newrepo"
    assert binding.initialized is False
    assert binding.path == root / "acme" / "newrepo" / "store.json"
    assert not root.exists()

    # The HTTP lookup must preserve the same no-side-effect behavior.
    with multi_server(root) as port:
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        status, payload = json_request(
            connection, port, "GET", "/api/state?repo=acme%2Fnewrepo"
        )
        connection.close()
    assert status == 200
    assert payload["repo"] == "acme/newrepo"
    assert payload["groups"] == [] and payload["prs"] == []
    assert payload["workspace"] == {
        "mode": "multi",
        "repo": "acme/newrepo",
        "initialized": False,
        "legacy": False,
    }
    assert not root.exists()

    legacy = tmp_path / "store.json"
    seed_store(legacy, "Acme/Legacy")
    before = legacy.read_bytes()
    router = WorkspaceRouter(root)
    assert router.default_repo == "acme/legacy"
    assert router.resolve().path == legacy
    with multi_server(root) as port:
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        status, payload = json_request(connection, port, "GET", "/api/state")
        connection.close()
    assert status == 200
    assert payload["repo"] == "acme/legacy"
    assert payload["workspace"]["legacy"] is True
    assert legacy.read_bytes() == before
    assert not root.exists()


def test_identical_ids_route_http_reads_writes_and_keepalive_independently(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspaces"
    alpha = root / "acme" / "alpha" / "store.json"
    beta = root / "acme" / "beta" / "store.json"
    seed_store(alpha, "acme/alpha")
    seed_store(beta, "acme/beta")

    with multi_server(root) as port:
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        status_a, state_a = json_request(
            connection, port, "GET", "/api/state?repo=acme%2Falpha"
        )
        status_b, state_b = json_request(
            connection, port, "GET", "/api/state?repo=acme%2Fbeta"
        )
        assert status_a == status_b == 200
        assert state_a["repo"] == "acme/alpha"
        assert state_b["repo"] == "acme/beta"
        assert state_a["groups"][0]["group_id"] == state_b["groups"][0]["group_id"] == "G001"
        assert state_a["prs"][0]["number"] == state_b["prs"][0]["number"] == 1
        assert state_a["prs"][0]["title"] != state_b["prs"][0]["title"]

        status, decision = json_request(
            connection,
            port,
            "POST",
            "/api/decide",
            body={
                "repo": "acme/alpha",
                "group_id": "G001",
                "decision": "reject",
                "expected_version": state_a["store_version"],
                "idempotency_key": "alpha-reject-1",
            },
        )
        connection.close()
    assert status == 200
    assert decision["decision"]["decision"] == "reject"
    alpha_store, beta_store = load_store(alpha), load_store(beta)
    assert alpha_store["repo"] == "acme/alpha"
    assert beta_store["repo"] == "acme/beta"
    assert len(alpha_store["decision_events"]) == 1
    assert beta_store["decision_events"] == []


@pytest.mark.parametrize(
    "value",
    ["", "acme", "acme/", "/repo", "acme/../repo", "acme\\repo", " acme/repo", "acme/repo "],
)
def test_unsafe_repo_identities_fail_closed(value: str) -> None:
    with pytest.raises(InvalidRepoError):
        canonical_repo(value)


def test_symlink_and_ambiguous_bindings_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    legacy = tmp_path / "store.json"
    seed_store(legacy, "acme/legacy")
    namespaced = root / "acme" / "legacy" / "store.json"
    seed_store(namespaced, "acme/legacy")
    with pytest.raises(WorkspaceConflictError):
        WorkspaceRouter(root).resolve("acme/legacy")

    escaped_root = tmp_path / "escaped-workspaces"
    (escaped_root / "safe").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (escaped_root / "safe" / "widgets").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceUnavailableError):
        WorkspaceRouter(escaped_root).resolve("safe/widgets")

def test_mocked_sync_persists_and_reports_only_its_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "workspaces" / "acme" / "alpha" / "store.json"
    other = tmp_path / "workspaces" / "acme" / "beta" / "store.json"
    started, progressed, release = threading.Event(), threading.Event(), threading.Event()

    def fake_run_fetch(
        source: str,
        repo: str,
        limit: int,
        *,
        store_path: Path,
        progress: dict[str, Any],
        refresh: bool,
    ) -> None:
        del limit, refresh
        started.set()
        progress.update(phase="pipeline", done=1, total=1, message="persisting")
        server_module._set_progress(phase="pipeline", done=1, total=1, message="persisting")
        progressed.set()
        assert release.wait(2)
        run_pipeline(
            [fixture_pr(repo)], persist=True, store_path=store_path,
            source=source, repo=repo,
        )

    with server_module._fetch_lock:
        server_module._fetch_progress.update(
            running=False, phase="", done=0, total=0, error=None,
            ready=False, message="", repo="", refresh=False, store_path="",
        )
        server_module._fetch_terminal.clear()
    monkeypatch.setattr(server_module, "run_fetch", fake_run_fetch)
    try:
        result = server_module.start_fetch_async(
            "fixtures", "acme/alpha", 1, store_path=target
        )
        assert result["started"] is True
        assert started.wait(2) and progressed.wait(2)
        alpha_progress = server_module.get_fetch_progress(
            repo="acme/alpha", store_path=target
        )
        beta_progress = server_module.get_fetch_progress(
            repo="acme/beta", store_path=other
        )
        assert alpha_progress["running"] is True
        assert alpha_progress["phase"] == "pipeline"
        assert beta_progress["running"] is False
        assert beta_progress["repo"] == "acme/beta"
        release.set()
        assert server_module._fetch_thread is not None
        server_module._fetch_thread.join(timeout=3)
        done = server_module.get_fetch_progress(repo="acme/alpha", store_path=target)
        assert done["running"] is False and done["ready"] is True
        assert target.exists() and not other.exists()
        assert load_store(target)["repo"] == "acme/alpha"
    finally:
        release.set()
        if server_module._fetch_thread is not None:
            server_module._fetch_thread.join(timeout=3)
        with server_module._fetch_lock:
            server_module._fetch_progress.update(
                running=False, phase="", done=0, total=0, error=None,
                ready=False, message="", repo="", refresh=False, store_path="",
            )
            server_module._fetch_terminal.clear()
