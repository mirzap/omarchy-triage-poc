"""OpenRouter voyage-code-4 ranker. Never used as a cluster edge."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from triage.gh import cached_pr_files
from triage.github import parse_repo
from triage.store import DEFAULT_STORE_PATH, load_store

MODEL = "voyageai/voyage-code-4"
DIM = 1024
MAX_CHARS = 12000
BATCH = 16
CANDIDATE_CAP = 80
OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"
CACHE_PATH = Path(".triage") / "embeddings.json"

_HEX = re.compile(r"\b[0-9a-f]{7,}\b", re.I)
_NUM = re.compile(r"\b\d{4,}\b")


def api_key() -> str:
    env = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if env:
        return env
    p = Path(".triage") / "openrouter.key"
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return ""


def normalize_patch(text: str) -> str:
    kept: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith(("index ", "diff --git", "--- ", "+++ ")):
            continue
        line = _HEX.sub("<hex>", line)
        line = _NUM.sub("<n>", line)
        kept.append(line)
    out = "\n".join(kept).strip()
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS]
    return out


def patch_text(owner: str, name: str, number: int, file_path: str | None = None) -> str:
    files = cached_pr_files(owner, name, number)
    parts: list[str] = []
    for f in files:
        path = f.get("path") or ""
        if file_path and path != file_path:
            continue
        patch = normalize_patch(f.get("patch") or "")
        if not patch:
            continue
        parts.append(f"# {path}\n{patch}")
    return "\n\n".join(parts)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _cache_key(number: int, file_path: str | None) -> str:
    return f"{number}::{file_path}" if file_path else str(number)


def load_cache(path: Path = CACHE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"model": MODEL, "dim": DIM, "items": {}}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("model") != MODEL or int(data.get("dim") or 0) != DIM:
        return {"model": MODEL, "dim": DIM, "items": {}}
    data.setdefault("items", {})
    return data


def save_cache(data: dict[str, Any], path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    tmp.replace(path)


def _embed_batch(texts: list[str], key: str) -> list[list[float]]:
    payload = json.dumps(
        {
            "model": MODEL,
            "input": texts,
            "dimensions": DIM,
            "encoding_format": "float",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://127.0.0.1:8741",
            "X-Title": "omarchy-triage-poc",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    rows = body.get("data") or []
    rows = sorted(rows, key=lambda r: int(r.get("index") or 0))
    vecs = [list(r.get("embedding") or []) for r in rows]
    if len(vecs) != len(texts):
        raise RuntimeError(f"embed count mismatch: {len(vecs)} vs {len(texts)}")
    return vecs


def ensure_vectors(
    jobs: list[tuple[str, str]],
    key: str,
    cache_path: Path = CACHE_PATH,
) -> dict[str, list[float]]:
    cache = load_cache(cache_path)
    items: dict[str, Any] = cache["items"]
    out: dict[str, list[float]] = {}
    missing: list[tuple[str, str]] = []
    for ck, text in jobs:
        rec = items.get(ck) or {}
        h = _text_hash(text)
        vec = rec.get("vec") if rec.get("hash") == h else None
        if isinstance(vec, list) and vec:
            out[ck] = vec
        elif text:
            missing.append((ck, text))
    for i in range(0, len(missing), BATCH):
        chunk = missing[i : i + BATCH]
        vecs = _embed_batch([t for _, t in chunk], key)
        for (ck, text), vec in zip(chunk, vecs):
            items[ck] = {"hash": _text_hash(text), "vec": vec}
            out[ck] = vec
        cache["items"] = items
        save_cache(cache, cache_path)
    return out


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def _candidates(prs: list[dict[str, Any]], query: dict[str, Any], file_path: str | None) -> list[dict[str, Any]]:
    qn = int(query["number"])
    if file_path:
        hits = [p for p in prs if file_path in (p.get("paths") or []) and int(p["number"]) != qn]
        return hits[:CANDIDATE_CAP]
    qpaths = set(query.get("paths") or [])
    scored: list[tuple[int, dict[str, Any]]] = []
    for p in prs:
        if int(p["number"]) == qn:
            continue
        shared = qpaths.intersection(p.get("paths") or [])
        if shared:
            scored.append((len(shared), p))
    scored.sort(key=lambda t: -t[0])
    return [p for _, p in scored[:CANDIDATE_CAP]]


def related(
    pr_number: int,
    *,
    file_path: str | None = None,
    store_path: Path = DEFAULT_STORE_PATH,
    k: int = 5,
) -> dict[str, Any]:
    key = api_key()
    if not key:
        return {
            "enabled": False,
            "reason": "OPENROUTER_API_KEY missing",
            "query": pr_number,
            "related": [],
        }
    store = load_store(store_path)
    repo = store.get("repo") or "omacom/omarchy"
    owner, name = parse_repo(repo)
    prs = store.get("last_prs") or []
    query = next((p for p in prs if int(p.get("number") or 0) == pr_number), None)
    if query is None:
        return {"enabled": True, "reason": "unknown PR", "query": pr_number, "related": []}
    qtext = patch_text(owner, name, pr_number, file_path)
    if not qtext:
        return {"enabled": True, "reason": "empty patch", "query": pr_number, "related": []}
    cands = _candidates(prs, query, file_path)
    jobs = [(_cache_key(pr_number, file_path), qtext)]
    for p in cands:
        n = int(p["number"])
        t = patch_text(owner, name, n, file_path)
        if not t:
            continue
        jobs.append((_cache_key(n, file_path), t))
    try:
        vecs = ensure_vectors(jobs, key)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        return {
            "enabled": True,
            "reason": f"openrouter {exc.code}: {detail}",
            "query": pr_number,
            "related": [],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "enabled": True,
            "reason": str(exc),
            "query": pr_number,
            "related": [],
        }
    qvec = vecs.get(_cache_key(pr_number, file_path))
    if not qvec:
        return {"enabled": True, "reason": "no query vector", "query": pr_number, "related": []}
    ranked: list[dict[str, Any]] = []
    for p in cands:
        n = int(p["number"])
        vec = vecs.get(_cache_key(n, file_path))
        if not vec:
            continue
        ranked.append(
            {
                "number": n,
                "title": p.get("title") or "",
                "user": p.get("user") or "",
                "group_id": p.get("group_id") or "",
                "html_url": p.get("html_url") or "",
                "score": round(cosine(qvec, vec), 4),
            }
        )
    ranked.sort(key=lambda r: -float(r["score"]))
    return {
        "enabled": True,
        "reason": "",
        "model": MODEL,
        "query": pr_number,
        "path": file_path or "",
        "related": ranked[:k],
    }
