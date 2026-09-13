"""File-level review records: human file reviews, findings, and agent drafts.

A file review is deliberately *not* a pull-request decision.  Marking a file
reviewed records that one human looked at one exact revision of one
repository-relative path.  It is never approval, it never proves the absence
of problems, and it never changes a PR disposition.  Agent output lives beside
these records as drafts: adopting a drafted finding is a separate, deliberate
human write, and no agent path can mark a file human-reviewed.

``path`` here is an inert repository path.  Nothing in this module or its
callers opens, resolves, or stats it on the local filesystem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from triage.models import RevisionEvidence

FINDING_SEVERITIES = ("blocker", "major", "minor", "nit", "question")
SEVERITY_ORDER = {name: index for index, name in enumerate(FINDING_SEVERITIES)}
FINDING_STATUSES = ("open", "resolved", "dismissed")
COVERAGE_STATUSES = ("inspected", "skipped", "missing")
DRAFT_FINDING_STATUSES = ("pending", "accepted", "dismissed")
REVIEW_SOURCES = ("human", "agent")

MAX_PATH_BYTES = 1024
MAX_TITLE = 200
MAX_TEXT = 4000
MAX_NOTE = 1000
MAX_HUNK = 200
MAX_ACTOR = 256
MAX_LINE = 2_000_000_000
MAX_DRAFT_FINDINGS = 100
MAX_DRAFT_COVERAGE = 500
MAX_ID = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class ReviewValidationError(ValueError):
    """A file-review record is structurally invalid or out of bounds."""


def review_path(value: Any) -> str:
    """Validate an inert repository-relative path; never touch the filesystem."""
    if not isinstance(value, str):
        raise ReviewValidationError("path must be a repository-relative string")
    try:
        raw = value.encode("utf-8", "strict").decode("utf-8")
    except UnicodeError as exc:
        raise ReviewValidationError("path must be valid UTF-8") from exc
    if not raw or len(raw.encode("utf-8")) > MAX_PATH_BYTES:
        raise ReviewValidationError("path must be 1-1024 bytes")
    if raw.startswith("/") or "\x00" in raw:
        raise ReviewValidationError("path must be repository-relative")
    if "\\" in raw:
        raise ReviewValidationError("path must be repository-relative")
    if any(part in {"", ".", ".."} for part in raw.split("/")):
        raise ReviewValidationError("path must be repository-relative")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ReviewValidationError("path must not contain control characters")
    return raw


def review_text(value: Any, name: str, *, limit: int, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ReviewValidationError(f"{name} must be a string")
    result = value.strip()
    if required and not result:
        raise ReviewValidationError(f"{name} is required")
    if len(result) > limit:
        raise ReviewValidationError(f"{name} must be at most {limit} characters")
    if "\x00" in result:
        raise ReviewValidationError(f"{name} must not contain a null character")
    return result


def review_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ReviewValidationError(f"{name} must be 1-128 safe identifier characters")
    return value


def review_actor(value: Any, default: str) -> str:
    raw = value if isinstance(value, str) else default
    result = raw.strip() or default
    if len(result) > MAX_ACTOR:
        raise ReviewValidationError("actor must be at most 256 characters")
    return result


def review_severity(value: Any) -> str:
    if not isinstance(value, str) or value not in FINDING_SEVERITIES:
        raise ReviewValidationError("severity must be blocker|major|minor|nit|question")
    return value


def review_line(value: Any) -> int | None:
    """Accept an optional, strictly positive line number."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewValidationError("line must be a positive integer")
    if not 1 <= value <= MAX_LINE:
        raise ReviewValidationError("line must be a positive integer")
    return value


