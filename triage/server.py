"""Secure loopback-only stdlib server for the local triage dashboard."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import mimetypes
import re
import secrets
import socket
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote_to_bytes, urlsplit

from triage import gh as gh_module
from triage import rank as rank_module
from triage import service as service_module
from triage import store as store_module
from triage.github import parse_repo
from triage.models import ChangedFile, PullRequest
from triage.pipeline import ingest, run_pipeline
from triage.store import (
    DEFAULT_STORE_PATH,
    load_store,
    overlap_for_group,
    prs_for_path,
    ui_state,
)
from triage.workspaces import (
    InvalidRepoError,
    WorkspaceBinding,
    WorkspaceError,
    WorkspaceRouter,
    canonical_repo,
)

WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_WORKSPACE_ROOT = Path(".triage/workspaces")
MAX_BODY_BYTES = 64 * 1024
MAX_URL_BYTES = 8 * 1024
MAX_FETCH_LIMIT = 5_000
MAX_ENRICH_CANDIDATES = 24
MAX_PATCH_PRS = 200
MAX_PATCH_PAGE_SIZE = 8
MAX_PATCH_CHUNK = 16_384
BODY_TIMEOUT_SECONDS = 2.0
RESPONSE_WRITE_TIMEOUT_SECONDS = 5.0
MAX_REQUEST_THREADS = 16
_GROUP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z")


class RequestProblem(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class RequestDeadlineExceeded(Exception):
    """The absolute request-ingress deadline expired."""


class _DeadlineReader:
    """Small socket reader whose deadline cannot be extended by byte drips."""

    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.buffer = bytearray()
        self.deadline: float | None = None
        self.closed = False

    def set_deadline(self, deadline: float | None) -> None:
        self.deadline = deadline

    def _recv(self) -> bytes:
        if self.deadline is None:
            raise RuntimeError("request deadline is not active")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RequestDeadlineExceeded
        self.connection.settimeout(remaining)
        try:
            return self.connection.recv(8192)
        except TimeoutError as exc:
            raise RequestDeadlineExceeded from exc

    def readline(self, limit: int = -1) -> bytes:
        while True:
            bounded = len(self.buffer) if limit < 0 else min(len(self.buffer), limit)
            newline = self.buffer.find(b"\n", 0, bounded)
            if newline >= 0:
                end = newline + 1
                result = bytes(self.buffer[:end])
                del self.buffer[:end]
                return result
            if limit >= 0 and len(self.buffer) >= limit:
                result = bytes(self.buffer[:limit])
                del self.buffer[:limit]
                return result
            chunk = self._recv()
            if not chunk:
                result = bytes(self.buffer)
                self.buffer.clear()
                return result
            self.buffer.extend(chunk)

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunks = [bytes(self.buffer)]
            self.buffer.clear()
            while True:
                chunk = self._recv()
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        while len(self.buffer) < size:
            chunk = self._recv()
            if not chunk:
                break
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def close(self) -> None:
        self.closed = True
        self.buffer.clear()


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Thread-per-connection server with a hard active-handler ceiling."""

    def __init__(self, *args: Any, max_request_threads: int = MAX_REQUEST_THREADS,
                 **kwargs: Any) -> None:
        if max_request_threads < 1:
            raise ValueError("max_request_threads must be positive")
        self.max_request_threads = max_request_threads
        self._request_slots = threading.BoundedSemaphore(max_request_threads)
        super().__init__(*args, **kwargs)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


_fetch_lock = threading.Lock()
_fetch_progress: dict[str, Any] = {
    "running": False, "phase": "", "done": 0, "total": 0, "error": None,
    "ready": False, "message": "", "repo": "", "refresh": False,
    "snapshot_available": False,
    # Internal only.  It is used to prevent a progress response for one
    # workspace from being presented as another workspace's job.
    "store_path": "",
}
_fetch_thread: threading.Thread | None = None
_fetch_terminal: list[dict[str, Any]] = []


def _progress_unlocked(*, include_internal: bool = False) -> dict[str, Any]:
    result = {
        "running": bool(_fetch_progress["running"]),
        "phase": _fetch_progress.get("phase") or "",
        "done": int(_fetch_progress.get("done") or 0),
        "total": int(_fetch_progress.get("total") or 0),
        "error": _fetch_progress.get("error"),
        "ready": bool(_fetch_progress.get("ready")),
        "message": _fetch_progress.get("message") or "",
        "repo": _fetch_progress.get("repo") or "",
        "refresh": bool(_fetch_progress.get("refresh")),
        "snapshot_available": bool(_fetch_progress.get("snapshot_available")),
    }
    if include_internal:
        result["store_path"] = _fetch_progress.get("store_path") or ""
    return result


def get_fetch_progress(*, repo: str | None = None, store_path: Path | None = None) -> dict[str, Any]:
    """Return progress scoped to a requested workspace when provided.

    The old no-argument helper remains intentionally global for embedding and
    tests.  HTTP callers always provide the resolved repository/path in
    multi-workspace mode, so a job for another workspace appears idle rather
    than leaking its phase or completion to the caller.
    """
    with _fetch_lock:
        progress = _progress_unlocked(include_internal=True)
        if repo is None and store_path is None:
            return {key: value for key, value in progress.items() if key != "store_path"}
        wanted_repo = str(repo or "").strip().strip("/").lower()
        wanted_path = str(Path(store_path)) if store_path is not None else ""
        if (wanted_repo and progress.get("repo", "").strip().strip("/").lower() != wanted_repo
                or wanted_path and progress.get("store_path") != wanted_path):
            # Keep a small terminal cache so a completed workspace remains
            # observable if another workspace starts before its UI poll.
            for terminal in reversed(_fetch_terminal):
                if (wanted_repo and terminal.get("repo", "").strip().strip("/").lower() != wanted_repo
                        or wanted_path and terminal.get("store_path") != wanted_path):
                    continue
                return {key: value for key, value in terminal.items() if key != "store_path"}
            return {
                "running": False, "phase": "", "done": 0, "total": 0,
                "error": None, "ready": False, "message": "",
                "repo": wanted_repo, "refresh": False, "snapshot_available": False,
            }
        progress.pop("store_path", None)
        return progress


def _finish_fetch(**fields: Any) -> None:
    """Publish and retain a terminal snapshot atomically with the job state."""
    with _fetch_lock:
        _fetch_progress.update(fields)
        _fetch_terminal.append(_progress_unlocked(include_internal=True))
        del _fetch_terminal[:-8]


def _set_progress(**fields: Any) -> None:
    with _fetch_lock:
        _fetch_progress.update(fields)


def _has_usable_snapshot(store_path: Path) -> bool:
    """Check for a persisted snapshot without treating an empty store as one."""
    try:
        if not store_path.is_file():
            return False
        data = load_store(store_path)
    except Exception:  # noqa: BLE001 - a malformed store is not a usable snapshot
        return False
    repo = str(data.get("repo") or "").strip()
    source = str(data.get("source") or "").strip()
    version = data.get("snapshot_version")
    return bool(repo and source and (
        (isinstance(version, int) and version > 0)
        or data.get("last_prs")
        or data.get("last_groups")
    ))


def _sync_failure_message(snapshot_available: bool) -> str:
    """Return an actionable error without provider, secret, or path details."""
    if snapshot_available:
        return "sync failed; previous snapshot preserved. Retry Sync."
    return "sync failed; no snapshot available. Check connectivity and retry Sync."


def _safe_progress_phase(raw: Any) -> str:
    phase = str(raw or "").strip().lower()
    if phase in {"reconcile", "reconciling"}:
        return "reconciliation"
    if phase in {"build_queue", "queue"}:
        return "building_queue"
    if phase in {"listing", "files", "reconciliation", "grouping",
                 "building_queue", "saving", "loading", "rendering"}:
        return phase
    if phase in {"failed", "error"}:
        return "error"
    # The adapter may add a more detailed phase in a later revision. Keep the
    # public progress contract bounded rather than echoing arbitrary strings.
    return "syncing"


