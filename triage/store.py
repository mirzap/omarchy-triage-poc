"""JSON persistence for trusted rules and last-run groups."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from triage.classify import classify_group
from triage.models import (
    DecisionEvent,
    Group,
    PullRequest,
    RevisionEvidence,
    TrustedRule,
    revision_snapshot_digest,
)

DEFAULT_STORE_DIR = Path(".triage")
DEFAULT_STORE_PATH = DEFAULT_STORE_DIR / "store.json"
STORE_SCHEMA_VERSION = 2
MAX_SAFE_INTEGER = 9_007_199_254_740_991
_STORE_LOCK = threading.RLock()


class StoreError(RuntimeError):
    """Base class for durable-store failures."""


class StoreCorruptionError(StoreError):
    """The on-disk JSON is unreadable or structurally invalid."""


class StoreConflictError(StoreError):
    """An optimistic write targeted a stale repository or snapshot."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        current_repo: str,
        current_version: int,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current_repo = current_repo
        self.current_version = current_version


class IncompleteEvidenceError(ValueError):
    """Approval was attempted without a complete revision-bound diff."""


class StorePathError(StoreError, ValueError):
    """A store path is unsafe or ambiguous for cross-process locking."""


class StoreVersionExhaustedError(StoreError, ValueError):
    """A store version can no longer be incremented safely for browser clients."""


def _repo_key(repo: str | None) -> str:
    return (repo or "").strip().strip("/").lower()


def _empty_store() -> dict[str, Any]:
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "store_version": 0,
        "snapshot_version": 0,
        "decision_events": [],
        "decision_idempotency": {},
        "legacy_decision_history": [],
        "trusted_rules": [],
        "last_groups": [],
        "last_pr_numbers": [],
        "last_prs": [],
        "last_edges": [],
        "last_group_edges": [],
        "last_overlap": {},
        "source": "",
        "repo": "",
        "last_new_pr_numbers": [],
        "last_queue": {},
        "groups_by_repo": {},
        "pr_numbers_by_repo": {},
        "reserved_group_ids": [],
        "reserved_rule_ids": [],
    }


def _materialize_identity(data: dict[str, Any]) -> dict[str, Any]:
    """Give legacy records their original repository before any repo switch."""
    data.setdefault("groups_by_repo", {})
    data.setdefault("pr_numbers_by_repo", {})
    data.setdefault("reserved_group_ids", [])
    data.setdefault("reserved_rule_ids", [])
    data.setdefault("store_version", 0)
    data.setdefault("snapshot_version", 0)
    data.setdefault("decision_events", [])
    data.setdefault("decision_idempotency", {})
    data.setdefault("legacy_decision_history", [])
    data.setdefault("schema_version", STORE_SCHEMA_VERSION)

    original_repo = _repo_key(data.get("repo"))

    normalized_groups: dict[str, list[dict[str, Any]]] = {}
    for stored_repo, records in data["groups_by_repo"].items():
        scoped_repo = _repo_key(stored_repo)
        if not scoped_repo:
            continue
        for raw in records:
            raw.setdefault("repo", scoped_repo)
        normalized_groups[scoped_repo] = records
    data["groups_by_repo"] = normalized_groups
    data["pr_numbers_by_repo"] = {
        _repo_key(stored_repo): numbers
        for stored_repo, numbers in data["pr_numbers_by_repo"].items()
        if _repo_key(stored_repo)
    }

    for raw in data.get("last_groups") or []:
        # A missing field is legacy data eligible for the store's original
        # repository context.  Once materialized, an explicit empty value means
        # provenance is unknown and must remain fail-closed across repo swaps.
        if "repo" not in raw:
            raw["repo"] = original_repo
    for raw in data.get("trusted_rules") or []:
        if "repo" not in raw:
            raw["repo"] = original_repo
        raw.setdefault("reviewed_pr_numbers", list(raw.get("created_from_prs") or []))
        if not raw.get("decision_event_id"):
            historical = dict(raw)
            historical["evidence_complete"] = False
            historical["legacy_unverified"] = True
            identity = (
                _repo_key(historical.get("repo")),
                historical.get("rule_id"),
                historical.get("group_id"),
                historical.get("decision"),
            )
            if not any(
                (
                    _repo_key(item.get("repo")),
                    item.get("rule_id"),
                    item.get("group_id"),
                    item.get("decision"),
                )
                == identity
                for item in data["legacy_decision_history"]
                if isinstance(item, dict)
            ):
                data["legacy_decision_history"].append(historical)

    if original_repo and data.get("last_groups"):
        data["groups_by_repo"][original_repo] = list(data["last_groups"])
        data["pr_numbers_by_repo"][original_repo] = list(data.get("last_pr_numbers") or [])

    group_ids = set(data.get("reserved_group_ids") or [])
    rule_ids = set(data.get("reserved_rule_ids") or [])
    group_ids.update(g.get("group_id", "") for g in data.get("last_groups") or [])
    for records in data["groups_by_repo"].values():
        group_ids.update(g.get("group_id", "") for g in records)
    for raw in data.get("trusted_rules") or []:
        group_ids.add(raw.get("group_id", ""))
        rule_ids.add(raw.get("rule_id", ""))
    data["reserved_group_ids"] = sorted(x for x in group_ids if x)
    data["reserved_rule_ids"] = sorted(x for x in rule_ids if x)
    return data


