"""Typed, cache-only query facade shared by HTTP and local MCP adapters.

This module deliberately contains no sync, provider, or persistence writes.  A
single dispatch loads the JSON store once, pins all GitHub reads to the
snapshot recorded in that store, and emits a bounded response envelope.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from triage import gh, rank, store
from triage.github import parse_repo
from triage.workspaces import InvalidRepoError, canonical_repo
from triage.models import (
    ChangedFile,
    DISPOSITIONS,
    Group,
    PullRequest,
    RevisionEvidence,
    unified_patch_line_counts,
)

Envelope = dict[str, Any]
MAX_INT = 9_007_199_254_740_991
MAX_PAGE = 80
MAX_GROUP_MEMBERS = 5_000
MAX_PR_LIST = 200
MAX_PATCH_CHUNK = 16_384
MIN_PATCH_CHUNK = 4
MAX_PATCH_OFFSET = 100_000_000
MAX_QUERY_BYTES = 256
MAX_REASON_CODES = 8
MAX_STATE_PATHS = 500
MAX_STATE_SHARED_FILES = 200
GROUP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")

READ_OPERATIONS = (
    "get_workspace", "list_groups", "search_prs", "get_group", "get_pr",
    "read_patch", "compare_prs", "find_related", "get_history",
)
_READ_SET = frozenset(READ_OPERATIONS)
_LABELS = frozenset({
    "unreviewed", "approved", "rejected", "hardware", "unique", "duplicate",
    "related-theme", "needs-look", "docs", "cosmetic", "update-path", "junk",
})
_PILES = frozenset({"needs_you", "known", "junk", "hardware", "upgrade", "hotspots"})
_DECISIONS = frozenset({"approve", "reject", "hardware", "upgrade"})
_FILTER_FIELDS = frozenset({"q", "label", "pile", "path", "user", "group_id", "decision", "pr"})


class ServiceError(Exception):
    """Safe, typed failure suitable for an HTTP status and tool envelope."""

    def __init__(self, status: int, code: str, message: str,
                 *, retryable: bool = False, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retryable = retryable
        self.context = dict(context or {})

    def envelope(self) -> Envelope:
        return {"ok": False, "error": {"status": self.status, "code": self.code,
            "message": self.message, "retryable": self.retryable, "context": self.context}}


@dataclass(frozen=True)
class _Context:
    data: dict[str, Any]
    repo: str
    source: str
    store_version: int
    snapshot_version: int
    snapshot_id: str

    def payload(self) -> dict[str, Any]:
        return {"repo": self.repo, "source": self.source,
                "store_version": self.store_version,
                "snapshot_version": self.snapshot_version,
                "snapshot_id": self.snapshot_id}


@dataclass(frozen=True)
class EvidenceBundle:
    """One PR's metadata and exact, pinned evidence (or an incomplete result)."""

    number: int
    meta: dict[str, Any]
    files: tuple[dict[str, Any], ...]
    revision: RevisionEvidence
    snapshot_id: str
    complete: bool
    reasons: tuple[str, ...]
    legacy_unverified: bool = False


@dataclass(frozen=True)
class ProposalRequest:
    """Typed request for an agent draft; drafts are never approvals."""

    repo: str
    group_id: str
    items: tuple[Mapping[str, Any], ...]
    canonical_pr: int | None = None
    expected_store_version: int | None = None
    expected_snapshot_version: int | None = None
    idempotency_key: str | None = None
    provenance: Mapping[str, Any] | None = None
    context: Mapping[str, Any] | None = None


def _schema(properties: Mapping[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    common = {
        "repo": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+$"},
        "expected_store_version": {"type": "integer", "minimum": 0, "maximum": MAX_INT},
        "expected_snapshot_version": {"type": "integer", "minimum": 0, "maximum": MAX_INT},
    }
    return {"type": "object", "properties": {**common, **properties},
            "additionalProperties": False, "required": required or []}


_FILTER_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "q": {"type": "string", "maxLength": 256},
    "label": {"type": "string", "enum": sorted(_LABELS)},
    "pile": {"type": "string", "enum": sorted(_PILES)},
    "path": {"type": "string", "maxLength": 1024},
    "user": {"type": "string", "maxLength": 256},
    "group_id": {"type": "string", "pattern": GROUP_ID.pattern[:-2]},
    "decision": {"type": "string", "enum": sorted(_DECISIONS)},
    "pr": {"type": "integer", "minimum": 1},
}}
_GROUP_FILTER_SCHEMA = {**_FILTER_SCHEMA, "properties": {
    **_FILTER_SCHEMA["properties"],
    "min_pr_count": {"type": "integer", "minimum": 0, "maximum": MAX_GROUP_MEMBERS},
    "sort": {"type": "string", "enum": ["pr_count_desc", "recent"]},
}}
_HISTORY_FILTER_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "group_id": {"type": "string", "pattern": GROUP_ID.pattern[:-2]},
    "decision": {"type": "string", "enum": sorted(_DECISIONS)},
    "pr": {"type": "integer", "minimum": 1},
    "include_legacy": {"type": "boolean"},
}}


def _tool(name: str, properties: Mapping[str, Any], required: list[str] | None = None,
          *, description: str = "Read local triage evidence; results are advisory.") -> dict[str, Any]:
    return {"name": name, "description": description, "read_only": True,
            "inputSchema": _schema(properties, required)}


TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    _tool("get_workspace", {}, description="Read active local workspace summary."),
    _tool("list_groups", {"page": {"type": "integer", "minimum": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
        "filters": _GROUP_FILTER_SCHEMA}, ["repo"]),
    _tool("search_prs", {"page": {"type": "integer", "minimum": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
        "filters": _FILTER_SCHEMA}, ["repo"]),
    _tool("get_group", {"group_id": {"type": "string", "pattern": GROUP_ID.pattern[:-2]},
        "member_page": {"type": "integer", "minimum": 1},
        "member_page_size": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE}}, ["repo", "group_id"]),
    _tool("get_pr", {"pr": {"type": "integer", "minimum": 1},
        "file_page": {"type": "integer", "minimum": 1},
        "file_page_size": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE}}, ["repo", "pr"]),
    _tool("read_patch", {"pr": {"type": "integer", "minimum": 1},
        "path": {"type": "string", "maxLength": 1024},
        "patch_offset": {"type": "integer", "minimum": 0, "maximum": MAX_PATCH_OFFSET},
        "patch_limit": {"type": "integer", "minimum": MIN_PATCH_CHUNK, "maximum": MAX_PATCH_CHUNK}},
        ["repo", "pr", "path"]),
    _tool("compare_prs", {"path": {"type": "string", "maxLength": 1024},
        "group_id": {"type": "string", "pattern": GROUP_ID.pattern[:-2]},
        "prs": {"type": "array", "items": {"type": "integer", "minimum": 1},
                "minItems": 1, "maxItems": MAX_PR_LIST, "uniqueItems": True},
        "page": {"type": "integer", "minimum": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 8},
        "patch_offset": {"type": "integer", "minimum": 0, "maximum": MAX_PATCH_OFFSET},
        "patch_limit": {"type": "integer", "minimum": MIN_PATCH_CHUNK, "maximum": MAX_PATCH_CHUNK}},
        ["repo", "path"]),
    _tool("find_related", {"pr": {"type": "integer", "minimum": 1},
        "path": {"type": "string", "maxLength": 1024},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["repo", "pr"]),
    _tool("get_history", {"page": {"type": "integer", "minimum": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
        "filters": _HISTORY_FILTER_SCHEMA}, ["repo"]),
)

# Exposed for the later stdio/WebMCP adapters.  It intentionally is not in
# ``TOOL_DEFINITIONS``: the shared read tool list remains read-only, and this
# operation can only create a draft (never accept or reject one).
DRAFT_PROPOSAL_TOOL_DEFINITION: dict[str, Any] = {
    "name": "propose_triage",
    "description": "Create a human-reviewable local triage proposal draft.",
    "read_only": False,
    "draft_only": True,
    "inputSchema": _schema({
        "group_id": {"type": "string", "pattern": GROUP_ID.pattern[:-2]},
        "canonical_pr": {"type": "integer", "minimum": 1},
        "items": {"type": "array", "minItems": 1, "maxItems": 200,
                  "items": {"type": "object", "additionalProperties": False,
                            "properties": {
                                "pr": {"type": "integer", "minimum": 1},
                                "disposition": {"type": "string", "enum": [*DISPOSITIONS]},
                                "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                                "revision": {"type": "object"},
                                "duplicate_of": {"type": "integer", "minimum": 1},
                                "duplicate_of_revision": {"type": "object"},
                            }, "required": ["pr", "disposition", "reason", "revision"]}},
        "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
        "provenance": {"type": "object"},
        "context": {"type": "object"},
    }, ["repo", "group_id", "items", "expected_store_version",
        "expected_snapshot_version", "idempotency_key"]),
}


def _error(status: int, code: str, message: str, *, retryable: bool = False,
           context: Mapping[str, Any] | None = None) -> ServiceError:
    return ServiceError(status, code, message, retryable=retryable, context=context)


def _repo(value: Any, *, required: bool = True) -> str:
    if value is None:
        if required:
            raise _error(409, "repository_unset", "this store has no active repository")
        return ""
    try:
        return canonical_repo(value)
    except InvalidRepoError as exc:
        if value == "" and not required:
            return ""
        raise _error(400, "invalid_repo", "repo must be canonical owner/name") from exc


def _path(value: Any) -> str:
    if not isinstance(value, str):
        raise _error(400, "invalid_path", "path must be repository-relative")
    try:
        raw = value.encode("utf-8", "strict").decode("utf-8")
    except UnicodeError as exc:
        raise _error(400, "invalid_path", "path must be repository-relative") from exc
    if not raw or len(raw.encode("utf-8")) > 1024 or "\x00" in raw or "\\" in raw:
        raise _error(400, "invalid_path", "path must be repository-relative")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise _error(400, "invalid_path", "path must be repository-relative")
    return raw


def _positive(value: Any, name: str, high: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
        raise _error(400, f"invalid_{name}", f"{name} is invalid")
    return value


def _nonnegative(value: Any, name: str, high: int = MAX_INT) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
        raise _error(400, f"invalid_{name}", f"{name} is invalid")
    return value


def _text(value: Any, name: str, max_bytes: int = MAX_QUERY_BYTES) -> str:
    if not isinstance(value, str):
        raise _error(400, f"invalid_{name}", f"{name} is invalid")
    try:
        if len(value.encode("utf-8", "strict")) > max_bytes:
            raise _error(400, f"invalid_{name}", f"{name} is invalid")
    except UnicodeError as exc:
        raise _error(400, f"invalid_{name}", f"{name} is invalid") from exc
    return value


def _filters(value: Any, *, groups: bool = False, history: bool = False) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _error(400, "invalid_filters", "filters must be an object")
    allowed = {"group_id", "decision", "pr", "include_legacy"} if history else set(_FILTER_FIELDS)
    if groups:
        allowed |= {"min_pr_count", "sort"}
    unknown = set(value) - allowed
    if unknown:
        raise _error(400, "unknown_parameter", f"unknown filters: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    if "q" in value: out["q"] = _text(value["q"], "q")
    if "label" in value:
        if not isinstance(value["label"], str) or value["label"] not in _LABELS:
            raise _error(400, "invalid_label", "label is invalid")
        out["label"] = value["label"]
    if "pile" in value:
        if not isinstance(value["pile"], str) or value["pile"] not in _PILES:
            raise _error(400, "invalid_pile", "pile is invalid")
        out["pile"] = value["pile"]
    if "path" in value: out["path"] = _path(value["path"])
    if "user" in value: out["user"] = _text(value["user"], "user")
    if "group_id" in value:
        if not isinstance(value["group_id"], str) or not GROUP_ID.fullmatch(value["group_id"]):
            raise _error(400, "invalid_group", "group_id is invalid")
        out["group_id"] = value["group_id"]
    if "decision" in value:
        if not isinstance(value["decision"], str) or value["decision"] not in _DECISIONS:
            raise _error(400, "invalid_decision", "decision is invalid")
        out["decision"] = value["decision"]
    if "pr" in value: out["pr"] = _positive(value["pr"], "pr")
    if "min_pr_count" in value: out["min_pr_count"] = _nonnegative(value["min_pr_count"], "min_pr_count", MAX_GROUP_MEMBERS)
    if "sort" in value:
        if not isinstance(value["sort"], str) or value["sort"] not in {"pr_count_desc", "recent"}:
            raise _error(400, "invalid_sort", "sort is invalid")
        out["sort"] = value["sort"]
    if "include_legacy" in value:
        if not isinstance(value["include_legacy"], bool): raise _error(400, "invalid_include_legacy", "include_legacy is invalid")
        out["include_legacy"] = value["include_legacy"]
    return out


def _page(value: Any, name: str, default: int, high: int = MAX_PAGE) -> int:
    return _positive(default if value is None else value, name, high)


def _page_info(total: int, page: int, page_size: int) -> dict[str, Any]:
    pages = (total + page_size - 1) // page_size
    start = (page - 1) * page_size
    return {"page": page, "page_size": page_size, "total": total, "pages": pages,
            "next_page": page + 1 if page < pages else None,
            "truncated": max(0, total - min(total, start + page_size))}


def _continuation_guard(args: Mapping[str, Any], *, page: int = 1,
                        file_page: int = 1, member_page: int = 1,
                        patch_offset: int = 0) -> None:
    """Require both store and snapshot preconditions for continuation reads."""
    if page <= 1 and file_page <= 1 and member_page <= 1 and patch_offset <= 0:
        return
    if args.get("expected_store_version") is None or args.get("expected_snapshot_version") is None:
        raise _error(
            409,
            "snapshot_required",
            "continuation reads require expected_store_version and expected_snapshot_version",
            retryable=True,
            context={"required": ["expected_store_version", "expected_snapshot_version"]},
        )


def _envelope(ctx: _Context, data: Any, *, page: Mapping[str, Any] | None = None,
              evidence: Mapping[str, Any] | None = None) -> Envelope:
    result: Envelope = {"ok": True, "data": data, "context": ctx.payload()}
    if page is not None: result["page"] = dict(page)
    result["evidence"] = dict(evidence or {"complete": True, "reasons": []})
    return result


def _evidence_status(complete: bool, reasons: list[str]) -> dict[str, Any]:
    unique: list[str] = []
    for reason in reasons:
        if reason and reason not in unique and len(unique) < MAX_REASON_CODES: unique.append(reason)
    return {"complete": bool(complete and not unique), "reasons": unique}


def _snapshot_id(data: Mapping[str, Any], source: str) -> str:
    if source != "github": return ""
    values = {str(raw.get("cache_snapshot_id") or "") for raw in data.get("last_prs") or [] if isinstance(raw, dict)}
    return next(iter(values)) if len(values) == 1 and next(iter(values), "") else ""


def _empty_context_data(repo: str = "") -> dict[str, Any]:
    """Return a store-shaped in-memory projection without touching the FS."""
    data = dict(store._empty_store())
    data["repo"] = repo
    return data


def _context(path: Path, args: Mapping[str, Any], *, require_repo: bool = True,
             allow_missing: bool = False, default_repo: str = "") -> _Context:
    requested = _repo(args.get("repo"), required=require_repo) if require_repo else (
        _repo(args.get("repo"), required=False) if "repo" in args else ""
    )
    if not requested and default_repo:
        requested = _repo(default_repo, required=True)

    path = Path(path)
    # ``store.load_store`` deliberately creates its parent and sidecar lock,
    # even for a read of a missing file.  A workspace selection must remain a
    # pure lookup, so get_workspace can opt into an in-memory empty context.
    missing = not path.exists() and not path.is_symlink()
    if missing and allow_missing:
        data = _empty_context_data(requested)
    elif missing and require_repo:
        raise _error(409, "repository_unset", "this store has no active repository")
    else:
        data = store.load_store(path)
    active = _repo(data.get("repo"), required=False)
    if missing and allow_missing and requested:
        active = requested
    if require_repo and not active:
        raise _error(409, "repository_unset", "this store has no active repository")
    if requested and active and requested != active:
        raise _error(409, "repository_conflict", "repository does not match this store",
                     context={"repo": active})
    if requested and not active and require_repo:
        raise _error(409, "repository_unset", "this store has no active repository")
    for name in ("expected_store_version", "expected_snapshot_version"):
        if name in args and args[name] is not None: _nonnegative(args[name], name)
    store_version = _nonnegative(data.get("store_version", 0), "store_version")
    snapshot_version = _nonnegative(data.get("snapshot_version", 0), "snapshot_version")
    if args.get("expected_store_version") is not None and args["expected_store_version"] != store_version:
        raise _error(409, "stale_store_version", "store changed; retry this read", retryable=True,
                     context={"repo": active, "store_version": store_version, "snapshot_version": snapshot_version})
    if args.get("expected_snapshot_version") is not None and args["expected_snapshot_version"] != snapshot_version:
        raise _error(409, "stale_snapshot_version", "repository snapshot changed; retry this read", retryable=True,
                     context={"repo": active, "store_version": store_version, "snapshot_version": snapshot_version})
    source = str(data.get("source") or "").strip().lower()
    source = "github" if source in {"gh", "github"} else source
    return _Context(data, active, source, store_version, snapshot_version, _snapshot_id(data, source))


def _state(ctx: _Context) -> dict[str, Any]:
    builder = getattr(store, "ui_state_from_data", None)
    if not callable(builder):
        # The projection must be made by store.py so all consumers share its
        # fail-closed provenance and revision checks.  Never resurrect the
        # pre-extraction projection here as a compatibility fallback.
        raise _error(500, "projection_unavailable", "store UI projection is unavailable")
    return builder(ctx.data)


def _stored_pr(ctx: _Context, number: int) -> dict[str, Any]:
    matches = [raw for raw in ctx.data.get("last_prs") or []
               if isinstance(raw, dict) and raw.get("number") == number]
    if len(matches) != 1:
        raise _error(404, "not_found", "PR is not in the active store")
    return matches[0]


def _candidate(number: int, meta: Mapping[str, Any], files: list[dict[str, Any]],
               source: str, stored: Mapping[str, Any]) -> PullRequest:
    merged = {**stored, **meta}
    return PullRequest(number=number, title=str(merged.get("title") or ""),
        body=str(merged.get("body") or ""), user=str(merged.get("user") or ""),
        changed_files=[ChangedFile.from_dict(item) for item in files],
        created_at=str(merged.get("created_at") or ""), html_url=str(merged.get("html_url") or ""),
        head_sha=str(merged.get("head_sha") or ""), base_sha=str(merged.get("base_sha") or ""),
        updated_at=str(merged.get("updated_at") or ""), additions=merged.get("additions"),
        deletions=merged.get("deletions"), evidence_complete=bool(merged.get("evidence_complete")),
        evidence_source=source,
        cache_snapshot_id=str(merged.get("cache_snapshot_id") or ""))


def _bundle(ctx: _Context, number: int) -> EvidenceBundle:
    stored = _stored_pr(ctx, number)
    source, files, snapshot, legacy = ctx.source, [], "", False
    # ``display_meta`` may retain the store's bounded title/body when a cache
    # miss leaves us with an incomplete result.  ``evidence_meta`` is kept
    # separate so persisted metadata cannot make a malformed cache response
    # appear revision-complete.
    display_meta: dict[str, Any] = dict(stored)
    evidence_meta: dict[str, Any] = {}
    evidence_loaded = source == "fixtures"
    if source == "fixtures":
        raw_files = stored.get("files") or []
        if not isinstance(raw_files, list) or any(not isinstance(item, dict) for item in raw_files):
            raise _error(409, "revision_conflict", "persisted evidence files are malformed")
        files = [dict(item) for item in raw_files]
        evidence_meta = dict(stored)
    elif source == "github":
        try:
            owner, name = parse_repo(ctx.repo)
            result = gh.cached_pr_evidence(owner, name, number,
                                           snapshot_id=str(stored.get("cache_snapshot_id") or ""))
        except (OSError, ValueError, TypeError, json.JSONDecodeError, gh.GhError) as exc:
            raise _error(409, "revision_conflict", "cached evidence is invalid") from exc
        if result:
            if not isinstance(result, Mapping):
                raise _error(409, "revision_conflict", "cached evidence is invalid")
            raw_meta = result.get("meta")
            if not isinstance(raw_meta, Mapping):
                raise _error(409, "revision_conflict", "cached evidence metadata is invalid")
            raw_number = raw_meta.get("number")
            if type(raw_number) is not int or raw_number != number:
                raise _error(409, "membership_conflict", "cached evidence PR number does not match the requested PR")
            legacy = result.get("legacy_unverified") is True
            raw_repo = raw_meta.get("repository")
            if (not isinstance(raw_repo, str) or not raw_repo.strip()) and not legacy:
                raise _error(409, "revision_conflict", "cached evidence has no repository identity")
            if isinstance(raw_repo, str) and raw_repo.strip():
                try:
                    returned_repo = _repo(raw_repo, required=True)
                except ServiceError as exc:
                    raise _error(409, "revision_conflict", "cached evidence repository is invalid") from exc
                if returned_repo != ctx.repo:
                    raise _error(409, "revision_conflict", "cached evidence repository does not match the active repository")
            returned_source = result.get(
                "source", raw_meta.get("source", raw_meta.get("evidence_source", "github"))
            )
            if not isinstance(returned_source, str) or returned_source.strip().lower() not in {"gh", "github"}:
                raise _error(409, "revision_conflict", "cached evidence source does not match the active store")
            snapshot = result.get("snapshot_id")
            if not isinstance(snapshot, str):
                raise _error(409, "snapshot_conflict", "cached evidence snapshot identity is invalid")
            metadata_snapshot = raw_meta.get("snapshot_id", "")
            if metadata_snapshot is not None and not isinstance(metadata_snapshot, str):
                raise _error(409, "snapshot_conflict", "cached evidence snapshot identity is invalid")
            expected = str(stored.get("cache_snapshot_id") or "")
            if expected and (snapshot != expected or str(metadata_snapshot or "") != expected or legacy):
                raise _error(409, "snapshot_conflict", "cached evidence snapshot does not match the triage snapshot")
            if not expected and (snapshot or metadata_snapshot or not legacy):
                raise _error(409, "snapshot_conflict", "cached evidence has no exact snapshot identity")
            evidence_meta = dict(raw_meta)
            display_meta = {**stored, **evidence_meta}
            raw_files = result.get("files")
            if not isinstance(raw_files, list) or any(not isinstance(item, dict) for item in raw_files):
                raise _error(409, "revision_conflict", "cached evidence files are malformed")
            declared_count = raw_meta.get("file_count", raw_meta.get("changed_files_count"))
            if declared_count is not None and (
                isinstance(declared_count, bool) or not isinstance(declared_count, int)
                or declared_count != len(raw_files)
            ):
                raise _error(409, "revision_conflict", "cached evidence file count is inconsistent")
            for item in raw_files:
                if "patch" in item and not isinstance(item.get("patch"), str):
                    raise _error(409, "revision_conflict", "cached evidence patch is malformed")
            files = [dict(item) for item in raw_files]
            evidence_loaded = True
        else:
            legacy = not bool(stored.get("cache_snapshot_id"))
    elif source:
        raw_files = stored.get("files") or []
        if not isinstance(raw_files, list) or any(not isinstance(item, dict) for item in raw_files):
            raise _error(409, "revision_conflict", "persisted evidence files are malformed")
        files = [dict(item) for item in raw_files]
        evidence_meta = dict(stored)
    else:
        raise _error(409, "invalid_source", "store source is unsupported")

    for item in files:
        if "patch" in item and not isinstance(item.get("patch"), str):
            raise _error(409, "revision_conflict", "evidence patch is malformed")

    # A GitHub cache result must stand on its own.  Stored metadata is not an
    # evidence source and must not fill missing head/base/completeness fields.
    try:
        candidate = _candidate(
            number,
            evidence_meta,
            files,
            source,
            stored if source != "github" or not evidence_loaded else {},
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise _error(409, "revision_conflict", "evidence records are malformed") from exc
    if source == "github" and snapshot:
        # The cache helper returns the snapshot separately from metadata; bind
        # it explicitly so the revision identity cannot silently lose it.
        candidate.cache_snapshot_id = snapshot
    revision = candidate.revision_evidence()
    expected_digest = str(stored.get("content_digest") or "")
    if expected_digest and evidence_loaded and files and revision.content_digest != expected_digest:
        raise _error(409, "revision_conflict", "evidence does not match the stored PR revision")
    for field in ("head_sha", "base_sha"):
        expected, actual = str(stored.get(field) or ""), str(getattr(revision, field) or "")
        if expected and actual and expected != actual:
            raise _error(409, "revision_conflict", "evidence does not match the stored PR revision")
    expected_updated, actual_updated = str(stored.get("updated_at") or ""), str(candidate.updated_at or "")
    if expected_updated and actual_updated and expected_updated != actual_updated:
        raise _error(409, "revision_conflict", "evidence does not match the stored PR revision")
    reasons: list[str] = []
    if legacy: reasons.append("legacy_unverified")
    if source == "github" and not snapshot: reasons.append("missing_snapshot")
    if source == "github" and (not revision.head_sha or not revision.base_sha):
        reasons.append("missing_revision")
    if not files: reasons.append("patch_unavailable")
    if any(not str(item.get("patch") or "") for item in files): reasons.append("patch_unavailable")
    if any(item.get("patch_complete") is not True for item in files): reasons.append("malformed_patch")
    if bool(evidence_meta.get("files_cap_reached")): reasons.append("provider_file_cap")
    if source == "github" and not expected_digest: reasons.append("revision_conflict")
    if not revision.evidence_complete: reasons.append("missing_revision")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return EvidenceBundle(number, display_meta, tuple(files), revision, snapshot,
                          revision.evidence_complete and not legacy and not unique_reasons,
                          unique_reasons, legacy)


def _file(bundle: EvidenceBundle, path: str) -> dict[str, Any] | None:
    exact = next((item for item in bundle.files if item.get("path") == path), None)
    if exact is not None: return exact
    return next((item for item in bundle.files if item.get("previous_path") == path), None)


def _file_complete(item: Mapping[str, Any] | None) -> bool:
    if not item: return False
    patch = item.get("patch")
    if not isinstance(patch, str) or not patch or item.get("patch_complete") is not True: return False
    adds, dels = unified_patch_line_counts(patch)
    return adds is not None and dels is not None and (item.get("additions") is None or item.get("additions") == adds) and (item.get("deletions") is None or item.get("deletions") == dels)


def _file_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    patch = item.get("patch")
    return {"path": str(item.get("path") or ""), "previous_path": str(item.get("previous_path") or ""),
            "status": str(item.get("status") or ""), "additions": item.get("additions"),
            "deletions": item.get("deletions"), "patch_complete": item.get("patch_complete") is True,
            "patch_available": isinstance(patch, str) and bool(patch)}


def _patch_chunk(patch: str, offset: int, limit: int) -> tuple[str, int, int, int | None]:
    """Return a UTF-8-safe chunk using byte offsets and a byte limit."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not MIN_PATCH_CHUNK <= limit <= MAX_PATCH_CHUNK:
        raise _error(400, "invalid_patch_limit", "patch_limit must be between 4 and 16384 bytes")
    encoded = patch.encode("utf-8")
    total = len(encoded)
    start = min(offset, total)
    # Callers normally pass a continuation returned by this function.  If a
    # hand-written client points into a code point, advance to the next valid
    # boundary instead of returning replacement characters.
    while start < total and start > 0 and (encoded[start] & 0xC0) == 0x80:
        start += 1
    end = min(total, start + limit)
    while end > start and end < total and (encoded[end] & 0xC0) == 0x80:
        end -= 1
    # With a four-byte minimum a complete UTF-8 code point always fits.  If a
    # caller points into the middle of one, ``start`` was advanced above, so
    # no byte can be silently skipped between continuation requests.
    text = encoded[start:end].decode("utf-8")
    return text, start, total, end if end < total else None


def _patch_limit(value: Any = None) -> int:
    if value is None:
        return MAX_PATCH_CHUNK
    result = _positive(value, "patch_limit", MAX_PATCH_CHUNK)
    if result < MIN_PATCH_CHUNK:
        raise _error(
            400,
            "invalid_patch_limit",
            "patch_limit must be between 4 and 16384 bytes",
        )
    return result


def _pr_data(bundle: EvidenceBundle, stored: Mapping[str, Any], *, file_page: int = 1,
             file_page_size: int = MAX_PAGE) -> dict[str, Any]:
    meta = bundle.meta
    file_rows = [_file_summary(item) for item in bundle.files]
    file_start = (file_page - 1) * file_page_size
    file_page_rows = file_rows[file_start:file_start + file_page_size]
    file_pages = _page_info(len(file_rows), file_page, file_page_size)
    out = {"number": bundle.number, "title": str(meta.get("title") or "")[:512],
           "body": str(meta.get("body") or "")[:24_000], "user": str(meta.get("user") or "")[:256],
           "html_url": str(meta.get("html_url") or "")[:2048], "created_at": str(meta.get("created_at") or ""),
           "updated_at": str(meta.get("updated_at") or ""), "head_sha": bundle.revision.head_sha,
           "base_sha": bundle.revision.base_sha, "content_digest": bundle.revision.content_digest,
           "evidence_source": bundle.revision.source, "cache_snapshot_id": bundle.snapshot_id,
           "evidence_complete": bundle.complete, "legacy_unverified": bundle.legacy_unverified,
           "revision": bundle.revision.to_dict(), "files": file_page_rows,
           "file_count": len(file_rows), "file_page": file_page, "file_page_size": file_page_size,
           "file_pages": file_pages["pages"], "next_file_page": file_pages["next_page"],
           "files_truncated": file_pages["truncated"]}
    if "group_id" in stored: out["group_id"] = stored.get("group_id") or ""
    if "label" in stored: out["label"] = stored.get("label") or "needs-human"
    return out


def _group_rows(ctx: _Context) -> list[dict[str, Any]]:
    return [raw for raw in ctx.data.get("last_groups") or [] if isinstance(raw, dict)]


def _rule_map(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(raw.get("group_id")): raw for raw in state.get("rules") or [] if isinstance(raw, dict)}


def _group_match(group: Mapping[str, Any], state: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    gid = str(group.get("group_id") or "")
    prs = [int(number) for number in group.get("pr_numbers") or []]
    pr_map = {int(raw.get("number")): raw for raw in state.get("prs") or [] if isinstance(raw, dict) and raw.get("number") is not None}
    rule = _rule_map(state).get(gid)
    local_values = {str((pr_map.get(n) or {}).get("disposition") or "") for n in prs}
    pile = filters.get("pile")
    if pile:
        if pile == "hotspots":
            hot = {str(item.get("path")) for item in (state.get("queue") or {}).get("hotspots") or []}
            if not any(path in hot for raw in (pr_map[n] for n in prs if n in pr_map) for path in raw.get("paths") or []): return False
        elif gid not in (state.get("queue") or {}).get(pile, []): return False
    label = filters.get("label")
    decision = (rule or {}).get("decision")
    card = group.get("card_class") or "needs-look"
    suggested = group.get("suggested_decision") or "unique"
    if label == "unreviewed" and (rule or any(value != "pending" for value in local_values)): return False
    if label == "approved" and not (decision == "approve" or "keep" in local_values or gid in (state.get("queue") or {}).get("known", [])): return False
    if label in {"rejected", "junk"} and not (decision == "reject" or "reject" in local_values or gid in (state.get("queue") or {}).get("junk", []) or (label == "junk" and card == "junk")): return False
    if label in {"hardware", "upgrade"} and not (decision == label or ("needs_hardware" if label == "hardware" else "upgrade") in local_values or card == label): return False
    if label in {"unique", "duplicate", "related-theme"} and suggested != label: return False
    if label and label not in {"unreviewed", "approved", "rejected", "junk", "hardware", "upgrade", "unique", "duplicate", "related-theme"} and card != label: return False
    if filters.get("decision"):
        wanted = filters["decision"]
        local_values = {str(pr_map[n].get("disposition") or "") for n in prs if n in pr_map}
        local_aliases = {
            "approve": "keep", "reject": "reject", "hardware": "needs_hardware",
            "upgrade": "upgrade",
        }
        if decision != wanted and local_aliases.get(wanted) not in local_values:
            return False
    if filters.get("min_pr_count", 0) > len(prs): return False
    if filters.get("group_id") and gid != filters["group_id"]: return False
    if filters.get("pr") is not None and filters["pr"] not in prs: return False
    if filters.get("user") and not any(
        str((pr_map.get(number) or {}).get("user") or "") == filters["user"]
        for number in prs
    ):
        return False
    if filters.get("path") and not any(filters["path"] in (pr_map[n].get("paths") or []) for n in prs if n in pr_map): return False
    q = str(filters.get("q") or "").strip().casefold()
    if q:
        hay = [gid, *[str(v) for v in group.get("title_variants") or []]]
        for number in prs:
            raw = pr_map.get(number) or {}
            hay += [str(number), str(raw.get("title") or ""), str(raw.get("user") or ""), *[str(v) for v in raw.get("paths") or []]]
        if q.startswith("#") and q[1:].isdigit():
            if int(q[1:]) not in prs: return False
        elif q.isdigit() and int(q) not in prs: return False
        elif not any(q in text.casefold() for text in hay): return False
    return True


def _pr_match(raw: Mapping[str, Any], state: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    number = int(raw.get("number") or 0)
    if filters.get("pr") is not None and number != filters["pr"]: return False
    if filters.get("group_id") and raw.get("group_id") != filters["group_id"]: return False
    if filters.get("path") and filters["path"] not in (raw.get("paths") or []): return False
    if filters.get("user") and str(raw.get("user") or "") != filters["user"]: return False
    group = next((g for g in state.get("groups") or [] if g.get("group_id") == raw.get("group_id")), None)
    if filters.get("label") or filters.get("pile") or filters.get("decision") or filters.get("min_pr_count"):
        if not group or not _group_match(group, state, filters): return False
    q = str(filters.get("q") or "").strip().casefold()
    if q:
        if q.startswith("#") and q[1:].isdigit():
            if int(q[1:]) != number:
                return False
        elif q.isdigit():
            if int(q) != number:
                return False
        elif not any(q in str(value or "").casefold() for value in [raw.get("title"), raw.get("user"), raw.get("group_id"), *(raw.get("paths") or [])]):
            return False
    return True


class WorkspaceService:
    def __init__(self, store_path: Path) -> None:
        self.store_path = Path(store_path)

    def draft_proposal(self, request: ProposalRequest, *, actor: str = "agent") -> Envelope:
        """Create an agent draft through the sole store writer."""
        if not isinstance(request, ProposalRequest):
            raise _error(400, "invalid_request", "request must be ProposalRequest")
        try:
            proposal = store.draft_proposal(
                repo=request.repo, group_id=request.group_id,
                items=list(request.items), canonical_pr=request.canonical_pr,
                path=self.store_path,
                expected_version=request.expected_store_version,
                expected_snapshot_version=request.expected_snapshot_version,
                idempotency_key=request.idempotency_key, actor=actor,
                provenance=request.provenance, context=request.context,
            )
            # A fresh read gives the response the post-write context without
            # asking the mutator to recursively acquire the store lock.
            ctx = _context(self.store_path, {"repo": request.repo})
            return _envelope(ctx, {"proposal": proposal.to_dict()})
        except store.StoreConflictError as exc:
            raise _error(409, exc.code, str(exc), retryable=True,
                         context={"repo": exc.current_repo, "store_version": exc.current_version}) from exc
        except store.IncompleteEvidenceError as exc:
            raise _error(422, "incomplete_evidence", str(exc)) from exc
        except (store.StoreError, ValueError, KeyError, TypeError) as exc:
            raise _error(400, "invalid_request", str(exc)) from exc

    def inspect_proposal(self, proposal_id: str, *, repo: str | None = None) -> Envelope:
        try:
            proposal = store.inspect_proposal(proposal_id, path=self.store_path, repo=repo)
            ctx = _context(self.store_path, {"repo": repo} if repo is not None else {}, require_repo=False)
            return _envelope(ctx, {"proposal": proposal.to_dict()})
        except store.StoreConflictError as exc:
            raise _error(409, exc.code, str(exc), retryable=True,
                         context={"repo": exc.current_repo, "store_version": exc.current_version}) from exc
        except (store.StoreError, ValueError, KeyError, TypeError) as exc:
            raise _error(404 if isinstance(exc, KeyError) else 400, "not_found" if isinstance(exc, KeyError) else "invalid_request", str(exc)) from exc

    def read(self, operation: str, args: Mapping[str, Any]) -> Envelope:
        if not isinstance(operation, str) or operation not in _READ_SET: raise _error(400, "invalid_operation", "operation is not allowlisted")
        if not isinstance(args, Mapping): raise _error(400, "invalid_request", "args must be an object")
        common = {"repo", "expected_store_version", "expected_snapshot_version"}
        per_operation = {
            "get_workspace": set(), "list_groups": {"page", "page_size", "filters"},
            "search_prs": {"page", "page_size", "filters"}, "get_group": {"group_id", "member_page", "member_page_size"},
            "get_pr": {"pr", "file_page", "file_page_size"}, "read_patch": {"pr", "path", "patch_offset", "patch_limit"},
            "compare_prs": {"path", "group_id", "prs", "page", "page_size", "patch_offset", "patch_limit"},
            "find_related": {"pr", "path", "limit"}, "get_history": {"page", "page_size", "filters"},
        }
        unknown = set(args) - common - per_operation[operation]
        if unknown: raise _error(400, "unknown_parameter", f"unknown parameters: {', '.join(sorted(unknown))}")
        if operation != "get_workspace" and "repo" not in args:
            raise _error(400, "missing_fields", "missing fields: repo")
        ctx = _context(
            self.store_path,
            args,
            require_repo=operation != "get_workspace",
            allow_missing=operation == "get_workspace",
        )
        if operation == "get_workspace": return self._workspace(ctx)
        if operation == "list_groups": return self._groups(ctx, args)
        if operation == "search_prs": return self._prs(ctx, args)
        if operation == "get_group": return self._group(ctx, args)
        if operation == "get_pr": return self._pr(ctx, args)
        if operation == "read_patch": return self._patch(ctx, args)
        if operation == "compare_prs": return self._compare(ctx, args)
        if operation == "find_related": return self._related(ctx, args)
        return self._history(ctx, args)

    def read_pr_evidence(self, repo: str, pr: int) -> EvidenceBundle:
        args = {"repo": repo}
        ctx = _context(self.store_path, args)
        return _bundle(ctx, _positive(pr, "pr"))

    def _workspace(self, ctx: _Context) -> Envelope:
        state = _state(ctx)
        prs = state.get("prs") or []
        incomplete = [raw for raw in prs if raw.get("evidence_complete") is not True]
        pending = [raw for raw in prs if raw.get("disposition", "pending") == "pending"]
        data = {"repo": ctx.repo, "source": ctx.source, "group_count": len(state.get("groups") or []),
                "pr_count": len(prs), "queue": state.get("queue") or {},
                "new_pr_numbers": state.get("new_pr_numbers") or [],
                "sync": state.get("sync"), "incomplete_pr_count": len(incomplete),
                "pending_pr_count": len(pending),
                "disposition_count": len(state.get("dispositions") or []),
                "proposal_count": len(state.get("proposals") or [])}
        return _envelope(ctx, data, evidence=_evidence_status(not incomplete, ["missing_revision"] if incomplete else []))

    def _groups(self, ctx: _Context, args: Mapping[str, Any]) -> Envelope:
        page, size = _page(args.get("page"), "page", 1, MAX_INT), _page(args.get("page_size"), "page_size", 40, MAX_PAGE)
        _continuation_guard(args, page=page)
        filters = _filters(args.get("filters"), groups=True)
        state = _state(ctx)
        rows = [raw for raw in state.get("groups") or [] if _group_match(raw, state, filters)]
        if filters.get("sort") == "recent":
            pr_map = {int(raw.get("number")): raw for raw in state.get("prs") or [] if raw.get("number") is not None}
            rows.sort(key=lambda raw: max((str(pr_map.get(n, {}).get("updated_at") or "") for n in raw.get("pr_numbers") or []), default=""), reverse=True)
        else:
            rows.sort(key=lambda raw: (-len(raw.get("pr_numbers") or []), str(raw.get("group_id") or "")))
        start = (page - 1) * size
        selected = rows[start:start + size]
        incomplete = [raw for raw in selected if raw.get("evidence_complete") is not True]
        return _envelope(ctx, {"groups": selected}, page=_page_info(len(rows), page, size),
                         evidence=_evidence_status(not incomplete, ["missing_revision"] if incomplete else []))

    def _prs(self, ctx: _Context, args: Mapping[str, Any]) -> Envelope:
        page, size = _page(args.get("page"), "page", 1, MAX_INT), _page(args.get("page_size"), "page_size", 40, MAX_PAGE)
        _continuation_guard(args, page=page)
        filters = _filters(args.get("filters"))
        state = _state(ctx)
        rows = [raw for raw in state.get("prs") or [] if _pr_match(raw, state, filters)]
        rows.sort(key=lambda raw: -int(raw.get("number") or 0))
        start = (page - 1) * size
        selected = rows[start:start + size]
        return _envelope(ctx, {"prs": selected}, page=_page_info(len(rows), page, size),
                         evidence=_evidence_status(all(raw.get("evidence_complete") is True for raw in selected), ["missing_revision"] if any(raw.get("evidence_complete") is not True for raw in selected) else []))

    def _group(self, ctx: _Context, args: Mapping[str, Any]) -> Envelope:
        gid = args.get("group_id")
        if not isinstance(gid, str) or not GROUP_ID.fullmatch(gid): raise _error(400, "invalid_group", "group_id is invalid")
        raw = next((item for item in _group_rows(ctx) if item.get("group_id") == gid), None)
        if raw is None: raise _error(404, "not_found", "group is not in the active store")
        group = Group.from_dict(raw)
        revisions, digest, complete = list(group.member_revisions), group.snapshot_digest, group.evidence_complete
        try:
            verifier = getattr(store, "_verified_group_snapshot")
            match, revisions, digest, complete = verifier(ctx.data, ctx.repo, gid)
            group = match
        except store.IncompleteEvidenceError:
            # Preserve the group for navigation, but never leak its persisted
            # complete flag after verification fails closed.
            complete = False
        except (store.StoreError, KeyError, TypeError, ValueError) as exc:
            raise _error(409, "revision_conflict", "group evidence does not match the active snapshot") from exc
        payload = group.to_dict()
        member_page = _page(args.get("member_page"), "member_page", 1, MAX_INT)
        member_page_size = _page(args.get("member_page_size"), "member_page_size", 80, MAX_PAGE)
        _continuation_guard(args, member_page=member_page)
        member_info = _page_info(len(revisions), member_page, member_page_size)
        member_start = (member_page - 1) * member_page_size
        revision_rows = revisions[member_start:member_start + member_page_size]
        payload["member_numbers"] = list(group.pr_numbers[member_start:member_start + member_page_size])
        payload["member_count"] = len(group.pr_numbers)
        payload["member_page"] = member_page
        payload["member_page_size"] = member_page_size
        payload["next_member_page"] = member_info["next_page"]
        payload["revision_refs"] = [item.to_dict() for item in revision_rows]
        payload["member_revisions"] = payload["revision_refs"]
        payload["revision_ref_count"] = len(revisions)
        payload["revision_ref_page"] = member_page
        payload["revision_ref_page_size"] = member_page_size
        payload["revision_ref_pages"] = member_info["pages"]
        payload["next_revision_ref_page"] = member_info["next_page"]
        payload["members_truncated"] = member_info["truncated"]
        payload["snapshot_digest"] = digest
        payload["evidence_complete"] = complete
        state = _state(ctx)
        payload["dispositions"] = {
            str(number): next(
                (row for row in state.get("prs") or []
                 if row.get("number") == number and row.get("group_id") == gid),
                {"number": number, "disposition": "pending", "disposition_status": "pending"},
            )
            for number in group.pr_numbers
        }
        reasons = [] if complete else ["missing_revision"]
        return _envelope(ctx, payload, evidence=_evidence_status(complete, reasons))

    def _pr(self, ctx: _Context, args: Mapping[str, Any]) -> Envelope:
        file_page = _page(args.get("file_page"), "file_page", 1, MAX_INT)
        file_page_size = _page(args.get("file_page_size"), "file_page_size", 80, MAX_PAGE)
        _continuation_guard(args, file_page=file_page)
        bundle = _bundle(ctx, _positive(args.get("pr"), "pr"))
        payload = _pr_data(bundle, _stored_pr(ctx, bundle.number), file_page=file_page,
                           file_page_size=file_page_size)
        state = _state(ctx)
        state_pr = next((row for row in state.get("prs") or []
                         if row.get("number") == bundle.number), None)
        if isinstance(state_pr, Mapping):
            for key in ("disposition", "disposition_status", "disposition_reason", "duplicate_of", "disposition_revision"):
                if key in state_pr:
                    payload[key] = state_pr[key]
        return _envelope(ctx, payload, evidence=_evidence_status(bundle.complete, list(bundle.reasons)))

    def _patch(self, ctx: _Context, args: Mapping[str, Any]) -> Envelope:
        number, path = _positive(args.get("pr"), "pr"), _path(args.get("path"))
        offset = _nonnegative(args.get("patch_offset", 0), "patch_offset", MAX_PATCH_OFFSET)
        limit = _patch_limit(args.get("patch_limit"))
        _continuation_guard(args, patch_offset=offset)
        bundle = _bundle(ctx, number)
        record = _file(bundle, path)
        patch = str((record or {}).get("patch") or "")
        complete = bundle.complete and _file_complete(record)
        chunk, actual_offset, patch_length, next_offset = _patch_chunk(patch, offset, limit)
        data = {"repo": ctx.repo, "pr": number, "path": path, "patch": chunk,
                "patch_offset": actual_offset, "patch_length": patch_length,
                "next_patch_offset": next_offset,
                "preview_truncated": actual_offset > 0 or next_offset is not None,
                "source_complete": complete, "evidence_complete": complete,
                "content_sha256": hashlib.sha256(patch.encode()).hexdigest() if complete else None,
                "head_sha": bundle.revision.head_sha, "base_sha": bundle.revision.base_sha,
                "snapshot_id": bundle.snapshot_id,
                "incomplete_reasons": list(bundle.reasons) + ([] if record else ["patch_unavailable"])}
        return _envelope(ctx, data, evidence=_evidence_status(complete, data["incomplete_reasons"]))

    def _compare(self, ctx: _Context, args: Mapping[str, Any]) -> Envelope:
        path = _path(args.get("path"))
        gid, prs = args.get("group_id"), args.get("prs")
        if bool(gid) == bool(prs): raise _error(400, "target_required", "provide exactly one of group_id or prs")
        if gid:
            if not isinstance(gid, str) or not GROUP_ID.fullmatch(gid): raise _error(400, "invalid_group", "group_id is invalid")
            raw = next((item for item in _group_rows(ctx) if item.get("group_id") == gid), None)
            if raw is None: raise _error(404, "not_found", "group is not in the active store")
            numbers = [int(number) for number in raw.get("pr_numbers") or []]
        else:
            if not isinstance(prs, list) or not prs or len(prs) > MAX_PR_LIST: raise _error(400, "invalid_prs", "prs must be a bounded list")
            numbers = [_positive(value, "pr") for value in prs]
            if len(set(numbers)) != len(numbers): raise _error(400, "invalid_prs", "prs must be unique")
        page, size = _page(args.get("page"), "page", 1, MAX_INT), _page(args.get("page_size"), "page_size", 8, 8)
        offset, limit = _nonnegative(args.get("patch_offset", 0), "patch_offset", MAX_PATCH_OFFSET), _patch_limit(args.get("patch_limit"))
        _continuation_guard(args, page=page, patch_offset=offset)
        start = (page - 1) * size
        selected = numbers[start:start + size]
        items, complete_flags, hashes, reasons = [], [], [], []
        for number in selected:
            bundle = _bundle(ctx, number)
            record, patch = _file(bundle, path), ""
            if record: patch = str(record.get("patch") or "")
            complete = bundle.complete and _file_complete(record)
            chunk, actual_offset, patch_length, next_offset = _patch_chunk(patch, offset, limit)
            item_reasons = list(bundle.reasons) + ([] if record else ["patch_unavailable"])
            if not complete and "patch_unavailable" not in item_reasons and record and not _file_complete(record): item_reasons.append("malformed_patch")
            items.append({"number": number, "path": path, "patch": chunk, "patch_offset": actual_offset,
                          "patch_length": patch_length, "next_patch_offset": next_offset,
                          "preview_truncated": actual_offset > 0 or next_offset is not None, "source_complete": complete,
                          "evidence_complete": complete, "content_sha256": hashlib.sha256(patch.encode()).hexdigest() if complete else None,
                          "head_sha": bundle.revision.head_sha, "base_sha": bundle.revision.base_sha,
                          "snapshot_id": bundle.snapshot_id, "incomplete_reasons": item_reasons})
            complete_flags.append(complete)
            reasons.extend(item_reasons)
            if complete: hashes.append(hashlib.sha256(patch.encode()).hexdigest())
        same = len(selected) >= 2 and all(complete_flags) and len(set(hashes)) == 1
        different = len(selected) >= 2 and all(complete_flags) and len(set(hashes)) > 1
        status = "same" if same else "different" if different else "unknown"
        data = {"repo": ctx.repo, "path": path, "items": items,
                "comparison": {"status": status, "complete": bool(selected) and all(complete_flags),
                                "same_complete_patch": same if len(selected) >= 2 and all(complete_flags) else None,
                                "scope": "page"}}
        return _envelope(ctx, data, page=_page_info(len(numbers), page, size), evidence=_evidence_status(bool(selected) and all(complete_flags), reasons))

    def _related(self, ctx: _Context, args: Mapping[str, Any]) -> Envelope:
        number = _positive(args.get("pr"), "pr")
        _stored_pr(ctx, number)
        path = _path(args["path"]) if args.get("path") is not None else None
        limit = _positive(args.get("limit", 5), "limit", 20)
        try:
            result = rank.related_cached(number, file_path=path, store_path=self.store_path, k=limit)
        except (rank.RankError, ValueError, OSError, TypeError) as exc:
            raise _error(409, "related_unavailable", "local related-PR retrieval is unavailable") from exc
        # rank.related_cached owns its legacy helper and therefore performs a
        # separate store load.  Do not return a result built against a context
        # that changed while it was reading.
        try:
            latest = store.load_store(self.store_path)
            latest_repo = _repo(latest.get("repo"), required=False)
            latest_store = _nonnegative(latest.get("store_version", 0), "store_version")
            latest_snapshot = _nonnegative(latest.get("snapshot_version", 0), "snapshot_version")
        except (store.StoreError, OSError, TypeError, ValueError) as exc:
            raise _error(409, "related_unavailable", "local related-PR retrieval is unavailable") from exc
        if latest_repo != ctx.repo:
            raise _error(409, "repository_conflict", "repository changed during related-PR retrieval", retryable=True)
        if latest_store != ctx.store_version:
            raise _error(409, "stale_store_version", "store changed during related-PR retrieval", retryable=True)
        if latest_snapshot != ctx.snapshot_version:
            raise _error(409, "stale_snapshot_version", "repository snapshot changed during related-PR retrieval", retryable=True)
        if not isinstance(result, Mapping):
            raise _error(409, "related_unavailable", "local related-PR retrieval returned invalid data")
        result = dict(result)
        result["advisory"] = True
        result["cache_only"] = True
        return _envelope(ctx, result, evidence={"complete": bool((result.get("evidence") or {}).get("query_complete", False)),
            "reasons": [] if (result.get("evidence") or {}).get("query_complete") else ["missing_revision"]})

    def _history(self, ctx: _Context, args: Mapping[str, Any]) -> Envelope:
        page, size = _page(args.get("page"), "page", 1, MAX_INT), _page(args.get("page_size"), "page_size", 40, MAX_PAGE)
        _continuation_guard(args, page=page)
        filters = _filters(args.get("filters"), history=True)
        rows: list[dict[str, Any]] = []
        if filters.get("include_legacy", True):
            for raw in ctx.data.get("legacy_decision_history") or []:
                if isinstance(raw, dict): rows.append({**raw, "history_kind": "legacy", "evidence_complete": False})
        for raw in ctx.data.get("decision_events") or []:
            if isinstance(raw, dict): rows.append({**raw, "history_kind": "revision-bound"})
        for raw in ctx.data.get("disposition_events") or []:
            if isinstance(raw, dict):
                rows.append({**raw, "history_kind": "disposition", "decision": "disposition"})
        for raw in ctx.data.get("proposal_events") or []:
            if isinstance(raw, dict):
                rows.append({**raw, "history_kind": "proposal", "decided_at": raw.get("at", "")})
        def match(raw: Mapping[str, Any]) -> bool:
            if str(raw.get("repo") or "").strip().strip("/").lower() != ctx.repo: return False
            for key in ("group_id", "decision"):
                if filters.get(key) is not None and raw.get(key) != filters[key]: return False
            event_prs = [int(item.get("pr_number", item.get("pr")) or 0)
                        for item in raw.get("revisions") or [] if isinstance(item, dict)]
            event_prs += [int(item.get("pr_number", item.get("pr")) or 0)
                          for item in raw.get("items") or [] if isinstance(item, dict)]
            if filters.get("pr") is not None and filters["pr"] not in event_prs: return False
            return True
        rows = [raw for raw in rows if match(raw)]
        rows.sort(key=lambda raw: str(raw.get("decided_at") or raw.get("created_at") or ""), reverse=True)
        start = (page - 1) * size
        selected = rows[start:start + size]
        return _envelope(ctx, {"events": selected}, page=_page_info(len(rows), page, size), evidence=_evidence_status(all(raw.get("evidence_complete") is not False for raw in selected), ["legacy_unverified"] if any(raw.get("history_kind") == "legacy" for raw in selected) else []))


def dispatch_read(store_path: Path, operation: str, args: Mapping[str, Any]) -> Envelope:
    """Run one allowlisted read and convert typed failures to an envelope."""
    try:
        return WorkspaceService(store_path).read(operation, args)
    except ServiceError as exc:
        return exc.envelope()
    except store.StoreError as exc:
        return _error(409, "store_unavailable", "the local triage store cannot be read").envelope()
    except OSError as exc:
        return _error(409, "store_unavailable", "the local triage store cannot be read").envelope()
