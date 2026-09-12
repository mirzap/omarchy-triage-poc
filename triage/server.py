"""Local stdlib dashboard server for Omarchy triage (read-only re: GitHub)."""

from __future__ import annotations

import json
import mimetypes
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from triage.gh import cached_pr_files
from triage.github import parse_repo
from triage.pipeline import ingest, run_pipeline
from triage.embed import related
from triage.store import (
    DEFAULT_STORE_PATH,
    decide_group,
    load_store,
    overlap_for_group,
    prs_for_path,
    ui_state,
)

WEB_DIR = Path(__file__).resolve().parent / "web"

# Shared async-fetch progress (module-level so handler instances share it)
_fetch_lock = threading.Lock()
_fetch_progress: dict[str, Any] = {
    "running": False,
    "phase": "",
    "done": 0,
    "total": 0,
    "error": None,
    "ready": False,
    "message": "",
}
_fetch_thread: threading.Thread | None = None


def get_fetch_progress() -> dict[str, Any]:
    """Snapshot of current fetch progress for GET /api/progress."""
    with _fetch_lock:
        return {
            "running": bool(_fetch_progress["running"]),
            "phase": _fetch_progress.get("phase") or "",
            "done": int(_fetch_progress.get("done") or 0),
            "total": int(_fetch_progress.get("total") or 0),
            "error": _fetch_progress.get("error"),
            "ready": bool(_fetch_progress.get("ready")),
            "message": _fetch_progress.get("message") or "",
        }


def _set_progress(**fields: Any) -> None:
    with _fetch_lock:
        _fetch_progress.update(fields)


def ensure_initial_fixtures(store_path: Path = DEFAULT_STORE_PATH) -> None:
    """Do not seed fixtures. Real gh cache/store only."""
    return