def ensure_store_dir(path: Path = DEFAULT_STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _canonical_store_path(path: Path) -> Path:
    """Resolve parent aliases but reject a symlink as the store itself."""
    path = Path(path).expanduser()
    if path.is_symlink():
        raise StorePathError(f"store path must not be a symbolic link: {path}")
    return path.parent.resolve(strict=False) / path.name


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextmanager
def _store_lock(path: Path, *, exclusive: bool):
    """Serialize cooperating threads and processes with one sidecar lock."""
    canonical = _canonical_store_path(path)
    with _STORE_LOCK:
        ensure_store_dir(canonical)
        lock_path = _lock_path(canonical)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield canonical
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _validate_store(data: Any, *, source: Path | None = None) -> dict[str, Any]:
    location = f" at {source}" if source is not None else ""
    if not isinstance(data, dict):
        raise StoreCorruptionError(f"store{location} must contain a JSON object")
    schema = data.get("schema_version")
    if schema is not None and (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema < 1
        or schema > STORE_SCHEMA_VERSION
    ):
        raise StoreCorruptionError(
            f"store{location} has unsupported schema_version {schema!r}"
        )
    for key in (
        "trusted_rules",
        "last_groups",
        "last_prs",
        "decision_events",
        "legacy_decision_history",
    ):
        value = data.get(key, [])
        if not isinstance(value, list):
            raise StoreCorruptionError(f"store{location} field {key!r} must be a list")
    for key in ("store_version", "snapshot_version"):
        value = data.get(key, 0)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_SAFE_INTEGER
        ):
            raise StoreCorruptionError(
                f"store{location} field {key!r} must be a JavaScript-safe "
                "non-negative integer"
            )
    if not isinstance(data.get("decision_idempotency", {}), dict):
        raise StoreCorruptionError(
            f"store{location} field 'decision_idempotency' must be an object"
        )
    for key in ("groups_by_repo", "pr_numbers_by_repo"):
        if not isinstance(data.get(key, {}), dict):
            raise StoreCorruptionError(f"store{location} field {key!r} must be an object")
    try:
        for raw in data.get("last_groups", []):
            if not isinstance(raw, dict):
                raise TypeError("group entry is not an object")
            if not isinstance(raw.get("group_id"), str) or not raw.get("group_id"):
                raise ValueError("group ID is missing")
            if not isinstance(raw.get("pr_numbers"), list):
                raise TypeError("group pr_numbers is not a list")
            if "member_revisions" in raw and not isinstance(raw["member_revisions"], list):
                raise TypeError("group member_revisions is not a list")
            Group.from_dict(raw)
        for raw in data.get("trusted_rules", []):
            if not isinstance(raw, dict):
                raise TypeError("trusted rule entry is not an object")
            if "reviewed_revisions" in raw and not isinstance(
                raw["reviewed_revisions"], list
            ):
                raise TypeError("trusted rule reviewed_revisions is not a list")
            TrustedRule.from_dict(raw)
        event_ids: set[str] = set()
        for raw in data.get("decision_events", []):
            if not isinstance(raw, dict):
                raise TypeError("decision event entry is not an object")
            if not isinstance(raw.get("revisions"), list):
                raise TypeError("decision event revisions is not a list")
            event = DecisionEvent.from_dict(raw)
            if not event.event_id or event.event_id in event_ids:
                raise ValueError("decision event IDs must be non-empty and unique")
            if not event.repo or not event.group_id or not event.decision or not event.actor:
                raise ValueError("decision event identity is incomplete")
            event_ids.add(event.event_id)
        for key, record in data.get("decision_idempotency", {}).items():
            if not isinstance(key, str) or not key or not isinstance(record, dict):
                raise ValueError("invalid decision idempotency entry")
            if record.get("event_id") not in event_ids:
                raise ValueError("decision idempotency entry references no event")
    except (TypeError, ValueError, KeyError) as exc:
        raise StoreCorruptionError(f"invalid store structure{location}: {exc}") from exc
    return data


def _read_store_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StoreCorruptionError(f"cannot read valid store JSON at {path}: {exc}") from exc
    data = _validate_store(data, source=path)
    try:
        data.setdefault("trusted_rules", [])
        data.setdefault("last_groups", [])
        data.setdefault("last_pr_numbers", [])
        data.setdefault("last_prs", [])
        data.setdefault("last_edges", [])
        data.setdefault("last_group_edges", [])
        data.setdefault("last_overlap", {})
        data.setdefault("source", "")
        data.setdefault("repo", "")
        data.setdefault("last_new_pr_numbers", [])
        data.setdefault("last_queue", {})
        return _materialize_identity(data)
    except (TypeError, ValueError, KeyError) as exc:
        raise StoreCorruptionError(f"invalid store structure at {path}: {exc}") from exc


