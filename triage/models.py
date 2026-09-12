"""Data models for PR triage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ChangedFile:
    path: str
    patch: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangedFile:
        return cls(path=data["path"], patch=data.get("patch", ""))


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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PullRequest:
        files = [ChangedFile.from_dict(f) for f in data.get("changed_files", [])]
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Group:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class TrustedRule:
    rule_id: str
    group_id: str
    decision: str  # approve | reject
    fingerprints: list[str]
    centroid: list[float]
    file_set_signature: str
    shared_files: list[str]
    created_from_prs: list[int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrustedRule:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