def run_fetch(
    source: str,
    repo: str,
    limit: int,
    store_path: Path = DEFAULT_STORE_PATH,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synchronous fetch + pipeline. Used by unit tests and the background worker."""
    print(f"[serve] fetch source={source} repo={repo} limit={limit}", flush=True)
    if progress is not None:
        progress.update(
            phase="ingest",
            done=0,
            total=0,
            message=f"ingesting ({source})",
        )

    def on_progress(snap: dict[str, Any]) -> None:
        if progress is None:
            return
        progress.update(
            phase=snap.get("phase", progress.get("phase", "")),
            done=snap.get("done", progress.get("done", 0)),
            total=snap.get("total", progress.get("total", 0)),
            message=snap.get("message", ""),
        )
        # Mirror into module progress when this is the live worker dict
        _set_progress(
            phase=progress.get("phase", ""),
            done=progress.get("done", 0),
            total=progress.get("total", 0),
            message=progress.get("message", ""),
        )

    prs = ingest(
        source=source,
        repo=repo,
        limit=limit,
        progress=progress,
        on_progress=on_progress if progress is not None else None,
    )
    if progress is not None:
        progress.update(
            phase="pipeline",
            done=len(prs),
            total=len(prs),
            message=f"clustering {len(prs)} PRs",
        )
        _set_progress(
            phase="pipeline",
            done=len(prs),
            total=len(prs),
            message=f"clustering {len(prs)} PRs",
        )
    run_pipeline(
        prs,
        persist=True,
        store_path=store_path,
        apply_rules=True,
        source=source,
        repo=repo,
    )
    return ui_state(store_path)


def _fetch_worker(
    source: str,
    repo: str,
    limit: int,
    store_path: Path,
) -> None:
    local_progress: dict[str, Any] = {
        "phase": "starting",
        "done": 0,
        "total": 0,
        "message": "starting",
    }
    try:
        run_fetch(source, repo, limit, store_path=store_path, progress=local_progress)
        _set_progress(
            running=False,
            phase="done",
            error=None,
            ready=True,
            message=local_progress.get("message") or "ready",
            done=local_progress.get("done", 0),
            total=local_progress.get("total", 0),
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _set_progress(
            running=False,
            phase="error",
            error=str(exc),
            ready=False,
            message=str(exc),
        )


def start_fetch_async(
    source: str,
    repo: str,
    limit: int,
    store_path: Path = DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """
    Start a daemon fetch thread if none is running.
    Returns {"started": True} or raises FetchBusyError.
    """
    global _fetch_thread
    with _fetch_lock:
        if _fetch_progress.get("running"):
            snap = {
                "running": True,
                "phase": _fetch_progress.get("phase") or "",
                "done": int(_fetch_progress.get("done") or 0),
                "total": int(_fetch_progress.get("total") or 0),
                "error": _fetch_progress.get("error"),
                "ready": bool(_fetch_progress.get("ready")),
                "message": _fetch_progress.get("message") or "",
            }
            raise FetchBusyError(snap)
        _fetch_progress.update(
            {
                "running": True,
                "phase": "starting",
                "done": 0,
                "total": 0,
                "error": None,
                "ready": False,
                "message": "starting",
            }
        )
        _fetch_thread = threading.Thread(
            target=_fetch_worker,
            args=(source, repo, limit, store_path),
            daemon=True,
            name="triage-fetch",
        )
        _fetch_thread.start()
        return {"started": True}


class FetchBusyError(RuntimeError):
    def __init__(self, progress: dict[str, Any]) -> None:
        super().__init__("fetch already running")
        self.progress = progress


class TriageHandler(BaseHTTPRequestHandler):
    store_path: Path = DEFAULT_STORE_PATH

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            self._send_json(200, ui_state(self.store_path))
            return
        if path == "/api/progress":
            self._send_json(200, get_fetch_progress())
            return
        if path == "/api/overlap":
            qs = parse_qs(parsed.query)
            group_id = (qs.get("group_id") or [""])[0]
            if not group_id:
                self._send_json(400, {"error": "group_id required"})
                return
            try:
                self._send_json(200, overlap_for_group(group_id, path=self.store_path))
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            return
        if path == "/api/file":
            qs = parse_qs(parsed.query)
            file_path = (qs.get("path") or [""])[0]
            if not file_path:
                self._send_json(400, {"error": "path required"})
                return
            self._send_json(200, prs_for_path(file_path, path=self.store_path))
            return
        if path == "/api/related":
            qs = parse_qs(parsed.query)
            raw_pr = (qs.get("pr") or [""])[0]
            if not raw_pr.isdigit():
                self._send_json(400, {"error": "pr required"})
                return
            file_path = (qs.get("path") or [""])[0] or None
            self._send_json(
                200,
                related(int(raw_pr), file_path=file_path, store_path=self.store_path),
            )
            return
        if path == "/api/patches":
            qs = parse_qs(parsed.query)
            repo = (qs.get("repo") or [""])[0] or (load_store(self.store_path).get("repo") or "omacom/omarchy")
            file_path = (qs.get("path") or [""])[0]
            raw_prs = (qs.get("prs") or [""])[0]
            numbers = [int(x) for x in raw_prs.split(",") if x.strip().isdigit()][:40]
            try:
                owner, name = parse_repo(repo)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            items = []
            for n in numbers:
                files = cached_pr_files(owner, name, n)
                patch = ""
                for f in files:
                    if f.get("path") == file_path:
                        patch = f.get("patch") or ""
                        break
                items.append({"number": n, "path": file_path, "patch": patch})
            self._send_json(200, {"path": file_path, "items": items})
            return
        if path == "/" or path == "/index.html":
            self._serve_file(WEB_DIR / "index.html")
            return
        # Static files under web/
        if path.startswith("/"):
            rel = path.lstrip("/")
            # Refuse path traversal
            candidate = (WEB_DIR / rel).resolve()
            if not str(candidate).startswith(str(WEB_DIR.resolve())):
                self._send_json(403, {"error": "forbidden"})
                return
            if candidate.is_file():
                self._serve_file(candidate)
                return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/fetch":
                body = self._read_json()
                source = str(body.get("source", "fixtures"))
                repo = str(body.get("repo", "omacom/omarchy"))
                limit = int(body.get("limit", 0))
                if source not in ("fixtures", "gh", "github"):
                    self._send_json(400, {"error": f"invalid source: {source}"})
                    return
                # Never proxy GitHub writes — ingest is GET-only by construction
                try:
                    result = start_fetch_async(
                        source, repo, limit, store_path=self.store_path
                    )
                    self._send_json(200, result)
                except FetchBusyError as busy:
                    self._send_json(409, {"error": "fetch already running", **busy.progress})
                return
            if path == "/api/decide":
                body = self._read_json()
                group_id = str(body.get("group_id", ""))
                decision = str(body.get("decision", ""))
                # Map UI "bless" -> approve
                if decision in ("bless", "approve"):
                    decision = "approve"
                elif decision in ("reject",):
                    decision = "reject"
                else:
                    self._send_json(400, {"error": "decision must be approve|reject"})
                    return
                decide_group(group_id, decision, path=self.store_path)
                self._send_json(200, ui_state(self.store_path))
                return
            self._send_json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 — surface to UI for POC
            traceback.print_exc()
            self._send_json(500, {"error": str(exc)})

    def _serve_file(self, path: Path) -> None:
        data = path.read_bytes()
        ctype, _ = mimetypes.guess_type(str(path))
        if ctype is None:
            if path.suffix == ".js":
                ctype = "application/javascript"
            elif path.suffix == ".css":
                ctype = "text/css"
            else:
                ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
            "application/javascript",
            "application/json",
        ):
            ctype = f"{ctype}; charset=utf-8"
        self._send(200, data, ctype)


def make_handler(store_path: Path) -> type[BaseHTTPRequestHandler]:
    class BoundHandler(TriageHandler):
        pass

    BoundHandler.store_path = store_path
    return BoundHandler


def serve(
    host: str = "127.0.0.1",
    port: int = 8741,
    open_browser: bool = True,
    store_path: Path = DEFAULT_STORE_PATH,
) -> None:
    ensure_initial_fixtures(store_path)
    handler = make_handler(store_path)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    print(f"Omarchy triage dashboard: {url}", flush=True)
    print("(local only — Bless/Reject never write to GitHub)", flush=True)
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
