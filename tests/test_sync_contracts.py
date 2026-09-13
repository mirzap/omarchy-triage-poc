"""Offline acceptance tests for the canonical GitHub synchronization contract."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from triage import gh, github


def pull(number: int, revision: str = "1") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "body",
        "user": {"login": "octo"},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": f"2026-09-{int(revision):02d}T00:00:00Z",
        "head": {"sha": f"head-{number}-{revision}"},
        "base": {"sha": "base-main"},
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
    }


def changed_file(number: int, *, patch: str | None = "@@ -0,0 +1 @@\n+ok") -> dict[str, Any]:
    result: dict[str, Any] = {
        "filename": f"src/{number}.py", "status": "modified", "additions": 1, "deletions": 0,
    }
    if patch is not None:
        result["patch"] = patch
    return result


class FakeTransport:
    def __init__(self, listing: list[dict[str, Any]]) -> None:
        self.listing = listing
        self.calls: list[str] = []
        self.fail_file: int | None = None
        self.revalidate_listing: list[dict[str, Any]] | None = None
        self.list_calls = 0

    def __call__(self, endpoint: str, **_kwargs: Any) -> Any:
        self.calls.append(endpoint)
        if "/files?" in endpoint:
            number = int(endpoint.split("/pulls/")[1].split("/")[0])
            if number == self.fail_file:
                raise gh.GhError("synthetic files failure")
            return [changed_file(number)]
        self.list_calls += 1
        if self.list_calls % 2 == 0 and self.revalidate_listing is not None:
            return list(self.revalidate_listing)
        return list(self.listing)


class SequencedTransport:
    """Return deterministic listing generations while recording endpoints."""

    def __init__(self, listings: list[list[dict[str, Any]]]) -> None:
        self.listings = listings
        self.list_index = 0
        self.calls: list[str] = []

    def __call__(self, endpoint: str, **_kwargs: Any) -> Any:
        self.calls.append(endpoint)
        if "/files?" in endpoint:
            number = int(endpoint.split("/pulls/")[1].split("/")[0])
            return [changed_file(number)]
        listing = self.listings[min(self.list_index, len(self.listings) - 1)]
        self.list_index += 1
        return list(listing)


@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "api", "--method=POST", "repos/acme/widgets/pulls"],
        ["gh", "api", "-ftitle=x", "repos/acme/widgets/pulls"],
        ["gh", "api", "-XPOST", "repos/acme/widgets/pulls"],
        ["gh", "api", "--paginate", "--slurp", "repos/acme/widgets/issues"],
        ["gh", "api", "repos/acme/widgets/pulls?state=closed"],
        ["gh", "api", "repos/acme/widgets/pulls?per_page=100%26x=y"],
    ],
)
def test_strict_argv_rejects_method_field_unknown_and_endpoint_bypasses(argv: list[str]) -> None:
    with pytest.raises(gh.GhError):
        gh.validate_gh_argv(argv)


def test_paginated_gh_uses_slurp_without_rewriting_json_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, '[[{"value":"]["}],[{"value":"two"}]]', "")

    monkeypatch.setattr(gh.subprocess, "run", run)
    assert gh.run_gh_api("repos/acme/widgets/pulls", paginate=True) == [
        {"value": "]["}, {"value": "two"}
    ]
    assert seen == [["gh", "api", "--paginate", "--slurp", "repos/acme/widgets/pulls"]]


def test_limit_keeps_complete_listing_and_snapshot_browse_never_fetches(tmp_path: Path) -> None:
    transport = FakeTransport([pull(1), pull(2), pull(3)])
    progress: dict[str, Any] = {}
    result = gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=transport, limit=1, cache_dir=tmp_path,
        progress=progress, refresh=True,
    )
    assert [item.number for item in result] == [1, 2, 3]
    assert result[1].evidence_complete is False
    assert progress["limited"] is True and progress["list_complete"] is True
    root = tmp_path / "acme" / "widgets"
    active = json.loads((root / "active.json").read_text())
    snapshot = root / "snapshots" / active["snapshot_id"]
    assert len(json.loads((snapshot / "pulls.json").read_text())) == 3
    assert gh.cached_pr_meta("acme", "widgets", 2, cache_dir=tmp_path)["head_sha"] == "head-2-1"
    assert gh.cached_pr_files("acme", "widgets", 2, cache_dir=tmp_path) == []
    calls = list(transport.calls)
    assert gh.cached_sync_status("acme", "widgets", tmp_path)["open_count"] == 3
    bundle = gh.cached_pr_evidence("acme", "widgets", 1, tmp_path)
    assert bundle is not None
    assert bundle["snapshot_id"] == active["snapshot_id"]
    assert bundle["meta"]["head_sha"] == "head-1-1"
    assert bundle["files"][0]["patch_complete"] is True
    assert transport.calls == calls


def test_unchanged_reuse_changed_revision_refresh_and_closed_new_listing(tmp_path: Path) -> None:
    transport = FakeTransport([pull(1), pull(2)])
    gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=transport, cache_dir=tmp_path, refresh=True
    )
    transport.calls.clear()
    transport.listing = [pull(1), pull(2, "2"), pull(3)]
    result = gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=transport, cache_dir=tmp_path, refresh=True
    )
    assert [item.number for item in result] == [1, 2, 3]
    assert not any("/pulls/1/files" in call for call in transport.calls)
    assert any("/pulls/2/files" in call for call in transport.calls)
    assert any("/pulls/3/files" in call for call in transport.calls)
    transport.calls.clear()
    transport.listing = [pull(2, "2"), pull(3)]
    gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=transport, cache_dir=tmp_path, force=True, limit=1
    )
    assert gh.cached_pr_meta("acme", "widgets", 1, cache_dir=tmp_path) is None


def test_failure_between_files_and_mid_fetch_revision_change_preserve_active(tmp_path: Path) -> None:
    transport = FakeTransport([pull(1), pull(2)])
    gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=transport, cache_dir=tmp_path, refresh=True
    )
    active_path = tmp_path / "acme" / "widgets" / "active.json"
    before = active_path.read_bytes()

    transport.listing = [pull(1, "2"), pull(2, "2")]
    transport.fail_file = 2
    with pytest.raises(gh.GhError, match="synthetic"):
        gh.fetch_pulls_with_transport(
            "acme", "widgets", transport=transport, cache_dir=tmp_path, refresh=True
        )
    assert active_path.read_bytes() == before

    transport.fail_file = None
    transport.revalidate_listing = [pull(1, "3"), pull(2, "2")]
    result = gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=transport, cache_dir=tmp_path, refresh=True
    )
    # Reconciliation is bounded.  The final full list is retained, but the
    # PR that kept moving never receives evidence from an older revision.
    assert [item.number for item in result] == [1, 2]
    assert result[0].head_sha == "head-1-2"
    assert result[0].evidence_complete is False
    assert active_path.read_bytes() != before


def test_reorder_and_metadata_churn_reuses_head_base_evidence(tmp_path: Path) -> None:
    initial = FakeTransport([pull(1), pull(2)])
    gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=initial, cache_dir=tmp_path, refresh=True
    )
    metadata_one = pull(1)
    metadata_one["title"] = "retagged"
    metadata_one["updated_at"] = "2026-09-03T00:00:00Z"
    metadata_two = pull(2)
    transport = SequencedTransport([[metadata_two, metadata_one], [metadata_one, metadata_two]])
    result = gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=transport, cache_dir=tmp_path, refresh=True
    )
    assert [item.number for item in result] == [1, 2]
    assert result[0].title == "retagged"
    assert not any("/files?" in call for call in transport.calls)
    active = json.loads((tmp_path / "acme" / "widgets" / "active.json").read_text())
    snapshot = tmp_path / "acme" / "widgets" / "snapshots" / active["snapshot_id"]
    metadata = json.loads((snapshot / gh.FETCH_METADATA_FILE).read_text())
    assert metadata["files"]["1"]["updated_at"] == metadata_one["updated_at"]


def test_changed_revision_is_refetched_and_perpetual_churn_is_stubbed(
    tmp_path: Path,
) -> None:
    stable = SequencedTransport([[pull(1)], [pull(1, "2")], [pull(1, "2")]])
    result = gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=stable, cache_dir=tmp_path, refresh=True
    )
    assert result[0].head_sha == "head-1-2"
    assert result[0].evidence_complete is True
    assert sum("/files?" in call for call in stable.calls) == 2

    moving = SequencedTransport(
        [[pull(1, "1")], [pull(1, "2")], [pull(1, "3")], [pull(1, "4")]]
    )
    moving_cache = tmp_path / "moving"
    result = gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=moving, cache_dir=moving_cache, refresh=True
    )
    assert result[0].head_sha == "head-1-4"
    assert result[0].evidence_complete is False
    assert sum("/files?" in call for call in moving.calls) == 3
    assert gh.cached_pr_files("acme", "widgets", 1, moving_cache) == []
    bundle = gh.cached_pr_evidence("acme", "widgets", 1, moving_cache)
    assert bundle is not None and bundle["files"] == []
    active = json.loads(
        (moving_cache / "acme" / "widgets" / "active.json").read_text()
    )
    snapshot = moving_cache / "acme" / "widgets" / "snapshots" / active["snapshot_id"]
    assert not (snapshot / "files" / "1.json").exists()


def test_new_pr_is_fetched_in_bounded_unlimited_reconciliation(tmp_path: Path) -> None:
    transport = SequencedTransport(
        [[pull(1)], [pull(1), pull(2)], [pull(1), pull(2)]]
    )
    result = gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=transport, cache_dir=tmp_path, refresh=True
    )
    assert [item.number for item in result] == [1, 2]
    assert all(item.evidence_complete for item in result)
    assert any("/pulls/2/files?" in call for call in transport.calls)


def test_duplicate_or_malformed_listing_preserves_active_snapshot(tmp_path: Path) -> None:
    initial = FakeTransport([pull(1)])
    gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=initial, cache_dir=tmp_path, refresh=True
    )
    active_path = tmp_path / "acme" / "widgets" / gh.ACTIVE_SNAPSHOT_FILE
    before = active_path.read_bytes()
    malformed = SequencedTransport([[pull(1), pull(1)]])
    with pytest.raises(gh.GhError, match="Duplicate PR number"):
        gh.fetch_pulls_with_transport(
            "acme", "widgets", transport=malformed, cache_dir=tmp_path, refresh=True
        )
    assert active_path.read_bytes() == before


def test_missing_patch_is_explicitly_incomplete(tmp_path: Path) -> None:
    transport = FakeTransport([pull(1)])
    original = transport.__call__

    def missing(endpoint: str, **kwargs: Any) -> Any:
        if "/files?" in endpoint:
            transport.calls.append(endpoint)
            return [changed_file(1, patch=None)]
        return original(endpoint, **kwargs)

    result = gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=missing, cache_dir=tmp_path, refresh=True
    )
    assert result[0].evidence_complete is False
    assert result[0].changed_files[0].patch_complete is False
    assert gh.cached_pr_files("acme", "widgets", 1, tmp_path)[0]["patch_complete"] is False
    assert gh.cached_pr_meta("acme", "widgets", 1, tmp_path)["evidence_complete"] is False


def test_urllib_transport_paginates_pulls_and_files(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    def request(_method: str, url: str, **_kwargs: Any) -> Any:
        urls.append(url)
        page = int(urllib_page(url))
        return list(range(100)) if page == 1 else [100]

    def urllib_page(url: str) -> str:
        return url.rsplit("page=", 1)[1]

    monkeypatch.setattr(github, "github_request", request)
    transport = github._transport("token")
    assert len(transport("repos/acme/widgets/pulls?per_page=100", paginate=True)) == 101
    assert len(urls) == 2


def test_cached_gh_ingest_does_not_require_cli_or_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport([pull(1)])
    gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=transport, cache_dir=tmp_path, refresh=True
    )
    monkeypatch.setattr(gh, "ensure_gh_auth", lambda: (_ for _ in ()).throw(AssertionError("auth called")))
    assert [item.number for item in gh.fetch_pulls_gh("acme", "widgets", cache_dir=tmp_path)] == [1]


def test_gh_retries_timeouts_and_transient_failures_but_not_permanent_or_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def transient(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.TimeoutExpired(argv, 1)
        if attempts == 2:
            return subprocess.CompletedProcess(argv, 1, "", "HTTP 503 service unavailable")
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(gh.subprocess, "run", transient)
    monkeypatch.setattr(gh.time, "sleep", lambda _seconds: None)
    assert gh.run_gh_api("repos/acme/widgets/pulls") == []
    assert attempts == 3

    attempts = 0

    def permanent(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 404 not found")

    monkeypatch.setattr(gh.subprocess, "run", permanent)
    with pytest.raises(gh.GhError, match="permanently"):
        gh.run_gh_api("repos/acme/widgets/pulls")
    assert attempts == 1

    def limited(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 429 rate limit exceeded; reset=123")

    monkeypatch.setattr(gh.subprocess, "run", limited)
    with pytest.raises(gh.GhError, match="rate limit.*reset=123"):
        gh.run_gh_api("repos/acme/widgets/pulls")


def test_three_thousand_file_cap_is_explicitly_incomplete() -> None:
    files = [changed_file(index) for index in range(gh.MAX_PULL_FILES)]
    _parsed, complete, _additions, _deletions = gh._file_evidence(files)
    assert complete is False


@pytest.mark.parametrize("bad_count", [True, 1.25, -1])
def test_invalid_github_file_counts_are_never_complete(bad_count: object) -> None:
    raw = changed_file(1)
    raw["additions"] = bad_count
    files, complete, additions, _deletions = gh._file_evidence([raw])
    assert complete is False
    assert additions is None
    assert files[0].additions is None
    assert files[0].patch_complete is False


def test_active_snapshot_requires_exact_pointer_and_metadata_repository(
    tmp_path: Path,
) -> None:
    root = tmp_path / "acme" / "widgets"
    snapshot = root / "snapshots" / "copied"
    snapshot.mkdir(parents=True)
    (snapshot / "pulls.json").write_text("[]", encoding="utf-8")
    (snapshot / gh.FETCH_METADATA_FILE).write_text(
        json.dumps({"snapshot_id": "copied", "repository": "other/project"}),
        encoding="utf-8",
    )
    (root / gh.ACTIVE_SNAPSHOT_FILE).write_text(
        json.dumps({"snapshot_id": "copied", "repository": "acme/widgets"}),
        encoding="utf-8",
    )
    with pytest.raises(gh.GhError, match="metadata is missing or mismatched"):
        gh.fetch_pulls_with_transport(
            "acme",
            "widgets",
            transport=lambda *_args, **_kwargs: pytest.fail("network used"),
            cache_dir=tmp_path,
            refresh=False,
        )
    assert gh.cached_pr_evidence(
        "acme", "widgets", 1, tmp_path, snapshot_id="copied"
    ) is None


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.headers: dict[str, str] = {}

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_urllib_refuses_redirects_and_bounds_response_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class Redirecting:
        def open(self, request, **_kwargs):
            nonlocal calls
            calls += 1
            assert request.get_header("Authorization") == "Bearer secret"
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "https://attacker.invalid/collect"},
                io.BytesIO(b"redirect"),
            )

    monkeypatch.setattr(github, "_opener", Redirecting())
    with pytest.raises(github.GitHubError, match="redirect refused"):
        github.github_request(
            "GET", "https://api.github.com/repos/acme/widgets/pulls", token="secret"
        )
    assert calls == 1

    class Oversized:
        def open(self, *_args, **_kwargs):
            return _Response(b"12345")

    monkeypatch.setattr(github, "_opener", Oversized())
    monkeypatch.setattr(github, "MAX_RESPONSE_BYTES", 4)
    with pytest.raises(github.GitHubError, match="exceeded"):
        github.github_request(
            "GET", "https://api.github.com/repos/acme/widgets/pulls", token="secret"
        )

    class OversizedError:
        def open(self, request, **_kwargs):
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "error",
                {},
                io.BytesIO(b"12345"),
            )

    monkeypatch.setattr(github, "_opener", OversizedError())
    monkeypatch.setattr(github, "MAX_ERROR_BYTES", 4)
    with pytest.raises(github.GitHubError, match="error response exceeded"):
        github.github_request(
            "GET", "https://api.github.com/repos/acme/widgets/pulls", token="secret"
        )


def test_pagination_total_time_bound_fails_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((0.0, github.MAX_PAGINATION_SECONDS + 1.0))
    monkeypatch.setattr(github.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        github,
        "github_request",
        lambda *_args, **_kwargs: pytest.fail("request started after deadline"),
    )
    with pytest.raises(github.GitHubError, match="total-time limit"):
        github._transport("token")(
            "repos/acme/widgets/pulls?per_page=100",
            paginate=True,
            timeout=github.MAX_PAGINATION_SECONDS,
        )


def test_pagination_page_bound_preserves_published_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = FakeTransport([pull(1)])
    gh.fetch_pulls_with_transport(
        "acme", "widgets", transport=initial, cache_dir=tmp_path, refresh=True
    )
    active = tmp_path / "acme" / "widgets" / gh.ACTIVE_SNAPSHOT_FILE
    before = active.read_bytes()
    monkeypatch.setattr(github, "MAX_PAGINATION_PAGES", 2)
    monkeypatch.setattr(
        github,
        "github_request",
        lambda *_args, **_kwargs: [pull(number) for number in range(100)],
    )
    with pytest.raises(github.GitHubError, match="exceeded 2 pages"):
        gh.fetch_pulls_with_transport(
            "acme",
            "widgets",
            transport=github._transport("token"),
            cache_dir=tmp_path,
            refresh=True,
        )
    assert active.read_bytes() == before