def load_store(path: Path = DEFAULT_STORE_PATH) -> dict[str, Any]:
    with _store_lock(path, exclusive=False) as store_path:
        return _read_store_unlocked(store_path)


def _next_version(value: int, *, field: str) -> int:
    if value >= MAX_SAFE_INTEGER:
        raise StoreVersionExhaustedError(
            f"{field} reached the maximum JavaScript-safe integer; "
            "the store was not changed"
        )
    return value + 1


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync after publishing a replacement file."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Some filesystems do not support syncing directory descriptors.  The
        # file itself was synced before the atomic replacement.
        pass
    finally:
        os.close(fd)


def _atomic_write_bytes(payload: bytes, path: Path) -> None:
    if path.is_symlink():
        raise StorePathError(f"store recovery path must not be a symbolic link: {path}")
    ensure_store_dir(path)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            temp_path = Path(fh.name)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
        temp_path = None
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_write_json(
    data: dict[str, Any],
    path: Path,
    *,
    preserve_previous: bool = True,
) -> None:
    """Publish complete JSON and retain the last valid copy as ``.bak``."""
    _validate_store(data)
    ensure_store_dir(path)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            temp_path = Path(fh.name)
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if preserve_previous and path.exists():
            # Validation is deliberate: never replace a known-good backup
            # with corrupt bytes from the primary file.
            _read_store_unlocked(path)
            _atomic_write_bytes(path.read_bytes(), backup_path(path))
        os.replace(temp_path, path)
        temp_path = None
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def backup_path(path: Path = DEFAULT_STORE_PATH) -> Path:
    return path.with_name(f"{path.name}.bak")


def _commit_store_unlocked(data: dict[str, Any], path: Path) -> None:
    _atomic_write_json(data, path)


def save_store(
    data: dict[str, Any],
    path: Path = DEFAULT_STORE_PATH,
    *,
    expected_version: int | None = None,
) -> None:
    """Replace the store as one transaction, optionally using optimistic CAS."""
    with _store_lock(path, exclusive=True) as store_path:
        current = _read_store_unlocked(store_path)
        current_version = int(current.get("store_version", 0))
        if expected_version is not None and expected_version != current_version:
            raise StoreConflictError(
                "store changed since it was read",
                code="stale_store_version",
                current_repo=_repo_key(current.get("repo")),
                current_version=current_version,
            )
        replacement = dict(data)
        replacement["store_version"] = _next_version(
            current_version, field="store_version"
        )
        replacement.setdefault("snapshot_version", current.get("snapshot_version", 0))
        _materialize_identity(replacement)
        _commit_store_unlocked(replacement, store_path)


def backup_store(
    path: Path = DEFAULT_STORE_PATH,
    destination: Path | None = None,
) -> Path:
    """Create a validated atomic backup and return its path."""
    store_path = _canonical_store_path(path)
    target = _canonical_store_path(destination or backup_path(store_path))
    if target == store_path:
        raise ValueError("backup destination must differ from store path")
    with _store_lock(store_path, exclusive=False):
        data = _read_store_unlocked(store_path)
        _atomic_write_json(data, target, preserve_previous=False)
    return target


def restore_store(
    path: Path = DEFAULT_STORE_PATH,
    source: Path | None = None,
) -> Path:
    """Restore a validated backup, retaining current bytes for recovery.

    The restored store receives a fresh, greater version so stale browser tabs
    cannot accidentally become current again after a rollback.
    """
    store_path = _canonical_store_path(path)
    backup = _canonical_store_path(source or backup_path(store_path))
    with _store_lock(store_path, exclusive=True):
        restored = _read_store_unlocked(backup)
        current: dict[str, Any] | None = None
        current_bytes: bytes | None = None
        if store_path.exists():
            current_bytes = store_path.read_bytes()
            try:
                current = _read_store_unlocked(store_path)
            except StoreCorruptionError:
                current = None
        previous = store_path.with_name(f"{store_path.name}.pre-restore")
        restored = dict(restored)
        old_versions = [int(restored.get("store_version", 0))]
        if current is not None:
            old_versions.append(int(current.get("store_version", 0)))
            restored["snapshot_version"] = max(
                int(restored.get("snapshot_version", 0)),
                int(current.get("snapshot_version", 0)),
            )
        # Milliseconds remain exactly representable by JavaScript Number while
        # making recovery from an unreadable primary advance beyond ordinary
        # counter values. A readable primary still advances monotonically by 1.
        next_counter = _next_version(max(old_versions), field="store_version")
        restored["store_version"] = max(next_counter, int(time.time() * 1000))
        if restored["store_version"] > MAX_SAFE_INTEGER:
            raise StoreVersionExhaustedError(
                "restore version exceeds the maximum JavaScript-safe integer; "
                "the store was not changed"
            )
        if current_bytes is not None:
            _atomic_write_bytes(current_bytes, previous)
        _atomic_write_json(restored, store_path, preserve_previous=False)
    return previous


