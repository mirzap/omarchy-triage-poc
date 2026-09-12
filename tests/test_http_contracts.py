"""Offline request-boundary regressions for the local dashboard."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection, RemoteDisconnected
from pathlib import Path
from typing import Any

import pytest

from triage import server as server_module
from triage.models import ChangedFile, PullRequest
from triage.pipeline import run_pipeline
from triage.server import (
    MAX_BODY_BYTES,
    BoundedThreadingHTTPServer,
    _serve_allowed_hostnames,
    _validate_loopback_host,
    make_handler,
)
from triage.store import load_store, save_store

TOKEN = "test-session-token-with-enough-entropy"


@contextmanager
def local_server(store: Path, *, timeout: float = 0.2,
                 max_request_threads: int = 16) -> Iterator[tuple[int, str]]:
    handler = make_handler(
        store,
        csrf_token=TOKEN,
        body_timeout_seconds=timeout,
        allowed_hostnames=_serve_allowed_hostnames("127.0.0.1"),
    )
    httpd = BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), handler, max_request_threads=max_request_threads
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1]), TOKEN
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def request(port: int, method: str, path: str, *, body: Any = None,
            headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:
    conn = HTTPConnection("127.0.0.1", port, timeout=3)
    payload = None if body is None else json.dumps(body)
    conn.request(method, path, body=payload, headers=headers or {})
    response = conn.getresponse()
    raw = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    conn.close()
    return response.status, response_headers, json.loads(raw) if raw else None


def mutation_headers(port: int, token: str = TOKEN) -> dict[str, str]:
    return {"Origin": f"http://127.0.0.1:{port}", "X-CSRF-Token": token,
            "Content-Type": "application/json"}


def fixture_pr(number: int = 1) -> PullRequest:
    return PullRequest(number=number, title="fixture", body="", user="local",
        changed_files=[ChangedFile(path="a.txt", patch="@@ -1 +1 @@\n-old\n+new\n")],
        created_at="2026-09-12T00:00:00Z")


def test_session_bootstrap_and_security_headers(tmp_path: Path) -> None:
    with local_server(tmp_path / "store.json") as (port, _):
        status, headers, payload = request(port, "GET", "/api/session")
    assert status == 200
    assert payload["csrf_token"] == TOKEN
    assert payload["origin"] == f"http://127.0.0.1:{port}"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "access-control-allow-origin" not in headers


def test_default_loopback_aliases_keep_exact_host_port_and_origin_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: dict[str, Any] = {}

    class FakeServer:
        server_address = ("127.0.0.1", 8741)

        def __init__(self, address: tuple[str, int], handler: Any) -> None:
            created.update(address=address, handler=handler)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            created["closed"] = True

    monkeypatch.setattr(server_module, "BoundedThreadingHTTPServer", FakeServer)
    server_module.serve(
        host="127.0.0.1", port=8741, open_browser=False,
        store_path=tmp_path / "unused.json",
    )
    assert created["handler"].allowed_hostnames == ("127.0.0.1", "localhost")
    assert created["closed"] is True

    monkeypatch.setattr(
        "triage.server.start_fetch_async",
        lambda source, repo, limit, store_path, refresh: {
            "started": True, "source": source, "repo": repo,
            "limit": limit, "refresh": refresh,
        },
    )
    body = {
        "source": "fixtures", "repo": "acme/widgets", "limit": 1,
        "refresh": False,
    }
    with local_server(tmp_path / "store.json") as (port, token):
        host = f"localhost:{port}"
        status, _, session = request(
            port, "GET", "/api/session", headers={"Host": host}
        )
        post_status, _, _ = request(
            port, "POST", "/api/fetch", body=body,
            headers={
                "Host": host,
                "Origin": f"http://{host}",
                "X-CSRF-Token": token,
                "Content-Type": "application/json",
            },
        )
        bad_hosts = [
            f"attacker.invalid:{port}",
            f"localhost.attacker.invalid:{port}",
            f"localhost:{port + 1}",
        ]
        bad_statuses = [
            request(port, "GET", "/api/session", headers={"Host": value})[0]
            for value in bad_hosts
        ]
        cross_origin = request(
            port, "GET", "/api/session",
            headers={"Host": host, "Origin": f"http://127.0.0.1:{port}"},
        )[0]

    assert status == 200
    assert session["origin"] == f"http://localhost:{port}"
    assert post_status == 202
    assert bad_statuses == [421, 421, 421]
    assert cross_origin == 403


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Host": "attacker.invalid"}, 421),
        ({"Origin": "https://attacker.invalid"}, 403),
        ({"Origin": "null"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
    ],
)
def test_get_rejects_bad_host_and_untrusted_origins(
    tmp_path: Path, headers: dict[str, str], expected: int
) -> None:
    with local_server(tmp_path / "store.json") as (port, _):
        status, _, _ = request(port, "GET", "/api/state", headers=headers)
    assert status == expected


def test_mutations_require_json_origin_and_token(tmp_path: Path) -> None:
    body = {"source": "fixtures", "repo": "acme/widgets", "limit": 1, "refresh": False}
    with local_server(tmp_path / "store.json") as (port, _):
        base = {"Origin": f"http://127.0.0.1:{port}"}
        assert request(port, "POST", "/api/fetch", body=body,
                       headers={**base, "Content-Type": "text/plain"})[0] == 403
        assert request(port, "POST", "/api/fetch", body=body,
                       headers={**base, "X-CSRF-Token": TOKEN,
                                "Content-Type": "text/plain"})[0] == 415
        assert request(port, "POST", "/api/fetch", body=body,
                       headers={"Origin": "null", "X-CSRF-Token": TOKEN,
                                "Content-Type": "application/json"})[0] == 403


def test_oversized_and_slow_bodies_are_rejected(tmp_path: Path) -> None:
    with local_server(tmp_path / "store.json", timeout=0.1) as (port, _):
        conn = HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("POST", "/api/fetch", body=b"{}", headers={
            **mutation_headers(port), "Content-Length": str(MAX_BODY_BYTES + 1)})
        assert conn.getresponse().status == 413
        conn.close()

        raw = socket.create_connection(("127.0.0.1", port), timeout=2)
        message = (f"POST /api/fetch HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Origin: http://127.0.0.1:{port}\r\nX-CSRF-Token: {TOKEN}\r\n"
                   "Content-Type: application/json\r\nContent-Length: 20\r\n\r\n{").encode()
        raw.sendall(message)
        response = raw.recv(4096)
        raw.close()
    assert b" 408 " in response


def test_absolute_deadline_rejects_drip_fed_headers_and_body(tmp_path: Path) -> None:
    with local_server(tmp_path / "store.json", timeout=0.12) as (port, _):
        header_client = socket.create_connection(("127.0.0.1", port), timeout=2)
        header_client.settimeout(1)
        header_client.sendall(b"GET /api/session HTTP/1.1\r\n")
        started = time.monotonic()
        for byte in f"Host: 127.0.0.1:{port}\r\n".encode():
            try:
                header_client.sendall(bytes([byte]))
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(0.04)
        header_response = header_client.recv(4096)
        header_elapsed = time.monotonic() - started
        header_client.close()

        body_client = socket.create_connection(("127.0.0.1", port), timeout=2)
        body_client.settimeout(1)
        headers = (f"POST /api/fetch HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Origin: http://127.0.0.1:{port}\r\nX-CSRF-Token: {TOKEN}\r\n"
                   "Content-Type: application/json\r\nContent-Length: 20\r\n\r\n").encode()
        body_client.sendall(headers)
        started = time.monotonic()
        for byte in b"{                  }":
            try:
                body_client.sendall(bytes([byte]))
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(0.04)
        body_response = body_client.recv(4096)
        body_elapsed = time.monotonic() - started
        body_client.close()

    assert b" 408 " in header_response
    assert b" 408 " in body_response
    assert header_elapsed < 0.6
    assert body_elapsed < 0.6


def test_bounded_server_rejects_connections_beyond_active_limit(tmp_path: Path) -> None:
    with local_server(tmp_path / "store.json", timeout=1,
                      max_request_threads=1) as (port, _):
        holding = socket.create_connection(("127.0.0.1", port), timeout=2)
        holding.sendall(b"GET /api/session HTTP/1.1\r\n")
        time.sleep(0.05)

        overflow = HTTPConnection("127.0.0.1", port, timeout=1)
        overflow.request("GET", "/api/session")
        with pytest.raises((RemoteDisconnected, ConnectionResetError, OSError)):
            overflow.getresponse()
        overflow.close()
        holding.close()


def test_ingress_deadline_does_not_limit_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store.json"
    run_pipeline([fixture_pr()], persist=True, store_path=store,
                 source="fixtures", repo="acme/widgets")

    def slow_enrichment(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        time.sleep(0.25)
        return {"related": [], "provider": "mock", "cache_only": False}

    monkeypatch.setattr("triage.server.rank_module.enrich_related", slow_enrichment)
    with local_server(store, timeout=0.1) as (port, token):
        version = request(port, "GET", "/api/state")[2]["store_version"]
        started = time.monotonic()
        status, _, payload = request(port, "POST", "/api/enrich", body={
            "repo": "acme/widgets", "pr": 1, "limit": 1,
            "allow_external": True, "expected_version": version,
        }, headers=mutation_headers(port, token))
        elapsed = time.monotonic() - started
    assert status == 200
    assert payload["provider"] == "mock"
    assert elapsed >= 0.2


@pytest.mark.parametrize("path", ["/%2e%2e/%2e%2e/README.md", "/../README.md",
                                   "/%2e%2e%2fREADME.md"])
def test_decoded_static_traversal_is_rejected(tmp_path: Path, path: str) -> None:
    with local_server(tmp_path / "store.json") as (port, _):
        status, _, _ = request(port, "GET", path)
    assert status == 403


def test_related_get_uses_only_cache_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "store.json"
    run_pipeline([fixture_pr(1), fixture_pr(2)], persist=True, store_path=store,
                 source="fixtures", repo="acme/widgets")
    calls = []

    def cached(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        return {"cache_only": True, "query": 1, "related": []}

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("legacy provider path was called by GET")

    monkeypatch.setattr("triage.server.rank_module.related_cached", cached, raising=False)
    monkeypatch.setattr("triage.server.rank_module.related", forbidden)
    with local_server(store) as (port, _):
        status, _, payload = request(port, "GET", "/api/related?pr=1")
    assert status == 200 and payload["cache_only"] is True
    assert len(calls) == 1


def test_external_enrichment_rejects_foreign_repo_before_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store.json"
    run_pipeline([fixture_pr()], persist=True, store_path=store,
                 source="fixtures", repo="acme/widgets")
    called = False

    def forbidden(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("triage.server.rank_module.enrich_related", forbidden, raising=False)
    body = {"repo": "other/repo", "pr": 1, "limit": 5, "allow_external": True,
            "expected_version": 1}
    with local_server(store) as (port, token):
        status, _, payload = request(port, "POST", "/api/enrich", body=body,
                                     headers=mutation_headers(port, token))
    assert status == 409 and payload["code"] == "repository_conflict"
    assert called is False


def test_stale_decision_conflicts_and_retry_is_idempotent(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    run_pipeline([fixture_pr()], persist=True, store_path=store,
                 source="fixtures", repo="acme/widgets")
    with local_server(store) as (port, token):
        state = request(port, "GET", "/api/state")[2]
        group_id = state["groups"][0]["group_id"]
        body = {"repo": "acme/widgets", "group_id": group_id, "decision": "reject",
                "expected_version": state["store_version"], "idempotency_key": "retry-key-0001"}
        first = request(port, "POST", "/api/decide", body=body,
                        headers=mutation_headers(port, token))
        retry = request(port, "POST", "/api/decide", body=body,
                        headers=mutation_headers(port, token))
        stale = request(port, "POST", "/api/decide",
                        body={**body, "idempotency_key": "stale-key-0001"},
                        headers=mutation_headers(port, token))
    assert first[0] == retry[0] == 200
    assert first[2]["decision"] == retry[2]["decision"]
    assert stale[0] == 409


def test_fixture_patch_request_never_reads_github_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store.json"
    run_pipeline([fixture_pr()], persist=True, store_path=store,
                 source="fixtures", repo="acme/widgets")
    monkeypatch.setattr("triage.server.gh_module.cached_pr_evidence",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache read")))
    with local_server(store) as (port, _):
        status, _, payload = request(
            port, "GET", "/api/patches?repo=acme%2Fwidgets&path=a.txt&prs=1")
    assert status == 200
    assert payload["items"][0]["evidence_complete"] is True
    assert payload["items"][0]["content_sha256"]


def test_file_overlap_and_patch_pages_are_available_over_http(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    run_pipeline([fixture_pr(number) for number in range(1, 11)], persist=True,
                 store_path=store, source="fixtures", repo="acme/widgets")
    with local_server(store) as (port, _):
        state = request(port, "GET", "/api/state")[2]
        group_id = state["groups"][0]["group_id"]
        file_page = request(port, "GET", "/api/file?path=a.txt&page=2&page_size=4")[2]
        overlap_page = request(
            port, "GET", "/api/overlap?group_id=" + group_id +
            "&member_page=2&member_page_size=3&row_page=1&row_page_size=1",
        )[2]
        patch_page = request(
            port, "GET", "/api/patches?repo=acme%2Fwidgets&path=a.txt&group_id=" +
            group_id + "&page=2&page_size=3",
        )[2]
        singleton = request(
            port, "GET", "/api/patches?repo=acme%2Fwidgets&path=a.txt&prs=1",
        )[2]
    assert file_page["page"] == 2 and file_page["next_page"] == 3
    assert len(file_page["prs"]) == 4
    assert overlap_page["member_page"] == 2
    assert len(overlap_page["pr_numbers"]) == 3
    assert patch_page["page"] == 2 and patch_page["total_items"] == 10
    assert len(patch_page["items"]) == 3
    assert patch_page["comparison"]["same_complete_patch"] is True
    assert singleton["comparison"]["same_complete_patch"] is None


def test_explicit_legacy_bundle_is_previewed_but_never_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_path = tmp_path / "store.json"
    run_pipeline([fixture_pr()], persist=True, store_path=store_path,
                 source="fixtures", repo="acme/widgets")
    stored = load_store(store_path)
    stored["source"] = "github"
    pr = stored["last_prs"][0]
    for field in ("head_sha", "base_sha", "updated_at", "content_digest"):
        pr[field] = ""
    pr["evidence_complete"] = False
    pr["cache_snapshot_id"] = ""
    save_store(stored, store_path)
    seen_snapshot_ids: list[str] = []

    def legacy_bundle(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen_snapshot_ids.append(kwargs["snapshot_id"])
        return {
            "legacy_unverified": True,
            "snapshot_id": "",
            "meta": {
                "number": 1, "title": "legacy", "body": "legacy body",
                "user": "cached", "head_sha": "unbound-head",
                "base_sha": "unbound-base", "updated_at": "2025-01-01T00:00:00Z",
                "evidence_complete": False,
            },
            "files": [{
                "path": "a.txt", "patch": "@@ -1 +1 @@\n-old\n+legacy\n",
                "patch_complete": False,
            }],
        }

    monkeypatch.setattr("triage.server.gh_module.cached_pr_evidence", legacy_bundle)
    with local_server(store_path) as (port, _):
        body = request(port, "GET", "/api/pr?number=1&repo=acme%2Fwidgets")[2]
        patches = request(
            port, "GET", "/api/patches?repo=acme%2Fwidgets&path=a.txt&prs=1",
        )[2]
    assert seen_snapshot_ids == ["", ""]
    assert body["body"] == "legacy body"
    assert body["legacy_unverified"] is True
    item = patches["items"][0]
    assert item["patch"].endswith("+legacy\n")
    assert item["legacy_unverified"] is True
    assert item["evidence_complete"] is False
    assert item["content_sha256"] is None
    assert "unverified legacy cache preview" in item["incomplete_reasons"]
    assert patches["comparison"]["same_complete_patch"] is None


def test_revision_bound_legacy_bundle_cannot_bypass_digest_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_path = tmp_path / "store.json"
    original_file = ChangedFile(
        "a.txt", "@@ -1 +1 @@\n-old\n+original\n", patch_complete=False
    )
    original = PullRequest(
        1, "legacy", "", "cached", [original_file], "2026-01-01T00:00:00Z",
        head_sha="bound-head", base_sha="bound-base",
        updated_at="2026-01-01T00:00:00Z", additions=1, deletions=1,
        evidence_complete=False, evidence_source="github", cache_snapshot_id="",
    )
    run_pipeline([original], persist=True, store_path=store_path,
                 source="github", repo="acme/widgets")
    changed_file = ChangedFile(
        "a.txt", "@@ -1 +1 @@\n-old\n+changed\n", patch_complete=False
    )

    monkeypatch.setattr("triage.server.gh_module.cached_pr_evidence", lambda *_a, **_k: {
        "legacy_unverified": True,
        "snapshot_id": "",
        "meta": {
            "number": 1, "title": "legacy", "body": "changed body", "user": "cached",
            "head_sha": "bound-head", "base_sha": "bound-base",
            "updated_at": "2026-01-01T00:00:00Z", "additions": 1, "deletions": 1,
            "evidence_complete": False,
        },
        "files": [changed_file.to_dict()],
    })
    with local_server(store_path) as (port, _):
        body_status = request(
            port, "GET", "/api/pr?number=1&repo=acme%2Fwidgets",
        )[0]
        patch_status = request(
            port, "GET", "/api/patches?repo=acme%2Fwidgets&path=a.txt&prs=1",
        )[0]
    assert body_status == 409
    assert patch_status == 409


def test_snapshot_bundle_still_requires_exact_revision_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_path = tmp_path / "store.json"
    run_pipeline([fixture_pr()], persist=True, store_path=store_path,
                 source="fixtures", repo="acme/widgets")
    stored = load_store(store_path)
    stored["source"] = "github"
    pr = stored["last_prs"][0]
    pr.update({
        "head_sha": "expected-head", "base_sha": "base",
        "updated_at": "2026-01-01T00:00:00Z", "cache_snapshot_id": "snapshot-1",
    })
    save_store(stored, store_path)

    monkeypatch.setattr("triage.server.gh_module.cached_pr_evidence", lambda *_a, **_k: {
        "legacy_unverified": False,
        "snapshot_id": "snapshot-1",
        "meta": {
            "number": 1, "head_sha": "changed-head", "base_sha": "base",
            "updated_at": "2026-01-01T00:00:00Z", "evidence_complete": True,
        },
        "files": [],
    })
    with local_server(store_path) as (port, _):
        body_status = request(
            port, "GET", "/api/pr?number=1&repo=acme%2Fwidgets",
        )[0]
        patch_status = request(
            port, "GET", "/api/patches?repo=acme%2Fwidgets&path=a.txt&prs=1",
        )[0]
    assert body_status == 409
    assert patch_status == 409


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.50"])
def test_non_loopback_serving_is_rejected(host: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        _validate_loopback_host(host)
