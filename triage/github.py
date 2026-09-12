"""Read-only GitHub REST client. GET only; refuses mutating methods."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from triage.models import ChangedFile, PullRequest

ALLOWED_METHODS = frozenset({"GET"})


class GitHubError(RuntimeError):
    pass


def _require_get(method: str) -> None:
    upper = method.upper()
    if upper not in ALLOWED_METHODS:
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
    """Perform a GET-only request against the GitHub API."""
    _require_get(method)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "omarchy-triage-poc",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(f"GitHub HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"GitHub network error: {exc}") from exc


def fetch_pulls(owner: str, repo: str, limit: int = 0, token: str | None = None) -> list[PullRequest]:
    """Fetch open PRs and their files via GET only. limit=0 means all (single page max 100)."""
    token = token if token is not None else os.environ.get("GITHUB_TOKEN")
    if not token:
        raise GitHubError(
            "GITHUB_TOKEN is missing. Set it to use --source github "
            "(read-only token is enough). Fixtures work without a token."
        )
    per_page = 100 if limit == 0 else min(max(limit, 1), 100)
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/pulls"
        f"?state=open&per_page={per_page}&sort=created&direction=desc"
    )
    raw_pulls = github_request("GET", url, token=token)
    if not isinstance(raw_pulls, list):
        raise GitHubError("Unexpected response for pulls list")

    selected = list(raw_pulls) if limit == 0 else list(raw_pulls[: max(limit, 0)])
    results: list[PullRequest] = []
    for item in selected:
        number = int(item["number"])
        files_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files?per_page=100"
        raw_files = github_request("GET", files_url, token=token)
        changed: list[ChangedFile] = []
        if isinstance(raw_files, list):
            for f in raw_files:
                changed.append(
                    ChangedFile(
                        path=f.get("filename", ""),
                        patch=f.get("patch", "") or "",
                    )
                )
        user = ""
        if isinstance(item.get("user"), dict):
            user = item["user"].get("login", "")
        results.append(
            PullRequest(
                number=number,
                title=item.get("title", "") or "",
                body=item.get("body", "") or "",
                user=user,
                changed_files=changed,
                created_at=item.get("created_at", "") or "",
            )
        )
    return results


def parse_repo(repo: str) -> tuple[str, str]:
    parts = repo.strip("/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"repo must be owner/name, got {repo!r}")
    return parts[0], parts[1]
