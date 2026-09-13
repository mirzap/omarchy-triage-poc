"""Safe local repository workspace resolution.

The workspace router is deliberately a read-only filesystem index.  It never
creates the workspace root, a repository directory, a store, or a lock.  The
normal store writer remains responsible for publishing a newly synchronized
workspace.

Namespaced stores live below ``root/<owner>/<repo>/store.json``.  A pre-
namespace ``root.parent/store.json`` is retained as a legacy binding when its
JSON contains a valid repository identity; its bytes and path are never
moved.  Filesystem aliases which could make two repository identities resolve
to the same location are rejected instead of guessed at.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Keep these constraints in step with triage.gh's cache namespace validation.
# They intentionally accept mixed-case user input and normalize it to the
# lower-case key used on disk.  The owner limit is GitHub's documented limit;
# repository names are bounded to a filesystem-safe, GitHub-compatible length.
_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_LIST_LIMIT = 200
_MAX_LEGACY_BYTES = 64 * 1024 * 1024
_STORE_NAME = "store.json"
_DEFAULT_REPO = "omacom/omarchy"


class WorkspaceError(RuntimeError):
    """Base for safe, stable workspace-resolution failures."""

    code = "unavailable"

    def __init__(self, message: str = "workspace resolution failed") -> None:
        super().__init__(message)
        self.message = message


class InvalidRepoError(WorkspaceError):
    """The requested repository is not a safe owner/name identity."""

    code = "invalid_repo"


class WorkspaceConflictError(WorkspaceError):
    """Two filesystem bindings would alias the same repository identity."""

    code = "workspace_conflict"


class WorkspaceUnavailableError(WorkspaceError):
    """The workspace cannot be inspected safely."""

    code = "unavailable"


@dataclass(frozen=True)
class WorkspaceBinding:
    """The immutable result of resolving one repository identity."""

    repo: str
    path: Path
    legacy: bool
    initialized: bool


@dataclass(frozen=True)
class _StatIdentity:
    """The metadata which identifies one published store file."""

    # Keep all of these fields: an atomic replacement changes the inode, while
    # an in-place edit can retain it.  ctime catches an edit which restores the
    # old size and mtime on filesystems with coarse mtime resolution.
    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int


@dataclass(frozen=True)
class _CachedStoreIdentity:
    identity: _StatIdentity
    # Empty means that this is a validated, uninitialized legacy store.  It is
    # deliberately not used for namespaced stores, which must carry an owner
    # and repository identity.
    repo: str


class _StoreIdentityCache:
    """A small, thread-safe cache of validation results, never store data."""

    def __init__(self, limit: int = 128) -> None:
        self._limit = max(1, int(limit))
        self._entries: OrderedDict[Path, _CachedStoreIdentity] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, path: Path, identity: _StatIdentity) -> _CachedStoreIdentity | None:
        with self._lock:
            entry = self._entries.get(path)
            if entry is None:
                return None
            if entry.identity != identity:
                # Replacement invalidation is atomic with respect to readers:
                # no caller can observe the old identity after this point.
                self._entries.pop(path, None)
                return None
            self._entries.move_to_end(path)
            return entry

    def put(self, path: Path, identity: _StatIdentity, repo: str) -> None:
        with self._lock:
            # Assigning one complete immutable entry makes replacement of a
            # path's cached identity atomic for concurrent resolver calls.
            self._entries[path] = _CachedStoreIdentity(identity, repo)
            self._entries.move_to_end(path)
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)

    def invalidate(self, path: Path) -> None:
        with self._lock:
            self._entries.pop(path, None)


def canonical_repo(value: Any) -> str:
    """Validate and normalize an owner/name repository identity.

    Surrounding whitespace or slashes are rejected rather than silently
    stripped.  This matters at the filesystem boundary: an input which looks
    like a path must never be interpreted as a different repository.
    """

    if not isinstance(value, str) or not value or _CONTROL_RE.search(value):
        raise InvalidRepoError("repo must be a safe owner/name identity")
    if value != value.strip() or "\\" in value or value.count("/") != 1:
        raise InvalidRepoError("repo must be a safe owner/name identity")
    owner, name = value.split("/")
    if not _OWNER_RE.fullmatch(owner) or not _REPO_RE.fullmatch(name):
        raise InvalidRepoError("repo must be a safe owner/name identity")
    if name in {".", ".."}:
        raise InvalidRepoError("repo must be a safe owner/name identity")
    return f"{owner.lower()}/{name.lower()}"


def _safe_lstat(path: Path) -> os.stat_result | None:
    """Return an lstat result, converting inspection failures safely."""

    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkspaceUnavailableError("workspace filesystem is unavailable") from exc


def _is_link(stat_result: os.stat_result | None) -> bool:
    # ``stat.S_ISLNK`` is kept in a tiny helper rather than using
    # ``Path.exists``: broken symlinks must be rejected, not treated as
    # missing workspaces.
    return stat_result is not None and stat.S_ISLNK(stat_result.st_mode)


def _is_dir(stat_result: os.stat_result | None) -> bool:
    return stat_result is not None and stat.S_ISDIR(stat_result.st_mode)


def _is_regular(stat_result: os.stat_result | None) -> bool:
    return stat_result is not None and stat.S_ISREG(stat_result.st_mode)


def _reject_root_aliases(raw_root: Path) -> None:
    """Reject a symlink in the configured namespace's parent chain.

    macOS exposes ``/tmp`` and ``/var`` as stable aliases into ``/private``;
    those two system prefixes are harmless and are deliberately accepted.
    Any symlink supplied by the caller below those prefixes is rejected before
    the root is canonicalized.
    """

    absolute = raw_root if raw_root.is_absolute() else Path.cwd() / raw_root
    current = Path(absolute.anchor)
    parts = absolute.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        if index == len(parts) - 1:
            break
        info = _safe_lstat(current)
        if not _is_link(info):
            continue
        if current in {Path("/tmp"), Path("/var")}:
            continue
        raise WorkspaceUnavailableError("workspace root contains a symbolic link")


class WorkspaceRouter:
    """Resolve immutable repository-to-store bindings below one root."""

    def __init__(self, root: Path) -> None:
        raw_root = Path(root).expanduser()
        _reject_root_aliases(raw_root)
        # A root symlink would make the apparent namespace differ from the
        # namespace being inspected.  Parent aliases are canonicalized once;
        # selected owner/repo components are checked again with lstat below.
        root_stat = _safe_lstat(raw_root)
        if _is_link(root_stat):
            raise WorkspaceUnavailableError("workspace root must not be a symbolic link")
        if root_stat is not None and not _is_dir(root_stat):
            raise WorkspaceUnavailableError("workspace root is not a directory")
        try:
            # ``abspath`` normalizes ``.``/``..`` without following symlinks.
            # Keep the caller's stable lexical prefix (notably /tmp vs
            # /private/tmp) so legacy binding paths remain unchanged.
            self.root = Path(os.path.abspath(raw_root))
        except (OSError, ValueError) as exc:
            raise WorkspaceUnavailableError("workspace root is unavailable") from exc
        self._store_identity_cache = _StoreIdentityCache()
        self._default_repo = self._discover_default_repo()

    @property
    def default_repo(self) -> str:
        """The stable startup default (legacy identity, else Omarchy)."""

        return self._default_repo

    @property
    def legacy_path(self) -> Path:
        return self.root.parent / _STORE_NAME

    def _discover_default_repo(self) -> str:
        legacy = self._legacy_identity()
        return legacy or _DEFAULT_REPO

    @staticmethod
    def _stat_identity(info: os.stat_result) -> _StatIdentity:
        return _StatIdentity(
            st_dev=int(info.st_dev),
            st_ino=int(info.st_ino),
            st_size=int(info.st_size),
            st_mtime_ns=int(info.st_mtime_ns),
            st_ctime_ns=int(info.st_ctime_ns),
        )

    @staticmethod
    def _read_and_validate_store(path: Path, info: os.stat_result) -> dict[str, Any]:
        """Read and fully validate one store without acquiring its lock."""

        if info.st_size > _MAX_LEGACY_BYTES:
            raise WorkspaceUnavailableError("workspace metadata is unavailable")
        try:
            with path.open("rb") as handle:
                payload = handle.read(_MAX_LEGACY_BYTES + 1)
            if len(payload) > _MAX_LEGACY_BYTES:
                raise WorkspaceUnavailableError("workspace metadata is unavailable")
            data = json.loads(payload.decode("utf-8"))
        except WorkspaceUnavailableError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceUnavailableError("workspace metadata is unavailable") from exc
        if not isinstance(data, dict):
            raise WorkspaceUnavailableError("workspace metadata is unavailable")

        # The store module owns the schema/model parser.  Keeping this call in
        # one helper ensures legacy and namespaced stores have exactly the same
        # structural validation and avoids an unlocked load_store call that
        # would create a lock or parent directory on a lookup.
        try:
            from triage import store as store_module

            validator = getattr(store_module, "_validate_store", None)
            if not callable(validator):
                raise TypeError("store validation machinery is unavailable")
            validator(data, source=path)
        except Exception as exc:  # noqa: BLE001 - expose one safe resolver code
            raise WorkspaceUnavailableError("workspace metadata is unavailable") from exc
        return data

    def _store_repo_identity(self, path: Path, *, require_repo: bool) -> str | None:
        """Validate one file and return its canonical persisted repo identity.

        The cache stores only the canonical identity and the file's stat tuple.
        Every call rechecks the file, parent directory, and sidecar lock before
        consulting it.  A changed or replaced file therefore cannot reuse a
        prior validation result.
        """

        info = self._validate_managed_files(path.parent)
        if info is None:
            self._store_identity_cache.invalidate(path)
            return None
        identity = self._stat_identity(info)
        cached = self._store_identity_cache.get(path, identity)
        if cached is None:
            data = self._read_and_validate_store(path, info)
            raw_repo = data.get("repo")
            if raw_repo is None or raw_repo == "":
                repo = ""
            else:
                try:
                    repo = canonical_repo(raw_repo)
                except InvalidRepoError as exc:
                    raise WorkspaceUnavailableError(
                        "workspace repository identity is invalid"
                    ) from exc

            # Do not cache a parse which raced an atomic replacement.  The
            # caller receives a safe failure and the next lookup revalidates.
            latest = _safe_lstat(path)
            if latest is None or _is_link(latest) or not _is_regular(latest):
                self._store_identity_cache.invalidate(path)
                raise WorkspaceUnavailableError("workspace store is unavailable")
            if self._stat_identity(latest) != identity:
                self._store_identity_cache.invalidate(path)
                raise WorkspaceUnavailableError("workspace store changed during lookup")
            self._store_identity_cache.put(path, identity, repo)
            cached = _CachedStoreIdentity(identity, repo)
        if require_repo and not cached.repo:
            raise WorkspaceUnavailableError("workspace repository identity is missing")
        return cached.repo or None

    def _legacy_identity(self) -> str | None:
        path = self.legacy_path
        info = _safe_lstat(path)
        if info is None:
            return None
        if _is_link(info) or not _is_regular(info):
            raise WorkspaceUnavailableError("legacy workspace is unavailable")
        # An empty identity is the ordinary, uninitialized store shape and is
        # intentionally not a legacy binding.  Validation itself is shared
        # with namespaced stores by _store_repo_identity.
        return self._store_repo_identity(path, require_repo=False)

    def _root_entries(self) -> list[os.DirEntry[str]]:
        info = _safe_lstat(self.root)
        if info is None:
            return []
        if _is_link(info) or not _is_dir(info):
            raise WorkspaceUnavailableError("workspace root is unavailable")
        try:
            with os.scandir(self.root) as entries:
                return list(entries)
        except OSError as exc:
            raise WorkspaceUnavailableError("workspace root is unavailable") from exc

    @staticmethod
    def _valid_owner(name: str) -> bool:
        return bool(_OWNER_RE.fullmatch(name))

    @staticmethod
    def _valid_name(name: str) -> bool:
        return bool(_REPO_RE.fullmatch(name)) and name not in {".", ".."}

    def _owner_dir(self, owner: str) -> Path | None:
        """Find one case-insensitive owner directory, rejecting aliases."""

        matches: list[os.DirEntry[str]] = []
        for entry in self._root_entries():
            try:
                if entry.is_symlink():
                    raise WorkspaceUnavailableError("workspace namespace contains a symbolic link")
                if not entry.is_dir(follow_symlinks=False):
                    if self._valid_owner(entry.name) and entry.name.lower() == owner:
                        raise WorkspaceUnavailableError("workspace owner path is unavailable")
                    continue
            except OSError as exc:
                raise WorkspaceUnavailableError("workspace namespace is unavailable") from exc
            if not self._valid_owner(entry.name):
                continue
            if entry.name.lower() == owner:
                matches.append(entry)
        if len(matches) > 1:
            raise WorkspaceConflictError("workspace names differ only by case")
        return self.root / matches[0].name if matches else None

    def _repo_dirs(self, owner_path: Path, owner: str) -> list[tuple[str, Path]]:
        """Enumerate valid repository directories under one owner safely."""

        info = _safe_lstat(owner_path)
        if info is None:
            return []
        if _is_link(info) or not _is_dir(info):
            raise WorkspaceUnavailableError("workspace namespace is unavailable")
        try:
            with os.scandir(owner_path) as entries:
                rows = list(entries)
        except OSError as exc:
            raise WorkspaceUnavailableError("workspace namespace is unavailable") from exc
        by_key: dict[str, list[os.DirEntry[str]]] = {}
        for entry in rows:
            try:
                if entry.is_symlink():
                    raise WorkspaceUnavailableError("workspace namespace contains a symbolic link")
                if not entry.is_dir(follow_symlinks=False):
                    if self._valid_name(entry.name):
                        raise WorkspaceUnavailableError("workspace repository path is unavailable")
                    continue
            except OSError as exc:
                raise WorkspaceUnavailableError("workspace namespace is unavailable") from exc
            if not self._valid_name(entry.name):
                continue
            key = entry.name.lower()
            by_key.setdefault(key, []).append(entry)
        result: list[tuple[str, Path]] = []
        for key, matches in by_key.items():
            if len(matches) > 1:
                raise WorkspaceConflictError("workspace names differ only by case")
            result.append((key, owner_path / matches[0].name))
        return result

    def _store_path(self, repo: str) -> Path:
        owner, name = repo.split("/")
        owner_path = self._owner_dir(owner)
        if owner_path is None:
            return self.root / owner / name / _STORE_NAME
        candidates = {candidate_repo: path for candidate_repo, path in self._repo_dirs(owner_path, owner)}
        repo_path = candidates.get(name)
        if repo_path is None:
            return owner_path / name / _STORE_NAME

        # A namespaced store has one exact filename.  A differently-cased
        # spelling would alias on case-insensitive filesystems and is refused.
        store_path = repo_path / _STORE_NAME
        self._validate_managed_files(repo_path)
        return store_path

    @staticmethod
    def _validate_managed_files(repo_path: Path) -> os.stat_result | None:
        """Validate store and sidecar lock paths before any store read/write."""

        directory_info = _safe_lstat(repo_path)
        if directory_info is None:
            # An uninitialized selection is a pure lookup.  In particular,
            # do not ask the store layer to create this directory or a lock.
            return None
        if _is_link(directory_info) or not _is_dir(directory_info):
            raise WorkspaceUnavailableError("workspace namespace is unavailable")
        store_path = repo_path / _STORE_NAME
        info = _safe_lstat(store_path)
        if info is not None and (_is_link(info) or not _is_regular(info)):
            raise WorkspaceUnavailableError("workspace store is unavailable")
        lock_info = _safe_lstat(repo_path / f".{_STORE_NAME}.lock")
        if lock_info is not None and (_is_link(lock_info) or not _is_regular(lock_info)):
            raise WorkspaceUnavailableError("workspace lock is unavailable")
        try:
            with os.scandir(repo_path) as entries:
                for entry in entries:
                    if entry.name.lower() == _STORE_NAME and entry.name != _STORE_NAME:
                        raise WorkspaceConflictError("workspace store names differ only by case")
                    if entry.name.lower() == f".{_STORE_NAME}.lock" and entry.name != f".{_STORE_NAME}.lock":
                        raise WorkspaceConflictError("workspace lock names differ only by case")
        except OSError as exc:
            raise WorkspaceUnavailableError("workspace namespace is unavailable") from exc
        return info

    def resolve(self, repo: Any = None) -> WorkspaceBinding:
        """Resolve ``repo`` without creating any filesystem entry.

        ``repo=None`` uses the startup-stable default.  If a legacy store and
        a namespaced directory represent the same identity, resolution fails
        closed even when the namespaced directory has no store yet.
        """

        requested = self._default_repo if repo is None else canonical_repo(repo)
        legacy_repo = self._legacy_identity()
        namespaced_path = self._store_path(requested)
        namespaced_dir = namespaced_path.parent
        namespaced_dir_info = _safe_lstat(namespaced_dir)
        namespaced_exists = namespaced_dir_info is not None

        # A present namespaced store is a binding, not merely an existence
        # marker.  Validate the complete JSON and require its persisted repo
        # to agree with the canonical path before endpoint dispatch can load
        # any data through the store layer.
        namespaced_repo = self._store_repo_identity(
            namespaced_path, require_repo=True
        )
        if namespaced_repo is not None and namespaced_repo != requested:
            raise WorkspaceConflictError(
                "workspace store repository does not match its namespace"
            )

        if legacy_repo == requested:
            if namespaced_exists:
                raise WorkspaceConflictError("legacy and namespaced workspaces conflict")
            return WorkspaceBinding(requested, self.legacy_path, True, True)

        initialized = namespaced_repo is not None
        return WorkspaceBinding(requested, namespaced_path, False, initialized)

    def list_workspaces(self, limit: int = _MAX_LIST_LIMIT) -> list[dict[str, Any]]:
        """Return at most ``limit`` safe workspace metadata rows.

        Rows are sorted by canonical repository identity and intentionally
        expose no absolute paths.  A legacy row is included alongside
        namespaced rows; duplicate identities fail closed.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("workspace list limit must be a positive integer")
        limit = min(limit, _MAX_LIST_LIMIT)
        legacy_repo = self._legacy_identity()
        rows: dict[str, dict[str, Any]] = {}
        if legacy_repo:
            rows[legacy_repo] = {"repo": legacy_repo, "initialized": True, "legacy": True}

        for owner_entry in self._root_entries():
            try:
                if owner_entry.is_symlink():
                    raise WorkspaceUnavailableError("workspace namespace contains a symbolic link")
                if not owner_entry.is_dir(follow_symlinks=False):
                    continue
            except OSError as exc:
                raise WorkspaceUnavailableError("workspace namespace is unavailable") from exc
            if not self._valid_owner(owner_entry.name):
                continue
            owner = owner_entry.name.lower()
            # Enumerating through _owner_dir also catches case-colliding owner
            # directories before metadata is emitted.
            owner_path = self._owner_dir(owner)
            if owner_path is None:
                continue
            for name, repo_path in self._repo_dirs(owner_path, owner):
                repo = f"{owner}/{name}"
                if repo in rows:
                    raise WorkspaceConflictError("legacy and namespaced workspaces conflict")
                info = self._validate_managed_files(repo_path)
                initialized = info is not None
                if initialized:
                    # Validate initialized entries even when they are only
                    # being advertised.  Otherwise list_workspaces could
                    # publish a path identity whose store data belongs to a
                    # different repository (or is malformed).
                    persisted_repo = self._store_repo_identity(
                        repo_path / _STORE_NAME, require_repo=True
                    )
                    if persisted_repo != repo:
                        raise WorkspaceConflictError(
                            "workspace store repository does not match its namespace"
                        )
                rows[repo] = {"repo": repo, "initialized": initialized, "legacy": False}

        ordered = [rows[key] for key in sorted(rows)]
        return ordered[:limit]


__all__ = [
    "WorkspaceBinding",
    "WorkspaceError",
    "InvalidRepoError",
    "WorkspaceConflictError",
    "WorkspaceUnavailableError",
    "WorkspaceRouter",
    "canonical_repo",
]