def save_groups(groups: list[Group], pr_numbers: list[int], path: Path = DEFAULT_STORE_PATH) -> None:
    save_run_state(groups, pr_numbers, path=path)


def save_run_state(
    groups: list[Group],
    pr_numbers: list[int],
    *,
    last_prs: list[dict[str, Any]] | None = None,
    last_edges: list[dict[str, Any]] | None = None,
    last_group_edges: list[dict[str, Any]] | None = None,
    last_overlap: dict[str, Any] | None = None,
    source: str | None = None,
    repo: str | None = None,
    path: Path = DEFAULT_STORE_PATH,
    last_new_pr_numbers: list[int] | None = None,
    last_queue: dict[str, Any] | None = None,
    expected_snapshot_version: int | None = None,
    expected_version: int | None = None,
) -> None:
    """Persist groups plus slim PR/graph payload for the UI."""
    with _store_lock(path, exclusive=True) as store_path:
        data = _read_store_unlocked(store_path)
        current_version = int(data.get("store_version", 0))
        current_snapshot = int(data.get("snapshot_version", 0))
        if (
            expected_snapshot_version is not None
            and expected_snapshot_version != current_snapshot
        ):
            raise StoreConflictError(
                "a newer repository snapshot was already published",
                code="stale_snapshot_version",
                current_repo=_repo_key(data.get("repo")),
                current_version=current_version,
            )
        if expected_version is not None and expected_version != current_version:
            raise StoreConflictError(
                "store changed while the repository snapshot was prepared",
                code="stale_store_version",
                current_repo=_repo_key(data.get("repo")),
                current_version=current_version,
            )
        active_repo = _repo_key(repo if repo is not None else data.get("repo"))
        for g in groups:
            g.repo = active_repo
        data["last_groups"] = [g.to_dict() for g in groups]
        data["last_pr_numbers"] = pr_numbers
        if last_prs is not None:
            data["last_prs"] = last_prs
        if last_edges is not None:
            data["last_edges"] = last_edges
        if last_group_edges is not None:
            data["last_group_edges"] = last_group_edges
        if last_overlap is not None:
            data["last_overlap"] = last_overlap
        if source is not None:
            data["source"] = source
        if repo is not None:
            data["repo"] = active_repo
        if last_new_pr_numbers is not None:
            data["last_new_pr_numbers"] = last_new_pr_numbers
        if last_queue is not None:
            data["last_queue"] = last_queue
        if active_repo:
            data["groups_by_repo"][active_repo] = list(data["last_groups"])
            data["pr_numbers_by_repo"][active_repo] = list(pr_numbers)
        _materialize_identity(data)
        data["store_version"] = _next_version(current_version, field="store_version")
        data["snapshot_version"] = _next_version(
            current_snapshot, field="snapshot_version"
        )
        _commit_store_unlocked(data, store_path)


def _groups_from_data(data: dict[str, Any], repo: str | None = None) -> list[Group]:
    wanted = _repo_key(repo)
    raw = data.get("last_groups", [])
    if wanted:
        raw = (data.get("groups_by_repo") or {}).get(wanted, [])
    return [Group.from_dict(g) for g in raw]


def load_groups(path: Path = DEFAULT_STORE_PATH, repo: str | None = None) -> list[Group]:
    return _groups_from_data(load_store(path), repo)


def _rules_from_data(
    data: dict[str, Any], repo: str | None = None
) -> list[TrustedRule]:
    rules = [TrustedRule.from_dict(r) for r in data.get("trusted_rules", [])]
    wanted = _repo_key(repo if repo is not None else data.get("repo"))
    return [r for r in rules if _repo_key(r.repo) == wanted]


def load_rules(path: Path = DEFAULT_STORE_PATH, repo: str | None = None) -> list[TrustedRule]:
    return _rules_from_data(load_store(path), repo)


def reserved_group_ids(path: Path = DEFAULT_STORE_PATH) -> set[str]:
    return set(load_store(path).get("reserved_group_ids") or [])


def _upsert_rule_in_data(data: dict[str, Any], rule: TrustedRule) -> None:
    if not rule.reviewed_pr_numbers:
        rule.reviewed_pr_numbers = list(rule.created_from_prs)
    rules = data.get("trusted_rules", [])
    # Replace existing rule for same group_id + decision type, or append
    replaced = False
    for i, existing in enumerate(rules):
        if (
            existing.get("group_id") == rule.group_id
            and _repo_key(existing.get("repo")) == _repo_key(rule.repo)
        ):
            rules[i] = rule.to_dict()
            replaced = True
            break
    if not replaced:
        rules.append(rule.to_dict())
    data["trusted_rules"] = rules
    _materialize_identity(data)


