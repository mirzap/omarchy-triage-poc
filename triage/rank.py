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

PROVIDERS = {
    "together": {
        "url": "https://api.together.xyz/v1/embeddings",
        "model": "intfloat/multilingual-e5-large-instruct",
        "dim": 1024,
        "env": "TOGETHER_API_KEY",
        "keyfile": "together.key",
        "max_chars": 600,
        "payload_extra": {},
        "headers_extra": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        },
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/embeddings",
        "model": "voyageai/voyage-code-4",
        "dim": 1024,
        "env": "OPENROUTER_API_KEY",
        "keyfile": "openrouter.key",
        "max_chars": 4000,
        "payload_extra": {"dimensions": 1024, "encoding_format": "float"},
        "headers_extra": {
            "HTTP-Referer": "http://127.0.0.1:8741",
            "X-Title": "omarchy-triage-poc",
        },
    },
}

BATCH = 8
CANDIDATE_CAP = 24
MAX_BATCH_CHARS = 400000

_HEX = re.compile(r"\b[0-9a-f]{7,}\b", re.I)
_NUM = re.compile(r"\b\d{4,}\b")


def provider_name() -> str:
    raw = (os.environ.get("EMBED_PROVIDER") or "together").strip().lower()
    return raw if raw in PROVIDERS else "together"


def provider() -> dict[str, Any]:
    cfg = dict(PROVIDERS[provider_name()])
    model = (os.environ.get("EMBED_MODEL") or "").strip()
    if model:
        cfg["model"] = model
    return cfg


def cache_path() -> Path:
    cfg = provider()
    slug = cfg["model"].replace("/", "-")
    return Path(".triage") / f"embeddings-{provider_name()}-{slug}.json"


def api_key() -> str:
    cfg = provider()
    env = (os.environ.get(cfg["env"]) or "").strip()
    if env:
        return env
    p = Path(".triage") / cfg["keyfile"]
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
    cap = int(provider().get("max_chars") or 4000)
    if len(out) > cap:
        out = out[:cap]
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
    joined = "\n\n".join(parts)
    cap = int(provider().get("max_chars") or 4000)
    if len(joined) > cap:
        joined = joined[:cap]
    return joined


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _cache_key(number: int, file_path: str | None) -> str:
    return f"{number}::{file_path}" if file_path else str(number)


def load_cache(path: Path | None = None) -> dict[str, Any]:
    cfg = provider()
    path = path or cache_path()
    if not path.exists():
        return {"model": cfg["model"], "dim": cfg["dim"], "items": {}}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("model") != cfg["model"] or int(data.get("dim") or 0) != cfg["dim"]:
        return {"model": cfg["model"], "dim": cfg["dim"], "items": {}}
    data.setdefault("items", {})
    return data


def save_cache(data: dict[str, Any], path: Path | None = None) -> None:
    path = path or cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    tmp.replace(path)


def _post_embed(texts: list[str], key: str) -> list[list[float]]:
    cfg = provider()
    texts_out = list(texts)
    if "e5" in str(cfg.get("model") or "").lower():
        texts_out = [
            t if t.lower().startswith(("query:", "passage:", "instruct:")) else ("passage: " + t)
            for t in texts
        ]
    body = {"model": cfg["model"], "input": texts_out}
    body.update(cfg.get("payload_extra") or {})
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    headers.update(cfg.get("headers_extra") or {})
    req = urllib.request.Request(
        cfg["url"],
        data=payload,
        headers=headers,
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


def _embed_batch(texts: list[str], key: str) -> list[list[float]]:
    out: list[list[float]] = []
    chunk: list[str] = []
    size = 0
    for text in texts:
        extra = len(text)
        if chunk and size + extra > MAX_BATCH_CHARS:
            out.extend(_post_embed(chunk, key))
            chunk = []
            size = 0
        chunk.append(text)
        size += extra
    if chunk:
        out.extend(_post_embed(chunk, key))
    return out


def ensure_vectors(
    jobs: list[tuple[str, str]],
    key: str,
    cache_file: Path | None = None,
) -> dict[str, list[float]]:
    cache = load_cache(cache_file)
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
        save_cache(cache, cache_file)
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
        cfg = provider()
        return {
            "enabled": False,
            "provider": provider_name(),
            "model": cfg["model"],
            "reason": f"{cfg['env']} missing (or .triage/{cfg['keyfile']}). Switch with EMBED_PROVIDER=together|openrouter.",
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
        if exc.code == 402:
            reason = f"{provider_name()} needs credits"
        else:
            detail = exc.read().decode("utf-8", errors="replace")[:180]
            reason = f"{provider_name()} {exc.code}: {detail}"
        return {
            "enabled": True,
            "provider": provider_name(),
            "reason": reason,
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
        "provider": provider_name(),
        "model": provider()["model"],
        "query": pr_number,
        "path": file_path or "",
        "related": ranked[:k],
    }
