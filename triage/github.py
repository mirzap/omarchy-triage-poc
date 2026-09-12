"""Token-backed transport for the canonical read-only GitHub snapshot service."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from triage.gh import MAX_ATTEMPTS, MAX_PULL_FILES, GhError, fetch_pulls_with_transport
from triage.models import PullRequest

ALLOWED_METHODS = frozenset({"GET"})
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_PAGINATION_PAGES = 100
MAX_PAGINATION_SECONDS = 120.0
_etag_cache: dict[tuple[str, str], tuple[str, Any]] = {}
_etag_lock = threading.Lock()


class GitHubError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a bearer credential through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirectHandler())


def _read_bounded(stream: Any, limit: int, *, label: str) -> bytes:
    body = stream.read(limit + 1)
    if len(body) > limit:
        raise GitHubError(f"GitHub {label} exceeded the {limit}-byte limit")
    return body


def _require_get(method: str) -> None:
    if method.upper() not in ALLOWED_METHODS:
        raise GitHubError(
            f"Refusing non-GET HTTP method {method!r}. "
            "This POC is read-only (no POST/PATCH/PUT/DELETE/MERGE)."
        )


def github_request(
    method: str,
    url: str,
    token: str | None = None,
    timeout: float = 30.0,
) -> Any:
    """Perform one bounded, GET-only request with conditional ETag reuse."""
    _require_get(method)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com":
        raise GitHubError(f"Refusing non-GitHub API URL: {url!r}")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "omarchy-triage-poc",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    token_scope = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    cache_key = (url, token_scope)
    with _etag_lock:
        cached = _etag_cache.get(cache_key)
    if cached:
        headers["If-None-Match"] = cached[0]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with _opener.open(req, timeout=timeout) as response:
                try:
                    body = _read_bounded(
                        response, MAX_RESPONSE_BYTES, label="response"
                    ).decode("utf-8")
                    result = json.loads(body) if body else None
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise GitHubError(f"GitHub returned invalid JSON: {exc}") from exc
                etag = response.headers.get("ETag")
                if etag:
                    with _etag_lock:
                        _etag_cache[cache_key] = (etag, result)
                return result
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached:
                return cached[1]
            detail = _read_bounded(
                exc, MAX_ERROR_BYTES, label="error response"
            ).decode("utf-8", errors="replace")
            response_headers = exc.headers or {}
            if exc.code in {403, 429} and (
                exc.code == 429
                or response_headers.get("X-RateLimit-Remaining") == "0"
                or "rate limit" in detail.lower()
            ):
                retry_after = response_headers.get("Retry-After")
                reset = response_headers.get("X-RateLimit-Reset")
                hint = f" Retry-After={retry_after}." if retry_after else ""
                hint += f" Reset={reset}." if reset else ""
                raise GitHubError(
                    f"GitHub rate limit reached; snapshot was not changed.{hint} Retry later."
                ) from exc
            if 300 <= exc.code < 400:
                raise GitHubError(
                    "GitHub redirect refused; credentials were not forwarded"
                ) from exc
            if exc.code not in {502, 503, 504} or attempt == MAX_ATTEMPTS:
                raise GitHubError(f"GitHub HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == MAX_ATTEMPTS:
                raise GitHubError(
                    f"GitHub network request failed after {MAX_ATTEMPTS} attempts: {exc}"
                ) from exc
        time.sleep(0.05 * (2 ** (attempt - 1)))
    raise GitHubError("GitHub request retry exhaustion")


def _transport(token: str | None):
    def request(endpoint: str, *, paginate: bool = False, timeout: float = 30.0) -> Any:
        if not token:
            raise GitHubError(
                "GITHUB_TOKEN is missing. Set it to refresh GitHub data "
                "(read-only token is enough). Cached snapshots remain available offline."
            )
        base_url = f"https://api.github.com/{endpoint}"
        if not paginate:
            return github_request("GET", base_url, token=token, timeout=timeout)
        merged: list[Any] = []
        page = 1
        is_files = "/files?" in endpoint
        deadline = time.monotonic() + min(timeout, MAX_PAGINATION_SECONDS)
        while page <= MAX_PAGINATION_PAGES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitHubError(
                    "GitHub pagination exceeded the total-time limit; "
                    "snapshot was not changed"
                )
            separator = "&" if "?" in base_url else "?"
            payload = github_request(
                "GET",
                f"{base_url}{separator}page={page}",
                token=token,
                timeout=remaining,
            )
            if not isinstance(payload, list):
                raise GitHubError("Unexpected paginated GitHub response")
            merged.extend(payload)
            if len(payload) < 100:
                return merged
            if is_files and len(merged) >= MAX_PULL_FILES:
                return merged[:MAX_PULL_FILES]
            page += 1
        raise GitHubError(
            f"GitHub pagination exceeded {MAX_PAGINATION_PAGES} pages; "
            "snapshot was not changed"
        )
    return request


def fetch_pulls(
    owner: str,
    repo: str,
    limit: int = 0,
    token: str | None = None,
    *,
    cache_dir: Path | None = None,
    refresh: bool = False,
    force: bool = False,
) -> list[PullRequest]:
    """Use the same pagination, staging, and completeness contract as ``gh``."""
    token = token if token is not None else os.environ.get("GITHUB_TOKEN")
    try:
        return fetch_pulls_with_transport(
            owner, repo, limit=limit, cache_dir=cache_dir,
            refresh=refresh, force=force, transport=_transport(token),
        )
    except GhError as exc:
        raise GitHubError(str(exc)) from exc


def parse_repo(repo: str) -> tuple[str, str]:
    parts = repo.strip("/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"repo must be owner/name, got {repo!r}")
    return parts[0], parts[1]
