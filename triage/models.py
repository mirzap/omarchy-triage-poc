"""Data models for PR triage."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ChangedFile:
    path: str
    patch: str = ""
    status: str = ""
    previous_path: str = ""
    additions: int | None = None
    deletions: int | None = None
    patch_complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangedFile:
        counts_valid = all(
            value is None or _is_count(value)
            for value in (data.get("additions"), data.get("deletions"))
        )
        return cls(
            path=data["path"],
            patch=data.get("patch", "") or "",
            status=data.get("status", "") or "",
            previous_path=data.get("previous_path", "") or "",
            additions=_optional_int(data.get("additions")),
            deletions=_optional_int(data.get("deletions")),
            patch_complete=(
                _bool_value(data.get("patch_complete"), default=False)
                if "patch_complete" in data
                else True
            )
            and counts_valid,
        )


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return value if _is_count(value) else None


def _bool_value(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return default


@dataclass(frozen=True)
class RevisionEvidence:
    """The complete, content-bound revision of one reviewed pull request."""

    pr_number: int
    head_sha: str = ""
    base_sha: str = ""
    additions: int | None = None
    deletions: int | None = None
    content_digest: str = ""
    evidence_complete: bool = False
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RevisionEvidence:
        counts_valid = all(
            value is None or _is_count(value)
            for value in (data.get("additions"), data.get("deletions"))
        )
        return cls(
            pr_number=int(data.get("pr_number", data.get("number", 0)) or 0),
            head_sha=str(data.get("head_sha", "") or ""),
            base_sha=str(data.get("base_sha", "") or ""),
            additions=_optional_int(data.get("additions")),
            deletions=_optional_int(data.get("deletions")),
            content_digest=str(data.get("content_digest", "") or ""),
            evidence_complete=_bool_value(
                data.get("evidence_complete"), default=False
            )
            and counts_valid,
            source=str(data.get("source", "") or ""),
        )


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    user: str
    changed_files: list[ChangedFile]
    created_at: str
    fingerprint: str = ""
    label: str = "needs-human"
    html_url: str = ""
    simhash: int = 0
    head_sha: str = ""
    base_sha: str = ""
    updated_at: str = ""
    additions: int | None = None
    deletions: int | None = None
    evidence_complete: bool = True
    content_digest: str = ""
    evidence_source: str = ""
    cache_snapshot_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "user": self.user,
            "changed_files": [f.to_dict() for f in self.changed_files],
            "created_at": self.created_at,
            "fingerprint": self.fingerprint,
            "label": self.label,
            "html_url": self.html_url,
            "simhash": self.simhash,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "updated_at": self.updated_at,
            "additions": self.additions,
            "deletions": self.deletions,
            "evidence_complete": self.evidence_complete,
            "content_digest": self.content_digest,
            "evidence_source": self.evidence_source,
            "cache_snapshot_id": self.cache_snapshot_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PullRequest:
        files = [ChangedFile.from_dict(f) for f in data.get("changed_files", [])]
        counts_valid = all(
            value is None or _is_count(value)
            for value in (data.get("additions"), data.get("deletions"))
        )
        raw_sh = data.get("simhash", 0)
        if isinstance(raw_sh, str):
            try:
                simhash_val = int(raw_sh, 16) if not raw_sh.isdigit() else int(raw_sh)
            except ValueError:
                simhash_val = 0
        else:
            simhash_val = int(raw_sh or 0)
        return cls(
            number=int(data["number"]),
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            user=data.get("user", ""),
            changed_files=files,
            created_at=data.get("created_at", ""),
            fingerprint=data.get("fingerprint", ""),
            label=data.get("label", "needs-human"),
            html_url=data.get("html_url", "") or "",
            simhash=simhash_val,
            head_sha=data.get("head_sha", "") or "",
            base_sha=data.get("base_sha", "") or "",
            updated_at=data.get("updated_at", "") or "",
            additions=_optional_int(data.get("additions")),
            deletions=_optional_int(data.get("deletions")),
            evidence_complete=(
                _bool_value(data.get("evidence_complete"), default=False)
                if "evidence_complete" in data
                else True
            )
            and counts_valid,
            content_digest=data.get("content_digest", "") or "",
            evidence_source=data.get("evidence_source", "") or "",
            cache_snapshot_id=data.get("cache_snapshot_id", "") or "",
        )

    @property
    def paths(self) -> list[str]:
        return sorted(f.path for f in self.changed_files)

    @property
    def text_for_embed(self) -> str:
        # Repeat paths so file overlap dominates title/body noise for clustering.
        paths = " ".join(self.paths)
        path_boost = " ".join(self.paths * 3)
        return f"{self.title}\n{self.body}\n{paths}\n{path_boost}"

    def revision_evidence(self) -> RevisionEvidence:
        """Return a fail-closed identity over exact, complete diff evidence."""
        total_counts_valid = all(
            value is None or _is_count(value)
            for value in (self.additions, self.deletions)
        )
        file_counts_valid = [
            all(
                value is None or _is_count(value)
                for value in (f.additions, f.deletions)
            )
            for f in self.changed_files
        ]
        additions = self.additions if _is_count(self.additions) else None
        deletions = self.deletions if _is_count(self.deletions) else None
        derived_counts = [unified_patch_line_counts(f.patch) for f in self.changed_files]
        file_additions = [
            f.additions
            if _is_count(f.additions)
            else derived[0]
            if f.additions is None
            else None
            for f, derived in zip(self.changed_files, derived_counts)
        ]
        file_deletions = [
            f.deletions
            if _is_count(f.deletions)
            else derived[1]
            if f.deletions is None
            else None
            for f, derived in zip(self.changed_files, derived_counts)
        ]
        if self.additions is None and self.changed_files and all(
            value is not None for value in file_additions
        ):
            additions = sum(int(value or 0) for value in file_additions)
        if self.deletions is None and self.changed_files and all(
            value is not None for value in file_deletions
        ):
            deletions = sum(int(value or 0) for value in file_deletions)
        files_complete = bool(self.changed_files) and all(
            f.patch_complete
            and file_counts_valid[index]
            and file_additions[index] is not None
            and file_deletions[index] is not None
            and (
                f.additions is None or f.additions == derived_counts[index][0]
            )
            and (
                f.deletions is None or f.deletions == derived_counts[index][1]
            )
            and bool(f.path)
            and bool(f.patch)
            for index, f in enumerate(self.changed_files)
        )
        totals_match = bool(
            file_additions
            and all(value is not None for value in file_additions + file_deletions)
            and additions == sum(int(value or 0) for value in file_additions)
            and deletions == sum(int(value or 0) for value in file_deletions)
        )
        source = self.evidence_source.strip().lower()
        if source in {"gh", "github"}:
            source = "github"
        revision_known = bool(self.head_sha and self.base_sha)
        local_fixture = source == "fixtures"
        complete = bool(
            self.evidence_complete
            and total_counts_valid
            and (revision_known or local_fixture)
            and additions is not None
            and deletions is not None
            and files_complete
            and totals_match
        )
        material = [
            {
                "path": f.path,
                "previous_path": f.previous_path,
                "status": f.status,
                "additions": file_additions[index],
                "deletions": file_deletions[index],
                "patch": f.patch,
                "patch_complete": f.patch_complete,
            }
            for index, f in sorted(
                enumerate(self.changed_files),
                key=lambda pair: (pair[1].path, pair[1].previous_path, pair[1].status),
            )
        ]
        encoded = json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        # This digest identifies all bytes and metadata that were available.
        # evidence_complete separately says whether that identity is sufficient
        # for approval; incomplete is never conflated with unknown.
        digest = hashlib.sha256(encoded).hexdigest()
        return RevisionEvidence(
            pr_number=self.number,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            additions=additions,
            deletions=deletions,
            content_digest=digest,
            evidence_complete=complete,
            source=source,
        )


_HUNK_HEADER = re.compile(
    r"^@@ -(?:\d+)(?:,(\d+))? \+(?:\d+)(?:,(\d+))? @@(?:.*)$"
)


def unified_patch_line_counts(patch: str) -> tuple[int | None, int | None]:
    """Validate unified hunk accounting and return observed +/- line counts.

    A patch is complete only when every old/new line declared by each hunk is
    present. File headers are accepted before the first hunk. Once inside a
    hunk, lines beginning ``---`` or ``+++`` are ordinary removed/added content.
    """
    if not patch:
        return None, None
    additions = 0
    deletions = 0
    old_remaining: int | None = None
    new_remaining: int | None = None
    saw_hunk = False
    for line in patch.splitlines():
        header = _HUNK_HEADER.fullmatch(line)
        if header:
            if old_remaining not in (None, 0) or new_remaining not in (None, 0):
                return None, None
            old_remaining = int(header.group(1) or 1)
            new_remaining = int(header.group(2) or 1)
            saw_hunk = True
            continue
        if not saw_hunk:
            # GitHub patches may include standard file metadata before hunks.
            if line.startswith(
                (
                    "diff ",
                    "index ",
                    "--- ",
                    "+++ ",
                    "new file ",
                    "deleted file ",
                    "similarity ",
                    "rename ",
                )
            ):
                continue
            return None, None
        assert old_remaining is not None and new_remaining is not None
        if line == r"\ No newline at end of file":
            continue
        if line.startswith("+"):
            additions += 1
            new_remaining -= 1
        elif line.startswith("-"):
            deletions += 1
            old_remaining -= 1
        elif line.startswith(" "):
            old_remaining -= 1
            new_remaining -= 1
        else:
            return None, None
        if old_remaining < 0 or new_remaining < 0:
            return None, None
    if not saw_hunk or old_remaining != 0 or new_remaining != 0:
        return None, None
    return additions, deletions


@dataclass
class Group:
    group_id: str
    pr_numbers: list[int]
    fingerprints: list[str] = field(default_factory=list)
    shared_files: list[str] = field(default_factory=list)
    title_variants: list[str] = field(default_factory=list)
    suggested_decision: str = "unique"
    centroid: list[float] = field(default_factory=list)
    file_set_signature: str = ""
    summary: str = ""
    simhash: int = 0
    repo: str = ""
    member_revisions: list[RevisionEvidence] = field(default_factory=list)
    snapshot_digest: str = ""
    evidence_complete: bool = False
    member_path_signatures: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "member_revisions": [r.to_dict() for r in self.member_revisions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Group:
        kwargs = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        if "simhash" in kwargs and isinstance(kwargs["simhash"], str):
            try:
                kwargs["simhash"] = (
                    int(kwargs["simhash"], 16)
                    if not str(kwargs["simhash"]).isdigit()
                    else int(kwargs["simhash"])
                )
            except ValueError:
                kwargs["simhash"] = 0
        kwargs["member_revisions"] = [
            RevisionEvidence.from_dict(raw)
            for raw in data.get("member_revisions", [])
            if isinstance(raw, dict)
        ]
        # Legacy groups have no trustworthy revision binding.
        kwargs["evidence_complete"] = _bool_value(
            data.get("evidence_complete"), default=False
        )
        return cls(**kwargs)

    def bind_revisions(self, prs: list[PullRequest]) -> None:
        by_number = {pr.number: pr for pr in prs}
        self.member_revisions = [
            by_number[number].revision_evidence()
            for number in sorted(self.pr_numbers)
            if number in by_number
        ]
        self.evidence_complete = bool(self.pr_numbers) and (
            len(self.member_revisions) == len(set(self.pr_numbers))
            and all(item.evidence_complete for item in self.member_revisions)
        )
        self.snapshot_digest = revision_snapshot_digest(self.member_revisions)
        self.member_path_signatures = {
            str(number): hashlib.sha256(
                json.dumps(
                    by_number[number].paths,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            for number in sorted(set(self.pr_numbers))
            if number in by_number
        }


def revision_snapshot_digest(revisions: list[RevisionEvidence]) -> str:
    """Digest membership and every revision field, including incompleteness."""
    payload = [item.to_dict() for item in sorted(revisions, key=lambda r: r.pr_number)]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DecisionEvent:
    """Append-only audit event for one exact group snapshot."""

    event_id: str
    idempotency_key: str
    repo: str
    group_id: str
    decision: str
    actor: str
    decided_at: str
    snapshot_digest: str
    revisions: tuple[RevisionEvidence, ...]
    evidence_complete: bool

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["revisions"] = [item.to_dict() for item in self.revisions]
        return value

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionEvent:
        return cls(
            event_id=str(data.get("event_id", "") or ""),
            idempotency_key=str(data.get("idempotency_key", "") or ""),
            repo=str(data.get("repo", "") or ""),
            group_id=str(data.get("group_id", "") or ""),
            decision=str(data.get("decision", "") or ""),
            actor=str(data.get("actor", "") or ""),
            decided_at=str(data.get("decided_at", "") or ""),
            snapshot_digest=str(data.get("snapshot_digest", "") or ""),
            revisions=tuple(
                RevisionEvidence.from_dict(raw)
                for raw in data.get("revisions", [])
                if isinstance(raw, dict)
            ),
            evidence_complete=_bool_value(
                data.get("evidence_complete"), default=False
            ),
        )


@dataclass
class TrustedRule:
    rule_id: str
    group_id: str
    decision: str  # approve | reject | hardware | upgrade
    fingerprints: list[str]
    centroid: list[float]
    file_set_signature: str
    shared_files: list[str]
    created_from_prs: list[int]
    simhash: int = 0
    repo: str = ""
    reviewed_pr_numbers: list[int] = field(default_factory=list)
    reviewed_revisions: list[RevisionEvidence] = field(default_factory=list)
    snapshot_digest: str = ""
    evidence_complete: bool = False
    decision_event_id: str = ""
    actor: str = ""
    decided_at: str = ""
    legacy_unverified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "reviewed_revisions": [r.to_dict() for r in self.reviewed_revisions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrustedRule:
        kwargs = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        if "simhash" in kwargs and isinstance(kwargs["simhash"], str):
            try:
                kwargs["simhash"] = (
                    int(kwargs["simhash"], 16)
                    if not str(kwargs["simhash"]).isdigit()
                    else int(kwargs["simhash"])
                )
            except ValueError:
                kwargs["simhash"] = 0
        # Back-compat: missing simhash → 0 (clause skipped)
        kwargs.setdefault("simhash", 0)
        # Legacy rules recorded the reviewed membership under created_from_prs.
        kwargs.setdefault(
            "reviewed_pr_numbers",
            [int(n) for n in data.get("created_from_prs", [])],
        )
        kwargs["reviewed_revisions"] = [
            RevisionEvidence.from_dict(raw)
            for raw in data.get("reviewed_revisions", [])
            if isinstance(raw, dict)
        ]
        kwargs["evidence_complete"] = _bool_value(
            data.get("evidence_complete"), default=False
        )
        kwargs["legacy_unverified"] = _bool_value(
            data.get("legacy_unverified"), default=False
        )
        if not kwargs["reviewed_revisions"]:
            kwargs["evidence_complete"] = False
            kwargs["legacy_unverified"] = True
        return cls(**kwargs)

    @property
    def reviewed_members(self) -> set[int]:
        """PR membership that a human actually reviewed for this decision."""
        values = self.reviewed_pr_numbers or self.created_from_prs
        return {int(n) for n in values}
