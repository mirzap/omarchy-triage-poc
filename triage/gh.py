"""Read-only GitHub ingest via the local `gh` CLI. GET-only; refuses writes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from triage.models import ChangedFile, PullRequest, unified_patch_line_counts

DEFAULT_CACHE_DIR = Path(".triage") / "cache"
MAX_FILE_WORKERS = 8
PULLS_TIMEOUT = 300
FILE_TIMEOUT = 60
FETCH_METADATA_FILE = "fetch-metadata.json"
ACTIVE_SNAPSHOT_FILE = "active.json"
SNAPSHOTS_DIR = "snapshots"
MAX_PULL_FILES = 3000
MAX_ATTEMPTS = 3
# A files response is fetched through a mutable PR-number endpoint.  Keep the
# retry budget deliberately small: repeated movement is represented as an
# incomplete pending PR, never as an excuse to publish an older diff.
MAX_RECONCILIATION_ROUNDS = 2

ProgressCallback = Callable[[dict[str, Any]], None]


class GhError(RuntimeError):
    pass


def _is_allowed_api_path(path: str) -> bool:
    """Allow only GET pulls list and pulls/{n}/files under repos/{owner}/{repo}/."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        return False
    if any(ord(char) < 32 for char in path) or "%" in path or "#" in path:
        return False
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return False
    path_only = parsed.path
    parts = path_only.split("/")
    # repos/{owner}/{repo}/pulls
    # repos/{owner}/{repo}/pulls/{n}/files
    if len(parts) < 4 or parts[0] != "repos":
        return False
    if (
        not _OWNER_RE.fullmatch(parts[1])
        or not _REPO_RE.fullmatch(parts[2])
        or parts[2] in {".", ".."}
    ):
        return False
    if parts[3] != "pulls":
        return False
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if any(len(values) != 1 for values in query.values()):
        return False
    if len(parts) == 4:
        allowed = {"state", "per_page", "sort", "direction"}
        return (
            set(query) <= allowed
            and query.get("state", ["open"]) == ["open"]
            and query.get("per_page", ["100"]) == ["100"]
            and query.get("sort", ["created"]) == ["created"]
            and query.get("direction", ["desc"]) == ["desc"]
        )
    if len(parts) == 6 and parts[5] == "files" and parts[4].isdigit():
        return set(query) <= {"per_page"} and query.get("per_page", ["100"]) == ["100"]
    return False


def validate_gh_argv(argv: list[str]) -> None:
    """
    Refuse anything other than `gh api` GET of pulls / pulls/{n}/files.
    Raises GhError on disallowed invocations.
    """
    if len(argv) < 3 or argv[:2] != ["gh", "api"]:
        raise GhError(
            f"Refusing gh command {argv!r}. "
            "Only `gh api` GET for pulls / pulls/N/files is allowed "
            "(no `gh pr merge`, `gh pr create`, etc.)."
        )
    rest = argv[2:]
    if rest[:2] == ["--paginate", "--slurp"]:
        rest = rest[2:]
    elif rest[:1] == ["--paginate"]:
        # Accepted for compatibility with callers using a streaming decoder.
        # The internal builder always adds --slurp.
        rest = rest[1:]
    if len(rest) != 1 or rest[0].startswith("-"):
        raise GhError(
            "Refusing gh api flags. Only the exact internal forms "
            "`gh api ENDPOINT` and `gh api --paginate --slurp ENDPOINT` are allowed."
        )
    endpoint = rest[0]
    if not _is_allowed_api_path(endpoint):
        raise GhError(
            f"Refusing gh api path {endpoint!r}. "
            "Allowed: repos/{{owner}}/{{repo}}/pulls and "
            "repos/{{owner}}/{{repo}}/pulls/{{n}}/files only "
            "(paths containing /merge are refused)."
        )


def ensure_gh_auth() -> None:
    """Raise clear error if gh is missing or not authenticated."""
    if shutil.which("gh") is None:
        raise GhError(
            "`gh` CLI not found on PATH. Install GitHub CLI and run `gh auth login`."
        )
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except OSError as exc:
        raise GhError(
            f"Failed to run `gh auth status`: {exc}. Run `gh auth login`."
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise GhError(
            "GitHub CLI is not authenticated. Run `gh auth login`. "
            f"Detail: {detail or f'exit {proc.returncode}'}"
        )


def run_gh_api(
    endpoint: str,
    *,
    paginate: bool = False,
    timeout: float = PULLS_TIMEOUT,
) -> Any:
    """Run a validated `gh api` GET and return parsed JSON."""
    argv = ["gh", "api"]
    if paginate:
        argv.extend(["--paginate", "--slurp"])
    # Quote-safe: pass endpoint as a single argv element (no shell)
    argv.append(endpoint)
    validate_gh_argv(argv)
    proc: subprocess.CompletedProcess[str] | None = None
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout,
            )
        except OSError as exc:
            raise GhError(f"Failed to run gh: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            last_error = f"timed out after {timeout}s"
            if attempt == MAX_ATTEMPTS:
                raise GhError(
                    f"gh api {last_error} after {MAX_ATTEMPTS} attempts: {endpoint}"
                ) from exc
        else:
            if proc.returncode == 0:
                break
            last_error = (proc.stderr or proc.stdout or "").strip()
            lowered = last_error.lower()
            if "rate limit" in lowered or "http 429" in lowered:
                raise GhError(
                    "GitHub rate limit reached; snapshot was not changed. "
                    "Wait until the reported reset time, then retry. " + last_error
                )
            transient = any(
                marker in lowered
                for marker in ("http 502", "http 503", "http 504", "bad gateway",
                               "service unavailable", "connection reset", "temporarily unavailable")
            )
            if not transient:
                detail = last_error or f"exit {proc.returncode}"
                raise GhError(f"gh api failed permanently (exit {proc.returncode}): {detail}")
            if attempt == MAX_ATTEMPTS:
                detail = last_error or f"exit {proc.returncode}"
                raise GhError(
                    f"gh api failed after {MAX_ATTEMPTS} attempts "
                    f"(exit {proc.returncode}): {detail}"
                )
        time.sleep(0.05 * (2 ** (attempt - 1)))
    assert proc is not None
    body = proc.stdout.strip()
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GhError(f"Failed to parse gh api JSON: {exc}") from exc
    if paginate:
        if isinstance(parsed, list) and all(isinstance(page, list) for page in parsed):
            return [item for page in parsed for item in page]
        # Compatibility with mocked `gh` processes and versions that already
        # produce one JSON array. The invocation still always requests --slurp.
        if isinstance(parsed, list) and all(not isinstance(page, list) for page in parsed):
            return parsed
        if not isinstance(parsed, list):
            raise GhError("Unexpected gh --paginate --slurp JSON shape")
        return []
    return parsed


_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_KNOWN_LEGACY_REPOSITORIES = frozenset({("omacom", "omarchy")})
_legacy_match_index: dict[
    tuple[str, str, str], tuple[object, object, bool]
] = {}


def _validate_repository(owner: str, repo: str) -> None:
    """Reject names that could escape or alias a cache namespace."""
    if not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner):
        raise GhError(f"Invalid GitHub repository owner: {owner!r}")
    if (
        not isinstance(repo, str)
        or not _REPO_RE.fullmatch(repo)
        or repo in {".", ".."}
        or "/" in repo
        or "\\" in repo
    ):
        raise GhError(f"Invalid GitHub repository name: {repo!r}")