def upsert_rule(rule: TrustedRule, path: Path = DEFAULT_STORE_PATH) -> None:
    with _store_lock(path, exclusive=True) as store_path:
        data = _read_store_unlocked(store_path)
        current_version = int(data.get("store_version", 0))
        _upsert_rule_in_data(data, rule)
        data["store_version"] = _next_version(current_version, field="store_version")
        _commit_store_unlocked(data, store_path)


def _stored_pr_revision(raw: dict[str, Any], repo: str) -> RevisionEvidence:
    """Rebuild the decision identity from the current persisted PR record."""
    try:
        number = raw.get("number")
        if type(number) is not int or number <= 0:
            raise ValueError("PR number must be a positive integer")
        source = str(raw.get("evidence_source") or "").strip().lower()
        if source in {"gh", "github"}:
            source = "github"
        if source == "fixtures":
            files = raw.get("files")
            if not isinstance(files, list):
                raise ValueError("fixture evidence has no persisted files")
            material = dict(raw)
            material["changed_files"] = files
            material["evidence_source"] = source
            evidence = PullRequest.from_dict(material).revision_evidence()
        else:
            evidence = RevisionEvidence.from_dict(
                {**raw, "pr_number": number, "source": source}
            )
        if source == "github" and evidence.evidence_complete:
            snapshot_id = raw.get("cache_snapshot_id")
            if not isinstance(snapshot_id, str) or not snapshot_id:
                raise ValueError("GitHub evidence has no immutable cache snapshot")
        if _repo_key(raw.get("repo", repo)) != repo:
            raise ValueError("PR evidence belongs to a different repository")
        return evidence
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreCorruptionError(
            f"current persisted PR evidence is invalid: {exc}"
        ) from exc


def _verified_group_snapshot(
    data: dict[str, Any], repo: str, group_id: str
) -> tuple[Group, list[RevisionEvidence], str, bool]:
    """Cross-check a group against unique current persisted PR evidence."""
    matches = [
        Group.from_dict(raw)
        for raw in (data.get("groups_by_repo") or {}).get(repo, [])
        if isinstance(raw, dict) and raw.get("group_id") == group_id
    ]
    if len(matches) != 1:
        if not matches:
            raise KeyError(f"group not found: {group_id}")
        raise StoreCorruptionError(f"group ID is not unique: {group_id}")
    match = matches[0]
    members = list(match.pr_numbers)
    if (
        not members
        or any(type(number) is not int or number <= 0 for number in members)
        or len(members) != len(set(members))
    ):
        raise StoreCorruptionError("group membership must be non-empty and unique")

    rows: dict[int, dict[str, Any]] = {}
    for raw in data.get("last_prs") or []:
        if not isinstance(raw, dict):
            continue
        number = raw.get("number")
        if type(number) is int and number in members:
            if number in rows:
                raise StoreCorruptionError(
                    f"persisted PR #{number} is duplicated in the active snapshot"
                )
            rows[number] = raw
    if set(rows) != set(members):
        raise IncompleteEvidenceError(
            "group has no current revision binding; reload Cached or Refresh GitHub "
            "before recording a decision"
        )
    if not match.member_revisions or not match.snapshot_digest:
        raise IncompleteEvidenceError(
            "group has no current revision binding; reload Cached or Refresh GitHub "
            "before recording a decision"
        )
    revisions = [_stored_pr_revision(rows[number], repo) for number in sorted(members)]
    bound = list(match.member_revisions)
    if (
        len(bound) != len(members)
        or len({item.pr_number for item in bound}) != len(members)
        or sorted(bound, key=lambda item: item.pr_number) != revisions
    ):
        raise StoreCorruptionError(
            "group revision evidence does not match the active persisted PR snapshot"
        )
    snapshot_digest = revision_snapshot_digest(revisions)
    if not match.snapshot_digest or match.snapshot_digest != snapshot_digest:
        raise StoreCorruptionError(
            "group snapshot digest does not match the active persisted PR snapshot"
        )
    complete = bool(
        match.evidence_complete
        and revisions
        and all(item.evidence_complete for item in revisions)
    )
    return match, revisions, snapshot_digest, complete


