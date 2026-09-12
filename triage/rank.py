"""Local-first related-PR retrieval with explicit optional enrichment.

All results are advisory. Cached reads never inspect credentials or use the
network; :func:`enrich_related` is the sole network entry point.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from triage import gh
from triage.github import parse_repo
from triage.models import ChangedFile, PullRequest
from triage.store import DEFAULT_STORE_PATH, load_store

PROVIDERS: dict[str, dict[str, Any]] = {
    "together": {"url": "https://api.together.xyz/v1/embeddings",
        "model": "intfloat/multilingual-e5-large-instruct", "dim": 1024,
        # E5 documents a 512-token context. Without its tokenizer, 480 UTF-8
        # bytes for the complete preprocessed input is a deliberately
        # conservative ASCII worst-case bound with room for special tokens.
        "env": "TOGETHER_API_KEY", "keyfile": "together.key", "max_bytes": 480, "payload_extra": {},
        "headers_extra": {"User-Agent": "omarchy-triage-poc/1", "Accept": "application/json"},
        "preprocessing": "e5-instruct-query-document-v1"},
    "openrouter": {"url": "https://openrouter.ai/api/v1/embeddings",
        "model": "voyageai/voyage-code-4", "dim": 1024,
        "env": "OPENROUTER_API_KEY", "keyfile": "openrouter.key", "max_bytes": 4000,
        "payload_extra": {"dimensions": 1024, "encoding_format": "float"},
        "headers_extra": {"HTTP-Referer": "http://127.0.0.1:8741", "X-Title": "omarchy-triage-poc"},
        "preprocessing": "plain-query-document-v1"},
}
BATCH = 8
CANDIDATE_CAP = 24
MAX_BATCH_CHARS = 120_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_CACHE_BYTES = 12 * 1024 * 1024
MAX_CACHE_ITEMS = 512
MAX_CACHE_RESULTS = 256
PROVIDER_TIMEOUT_SECONDS = 25
MAX_PROVIDER_CONCURRENCY = 2
MAX_CHUNKS_PER_PR = 3
MAX_EMBED_CHUNKS = (1 + CANDIDATE_CAP) * MAX_CHUNKS_PER_PR
ENRICH_TIME_BUDGET_SECONDS = 35
CACHE_SCHEMA_VERSION = 2
LOCAL_TEXT_CAP = 24_000
MAX_LOCAL_METADATA_BYTES = 4_000
MAX_LOCAL_PATCH_FILES = 24
MAX_LOCAL_TOKENS = 2_048
MAX_CORPUS_CACHE_BYTES = 16 * 1024 * 1024
MAX_KEY_BYTES = 16_384
QUERY_INSTRUCTION = "Given a pull request code change, retrieve pull requests with related implementation changes"
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*|0x[0-9a-fA-F]+|\d+(?:\.\d+)*")
_provider_slots = threading.BoundedSemaphore(MAX_PROVIDER_CONCURRENCY)
_inflight_lock = threading.Lock()
_inflight: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
_corpus_cache_lock = threading.Lock()
_corpus_cache: dict[str, tuple[list[dict[str, Any]], dict[str, int]]] = {}
_corpus_cache_sizes: dict[str, int] = {}
_CORPUS_CACHE_CAP = 4


class RankError(RuntimeError):
    """A safe user-displayable ranking failure."""


def provider_name() -> str:
    name = (os.environ.get("EMBED_PROVIDER") or "together").strip().lower()
    if name not in PROVIDERS:
        raise RankError("embedding provider is not supported")
    return name


def provider() -> dict[str, Any]:
    # The model and dimension are a fixed pair, never inferred from an env model.
    cfg = dict(PROVIDERS[provider_name()])
    override = (os.environ.get("EMBED_MODEL") or "").strip()
    if override and override != cfg["model"]:
        raise RankError("embedding model override is not supported for this provider")
    return cfg


def cache_path(store_path: Path = DEFAULT_STORE_PATH) -> Path:
    return Path(store_path).parent / "rank-cache.json"


def api_key(store_path: Path = DEFAULT_STORE_PATH) -> str:
    """Environment first, then a bounded regular key file in this workspace."""
    cfg = provider()
    configured = (os.environ.get(str(cfg["env"])) or "").strip()
    if configured:
        if len(configured.encode("utf-8")) > MAX_KEY_BYTES:
            raise RankError("embedding credential exceeded the size limit")
        return configured
    path = Path(store_path).parent / str(cfg["keyfile"])
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError):
        return ""
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return ""
        raw = os.read(descriptor, MAX_KEY_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_KEY_BYTES:
        raise RankError("embedding credential file exceeded the size limit")
    try:
        return raw.decode("utf-8").strip()
    except UnicodeError as exc:
        raise RankError("embedding credential file is not valid UTF-8") from exc


def _enrichment_disclosure() -> dict[str, Any]:
    """Non-secret configuration safe for a cache-only GET response."""
    try:
        cfg, name = provider(), provider_name()
    except RankError as exc:
        return {"enabled": False, "reason": str(exc), "requires_explicit_consent": True}
    return {"enabled": True, "provider": name, "model": cfg["model"],
        "max_candidates": CANDIDATE_CAP, "max_chunks_per_pr": MAX_CHUNKS_PER_PR,
        "max_input_bytes_per_chunk": cfg["max_bytes"], "max_total_input_bytes": MAX_BATCH_CHARS,
        "requires_explicit_consent": True}


def _valid_path(path: str | None) -> str | None:
    if path is None:
        return None
    value, pure = str(path).strip(), PurePosixPath(str(path).strip())
    if (not value or len(value) > 1024 or value.startswith(("/", "\\")) or "\\" in value
            or any(part in {"", ".", ".."} for part in pure.parts)):
        raise ValueError("file_path must be repository-relative")
    return value


def _repo_key(repo: str) -> str:
    value = str(repo or "").strip().strip("/").lower()
    try:
        owner, name = parse_repo(value)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("repo must be owner/name") from exc
    if f"{owner}/{name}".lower() != value:
        raise ValueError("repo must be canonical owner/name")
    return value


def _balanced(text: str, cap: int) -> tuple[str, int]:
    """Sample every hunk and both ends rather than retaining only a prefix."""
    if len(text) <= cap:
        return text, 0
    chunks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("@@") and current:
            chunks.append(current)
            current = []
        current.append(line)
    if current:
        chunks.append(current)
    if not chunks:
        half = max(1, (cap - 3) // 2)
        out = text[:half] + "\n…\n" + text[-half:]
        return out[:cap], len(text) - min(len(text), cap)
    share, kept = max(80, cap // len(chunks)), []
    for chunk in chunks:
        raw = "\n".join(chunk)
        if len(raw) > share:
            half = max(32, (share - 3) // 2)
            raw = raw[:half] + "\n…\n" + raw[-half:]
        kept.append(raw)
    out = "\n".join(kept)
    if len(out) > cap:
        half = max(1, (cap - 3) // 2)
        out = out[:half] + "\n…\n" + out[-half:]
    return out[:cap], max(0, len(text) - min(len(text), len(out)))


def _bounded_utf8(text: str, max_bytes: int) -> tuple[str, int]:
    """Bound exact UTF-8 bytes while retaining evidence from both ends."""
    encoded_size = len(text.encode("utf-8"))
    if encoded_size <= max_bytes:
        return text, 0
    char_cap = max(32, int(len(text) * max_bytes / encoded_size) - 8)
    out = _balanced(text, char_cap)[0]
    while len(out.encode("utf-8")) > max_bytes and char_cap > 32:
        char_cap = max(32, char_cap - max(1, char_cap // 12))
        out = _balanced(text, char_cap)[0]
    return out, encoded_size - len(out.encode("utf-8"))


_PATCH_METADATA_PREFIXES = (
    "index ", "diff --git", "--- ", "+++ ", "new file ", "deleted file ",
    "similarity ", "rename ",
)


def _semantic_patch_lines(text: str) -> Iterator[str]:
    """Drop file metadata only before a hunk; signed hunk content is code."""
    saw_hunk = False
    for line in (text or "").splitlines():
        if line.startswith("@@"):
            saw_hunk = True
        if not saw_hunk and line.startswith(_PATCH_METADATA_PREFIXES):
            continue
        yield line


def normalize_patch(text: str, *, cap: int = LOCAL_TEXT_CAP) -> str:
    """Remove transport noise but preserve meaningful constants and late hunks."""
    lines = list(_semantic_patch_lines(text))
    return _balanced("\n".join(lines).strip(), cap)[0]


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _digest(value: Any) -> str:
    return _text_hash(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")))


def _tokens(text: str) -> set[str]:
    return {match.group(0).casefold() for match in _TOKEN.finditer(text or "")}


def _numeric(tokens: set[str]) -> set[str]:
    return {token for token in tokens if token[0].isdigit() or token.startswith("0x")}


def _path_tokens(paths: set[str]) -> set[str]:
    out: set[str] = set()
    for path in paths:
        lower = path.casefold()
        out.add("path:" + lower)
        parts = [part for part in re.split(r"[/_.-]+", lower) if part]
        out.update("part:" + part for part in parts)
        if parts:
            out.add("base:" + parts[-1])
    return out


def _changed_token_facts(files: list[dict[str, Any]],
                         file_path: str | None = None) -> tuple[set[str], int]:
    ordered: dict[str, None] = {}
    for item in files:
        if file_path and file_path not in {item.get("path"), item.get("previous_path")}:
            continue
        for line in _semantic_patch_lines(str(item.get("patch") or "")):
            if line.startswith("@@"):
                continue
            prefix = "add:" if line.startswith("+") else "del:" if line.startswith("-") else "ctx:"
            for token in _tokens(line[1:] if line[:1] in "+- " else line):
                ordered.setdefault(token, None)
                ordered.setdefault(prefix + token, None)
    available = len(ordered)
    if available <= MAX_LOCAL_TOKENS:
        return set(ordered), available
    values = list(ordered)
    indices = {
        round(index * (available - 1) / (MAX_LOCAL_TOKENS - 1))
        for index in range(MAX_LOCAL_TOKENS)
    }
    return {values[index] for index in indices}, available


def _changed_tokens(files: list[dict[str, Any]], file_path: str | None = None) -> set[str]:
    return _changed_token_facts(files, file_path)[0]


def _sampled_files(files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Retain bounded, evenly distributed patch snippets and all path facts."""
    count = len(files)
    if count <= MAX_LOCAL_PATCH_FILES:
        selected_indices = list(range(count))
    elif MAX_LOCAL_PATCH_FILES == 1:
        selected_indices = [0]
    else:
        selected_indices = sorted({
            round(index * (count - 1) / (MAX_LOCAL_PATCH_FILES - 1))
            for index in range(MAX_LOCAL_PATCH_FILES)
        })
    selected = [files[index] for index in selected_indices]
    share = max(64, LOCAL_TEXT_CAP // max(1, len(selected)))
    sampled: list[dict[str, Any]] = []
    original_bytes = sum(len(str(item.get("patch") or "").encode("utf-8")) for item in files)
    retained_bytes = 0
    for item in selected:
        patch, _dropped = _bounded_utf8(str(item.get("patch") or ""), share)
        retained_bytes += len(patch.encode("utf-8"))
        sampled.append({
            "path": str(item.get("path") or ""),
            "previous_path": str(item.get("previous_path") or ""),
            "patch": patch,
            "patch_complete": item.get("patch_complete") is True,
        })
    return sampled, {
        "files_available": count,
        "files_sampled": len(sampled),
        "omitted_files": max(0, count - len(sampled)),
        "patch_bytes_available": original_bytes,
        "patch_bytes_sampled": retained_bytes,
        "omitted_patch_bytes": max(0, original_bytes - retained_bytes),
    }


def _fixture_files(pr: dict[str, Any]) -> list[dict[str, Any]]:
    raw = pr.get("files") or []
    if raw and isinstance(raw[0], str):
        return [{"path": str(path), "patch": "", "patch_complete": False} for path in raw]
    return [dict(item) for item in raw if isinstance(item, dict)]


def _bundle_pr(bundle: dict[str, Any]) -> PullRequest:
    meta = bundle.get("meta") or {}
    return PullRequest(number=int(meta.get("number") or 0), title=str(meta.get("title") or ""),
        body=str(meta.get("body") or ""), user=str(meta.get("user") or ""),
        changed_files=[ChangedFile.from_dict(item) for item in bundle.get("files") or []],
        created_at=str(meta.get("created_at") or ""), html_url=str(meta.get("html_url") or ""),
        head_sha=str(meta.get("head_sha") or ""), base_sha=str(meta.get("base_sha") or ""),
        updated_at=str(meta.get("updated_at") or ""),
        evidence_complete=bool(meta.get("evidence_complete")), evidence_source="github")


def _fixture_pr(item: dict[str, Any]) -> PullRequest:
    return PullRequest(number=int(item.get("number") or 0), title=str(item.get("title") or ""),
        body=str(item.get("body") or ""), user=str(item.get("user") or ""),
        changed_files=[ChangedFile.from_dict(changed) for changed in _fixture_files(item)],
        created_at=str(item.get("created_at") or ""), html_url=str(item.get("html_url") or ""),
        head_sha=str(item.get("head_sha") or ""), base_sha=str(item.get("base_sha") or ""),
        updated_at=str(item.get("updated_at") or ""), additions=item.get("additions"),
        deletions=item.get("deletions"), evidence_complete=bool(item.get("evidence_complete")),
        evidence_source="fixtures")


def _document(stored: dict[str, Any], files: list[dict[str, Any]], snapshot_id: str = "") -> dict[str, Any]:
    paths = {str(item.get("path") or "") for item in files if item.get("path")}
    previous = {str(item.get("previous_path") or "") for item in files if item.get("previous_path")}
    paths.update(str(path) for path in stored.get("paths") or [] if path)
    sampled_files, sampling = _sampled_files(files)
    content_tokens, available_tokens = _changed_token_facts(sampled_files)
    title, title_dropped = _bounded_utf8(str(stored.get("title") or ""), 512)
    body, body_dropped = _bounded_utf8(str(stored.get("body") or ""), MAX_LOCAL_METADATA_BYTES)
    sampling.update(tokens_available=available_tokens, tokens_retained=len(content_tokens),
                    omitted_tokens=max(0, available_tokens - len(content_tokens)),
                    omitted_metadata_bytes=title_dropped + body_dropped)
    return {"number": int(stored.get("number") or 0), "title": title,
        "body": body, "user": str(stored.get("user") or ""),
        "group_id": str(stored.get("group_id") or ""), "html_url": str(stored.get("html_url") or ""),
        "digest": str(stored.get("content_digest") or ""), "paths": paths,
        "previous_paths": previous, "path_tokens": _path_tokens(paths | previous),
        "content_tokens": content_tokens,
        "metadata_tokens": _tokens(f"{title}\n{body}"),
        "files": sampled_files, "local_input_truncation": sampling,
        "evidence_complete": bool(stored.get("evidence_complete")) and bool(files)
            and all(item.get("patch_complete") is True for item in files), "snapshot_id": snapshot_id}


def _corpus_memory_estimate(docs: list[dict[str, Any]]) -> int:
    estimate = 0
    for doc in docs:
        strings = (doc.get("title"), doc.get("body"), doc.get("user"), doc.get("group_id"),
                   doc.get("html_url"), doc.get("digest"), doc.get("snapshot_id"))
        estimate += 512 + sum(len(str(value or "").encode("utf-8")) for value in strings)
        estimate += sum(96 + len(value.encode("utf-8")) for value in doc.get("paths") or set())
        estimate += sum(96 + len(value.encode("utf-8")) for value in doc.get("previous_paths") or set())
        estimate += sum(96 + len(value.encode("utf-8")) for value in doc.get("content_tokens") or set())
        estimate += sum(96 + len(value.encode("utf-8")) for value in doc.get("metadata_tokens") or set())
        estimate += sum(256 + len(str(item.get("patch") or "").encode("utf-8"))
                        for item in doc.get("files") or [])
    return estimate


def _remember_corpus(cache_key: str, result: tuple[list[dict[str, Any]], dict[str, int]]) -> None:
    size = _corpus_memory_estimate(result[0])
    with _corpus_cache_lock:
        for stale in set(_corpus_cache_sizes) - set(_corpus_cache):
            _corpus_cache_sizes.pop(stale, None)
        while _corpus_cache and (
            len(_corpus_cache) >= _CORPUS_CACHE_CAP
            or sum(_corpus_cache_sizes.values()) + size > MAX_CORPUS_CACHE_BYTES
        ):
            oldest = next(iter(_corpus_cache))
            _corpus_cache.pop(oldest, None)
            _corpus_cache_sizes.pop(oldest, None)
        if size <= MAX_CORPUS_CACHE_BYTES:
            _corpus_cache[cache_key] = result
            _corpus_cache_sizes[cache_key] = size


def _corpus_identity(store: dict[str, Any], repo: str, store_path: Path) -> str:
    fixture_source = str(store.get("source") or "").strip().lower() == "fixtures"
    records = [{"number": item.get("number"), "title": item.get("title"),
        "body": item.get("body"), "user": item.get("user"),
        "group_id": item.get("group_id"), "html_url": item.get("html_url"),
        "digest": item.get("content_digest"), "head": item.get("head_sha"),
        "base": item.get("base_sha"), "updated_at": item.get("updated_at"),
        "additions": item.get("additions"), "deletions": item.get("deletions"),
        "evidence_complete": item.get("evidence_complete"),
        "snapshot": item.get("cache_snapshot_id"), "paths": item.get("paths"),
        # Fixture patches live in the store rather than an immutable snapshot.
        "fixture_files_digest": _digest(item.get("files") or []) if fixture_source else None}
        for item in store.get("last_prs") or []]
    return _digest({"workspace": str(store_path.resolve()), "repo": repo,
                    "source": store.get("source"), "records": records})


def _documents(store: dict[str, Any], repo: str, store_path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    cache_key = _corpus_identity(store, repo, store_path)
    with _corpus_cache_lock:
        cached = _corpus_cache.get(cache_key)
        if cached is not None:
            return cached
    source, stored_prs = str(store.get("source") or "").strip().lower(), store.get("last_prs") or []
    stats = {"unavailable": 0, "revision_mismatch": 0, "incomplete": 0}
    docs: list[dict[str, Any]] = []
    if source == "fixtures":
        for item in stored_prs:
            # Recompute, rather than trusting persisted flags. Fixture evidence
            # is local and must never be substituted from a same-number GH PR.
            canonical = _fixture_pr(item).revision_evidence()
            bound = canonical.evidence_complete and canonical.content_digest == str(item.get("content_digest") or "")
            doc = _document({**item, "evidence_complete": bound}, _fixture_files(item))
            docs.append(doc)
            stats["incomplete"] += not doc["evidence_complete"]
        result = (docs, stats)
        _remember_corpus(cache_key, result)
        return result
    owner, name = parse_repo(repo)
    for item in stored_prs:
        number = int(item.get("number") or 0)
        pinned_snapshot = str(item.get("cache_snapshot_id") or "")
        # Pass even a blank legacy id: the canonical helper then returns None
        # and never falls through to whichever snapshot is currently active.
        bundle = gh.cached_pr_evidence(owner, name, number, snapshot_id=pinned_snapshot)
        if not bundle:
            stats["unavailable"] += 1
            # Legacy or evicted evidence can still participate in transparent
            # title/path-only local advice. It is never eligible for egress.
            fallback = _document({**item, "evidence_complete": False}, [])
            docs.append(fallback)
            stats["incomplete"] += 1
            continue
        meta = bundle.get("meta") or {}
        canonical = _bundle_pr(bundle).revision_evidence()
        matches = (str(meta.get("repository") or "").strip().lower() == repo
            and canonical.pr_number == number
            and canonical.head_sha == str(item.get("head_sha") or "")
            and canonical.base_sha == str(item.get("base_sha") or "")
            and canonical.content_digest == str(item.get("content_digest") or "")
            and str(meta.get("updated_at") or "") == str(item.get("updated_at") or ""))
        if not matches:
            stats["revision_mismatch"] += 1
            fallback = _document({**item, "evidence_complete": False}, [])
            docs.append(fallback)
            stats["incomplete"] += 1
            continue
        merged = {**item, "title": meta.get("title") or item.get("title") or "",
            "body": meta.get("body") or "", "user": meta.get("user") or item.get("user") or "",
            "evidence_complete": canonical.evidence_complete}
        doc = _document(merged, bundle.get("files") or [], str(bundle.get("snapshot_id") or ""))
        docs.append(doc)
        stats["incomplete"] += not doc["evidence_complete"]
    result = (docs, stats)
    _remember_corpus(cache_key, result)
    return result


def _weighted_overlap(a: set[str], b: set[str], weights: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    numerator = sum(weights.get(token, 1.0) ** 2 for token in a & b)
    na = sum(weights.get(token, 1.0) ** 2 for token in a)
    nb = sum(weights.get(token, 1.0) ** 2 for token in b)
    return numerator / math.sqrt(na * nb) if na and nb else 0.0


def _score_corpus(docs: list[dict[str, Any]], query: dict[str, Any], file_path: str | None) -> list[dict[str, Any]]:
    qcontent = _changed_tokens(query["files"], file_path) if file_path else query["content_tokens"]
    qpaths = {file_path} if file_path else set(query["paths"])
    qpath_tokens, token_df = _path_tokens(qpaths), Counter()
    for doc in docs:
        token_df.update(doc["content_tokens"] | doc["path_tokens"])
    total = max(1, len(docs))
    weights = {token: 1 + math.log((total + 1) / (count + 1)) for token, count in token_df.items()}
    rows, qnums = [], _numeric(qcontent)
    for doc in docs:
        if doc["number"] == query["number"]:
            continue
        content = _weighted_overlap(qcontent, doc["content_tokens"], weights)
        path = _weighted_overlap(qpath_tokens, doc["path_tokens"], weights)
        metadata = _weighted_overlap(query["metadata_tokens"], doc["metadata_tokens"], weights)
        shared_paths = sorted(qpaths & (doc["paths"] | doc["previous_paths"]))
        rename = bool(qpaths & doc["previous_paths"] or query["previous_paths"] & doc["paths"])
        cnums, numeric_shared = _numeric(doc["content_tokens"]), sorted(qnums & _numeric(doc["content_tokens"]))
        score = .62 * content + .30 * path + .08 * metadata
        score += .08 if shared_paths else 0
        score += .06 if rename else 0
        score += min(.12, .04 * len(numeric_shared))
        if qnums and cnums and not numeric_shared:
            score *= .72
        if score <= 0:
            continue
        shared_tokens = sorted(qcontent & doc["content_tokens"], key=lambda x: (-weights.get(x, 1), x))
        rows.append({"number": doc["number"], "title": doc["title"], "user": doc["user"],
            "group_id": doc["group_id"], "html_url": doc["html_url"],
            "score": round(min(1., score), 6), "score_kind": "local-evidence-similarity",
            "advisory": True, "evidence": {"complete": bool(query["evidence_complete"] and doc["evidence_complete"]),
                "shared_paths": shared_paths[:4], "shared_tokens": shared_tokens[:8],
                "matching_constants": numeric_shared[:6], "rename_or_relocation": rename,
                "input_truncation": dict(doc.get("local_input_truncation") or {})},
            "_content_score": content, "_path_score": path, "_doc": doc})
    rows.sort(key=lambda row: (-row["score"], row["number"]))
    return rows


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _local_result(pr_number: int, file_path: str | None, store: dict[str, Any], repo: str,
                  k: int, store_path: Path):
    docs, stats = _documents(store, repo, store_path)
    query = next((doc for doc in docs if doc["number"] == pr_number), None)
    result = {"enabled": True, "cache_only": True, "query": pr_number, "path": file_path or "",
        "source": "local-content-path", "advisory": True, "provider": None, "model": None,
        "enrichment": _enrichment_disclosure(),
        "related": [], "corpus_count": max(0, len(store.get("last_prs") or []) - 1),
        "candidate_count": 0, "evidence": {**stats, "query_complete": bool(query and query["evidence_complete"]),
            "query_input_truncation": dict((query or {}).get("local_input_truncation") or {})},
        "truncation": {"returned": 0, "available": 0, "rerank_cap": CANDIDATE_CAP}}
    if query is None:
        result["reason"] = "query evidence is unavailable or does not match the active revision"
        return result, [], None
    rows = _score_corpus(docs, query, file_path)
    result.update(candidate_count=len(rows), related=[_public(row) for row in rows[:k]],
        truncation={"returned": min(k, len(rows)), "available": len(rows),
            "omitted": max(0, len(rows) - k), "rerank_cap": CANDIDATE_CAP},
        reason="" if rows else "no related local evidence")
    return result, rows, query


def _diverse_pool(rows: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    orders = [sorted(rows, key=lambda r: (-r["_content_score"], r["number"])),
              sorted(rows, key=lambda r: (-r["_path_score"], r["number"])), rows]
    out, seen, cursor = [], set(), 0
    while len(out) < cap and any(cursor < len(order) for order in orders):
        for order in orders:
            if cursor < len(order) and order[cursor]["number"] not in seen:
                seen.add(order[cursor]["number"]); out.append(order[cursor])
                if len(out) == cap:
                    break
        cursor += 1
    return out


def _embedding_chunks(doc: dict[str, Any], file_path: str | None,
                      max_bytes: int) -> tuple[list[str], dict[str, Any]]:
    files = [item for item in doc["files"] if not file_path
             or file_path in {item.get("path"), item.get("previous_path")}]
    if not files and file_path:
        files = [{"path": file_path, "patch": "", "patch_complete": False}]
    elif not files:
        files = list(doc["files"])
    header = f"Title: {doc['title'][:200]}\nDescription: {doc['body'][:300]}\n"
    units: list[str] = []
    for item in files:
        names = f"File: {item.get('path') or ''}"
        if item.get("previous_path"):
            names += f" (previously {item['previous_path']})"
        patch = normalize_patch(str(item.get("patch") or ""))
        hunks = re.split(r"(?=^@@)", patch, flags=re.MULTILINE)
        units.extend(f"{names}\n{hunk}" for hunk in hunks if hunk.strip())
    if not units:
        units = ["\n".join(f"File: {item.get('path') or ''}" for item in files)]
    if len(units) <= MAX_CHUNKS_PER_PR:
        selected = units
    else:
        # Deterministically cover the beginning, middle, and end of the change.
        selected = [units[0], units[len(units) // 2], units[-1]]
    chunks = []
    omitted_bytes = sum(len(unit.encode("utf-8")) for unit in units) - sum(
        len(unit.encode("utf-8")) for unit in selected
    )
    for unit in selected:
        text, dropped = _bounded_utf8(header + unit, max_bytes)
        chunks.append(text); omitted_bytes += dropped
    return chunks, {"chunks_embedded": len(chunks), "chunks_available": len(units),
        "omitted_chunks": len(units) - len(selected), "omitted_bytes": max(0, omitted_bytes),
        "files": len(files), "max_bytes_per_chunk": max_bytes,
        "local_sampling": dict(doc.get("local_input_truncation") or {})}


def _empty_cache() -> dict[str, Any]:
    return {"schema_version": CACHE_SCHEMA_VERSION, "items": {}, "results": {}}


def _record_recency(record: Any) -> int:
    if not isinstance(record, dict):
        return 0
    value = record.get("cached_at_ns")
    if not isinstance(value, bool) and isinstance(value, int) and value >= 0:
        return value
    created = record.get("created_at")
    if not isinstance(created, bool) and isinstance(created, (int, float)) and created >= 0:
        return int(created * 1_000_000_000)
    return 0


def _drop_oldest(records: dict[str, Any]) -> bool:
    if not records:
        return False
    oldest = min(records, key=lambda key: (_record_recency(records[key]), key))
    del records[oldest]
    return True


def _cache_size(data: dict[str, Any]) -> int:
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _cache_entry_size(key: str, value: Any) -> int:
    # Include one comma as a safe upper bound for each object entry.
    return len(json.dumps(key, ensure_ascii=False).encode("utf-8")) + 2 + len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _prune_cache(data: dict[str, Any]) -> dict[str, Any]:
    """Keep the persisted cache safely below its bounded-read ceiling."""
    items, results = data["items"], data["results"]
    while len(items) > MAX_CACHE_ITEMS:
        _drop_oldest(items)
    while len(results) > MAX_CACHE_RESULTS:
        _drop_oldest(results)
    estimated = _cache_size(_empty_cache()) + sum(
        _cache_entry_size(key, value)
        for records in (items, results)
        for key, value in records.items()
    )
    oldest_first = sorted(
        (_record_recency(value), bucket, key, _cache_entry_size(key, value))
        for bucket, records in (("items", items), ("results", results))
        for key, value in records.items()
    )
    for _age, bucket, key, entry_size in oldest_first:
        if estimated <= MAX_CACHE_BYTES:
            break
        del data[bucket][key]
        estimated -= entry_size
    return data


def load_cache(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else cache_path()
    try:
        with target.open("rb") as fh:
            raw = fh.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return _empty_cache()
        data = json.loads(raw.decode("utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return _empty_cache()
    if (not isinstance(data, dict) or data.get("schema_version") != CACHE_SCHEMA_VERSION
            or not isinstance(data.get("items"), dict) or not isinstance(data.get("results"), dict)):
        return _empty_cache()
    return data


@contextmanager
def _cache_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _atomic_write(data: dict[str, Any], path: Path) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as fh:
            temporary = Path(fh.name); json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush(); os.fsync(fh.fileno())
        os.replace(temporary, path); temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_cache(data: dict[str, Any], path: Path | None = None) -> None:
    """Merge records under a process-shared lock; no network is under this lock."""
    target = Path(path) if path is not None else cache_path()
    with _cache_lock(target):
        current = load_cache(target)
        stamp = time.time_ns()
        for bucket in ("items", "results"):
            incoming = data.get(bucket) or {}
            for offset, (key, value) in enumerate(incoming.items()):
                if not isinstance(value, dict):
                    continue
                record = dict(value)
                record.setdefault("cached_at_ns", stamp + offset)
                current[bucket][key] = record
        _atomic_write(_prune_cache(current), target)


def _preprocess(text: str, role: str, cfg: dict[str, Any]) -> str:
    if role not in {"query", "document"}:
        raise ValueError("embedding role must be query or document")
    if "e5" in str(cfg["model"]).casefold() and role == "query":
        return f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}"
    return text  # E5 documents are deliberately unprefixed per its model card.


class _NoCredentialRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never automatically forward a provider credential to a redirect."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


_provider_opener = urllib.request.build_opener(_NoCredentialRedirectHandler())


def _open_provider_request(request: urllib.request.Request, timeout: float) -> Any:
    return _provider_opener.open(request, timeout=timeout)


def _read_response(response: Any) -> dict[str, Any]:
    length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
    try:
        if length and (int(length) < 0 or int(length) > MAX_RESPONSE_BYTES):
            raise RankError("embedding response exceeded the size limit")
    except ValueError as exc:
        raise RankError("embedding response had an invalid size") from exc
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RankError("embedding response exceeded the size limit")
    try:
        body = json.loads(raw.decode())
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RankError("embedding provider returned an invalid response") from exc
    if not isinstance(body, dict):
        raise RankError("embedding provider returned an invalid response")
    return body


def _validate_vectors(body: dict[str, Any], count: int, cfg: dict[str, Any]) -> list[list[float]]:
    if body.get("model") != cfg["model"]:
        raise RankError("embedding response model did not match the request")
    rows = body.get("data")
    if not isinstance(rows, list) or len(rows) != count:
        raise RankError("embedding response count did not match the request")
    vectors: dict[int, list[float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RankError("embedding response contained an invalid item")
        index, raw = row.get("index"), row.get("embedding")
        if (isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count
                or index in vectors):
            raise RankError("embedding response contained invalid or duplicate indices")
        if not isinstance(raw, list) or len(raw) != int(cfg["dim"]):
            raise RankError("embedding response dimension did not match the model")
        vector = _finite_vector(raw, int(cfg["dim"]))
        if vector is None:
            raise RankError("embedding response contained an invalid vector")
        vectors[index] = vector
    if set(vectors) != set(range(count)):
        raise RankError("embedding response indices were incomplete")
    return [vectors[index] for index in range(count)]


def _post_embed(texts: list[str], key: str, *, role: str = "document",
                timeout: float = PROVIDER_TIMEOUT_SECONDS) -> list[list[float]]:
    cfg = provider()
    inputs = [_preprocess(text, role, cfg) for text in texts]
    if any(len(text.encode("utf-8")) > int(cfg["max_bytes"]) for text in inputs):
        raise RankError("embedding input exceeded the model byte limit")
    if sum(len(text.encode("utf-8")) for text in inputs) > MAX_BATCH_CHARS:
        raise RankError("embedding request exceeded the character budget")
    body = {"model": cfg["model"], "input": inputs, **(cfg.get("payload_extra") or {})}
    request = urllib.request.Request(cfg["url"], data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 **(cfg.get("headers_extra") or {})})
    absolute_timeout = max(.001, min(PROVIDER_TIMEOUT_SECONDS, timeout))
    request_deadline = time.monotonic() + absolute_timeout
    if not _provider_slots.acquire(timeout=max(0., request_deadline - time.monotonic())):
        raise RankError("embedding enrichment is busy")
    remaining = request_deadline - time.monotonic()
    if remaining <= 0:
        _provider_slots.release()
        raise RankError("embedding enrichment exceeded the time budget")
    finished, holder = threading.Event(), {}

    def perform() -> None:
        try:
            with _open_provider_request(request, timeout=remaining) as response:
                holder["body"] = _read_response(response)
        except urllib.error.HTTPError as exc:
            holder["error"] = RankError(f"embedding provider request failed ({exc.code})")
        except RankError as exc:
            holder["error"] = exc
        except (urllib.error.URLError, TimeoutError, OSError):
            holder["error"] = RankError("embedding provider request failed")
        except Exception:  # noqa: BLE001 - provider details must never escape
            holder["error"] = RankError("embedding provider request failed")
        finally:
            _provider_slots.release()
            finished.set()

    worker = threading.Thread(target=perform, daemon=True, name="triage-embedding-request")
    try:
        worker.start()
    except Exception:
        _provider_slots.release()
        raise RankError("embedding provider request failed") from None
    if not finished.wait(max(0., request_deadline - time.monotonic())):
        # The bounded provider slot remains owned until the underlying request
        # really stops; retries cannot create an unbounded set of runaway calls.
        raise RankError("embedding enrichment exceeded the time budget")
    if "error" in holder:
        raise holder["error"]
    response_body = holder.get("body")
    if not isinstance(response_body, dict):
        raise RankError("embedding provider returned an invalid response")
    return _validate_vectors(response_body, len(inputs), cfg)


def _embed_batch(texts: list[str], key: str, *, role: str = "document",
                 deadline: float | None = None) -> list[list[float]]:
    out, chunk, chars = [], [], 0
    for text in texts:
        text_bytes = len(text.encode("utf-8"))
        if chunk and (len(chunk) >= BATCH or chars + text_bytes > MAX_BATCH_CHARS):
            remaining = PROVIDER_TIMEOUT_SECONDS if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                raise RankError("embedding enrichment exceeded the time budget")
            out.extend(_post_embed(chunk, key, role=role, timeout=remaining)); chunk, chars = [], 0
        chunk.append(text); chars += text_bytes
    if chunk:
        remaining = PROVIDER_TIMEOUT_SECONDS if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise RankError("embedding enrichment exceeded the time budget")
        out.extend(_post_embed(chunk, key, role=role, timeout=remaining))
    return out


def _vector_identity(repo: str, digest: str, provider_value: str, model: str,
        role: str, preprocessing: str, file_path: str | None) -> tuple[str, dict[str, Any]]:
    identity = {"repo": repo, "content_digest": digest, "provider": provider_value,
        "model": model, "role": role, "preprocessing": preprocessing, "file_path": file_path or ""}
    return _digest(identity), identity


def _finite_vector(value: Any, dim: int) -> list[float] | None:
    """Return finite, nonzero floats without leaking numeric conversion errors."""
    if not isinstance(value, list) or len(value) != dim:
        return None
    converted: list[float] = []
    nonzero = False
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        try:
            number = float(item)
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        converted.append(number)
        nonzero = nonzero or number != 0.0
    return converted if nonzero else None


def _valid_vector(value: Any, dim: int) -> bool:
    return _finite_vector(value, dim) is not None


def ensure_vectors(jobs: list[tuple[str, str]] | list[dict[str, Any]], key: str,
        cache_file: Path | None = None, *, role: str = "document",
        deadline: float | None = None) -> dict[str, list[float]]:
    cfg = provider()
    normalized = [job if isinstance(job, dict) else {"key": job[0], "text": job[1], "identity": None}
                  for job in jobs]
    cache, out, missing = load_cache(cache_file), {}, []
    for job in normalized:
        record = cache["items"].get(job["key"])
        if (isinstance(record, dict) and record.get("text_hash") == _text_hash(job["text"])
                and record.get("identity") == job.get("identity")
                and _valid_vector(record.get("vec"), int(cfg["dim"]))):
            out[job["key"]] = [float(x) for x in record["vec"]]
        elif job["text"]:
            missing.append(job)
    if missing:
        vectors = _embed_batch([job["text"] for job in missing], key, role=role, deadline=deadline)
        updates = _empty_cache()
        for job, vector in zip(missing, vectors):
            updates["items"][job["key"]] = {"text_hash": _text_hash(job["text"]),
                "identity": job.get("identity"), "vec": vector}
            out[job["key"]] = vector
        save_cache(updates, cache_file)
    return out


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.
    left, right = _finite_vector(a, len(a)), _finite_vector(b, len(b))
    if left is None or right is None:
        return 0.
    # Scale each vector before its norm. Squaring raw finite provider values can
    # overflow (or tiny values can underflow), producing a non-finite JSON score.
    left_scale = max(abs(value) for value in left)
    right_scale = max(abs(value) for value in right)
    left_unit = [value / left_scale for value in left]
    right_unit = [value / right_scale for value in right]
    left_norm = math.sqrt(math.fsum(value * value for value in left_unit))
    right_norm = math.sqrt(math.fsum(value * value for value in right_unit))
    score = math.fsum(x * y for x, y in zip(left_unit, right_unit)) / (
        left_norm * right_norm
    )
    if not math.isfinite(score):
        return 0.
    return max(-1.0, min(1.0, score))


def _result_key(store: dict[str, Any], query: dict[str, Any], file_path: str | None,
        cfg: dict[str, Any], store_path: Path) -> str:
    repo = str(store.get("repo") or "").lower()
    return _digest({"workspace": str(store_path.resolve()), "corpus": _corpus_identity(store, repo, store_path),
        "repo": repo, "query_digest": query["digest"],
        "file_path": file_path or "", "provider": provider_name(), "model": cfg["model"],
        "preprocessing": cfg["preprocessing"]})


def related_cached(pr_number: int, *, file_path: str | None = None,
        store_path: Path, k: int = 5) -> dict[str, Any]:
    """Deterministic local retrieval, overlaid by a matching cached rerank if present."""
    if isinstance(pr_number, bool) or int(pr_number) < 1 or isinstance(k, bool) or not 1 <= int(k) <= 20:
        raise ValueError("invalid related request")
    file_path, store = _valid_path(file_path), load_store(Path(store_path))
    repo = _repo_key(str(store.get("repo") or ""))
    local, _rows, query = _local_result(int(pr_number), file_path, store, repo, int(k), Path(store_path))
    if query is None:
        return local
    try:
        cfg, cached = provider(), load_cache(cache_path(Path(store_path)))
        record = cached["results"].get(_result_key(store, query, file_path, cfg, Path(store_path)))
    except RankError:
        return local
    if not isinstance(record, dict) or not isinstance(record.get("related"), list):
        return local
    result = {**local, "cache_only": True, "source": "cached-embedding-rerank",
        "provider": record.get("provider"), "model": record.get("model"), "reason": "",
        "related": record["related"][:int(k)], "candidate_count": int(record.get("candidate_count") or 0),
        "query_input_truncation": dict(record.get("query_input_truncation") or {}),
        "truncation": dict(record.get("truncation") or local["truncation"])}
    result["truncation"]["returned"] = len(result["related"])
    return result


def _enrich_once(pr_number: int, repo: str, file_path: str | None, store_path: Path,
        limit: int, expected_version: int | None) -> dict[str, Any]:
    started, initial = time.monotonic(), load_store(store_path)
    deadline = started + ENRICH_TIME_BUDGET_SECONDS
    if str(initial.get("repo") or "").strip().lower() != repo:
        raise ValueError("repository does not match the active store")
    version = int(initial.get("store_version") or 0)
    if expected_version is not None and version != expected_version:
        raise ValueError("store version changed before enrichment")
    local, scored, query = _local_result(pr_number, file_path, initial, repo, limit, store_path)
    if query is None:
        return {**local, "cache_only": False}
    if not query["evidence_complete"]:
        return {**local, "cache_only": False, "reason": "complete revision-bound query evidence is required"}
    pool = _diverse_pool(
        [row for row in scored if row["_doc"]["evidence_complete"]],
        min(CANDIDATE_CAP, limit),
    )
    if not pool:
        return {**local, "cache_only": False}
    cfg, key = provider(), api_key(store_path)
    if not key:
        return {**local, "cache_only": False, "provider": provider_name(), "model": cfg["model"],
            "reason": f"{cfg['env']} is not configured; enrichment can be retried"}
    query_overhead = len(_preprocess("", "query", cfg).encode("utf-8"))
    query_chunks, query_trunc = _embedding_chunks(
        query, file_path, max(256, int(cfg["max_bytes"]) - query_overhead)
    )
    doc_chunks = [_embedding_chunks(row["_doc"], file_path, int(cfg["max_bytes"])) for row in pool]
    all_doc_chunks = [text for texts, _truncation in doc_chunks for text in texts]
    if (len(query_chunks) + len(all_doc_chunks) > MAX_EMBED_CHUNKS
            or sum(len(_preprocess(text, "query", cfg).encode("utf-8")) for text in query_chunks)
                + sum(len(text.encode("utf-8")) for text in all_doc_chunks) > MAX_BATCH_CHARS):
        raise RankError("embedding request exceeded the character budget")
    current = load_store(store_path)  # optimistic context check immediately before egress
    if int(current.get("store_version") or 0) != version or str(current.get("repo") or "").lower() != repo:
        raise ValueError("store changed before enrichment")
    qjobs = []
    for index, text in enumerate(query_chunks):
        qkey, qidentity = _vector_identity(repo, query["digest"], provider_name(), cfg["model"],
            "query", cfg["preprocessing"], f"{file_path or ''}#chunk={index}:{_text_hash(text)}")
        qjobs.append({"key": qkey, "text": text, "identity": qidentity})
    qvectors = ensure_vectors(qjobs, key, cache_path(store_path), role="query", deadline=deadline)
    jobs = []
    for row, (texts, _trunc) in zip(pool, doc_chunks):
        row["_vector_keys"] = []
        for index, text in enumerate(texts):
            vkey, identity = _vector_identity(repo, row["_doc"]["digest"], provider_name(), cfg["model"],
                "document", cfg["preprocessing"], f"{file_path or ''}#chunk={index}:{_text_hash(text)}")
            row["_vector_keys"].append(vkey)
            jobs.append({"key": vkey, "text": text, "identity": identity})
    dvectors = ensure_vectors(jobs, key, cache_path(store_path), role="document", deadline=deadline)
    ranked = []
    for row, (_texts, truncation) in zip(pool, doc_chunks):
        vectors = [dvectors[key] for key in row["_vector_keys"] if key in dvectors]
        if not vectors:
            continue
        public = _public(row)
        public.update(score=round(max(cosine(qvector, dvector)
            for qvector in qvectors.values() for dvector in vectors), 6),
            score_kind="embedding-cosine-ranking-signal", advisory=True, input_truncation=truncation)
        ranked.append(public)
    local_scores = {row["number"]: row["score"] for row in scored}
    ranked.sort(key=lambda row: (-row["score"], -local_scores.get(row["number"], 0), row["number"]))
    truncation = {"returned": min(limit, len(ranked)), "available_local": len(scored),
        "reranked": len(pool), "rerank_cap": CANDIDATE_CAP,
        "omitted_from_rerank": max(0, len(scored)-len(pool))}
    output = {**local, "cache_only": False, "source": "embedding-rerank",
        "provider": provider_name(), "model": cfg["model"], "reason": "", "related": ranked[:limit],
        "candidate_count": len(pool), "query_input_truncation": query_trunc, "truncation": truncation}
    latest = load_store(store_path)
    if int(latest.get("store_version") or 0) != version or str(latest.get("repo") or "").lower() != repo:
        raise ValueError("store changed during enrichment")
    save_cache({**_empty_cache(), "results": {_result_key(initial, query, file_path, cfg, store_path): {
        "provider": provider_name(), "model": cfg["model"], "related": ranked,
        "candidate_count": len(pool), "query_input_truncation": query_trunc,
        "truncation": truncation, "created_at": int(time.time()),
        "elapsed_ms": round((time.monotonic()-started)*1000)}}}, cache_path(store_path))
    return output


def enrich_related(pr_number: int, *, repo: str, file_path: str | None = None,
        store_path: Path, limit: int = 24,
        expected_version: int | None = None) -> dict[str, Any]:
    """Explicit enrichment. Per-request consent is enforced by the HTTP POST boundary."""
    if isinstance(pr_number, bool) or int(pr_number) < 1:
        raise ValueError("invalid PR number")
    if isinstance(limit, bool) or not 1 <= int(limit) <= CANDIDATE_CAP:
        raise ValueError("limit must be between 1 and 24")
    if expected_version is not None and (isinstance(expected_version, bool) or int(expected_version) < 0):
        raise ValueError("expected_version is invalid")
    repo, file_path = _repo_key(repo), _valid_path(file_path)
    cfg = provider()
    identity = _digest({"workspace": str(Path(store_path).resolve()), "repo": repo,
        "provider": provider_name(), "model": cfg["model"], "pr": int(pr_number),
        "path": file_path or "", "limit": int(limit), "expected_version": expected_version})
    with _inflight_lock:
        active = _inflight.get(identity)
        if active is None:
            event, holder, leader = threading.Event(), {}, True
            _inflight[identity] = (event, holder)
        else:
            event, holder, leader = *active, False
    if not leader:
        if not event.wait(PROVIDER_TIMEOUT_SECONDS * 3):
            raise RankError("embedding enrichment timed out")
        if "error" in holder:
            raise RankError(str(holder["error"]))
        return dict(holder["result"])
    try:
        result = _enrich_once(int(pr_number), repo, file_path, Path(store_path), int(limit),
            int(expected_version) if expected_version is not None else None)
        holder["result"] = result
        return result
    except Exception as exc:
        holder["error"] = str(exc) if isinstance(exc, (RankError, ValueError)) else "embedding enrichment failed"
        raise
    finally:
        event.set()
        with _inflight_lock:
            _inflight.pop(identity, None)


def related(pr_number: int, *, file_path: str | None = None,
        store_path: Path = DEFAULT_STORE_PATH, k: int = 5) -> dict[str, Any]:
    """Compatibility alias: legacy GET callers are now cache-only."""
    return related_cached(pr_number, file_path=file_path, store_path=store_path, k=k)