def _cache_root(owner: str, repo: str, base: Path | None = None) -> Path:
    """Canonical, collision-free cache namespace: ``base/owner/repo``."""
    _validate_repository(owner, repo)
    root = base or DEFAULT_CACHE_DIR
    return root / owner / repo


def _legacy_cache_root(owner: str, repo: str, base: Path | None = None) -> Path:
    """The pre-namespace cache path, used only for verified read fallback."""
    _validate_repository(owner, repo)
    root = base or DEFAULT_CACHE_DIR
    return root / f"{owner}-{repo}"


def _item_repository(item: dict[str, Any]) -> str | None:
    base = item.get("base")
    if isinstance(base, dict):
        base_repo = base.get("repo")
        if isinstance(base_repo, dict) and isinstance(base_repo.get("full_name"), str):
            return base_repo["full_name"]
    html_url = item.get("html_url")
    if isinstance(html_url, str) and html_url.startswith("https://github.com/"):
        path = html_url.removeprefix("https://github.com/").split("?", 1)[0]
        parts = path.strip("/").split("/")
        if len(parts) >= 4 and parts[2] == "pull":
            return f"{parts[0]}/{parts[1]}"
    return None


def _legacy_cache_matches(owner: str, repo: str, root: Path) -> bool:
    """Avoid reading an ambiguous flattened cache for the wrong repository."""
    metadata_path = root / FETCH_METADATA_FILE
    pulls_path = root / "pulls.json"
    key = (owner, repo, str(root.resolve()))
    metadata_signature = _path_signature(metadata_path)
    pulls_signature = _path_signature(pulls_path)
    indexed = _legacy_match_index.get(key)
    if (
        indexed is not None
        and indexed[0] == metadata_signature
        and indexed[1] == pulls_signature
    ):
        return indexed[2]

    metadata = _load_json(metadata_path)
    expected = f"{owner}/{repo}"
    metadata_repository = (
        metadata.get("repository") if isinstance(metadata, dict) else None
    )
    identity_known = isinstance(metadata_repository, str) and bool(metadata_repository)
    if identity_known:
        matches = metadata_repository == expected
    else:
        raw = _load_json(pulls_path)
        matches = False
        if isinstance(raw, list) and raw:
            items = [item for item in raw if isinstance(item, dict)]
            identities = [_item_repository(item) for item in items]
            if items and all(identity is not None for identity in identities):
                identity_known = True
                matches = set(identities) == {expected}

        # The original POC cache predates identity metadata. Keep its one known
        # production namespace readable, but never write or migrate it.
        if not identity_known:
            matches = (owner, repo) in _KNOWN_LEGACY_REPOSITORIES

    _legacy_match_index[key] = (metadata_signature, pulls_signature, matches)
    return matches


def _cache_read_root(owner: str, repo: str, base: Path | None = None) -> Path:
    canonical = _cache_root(owner, repo, base)
    active = _active_snapshot_root(canonical, f"{owner}/{repo}")
    if active is not None:
        return active
    if (canonical / "pulls.json").exists():
        return canonical
    legacy = _legacy_cache_root(owner, repo, base)
    if legacy.exists() and _legacy_cache_matches(owner, repo, legacy):
        return legacy
    return canonical


def _load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def _atomic_save_json(path: Path, data: Any) -> None:
    """Durably replace one JSON file without exposing a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _active_snapshot_root(root: Path, repository: str) -> Path | None:
    """Resolve an exact-repository snapshot, rejecting invalid publications."""
    try:
        manifest = _load_json(root / ACTIVE_SNAPSHOT_FILE)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GhError(
            f"Cached active snapshot pointer is unreadable; run an explicit refresh: {exc}"
        ) from exc
    if manifest is None:
        return None
    if not isinstance(manifest, dict):
        raise GhError("Cached active snapshot pointer is invalid; run an explicit refresh")
    snapshot_id = manifest.get("snapshot_id")
    if (
        not isinstance(snapshot_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", snapshot_id)
        or manifest.get("repository") != repository
    ):
        raise GhError(
            "Cached active snapshot does not match the requested repository; "
            "run an explicit refresh"
        )
    candidate = root / SNAPSHOTS_DIR / snapshot_id
    try:
        metadata = _load_json(candidate / FETCH_METADATA_FILE)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GhError(
            f"Cached active snapshot metadata is unreadable; run an explicit refresh: {exc}"
        ) from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("snapshot_id") != snapshot_id
        or metadata.get("repository") != repository
        or not (candidate / "pulls.json").is_file()
    ):
        raise GhError(
            "Cached active snapshot metadata is missing or mismatched; "
            "run an explicit refresh"
        )
    return candidate


def _pinned_snapshot_root(
    owner: str,
    repo: str,
    snapshot_id: str,
    cache_dir: Path | None,
) -> Path | None:
    """Resolve one immutable canonical snapshot without consulting active.json."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", snapshot_id):
        return None
    candidate = _cache_root(owner, repo, cache_dir) / SNAPSHOTS_DIR / snapshot_id
    try:
        metadata = _load_json(candidate / FETCH_METADATA_FILE)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    if metadata.get("snapshot_id") != snapshot_id:
        return None
    if metadata.get("repository") != f"{owner}/{repo}":
        return None
    if not (candidate / "pulls.json").is_file():
        return None
    return candidate