def decide_group(
    group_id: str,
    decision: str,
    path: Path = DEFAULT_STORE_PATH,
    *,
    expected_repo: str | None = None,
    expected_version: int | None = None,
    idempotency_key: str | None = None,
    actor: str = "local",
) -> TrustedRule:
    if decision not in ("approve", "reject", "hardware", "upgrade"):
        raise ValueError(
            f"decision must be approve|reject|hardware|upgrade, got {decision!r}"
        )
    normalized_expected_repo = (
        _repo_key(expected_repo) if expected_repo is not None else None
    )
    actor = actor.strip() or "local"
    key = (idempotency_key or "").strip()
    if idempotency_key is not None and not key:
        raise ValueError("idempotency_key must not be blank")
    with _store_lock(path, exclusive=True) as store_path:
        data = _read_store_unlocked(store_path)
        current_version = int(data.get("store_version", 0))
        repo = _repo_key(data.get("repo"))
        if not repo:
            raise ValueError("decision requires an active repository")
        request_identity = {
            "repo": normalized_expected_repo if normalized_expected_repo is not None else repo,
            "group_id": group_id,
            "decision": decision,
            "actor": actor,
        }
        if key:
            prior = (data.get("decision_idempotency") or {}).get(key)
            if isinstance(prior, dict):
                if prior.get("request") != request_identity:
                    raise StoreConflictError(
                        "idempotency key was already used for a different decision",
                        code="idempotency_key_reused",
                        current_repo=repo,
                        current_version=current_version,
                    )
                event_id = prior.get("event_id")
                event = next(
                    (
                        raw
                        for raw in data.get("decision_events") or []
                        if raw.get("event_id") == event_id
                    ),
                    None,
                )
                if not isinstance(event, dict) or not isinstance(event.get("rule"), dict):
                    raise StoreCorruptionError(
                        f"idempotency record {key!r} has no decision event"
                    )
                return TrustedRule.from_dict(event["rule"])
        if normalized_expected_repo is not None and normalized_expected_repo != repo:
            raise StoreConflictError(
                "active repository changed since the decision form was loaded",
                code="stale_repository",
                current_repo=repo,
                current_version=current_version,
            )
        if expected_version is not None and expected_version != current_version:
            raise StoreConflictError(
                "store changed since the decision form was loaded",
                code="stale_store_version",
                current_repo=repo,
                current_version=current_version,
            )
        match, revisions, snapshot_digest, complete = _verified_group_snapshot(
            data, repo, group_id
        )
        if decision == "approve" and not complete:
            raise IncompleteEvidenceError(
                "approve requires complete patches, +/- counts, and head/base revision identity"
            )
        event_id = f"decision-{uuid.uuid4()}"
        rule_id = f"rule-{group_id}-{decision}"
        if rule_id in set(data.get("reserved_rule_ids") or []):
            base = rule_id
            suffix = 2
            while rule_id in set(data.get("reserved_rule_ids") or []):
                rule_id = f"{base}-{suffix}"
                suffix += 1
        decided_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        rule = TrustedRule(
            rule_id=rule_id,
            group_id=group_id,
            decision=decision,
            fingerprints=list(match.fingerprints),
            centroid=list(match.centroid),
            file_set_signature=match.file_set_signature,
            shared_files=list(match.shared_files),
            created_from_prs=list(match.pr_numbers),
            simhash=int(getattr(match, "simhash", 0) or 0),
            repo=repo,
            reviewed_pr_numbers=list(match.pr_numbers),
            reviewed_revisions=revisions,
            snapshot_digest=snapshot_digest,
            evidence_complete=complete,
            decision_event_id=event_id,
            actor=actor,
            decided_at=decided_at,
            legacy_unverified=False,
        )
        event = DecisionEvent(
            event_id=event_id,
            idempotency_key=key,
            repo=repo,
            group_id=group_id,
            decision=decision,
            actor=actor,
            decided_at=decided_at,
            snapshot_digest=snapshot_digest,
            revisions=tuple(revisions),
            evidence_complete=complete,
        ).to_dict()
        # Keep the exact materialized result with the immutable event so an
        # idempotent retry can return its original response after later writes.
        event["rule"] = rule.to_dict()
        data.setdefault("decision_events", []).append(event)
        if key:
            data.setdefault("decision_idempotency", {})[key] = {
                "request": request_identity,
                "event_id": event_id,
            }
        _upsert_rule_in_data(data, rule)
        _refresh_queue_in_data(data)
        data["store_version"] = _next_version(current_version, field="store_version")
        _commit_store_unlocked(data, store_path)
        return rule


def _refresh_queue_in_data(data: dict[str, Any]) -> None:
    from triage.overlap import slim_to_pr
    from triage.queue import build_queue

    groups = [Group.from_dict(g) for g in (data.get("last_groups") or [])]
    prs = [slim_to_pr(p) for p in (data.get("last_prs") or [])]
    repo = _repo_key(data.get("repo"))
    rules = _rules_from_data(data, repo=repo)
    data["last_queue"] = build_queue(
        groups,
        prs,
        rules,
        new_pr_numbers=data.get("last_new_pr_numbers") or [],
        repo=repo,
    )


def _refresh_queue(path: Path = DEFAULT_STORE_PATH) -> None:
    """Rebuild last_queue after a local decision so the Queue tab moves."""
    with _store_lock(path, exclusive=True) as store_path:
        data = _read_store_unlocked(store_path)
        current_version = int(data.get("store_version", 0))
        _refresh_queue_in_data(data)
        data["store_version"] = _next_version(current_version, field="store_version")
        _commit_store_unlocked(data, store_path)


MAX_STATE_TITLES = 4