def run_fetch(source: str, repo: str, limit: int,
              store_path: Path = DEFAULT_STORE_PATH,
              progress: dict[str, Any] | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """Synchronously ingest and recompute; used by the worker and unit tests."""
    print(f"[serve] fetch source={source} repo={repo} limit={limit} refresh={refresh}", flush=True)
    if progress is not None:
        progress.update(phase="listing", done=0, total=0, message=f"loading {source} pull list")
        _set_progress(phase="listing", done=0, total=0,
                      message=f"loading {source} pull list")

    def on_progress(snap: dict[str, Any]) -> None:
        if progress is None:
            return
        phase = _safe_progress_phase(snap.get("phase", progress.get("phase", "")))
        if phase == "syncing" and str(snap.get("phase") or "").lower() == "done":
            phase = "reconciliation"
        if phase == "error":
            message = _sync_failure_message(bool(progress.get("_snapshot_available")))
        else:
            message = str(snap.get("message") or progress.get("message") or "syncing")
        progress.update(
            phase=phase,
            done=snap.get("done", progress.get("done", 0)),
            total=snap.get("total", progress.get("total", 0)),
            message=message,
        )
        # The GitHub adapter may attach a provider exception to its private
        # progress mapping. Scrub it before the worker or an embedding caller
        # can observe the mapping.
        progress["error"] = (
            _sync_failure_message(bool(progress.get("_snapshot_available")))
            if phase == "error" else None
        )
        _set_progress(**{k: progress.get(k) for k in ("phase", "done", "total", "message", "error")})

    prs = ingest(source=source, repo=repo, limit=limit, refresh=refresh,
                 progress=progress,
                 on_progress=on_progress if progress is not None else None)
    if progress is not None:
        message = f"reconciled {len(prs)} pull revisions"
        progress.update(phase="reconciliation", done=len(prs), total=len(prs), message=message)
        _set_progress(phase="reconciliation", done=len(prs), total=len(prs), message=message)
    run_pipeline(prs, persist=True, store_path=store_path, apply_rules=True,
                 source=source, repo=repo, progress=progress,
                 on_progress=on_progress if progress is not None else None)
    if progress is not None:
        progress.update(phase="loading", done=0, total=0,
                        message="loading saved triage state")
        _set_progress(phase="loading", done=0, total=0,
                      message="loading saved triage state")
        if progress.get("_skip_state_payload"):
            return {}
    return _state_payload(store_path)


def _fetch_worker(source: str, repo: str, limit: int, refresh: bool, store_path: Path) -> None:
    with _fetch_lock:
        snapshot_available = bool(_fetch_progress.get("snapshot_available"))
    local = {
        "phase": "starting", "done": 0, "total": 0, "message": "starting",
        "_snapshot_available": snapshot_available,
        # The browser performs the authoritative state GET after the worker
        # publishes ready. Preserve run_fetch's historical return for direct
        # callers while avoiding an unneeded large state projection here.
        "_skip_state_payload": True,
    }
    try:
        run_fetch(source, repo, limit, store_path=store_path, progress=local, refresh=refresh)
        _finish_fetch(running=False, phase="loading", error=None, ready=True,
                      snapshot_available=True,
                      message="snapshot saved; loading triage state",
                      done=local.get("done", 0), total=local.get("total", 0))
    except Exception:  # noqa: BLE001
        message = _sync_failure_message(snapshot_available)
        _finish_fetch(running=False, phase="error", error=message, ready=False,
                      snapshot_available=snapshot_available, message=message)


class FetchBusyError(RuntimeError):
    def __init__(self, progress: dict[str, Any]) -> None:
        super().__init__("fetch already running")
        self.progress = progress


def start_fetch_async(source: str, repo: str, limit: int,
                      store_path: Path = DEFAULT_STORE_PATH, *, refresh: bool = False) -> dict[str, Any]:
    global _fetch_thread
    with _fetch_lock:
        if _fetch_progress.get("running"):
            raise FetchBusyError(_progress_unlocked())
        _fetch_progress.update(running=True, phase="starting", done=0, total=0,
                               error=None, ready=False, message="starting", repo=repo,
                               refresh=refresh, store_path=str(Path(store_path)),
                               snapshot_available=_has_usable_snapshot(Path(store_path)))
        _fetch_thread = threading.Thread(
            target=_fetch_worker, args=(source, repo, limit, refresh, store_path),
            daemon=True, name="triage-fetch",
        )
        _fetch_thread.start()
    return {"started": True, "repo": repo, "refresh": refresh, "limit": limit}


def _repo_key(value: Any) -> str:
    if not isinstance(value, str):
        raise RequestProblem(400, "invalid_repo", "repo must be owner/name")
    try:
        return canonical_repo(value)
    except InvalidRepoError as exc:
        raise RequestProblem(400, "invalid_repo", "repo must be owner/name") from exc


def _safe_file_path(value: Any) -> str:
    if not isinstance(value, str):
        raise RequestProblem(400, "invalid_path", "path is required and must be at most 1024 bytes")
    try:
        path = value.encode("utf-8", "strict").decode("utf-8")
    except UnicodeError as exc:
        raise RequestProblem(400, "invalid_path", "path is invalid") from exc
    if not path or len(path.encode()) > 1024 or "\x00" in path:
        raise RequestProblem(400, "invalid_path", "path is required and must be at most 1024 bytes")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RequestProblem(400, "invalid_path", "path must be repository-relative")
    return path


def _empty_state(repo: str = "", *, mode: str = "multi", initialized: bool = False,
                 legacy: bool = False) -> dict[str, Any]:
    """Build an in-memory projection for an uninitialized workspace.

    Calling ``store.load_store`` for an unknown workspace is deliberately
    avoided: the store layer takes a lock and may create its parent directory.
    Selection and discovery must therefore remain side-effect free.
    """
    data = store_module._empty_store()  # type: ignore[attr-defined]
    data["repo"] = repo
    state = store_module.ui_state_from_data(data)
    state.setdefault("sync", None)
    state["workspace"] = {
        "mode": mode, "repo": repo, "initialized": bool(initialized),
        "legacy": bool(legacy),
    }
    return state


def _workspace_descriptor(mode: str, repo: str, binding: WorkspaceBinding | None = None,
                          *, initialized: bool | None = None,
                          legacy: bool | None = None) -> dict[str, Any]:
    if initialized is None:
        initialized = bool(binding.initialized) if binding is not None else False
    if legacy is None:
        legacy = bool(binding.legacy) if binding is not None else False
    return {"mode": mode, "repo": repo, "initialized": bool(initialized),
            "legacy": bool(legacy)}


def _state_payload(store_path: Path, binding: WorkspaceBinding | None = None, *, mode: str = "single",
                   repo: str | None = None) -> dict[str, Any]:
    requested_repo = str(repo or (binding.repo if binding is not None else "") or "")
    initialized = bool(binding.initialized) if binding is not None else True
    legacy = bool(binding.legacy) if binding is not None else False
    if binding is not None and not initialized:
        return _empty_state(requested_repo, mode=mode, initialized=False, legacy=legacy)
    state = ui_state(store_path)
    repo, source = str(state.get("repo") or ""), str(state.get("source") or "")
    if repo and source in {"gh", "github"}:
        try:
            owner, name = parse_repo(repo)
            stored = load_store(store_path)
            snapshots = [
                str(pr.get("cache_snapshot_id") or "")
                for pr in stored.get("last_prs") or []
            ]
            unique_snapshots = set(snapshots)
            if not snapshots or "" in unique_snapshots or len(unique_snapshots) != 1:
                state["sync"] = {
                    "repository": repo,
                    "cache_status": "unverified-store-snapshot",
                    "evidence_complete": False,
                }
            else:
                snapshot_id = snapshots[0]
                status = gh_module.cached_sync_status(
                    owner, name, snapshot_id=snapshot_id
                )
                state["sync"] = status or {
                    "repository": repo,
                    "snapshot_id": snapshot_id,
                    "cache_status": "stored-snapshot-unavailable",
                    "evidence_complete": False,
                }
        except (OSError, ValueError, json.JSONDecodeError):
            state["sync"] = {"repository": repo, "cache_status": "unavailable",
                             "evidence_complete": False}
    else:
        state.setdefault("sync", None)
    state["workspace"] = _workspace_descriptor(
        mode, requested_repo or repo, binding,
        initialized=initialized, legacy=legacy,
    )
    return state


def _cached_related(pr_number: int, *, file_path: str | None,
                    store_path: Path, k: int) -> dict[str, Any]:
    return rank_module.related_cached(
        pr_number, file_path=file_path, store_path=store_path, k=k
    )


def _run_enrichment(pr_number: int, *, repo: str, file_path: str | None,
                    store_path: Path, limit: int, expected_version: int) -> dict[str, Any]:
    return rank_module.enrich_related(
        pr_number, repo=repo, file_path=file_path, store_path=store_path, limit=limit,
        expected_version=expected_version,
    )


class TriageHandler(BaseHTTPRequestHandler):
    # ``store_path`` is immutable server configuration in fixed mode.  In
    # multi mode it is None and every request binds a local path through the
    # router; no request ever changes this class attribute.
    store_path: Path | None = DEFAULT_STORE_PATH
    workspace_root: Path | None = None
    workspace_router: WorkspaceRouter | None = None
    workspace_mode: str = "single"
    csrf_token = ""
    allowed_hostnames: tuple[str, ...] = ()
    body_timeout_seconds = BODY_TIMEOUT_SECONDS
    response_write_timeout_seconds = RESPONSE_WRITE_TIMEOUT_SECONDS
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self._deadline_reader = _DeadlineReader(self.connection)
        self.rfile = self._deadline_reader
        self._request_store_path: Path | None = None
        self._request_repo = ""
        self._request_binding: WorkspaceBinding | None = None
        self._request_initialized = True
        self._request_legacy = False

    def handle_one_request(self) -> None:
        self._deadline_reader.set_deadline(time.monotonic() + self.body_timeout_seconds)
        try:
            super().handle_one_request()
        except RequestDeadlineExceeded:
            self.close_connection = True
            self._finish_ingress()
            if (getattr(self, "command", None)
                    and str(getattr(self, "request_version", "")).startswith("HTTP/")):
                try:
                    self._send_json(408, {
                        "error": "request headers or body timed out",
                        "code": "request_timeout",
                    })
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
        finally:
            # Keep-alive requests must not inherit the previous request's
            # repository binding, even when a handler method returns early.
            self._request_store_path = None
            self._request_repo = ""
            self._request_binding = None
            self._request_initialized = True
            self._request_legacy = False

    def _store(self) -> Path:
        path = self._request_store_path
        if path is not None:
            return path
        if self.store_path is None:
            raise RequestProblem(503, "workspace_unavailable", "workspace routing is unavailable")
        return Path(self.store_path)

    def _new_router(self) -> WorkspaceRouter:
        router = self.workspace_router
        if router is None:
            raise RequestProblem(503, "workspace_unavailable", "workspace routing is unavailable")
        return router

    @staticmethod
    def _resolver_problem(exc: WorkspaceError) -> RequestProblem:
        code = str(exc.code or "workspace_unavailable")
        if code == "unavailable":
            code = "workspace_unavailable"
        if code not in {"invalid_repo", "workspace_conflict", "workspace_unavailable"}:
            code = "workspace_unavailable"
        status = 400 if code == "invalid_repo" else 409 if code == "workspace_conflict" else 503
        message = {
            "invalid_repo": "repo must be owner/name",
            "workspace_conflict": "workspace selection is ambiguous",
            "workspace_unavailable": "workspace is unavailable",
        }[code]
        return RequestProblem(status, code, message)

    def _route_request(self, requested: str | None, *, required: bool = False,
                       allow_default: bool = False) -> tuple[Path, str]:
        """Bind this request to one immutable workspace path and repository."""
        if self.workspace_mode != "multi":
            path = self._store()
            # Fixed-store calls retain their existing repository guard.  A
            # fetch may bind an empty store to its first explicitly requested
            # repository, while detail reads still fail repository_unset.
            requested_repo = _repo_key(requested) if requested is not None else ""
            data = load_store(path)
            active = str(data.get("repo") or "").strip().strip("/").lower()
            if requested_repo and active and requested_repo != active:
                raise RequestProblem(409, "repository_conflict",
                                     "repository does not match this store; use a separate workspace")
            if required and not (requested_repo or active):
                raise RequestProblem(409, "repository_unset", "this store has no active repository")
            effective = requested_repo or active
            self._request_store_path = path
            self._request_repo = effective
            self._request_binding = None
            self._request_initialized = path.exists()
            self._request_legacy = False
            return path, effective

        if requested is None:
            if not allow_default:
                if required:
                    raise RequestProblem(400, "missing_fields", "missing fields: repo")
                requested = None
            else:
                try:
                    requested = str(self._new_router().default_repo or "")
                except WorkspaceError as exc:
                    raise self._resolver_problem(exc) from exc
        elif requested == "":
            raise RequestProblem(400, "invalid_repo", "repo must be owner/name")
        if requested is None or requested == "":
            requested = "omacom/omarchy"
        canonical = _repo_key(requested)
        try:
            binding = self._new_router().resolve(canonical)
        except WorkspaceError as exc:
            raise self._resolver_problem(exc) from exc
        path = binding.path
        effective = binding.repo
        self._request_store_path = path
        self._request_repo = effective
        self._request_binding = binding
        self._request_initialized = binding.initialized
        self._request_legacy = binding.legacy
        return path, effective

    def _bound_store(self) -> dict[str, Any]:
        if self.workspace_mode == "multi" and not self._request_initialized:
            return store_module._empty_store()  # type: ignore[attr-defined]
        return load_store(self._store())

    def _require_initialized(self) -> None:
        """Reject operations that would read/write an unknown store path."""
        if self.workspace_mode == "multi" and not self._request_initialized:
            raise RequestProblem(404, "workspace_uninitialized", "workspace is not initialized")

    @staticmethod
    def _empty_file_result(file_path: str, page: int, page_size: int) -> dict[str, Any]:
        return {"path": file_path, "pr_count": 0, "prs": [], "page": page,
                "page_size": page_size, "pages": 0, "next_page": None, "truncated": 0}

    def _workspaces_payload(self) -> dict[str, Any]:
        """Return bounded workspace metadata without exposing filesystem paths."""
        if self.workspace_mode != "multi":
            path = self._store()
            if path.exists():
                try:
                    data = load_store(path)
                except Exception as exc:
                    raise RequestProblem(503, "workspace_unavailable", "workspace is unavailable") from exc
            else:
                data = store_module._empty_store()  # type: ignore[attr-defined]
            active = str(data.get("repo") or "").strip().strip("/").lower()
            return {
                "mode": "single", "default_repo": active,
                "workspaces": ([{"repo": active, "initialized": True, "legacy": False}]
                                if active else []),
                "truncated": False,
            }
        try:
            router = self._new_router()
            default_repo = str(router.default_repo or "omacom/omarchy").strip().strip("/").lower()
            raw_rows = router.list_workspaces(limit=200)
        except WorkspaceError as exc:
            raise self._resolver_problem(exc) from exc
        rows: list[dict[str, Any]] = []
        for item in raw_rows or []:
            repo = str(item.get("repo") or "").strip().strip("/").lower()
            if not repo:
                continue
            rows.append({
                "repo": repo,
                "initialized": bool(item.get("initialized", False)),
                "legacy": bool(item.get("legacy", False)),
            })
        truncated = len(rows) >= 200
        return {"mode": "multi", "default_repo": default_repo,
                "workspaces": rows[:200], "truncated": truncated}

    def _finish_ingress(self) -> None:
        self._deadline_reader.set_deadline(None)
        self.connection.settimeout(None)

    def log_message(self, fmt: str, *args: Any) -> None:
        client = self.client_address[0] if self.client_address else "local"
        raw = fmt % args
        safe = "".join(char if 32 <= ord(char) < 127 else "?" for char in raw)[:500]
        print(f"[http] {client} {safe}", flush=True)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        # Provider work may legitimately take longer than the request-ingress
        # deadline. It must not inherit the final short receive timeout.
        self._finish_ingress()
        self.connection.settimeout(self.response_write_timeout_seconds)
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def send_error(self, code: int, message: str | None = None,
                   explain: str | None = None) -> None:
        del explain
        safe = message if code < 500 and message else "request failed"
        self._send_json(code, {"error": safe})

    def _problem(self, problem: RequestProblem) -> None:
        self._send_json(problem.status, {"error": problem.message, "code": problem.code})

    def _authorities(self) -> tuple[str, ...]:
        port = int(self.server.server_address[1])
        hosts = self.allowed_hostnames or (str(self.server.server_address[0]).lower(),)
        return tuple(f"[{h}]:{port}" if ":" in h and not h.startswith("[") else f"{h}:{port}"
                     for h in hosts)

    def _guard(self, *, mutation: bool) -> str:
        if len(self.path.encode(errors="replace")) > MAX_URL_BYTES:
            raise RequestProblem(414, "url_too_long", "request target is too long")
        host = self._header("Host").strip().lower()
        if not host or host not in self._authorities():
            raise RequestProblem(421, "invalid_host", "Host is not allowed")
        fetch_site = self._header("Sec-Fetch-Site").strip().lower()
        if fetch_site in {"cross-site", "same-site"}:
            raise RequestProblem(403, "cross_origin", "cross-origin requests are forbidden")
        expected_origin = f"http://{host}"
        origin = self._header("Origin").strip()
        if origin and origin != expected_origin:
            raise RequestProblem(403, "invalid_origin", "Origin is not allowed")
        if mutation:
            if origin != expected_origin:
                raise RequestProblem(403, "origin_required", "a same-origin Origin header is required")
            token = self._header("X-CSRF-Token")
            if not token or not hmac.compare_digest(token, self.csrf_token):
                raise RequestProblem(403, "csrf_required", "a valid CSRF token is required")
            media_type = self._header("Content-Type").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                raise RequestProblem(415, "json_required", "Content-Type must be application/json")
        return expected_origin

    def _read_json(self) -> dict[str, Any]:
        if self._header("Transfer-Encoding"):
            raise RequestProblem(400, "transfer_encoding", "Transfer-Encoding is not supported")
        raw_length = self._header("Content-Length")
        if not raw_length:
            raise RequestProblem(411, "length_required", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise RequestProblem(400, "invalid_length", "Content-Length is invalid") from exc
        if length < 0:
            raise RequestProblem(400, "invalid_length", "Content-Length is invalid")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise RequestProblem(413, "body_too_large", "JSON body exceeds 65536 bytes")
        try:
            raw = self.rfile.read(length)
        except (TimeoutError, RequestDeadlineExceeded) as exc:
            self.close_connection = True
            raise RequestProblem(408, "body_timeout", "request body timed out") from exc
        finally:
            self._finish_ingress()
        if len(raw) != length:
            self.close_connection = True
            raise RequestProblem(400, "incomplete_body", "request body is incomplete")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestProblem(400, "invalid_json", "body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise RequestProblem(400, "invalid_json", "JSON body must be an object")
        return value

    def _header(self, name: str) -> str:
        values = self.headers.get_all(name) or []
        if len(values) > 1:
            self.close_connection = True
            raise RequestProblem(400, "duplicate_header", f"{name} must occur once")
        return values[0] if values else ""

    @staticmethod
    def _fields(body: dict[str, Any], *, required: set[str],
                optional: set[str] = frozenset()) -> None:
        missing, unknown = required - body.keys(), body.keys() - required - optional
        if missing:
            raise RequestProblem(400, "missing_fields", f"missing fields: {', '.join(sorted(missing))}")
        if unknown:
            raise RequestProblem(400, "unknown_fields", f"unknown fields: {', '.join(sorted(unknown))}")

    def _query(self, parsed: Any, allowed: set[str]) -> dict[str, list[str]]:
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True,
                             max_num_fields=16)
        except ValueError as exc:
            raise RequestProblem(400, "invalid_query", "query string is invalid") from exc
        unknown = query.keys() - allowed
        if unknown:
            raise RequestProblem(400, "unknown_parameter",
                                 f"unknown parameters: {', '.join(sorted(unknown))}")
        return query

    @staticmethod
    def _one(query: dict[str, list[str]], name: str, default: str = "") -> str:
        values = query.get(name)
        if not values:
            return default
        if len(values) != 1:
            raise RequestProblem(400, "duplicate_parameter", f"{name} must occur once")
        return values[0]

    @staticmethod
    def _int(raw: str, name: str, low: int, high: int) -> int:
        if not raw.isdigit() or not low <= int(raw) <= high:
            raise RequestProblem(400, f"invalid_{name}", f"{name} is invalid")
        return int(raw)

    @staticmethod
    def _json_int(raw: Any, name: str, low: int, high: int) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
            raise RequestProblem(400, f"invalid_{name}", f"{name} is invalid")
        return raw

    def _active_repo(self, requested: str | None = None) -> tuple[dict[str, Any], str]:
        if self._request_store_path is None:
            # Compatibility for small direct handler helpers: route the
            # request before applying the old fixed-store repository guard.
            self._route_request(requested, required=requested is not None)
        store = self._bound_store()
        active = str(store.get("repo") or "").strip().strip("/").lower()
        if requested is not None:
            repo = _repo_key(requested)
            if active and repo != active:
                raise RequestProblem(409, "repository_conflict",
                                     "repository does not match this store; use a separate workspace")
        elif not active:
            raise RequestProblem(409, "repository_unset", "this store has no active repository")
        return store, (self._request_repo or active)

    @staticmethod
    def _decoded_path(raw: str) -> str:
        try:
            path = unquote_to_bytes(raw).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RequestProblem(400, "invalid_path", "request path must be UTF-8") from exc
        if "\x00" in path or "\\" in path:
            raise RequestProblem(400, "invalid_path", "request path is invalid")
        return path

    def do_GET(self) -> None:
        try:
            origin = self._guard(mutation=False)
            parsed = urlsplit(self.path)
            path = self._decoded_path(parsed.path)
            if path == "/api/tools":
                self._send_json(200, {"tools": list(service_module.TOOL_DEFINITIONS)})
            elif path == "/api/tools/definitions":
                self._send_json(200, {
                    "tools": list(service_module.TOOL_DEFINITIONS),
                    "draft_tool": service_module.DRAFT_PROPOSAL_TOOL_DEFINITION,
                    "draft_tools": list(service_module.DRAFT_TOOL_DEFINITIONS),
                })
            elif path == "/api/session":
                self._send_json(200, {"csrf_token": self.csrf_token, "origin": origin,
                    "limits": {"body_bytes": MAX_BODY_BYTES, "fetch_prs": MAX_FETCH_LIMIT,
                               "enrichment_candidates": MAX_ENRICH_CANDIDATES}})
            elif path == "/api/workspaces":
                self._send_json(200, self._workspaces_payload())
            elif path == "/api/state":
                query = self._query(parsed, {"repo"})
                requested = self._one(query, "repo") if "repo" in query else None
                store_path, repo = self._route_request(
                    requested, required=False, allow_default=True,
                )
                self._send_json(200, _state_payload(
                    store_path, self._request_binding, mode=self.workspace_mode,
                    repo=repo,
                ))
            elif path == "/api/progress":
                query = self._query(parsed, {"repo"})
                requested = self._one(query, "repo") if "repo" in query else None
                store_path, repo = self._route_request(
                    requested, required=False, allow_default=True,
                )
                self._send_json(200, get_fetch_progress(repo=repo, store_path=store_path))
            elif path.startswith("/api/proposals/"):
                proposal_id = path[len("/api/proposals/"):]
                if not proposal_id or "/" in proposal_id:
                    raise RequestProblem(400, "invalid_proposal", "proposal_id is invalid")
                query = self._query(parsed, {"repo"})
                repo = self._one(query, "repo") if "repo" in query else None
                store_path, active = self._route_request(
                    repo, required=self.workspace_mode == "multi",
                )
                self._require_initialized()
                proposal = store_module.inspect_proposal(
                    proposal_id, path=store_path, repo=repo or active or None
                )
                self._send_json(200, {"proposal": proposal.to_dict()})
            elif path == "/api/overlap":
                query = self._query(parsed, {"repo", "group_id", "member_page", "member_page_size",
                                             "row_page", "row_page_size"})
                requested = self._one(query, "repo") if "repo" in query else None
                store_path, _ = self._route_request(
                    requested, required=self.workspace_mode == "multi",
                )
                group_id = self._one(query, "group_id")
                if not _GROUP_ID.fullmatch(group_id):
                    raise RequestProblem(400, "invalid_group", "group_id is invalid")
                self._require_initialized()
                try:
                    self._send_json(200, overlap_for_group(
                        group_id, path=store_path,
                        member_page=self._int(self._one(query, "member_page", "1"),
                                              "member_page", 1, 1_000_000),
                        member_page_size=self._int(self._one(query, "member_page_size", "24"),
                                                   "member_page_size", 1, 24),
                        row_page=self._int(self._one(query, "row_page", "1"),
                                           "row_page", 1, 1_000_000),
                        row_page_size=self._int(self._one(query, "row_page_size", "80"),
                                                "row_page_size", 1, 80),
                    ))
                except KeyError as exc:
                    raise RequestProblem(404, "group_not_found", "group not found") from exc
            elif path == "/api/pr":
                query = self._query(parsed, {"number", "repo"})
                number = self._int(self._one(query, "number"), "number", 1, 2_147_483_647)
                requested = self._one(query, "repo") if "repo" in query else None
                self._route_request(requested, required=self.workspace_mode == "multi")
                store, repo = self._active_repo(requested)
                if str(store.get("source") or "") == "fixtures":
                    meta = next((p for p in store.get("last_prs") or []
                                 if int(p.get("number") or 0) == number), None)
                    evidence = {
                        "meta": meta,
                        "files": (meta or {}).get("files") or [],
                        "snapshot_id": "",
                    }
                    stored = meta
                    fixture_source = True
                else:
                    fixture_source = False
                    owner, name = parse_repo(repo)
                    stored = next((p for p in store.get("last_prs") or []
                                   if int(p.get("number") or 0) == number), None)
                    if stored is None:
                        raise RequestProblem(404, "pr_not_found", "PR is not in the active store")
                    evidence = gh_module.cached_pr_evidence(
                        owner, name, number, snapshot_id=stored.get("cache_snapshot_id", "")
                    ) or {}
                    if not isinstance(evidence, dict):
                        raise RequestProblem(
                            409, "revision_conflict", "cached evidence is invalid"
                        )
                    meta = evidence.get("meta")
                    legacy_unverified = self._legacy_unverified(stored, evidence)
                    unbound_preview = (
                        legacy_unverified and self._legacy_revision_unbound(stored)
                    )
                    if meta and not unbound_preview and not self._revision_matches(
                            stored, meta, evidence.get("files") or []):
                        raise RequestProblem(409, "revision_conflict",
                                             "cached description does not match the triage revision")
                if not meta:
                    raise RequestProblem(404, "cache_miss", "PR is not in the active local cache")
                self._guard_evidence_identity(
                    stored,
                    evidence,
                    active_repo=repo,
                    fixture_source=fixture_source,
                )
                payload = dict(meta)
                if str(store.get("source") or "") != "fixtures" and legacy_unverified:
                    payload["legacy_unverified"] = True
                    payload["evidence_complete"] = False
                self._send_json(200, payload)
            elif path == "/api/file":
                query = self._query(parsed, {"repo", "path", "page", "page_size"})
                requested = self._one(query, "repo") if "repo" in query else None
                store_path, _ = self._route_request(
                    requested, required=self.workspace_mode == "multi",
                )
                file_path = _safe_file_path(self._one(query, "path"))
                page = self._int(self._one(query, "page", "1"), "page", 1, 1_000_000)
                page_size = self._int(self._one(query, "page_size", "40"), "page_size", 1, 40)
                if self.workspace_mode == "multi" and not self._request_initialized:
                    self._send_json(200, self._empty_file_result(file_path, page, page_size))
                    return
                self._send_json(200, prs_for_path(file_path, path=store_path,
                    page=page, page_size=page_size))
            elif path == "/api/related":
                query = self._query(parsed, {"repo", "pr", "path", "limit"})
                requested = self._one(query, "repo") if "repo" in query else None
                store_path, _ = self._route_request(
                    requested, required=self.workspace_mode == "multi",
                )
                self._require_initialized()
                number = self._int(self._one(query, "pr"), "pr", 1, 2_147_483_647)
                raw_path = self._one(query, "path")
                limit = self._int(self._one(query, "limit", "5"), "limit", 1, 20)
                self._send_json(200, _cached_related(number,
                    file_path=_safe_file_path(raw_path) if raw_path else None,
                    store_path=store_path, k=limit))
            elif path == "/api/patches":
                self._send_json(200, self._patches(parsed))
            elif path in {"/", "/index.html"}:
                self._serve_file(WEB_DIR / "index.html")
            else:
                self._serve_static(path)
        except RequestProblem as problem:
            self.close_connection = True
            self._problem(problem)
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._send_json(500, {"error": "internal server error", "code": "internal_error"})

    def do_POST(self) -> None:
        try:
            self._guard(mutation=True)
            parsed = urlsplit(self.path)
            path = self._decoded_path(parsed.path)
            if parsed.query:
                raise RequestProblem(400, "query_forbidden", "POST endpoints do not accept query")
            body = self._read_json()
            if path == "/api/tools/read":
                self._post_tool_read(body)
            elif path == "/api/proposals/draft":
                self._post_proposal_draft(body)
            elif path == "/api/file-reviews/draft":
                self._post_file_review_draft(body)
            elif path == "/api/file-reviews":
                self._post_file_review(body)
            elif path == "/api/file-findings":
                self._post_file_finding(body)
            elif path.startswith("/api/file-findings/"):
                self._post_file_finding_action(path, body)
            elif path.startswith("/api/file-drafts/"):
                self._post_file_draft_adoption(path, body)
            elif path == "/api/dispositions":
                self._post_dispositions(body)
            elif path.startswith("/api/proposals/"):
                self._post_proposal_transition(path, body)
            elif path == "/api/fetch":
                self._post_fetch(body)
            elif path == "/api/enrich":
                self._post_enrich(body)
            elif path == "/api/decide":
                self._post_decide(body)
            else:
                raise RequestProblem(404, "not_found", "not found")
        except RequestProblem as problem:
            self.close_connection = True
            self._problem(problem)
        except Exception as exc:  # noqa: BLE001
            conflict = getattr(store_module, "StoreConflictError", ())
            incomplete = getattr(store_module, "IncompleteEvidenceError", ())
            service_error = getattr(service_module, "ServiceError", ())
            if service_error and isinstance(exc, service_error):
                result = exc.envelope()
                self._send_json(int(getattr(exc, "status", 500)), result)
            elif conflict and isinstance(exc, conflict):
                self._send_json(409, {"error": "target changed; reload before deciding",
                    "code": getattr(exc, "code", "revision_conflict"),
                    "repo": getattr(exc, "current_repo", ""),
                    "version": getattr(exc, "current_version", None)})
            elif incomplete and isinstance(exc, incomplete):
                self._send_json(422, {"error": "complete evidence is required",
                                      "code": "incomplete_evidence"})
            elif isinstance(exc, KeyError):
                self._send_json(404, {"error": "target not found", "code": "not_found"})
            elif isinstance(exc, getattr(store_module, "UnknownReviewPathError", ())):
                self._send_json(404, {"error": "path is not part of this pull "
                                               "request revision",
                                      "code": "unknown_path"})
            elif isinstance(exc, ValueError):
                self._send_json(400, {"error": "request is invalid", "code": "invalid_request"})
            else:
                traceback.print_exc()
                self._send_json(500, {"error": "internal server error", "code": "internal_error"})

    def _post_tool_read(self, body: dict[str, Any]) -> None:
        self._fields(body, required={"operation", "args"})
        operation, args = body["operation"], body["args"]
        if not isinstance(operation, str) or not isinstance(args, dict):
            raise RequestProblem(400, "invalid_request", "operation and args are required")
        if operation != "get_workspace" and "repo" not in args:
            raise RequestProblem(400, "missing_fields", "missing fields: repo")
        requested = args.get("repo") if "repo" in args else None
        store_path, repo = self._route_request(
            requested, required=operation != "get_workspace", allow_default=True,
        )
        # Bind even the default get_workspace call explicitly so a service
        # implementation cannot accidentally consult a browser/server active
        # repository.  This is a request-local copy; caller args are untouched.
        routed_args = dict(args)
        if repo:
            routed_args["repo"] = repo
        result = service_module.dispatch_read(store_path, operation, routed_args)
        if operation == "get_workspace" and result.get("ok") is True:
            # Discovery is advisory metadata only.  It is resolved from the
            # frozen router and never changes the request's selected repo.
            try:
                discovery = self._workspaces_payload()
            except RequestProblem:
                discovery = {"workspaces": [], "truncated": True}
            data = result.get("data")
            if isinstance(data, dict):
                data = dict(data)
                data["available_workspaces"] = discovery["workspaces"]
                data["workspaces_truncated"] = bool(discovery["truncated"])
                result = dict(result)
                result["data"] = data
        if result.get("ok") is not True:
            error = result.get("error") or {}
            status = error.get("status", 500)
            if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 599:
                status = 500
            self._send_json(status, result)
            return
        self._send_json(200, result)

    @staticmethod
    def _proposal_items(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value or len(value) > 200:
            raise RequestProblem(400, "invalid_items", "items must be a bounded non-empty list")
        rows: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                raise RequestProblem(400, "invalid_items", "each item must be an object")
            # HTTP callers must bind proposals to the exact evidence they
            # displayed. The store may fill refs for small direct Python uses,
            # but the network boundary never accepts an unbound item.
            if not isinstance(item.get("revision"), dict):
                raise RequestProblem(400, "revision_required", "each item requires an exact revision ref")
            if item.get("disposition", item.get("decision")) == "duplicate" and not isinstance(
                item.get("duplicate_of_revision", item.get("canonical_revision")), dict
            ):
                raise RequestProblem(400, "revision_required", "duplicate requires an exact canonical revision ref")
            rows.append(item)
        return rows

    def _proposal_versions(self, body: dict[str, Any]) -> tuple[int, int]:
        if "expected_store_version" not in body and "expected_version" in body:
            body["expected_store_version"] = body["expected_version"]
        if "expected_snapshot_version" not in body:
            raise RequestProblem(400, "missing_fields", "missing fields: expected_snapshot_version")
        return (
            self._json_int(body.get("expected_store_version"), "expected_store_version", 0, 9_007_199_254_740_991),
            self._json_int(body.get("expected_snapshot_version"), "expected_snapshot_version", 0, 9_007_199_254_740_991),
        )

    def _proposal_key(self, body: dict[str, Any]) -> str:
        value = body.get("idempotency_key")
        if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
            raise RequestProblem(400, "invalid_idempotency_key", "idempotency_key must be 8-128 safe characters")
        return value

    def _post_proposal_draft(self, body: dict[str, Any]) -> None:
        self._fields(body, required={"repo", "group_id", "items", "expected_store_version",
                                      "expected_snapshot_version", "idempotency_key"},
                     optional={"canonical_pr", "provenance", "context", "actor", "expected_version"})
        repo = _repo_key(body["repo"])
        store_path, _ = self._route_request(repo, required=True)
        self._require_initialized()
        group_id = body["group_id"]
        if not isinstance(group_id, str) or not _GROUP_ID.fullmatch(group_id):
            raise RequestProblem(400, "invalid_group", "group_id is invalid")
        items = self._proposal_items(body["items"])
        canonical = body.get("canonical_pr")
        if canonical is not None:
            canonical = self._json_int(canonical, "canonical_pr", 1, 2_147_483_647)
        expected_version, expected_snapshot = self._proposal_versions(body)
        key = self._proposal_key(body)
        provenance = body.get("provenance", {})
        context = body.get("context", {})
        if not isinstance(provenance, dict) or not isinstance(context, dict):
            raise RequestProblem(400, "invalid_request", "provenance and context must be objects")
        actor = body.get("actor", "agent")
        if not isinstance(actor, str) or not 1 <= len(actor.strip()) <= 256:
            raise RequestProblem(400, "invalid_actor", "actor must be 1-256 characters")
        request = service_module.ProposalRequest(
            repo=repo, group_id=group_id, items=tuple(items), canonical_pr=canonical,
            expected_store_version=expected_version,
            expected_snapshot_version=expected_snapshot,
            idempotency_key=key, provenance=provenance, context=context,
        )
        result = service_module.WorkspaceService(store_path).draft_proposal(
            request, actor=actor
        )
        if result.get("ok") is not True:
            error = result.get("error") or {}
            status = error.get("status", 500)
            self._send_json(status if isinstance(status, int) else 500, result)
            return
        # Keep the standard envelope while also exposing the proposal at the
        # top level for small browser clients that do not need generic tool
        # envelope plumbing.
        response = dict(result)
        if isinstance(result.get("data"), dict):
            response["proposal"] = result["data"].get("proposal")
        self._send_json(201, response)

    def _post_dispositions(self, body: dict[str, Any]) -> None:
        self._fields(
            body,
            required={"repo", "group_id", "items", "expected_store_version",
                      "expected_snapshot_version", "idempotency_key", "actor"},
        )
        repo = _repo_key(body["repo"])
        store_path, _ = self._route_request(repo, required=True)
        self._require_initialized()
        group_id = body["group_id"]
        if not isinstance(group_id, str) or not _GROUP_ID.fullmatch(group_id):
            raise RequestProblem(400, "invalid_group", "group_id is invalid")
        items = self._proposal_items(body["items"])
        expected_version, expected_snapshot = self._proposal_versions(body)
        key = self._proposal_key(body)
        actor = body["actor"]
        if not isinstance(actor, str) or not 1 <= len(actor.strip()) <= 256:
            raise RequestProblem(400, "invalid_actor", "actor must be 1-256 characters")
        rows = store_module.save_dispositions(
            items, path=store_path, repo=repo, group_id=group_id,
            expected_version=expected_version,
            expected_snapshot_version=expected_snapshot,
            idempotency_key=key, actor=actor.strip(), source="human",
        )
        self._send_json(200, {
            "dispositions": [row.to_dict() for row in rows],
            "state": _state_payload(store_path, self._request_binding,
                                     mode=self.workspace_mode, repo=repo),
        })

    def _actor(self, value: Any) -> str:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 256:
            raise RequestProblem(400, "invalid_actor", "actor must be 1-256 characters")
        return value.strip()

    def _review_revision(self, value: Any) -> dict[str, Any]:
        """Every file write must name the exact revision the reviewer saw."""
        if not isinstance(value, dict):
            raise RequestProblem(400, "revision_required",
                                 "this write requires an exact revision ref")
        return value

    def _review_state(self, store_path: Path, repo: str) -> dict[str, Any]:
        return _state_payload(store_path, self._request_binding,
                              mode=self.workspace_mode, repo=repo)

    def _post_file_review(self, body: dict[str, Any]) -> None:
        """Mark or unmark one file as human-reviewed for one exact revision."""
        self._fields(
            body,
            required={"repo", "pr", "path", "reviewed", "revision",
                      "expected_store_version", "expected_snapshot_version",
                      "idempotency_key", "actor"},
            optional={"note", "expected_version"},
        )
        repo = _repo_key(body["repo"])
        store_path, _ = self._route_request(repo, required=True)
        self._require_initialized()
        number = self._json_int(body["pr"], "pr", 1, 2_147_483_647)
        file_path = _safe_file_path(body["path"])
        reviewed = body["reviewed"]
        if not isinstance(reviewed, bool):
            raise RequestProblem(400, "invalid_reviewed", "reviewed must be a boolean")
        note = body.get("note", "")
        if not isinstance(note, str):
            raise RequestProblem(400, "invalid_note", "note must be a string")
        revision = self._review_revision(body["revision"])
        expected_version, expected_snapshot = self._proposal_versions(body)
        result = store_module.save_file_review(
            store_path=store_path, repo=repo, pr=number, path=file_path,
            reviewed=reviewed, revision=revision, note=note,
            expected_version=expected_version,
            expected_snapshot_version=expected_snapshot,
            idempotency_key=self._proposal_key(body),
            actor=self._actor(body["actor"]),
        )
        self._send_json(200, {**result, "state": self._review_state(store_path, repo)})

    def _post_file_finding(self, body: dict[str, Any]) -> None:
        """Record one human finding about one exact file revision."""
        self._fields(
            body,
            required={"repo", "pr", "path", "severity", "title", "revision",
                      "expected_store_version", "expected_snapshot_version",
                      "idempotency_key", "actor"},
            optional={"explanation", "evidence", "suggested_fix", "line", "hunk",
                      "expected_version"},
        )
        repo = _repo_key(body["repo"])
        store_path, _ = self._route_request(repo, required=True)
        self._require_initialized()
        number = self._json_int(body["pr"], "pr", 1, 2_147_483_647)
        file_path = _safe_file_path(body["path"])
        line = body.get("line")
        if line is not None:
            line = self._json_int(line, "line", 1, 2_000_000_000)
        texts: dict[str, Any] = {}
        for name in ("severity", "title", "explanation", "evidence",
                     "suggested_fix", "hunk"):
            value = body.get(name, "")
            if not isinstance(value, str):
                raise RequestProblem(400, f"invalid_{name}", f"{name} must be a string")
            texts[name] = value
        revision = self._review_revision(body["revision"])
        expected_version, expected_snapshot = self._proposal_versions(body)
        result = store_module.save_file_finding(
            store_path=store_path, repo=repo, pr=number, path=file_path,
            line=line, revision=revision,
            expected_version=expected_version,
            expected_snapshot_version=expected_snapshot,
            idempotency_key=self._proposal_key(body),
            actor=self._actor(body["actor"]), **texts,
        )
        self._send_json(201, {**result, "state": self._review_state(store_path, repo)})

    def _post_file_finding_action(self, path: str, body: dict[str, Any]) -> None:
        """Resolve, reopen, or dismiss one finding; nothing else changes."""
        remainder = path[len("/api/file-findings/"):]
        finding_id, separator, action = remainder.partition("/")
        if not finding_id or not separator or action not in store_module.FINDING_ACTIONS:
            raise RequestProblem(404, "not_found", "finding route not found")
        self._fields(
            body,
            required={"repo", "expected_store_version", "expected_snapshot_version",
                      "idempotency_key", "actor"},
            optional={"expected_version"},
        )
        repo = _repo_key(body["repo"])
        store_path, _ = self._route_request(repo, required=True)
        self._require_initialized()
        expected_version, expected_snapshot = self._proposal_versions(body)
        result = store_module.update_file_finding(
            finding_id, action, store_path=store_path, repo=repo,
            expected_version=expected_version,
            expected_snapshot_version=expected_snapshot,
            idempotency_key=self._proposal_key(body),
            actor=self._actor(body["actor"]),
        )
        self._send_json(200, {**result, "state": self._review_state(store_path, repo)})

    def _post_file_draft_adoption(self, path: str, body: dict[str, Any]) -> None:
        """Accept or dismiss one drafted finding as a deliberate human act.

        Adoption lives only on this CSRF-guarded browser route.  No MCP or
        WebMCP tool can reach it, so an agent can propose but never accept.
        """
        parts = path[len("/api/file-drafts/"):].split("/")
        if len(parts) != 4 or parts[1] != "findings" or not parts[0] or not parts[2]:
            raise RequestProblem(404, "not_found", "draft route not found")
        draft_id, _findings, draft_finding_id, action = parts
        if action not in {"accept", "dismiss"}:
            raise RequestProblem(404, "not_found", "draft route not found")
        self._fields(
            body,
            required={"repo", "expected_store_version", "expected_snapshot_version",
                      "idempotency_key", "actor"},
            optional={"expected_version"},
        )
        repo = _repo_key(body["repo"])
        store_path, _ = self._route_request(repo, required=True)
        self._require_initialized()
        expected_version, expected_snapshot = self._proposal_versions(body)
        result = store_module.adopt_draft_finding(
            draft_id, draft_finding_id, action, store_path=store_path, repo=repo,
            expected_version=expected_version,
            expected_snapshot_version=expected_snapshot,
            idempotency_key=self._proposal_key(body),
            actor=self._actor(body["actor"]),
        )
        self._send_json(200, {**result, "state": self._review_state(store_path, repo)})

    def _post_file_review_draft(self, body: dict[str, Any]) -> None:
        """Accept one agent file-review draft; a draft never reviews or decides."""
        self._fields(
            body,
            required={"repo", "pr", "revision", "expected_store_version",
                      "expected_snapshot_version", "idempotency_key"},
            optional={"findings", "coverage", "provenance", "context", "actor",
                      "expected_version"},
        )
        repo = _repo_key(body["repo"])
        store_path, _ = self._route_request(repo, required=True)
        self._require_initialized()
        number = self._json_int(body["pr"], "pr", 1, 2_147_483_647)
        revision = self._review_revision(body["revision"])
        findings = body.get("findings", [])
        coverage = body.get("coverage", [])
        for name, rows, cap in (("findings", findings, 100), ("coverage", coverage, 500)):
            if not isinstance(rows, list) or len(rows) > cap:
                raise RequestProblem(400, f"invalid_{name}",
                                     f"{name} must be a bounded list")
            if any(not isinstance(item, dict) for item in rows):
                raise RequestProblem(400, f"invalid_{name}",
                                     f"each {name} entry must be an object")
        provenance = body.get("provenance", {})
        context = body.get("context", {})
        if not isinstance(provenance, dict) or not isinstance(context, dict):
            raise RequestProblem(400, "invalid_request",
                                 "provenance and context must be objects")
        expected_version, expected_snapshot = self._proposal_versions(body)
        request = service_module.FileReviewDraftRequest(
            repo=repo, pr=number, revision=revision,
            findings=tuple(findings), coverage=tuple(coverage),
            expected_store_version=expected_version,
            expected_snapshot_version=expected_snapshot,
            idempotency_key=self._proposal_key(body),
            provenance=provenance, context=context,
        )
        result = service_module.WorkspaceService(store_path).draft_file_review(
            request, actor=self._actor(body.get("actor", "agent")),
        )
        if result.get("ok") is not True:
            error = result.get("error") or {}
            status = error.get("status", 500)
            self._send_json(status if isinstance(status, int) else 500, result)
            return
        response = dict(result)
        if isinstance(result.get("data"), dict):
            response["draft"] = result["data"].get("draft")
        self._send_json(201, response)

    def _post_proposal_transition(self, path: str, body: dict[str, Any]) -> None:
        prefix = "/api/proposals/"
        remainder = path[len(prefix):]
        proposal_id, separator, action = remainder.partition("/")
        if not proposal_id or not separator or action not in {"accept", "edit", "reject"}:
            raise RequestProblem(404, "not_found", "proposal route not found")
        allowed = {"repo", "expected_store_version", "expected_snapshot_version",
                   "expected_version", "idempotency_key", "actor"}
        if action in {"accept", "edit"}:
            allowed |= {"items", "canonical_pr"}
        if action == "reject":
            allowed.add("reason")
        required = {"repo", "idempotency_key", "actor", "expected_snapshot_version"}
        if "expected_store_version" not in body and "expected_version" not in body:
            required.add("expected_store_version")
        self._fields(body, required=required, optional=allowed - required)
        repo = _repo_key(body["repo"])
        store_path, _ = self._route_request(repo, required=True)
        self._require_initialized()
        expected_version, expected_snapshot = self._proposal_versions(body)
        key = self._proposal_key(body)
        actor = body["actor"]
        if not isinstance(actor, str) or not 1 <= len(actor.strip()) <= 256:
            raise RequestProblem(400, "invalid_actor", "actor must be 1-256 characters")
        canonical = body.get("canonical_pr")
        if canonical is not None:
            canonical = self._json_int(canonical, "canonical_pr", 1, 2_147_483_647)
        if action == "edit" and "items" not in body:
            raise RequestProblem(400, "missing_fields", "missing fields: items")
        items = self._proposal_items(body["items"]) if action == "edit" or "items" in body else None
        kwargs: dict[str, Any] = {
            "path": store_path, "repo": repo,
            "expected_version": expected_version,
            "expected_snapshot_version": expected_snapshot,
            "idempotency_key": key, "actor": actor,
        }
        if action == "accept":
            proposal = store_module.accept_proposal(
                proposal_id, items=items, canonical_pr=canonical, **kwargs
            )
        elif action == "edit":
            proposal = store_module.edit_proposal(
                proposal_id, items=items or [], canonical_pr=canonical, **kwargs
            )
        else:
            proposal = store_module.reject_proposal(
                proposal_id, reason=body.get("reason", "rejected by maintainer"), **kwargs
            )
        self._send_json(200, {"proposal": proposal.to_dict(),
                              "state": _state_payload(store_path, self._request_binding,
                                                       mode=self.workspace_mode, repo=repo)})

    def _post_fetch(self, body: dict[str, Any]) -> None:
        self._fields(body, required={"source", "repo", "limit", "refresh"})
        source = body["source"]
        if source not in {"fixtures", "gh", "github"}:
            raise RequestProblem(400, "invalid_source", "source must be fixtures|gh|github")
        repo = _repo_key(body["repo"])
        store_path, _ = self._route_request(repo, required=True)
        requested_limit = self._json_int(body["limit"], "limit", 0, MAX_FETCH_LIMIT)
        effective_limit = MAX_FETCH_LIMIT if requested_limit == 0 else requested_limit
        refresh = body["refresh"]
        if not isinstance(refresh, bool):
            raise RequestProblem(400, "invalid_refresh", "refresh must be a boolean")
        store = self._bound_store()
        active_repo = str(store.get("repo") or "").strip().strip("/").lower()
        active_source = str(store.get("source") or "")
        if active_repo and active_repo != repo:
            raise RequestProblem(409, "repository_conflict",
                                 "repository does not match this store; use a separate workspace")
        canonical = "github" if source in {"gh", "github"} else source
        active_canonical = "github" if active_source in {"gh", "github"} else active_source
        if active_canonical and active_canonical != canonical:
            raise RequestProblem(409, "source_conflict",
                                 "source does not match this store; use a separate workspace")
        try:
            result = start_fetch_async(source, repo, effective_limit,
                                       store_path=store_path, refresh=refresh)
        except FetchBusyError as busy:
            # The active job belongs to another workspace.  Return its
            # captured, path-free progress so the caller can stop polling its
            # own workspace; a scoped GET /api/progress remains idle for B.
            self._send_json(409, {**busy.progress,
                                  "error": "fetch already running", "code": "fetch_busy"})
            return
        result["requested_limit"] = requested_limit
        self._send_json(202, result)

    def _post_enrich(self, body: dict[str, Any]) -> None:
        self._fields(body,
            required={"repo", "pr", "limit", "allow_external", "expected_version"},
            optional={"path"})
        if body["allow_external"] is not True:
            raise RequestProblem(403, "external_consent_required",
                                 "this request must explicitly allow external processing")
        repo = _repo_key(body["repo"])
        store_path, _ = self._route_request(repo, required=True)
        store, active = self._active_repo(repo)
        version = self._json_int(body["expected_version"], "expected_version", 0,
                                 9_007_199_254_740_991)
        if version != int(store.get("store_version") or 0):
            raise RequestProblem(409, "revision_conflict",
                                 "store version changed; reload before enriching")
        number = self._json_int(body["pr"], "pr", 1, 2_147_483_647)
        if number not in {int(p.get("number") or 0) for p in store.get("last_prs") or []}:
            raise RequestProblem(404, "pr_not_found", "PR is not in the active store")
        limit = self._json_int(body["limit"], "limit", 1, MAX_ENRICH_CANDIDATES)
        file_path = _safe_file_path(body["path"]) if body.get("path") else None
        self._send_json(200, _run_enrichment(
            number, repo=active, file_path=file_path, store_path=store_path,
            limit=limit, expected_version=version
        ))

    def _post_decide(self, body: dict[str, Any]) -> None:
        self._fields(body, required={"repo", "group_id", "decision", "expected_version",
                                          "idempotency_key"})
        repo = _repo_key(body["repo"])
        store_path, _ = self._route_request(repo, required=True)
        self._require_initialized()
        self._active_repo(repo)
        group_id = body["group_id"]
        if not isinstance(group_id, str) or not _GROUP_ID.fullmatch(group_id):
            raise RequestProblem(400, "invalid_group", "group_id is invalid")
        mapping = {"bless": "approve", "approve": "approve", "reject": "reject",
                   "hardware": "hardware", "needs-hardware": "hardware",
                   "upgrade": "upgrade", "can-break-upgrade": "upgrade"}
        decision = body["decision"]
        if not isinstance(decision, str) or decision not in mapping:
            raise RequestProblem(400, "invalid_decision",
                                 "decision must be approve|reject|hardware|upgrade")
        version = self._json_int(body["expected_version"], "expected_version", 0,
                                 9_007_199_254_740_991)
        key = body["idempotency_key"]
        if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
            raise RequestProblem(400, "invalid_idempotency_key",
                                 "idempotency_key must be 8-128 safe characters")
        rule = store_module.decide_group(
            group_id, mapping[decision], path=store_path, expected_repo=repo,
            expected_version=version, idempotency_key=key
        )
        self._send_json(200, {"decision": rule.to_dict(),
                              "state": _state_payload(store_path, self._request_binding,
                                                       mode=self.workspace_mode, repo=repo)})

    def _patches(self, parsed: Any) -> dict[str, Any]:
        query = self._query(parsed, {"repo", "path", "prs", "group_id", "page",
            "page_size", "patch_offset", "patch_limit"})
        requested = self._one(query, "repo") if "repo" in query else None
        self._route_request(requested, required=self.workspace_mode == "multi")
        store, active = self._active_repo(requested)
        file_path = _safe_file_path(self._one(query, "path"))
        raw_prs, group_id = self._one(query, "prs"), self._one(query, "group_id")
        if bool(raw_prs) == bool(group_id):
            raise RequestProblem(400, "target_required", "provide exactly one of prs or group_id")
        if group_id:
            if not _GROUP_ID.fullmatch(group_id):
                raise RequestProblem(400, "invalid_group", "group_id is invalid")
            group = next((g for g in store.get("last_groups") or []
                          if g.get("group_id") == group_id), None)
            if group is None:
                raise RequestProblem(404, "group_not_found", "group not found")
            numbers = [int(n) for n in group.get("pr_numbers") or []]
        else:
            tokens = raw_prs.split(",")
            if len(tokens) > MAX_PATCH_PRS or any(not token.isdigit() for token in tokens):
                raise RequestProblem(400, "invalid_prs", "prs must be a bounded integer list")
            numbers = [int(token) for token in tokens]
            if not numbers or any(n <= 0 for n in numbers) or len(set(numbers)) != len(numbers):
                raise RequestProblem(400, "invalid_prs", "prs must be unique positive integers")
        page = self._int(self._one(query, "page", "1"), "page", 1, 1_000_000)
        page_size = self._int(self._one(query, "page_size", str(MAX_PATCH_PAGE_SIZE)),
                              "page_size", 1, MAX_PATCH_PAGE_SIZE)
        offset = self._int(self._one(query, "patch_offset", "0"), "patch_offset",
                           0, 100_000_000)
        chunk = self._int(self._one(query, "patch_limit", "6000"), "patch_limit",
                          1, MAX_PATCH_CHUNK)
        start = (page - 1) * page_size
        selected = numbers[start:start + page_size]
        owner, name = parse_repo(active)
        fixture_source = str(store.get("source") or "") == "fixtures"
        stored_prs = {int(p.get("number") or 0): p for p in store.get("last_prs") or []}
        items, hashes, snapshot_ids = [], [], set()
        all_complete = bool(selected)
        for number in selected:
            stored = stored_prs.get(number)
            if stored is None:
                raise RequestProblem(409, "membership_conflict",
                                     "requested PR is not in the active store")
            evidence = ({"meta": stored, "files": stored.get("files") or [], "snapshot_id": ""}
                        if fixture_source
                        else (gh_module.cached_pr_evidence(
                            owner, name, number,
                            snapshot_id=stored.get("cache_snapshot_id", ""),
                        ) or {}))
            if not isinstance(evidence, dict):
                raise RequestProblem(409, "revision_conflict", "cached evidence is invalid")
            meta, files = evidence.get("meta"), evidence.get("files") or []
            self._guard_evidence_identity(
                stored,
                evidence,
                active_repo=active,
                fixture_source=fixture_source,
            )
            legacy_unverified = (
                not fixture_source and self._legacy_unverified(stored, evidence)
            )
            if evidence.get("snapshot_id"):
                snapshot_ids.add(str(evidence["snapshot_id"]))
                if len(snapshot_ids) > 1:
                    raise RequestProblem(409, "snapshot_conflict",
                                         "active cache snapshot changed; retry this read")
            unbound_preview = (
                legacy_unverified and self._legacy_revision_unbound(stored)
            )
            if meta and not unbound_preview and not self._revision_matches(stored, meta, files):
                raise RequestProblem(409, "revision_conflict",
                                     "cached evidence changed; refresh the triage snapshot")
            record = next((row for row in files if row.get("path") == file_path), None)
            patch = str((record or {}).get("patch") or "")
            complete = (
                bool(patch)
                and not legacy_unverified
                and bool(stored.get("evidence_complete", False))
                and bool((meta or {}).get("evidence_complete", False))
                and (record or {}).get("patch_complete") is True
            )
            complete = complete and not patch.rstrip().endswith(("… truncated", "... truncated"))
            incomplete_reasons = []
            if legacy_unverified:
                incomplete_reasons.append("unverified legacy cache preview")
            if not patch:
                incomplete_reasons.append("patch unavailable or binary")
            if stored.get("evidence_complete") is not True:
                incomplete_reasons.append("stored revision evidence is incomplete")
            if not fixture_source and (meta or {}).get("evidence_complete") is not True:
                incomplete_reasons.append("cached revision evidence is incomplete")
            if (meta or {}).get("files_cap_reached"):
                incomplete_reasons.append("provider file cap reached")
            if int((meta or {}).get("missing_patch_count") or 0):
                incomplete_reasons.append("one or more patches are unavailable")
            digest = hashlib.sha256(patch.encode()).hexdigest() if complete else None
            if digest:
                hashes.append(digest)
            all_complete = all_complete and complete
            end = min(len(patch), offset + chunk)
            next_offset = end if end < len(patch) else None
            items.append({"number": number, "path": file_path, "patch": patch[offset:end],
                "patch_offset": offset, "patch_length": len(patch),
                "next_patch_offset": next_offset,
                "preview_truncated": next_offset is not None or offset > 0,
                "source_complete": complete, "evidence_complete": complete,
                "legacy_unverified": legacy_unverified,
                "incomplete_reasons": incomplete_reasons,
                "content_sha256": digest,
                "head_sha": (meta or {}).get("head_sha") or stored.get("head_sha") or "",
                "base_sha": (meta or {}).get("base_sha") or stored.get("base_sha") or "",
                "snapshot_id": evidence.get("snapshot_id") or ""})
        equality = len(set(hashes)) == 1 if all_complete and len(selected) >= 2 else None
        total_pages = (len(numbers) + page_size - 1) // page_size
        return {"repo": active, "path": file_path, "items": items, "page": page,
                "page_size": page_size, "total_items": len(numbers),
                "total_pages": total_pages,
                "next_page": page + 1 if page < total_pages else None,
                "comparison": {"complete": all_complete,
                               "same_complete_patch": equality, "scope": "page"}}

    @staticmethod
    def _guard_evidence_identity(
        stored: dict[str, Any],
        evidence: dict[str, Any],
        *,
        active_repo: str,
        fixture_source: bool,
    ) -> None:
        """Require returned evidence to stay bound to the requested PR/snapshot."""
        meta = evidence.get("meta")
        if meta is None:
            return
        if not isinstance(meta, dict):
            raise RequestProblem(
                409,
                "revision_conflict",
                "cached evidence metadata is invalid",
            )
        expected_number = stored.get("number")
        current_number = meta.get("number")
        if (
            type(expected_number) is not int
            or type(current_number) is not int
            or current_number != expected_number
        ):
            raise RequestProblem(
                409,
                "membership_conflict",
                "cached evidence PR number does not match the requested PR",
            )
        meta_repo = str(meta.get("repository") or "").strip().strip("/").lower()
        if meta_repo and meta_repo != active_repo:
            raise RequestProblem(
                409,
                "revision_conflict",
                "cached evidence repository does not match the active repository",
            )

        expected_snapshot = str(stored.get("cache_snapshot_id") or "")
        actual_snapshot = str(evidence.get("snapshot_id") or "")
        legacy_unverified = evidence.get("legacy_unverified") is True
        if fixture_source:
            if expected_snapshot or actual_snapshot:
                raise RequestProblem(
                    409,
                    "snapshot_conflict",
                    "fixture evidence must not carry a GitHub cache snapshot",
                )
            return
        if expected_snapshot:
            if actual_snapshot != expected_snapshot or legacy_unverified:
                raise RequestProblem(
                    409,
                    "snapshot_conflict",
                    "cached evidence snapshot does not match the triage snapshot",
                )
            if meta_repo != active_repo:
                raise RequestProblem(
                    409,
                    "revision_conflict",
                    "cached evidence has no exact repository identity",
                )
            return
        if actual_snapshot or not legacy_unverified:
            raise RequestProblem(
                409,
                "snapshot_conflict",
                "cached evidence has no exact snapshot identity",
            )

    @staticmethod
    def _legacy_unverified(stored: dict[str, Any], evidence: dict[str, Any]) -> bool:
        """Accept only an explicitly isolated pre-snapshot cache as a preview."""
        return (
            str(stored.get("cache_snapshot_id") or "") == ""
            and evidence.get("snapshot_id") == ""
            and evidence.get("legacy_unverified") is True
        )

    @staticmethod
    def _legacy_revision_unbound(stored: dict[str, Any]) -> bool:
        """Old rollout records without any content identity may preview only."""
        return not any(
            str(stored.get(field) or "")
            for field in ("content_digest", "head_sha", "base_sha")
        )

    @staticmethod
    def _revision_matches(
        stored: dict[str, Any], meta: dict[str, Any], files: list[dict[str, Any]]
    ) -> bool:
        compared = False
        for field in ("head_sha", "base_sha", "updated_at"):
            expected, current = stored.get(field), meta.get(field)
            if expected and current:
                compared = True
                if str(expected) != str(current):
                    return False
            elif expected or current:
                return False
        expected_digest = str(stored.get("content_digest") or "")
        # A record with a known revision but no content identity is not safe
        # to present as verified evidence.  Explicitly unbound legacy rows are
        # handled by the caller as previews before reaching this check.
        if not expected_digest and compared:
            return False
        if expected_digest:
            changed_files = [ChangedFile.from_dict(item) for item in files]
            candidate = PullRequest(
                number=int(meta.get("number") or stored.get("number") or 0),
                title=str(meta.get("title") or ""), body=str(meta.get("body") or ""),
                user=str(meta.get("user") or ""), changed_files=changed_files,
                created_at=str(meta.get("created_at") or ""),
                head_sha=str(meta.get("head_sha") or ""),
                base_sha=str(meta.get("base_sha") or ""),
                updated_at=str(meta.get("updated_at") or ""),
                additions=meta.get("additions"), deletions=meta.get("deletions"),
                evidence_complete=bool(meta.get("evidence_complete", False)),
                evidence_source=str(stored.get("evidence_source") or "github"),
            )
            if candidate.revision_evidence().content_digest != expected_digest:
                return False
        return compared or not any(stored.get(k) for k in ("head_sha", "base_sha", "updated_at"))

    def _serve_static(self, request_path: str) -> None:
        rel = request_path[1:] if request_path.startswith("/") else ""
        if not rel or any(part in {"", ".", ".."} for part in PurePosixPath(rel).parts):
            raise RequestProblem(403, "forbidden", "forbidden")
        root, candidate = WEB_DIR.resolve(), (WEB_DIR / rel).resolve()
        if not candidate.is_relative_to(root):
            raise RequestProblem(403, "forbidden", "forbidden")
        if not candidate.is_file():
            raise RequestProblem(404, "not_found", "not found")
        self._serve_file(candidate)

    def _serve_file(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise RequestProblem(404, "not_found", "not found") from exc
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript",
                                                                 "application/json"}:
            content_type += "; charset=utf-8"
        self._send(200, data, content_type)

    def do_OPTIONS(self) -> None:
        try:
            self._guard(mutation=False)
            self._send_json(405, {"error": "method not allowed", "code": "method_not_allowed"})
        except RequestProblem as problem:
            self._problem(problem)


def make_handler(store_path: Path | None, *, allowed_hostnames: tuple[str, ...] | None = None,
                 csrf_token: str | None = None,
                 body_timeout_seconds: float = BODY_TIMEOUT_SECONDS,
                 workspace_root: Path | None = None) -> type[BaseHTTPRequestHandler]:
    """Create a per-server handler with a fresh, unguessable session token."""
    token = csrf_token or secrets.token_urlsafe(32)
    allowed = tuple(host.lower() for host in (allowed_hostnames or ()))

    if store_path is not None and workspace_root is not None:
        raise ValueError("--store and --workspace-root are mutually exclusive")
    mode = "multi" if store_path is None else "single"
    router: WorkspaceRouter | None = None
    if mode == "multi":
        router = WorkspaceRouter(root=Path(workspace_root or DEFAULT_WORKSPACE_ROOT))

    class BoundHandler(TriageHandler):
        pass

    BoundHandler.store_path = Path(store_path) if store_path is not None else None
    BoundHandler.workspace_root = Path(workspace_root or DEFAULT_WORKSPACE_ROOT) if mode == "multi" else None
    BoundHandler.workspace_router = router
    BoundHandler.workspace_mode = mode
    BoundHandler.csrf_token = token
    BoundHandler.allowed_hostnames = allowed
    BoundHandler.body_timeout_seconds = body_timeout_seconds
    return BoundHandler


def _validate_loopback_host(host: str) -> None:
    if not host or host in {"0.0.0.0", "::", "*"}:
        raise ValueError("dashboard serving is restricted to loopback addresses")
    if host.lower() == "localhost":
        return
    try:
        loopback = ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError as exc:
        raise ValueError("dashboard host must be localhost or a literal loopback address") from exc
    if not loopback:
        raise ValueError("dashboard serving is restricted to loopback addresses")


def _serve_allowed_hostnames(host: str) -> tuple[str, ...]:
    """Return exact Host aliases that reach the same default IPv4 listener."""
    normalized = host.lower()
    if normalized in {"127.0.0.1", "localhost"}:
        return ("127.0.0.1", "localhost")
    return (normalized,)


def serve(host: str = "127.0.0.1", port: int = 8741, open_browser: bool = True,
          store_path: Path | None = None,
          workspace_root: Path | None = None) -> None:
    """Serve fixed ``--store`` or request-routed local workspaces."""
    _validate_loopback_host(host)
    if store_path is not None and workspace_root is not None:
        raise ValueError("--store and --workspace-root are mutually exclusive")
    httpd = BoundedThreadingHTTPServer((host, port), make_handler(
        store_path, workspace_root=workspace_root,
        allowed_hostnames=_serve_allowed_hostnames(host)
    ))
    actual_port = int(httpd.server_address[1])
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    url = f"http://{display_host}:{actual_port}"
    print(f"Omarchy triage dashboard: {url}", flush=True)
    print("(loopback only; refresh and external enrichment require explicit POSTs)", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            print(f"(could not open browser: {exc})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        httpd.server_close()
