"""JSON persistence for trusted rules and last-run groups."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from triage.classify import classify_group
from triage.models import (
    DecisionEvent,
    DISPOSITIONS,
    Group,
    Disposition,
    Proposal,
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
_DISPOSITION_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z")
MAX_PROPOSAL_ITEMS = 200


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
        # Additive local per-PR workflow data.  Keeping these as arrays makes
        # the JSON easy to inspect/recover and lets old stores load unchanged.
        "dispositions": [],
        "disposition_events": [],
        "disposition_idempotency": {},
        "proposals": [],
        "proposal_events": [],
        "proposal_idempotency": {},
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
    data.setdefault("dispositions", [])
    data.setdefault("disposition_events", [])
    data.setdefault("disposition_idempotency", {})
    data.setdefault("proposals", [])
    data.setdefault("proposal_events", [])
    data.setdefault("proposal_idempotency", {})
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
        "dispositions",
        "disposition_events",
        "proposals",
        "proposal_events",
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
    for key in (
        "decision_idempotency", "disposition_idempotency", "proposal_idempotency",
    ):
        if not isinstance(data.get(key, {}), dict):
            raise StoreCorruptionError(
                f"store{location} field {key!r} must be an object"
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
        parsed_dispositions: list[Disposition] = []
        for raw in data.get("dispositions", []):
            if not isinstance(raw, dict):
                raise TypeError("disposition entry is not an object")
            disposition = Disposition.from_dict(raw)
            parsed_dispositions.append(disposition)
            if disposition.disposition not in DISPOSITIONS:
                raise ValueError("invalid disposition")
            if disposition.pr <= 0 or not disposition.repo:
                raise ValueError("disposition identity is incomplete")
            if not 1 <= len(disposition.reason.strip()) <= 1000:
                raise ValueError("disposition reason is required and bounded")
        _check_duplicate_cycles(parsed_dispositions, {})
        for raw in data.get("proposals", []):
            if not isinstance(raw, dict):
                raise TypeError("proposal entry is not an object")
            proposal = Proposal.from_dict(raw)
            if not proposal.proposal_id or not proposal.repo or not proposal.group_id:
                raise ValueError("proposal identity is incomplete")
            if proposal.status not in {"draft", "accepted", "edited", "rejected"}:
                raise ValueError("invalid proposal status")
            _check_duplicate_cycles(list(proposal.items), {})
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


def _source_key(source: Any) -> str:
    value = str(source or "").strip().lower()
    if value in {"gh", "github"}:
        return "github"
    if value == "fixtures":
        return "fixtures"
    return value


def _persisted_pr_scope(raw: Mapping[str, Any], repo: str, source: Any) -> str:
    """Validate persisted provenance before callers add authoritative fields.

    Existing fixture stores intentionally omit a per-row ``repo`` and some
    hand-authored fixture rows predate ``evidence_source``.  That one local,
    self-contained representation is safe to bind to the active fixture store;
    cached provider evidence must always carry an explicit source.
    """
    active_source = _source_key(source)
    if active_source not in {"fixtures", "github"}:
        raise IncompleteEvidenceError("the active evidence source is unsupported")
    raw_source_value = raw.get("evidence_source")
    raw_source = _source_key(raw_source_value)
    if not raw_source:
        if active_source == "fixtures" and isinstance(raw.get("files"), list):
            raw_source = "fixtures"
        else:
            raise IncompleteEvidenceError("persisted PR has no evidence source")
    if raw_source not in {"fixtures", "github"} or raw_source != active_source:
        raise StoreCorruptionError(
            "persisted PR evidence source does not match the active store"
        )
    if "repo" in raw:
        raw_repo = raw.get("repo")
        if not isinstance(raw_repo, str) or not raw_repo.strip():
            raise StoreCorruptionError("persisted PR repository identity is invalid")
        if _repo_key(raw_repo) != repo:
            raise StoreCorruptionError(
                "persisted PR repository does not match the active store"
            )
    return raw_source


def _stored_pr_revision(
    raw: dict[str, Any],
    repo: str,
    *,
    expected_source: str | None = None,
) -> RevisionEvidence:
    """Rebuild the decision identity from the current persisted PR record."""
    try:
        number = raw.get("number")
        if type(number) is not int or number <= 0:
            raise ValueError("PR number must be a positive integer")
        source = _persisted_pr_scope(
            raw, repo, expected_source if expected_source is not None else raw.get("evidence_source")
        )
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
        stored_digest = raw.get("content_digest")
        if not isinstance(stored_digest, str) or not stored_digest:
            raise ValueError("persisted PR has no content digest")
        if source == "fixtures" and evidence.content_digest != stored_digest:
            raise ValueError("fixture evidence does not match its content digest")
        if source == "github" and evidence.evidence_complete:
            snapshot_id = raw.get("cache_snapshot_id")
            if not isinstance(snapshot_id, str) or not snapshot_id:
                raise ValueError("GitHub evidence has no immutable cache snapshot")
        return evidence
    except IncompleteEvidenceError:
        raise
    except StoreCorruptionError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreCorruptionError(
            f"current persisted PR evidence is invalid: {exc}"
        ) from exc


def _captured_pr_revision(
    data: dict[str, Any],
    repo: str,
    raw: dict[str, Any],
    *,
    expected_source: str | None = None,
) -> RevisionEvidence:
    """Read one PR's exact evidence from the captured store snapshot.

    This function is intentionally called while the store's exclusive lock is
    held by decision/proposal writers.  GitHub reads are strictly cache reads
    and are pinned to the ``cache_snapshot_id`` persisted with the PR; an
    active-cache pointer is never consulted.  Keeping this reader in store.py
    prevents approval code from trusting a UI/store completeness boolean.
    """
    try:
        number = raw.get("number")
        if type(number) is not int or number <= 0:
            raise ValueError("PR number must be a positive integer")
        source = _source_key(data.get("source"))
        if expected_source is not None and source != _source_key(expected_source):
            raise ValueError("PR evidence source does not match the active store")
        source = _persisted_pr_scope(raw, repo, source)
        material: dict[str, Any]
        snapshot_id = ""
        if source == "fixtures":
            files = raw.get("files")
            if not isinstance(files, list):
                raise IncompleteEvidenceError("fixture evidence has no persisted files")
            material = dict(raw)
            material["changed_files"] = files
            material["evidence_source"] = source
        elif source == "github":
            snapshot_id = str(raw.get("cache_snapshot_id") or "")
            if not snapshot_id:
                raise IncompleteEvidenceError(
                    "GitHub evidence has no immutable cache snapshot"
                )
            from triage import gh
            from triage.github import parse_repo

            owner, name = parse_repo(repo)
            captured = gh.cached_pr_evidence(
                owner, name, number, snapshot_id=snapshot_id
            )
            if not isinstance(captured, dict) or not captured:
                raise IncompleteEvidenceError(
                    "pinned GitHub evidence is unavailable; reload Cached or Refresh GitHub"
                )
            if captured.get("legacy_unverified") is True:
                raise IncompleteEvidenceError("legacy GitHub evidence is unverified")
            returned_snapshot_raw = captured.get("snapshot_id")
            if not isinstance(returned_snapshot_raw, str):
                raise StoreCorruptionError("cached evidence snapshot identity is invalid")
            returned_snapshot = returned_snapshot_raw
            if returned_snapshot != snapshot_id:
                raise StoreConflictError(
                    "pinned cache snapshot changed",
                    code="stale_snapshot_version",
                    current_repo=repo,
                    current_version=int(data.get("store_version", 0)),
                )
            meta = captured.get("meta")
            files = captured.get("files")
            if not isinstance(meta, dict) or not isinstance(files, list):
                raise IncompleteEvidenceError("pinned GitHub evidence is incomplete")
            sync = captured.get("sync")
            if not isinstance(sync, Mapping):
                raise StoreCorruptionError("cached evidence sync identity is missing")
            if any(not isinstance(item, dict) for item in files):
                raise StoreCorruptionError("cached evidence files are malformed")
            if type(meta.get("number")) is not int or meta.get("number") != number:
                raise StoreCorruptionError("cached evidence PR number does not match")
            returned_repo_raw = meta.get("repository")
            if not isinstance(returned_repo_raw, str) or not returned_repo_raw.strip():
                raise StoreCorruptionError("cached evidence repository identity is missing")
            returned_repo = _repo_key(returned_repo_raw)
            if returned_repo != repo:
                raise StoreCorruptionError("cached evidence repository does not match")
            returned_source = captured.get(
                "source", meta.get("source", meta.get("evidence_source"))
            )
            if returned_source is not None and _source_key(returned_source) != "github":
                raise StoreCorruptionError("cached evidence source does not match")
            if meta.get("snapshot_id") != snapshot_id:
                raise StoreCorruptionError("cached evidence metadata snapshot does not match")
            if (
                sync.get("repository") != repo
                or sync.get("snapshot_id") != snapshot_id
                or sync.get("schema_version") != 2
            ):
                raise StoreCorruptionError("cached evidence sync identity does not match")
            if meta.get("evidence_complete") is not True:
                raise IncompleteEvidenceError("provider metadata marks PR evidence incomplete")
            if meta.get("files_cap_reached") is not False:
                raise IncompleteEvidenceError("provider file cap prevents complete evidence")
            declared_count = meta.get("file_count")
            if (
                type(declared_count) is not int
                or declared_count <= 0
                or declared_count != len(files)
            ):
                raise IncompleteEvidenceError(
                    "provider file count does not match the cached manifest"
                )
            missing_patch_count = meta.get("missing_patch_count")
            if type(missing_patch_count) is not int or missing_patch_count != 0:
                raise IncompleteEvidenceError("provider metadata reports missing patches")
            for item in files:
                if (
                    not isinstance(item.get("path"), str)
                    or not item.get("path")
                    or not isinstance(item.get("patch"), str)
                    or not item.get("patch")
                    or item.get("patch_complete") is not True
                    or type(item.get("additions")) is not int
                    or item.get("additions") < 0
                    or type(item.get("deletions")) is not int
                    or item.get("deletions") < 0
                    or not isinstance(item.get("status"), str)
                    or not isinstance(item.get("previous_path"), str)
                ):
                    raise StoreCorruptionError("cached evidence file manifest is malformed")
            if raw.get("evidence_complete") is not True:
                raise IncompleteEvidenceError("persisted PR evidence is incomplete")
            material = {**meta, "number": number, "changed_files": files,
                        "evidence_source": "github",
                        "cache_snapshot_id": snapshot_id,
                        "evidence_complete": meta["evidence_complete"]}
        else:
            raise IncompleteEvidenceError("the active evidence source is unsupported")

        candidate = PullRequest.from_dict(material)
        candidate.cache_snapshot_id = snapshot_id or str(
            raw.get("cache_snapshot_id") or ""
        )
        revision = candidate.revision_evidence()
        # Compare the newly captured identity to the identity stored at the
        # time of the run.  A mismatch is a revision conflict, even when the
        # persisted boolean happened to say that evidence was complete.
        for field in ("head_sha", "base_sha", "updated_at"):
            expected = str(raw.get(field) or "")
            actual = str(getattr(candidate, field, "") or "")
            if source == "github" and (not expected or not actual or expected != actual):
                raise StoreConflictError(
                    "cached evidence does not match the stored revision",
                    code="revision_conflict",
                    current_repo=repo,
                    current_version=int(data.get("store_version", 0)),
                )
        expected_digest = str(raw.get("content_digest") or "")
        if not expected_digest or revision.content_digest != expected_digest:
            raise StoreConflictError(
                "cached evidence does not match the stored revision",
                code="revision_conflict",
                current_repo=repo,
                current_version=int(data.get("store_version", 0)),
            )
        expected_snapshot = str(raw.get("cache_snapshot_id") or "")
        if expected_snapshot and revision.cache_snapshot_id != expected_snapshot:
            raise StoreConflictError(
                "cached evidence snapshot does not match the stored revision",
                code="snapshot_conflict",
                current_repo=repo,
                current_version=int(data.get("store_version", 0)),
            )
        if not revision.evidence_complete:
            raise IncompleteEvidenceError(
                "complete patches, +/- counts, and revision identity are required"
            )
        return revision
    except StoreConflictError:
        raise
    except IncompleteEvidenceError:
        raise
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
        raise StoreCorruptionError(
            f"current persisted PR evidence is invalid: {exc}"
        ) from exc


def _index_persisted_prs(data: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    """Index active persisted PR rows while retaining duplicates for validation."""
    indexed: dict[int, list[dict[str, Any]]] = {}
    for raw in data.get("last_prs") or []:
        if not isinstance(raw, dict):
            continue
        number = raw.get("number")
        if type(number) is int:
            indexed.setdefault(number, []).append(raw)
    return indexed


def _current_persisted_revisions(
    data: dict[str, Any], repo: str,
) -> dict[int, RevisionEvidence]:
    """Return unique, scoped PR identities from the active persisted rows."""
    result: dict[int, RevisionEvidence] = {}
    source = _source_key(data.get("source"))
    for number, rows in _index_persisted_prs(data).items():
        if len(rows) != 1:
            continue
        try:
            result[number] = _stored_pr_revision(
                rows[0], repo, expected_source=source if source else None
            )
        except (StoreError, IncompleteEvidenceError, TypeError, ValueError, KeyError):
            # Read projections fail closed per PR.  Writers use the stricter
            # group verifier and surface corruption rather than silently using
            # an invalid row.
            continue
    return result


def _verified_group_snapshot(
    data: dict[str, Any],
    repo: str,
    group_id: str,
    *,
    pr_index: dict[int, list[dict[str, Any]]] | None = None,
    reverify: bool = False,
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

    indexed = pr_index
    if indexed is None:
        indexed = _index_persisted_prs(data)
    rows: dict[int, dict[str, Any]] = {}
    for number in members:
        candidates = indexed.get(number, [])
        if len(candidates) > 1:
            raise StoreCorruptionError(
                f"persisted PR #{number} is duplicated in the active snapshot"
            )
        if candidates:
            rows[number] = candidates[0]
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
    expected_source = _source_key(data.get("source"))
    revisions: list[RevisionEvidence] = []
    for number in sorted(members):
        row = rows[number]
        if not reverify:
            revisions.append(
                _stored_pr_revision(
                    row,
                    repo,
                    expected_source=expected_source if expected_source else None,
                )
            )
            continue
        try:
            # This is the important approval boundary: re-read the exact
            # pinned cache/fixture evidence while the writer lock is held.
            revisions.append(
                _captured_pr_revision(
                    data,
                    repo,
                    row,
                    expected_source=expected_source if expected_source else None,
                )
            )
        except IncompleteEvidenceError:
            # Incomplete evidence may still be represented for non-approval
            # dispositions. The persisted identity is marked incomplete; it
            # is never sufficient for approve/keep/duplicate.
            revisions.append(
                RevisionEvidence.from_dict(
                    {
                        **row,
                        "pr_number": number,
                        "source": expected_source or data.get("source", ""),
                        "evidence_complete": False,
                    }
                )
            )
    bound = list(match.member_revisions)
    bound_by_number = {item.pr_number: item for item in bound}
    revision_by_number = {item.pr_number: item for item in revisions}
    if (
        len(bound) != len(members)
        or len(bound_by_number) != len(members)
        or set(bound_by_number) != set(revision_by_number)
        or any(
            not _revision_binding_equal(bound_by_number[number], revision)
            for number, revision in revision_by_number.items()
        )
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


def _group_snapshot_is_current(
    data: dict[str, Any],
    repo: str,
    group: Group,
    *,
    pr_index: dict[int, list[dict[str, Any]]] | None = None,
) -> bool:
    """Return whether a projected group still matches its persisted PR rows."""
    try:
        match, _revisions, _digest, _complete = _verified_group_snapshot(
            data, repo, group.group_id, pr_index=pr_index
        )
    except (StoreError, IncompleteEvidenceError, KeyError, TypeError, ValueError):
        # A read projection must fail closed.  The next run/explicit reload can
        # repair the persisted binding; it must not make an old decision Known.
        return False
    return bool(
        _repo_key(match.repo) == _repo_key(group.repo)
        and match.pr_numbers == group.pr_numbers
        and match.member_revisions == group.member_revisions
        and match.snapshot_digest == group.snapshot_digest
        and match.evidence_complete == group.evidence_complete
    )


def _normal_repo(value: Any) -> str:
    return str(value or "").strip().strip("/").lower()


def _reason(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("reason is required")
    result = value.strip()
    if not 1 <= len(result) <= 1000:
        raise ValueError("reason must be between 1 and 1000 characters")
    return result


def _safe_disposition_key(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or not _DISPOSITION_IDEMPOTENCY_RE.fullmatch(value):
        raise ValueError("idempotency_key must be 8-128 safe characters")
    return value


def _raw_pr_index(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for raw in data.get("last_prs") or []:
        if isinstance(raw, dict) and type(raw.get("number")) is int:
            # Duplicate rows are not silently collapsed by writers.
            if raw["number"] in rows:
                raise StoreCorruptionError(
                    f"persisted PR #{raw['number']} is duplicated in the active snapshot"
                )
            rows[raw["number"]] = raw
    return rows


def _group_for_write(
    data: dict[str, Any],
    repo: str,
    group_id: str,
) -> tuple[Group, list[RevisionEvidence], str, bool]:
    if not isinstance(group_id, str) or not group_id or len(group_id) > 128:
        raise ValueError("group_id is invalid")
    index = _raw_pr_index(data)
    return _verified_group_snapshot(data, repo, group_id, pr_index={
        n: [row] for n, row in index.items()
    }, reverify=True)


def _revision_equal(left: RevisionEvidence, right: RevisionEvidence) -> bool:
    return (
        left.pr_number == right.pr_number
        and left.head_sha == right.head_sha
        and left.base_sha == right.base_sha
        and left.content_digest == right.content_digest
        and _source_key(left.source) == _source_key(right.source)
    )


def _revision_binding_equal(left: RevisionEvidence, right: RevisionEvidence) -> bool:
    """Compare persisted group-binding fields without cache provenance."""
    return (
        left.pr_number == right.pr_number
        and left.head_sha == right.head_sha
        and left.base_sha == right.base_sha
        and left.additions == right.additions
        and left.deletions == right.deletions
        and left.content_digest == right.content_digest
        and left.evidence_complete == right.evidence_complete
        and _source_key(left.source) == _source_key(right.source)
    )


def _revision_from_item(
    raw: Mapping[str, Any], current: RevisionEvidence, *, repo: str = "", version: int = 0,
) -> RevisionEvidence:
    supplied = raw.get("revision")
    if supplied is None:
        # A local human form may omit the ref; the writer binds it to the
        # captured revision rather than accepting an unbound boolean.
        return current
    if not isinstance(supplied, dict):
        raise ValueError("revision must be an object")
    candidate = RevisionEvidence.from_dict({**supplied, "pr_number": current.pr_number})
    if not _revision_equal(candidate, current):
        raise StoreConflictError(
            "disposition targets a stale PR revision",
            code="revision_conflict",
            current_repo=repo,
            current_version=version,
        )
    return current


def _normalize_disposition_items(
    data: dict[str, Any],
    repo: str,
    group: Group,
    revisions: list[RevisionEvidence],
    items: Any,
    *,
    actor: str,
    source: str,
    require_complete: bool,
) -> list[Disposition]:
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("items must be a non-empty list")
    if len(items) > MAX_PROPOSAL_ITEMS or len(items) > len(group.pr_numbers):
        raise ValueError("items cannot exceed group membership")
    members = set(group.pr_numbers)
    by_number = {item.pr_number: item for item in revisions}
    raw_items: list[Mapping[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("each disposition must be an object")
        raw_items.append(item)
    numbers: set[int] = set()
    out: list[Disposition] = []
    for raw in raw_items:
        number = raw.get("pr", raw.get("pr_number"))
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError("disposition pr must be a positive integer")
        if number not in members:
            raise ValueError("disposition PR is not a member of this group")
        if number in numbers:
            raise ValueError("disposition PRs must be unique")
        numbers.add(number)
        disposition = raw.get("disposition", raw.get("decision"))
        if not isinstance(disposition, str) or disposition not in DISPOSITIONS:
            raise ValueError("disposition must be keep|duplicate|reject|needs_hardware|upgrade|pending")
        reason = _reason(raw.get("reason"))
        current = by_number[number]
        bound = _revision_from_item(
            raw, current, repo=repo, version=int(data.get("store_version", 0))
        )
        if require_complete and disposition in {"keep", "duplicate"} and not bound.evidence_complete:
            raise IncompleteEvidenceError(
                f"{disposition} requires complete exact revision evidence"
            )
        duplicate_of = raw.get("duplicate_of")
        if duplicate_of is not None:
            if isinstance(duplicate_of, bool) or not isinstance(duplicate_of, int):
                raise ValueError("duplicate_of must be a positive PR number")
            if duplicate_of <= 0:
                raise ValueError("duplicate_of must be a positive PR number")
        if disposition == "duplicate":
            if duplicate_of is None:
                raise ValueError("duplicate requires duplicate_of")
            if duplicate_of == number:
                raise ValueError("duplicate cannot reference itself")
            if duplicate_of not in members:
                raise ValueError("duplicate_of must be another PR in the same group")
        elif duplicate_of is not None:
            raise ValueError("duplicate_of is only valid for duplicate dispositions")
        elif raw.get("duplicate_of_revision", raw.get("canonical_revision")) is not None:
            raise ValueError(
                "duplicate_of_revision is only valid for duplicate dispositions"
            )
        target_revision = None
        if duplicate_of is not None:
            target_revision = by_number[duplicate_of]
            supplied_target = raw.get("duplicate_of_revision", raw.get("canonical_revision"))
            if supplied_target is not None:
                if not isinstance(supplied_target, dict):
                    raise ValueError("duplicate_of_revision must be an object")
                provided_target = RevisionEvidence.from_dict({
                    **supplied_target, "pr_number": duplicate_of
                })
                if not _revision_equal(provided_target, target_revision):
                    raise StoreConflictError(
                        "duplicate canonical PR revision is stale",
                        code="revision_conflict", current_repo=repo,
                        current_version=int(data.get("store_version", 0)),
                    )
            if require_complete and not target_revision.evidence_complete:
                raise IncompleteEvidenceError(
                    "duplicate requires complete canonical revision evidence"
                )
        out.append(Disposition(
            repo=repo, pr=number, revision=bound, disposition=disposition,
            reason=reason, duplicate_of=duplicate_of,
            duplicate_of_revision=target_revision, actor=actor,
            decided_at="", event_id="", source=source,
        ))
    return out


def _existing_current_dispositions(
    data: dict[str, Any], repo: str, revisions_by_pr: Mapping[int, RevisionEvidence],
) -> dict[int, Disposition]:
    result: dict[int, Disposition] = {}
    for raw in data.get("dispositions") or []:
        if not isinstance(raw, dict) or _normal_repo(raw.get("repo")) != repo:
            continue
        try:
            item = Disposition.from_dict(raw)
        except (TypeError, ValueError, KeyError):
            continue
        current = revisions_by_pr.get(item.pr)
        if current is not None and _revision_equal(item.revision, current):
            result[item.pr] = item
    return result


def _check_duplicate_cycles(
    incoming: list[Disposition], existing: Mapping[int, Disposition],
) -> None:
    graph = {
        number: item.duplicate_of
        for number, item in existing.items()
        if item.disposition == "duplicate" and item.duplicate_of is not None
    }
    for item in incoming:
        if item.disposition == "duplicate" and item.duplicate_of is not None:
            graph[item.pr] = item.duplicate_of
        else:
            graph.pop(item.pr, None)
    for start in graph:
        seen: set[int] = set()
        cursor: int | None = start
        while cursor in graph:
            if cursor in seen:
                raise ValueError("duplicate dispositions cannot contain cycles")
            seen.add(cursor)
            cursor = graph[cursor]


def _apply_dispositions_unlocked(
    data: dict[str, Any], repo: str, group: Group,
    revisions: list[RevisionEvidence], items: list[Disposition],
    *, actor: str, source: str, require_complete: bool, event_id: str | None = None,
) -> tuple[list[Disposition], str]:
    revisions_by_pr = {item.pr_number: item for item in revisions}
    existing = _existing_current_dispositions(data, repo, revisions_by_pr)
    _check_duplicate_cycles(items, existing)
    event_id = event_id or f"disposition-{uuid.uuid4()}"
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    finalized = [Disposition(
        repo=item.repo, pr=item.pr, revision=item.revision,
        disposition=item.disposition, reason=item.reason,
        duplicate_of=item.duplicate_of,
        duplicate_of_revision=item.duplicate_of_revision,
        actor=actor, decided_at=timestamp, event_id=event_id, source=source,
    ) for item in items]
    rows = [raw for raw in data.get("dispositions") or []
            if not (isinstance(raw, dict) and _normal_repo(raw.get("repo")) == repo
                    and raw.get("pr", raw.get("pr_number")) in {item.pr for item in finalized})]
    rows.extend(item.to_dict() for item in finalized)
    data["dispositions"] = rows
    data.setdefault("disposition_events", []).append({
        "event_id": event_id, "repo": repo, "group_id": group.group_id,
        "actor": actor, "source": source, "decided_at": timestamp,
        "items": [item.to_dict() for item in finalized],
    })
    return finalized, event_id


def save_dispositions(
    items: list[Mapping[str, Any] | Disposition],
    path: Path = DEFAULT_STORE_PATH,
    *,
    repo: str | None = None,
    group_id: str | None = None,
    expected_version: int | None = None,
    expected_snapshot_version: int | None = None,
    idempotency_key: str | None = None,
    actor: str = "human",
    source: str = "human",
    require_complete: bool = True,
) -> list[Disposition]:
    """Atomically persist independent local PR dispositions.

    The lock encloses all validation, exact evidence capture, idempotency, and
    publication.  ``save_dispositions`` never calls ``load_store`` or another
    writer while holding the lock.
    """
    key = _safe_disposition_key(idempotency_key)
    actor = str(actor or "human").strip() or "human"
    source = str(source or "human").strip() or "human"
    normalized_input = [
        item.to_dict() if isinstance(item, Disposition) else item for item in items
    ]
    with _store_lock(path, exclusive=True) as store_path:
        data = _read_store_unlocked(store_path)
        current_version = int(data.get("store_version", 0))
        current_snapshot = int(data.get("snapshot_version", 0))
        active_repo = _normal_repo(data.get("repo"))
        target_repo = _normal_repo(repo) if repo is not None else active_repo
        if not active_repo or target_repo != active_repo:
            raise StoreConflictError("repository does not match this store",
                                     code="repository_conflict", current_repo=active_repo,
                                     current_version=current_version)
        request_identity = {
            "repo": target_repo, "group_id": group_id or "", "items": [dict(item) for item in normalized_input],
            "actor": actor, "source": source,
        }
        if key:
            prior = (data.get("disposition_idempotency") or {}).get(key)
            if isinstance(prior, dict):
                if prior.get("request") != request_identity:
                    raise StoreConflictError("idempotency key was already used for different input",
                                             code="idempotency_key_reused", current_repo=active_repo,
                                             current_version=current_version)
                prior_items = [Disposition.from_dict(item) for item in prior.get("items") or []]
                return prior_items
        if expected_snapshot_version is not None and expected_snapshot_version != current_snapshot:
            raise StoreConflictError("repository snapshot changed", code="stale_snapshot_version",
                                     current_repo=active_repo, current_version=current_version)
        if expected_version is not None and expected_version != current_version:
            raise StoreConflictError("store changed", code="stale_store_version",
                                     current_repo=active_repo, current_version=current_version)
        if not group_id:
            numbers = {
                item.get("pr", item.get("pr_number"))
                for item in normalized_input if isinstance(item, Mapping)
            }
            candidates = [
                raw for raw in data.get("last_groups") or []
                if isinstance(raw, dict) and numbers and numbers.issubset(set(raw.get("pr_numbers") or []))
            ]
            if len(candidates) != 1:
                raise ValueError("group_id is required when PR membership is ambiguous")
            group_id = str(candidates[0].get("group_id") or "")
        group, revisions, _digest, _complete = _group_for_write(data, active_repo, group_id)
        normalized = _normalize_disposition_items(
            data, active_repo, group, revisions, normalized_input,
            actor=actor, source=source, require_complete=require_complete,
        )
        finalized, event_id = _apply_dispositions_unlocked(
            data, active_repo, group, revisions, normalized,
            actor=actor, source=source, require_complete=require_complete,
        )
        if key:
            data.setdefault("disposition_idempotency", {})[key] = {
                "request": request_identity, "event_id": event_id,
                "items": [item.to_dict() for item in finalized],
            }
        _refresh_queue_in_data(data)
        data["store_version"] = _next_version(current_version, field="store_version")
        _commit_store_unlocked(data, store_path)
        return finalized


def _proposal_by_id(data: dict[str, Any], proposal_id: str) -> Proposal:
    matches = [
        Proposal.from_dict(raw)
        for raw in data.get("proposals") or []
        if isinstance(raw, dict) and raw.get("proposal_id") == proposal_id
    ]
    if len(matches) != 1:
        raise KeyError(f"proposal not found: {proposal_id}")
    return matches[0]


def _proposal_request_identity(
    proposal_id: str, action: str, *, items: Any = None,
    canonical_pr: int | None = None, actor: str = "",
) -> dict[str, Any]:
    return {
        "proposal_id": proposal_id, "action": action,
        "items": [
            item.to_dict() if isinstance(item, Disposition) else dict(item)
            for item in items
        ] if isinstance(items, (list, tuple)) else None,
        "canonical_pr": canonical_pr, "actor": actor,
    }


def _check_write_versions(
    data: dict[str, Any], repo: str, *, expected_version: int | None,
    expected_snapshot_version: int | None,
) -> tuple[str, int, int]:
    active_repo = _normal_repo(data.get("repo"))
    current_version = int(data.get("store_version", 0))
    current_snapshot = int(data.get("snapshot_version", 0))
    if not active_repo or _normal_repo(repo) != active_repo:
        raise StoreConflictError("repository does not match this store",
                                 code="repository_conflict", current_repo=active_repo,
                                 current_version=current_version)
    if expected_snapshot_version is not None and expected_snapshot_version != current_snapshot:
        raise StoreConflictError("repository snapshot changed", code="stale_snapshot_version",
                                 current_repo=active_repo, current_version=current_version)
    if expected_version is not None and expected_version != current_version:
        raise StoreConflictError("store changed", code="stale_store_version",
                                 current_repo=active_repo, current_version=current_version)
    return active_repo, current_version, current_snapshot


def draft_proposal(
    *,
    repo: str,
    group_id: str,
    items: list[Mapping[str, Any]],
    canonical_pr: int | None = None,
    path: Path = DEFAULT_STORE_PATH,
    expected_version: int | None = None,
    expected_snapshot_version: int | None = None,
    idempotency_key: str | None = None,
    actor: str = "agent",
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> Proposal:
    """Persist an agent-authored proposal in ``draft`` state only."""
    key = _safe_disposition_key(idempotency_key)
    actor = str(actor or "agent").strip() or "agent"
    normalized_input = [
        item.to_dict() if isinstance(item, Disposition) else item for item in items
    ]
    if not isinstance(provenance, Mapping):
        provenance = {}
    with _store_lock(path, exclusive=True) as store_path:
        data = _read_store_unlocked(store_path)
        active_repo, current_version, current_snapshot = _check_write_versions(
            data, repo, expected_version=None, expected_snapshot_version=None,
        )
        identity = _proposal_request_identity(
            "", "draft", items=normalized_input, canonical_pr=canonical_pr, actor=actor
        )
        identity["repo"] = active_repo
        identity["group_id"] = group_id
        identity["provenance"] = dict(provenance)
        identity["context"] = dict(context or {})
        if key:
            prior = (data.get("proposal_idempotency") or {}).get(key)
            if isinstance(prior, dict):
                if prior.get("request") != identity:
                    raise StoreConflictError("idempotency key was already used for different input",
                                             code="idempotency_key_reused", current_repo=active_repo,
                                             current_version=current_version)
                return _proposal_by_id(data, str(prior.get("proposal_id") or ""))
        if expected_snapshot_version is not None and expected_snapshot_version != current_snapshot:
            raise StoreConflictError("repository snapshot changed", code="stale_snapshot_version",
                                     current_repo=active_repo, current_version=current_version)
        if expected_version is not None and expected_version != current_version:
            raise StoreConflictError("store changed", code="stale_store_version",
                                     current_repo=active_repo, current_version=current_version)
        group, revisions, digest, _complete = _group_for_write(data, active_repo, group_id)
        if canonical_pr is not None:
            if isinstance(canonical_pr, bool) or not isinstance(canonical_pr, int) or canonical_pr not in set(group.pr_numbers):
                raise ValueError("canonical_pr must be a member of the group")
        normalized = _normalize_disposition_items(
            data, active_repo, group, revisions, normalized_input,
            actor=actor, source="agent", require_complete=False,
        )
        if canonical_pr is None:
            duplicate_targets = [item.duplicate_of for item in normalized if item.disposition == "duplicate"]
            canonical_pr = duplicate_targets[0] if duplicate_targets else None
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        proposal_id = f"proposal-{uuid.uuid4()}"
        proposal_context = {
            **dict(context or {}),
            "repo": active_repo,
            "store_version": current_version,
            "snapshot_version": current_snapshot,
            "group_snapshot_digest": digest,
            "group_members": list(group.pr_numbers),
        }
        proposal_provenance = {
            **dict(provenance),
            # Agent input may add provenance fields, but cannot impersonate a
            # human or rewrite when the draft was created.
            "actor": actor, "source": "agent", "created_at": now,
        }
        proposal = Proposal(
            proposal_id=proposal_id, status="draft", repo=active_repo,
            group_id=group_id, canonical_pr=canonical_pr,
            items=tuple(normalized), context=proposal_context,
            provenance=proposal_provenance, created_at=now, updated_at=now,
        )
        data.setdefault("proposals", []).append(proposal.to_dict())
        data.setdefault("proposal_events", []).append({
            "event_id": f"proposal-event-{uuid.uuid4()}", "proposal_id": proposal_id,
            "action": "draft", "actor": actor, "source": "agent", "at": now,
        })
        if key:
            data.setdefault("proposal_idempotency", {})[key] = {
                "request": identity, "proposal_id": proposal_id,
            }
        data["store_version"] = _next_version(current_version, field="store_version")
        _commit_store_unlocked(data, store_path)
        return proposal


def inspect_proposal(
    proposal_id: str, path: Path = DEFAULT_STORE_PATH, *, repo: str | None = None,
) -> Proposal:
    if not isinstance(proposal_id, str) or not proposal_id or len(proposal_id) > 128:
        raise ValueError("proposal_id is invalid")
    data = load_store(path)
    proposal = _proposal_by_id(data, proposal_id)
    if repo is not None and _normal_repo(repo) != _normal_repo(proposal.repo):
        raise StoreConflictError("proposal repository does not match this store",
                                 code="repository_conflict", current_repo=_normal_repo(data.get("repo")),
                                 current_version=int(data.get("store_version", 0)))
    return proposal


# Small compatibility aliases keep the backend seam discoverable without
# introducing a second mutation path.
def save_disposition(item: Mapping[str, Any], path: Path = DEFAULT_STORE_PATH, **kwargs: Any) -> Disposition:
    rows = save_dispositions([item], path=path, **kwargs)
    return rows[0]


create_proposal = draft_proposal
get_proposal = inspect_proposal
record_disposition = save_disposition
apply_dispositions = save_dispositions


def _update_proposal_unlocked(data: dict[str, Any], proposal: Proposal) -> None:
    rows = data.setdefault("proposals", [])
    for index, raw in enumerate(rows):
        if isinstance(raw, dict) and raw.get("proposal_id") == proposal.proposal_id:
            rows[index] = proposal.to_dict()
            return
    raise KeyError(f"proposal not found: {proposal.proposal_id}")


def edit_proposal(
    proposal_id: str,
    *,
    items: list[Mapping[str, Any]],
    path: Path = DEFAULT_STORE_PATH,
    repo: str,
    canonical_pr: int | None = None,
    expected_version: int | None = None,
    expected_snapshot_version: int | None = None,
    idempotency_key: str | None = None,
    actor: str = "human",
) -> Proposal:
    return _transition_proposal(
        proposal_id, "edit", path=path, repo=repo,
        items=items, canonical_pr=canonical_pr,
        expected_version=expected_version,
        expected_snapshot_version=expected_snapshot_version,
        idempotency_key=idempotency_key, actor=actor,
    )


def reject_proposal(
    proposal_id: str, *, path: Path = DEFAULT_STORE_PATH, repo: str,
    reason: str = "rejected by maintainer", expected_version: int | None = None,
    expected_snapshot_version: int | None = None, idempotency_key: str | None = None,
    actor: str = "human",
) -> Proposal:
    return _transition_proposal(
        proposal_id, "reject", path=path, repo=repo, reason=reason,
        expected_version=expected_version, expected_snapshot_version=expected_snapshot_version,
        idempotency_key=idempotency_key, actor=actor,
    )


def accept_proposal(
    proposal_id: str, *, path: Path = DEFAULT_STORE_PATH, repo: str,
    items: list[Mapping[str, Any]] | None = None, canonical_pr: int | None = None,
    expected_version: int | None = None, expected_snapshot_version: int | None = None,
    idempotency_key: str | None = None, actor: str = "human",
) -> Proposal:
    return _transition_proposal(
        proposal_id, "accept", path=path, repo=repo,
        items=items, canonical_pr=canonical_pr,
        expected_version=expected_version,
        expected_snapshot_version=expected_snapshot_version,
        idempotency_key=idempotency_key, actor=actor,
    )


def _transition_proposal(
    proposal_id: str, action: str, *, path: Path, repo: str,
    items: list[Mapping[str, Any]] | None = None, canonical_pr: int | None = None,
    reason: str = "", expected_version: int | None = None,
    expected_snapshot_version: int | None = None, idempotency_key: str | None = None,
    actor: str = "human",
) -> Proposal:
    if action not in {"accept", "edit", "reject"}:
        raise ValueError("invalid proposal action")
    key = _safe_disposition_key(idempotency_key)
    actor = str(actor or "human").strip() or "human"
    with _store_lock(path, exclusive=True) as store_path:
        data = _read_store_unlocked(store_path)
        active_repo, current_version, current_snapshot = _check_write_versions(
            data, repo, expected_version=None,
            expected_snapshot_version=None,
        )
        proposal = _proposal_by_id(data, proposal_id)
        identity = _proposal_request_identity(
            proposal_id, action, items=items, canonical_pr=canonical_pr, actor=actor
        )
        identity["reason"] = reason
        if key:
            prior = (data.get("proposal_idempotency") or {}).get(key)
            if isinstance(prior, dict):
                if prior.get("request") != identity:
                    raise StoreConflictError("idempotency key was already used for different input",
                                             code="idempotency_key_reused", current_repo=active_repo,
                                             current_version=current_version)
                saved = prior.get("proposal")
                return Proposal.from_dict(saved) if isinstance(saved, dict) else proposal
        if expected_snapshot_version is not None and expected_snapshot_version != current_snapshot:
            raise StoreConflictError("repository snapshot changed", code="stale_snapshot_version",
                                     current_repo=active_repo, current_version=current_version)
        if expected_version is not None and expected_version != current_version:
            raise StoreConflictError("store changed", code="stale_store_version",
                                     current_repo=active_repo, current_version=current_version)
        if proposal.repo != active_repo:
            raise StoreConflictError("proposal repository does not match this store",
                                     code="repository_conflict", current_repo=active_repo,
                                     current_version=current_version)
        if action == "accept" and proposal.status not in {"draft", "edited"}:
            if proposal.status == "accepted":
                return proposal
            raise StoreConflictError("proposal is no longer actionable", code="proposal_conflict",
                                     current_repo=active_repo, current_version=current_version)
        if action == "edit" and proposal.status not in {"draft", "edited"}:
            raise StoreConflictError("only a draft proposal can be edited", code="proposal_conflict",
                                     current_repo=active_repo, current_version=current_version)
        if action == "reject" and proposal.status in {"accepted", "rejected"}:
            if proposal.status == "rejected":
                return proposal
            raise StoreConflictError(
                "accepted proposal cannot be rejected", code="proposal_conflict",
                current_repo=active_repo, current_version=current_version,
            )
        group, revisions, digest, _complete = _group_for_write(data, active_repo, proposal.group_id)
        bound_context = proposal.context or {}
        if bound_context.get("group_snapshot_digest") != digest or list(bound_context.get("group_members") or []) != list(group.pr_numbers):
            raise StoreConflictError("proposal targets a stale group snapshot", code="revision_conflict",
                                     current_repo=active_repo, current_version=current_version)
        use_items = list(items if items is not None else proposal.items)
        # Convert model items back to plain dictionaries for the common exact
        # ref/reason/duplicate validation path.
        use_items = [item.to_dict() if isinstance(item, Disposition) else item for item in use_items]
        if action == "reject":
            reason = _reason(reason)
            final_items: list[Disposition] = []
        else:
            final_items = _normalize_disposition_items(
                data, active_repo, group, revisions, use_items,
                actor=actor, source="human", require_complete=action == "accept",
            )
            if canonical_pr is not None and canonical_pr not in set(group.pr_numbers):
                raise ValueError("canonical_pr must be a member of the group")
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        decision_event_id = ""
        persisted_items = final_items
        if action == "accept":
            persisted_items, decision_event_id = _apply_dispositions_unlocked(
                data, active_repo, group, revisions, final_items, actor=actor,
                source="human", require_complete=True,
            )
        next_status = {"accept": "accepted", "edit": "edited", "reject": "rejected"}[action]
        next_items = tuple(persisted_items) if action != "reject" else proposal.items
        updated = Proposal(
            proposal_id=proposal.proposal_id, status=next_status, repo=proposal.repo,
            group_id=proposal.group_id,
            canonical_pr=canonical_pr if canonical_pr is not None else proposal.canonical_pr,
            items=next_items, context={**proposal.context, "accepted_store_version": current_version + 1}
            if action == "accept" else proposal.context,
            provenance={**proposal.provenance, "last_actor": actor, "last_action": action},
            created_at=proposal.created_at, updated_at=now,
            decision_event_id=decision_event_id or proposal.decision_event_id,
        )
        _update_proposal_unlocked(data, updated)
        data.setdefault("proposal_events", []).append({
            "event_id": f"proposal-event-{uuid.uuid4()}", "proposal_id": proposal_id,
            "action": action, "actor": actor, "source": "human", "at": now,
            "reason": reason if action == "reject" else "",
            "items": [item.to_dict() for item in persisted_items],
        })
        if key:
            data.setdefault("proposal_idempotency", {})[key] = {
                "request": identity, "proposal_id": proposal_id,
                "proposal": updated.to_dict(),
            }
        _refresh_queue_in_data(data)
        data["store_version"] = _next_version(current_version, field="store_version")
        _commit_store_unlocked(data, store_path)
        return updated


approve_proposal = accept_proposal


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
            data, repo, group_id, reverify=True
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
    from triage.queue import active_dispositions, build_queue

    groups = [Group.from_dict(g) for g in (data.get("last_groups") or [])]
    prs = [slim_to_pr(p) for p in (data.get("last_prs") or [])]
    repo = _repo_key(data.get("repo"))
    current_revisions = _current_persisted_revisions(data, repo)
    rules = _rules_from_data(data, repo=repo)
    data["last_queue"] = build_queue(
        groups,
        prs,
        rules,
        new_pr_numbers=data.get("last_new_pr_numbers") or [],
        repo=repo,
        dispositions=list(active_dispositions(
            data.get("dispositions") or [], current_revisions, repo,
            evidence_source=data.get("source") or "",
        ).values()),
        current_revisions=current_revisions,
        evidence_source=data.get("source") or "",
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
MAX_STATE_PATHS = 500
MAX_STATE_SHARED_FILES = 200


def _slim_group_for_ui(
    g: dict[str, Any],
    member_paths: list[str] | None = None,
) -> dict[str, Any]:
    out = dict(g)
    out.pop("centroid", None)
    out.pop("member_revisions", None)
    titles = out.get("title_variants") or []
    paths = list(member_paths or out.get("shared_files") or [])
    if len(paths) > MAX_STATE_SHARED_FILES:
        out["shared_files_truncated"] = len(paths) - MAX_STATE_SHARED_FILES
    out["shared_files"] = list(out.get("shared_files") or [])[:MAX_STATE_SHARED_FILES]
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



def _slim_pr_for_ui(pr: dict[str, Any], disposition: Disposition | None = None) -> dict[str, Any]:
    paths = pr.get("paths") or []
    if not paths:
        files = pr.get("files") or []
        if files and isinstance(files[0], dict):
            paths = [f.get("path", "") for f in files if f.get("path")]
        elif files and isinstance(files[0], str):
            paths = list(files)
    out = {
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "user": pr.get("user") or "",
        "label": pr.get("label") or "needs-human",
        "group_id": pr.get("group_id") or "",
        "html_url": pr.get("html_url") or "",
        "paths": paths[:MAX_STATE_PATHS],
        "path_count": len(paths),
        "paths_truncated": max(0, len(paths) - MAX_STATE_PATHS),
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
    if disposition is None:
        out["disposition"] = "pending"
        out["disposition_status"] = "pending"
        out["disposition_reason"] = ""
        out["duplicate_of"] = None
    else:
        out["disposition"] = disposition.disposition
        out["disposition_status"] = disposition.disposition
        out["disposition_reason"] = disposition.reason
        out["duplicate_of"] = disposition.duplicate_of
        out["disposition_revision"] = disposition.revision.to_dict()
    return out


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


def ui_state_from_data(data: dict[str, Any]) -> dict[str, Any]:
    """Build the slim dashboard payload from one already-loaded store."""
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
    from triage.queue import active_dispositions, rule_applies_to_group

    # Validate only groups that have a candidate rule, but build the PR index
    # once so a large store is not rescanned once per decision.  A persisted
    # rule is not projected until its group binding also matches current PR
    # rows; this prevents stale ``last_prs`` from leaving a group Known.
    pr_index = _index_persisted_prs(data)
    active_rule_models = []
    for rule in _rules_from_data(data, repo=repo):
        if any(
            _group_snapshot_is_current(data, repo, group, pr_index=pr_index)
            and rule_applies_to_group(rule, group, repo)
            for group in group_models
            if group.group_id == rule.group_id
        ):
            active_rule_models.append(rule)
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

    current_revisions = _current_persisted_revisions(data, repo)
    active_disposition_by_pr = active_dispositions(
        data.get("dispositions") or [], current_revisions, repo,
        evidence_source=data.get("source") or "",
    )
    queue = build_queue(
        group_models,
        [slim_to_pr(pr) for pr in data.get("last_prs") or []],
        active_rule_models,
        new_pr_numbers=data.get("last_new_pr_numbers") or [],
        repo=repo,
        dispositions=list(active_disposition_by_pr.values()),
        current_revisions=current_revisions,
        evidence_source=data.get("source") or "",
    )
    disposition_rows = [
        active_disposition_by_pr[number].to_dict()
        for number in sorted(active_disposition_by_pr)
    ]
    proposal_rows = []
    for raw in (data.get("proposals") or [])[-200:]:
        if isinstance(raw, dict) and _normal_repo(raw.get("repo")) == repo:
            # The full item/evidence payload is available through proposal
            # inspect; dashboard state only needs bounded status metadata.
            proposal_rows.append({
                "proposal_id": raw.get("proposal_id"),
                "status": raw.get("status"),
                "repo": raw.get("repo"),
                "group_id": raw.get("group_id"),
                "canonical_pr": raw.get("canonical_pr"),
                "created_at": raw.get("created_at"),
                "updated_at": raw.get("updated_at"),
            })
    return {
        "schema_version": int(data.get("schema_version", STORE_SCHEMA_VERSION)),
        "store_version": int(data.get("store_version", 0)),
        "snapshot_version": int(data.get("snapshot_version", 0)),
        "groups": [
            _slim_group_for_ui(g, paths_by_gid.get(g.get("group_id") or ""))
            for g in data.get("last_groups", [])
        ],
        "prs": [
            _slim_pr_for_ui(pr, active_disposition_by_pr.get(pr.get("number")))
            for pr in data.get("last_prs", [])
        ],
        "edges": [],
        "group_edges": [],
        "rules": active_rules,
        "overlap": overlap,
        "source": data.get("source", ""),
        "repo": data.get("repo", ""),
        "queue": queue,
        "new_pr_numbers": data.get("last_new_pr_numbers") or [],
        "dispositions": disposition_rows,
        "proposals": proposal_rows,
    }


def ui_state(path: Path = DEFAULT_STORE_PATH) -> dict[str, Any]:
    """Slim dashboard payload. Full overlap matrices are lazy via overlap_for_group."""
    return ui_state_from_data(load_store(path))