def _slim_group_for_ui(
    g: dict[str, Any],
    member_paths: list[str] | None = None,
) -> dict[str, Any]:
    out = dict(g)
    out.pop("centroid", None)
    out.pop("member_revisions", None)
    titles = out.get("title_variants") or []
    paths = list(member_paths or out.get("shared_files") or [])
    card = classify_group(titles, paths)
    out["card_class"] = card["card_class"]
    out["card_note"] = card["card_note"]
    fps = [f for f in (out.get("fingerprints") or []) if f]
    out["fingerprint_count"] = len(fps)
    out.pop("fingerprints", None)
    if out.get("suggested_decision") == "duplicate" and len(set(fps)) > 1:
        out["suggested_decision"] = "related-theme"
    if len(titles) > MAX_STATE_TITLES:
        out["title_variants"] = titles[:MAX_STATE_TITLES]
    return out


def _overlap_summary(ov: dict[str, Any]) -> dict[str, Any]:
    """Keep legacy summaries truthful; exact rows are always derived lazily."""
    shared = ov.get("shared") or []
    partial = ov.get("partial") or []
    unique = ov.get("unique") or []
    matrix = ov.get("matrix") or []
    return {
        "jaccard": ov.get("jaccard"),
        "shared": shared[:8],
        "partial": partial[:8],
        "unique": [],
        "matrix": [],
        "shared_n": len(shared),
        "partial_n": len(partial),
        "unique_n": len(unique),
        "matrix_n": len(matrix),
        "lazy": True,
    }


def overlap_for_group(
    group_id: str,
    path: Path = DEFAULT_STORE_PATH,
    *,
    member_page: int = 1,
    member_page_size: int = 24,
    row_page: int = 1,
    row_page_size: int = 80,
) -> dict[str, Any]:
    """Derive independent bounded member/row pages from persisted path facts."""
    if member_page < 1 or row_page < 1:
        raise ValueError("page numbers must be positive")
    if not 1 <= member_page_size <= 24 or not 1 <= row_page_size <= 80:
        raise ValueError("page size exceeds overlap bounds")
    data = load_store(path)
    groups = data.get("last_groups") or []
    g = next((x for x in groups if x.get("group_id") == group_id), None)
    if g is None:
        raise KeyError(f"group not found: {group_id}")
    pr_numbers = [int(number) for number in g.get("pr_numbers") or []]
    pr_by_number = {
        int(pr.get("number") or 0): pr for pr in data.get("last_prs") or []
    }
    paths_by_pr = {
        number: {str(value) for value in (pr_by_number.get(number, {}).get("paths") or []) if value}
        for number in pr_numbers
    }
    counts: dict[str, int] = {}
    for paths in paths_by_pr.values():
        for file_path in paths:
            counts[file_path] = counts.get(file_path, 0) + 1
    total_members = len(pr_numbers)
    rows = []
    for file_path in sorted(counts):
        count = counts[file_path]
        kind = "shared" if total_members and count == total_members else "unique" if count == 1 else "partial"
        rows.append((file_path, kind))
    member_start = (member_page - 1) * member_page_size
    shown = pr_numbers[member_start : member_start + member_page_size]
    row_start = (row_page - 1) * row_page_size
    shown_rows = rows[row_start : row_start + row_page_size]
    matrix = [
        {
            "path": file_path,
            "kind": kind,
            # Patch identity is authoritative only in /api/patches.
            "same_patch": None,
            "same_patch_status": "unknown",
            "patch_evidence_complete": {str(number): False for number in shown},
            "prs": {str(number): file_path in paths_by_pr.get(number, set()) for number in shown},
        }
        for file_path, kind in shown_rows
    ]
    shared_n = sum(kind == "shared" for _, kind in rows)
    partial_n = sum(kind == "partial" for _, kind in rows)
    unique_n = sum(kind == "unique" for _, kind in rows)
    member_pages = (total_members + member_page_size - 1) // member_page_size
    row_pages = (len(rows) + row_page_size - 1) // row_page_size
    return {
        "group_id": group_id,
        "jaccard": (shared_n / len(rows)) if rows else (1.0 if total_members else 0.0),
        "shared": [value for value, kind in shown_rows if kind == "shared"],
        "partial": [value for value, kind in shown_rows if kind == "partial"],
        "unique": [value for value, kind in shown_rows if kind == "unique"],
        "matrix": matrix,
        "shared_n": shared_n,
        "partial_n": partial_n,
        "unique_n": unique_n,
        "matrix_n": len(rows),
        "pr_numbers": shown,
        "member_page": member_page,
        "member_page_size": member_page_size,
        "member_pages": member_pages,
        "next_member_page": member_page + 1 if member_page < member_pages else None,
        "row_page": row_page,
        "row_page_size": row_page_size,
        "row_pages": row_pages,
        "next_row_page": row_page + 1 if row_page < row_pages else None,
        "pr_truncated": max(0, total_members - len(shown)),
        "row_truncated": max(0, len(rows) - len(matrix)),
        "lazy": False,
    }