def _enum(value: Any, allowed: tuple[str, ...], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        joined = "|".join(allowed)
        raise ReviewValidationError(f"{name} must be one of {joined}")
    return value


def _bool(value: Any, *, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReviewValidationError(f"{name} must be a positive integer")
    return value


def _revision(value: Any) -> RevisionEvidence:
    if isinstance(value, RevisionEvidence):
        return value
    if not isinstance(value, Mapping):
        raise ReviewValidationError("revision must be an object")
    return RevisionEvidence.from_dict(dict(value))


def revision_key(revision: RevisionEvidence) -> tuple[str, str, str]:
    """The exact revision identity that a file record is bound to."""
    return (revision.head_sha, revision.base_sha, revision.content_digest)


def same_revision(left: RevisionEvidence, right: RevisionEvidence) -> bool:
    return revision_key(left) == revision_key(right)


@dataclass(frozen=True)
class FileReview:
    """One human statement that an exact file revision was looked at."""

    repo: str
    pr: int
    path: str
    revision: RevisionEvidence
    reviewed: bool
    actor: str = ""
    reviewed_at: str = ""
    event_id: str = ""
    source: str = "human"
    note: str = ""
    file_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "pr": self.pr,
            "path": self.path,
            "revision": self.revision.to_dict(),
            "reviewed": bool(self.reviewed),
            "actor": self.actor,
            "reviewed_at": self.reviewed_at,
            "event_id": self.event_id,
            "source": self.source,
            "note": self.note,
            "file_digest": self.file_digest,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FileReview":
        return cls(
            repo=str(data.get("repo", "") or ""),
            pr=_int(data.get("pr", data.get("pr_number")), "pr"),
            path=review_path(data.get("path")),
            revision=_revision(data.get("revision")),
            reviewed=_bool(data.get("reviewed")),
            actor=str(data.get("actor", "") or ""),
            reviewed_at=str(data.get("reviewed_at", "") or ""),
            event_id=str(data.get("event_id", "") or ""),
            # A file review is a human act by construction.  A persisted row is
            # never trusted to widen that into an agent source.
            source="human",
            note=review_text(data.get("note"), "note", limit=MAX_NOTE),
            file_digest=str(data.get("file_digest", "") or ""),
        )


@dataclass(frozen=True)
class FileCoverage:
    """Explicit agent coverage for one exact file revision.

    Absence of a drafted finding is never inspection.  Only a recorded
    ``inspected`` row means an agent claims to have read that file revision.
    """

    repo: str
    pr: int
    path: str
    revision: RevisionEvidence
    status: str
    note: str = ""
    actor: str = ""
    recorded_at: str = ""
    draft_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "pr": self.pr,
            "path": self.path,
            "revision": self.revision.to_dict(),
            "status": self.status,
            "note": self.note,
            "actor": self.actor,
            "recorded_at": self.recorded_at,
            "draft_id": self.draft_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FileCoverage":
        return cls(
            repo=str(data.get("repo", "") or ""),
            pr=_int(data.get("pr", data.get("pr_number")), "pr"),
            path=review_path(data.get("path")),
            revision=_revision(data.get("revision")),
            status=_enum(data.get("status"), COVERAGE_STATUSES, "coverage status"),
            note=review_text(data.get("note"), "note", limit=MAX_NOTE),
            actor=str(data.get("actor", "") or ""),
            recorded_at=str(data.get("recorded_at", "") or ""),
            draft_id=str(data.get("draft_id", "") or ""),
        )


@dataclass(frozen=True)
class FileFinding:
    """One concrete, addressable concern about an exact file revision."""

    finding_id: str
    repo: str
    pr: int
    path: str
    revision: RevisionEvidence
    severity: str
    title: str
    explanation: str = ""
    evidence: str = ""
    suggested_fix: str = ""
    status: str = "open"
    line: int | None = None
    hunk: str = ""
    author: str = ""
    source: str = "human"
    origin: str = "human"
    draft_id: str = ""
    draft_finding_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    status_actor: str = ""
    status_at: str = ""
    adopted_by: str = ""
    adopted_at: str = ""
    file_digest: str = ""
    about_missing_patch: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "repo": self.repo,
            "pr": self.pr,
            "path": self.path,
            "revision": self.revision.to_dict(),
            "severity": self.severity,
            "title": self.title,
            "explanation": self.explanation,
            "evidence": self.evidence,
            "suggested_fix": self.suggested_fix,
            "status": self.status,
            "line": self.line,
            "hunk": self.hunk,
            "author": self.author,
            "source": self.source,
            "origin": self.origin,
            "draft_id": self.draft_id,
            "draft_finding_id": self.draft_finding_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status_actor": self.status_actor,
            "status_at": self.status_at,
            "adopted_by": self.adopted_by,
            "adopted_at": self.adopted_at,
            "file_digest": self.file_digest,
            "about_missing_patch": bool(self.about_missing_patch),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FileFinding":
        return cls(
            finding_id=review_identifier(data.get("finding_id"), "finding_id"),
            repo=str(data.get("repo", "") or ""),
            pr=_int(data.get("pr", data.get("pr_number")), "pr"),
            path=review_path(data.get("path")),
            revision=_revision(data.get("revision")),
            severity=review_severity(data.get("severity")),
            title=review_text(data.get("title"), "title", limit=MAX_TITLE, required=True),
            explanation=review_text(
                data.get("explanation"), "explanation", limit=MAX_TEXT
            ),
            evidence=review_text(data.get("evidence"), "evidence", limit=MAX_TEXT),
            suggested_fix=review_text(
                data.get("suggested_fix"), "suggested_fix", limit=MAX_TEXT
            ),
            status=_enum(data.get("status", "open"), FINDING_STATUSES, "status"),
            line=review_line(data.get("line")),
            hunk=review_text(data.get("hunk"), "hunk", limit=MAX_HUNK),
            author=str(data.get("author", "") or ""),
            source=_enum(data.get("source", "human"), REVIEW_SOURCES, "source"),
            origin=_enum(
                data.get("origin", "human"), ("human", "agent_draft"), "origin"
            ),
            draft_id=str(data.get("draft_id", "") or ""),
            draft_finding_id=str(data.get("draft_finding_id", "") or ""),
            created_at=str(data.get("created_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
            status_actor=str(data.get("status_actor", "") or ""),
            status_at=str(data.get("status_at", "") or ""),
            adopted_by=str(data.get("adopted_by", "") or ""),
            adopted_at=str(data.get("adopted_at", "") or ""),
            file_digest=str(data.get("file_digest", "") or ""),
            about_missing_patch=_bool(data.get("about_missing_patch")),
        )


@dataclass(frozen=True)
class DraftFinding:
    """One agent-proposed finding awaiting an explicit human decision."""

    draft_finding_id: str
    path: str
    severity: str
    title: str
    explanation: str = ""
    evidence: str = ""
    suggested_fix: str = ""
    line: int | None = None
    hunk: str = ""
    status: str = "pending"
    decided_by: str = ""
    decided_at: str = ""
    adopted_finding_id: str = ""
    about_missing_patch: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_finding_id": self.draft_finding_id,
            "path": self.path,
            "severity": self.severity,
            "title": self.title,
            "explanation": self.explanation,
            "evidence": self.evidence,
            "suggested_fix": self.suggested_fix,
            "line": self.line,
            "hunk": self.hunk,
            "status": self.status,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "adopted_finding_id": self.adopted_finding_id,
            "about_missing_patch": bool(self.about_missing_patch),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DraftFinding":
        return cls(
            draft_finding_id=review_identifier(
                data.get("draft_finding_id"), "draft_finding_id"
            ),
            path=review_path(data.get("path")),
            severity=review_severity(data.get("severity")),
            title=review_text(data.get("title"), "title", limit=MAX_TITLE, required=True),
            explanation=review_text(
                data.get("explanation"), "explanation", limit=MAX_TEXT
            ),
            evidence=review_text(data.get("evidence"), "evidence", limit=MAX_TEXT),
            suggested_fix=review_text(
                data.get("suggested_fix"), "suggested_fix", limit=MAX_TEXT
            ),
            line=review_line(data.get("line")),
            hunk=review_text(data.get("hunk"), "hunk", limit=MAX_HUNK),
            status=_enum(
                data.get("status", "pending"), DRAFT_FINDING_STATUSES, "draft status"
            ),
            decided_by=str(data.get("decided_by", "") or ""),
            decided_at=str(data.get("decided_at", "") or ""),
            adopted_finding_id=str(data.get("adopted_finding_id", "") or ""),
            about_missing_patch=_bool(data.get("about_missing_patch")),
        )


@dataclass(frozen=True)
class FileReviewDraft:
    """A bounded, revision-bound agent file-review proposal.

    A draft carries findings and coverage only.  It can never contain a human
    review, a PR disposition, or an acceptance.
    """

    draft_id: str
    repo: str
    pr: int
    revision: RevisionEvidence
    findings: tuple[DraftFinding, ...]
    coverage_summary: dict[str, int]
    coverage_paths: tuple[str, ...]
    provenance: dict[str, Any]
    context: dict[str, Any]
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "repo": self.repo,
            "pr": self.pr,
            "revision": self.revision.to_dict(),
            "findings": [item.to_dict() for item in self.findings],
            "coverage_summary": dict(self.coverage_summary),
            "coverage_paths": list(self.coverage_paths),
            "provenance": dict(self.provenance),
            "context": dict(self.context),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FileReviewDraft":
        raw_findings = data.get("findings") or []
        if not isinstance(raw_findings, list) or len(raw_findings) > MAX_DRAFT_FINDINGS:
            raise ReviewValidationError("draft findings must be a bounded list")
        summary = data.get("coverage_summary") or {}
        if not isinstance(summary, Mapping):
            raise ReviewValidationError("coverage_summary must be an object")
        paths = data.get("coverage_paths") or []
        if not isinstance(paths, list) or len(paths) > MAX_DRAFT_COVERAGE:
            raise ReviewValidationError("coverage_paths must be a bounded list")
        return cls(
            draft_id=review_identifier(data.get("draft_id"), "draft_id"),
            repo=str(data.get("repo", "") or ""),
            pr=_int(data.get("pr", data.get("pr_number")), "pr"),
            revision=_revision(data.get("revision")),
            findings=tuple(DraftFinding.from_dict(item) for item in raw_findings),
            coverage_summary={
                name: int(summary.get(name) or 0)
                for name in COVERAGE_STATUSES
                if not isinstance(summary.get(name), bool)
            },
            coverage_paths=tuple(review_path(item) for item in paths),
            provenance=dict(data.get("provenance") or {}),
            context=dict(data.get("context") or {}),
            created_at=str(data.get("created_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
        )

    @property
    def pending_findings(self) -> tuple[DraftFinding, ...]:
        return tuple(item for item in self.findings if item.status == "pending")


def finding_sort_key(finding: FileFinding) -> tuple[Any, ...]:
    """Order findings by severity, then path, then a stable identity."""
    return (
        SEVERITY_ORDER.get(finding.severity, len(FINDING_SEVERITIES)),
        finding.path,
        finding.line if finding.line is not None else 0,
        finding.created_at,
        finding.finding_id,
    )
