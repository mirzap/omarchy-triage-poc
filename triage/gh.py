"""Read-only GitHub ingest via the local `gh` CLI. GET-only; refuses writes."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from triage.models import ChangedFile, PullRequest

DEFAULT_CACHE_DIR = Path(".triage") / "cache"
MAX_FILE_WORKERS = 8
PULLS_TIMEOUT = 300
FILE_TIMEOUT = 60
FETCH_METADATA_FILE = "fetch-metadata.json"

ProgressCallback = Callable[[dict[str, Any]], None]


class GhError(RuntimeError):
    pass


_FORBIDDEN_FLAGS = ("-X", "--method", "--input")
_MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE", "MERGE"})
_ALLOWED_PATH_PREFIXES = (
    "repos/",
)


def _is_allowed_api_path(path: str) -> bool:
    """Allow only GET pulls list and pulls/{n}/files under repos/{owner}/{repo}/."""
    cleaned = path.lstrip("/")
    # Strip query string for path checks
    path_only = cleaned.split("?", 1)[0]
    if "/merge" in path_only.lower():
        return False
    parts = path_only.split("/")
    # repos/{owner}/{repo}/pulls
    # repos/{owner}/{repo}/pulls/{n}/files
    if len(parts) < 4 or parts[0] != "repos":
        return False
    if parts[3] != "pulls":
        return False
    if len(parts) == 4:
        return True
    if len(parts) == 6 and parts[5] == "files" and parts[4].isdigit():
        return True
    return False


def validate_gh_argv(argv: list[str]) -> None:
    """
    Refuse anything other than `gh api` GET of pulls / pulls/{n}/files.
    Raises GhError on disallowed invocations.
    """
    if not argv:
        raise GhError("Empty gh command refused")
    # Expect: gh api [--paginate] <endpoint>
    if argv[0] != "gh":
        raise GhError(f"Refusing non-gh command: {argv[0]!r}")
    if len(argv) < 2 or argv[1] != "api":
        raise GhError(
            f"Refusing gh subcommand {argv[1:]!r}. "
            "Only `gh api` GET for pulls / pulls/N/files is allowed "
            "(no `gh pr merge`, `gh pr create`, etc.)."
        )
    joined = " ".join(argv).lower()
    if " pr merge" in f" {joined}" or "pr merge" in joined:
        raise GhError("Refusing `gh pr merge` — this POC never writes to GitHub.")
    if " pr create" in f" {joined}":
        raise GhError("Refusing `gh pr create` — this POC never writes to GitHub.")

    method = "GET"
    endpoint: str | None = None
    i = 2
    while i < len(argv):
        arg = argv[i]
        if arg in ("-X", "--method"):
            if i + 1 >= len(argv):
                raise GhError("Refusing incomplete -X/--method flag")
            method = argv[i + 1].upper()
            i += 2
            continue
        if arg.startswith("-X") and arg != "-X":
            # -XPOST style
            method = arg[2:].upper()
            i += 1
            continue
        if arg == "--input" or arg.startswith("--input="):
            raise GhError("Refusing --input (would enable write body)")
        if arg in ("--paginate",):
            i += 1
            continue
        if arg.startswith("-"):
            # Allow harmless flags like -H but still require GET path
            i += 1
            continue
        # positional endpoint
        endpoint = arg
        i += 1

    if method in _MUTATING_METHODS or method != "GET":
        raise GhError(
            f"Refusing non-GET gh api method {method!r}. "
            "This POC is read-only (no POST/PATCH/PUT/DELETE/MERGE)."
        )
    if endpoint is None:
        raise GhError("Refusing gh api without an endpoint path")
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
        argv.append("--paginate")
    # Quote-safe: pass endpoint as a single argv element (no shell)
    argv.append(endpoint)
    validate_gh_argv(argv)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except OSError as exc:
        raise GhError(f"Failed to run gh: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh api timed out after {timeout}s: {endpoint}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise GhError(f"gh api failed (exit {proc.returncode}): {err}")
    body = proc.stdout.strip()
    if not body:
        return None
    # --paginate may concatenate JSON arrays; handle NDJSON-ish concat of arrays
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # gh --paginate sometimes emits concatenated arrays: ][
        fixed = body.replace("][", "],[")
        try:
            chunks = json.loads(f"[{fixed}]")
            if isinstance(chunks, list) and all(isinstance(c, list) for c in chunks):
                merged: list[Any] = []
                for c in chunks:
                    merged.extend(c)
                return merged
        except json.JSONDecodeError as exc:
            raise GhError(f"Failed to parse gh api JSON: {exc}") from exc
        raise GhError("Unexpected gh api JSON shape after paginate merge")


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


def _parse_pr_item(item: dict[str, Any], files: list[ChangedFile], repo: str) -> PullRequest:
    user = ""
    if isinstance(item.get("user"), dict):
        user = item["user"].get("login", "") or ""
    elif isinstance(item.get("user"), str):
        user = item["user"]
    number = int(item["number"])
    html_url = item.get("html_url") or f"https://github.com/{repo}/pull/{number}"
    return PullRequest(
        number=number,
        title=item.get("title", "") or "",
        body=item.get("body", "") or "",
        user=user,
        changed_files=files,
        created_at=item.get("created_at", "") or "",
        html_url=html_url,
    )


def _parse_files(raw_files: Any) -> list[ChangedFile]:
    changed: list[ChangedFile] = []
    if not isinstance(raw_files, list):
        return changed
    for f in raw_files:
        if not isinstance(f, dict):
            continue
        changed.append(
            ChangedFile(
                path=f.get("filename", "") or f.get("path", "") or "",
                patch=f.get("patch", "") or "",
            )
        )
    return changed


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
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _path_timestamp(path: Path) -> str | None:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None
    return modified.isoformat().replace("+00:00", "Z")


def _pull_revision(item: dict[str, Any] | None) -> tuple[str, str] | None:
    """Return comparable revision evidence, or None when it is incomplete."""
    if not isinstance(item, dict):
        return None
    updated_at = item.get("updated_at")
    head = item.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(updated_at, str) or not updated_at:
        return None
    if not isinstance(head_sha, str) or not head_sha:
        return None
    return updated_at, head_sha


def _file_metadata_revision(metadata: Any, number: int) -> tuple[str, str] | None:
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
    if not isinstance(updated_at, str) or not updated_at:
        return None
    if not isinstance(head_sha, str) or not head_sha:
        return None
    return updated_at, head_sha


def _safe_legacy_root(owner: str, repo: str, base: Path | None) -> Path | None:
    legacy = _legacy_cache_root(owner, repo, base)
    if legacy.exists() and _legacy_cache_matches(owner, repo, legacy):
        return legacy
    return None


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
    """
    Fetch open PRs + files via `gh api` (GET only).
    limit=0 means all open PRs (paginate). Default 0 = all.
    Caches under .triage/cache/{owner}/{repo}/.
    With refresh=True (or its force=True alias), re-list all open PRs and
    re-fetch files whose updated_at/head SHA revision changed or is unknown.
    Without refresh, a cache hit is deliberately reported as cached/stale.
    Parallelizes per-PR file fetches (max 8 workers). Still GET-only.
    """
    repo_slug = f"{owner}/{repo}"
    root = _cache_root(owner, repo, cache_dir)
    read_root = _cache_read_root(owner, repo, cache_dir)
    legacy_root = _safe_legacy_root(owner, repo, cache_dir)
    ensure_gh_auth()
    pulls_cache = root / "pulls.json"
    cached_pulls_path = read_root / "pulls.json"
    metadata_path = root / FETCH_METADATA_FILE
    metadata = _load_json(metadata_path)
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.setdefault("repository", repo_slug)
    files_metadata = metadata.get("files")
    if not isinstance(files_metadata, dict):
        files_metadata = {}
        metadata["files"] = files_metadata
    legacy_metadata: Any = None
    if legacy_root is not None:
        legacy_metadata = _load_json(legacy_root / FETCH_METADATA_FILE)

    _emit_progress(
        progress,
        on_progress,
        phase="listing",
        done=0,
        total=0,
        message="listing open pulls",
    )

    old_raw_pulls = _load_json(cached_pulls_path)
    refresh_requested = bool(refresh or force)
    listing_fetched = old_raw_pulls is None or refresh_requested
    listed_at: str | None = None
    if listing_fetched:
        # Quote query so shell never sees ? — we pass argv list, but still encode
        endpoint = f"repos/{owner}/{repo}/pulls?state=open&per_page=100&sort=created&direction=desc"
        print(f"[gh] fetching pulls for {repo_slug} ...", flush=True)
        raw_pulls = run_gh_api(endpoint, paginate=True, timeout=PULLS_TIMEOUT)
        if not isinstance(raw_pulls, list):
            raise GhError("Unexpected response for pulls list")
        listed_at = _utc_timestamp()
        _save_json(pulls_cache, raw_pulls)
        metadata["repository"] = repo_slug
        metadata["pulls_fetched_at"] = listed_at
        _save_json(metadata_path, metadata)
        print(f"[gh] cached {len(raw_pulls)} pulls -> {pulls_cache}", flush=True)
        cache_status = "refreshed" if old_raw_pulls is not None else "fresh"
    else:
        raw_pulls = old_raw_pulls
        source_metadata = _load_json(read_root / FETCH_METADATA_FILE)
        if isinstance(source_metadata, dict):
            candidate = source_metadata.get("pulls_fetched_at")
            if isinstance(candidate, str) and candidate:
                listed_at = candidate
        if listed_at is None:
            listed_at = _path_timestamp(cached_pulls_path)
        cache_status = "cached_stale"
        print(
            f"[gh] cache hit (stale): {cached_pulls_path} ({len(raw_pulls)} pulls)"
            + (f"; fetched {listed_at}" if listed_at else ""),
            flush=True,
        )

    if not isinstance(raw_pulls, list):
        raise GhError("Cached pulls.json is not a list")

    if limit == 0:
        selected = list(raw_pulls)
    else:
        selected = list(raw_pulls[: max(limit, 0)])

    total = len(selected)
    partial = total < len(raw_pulls)
    status_message = {
        "cached_stale": "using cached/stale open-pull list",
        "refreshed": "refreshed open-pull list",
        "fresh": "fetched open-pull list",
    }[cache_status]
    if listed_at:
        status_message += f" (as of {listed_at})"
    if partial:
        status_message += f"; selected {total} of {len(raw_pulls)} open pulls"
    _emit_progress(
        progress,
        on_progress,
        phase="listing",
        done=0,
        total=total,
        message=status_message,
        cache_status=cache_status,
        fetched_at=listed_at,
        refresh_requested=refresh_requested,
        partial=partial,
        selected_count=total,
        open_count=len(raw_pulls),
    )
    _emit_progress(
        progress,
        on_progress,
        phase="files",
        done=0,
        total=total,
        message=f"files 0/{total}",
    )

    files_dir = root / "files"
    cache_lock = threading.Lock()
    progress_lock = threading.Lock()
    done_count = 0
    cached_file_count = 0
    fetched_file_count = 0
    refreshed_file_count = 0

    def _cached_file(number: int) -> tuple[Path, tuple[str, str] | None]:
        canonical = files_dir / f"{number}.json"
        if canonical.exists():
            return canonical, _file_metadata_revision(metadata, number)
        if legacy_root is not None:
            legacy = legacy_root / "files" / f"{number}.json"
            if legacy.exists():
                return legacy, _file_metadata_revision(legacy_metadata, number)
        return canonical, None

    def _fetch_one(item: dict[str, Any]) -> PullRequest:
        nonlocal done_count, cached_file_count, fetched_file_count, refreshed_file_count
        number = int(item["number"])
        files_cache = files_dir / f"{number}.json"
        cached_files_path, cached_revision = _cached_file(number)
        new_revision = _pull_revision(item)
        known_revision_mismatch = (
            cached_revision is not None
            and new_revision is not None
            and cached_revision != new_revision
        )
        unverified_after_listing = listing_fetched and (
            cached_revision is None or new_revision is None
        )
        revision_changed_or_unknown = known_revision_mismatch or unverified_after_listing
        with cache_lock:
            raw_files = None if revision_changed_or_unknown else _load_json(cached_files_path)
        fetched_files = raw_files is None
        if fetched_files:
            endpoint = f"repos/{owner}/{repo}/pulls/{number}/files?per_page=100"
            print(f"[gh] fetching files for PR #{number} ...", flush=True)
            raw_files = run_gh_api(endpoint, paginate=True, timeout=FILE_TIMEOUT)
            if raw_files is None:
                raw_files = []
            file_fetched_at = _utc_timestamp()
            with cache_lock:
                _save_json(files_cache, raw_files)
                files_metadata[str(number)] = {
                    "fetched_at": file_fetched_at,
                    "updated_at": new_revision[0] if new_revision else None,
                    "head_sha": new_revision[1] if new_revision else None,
                }
        changed = _parse_files(raw_files)
        pr = _parse_pr_item(item, changed, repo_slug)
        with progress_lock:
            done_count += 1
            if fetched_files:
                fetched_file_count += 1
                if revision_changed_or_unknown:
                    refreshed_file_count += 1
            else:
                cached_file_count += 1
            current = done_count
            _emit_progress(
                progress,
                on_progress,
                phase="files",
                done=current,
                total=total,
                message=f"files {current}/{total}",
                cached_files=cached_file_count,
                fetched_files=fetched_file_count,
                refreshed_files=refreshed_file_count,
            )
        return pr

    results: list[PullRequest] = []
    if total:
        with ThreadPoolExecutor(max_workers=MAX_FILE_WORKERS) as pool:
            futures = {pool.submit(_fetch_one, item): item for item in selected}
            # Preserve selection order
            by_number: dict[int, PullRequest] = {}
            for fut in as_completed(futures):
                pr = fut.result()
                by_number[pr.number] = pr
            for item in selected:
                results.append(by_number[int(item["number"])])

    if fetched_file_count:
        _save_json(metadata_path, metadata)

    final_message = f"loaded {total} PRs"
    if cache_status == "cached_stale":
        final_message += " from cached/stale open-pull list"
        if listed_at:
            final_message += f" (as of {listed_at})"
    elif cache_status == "refreshed":
        final_message += " after refreshing open-pull list"
    if partial:
        final_message += f"; partial selection ({total}/{len(raw_pulls)})"
    _emit_progress(
        progress,
        on_progress,
        phase="done",
        done=total,
        total=total,
        message=final_message,
        cached_files=cached_file_count,
        fetched_files=fetched_file_count,
        refreshed_files=refreshed_file_count,
    )
    return results


def gh_available() -> bool:
    """True if gh is on PATH and authenticated."""
    try:
        ensure_gh_auth()
        return True
    except GhError:
        return False


_PathSignature = Optional[tuple[int, int, int, int]]
_pulls_index: dict[
    tuple[str, str, str],
    tuple[
        _PathSignature,
        dict[int, dict[str, Any]],
        dict[int, tuple[str, str] | None],
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
) -> tuple[dict[int, dict[str, Any]], dict[int, tuple[str, str] | None]]:
    pulls_path = root / "pulls.json"
    key = (owner, repo, str(root.resolve()))
    signature = _path_signature(pulls_path)
    indexed = _pulls_index.get(key)
    if indexed is None or indexed[0] != signature:
        raw = _load_json(pulls_path)
        metadata_by_number: dict[int, dict[str, Any]] = {}
        revisions_by_number: dict[int, tuple[str, str] | None] = {}
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
                metadata_by_number[number] = {
                    "number": number,
                    "title": item.get("title") or "",
                    "body": item.get("body") or "",
                    "user": user,
                    "html_url": item.get("html_url") or "",
                }
                revisions_by_number[number] = _pull_revision(item)
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
) -> list[dict[str, str]]:
    """Read revision-consistent cached pull files. Empty list on cache miss."""
    number = int(number)
    canonical_root = _cache_root(owner, repo, cache_dir)
    canonical_pulls_exist = (canonical_root / "pulls.json").exists()
    pulls_root = _cache_read_root(owner, repo, cache_dir)
    pull_metadata, pull_revisions = _cached_pulls_indexes(owner, repo, pulls_root)

    # A canonical open-list cache is authoritative: an omitted PR is closed (or
    # otherwise no longer selected), and an unverifiable patch must not leak
    # through from an older canonical or flattened cache.
    if canonical_pulls_exist and number not in pull_metadata:
        return []
    pull_revision = pull_revisions.get(number)

    roots = [canonical_root]
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
    out: list[dict[str, str]] = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        out.append(
            {
                "path": f.get("filename") or f.get("path") or "",
                "patch": f.get("patch") or "",
            }
        )
    return out


def cached_pr_meta(
    owner: str,
    repo: str,
    number: int,
    cache_dir: Path | None = None,
) -> dict[str, str] | None:
    """Title/body/user from cached pulls.json. No network."""
    root = _cache_read_root(owner, repo, cache_dir)
    metadata_by_number, _ = _cached_pulls_indexes(owner, repo, root)
    return metadata_by_number.get(int(number))