def _slim_pr_for_ui(pr: dict[str, Any]) -> dict[str, Any]:
    paths = pr.get("paths") or []
    if not paths:
        files = pr.get("files") or []
        if files and isinstance(files[0], dict):
            paths = [f.get("path", "") for f in files if f.get("path")]
        elif files and isinstance(files[0], str):
            paths = list(files)
    return {
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "user": pr.get("user") or "",
        "label": pr.get("label") or "needs-human",
        "group_id": pr.get("group_id") or "",
        "html_url": pr.get("html_url") or "",
        "paths": paths,
        "path_count": len(paths),
        "created_at": pr.get("created_at") or "",
        "head_sha": pr.get("head_sha") or "",
        "base_sha": pr.get("base_sha") or "",
        "updated_at": pr.get("updated_at") or "",
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "content_digest": pr.get("content_digest") or "",
        "evidence_complete": bool(pr.get("evidence_complete", False)),
        "evidence_source": pr.get("evidence_source") or "",
    }


def prs_for_path(
    file_path: str,
    path: Path = DEFAULT_STORE_PATH,
    *,
    page: int = 1,
    page_size: int = 40,
) -> dict:
    """Return a bounded, traversable page of open PRs touching ``file_path``."""
    if page < 1 or not 1 <= page_size <= 40:
        raise ValueError("invalid file-list page")
    data = load_store(path)
    hits = []
    for pr in data.get("last_prs") or []:
        paths = pr.get("paths") or []
        if file_path in paths:
            hits.append({
                "number": pr.get("number"),
                "title": pr.get("title") or "",
                "user": pr.get("user") or "",
                "group_id": pr.get("group_id") or "",
                "html_url": pr.get("html_url") or "",
                "label": pr.get("label") or "needs-human",
            })
    hits.sort(key=lambda x: -(x["number"] or 0))
    start = (page - 1) * page_size
    pages = (len(hits) + page_size - 1) // page_size
    return {
        "path": file_path,
        "pr_count": len(hits),
        "prs": hits[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "next_page": page + 1 if page < pages else None,
        "truncated": max(0, len(hits) - min(len(hits), start + page_size)),
    }


def ui_state(path: Path = DEFAULT_STORE_PATH) -> dict[str, Any]:
    """Slim dashboard payload. Full overlap matrices are lazy via overlap_for_group."""
    data = load_store(path)
    raw_ov = data.get("last_overlap") or {}
    overlap = {gid: _overlap_summary(ov) for gid, ov in raw_ov.items()}
    paths_by_gid: dict[str, list[str]] = {}
    for pr in data.get("last_prs") or []:
        gid = pr.get("group_id") or ""
        if not gid:
            continue
        bucket = paths_by_gid.setdefault(gid, [])
        for member_path in pr.get("paths") or []:
            if member_path and member_path not in bucket:
                bucket.append(member_path)
    repo = _repo_key(data.get("repo"))
    group_models = [Group.from_dict(g) for g in data.get("last_groups", [])]
    from triage.queue import rule_applies_to_group

    active_rule_models = [
        rule
        for rule in _rules_from_data(data, repo=repo)
        if any(rule_applies_to_group(rule, group, repo) for group in group_models)
    ]
    active_rules = [
        {
            "rule_id": rule.rule_id,
            "group_id": rule.group_id,
            "decision": rule.decision,
            "repo": rule.repo,
            "snapshot_digest": rule.snapshot_digest,
            "evidence_complete": rule.evidence_complete,
            "decision_event_id": rule.decision_event_id,
            "actor": rule.actor,
            "decided_at": rule.decided_at,
        }
        for rule in active_rule_models
    ]
    # Derive the visible queue from the scoped rules on every read.  This
    # prevents a legacy persisted queue (including stale auto labels) from
    # exposing a foreign or no-longer-applicable decision before the next run.
    from triage.overlap import slim_to_pr
    from triage.queue import build_queue

    queue = build_queue(
        group_models,
        [slim_to_pr(pr) for pr in data.get("last_prs") or []],
        active_rule_models,
        new_pr_numbers=data.get("last_new_pr_numbers") or [],
        repo=repo,
    )
    return {
        "schema_version": int(data.get("schema_version", STORE_SCHEMA_VERSION)),
        "store_version": int(data.get("store_version", 0)),
        "snapshot_version": int(data.get("snapshot_version", 0)),
        "groups": [
            _slim_group_for_ui(g, paths_by_gid.get(g.get("group_id") or ""))
            for g in data.get("last_groups", [])
        ],
        "prs": [_slim_pr_for_ui(pr) for pr in data.get("last_prs", [])],
        "edges": [],
        "group_edges": [],
        "rules": active_rules,
        "overlap": overlap,
        "source": data.get("source", ""),
        "repo": data.get("repo", ""),
        "queue": queue,
        "new_pr_numbers": data.get("last_new_pr_numbers") or [],
    }
