"""Read-only GitHub ingest via the local `gh` CLI. GET-only; refuses writes."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from triage.models import ChangedFile, PullRequest

DEFAULT_CACHE_DIR = Path(".triage") / "cache"
MAX_FILE_WORKERS = 8
PULLS_TIMEOUT = 300
FILE_TIMEOUT = 60

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


def _cache_root(owner: str, repo: str, base: Path | None = None) -> Path:
    root = base or DEFAULT_CACHE_DIR
    return root / f"{owner}-{repo}"


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


def fetch_pulls_gh(
    owner: str,
    repo: str,
    limit: int = 0,
    cache_dir: Path | None = None,
    progress: dict[str, Any] | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[PullRequest]:
    """
    Fetch open PRs + files via `gh api` (GET only).
    limit=0 means all open PRs (paginate). Default 0 = all.
    Caches under .triage/cache/{owner}-{repo}/.
    Parallelizes per-PR file fetches (max 8 workers). Still GET-only.
    """
    ensure_gh_auth()
    repo_slug = f"{owner}/{repo}"
    root = _cache_root(owner, repo, cache_dir)
    pulls_cache = root / "pulls.json"
    files_dir = root / "files"

    _emit_progress(
        progress,
        on_progress,
        phase="listing",
        done=0,
        total=0,
        message="listing open pulls",
    )

    raw_pulls = _load_json(pulls_cache)
    if raw_pulls is None:
        # Quote query so shell never sees ? — we pass argv list, but still encode
        endpoint = f"repos/{owner}/{repo}/pulls?state=open&per_page=100&sort=created&direction=desc"
        print(f"[gh] fetching pulls for {repo_slug} ...", flush=True)
        raw_pulls = run_gh_api(endpoint, paginate=True, timeout=PULLS_TIMEOUT)
        if not isinstance(raw_pulls, list):
            raise GhError("Unexpected response for pulls list")
        _save_json(pulls_cache, raw_pulls)
        print(f"[gh] cached {len(raw_pulls)} pulls -> {pulls_cache}", flush=True)
    else:
        print(f"[gh] cache hit: {pulls_cache} ({len(raw_pulls)} pulls)", flush=True)

    if not isinstance(raw_pulls, list):
        raise GhError("Cached pulls.json is not a list")

    if limit == 0:
        selected = list(raw_pulls)
    else:
        selected = list(raw_pulls[: max(limit, 0)])

    total = len(selected)
    _emit_progress(
        progress,
        on_progress,
        phase="files",
        done=0,
        total=total,
        message=f"files 0/{total}",
    )

    files_dir.mkdir(parents=True, exist_ok=True)
    cache_lock = threading.Lock()
    progress_lock = threading.Lock()
    done_count = 0

    def _fetch_one(item: dict[str, Any]) -> PullRequest:
        nonlocal done_count
        number = int(item["number"])
        files_cache = files_dir / f"{number}.json"
        with cache_lock:
            raw_files = _load_json(files_cache)
        if raw_files is None:
            endpoint = f"repos/{owner}/{repo}/pulls/{number}/files?per_page=100"
            print(f"[gh] fetching files for PR #{number} ...", flush=True)
            raw_files = run_gh_api(endpoint, paginate=True, timeout=FILE_TIMEOUT)
            if raw_files is None:
                raw_files = []
            with cache_lock:
                _save_json(files_cache, raw_files)
        changed = _parse_files(raw_files)
        pr = _parse_pr_item(item, changed, repo_slug)
        with progress_lock:
            done_count += 1
            current = done_count
            _emit_progress(
                progress,
                on_progress,
                phase="files",
                done=current,
                total=total,
                message=f"files {current}/{total}",
            )
        return pr

    results: list[PullRequest] = []
    if total == 0:
        return results

    with ThreadPoolExecutor(max_workers=MAX_FILE_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, item): item for item in selected}
        # Preserve selection order
        by_number: dict[int, PullRequest] = {}
        for fut in as_completed(futures):
            pr = fut.result()
            by_number[pr.number] = pr
        for item in selected:
            results.append(by_number[int(item["number"])])

    _emit_progress(
        progress,
        on_progress,
        phase="done",
        done=total,
        total=total,
        message=f"fetched {total} PRs",
    )
    return results


def gh_available() -> bool:
    """True if gh is on PATH and authenticated."""
    try:
        ensure_gh_auth()
        return True
    except GhError:
        return False


def cached_pr_files(
    owner: str,
    repo: str,
    number: int,
    cache_dir: Path | None = None,
) -> list[dict[str, str]]:
    """Read cached pull files (path + patch). Empty list on cache miss. No network."""
    raw = _load_json(_cache_root(owner, repo, cache_dir) / "files" / f"{number}.json")
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