def _sha(item: dict[str, Any], side: str) -> str:
    value = item.get(side)
    return str(value.get("sha") or "") if isinstance(value, dict) else ""


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _file_evidence(raw_files: Any) -> tuple[list[ChangedFile], bool, int | None, int | None]:
    changed: list[ChangedFile] = []
    if not isinstance(raw_files, list):
        return changed, False, None, None
    complete = len(raw_files) < MAX_PULL_FILES
    additions = 0
    deletions = 0
    counts_complete = True
    for raw in raw_files:
        if not isinstance(raw, dict):
            complete = False
            continue
        status = str(raw.get("status") or "")
        patch_present = isinstance(raw.get("patch"), str) and bool(raw.get("patch", "").strip())
        previous_path = str(raw.get("previous_filename") or "")
        raw_additions = raw.get("additions")
        raw_deletions = raw.get("deletions")
        parsed_additions = _count(raw_additions)
        parsed_deletions = _count(raw_deletions)
        file_counts_complete = (
            parsed_additions is not None and parsed_deletions is not None
        )
        observed_counts = unified_patch_line_counts(raw.get("patch", "") if patch_present else "")
        patch_complete = bool(
            patch_present
            and file_counts_complete
            and observed_counts == (parsed_additions, parsed_deletions)
            and not (status == "renamed" and not previous_path)
        )
        if file_counts_complete:
            additions += parsed_additions
            deletions += parsed_deletions
        else:
            counts_complete = False
        complete = complete and patch_complete and file_counts_complete
        changed.append(
            ChangedFile(
                path=raw.get("filename", "") or raw.get("path", "") or "",
                patch=raw.get("patch", "") if patch_present else "",
                status=status,
                previous_path=previous_path,
                additions=parsed_additions,
                deletions=parsed_deletions,
                patch_complete=patch_complete,
            )
        )
    return changed, complete, additions if counts_complete else None, deletions if counts_complete else None


def _content_digest(repo: str, item: dict[str, Any], files: list[ChangedFile]) -> str:
    evidence = {
        "repository": repo,
        "number": int(item["number"]),
        "head_sha": _sha(item, "head"),
        "base_sha": _sha(item, "base"),
        "files": [f.to_dict() for f in files],
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_pr_item(
    item: dict[str, Any],
    raw_files: Any,
    repo: str,
    cache_snapshot_id: str = "",
) -> PullRequest:
    user = ""
    if isinstance(item.get("user"), dict):
        user = item["user"].get("login", "") or ""
    elif isinstance(item.get("user"), str):
        user = item["user"]
    number = int(item["number"])
    html_url = item.get("html_url") or f"https://github.com/{repo}/pull/{number}"
    files, evidence_complete, additions, deletions = _file_evidence(raw_files)
    head_sha = _sha(item, "head")
    base_sha = _sha(item, "base")
    updated_at = str(item.get("updated_at") or "")
    evidence_complete = evidence_complete and bool(head_sha and base_sha and updated_at)
    return PullRequest(
        number=number,
        title=item.get("title", "") or "",
        body=item.get("body", "") or "",
        user=user,
        changed_files=files,
        created_at=item.get("created_at", "") or "",
        html_url=html_url,
        head_sha=head_sha,
        base_sha=base_sha,
        updated_at=updated_at,
        additions=additions,
        deletions=deletions,
        evidence_complete=evidence_complete,
        content_digest=_content_digest(repo, item, files),
        evidence_source="github",
        cache_snapshot_id=cache_snapshot_id,
    )


def _parse_files(raw_files: Any) -> list[ChangedFile]:
    return _file_evidence(raw_files)[0]


def _emit_progress(
    progress: dict[str, Any] | None,
    on_progress: ProgressCallback | None,
    **fields: Any,
) -> None:
    if progress is not None:
        progress.update(fields)
    if on_progress is not None:
        snapshot = dict(progress) if progress is not None else dict(fields)
        on_progress(snapshot)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _path_timestamp(path: Path) -> str | None:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None
    return modified.isoformat().replace("+00:00", "Z")


def _pull_revision(item: dict[str, Any] | None) -> tuple[str, str, str] | None:
    """Return comparable revision evidence, or None when it is incomplete."""
    if not isinstance(item, dict):
        return None
    updated_at = item.get("updated_at")
    head = item.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    base = item.get("base")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    if not isinstance(updated_at, str) or not updated_at:
        return None
    if not isinstance(head_sha, str) or not head_sha:
        return None
    if not isinstance(base_sha, str) or not base_sha:
        return None
    return updated_at, head_sha, base_sha


def _evidence_revision(
    item: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """Return the SHA identity used to bind file evidence to a PR."""
    revision = _pull_revision(item)
    if revision is None:
        return None
    return revision[1], revision[2]


def _same_evidence_revision(
    left: tuple[str, str, str] | None,
    right: tuple[str, str, str] | None,
) -> bool:
    """Compare file evidence by head/base, not mutable listing metadata."""
    return (
        left is not None
        and right is not None
        and left[1:] == right[1:]
    )


def _validate_pulls_listing(raw: Any, *, source: str = "GitHub") -> list[dict[str, Any]]:
    """Validate a complete open-PR list before any number-based staging.

    GitHub normally returns integer PR numbers.  Refusing coercion is
    intentional: coercing malformed values (or accepting duplicate numbers)
    can bind a mutable PR-number files response to the wrong metadata.
    """
    if not isinstance(raw, list):
        raise GhError(f"Unexpected response for {source} pulls list")
    validated: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GhError(f"Invalid {source} pulls list item at index {index}")
        number = item.get("number")
        if type(number) is not int or number <= 0:
            raise GhError(
                f"Invalid PR number in {source} pulls list at index {index}"
            )
        if number in seen:
            raise GhError(f"Duplicate PR number in {source} pulls list: #{number}")
        seen.add(number)
        validated.append(item)
    return validated


def _file_metadata_revision(metadata: Any, number: int) -> tuple[str, str, str] | None:
    if not isinstance(metadata, dict):
        return None
    files = metadata.get("files")
    if not isinstance(files, dict):
        return None
    entry = files.get(str(number))
    if not isinstance(entry, dict):
        return None
    updated_at = entry.get("updated_at")
    head_sha = entry.get("head_sha")
    base_sha = entry.get("base_sha")
    if not isinstance(updated_at, str) or not updated_at:
        return None
    if not isinstance(head_sha, str) or not head_sha:
        return None
    if not isinstance(base_sha, str) or not base_sha:
        return None
    return updated_at, head_sha, base_sha


def _safe_legacy_root(owner: str, repo: str, base: Path | None) -> Path | None:
    legacy = _legacy_cache_root(owner, repo, base)
    if legacy.exists() and _legacy_cache_matches(owner, repo, legacy):
        return legacy
    return None


_process_sync_locks: dict[str, threading.Lock] = {}
_process_sync_locks_guard = threading.Lock()


@contextmanager
def _sync_lock(root: Path, timeout: float = 10.0):
    """Serialize snapshot publication across both threads and processes."""
    root.mkdir(parents=True, exist_ok=True)
    key = str(root.resolve())
    with _process_sync_locks_guard:
        thread_lock = _process_sync_locks.setdefault(key, threading.Lock())
    if not thread_lock.acquire(timeout=timeout):
        raise GhError(f"Timed out waiting for repository sync lock: {root}")
    lock_path = root / ".sync.lock"
    handle = lock_path.open("a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GhError(f"Timed out waiting for repository sync lock: {root}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            thread_lock.release()


def _listing_signature(items: list[Any]) -> str:
    items = _validate_pulls_listing(items)
    relevant: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda value: value["number"]):
        relevant.append(
            {
                "number": item["number"],
                "title": item.get("title") or "",
                "body": item.get("body") or "",
                "user": item.get("user"),
                "created_at": item.get("created_at") or "",
                "updated_at": item.get("updated_at") or "",
                "head_sha": _sha(item, "head"),
                "base_sha": _sha(item, "base"),
            }
        )
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _raw_files_for(root: Path, number: int, revision: tuple[str, str, str] | None) -> Any | None:
    if revision is None:
        return None
    metadata = _load_json(root / FETCH_METADATA_FILE)
    if not _same_evidence_revision(
        _file_metadata_revision(metadata, number), revision
    ):
        return None
    raw = _load_json(root / "files" / f"{number}.json")
    return raw if isinstance(raw, list) else None


def fetch_pulls_gh(
    owner: str,
    repo: str,
    limit: int = 0,
    cache_dir: Path | None = None,
    progress: dict[str, Any] | None = None,
    on_progress: ProgressCallback | None = None,
    refresh: bool = False,
    force: bool = False,
) -> list[PullRequest]:
    authenticated = False

    def authenticated_transport(endpoint: str, **kwargs: Any) -> Any:
        nonlocal authenticated
        if not authenticated:
            ensure_gh_auth()
            authenticated = True
        return run_gh_api(endpoint, **kwargs)

    return fetch_pulls_with_transport(
        owner,
        repo,
        limit=limit,
        cache_dir=cache_dir,
        progress=progress,
        on_progress=on_progress,
        refresh=refresh,
        force=force,
        transport=authenticated_transport,
    )


def fetch_pulls_with_transport(
    owner: str,
    repo: str,
    *,
    transport: Callable[..., Any],
    limit: int = 0,
    cache_dir: Path | None = None,
    progress: dict[str, Any] | None = None,
    on_progress: ProgressCallback | None = None,
    refresh: bool = False,
    force: bool = False,
) -> list[PullRequest]:
    """Canonical paginated, revision-consistent snapshot service."""
    _validate_repository(owner, repo)
    if type(limit) is not int or limit < 0:
        raise GhError("limit must be a non-negative integer")
    root = _cache_root(owner, repo, cache_dir)
    repo_slug = f"{owner}/{repo}"
    endpoint = f"repos/{owner}/{repo}/pulls?state=open&per_page=100&sort=created&direction=desc"
    refresh_requested = bool(refresh or force)

    def request(request_endpoint: str, **kwargs: Any) -> Any:
        try:
            return transport(request_endpoint, **kwargs)
        except Exception as exc:
            _emit_progress(
                progress, on_progress, phase="failed", error=str(exc),
                cache_status="preserved", message="sync failed; previous snapshot preserved",
            )
            raise

    def request_listing() -> list[dict[str, Any]]:
        try:
            return _validate_pulls_listing(
                request(endpoint, paginate=True, timeout=PULLS_TIMEOUT)
            )
        except GhError as exc:
            # A syntactically successful HTTP response can still be an unsafe
            # listing.  Keep the failure path identical to transport errors so
            # callers know the previous active snapshot remains authoritative.
            _emit_progress(
                progress,
                on_progress,
                phase="failed",
                error=str(exc),
                cache_status="preserved",
                message="sync failed; previous snapshot preserved",
            )
            raise

    with _sync_lock(root):
        read_root = _cache_read_root(owner, repo, cache_dir)
        old_pulls = _load_json(read_root / "pulls.json")
        listing_fetched = refresh_requested
        if old_pulls is not None:
            try:
                old_pulls = _validate_pulls_listing(old_pulls, source="cached")
            except GhError:
                # A refresh can replace a pre-snapshot/legacy preview whose
                # list has no canonical PR-number identity.  Cache-only reads
                # remain fail-closed and reject the malformed list.
                if not listing_fetched:
                    raise
                old_pulls = None
        raw_pulls = old_pulls
        if raw_pulls is None and not refresh_requested:
            raise GhError(
                "No cached GitHub snapshot is available. Run with refresh=True "
                "(CLI: --refresh) to fetch one explicitly."
            )

        _emit_progress(progress, on_progress, phase="listing", done=0, total=0,
                       message="listing open pulls" if listing_fetched else "using cached open-pull list")
        if listing_fetched:
            raw_pulls = request_listing()
            listed_at = _utc_timestamp()
            cache_status = "refreshed" if old_pulls is not None else "fresh"
        else:
            assert isinstance(raw_pulls, list)
            old_metadata = _load_json(read_root / FETCH_METADATA_FILE)
            listed_at = old_metadata.get("fetched_at") if isinstance(old_metadata, dict) else None
            listed_at = listed_at or _path_timestamp(read_root / "pulls.json")
            cache_status = "cached_stale"

        selected = list(raw_pulls if limit == 0 else raw_pulls[:limit])
        total = len(selected)
        limited = total < len(raw_pulls)
        _emit_progress(
            progress, on_progress, phase="listing", done=0, total=total,
            message=(f"{cache_status} list; selected {total} of {len(raw_pulls)} open pulls"
                     if limited else f"{cache_status} list with {len(raw_pulls)} open pulls"),
            cache_status=cache_status, fetched_at=listed_at,
            refresh_requested=refresh_requested, partial=limited, limited=limited,
            list_complete=True, selected_count=total, open_count=len(raw_pulls),
        )

        if not listing_fetched:
            cached_metadata = _load_json(read_root / FETCH_METADATA_FILE)
            cached_snapshot_id = (
                str(cached_metadata.get("snapshot_id") or "")
                if isinstance(cached_metadata, dict)
                else ""
            )
            results = []
            schema_version = (
                cached_metadata.get("schema_version")
                if isinstance(cached_metadata, dict)
                else None
            )
            for item in raw_pulls:
                if not isinstance(item, dict):
                    continue
                number = int(item["number"])
                raw_files = _raw_files_for(
                    read_root, number, _pull_revision(item)
                )
                legacy_preview = raw_files is None and schema_version != 2
                if legacy_preview:
                    candidate = _load_json(read_root / "files" / f"{number}.json")
                    raw_files = candidate if isinstance(candidate, list) else None
                pr = _parse_pr_item(
                    item, raw_files, repo_slug, cached_snapshot_id
                )
                if legacy_preview:
                    pr.evidence_complete = False
                    for changed in pr.changed_files:
                        changed.patch_complete = False
                results.append(pr)
            _emit_progress(progress, on_progress, phase="done", done=total, total=total,
                           message=f"loaded {len(results)} open PRs from cached snapshot",
                           cached_files=total, fetched_files=0, refreshed_files=0)
            return results

        snapshot_id = (
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-"
            f"{uuid.uuid4().hex[:12]}"
        )
        stage_root = root / SNAPSHOTS_DIR / snapshot_id
        progress_lock = threading.Lock()
        counts = {"done": 0, "cached": 0, "fetched": 0, "refreshed": 0}

        # The limit is a cap on file scopes for this refresh.  Keep the scope
        # stable while reconciling so a reordered list cannot turn one refresh
        # into an unbounded series of downloads.  Unlimited refreshes include
        # newly observed PRs in each bounded round.
        scope_numbers = (
            None
            if limit == 0
            else {item["number"] for item in selected}
        )
        # number -> (listing revision metadata, raw files).  The revision is
        # retained with the staged bytes so a later confirmation can never
        # publish those bytes under a different head/base pair.
        staged: dict[int, tuple[tuple[str, str, str], list[Any]]] = {}
        staged_fetched_at: dict[int, str] = {}

        def file_status(
            item: dict[str, Any], raw_files: list[Any], *, outside_limit: bool = False,
        ) -> dict[str, Any]:
            revision = _pull_revision(item)
            status: dict[str, Any] = {
                "fetched_at": staged_fetched_at.get(item["number"], listed_at),
                "updated_at": revision[0] if revision else None,
                "head_sha": revision[1] if revision else None,
                "base_sha": revision[2] if revision else None,
                "evidence_complete": _parse_pr_item(
                    item, raw_files, repo_slug, snapshot_id
                ).evidence_complete,
                "file_count": len(raw_files),
                "files_cap_reached": len(raw_files) >= MAX_PULL_FILES,
                "missing_patch_count": sum(
                    1 for raw in raw_files
                    if isinstance(raw, dict)
                    and (
                        not isinstance(raw.get("patch"), str)
                        or not raw.get("patch", "").strip()
                    )
                ),
            }
            if outside_limit:
                status["reused_outside_limit"] = True
            return status

        def stage_one(
            item: dict[str, Any],
            *,
            reconciliation_round: int,
            total_scopes: int,
        ) -> tuple[int, tuple[str, str, str] | None, list[Any] | None, bool]:
            number = item["number"]
            revision = _pull_revision(item)
            existing = staged.get(number)
            # Unknown revisions are deliberately evidence-less.  In
            # particular, never retain old bytes merely because the number is
            # still present in the open list.
            if revision is None:
                staged.pop(number, None)
                staged_fetched_at.pop(number, None)
                raw_files = None
                reused = False
            elif existing is not None and _same_evidence_revision(existing[0], revision):
                raw_files = existing[1]
                reused = True
                staged_fetched_at.setdefault(number, listed_at)
            else:
                staged.pop(number, None)
                staged_fetched_at.pop(number, None)
                raw_files = _raw_files_for(read_root, number, revision)
                reused = raw_files is not None
                if raw_files is None:
                    files_endpoint = (
                        f"repos/{owner}/{repo}/pulls/{number}/files?per_page=100"
                    )
                    raw_files = request(
                        files_endpoint, paginate=True, timeout=FILE_TIMEOUT
                    )
                    if not isinstance(raw_files, list):
                        raise GhError(f"Unexpected response for PR #{number} files")
                staged[number] = (revision, raw_files)
                staged_fetched_at[number] = listed_at if reused else _utc_timestamp()
            with progress_lock:
                counts["done"] += 1
                if raw_files is not None:
                    counts["cached" if reused else "fetched"] += 1
                    if not reused and old_pulls is not None:
                        counts["refreshed"] += 1
                    _atomic_save_json(stage_root / "files" / f"{number}.json", raw_files)
                _emit_progress(
                    progress,
                    on_progress,
                    phase="files",
                    done=counts["done"],
                    total=total_scopes,
                    reconciliation_round=reconciliation_round,
                    message=f"files {counts['done']}/{total_scopes}",
                    cached_files=counts["cached"],
                    fetched_files=counts["fetched"],
                    refreshed_files=counts["refreshed"],
                )
            return number, revision, raw_files, reused

        def stage_listing(
            listing: list[dict[str, Any]],
            *,
            reconciliation_round: int,
        ) -> None:
            # Progress is scoped to this stage/reconciliation round.  The
            # cumulative fetched/cached counters remain useful in the final
            # publication status, but done/total must not grow past total.
            counts["done"] = 0
            if scope_numbers is None:
                scoped = list(listing)
                scope_total = len(scoped)
            else:
                scoped = [item for item in listing if item["number"] in scope_numbers]
                scope_total = len(scope_numbers)
            if scoped:
                with ThreadPoolExecutor(max_workers=MAX_FILE_WORKERS) as pool:
                    futures = [
                        pool.submit(
                            stage_one,
                            item,
                            reconciliation_round=reconciliation_round,
                            total_scopes=scope_total,
                        )
                        for item in scoped
                    ]
                    for future in as_completed(futures):
                        future.result()
            # Reuse complete evidence outside the file cap when it is bound to
            # the exact current SHA pair.  Changed/new outside-cap PRs remain
            # stubs; they are never downloaded as a side effect of reordering.
            scoped_set = {item["number"] for item in scoped}
            for item in listing:
                number = item["number"]
                if number in scoped_set:
                    continue
                revision = _pull_revision(item)
                existing = staged.get(number)
                if revision is None:
                    staged.pop(number, None)
                    staged_fetched_at.pop(number, None)
                    continue
                if existing is not None and _same_evidence_revision(existing[0], revision):
                    staged_fetched_at.setdefault(number, listed_at)
                    continue
                staged.pop(number, None)
                staged_fetched_at.pop(number, None)
                raw_files = _raw_files_for(read_root, number, revision)
                if raw_files is not None:
                    staged[number] = (revision, raw_files)
                    staged_fetched_at[number] = listed_at
                    _atomic_save_json(stage_root / "files" / f"{number}.json", raw_files)

        current_listing = raw_pulls
        final_listing: list[dict[str, Any]] | None = None
        for reconciliation_round in range(MAX_RECONCILIATION_ROUNDS + 1):
            if reconciliation_round:
                _emit_progress(
                    progress,
                    on_progress,
                    phase="reconciling",
                    done=0,
                    total=(len(current_listing) if limit == 0 else min(limit, len(current_listing))),
                    reconciliation_round=reconciliation_round,
                    message=(
                        f"reconciling changed PR revisions (round "
                        f"{reconciliation_round}/{MAX_RECONCILIATION_ROUNDS})"
                    ),
                )
            stage_listing(current_listing, reconciliation_round=reconciliation_round)
            confirmed = request_listing()
            current_by_number = {item["number"]: item for item in current_listing}
            confirmed_by_number = {item["number"]: item for item in confirmed}
            revision_changed = any(
                number in confirmed_by_number
                and (
                    number not in current_by_number
                    or _evidence_revision(current_by_number[number])
                    != _evidence_revision(confirmed_by_number[number])
                )
                and (scope_numbers is None or number in scope_numbers)
                for number in set(current_by_number) | set(confirmed_by_number)
            )
            if not revision_changed or reconciliation_round >= MAX_RECONCILIATION_ROUNDS:
                final_listing = confirmed
                break
            current_listing = confirmed
        assert final_listing is not None

        # Materialize results against the final complete list.  A staged file
        # is usable only when its recorded SHA pair still matches that final
        # listing.  This is the fail-closed boundary for repeatedly moving PRs.
        _atomic_save_json(stage_root / "pulls.json", final_listing)
        files_metadata: dict[str, Any] = {}
        results: list[PullRequest] = []
        for item in final_listing:
            number = item["number"]
            revision = _pull_revision(item)
            evidence = staged.get(number)
            raw_files = (
                evidence[1]
                if revision is not None
                and evidence is not None
                and _same_evidence_revision(evidence[0], revision)
                else None
            )
            if raw_files is not None:
                pr = _parse_pr_item(item, raw_files, repo_slug, snapshot_id)
                files_metadata[str(number)] = file_status(
                    item,
                    raw_files,
                    outside_limit=(
                        scope_numbers is not None and number not in scope_numbers
                    ),
                )
            else:
                # No files manifest is emitted for an unknown or repeatedly
                # moving revision, so cached_pr_evidence cannot surface stale
                # patches for this final PR number.
                staged.pop(number, None)
                staged_fetched_at.pop(number, None)
                try:
                    (stage_root / "files" / f"{number}.json").unlink()
                except FileNotFoundError:
                    pass
                pr = _parse_pr_item(item, None, repo_slug, snapshot_id)
            results.append(pr)

        # A PR can disappear or lose its revision while the bounded
        # reconciliation is running.  Do not leave unreferenced staged files
        # in the immutable snapshot where a future reader could mistake them
        # for evidence.
        staged_files_root = stage_root / "files"
        if staged_files_root.is_dir():
            for path in staged_files_root.glob("*.json"):
                if path.stem not in files_metadata:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass

        raw_pulls = final_listing
        total = min(limit, len(raw_pulls)) if limit else len(raw_pulls)
        limited = bool(limit and len(raw_pulls) > limit)

        generation = 1
        active = _load_json(root / ACTIVE_SNAPSHOT_FILE)
        if isinstance(active, dict) and type(active.get("generation")) is int:
            generation = active["generation"] + 1
        metadata = {
            "schema_version": 2,
            "repository": repo_slug,
            "snapshot_id": snapshot_id,
            "generation": generation,
            "fetched_at": listed_at,
            "cache_status": cache_status,
            "list_complete": True,
            "limited": limited,
            "selected_count": total,
            "open_count": len(raw_pulls),
            "evidence_complete": all(pr.evidence_complete for pr in results),
            "files": files_metadata,
        }
        _atomic_save_json(stage_root / FETCH_METADATA_FILE, metadata)
        _atomic_save_json(root / ACTIVE_SNAPSHOT_FILE, metadata)

        _emit_progress(
            progress, on_progress, phase="done", done=total, total=total,
            message=f"published snapshot {snapshot_id} with {len(results)} open PRs ({total} file scopes)",
            snapshot_id=snapshot_id, generation=generation,
            cached_files=counts["cached"], fetched_files=counts["fetched"],
            refreshed_files=counts["refreshed"], evidence_complete=metadata["evidence_complete"],
        )
        return results


def gh_available() -> bool:
    """True if gh is on PATH and authenticated."""
    try:
        ensure_gh_auth()
        return True
    except GhError:
        return False


_PathSignature = tuple[int, int, int, int] | None
_pulls_index: dict[
    tuple[str, str, str],
    tuple[
        _PathSignature,
        dict[int, dict[str, Any]],
        dict[int, tuple[str, str, str] | None],
    ],
] = {}
_fetch_metadata_index: dict[str, tuple[_PathSignature, dict[str, Any]]] = {}


def _path_signature(path: Path) -> _PathSignature:
    """Cheap signature robust to in-place writes and atomic file replacement."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_size


def _cached_pulls_indexes(
    owner: str,
    repo: str,
    root: Path,
) -> tuple[dict[int, dict[str, Any]], dict[int, tuple[str, str, str] | None]]:
    pulls_path = root / "pulls.json"
    key = (owner, repo, str(root.resolve()))
    signature = _path_signature(pulls_path)
    indexed = _pulls_index.get(key)
    if indexed is None or indexed[0] != signature:
        raw = _load_json(pulls_path)
        metadata_by_number: dict[int, dict[str, Any]] = {}
        revisions_by_number: dict[int, tuple[str, str, str] | None] = {}
        sync_metadata = _load_json(root / FETCH_METADATA_FILE)
        snapshot_id = sync_metadata.get("snapshot_id", "") if isinstance(sync_metadata, dict) else ""
        repository = sync_metadata.get("repository", "") if isinstance(sync_metadata, dict) else ""
        fetched_at = sync_metadata.get("fetched_at", "") if isinstance(sync_metadata, dict) else ""
        cache_status = sync_metadata.get("cache_status", "legacy") if isinstance(sync_metadata, dict) else "legacy"
        files_metadata = sync_metadata.get("files", {}) if isinstance(sync_metadata, dict) else {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict) or "number" not in item:
                    continue
                number = int(item["number"])
                user = ""
                u = item.get("user")
                if isinstance(u, dict):
                    user = u.get("login") or ""
                elif isinstance(u, str):
                    user = u
                revision = _pull_revision(item)
                file_status = files_metadata.get(str(number), {}) if isinstance(files_metadata, dict) else {}
                metadata_by_number[number] = {
                    "number": number,
                    "title": item.get("title") or "",
                    "body": item.get("body") or "",
                    "user": user,
                    "html_url": item.get("html_url") or "",
                    "head_sha": revision[1] if revision else "",
                    "base_sha": revision[2] if revision else "",
                    "updated_at": revision[0] if revision else "",
                    "evidence_complete": bool(file_status.get("evidence_complete", False)),
                    "file_count": file_status.get("file_count"),
                    "files_cap_reached": bool(file_status.get("files_cap_reached", False)),
                    "missing_patch_count": int(file_status.get("missing_patch_count", 0) or 0),
                    "snapshot_id": snapshot_id,
                    "repository": repository,
                    "cache_status": cache_status,
                    "fetched_at": fetched_at,
                }
                revisions_by_number[number] = revision
        indexed = (signature, metadata_by_number, revisions_by_number)
        _pulls_index[key] = indexed
    return indexed[1], indexed[2]


def _cached_fetch_metadata(root: Path) -> dict[str, Any]:
    path = root / FETCH_METADATA_FILE
    key = str(root.resolve())
    signature = _path_signature(path)
    indexed = _fetch_metadata_index.get(key)
    if indexed is None or indexed[0] != signature:
        raw = _load_json(path)
        metadata = raw if isinstance(raw, dict) else {}
        indexed = (signature, metadata)
        _fetch_metadata_index[key] = indexed
    return indexed[1]


def cached_pr_files(
    owner: str,
    repo: str,
    number: int,
    cache_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Read revision-consistent cached pull files. Empty list on cache miss."""
    number = int(number)
    canonical_root = _cache_root(owner, repo, cache_dir)
    pulls_root = _cache_read_root(owner, repo, cache_dir)
    canonical_pulls_exist = pulls_root != _safe_legacy_root(owner, repo, cache_dir)
    pull_metadata, pull_revisions = _cached_pulls_indexes(owner, repo, pulls_root)

    # A canonical open-list cache is authoritative: an omitted PR is closed (or
    # otherwise no longer selected), and an unverifiable patch must not leak
    # through from an older canonical or flattened cache.
    if canonical_pulls_exist and number not in pull_metadata:
        return []
    pull_revision = pull_revisions.get(number)

    roots = [pulls_root]
    legacy_root = _safe_legacy_root(owner, repo, cache_dir)
    if legacy_root is not None and legacy_root != canonical_root:
        roots.append(legacy_root)

    raw: Any = None
    for candidate_root in roots:
        candidate = candidate_root / "files" / f"{number}.json"
        if not candidate.exists():
            continue
        file_revision = _file_metadata_revision(
            _cached_fetch_metadata(candidate_root), number
        )
        if canonical_pulls_exist:
            if pull_revision is None or file_revision != pull_revision:
                continue
        elif (
            pull_revision is not None
            and file_revision is not None
            and pull_revision != file_revision
        ):
            continue
        raw = _load_json(candidate)
        break
    if not isinstance(raw, list):
        return []
    status_metadata = _cached_fetch_metadata(pulls_root)
    schema_version = status_metadata.get("schema_version") if isinstance(status_metadata, dict) else None
    out: list[dict[str, Any]] = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        item: dict[str, Any] = {
            "path": f.get("filename") or f.get("path") or "",
            "patch": f.get("patch") or "",
        }
        if schema_version == 2:
            patch_present = isinstance(f.get("patch"), str)
            status = str(f.get("status") or "")
            previous_path = str(f.get("previous_filename") or "")
            additions = _count(f.get("additions"))
            deletions = _count(f.get("deletions"))
            item.update(
                status=status,
                previous_path=previous_path,
                additions=additions,
                deletions=deletions,
                patch_complete=bool(
                    patch_present
                    and unified_patch_line_counts(f.get("patch") or "")
                    == (additions, deletions)
                    and not (status == "renamed" and not previous_path)
                ),
            )
        out.append(item)
    return out


def cached_pr_meta(
    owner: str,
    repo: str,
    number: int,
    cache_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Title/body/user from cached pulls.json. No network."""
    root = _cache_read_root(owner, repo, cache_dir)
    metadata_by_number, _ = _cached_pulls_indexes(owner, repo, root)
    return metadata_by_number.get(int(number))


def cached_sync_status(
    owner: str,
    repo: str,
    cache_dir: Path | None = None,
    *,
    snapshot_id: str | None = None,
) -> dict[str, Any] | None:
    """Return published snapshot status without authentication or network access."""
    if snapshot_id is not None:
        if not snapshot_id:
            return None
        root = _pinned_snapshot_root(owner, repo, snapshot_id, cache_dir)
        if root is None:
            return None
    else:
        root = _cache_read_root(owner, repo, cache_dir)
    metadata = _cached_fetch_metadata(root)
    if not metadata:
        return None
    return {
        key: metadata.get(key)
        for key in (
            "schema_version", "repository", "snapshot_id", "generation", "fetched_at",
            "cache_status", "list_complete", "limited", "selected_count", "open_count",
            "evidence_complete",
        )
    }


def cached_pr_evidence(
    owner: str,
    repo: str,
    number: int,
    cache_dir: Path | None = None,
    *,
    snapshot_id: str | None = None,
) -> dict[str, Any] | None:
    """Pin and read metadata, files, and status from one immutable snapshot.

    This is the authoritative HTTP/ranking read contract. Unlike calling the
    convenience helpers separately, an active-pointer swap cannot mix metadata
    from one snapshot with patches from another.
    """
    number = int(number)
    legacy_unverified = False
    if snapshot_id is not None:
        if not snapshot_id:
            # Explicit blank means a pre-snapshot store record. Read only the
            # unchanged legacy location, never whichever snapshot is active.
            root = _cache_root(owner, repo, cache_dir)
            if not (root / "pulls.json").is_file():
                root = _safe_legacy_root(owner, repo, cache_dir)
            if root is None:
                return None
            legacy_unverified = True
        else:
            root = _pinned_snapshot_root(owner, repo, snapshot_id, cache_dir)
            if root is None:
                return None
    else:
        root = _cache_read_root(owner, repo, cache_dir)
    meta_by_number, revisions = _cached_pulls_indexes(owner, repo, root)
    meta = meta_by_number.get(number)
    if meta is None:
        return None
    sync_metadata = _cached_fetch_metadata(root)
    schema_version = sync_metadata.get("schema_version") if sync_metadata else None
    raw = _load_json(root / "files" / f"{number}.json")
    pull_revision = revisions.get(number)
    file_revision = _file_metadata_revision(sync_metadata, number)
    revision_matches = pull_revision is not None and file_revision == pull_revision
    if schema_version == 2 and not revision_matches:
        raw = None
    files: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            previous_path = str(item.get("previous_filename") or "")
            patch_present = isinstance(item.get("patch"), str) and bool(item.get("patch", "").strip())
            additions = _count(item.get("additions"))
            deletions = _count(item.get("deletions"))
            observed_counts = unified_patch_line_counts(
                item.get("patch", "") if patch_present else ""
            )
            files.append(
                {
                    "path": item.get("filename") or item.get("path") or "",
                    "patch": item.get("patch") if patch_present else "",
                    "status": status,
                    "previous_path": previous_path,
                    "additions": additions,
                    "deletions": deletions,
                    # Legacy patches have no trustworthy revision provenance.
                    "patch_complete": bool(
                        not legacy_unverified
                        and schema_version == 2
                        and revision_matches
                        and patch_present
                        and observed_counts == (additions, deletions)
                        and not (status == "renamed" and not previous_path)
                    ),
                }
            )
    sync = {
        key: sync_metadata.get(key)
        for key in (
            "schema_version", "repository", "snapshot_id", "generation", "fetched_at",
            "cache_status", "list_complete", "limited", "selected_count", "open_count",
            "evidence_complete",
        )
    } if sync_metadata else None
    resolved_snapshot_id = (
        "" if legacy_unverified else sync_metadata.get("snapshot_id", "")
        if sync_metadata
        else ""
    )
    if legacy_unverified:
        meta = dict(meta)
        meta["evidence_complete"] = False
        meta["snapshot_id"] = ""
        for item in files:
            item["patch_complete"] = False
        if sync is not None:
            sync = dict(sync)
            sync["evidence_complete"] = False
            sync["snapshot_id"] = ""
    return {
        "meta": meta,
        "files": files,
        "sync": sync,
        "snapshot_id": resolved_snapshot_id,
        "legacy_unverified": legacy_unverified,
    }
