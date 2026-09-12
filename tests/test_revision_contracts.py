"""Offline revision identity and durable JSON transaction contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from triage import gh
from triage.models import ChangedFile, PullRequest, unified_patch_line_counts
from triage.pipeline import run_pipeline
from triage.store import (
    MAX_SAFE_INTEGER,
    IncompleteEvidenceError,
    StoreConflictError,
    StoreCorruptionError,
    StorePathError,
    StoreVersionExhaustedError,
    backup_store,
    decide_group,
    load_store,
    restore_store,
    save_store,
    ui_state,
)


def _pr(number: int, value: str = "new") -> PullRequest:
    return PullRequest(
        number=number,
        title=f"change {number}",
        body="",
        user="fixture",
        changed_files=[
            ChangedFile(
                path=f"config/{number}.conf",
                patch=f"@@ -1 +1 @@\n-old\n+{value}\n",
            )
        ],
        created_at="2026-09-01T00:00:00Z",
    )


def _publish(path: Path, prs: list[PullRequest]) -> dict:
    return run_pipeline(
        prs,
        persist=True,
        source="fixtures",
        repo="acme/widgets",
        store_path=path,
    )


def test_evidence_validation_recomputes_digest_counts_and_booleans() -> None:
    pr = _pr(1)
    pr.evidence_source = "fixtures"
    first = pr.revision_evidence()
    assert first.evidence_complete
    assert (first.additions, first.deletions) == (1, 1)

    pr.content_digest = first.content_digest  # stale caller-supplied cache
    pr.changed_files[0].patch = "@@ -1 +1 @@\n-old\n+different\n"
    assert pr.revision_evidence().content_digest != first.content_digest

    malformed = PullRequest.from_dict(
        {
            **pr.to_dict(),
            "evidence_complete": "false",
            "changed_files": [
                {
                    **pr.changed_files[0].to_dict(),
                    "patch_complete": "false",
                }
            ],
        }
    )
    malformed.evidence_source = "fixtures"
    assert not malformed.revision_evidence().evidence_complete

    mismatch = _pr(2)
    mismatch.evidence_source = "fixtures"
    mismatch.additions = 99
    assert not mismatch.revision_evidence().evidence_complete
    assert mismatch.revision_evidence().content_digest


@pytest.mark.parametrize("bad_count", [True, 1.5, -1])
def test_non_integer_boolean_and_negative_counts_fail_closed(bad_count: object) -> None:
    raw = _pr(9).to_dict()
    raw["evidence_source"] = "fixtures"
    raw["additions"] = bad_count
    raw["changed_files"][0]["additions"] = bad_count
    decoded = PullRequest.from_dict(raw)
    evidence = decoded.revision_evidence()
    assert evidence.evidence_complete is False
    assert decoded.changed_files[0].additions is None
    assert decoded.changed_files[0].patch_complete is False
    direct = _pr(10)
    direct.evidence_source = "fixtures"
    direct.additions = bad_count  # type: ignore[assignment]
    direct.changed_files[0].additions = bad_count  # type: ignore[assignment]
    direct_evidence = direct.revision_evidence()
    assert direct_evidence.evidence_complete is False
    assert direct_evidence.additions is None


def test_unified_patch_hunks_fail_closed_when_truncated_or_malformed() -> None:
    assert unified_patch_line_counts(
        "@@ -1,3 +1,3 @@\n-old\n+new"
    ) == (None, None)
    assert unified_patch_line_counts(
        "@@ -1,3 +1,3 @@\n-old\n+new\n same\n context"
    ) == (1, 1)
    # Inside a hunk, three leading signs are content, not file headers.
    assert unified_patch_line_counts(
        "--- a/file\n+++ b/file\n@@ -1 +1 @@\n---old\n+++new"
    ) == (1, 1)
    assert unified_patch_line_counts(
        "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file"
    ) == (1, 1)
    assert unified_patch_line_counts("@@ -1 +1 @@\n-old") == (None, None)
    assert unified_patch_line_counts("not a unified patch") == (None, None)

    truncated = PullRequest(
        number=7,
        title="truncated",
        body="",
        user="octo",
        created_at="2026-09-01T00:00:00Z",
        changed_files=[
            ChangedFile(
                "a",
                "@@ -1,3 +1,3 @@\n-old\n+new",
                additions=1,
                deletions=1,
            )
        ],
        head_sha="head",
        base_sha="base",
        updated_at="2026-09-01T00:00:00Z",
        additions=1,
        deletions=1,
        evidence_source="github",
    )
    assert not truncated.revision_evidence().evidence_complete


def test_cached_legacy_preview_is_offline_and_unverified(tmp_path: Path) -> None:
    root = tmp_path / "acme" / "widgets"
    (root / "files").mkdir(parents=True)
    pull = {
        "number": 1,
        "title": "cached",
        "body": "body",
        "user": {"login": "octo"},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
        "head": {"sha": "head-1"},
        "base": {"sha": "base-1"},
    }
    (root / "pulls.json").write_text(json.dumps([pull]), encoding="utf-8")
    (root / "files" / "1.json").write_text(
        json.dumps(
            [
                {
                    "filename": "src/a.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ]
        ),
        encoding="utf-8",
    )
    calls = 0

    def forbidden_transport(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("cached mode contacted transport")

    result = gh.fetch_pulls_with_transport(
        "acme",
        "widgets",
        transport=forbidden_transport,
        cache_dir=tmp_path,
        refresh=False,
    )
    assert calls == 0
    assert result[0].changed_files[0].patch.endswith("+new")
    assert result[0].changed_files[0].patch_complete is False
    assert result[0].evidence_complete is False
    assert result[0].cache_snapshot_id == ""
    legacy = gh.cached_pr_evidence(
        "acme", "widgets", 1, tmp_path, snapshot_id=""
    )
    assert legacy is not None
    assert legacy["legacy_unverified"] is True
    assert legacy["snapshot_id"] == ""
    assert legacy["meta"]["evidence_complete"] is False
    assert legacy["files"][0]["patch_complete"] is False

    snapshot = root / "snapshots" / "newer"
    (snapshot / "files").mkdir(parents=True)
    newer_pull = {**pull, "body": "new active body", "head": {"sha": "head-2"}}
    (snapshot / "pulls.json").write_text(json.dumps([newer_pull]), encoding="utf-8")
    (snapshot / "files" / "1.json").write_text(
        json.dumps(
            [
                {
                    "filename": "src/a.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "@@ -1 +1 @@\n-old\n+active",
                }
            ]
        ),
        encoding="utf-8",
    )
    metadata = {
        "schema_version": 2,
        "repository": "acme/widgets",
        "snapshot_id": "newer",
        "files": {
            "1": {
                "updated_at": newer_pull["updated_at"],
                "head_sha": "head-2",
                "base_sha": "base-1",
                "evidence_complete": True,
            }
        },
    }
    (snapshot / gh.FETCH_METADATA_FILE).write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    (root / gh.ACTIVE_SNAPSHOT_FILE).write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    legacy_again = gh.cached_pr_evidence(
        "acme", "widgets", 1, tmp_path, snapshot_id=""
    )
    active = gh.cached_pr_evidence("acme", "widgets", 1, tmp_path)
    assert legacy_again is not None and active is not None
    assert legacy_again["meta"]["body"] == "body"
    assert legacy_again["files"][0]["patch"].endswith("+new")
    assert active["meta"]["body"] == "new active body"
    assert active["files"][0]["patch"].endswith("+active")


def test_cached_evidence_can_pin_an_older_immutable_snapshot(tmp_path: Path) -> None:
    revision = "1"

    def transport(endpoint: str, **_kwargs):
        if "/files?" in endpoint:
            return [
                {
                    "filename": "src/a.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": f"@@ -1 +1 @@\n-old\n+new-{revision}",
                }
            ]
        return [
            {
                "number": 1,
                "title": f"revision {revision}",
                "body": "body",
                "user": {"login": "octo"},
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": f"2026-09-0{revision}T00:00:00Z",
                "head": {"sha": f"head-{revision}"},
                "base": {"sha": "base"},
            }
        ]

    first = gh.fetch_pulls_with_transport(
        "acme",
        "widgets",
        transport=transport,
        cache_dir=tmp_path,
        refresh=True,
    )[0]
    first_snapshot = first.cache_snapshot_id
    assert first_snapshot

    revision = "2"
    second = gh.fetch_pulls_with_transport(
        "acme",
        "widgets",
        transport=transport,
        cache_dir=tmp_path,
        refresh=True,
    )[0]
    assert second.cache_snapshot_id != first_snapshot

    pinned = gh.cached_pr_evidence(
        "acme", "widgets", 1, tmp_path, snapshot_id=first_snapshot
    )
    active = gh.cached_pr_evidence("acme", "widgets", 1, tmp_path)
    pinned_status = gh.cached_sync_status(
        "acme", "widgets", tmp_path, snapshot_id=first_snapshot
    )
    active_status = gh.cached_sync_status("acme", "widgets", tmp_path)
    assert pinned is not None and active is not None
    assert pinned_status is not None and active_status is not None
    assert pinned["meta"]["head_sha"] == "head-1"
    assert pinned["files"][0]["patch"].endswith("+new-1")
    assert active["meta"]["head_sha"] == "head-2"
    assert active["files"][0]["patch"].endswith("+new-2")
    assert pinned_status["snapshot_id"] == first_snapshot
    assert active_status["snapshot_id"] == second.cache_snapshot_id
    assert gh.cached_sync_status(
        "acme", "widgets", tmp_path, snapshot_id=""
    ) is None


def test_refresh_does_not_rewrite_pre_snapshot_cache_files(tmp_path: Path) -> None:
    root = tmp_path / "acme" / "widgets"
    (root / "files").mkdir(parents=True)
    old_pulls = b'[{"legacy":"pull-list"}]\n'
    old_files = b'[{"legacy":"patch-preview"}]\n'
    (root / "pulls.json").write_bytes(old_pulls)
    (root / "files" / "1.json").write_bytes(old_files)

    def transport(endpoint: str, **_kwargs):
        if "/files?" in endpoint:
            return [
                {
                    "filename": "src/a.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ]
        return [
            {
                "number": 1,
                "title": "fresh",
                "body": "body",
                "user": {"login": "octo"},
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-02T00:00:00Z",
                "head": {"sha": "head-2"},
                "base": {"sha": "base"},
            }
        ]

    refreshed = gh.fetch_pulls_with_transport(
        "acme",
        "widgets",
        transport=transport,
        cache_dir=tmp_path,
        refresh=True,
    )
    assert refreshed[0].cache_snapshot_id
    assert (root / "pulls.json").read_bytes() == old_pulls
    assert (root / "files" / "1.json").read_bytes() == old_files


def test_changed_content_revision_or_membership_returns_to_pending(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    first = _publish(path, [_pr(1)])
    gid = first["groups"][0].group_id
    state = ui_state(path)
    decide_group(
        gid,
        "approve",
        path,
        expected_repo="acme/widgets",
        expected_version=state["store_version"],
        idempotency_key="first-review",
        actor="tester",
    )
    assert ui_state(path)["queue"]["known"] == [gid]

    changed = _publish(path, [_pr(1, "opposite")])
    assert changed["queue"]["known"] == []
    assert changed["queue"]["needs_you"]
    stored = load_store(path)
    assert stored["last_prs"][0]["files"][0]["patch"].endswith("+opposite\n")
    assert "body" in stored["last_prs"][0]
    event = stored["decision_events"][0]
    assert event["revisions"][0]["content_digest"]
    assert event["revisions"][0]["additions"] == 1
    assert event["revisions"][0]["deletions"] == 1

    expanded = _publish(path, [_pr(1, "opposite"), _pr(2)])
    assert expanded["queue"]["known"] == []
    assert set(expanded["new_pr_numbers"]) == {2}


def test_stale_version_and_idempotent_decision(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    result = _publish(path, [_pr(1)])
    gid = result["groups"][0].group_id
    initial = ui_state(path)
    rule = decide_group(
        gid,
        "approve",
        path,
        expected_repo="acme/widgets",
        expected_version=initial["store_version"],
        idempotency_key="request-1",
        actor="tester",
    )
    after = ui_state(path)
    duplicate = decide_group(
        gid,
        "approve",
        path,
        expected_repo="acme/widgets",
        expected_version=initial["store_version"],
        idempotency_key="request-1",
        actor="tester",
    )
    assert duplicate.decision_event_id == rule.decision_event_id
    assert ui_state(path)["store_version"] == after["store_version"]
    assert len(load_store(path)["decision_events"]) == 1

    with pytest.raises(StoreConflictError) as stale:
        decide_group(
            gid,
            "reject",
            path,
            expected_repo="acme/widgets",
            expected_version=initial["store_version"],
            idempotency_key="request-2",
        )
    assert stale.value.code == "stale_store_version"


def test_subprocess_decisions_do_not_lose_updates(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    result = _publish(path, [_pr(1), _pr(2)])
    gids = [group.group_id for group in result["groups"]]
    assert len(gids) == 2
    code = """
import sys
from pathlib import Path
from triage.store import decide_group
decide_group(sys.argv[2], 'reject', Path(sys.argv[1]), idempotency_key=sys.argv[3], actor='child')
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(path), gid, f"child-{gid}"],
            env=env,
        )
        for gid in gids
    ]
    assert [process.wait(timeout=10) for process in processes] == [0, 0]
    stored = load_store(path)
    assert {event["group_id"] for event in stored["decision_events"]} == set(gids)
    assert set(ui_state(path)["queue"]["junk"]) == set(gids)


def test_corruption_is_explicit_and_restore_is_validated_and_monotonic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.json"
    _publish(path, [_pr(1)])
    backup = backup_store(path)
    old_version = load_store(path)["store_version"]
    path.write_text('{"interrupted":', encoding="utf-8")
    with pytest.raises(StoreCorruptionError):
        load_store(path)

    previous = restore_store(path, backup)
    restored = load_store(path)
    assert restored["store_version"] > old_version
    assert restored["last_pr_numbers"] == [1]
    assert previous.read_text(encoding="utf-8") == '{"interrupted":'
    json.loads(path.read_text(encoding="utf-8"))

    future = tmp_path / "future.json"
    future.write_text('{"schema_version":999}', encoding="utf-8")
    with pytest.raises(StoreCorruptionError, match="unsupported schema_version"):
        load_store(future)


def test_decision_rechecks_group_against_current_persisted_prs(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    result = _publish(path, [_pr(1)])
    group_id = result["groups"][0].group_id
    data = load_store(path)
    raw_group = data["last_groups"][0]
    raw_group["member_revisions"][0]["content_digest"] = "forged"
    from triage.models import RevisionEvidence, revision_snapshot_digest

    forged = [
        RevisionEvidence.from_dict(item) for item in raw_group["member_revisions"]
    ]
    raw_group["snapshot_digest"] = revision_snapshot_digest(forged)
    save_store(data, path, expected_version=data["store_version"])
    before = path.read_bytes()
    with pytest.raises(StoreCorruptionError, match="does not match"):
        decide_group(group_id, "approve", path)
    assert path.read_bytes() == before
    assert load_store(path)["decision_events"] == []

    duplicate = load_store(path)
    duplicate["last_groups"][0]["pr_numbers"] = [1, 1]
    save_store(duplicate, path, expected_version=duplicate["store_version"])
    duplicate_before = path.read_bytes()
    with pytest.raises(StoreCorruptionError, match="membership"):
        decide_group(group_id, "reject", path)
    assert path.read_bytes() == duplicate_before


def test_unbound_legacy_group_decision_is_actionable_and_non_mutating(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.json"
    legacy = {
        "source": "gh",
        "repo": "omacom/omarchy",
        "last_prs": [
            {
                "number": 1,
                "title": "legacy",
                "user": "octo",
                "paths": ["a"],
                "group_id": "G001",
            }
        ],
        "last_groups": [
            {
                "group_id": "G001",
                "pr_numbers": [1],
                "fingerprints": [],
                "shared_files": ["a"],
                "title_variants": ["legacy"],
                "suggested_decision": "unique",
                "centroid": [],
                "file_set_signature": "legacy",
            }
        ],
        "trusted_rules": [],
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert load_store(path)["store_version"] == 0
    before = path.read_bytes()
    with pytest.raises(IncompleteEvidenceError, match="reload Cached or Refresh"):
        decide_group("G001", "reject", path)
    assert path.read_bytes() == before


def test_store_leaf_symlinks_are_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "actual.json"
    _publish(target, [_pr(1)])
    before = target.read_bytes()
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)

    for operation in (
        lambda: load_store(alias),
        lambda: save_store(load_store(target), alias),
        lambda: backup_store(alias),
        lambda: backup_store(target, alias),
        lambda: restore_store(alias, target),
        lambda: restore_store(target, alias),
    ):
        with pytest.raises(StorePathError, match="symbolic link"):
            operation()
    assert alias.is_symlink()
    assert target.read_bytes() == before

    recovery_target = tmp_path / "unrelated.json"
    recovery_target.write_text("do not replace", encoding="utf-8")
    target.with_name("actual.json.bak").symlink_to(recovery_target)
    data = load_store(target)
    with pytest.raises(StorePathError, match="recovery path"):
        save_store(data, target, expected_version=data["store_version"])
    assert target.read_bytes() == before
    assert recovery_target.read_text(encoding="utf-8") == "do not replace"


def test_version_exhaustion_never_replaces_store_or_restore_target(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.json"
    _publish(path, [_pr(1)])
    exhausted = load_store(path)
    exhausted["store_version"] = MAX_SAFE_INTEGER
    path.write_text(json.dumps(exhausted), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(StoreVersionExhaustedError, match="maximum"):
        save_store(exhausted, path)
    assert path.read_bytes() == before

    target = tmp_path / "target.json"
    _publish(target, [_pr(2)])
    target_before = target.read_bytes()
    with pytest.raises(StoreVersionExhaustedError, match="maximum"):
        restore_store(target, path)
    assert target.read_bytes() == target_before
    assert not target.with_name("target.json.pre-restore").exists()

    snapshot_path = tmp_path / "snapshot-max.json"
    _publish(snapshot_path, [_pr(3)])
    snapshot_max = load_store(snapshot_path)
    snapshot_max["snapshot_version"] = MAX_SAFE_INTEGER
    snapshot_path.write_text(json.dumps(snapshot_max), encoding="utf-8")
    snapshot_before = snapshot_path.read_bytes()
    with pytest.raises(StoreVersionExhaustedError, match="snapshot_version"):
        _publish(snapshot_path, [_pr(3, "still-current")])
    assert snapshot_path.read_bytes() == snapshot_before


def test_older_pipeline_snapshot_cannot_replace_newer_publication(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    _publish(path, [_pr(1)])
    stale_snapshot = load_store(path)["snapshot_version"]
    _publish(path, [_pr(1), _pr(2)])

    from triage.models import Group
    from triage.store import save_run_state

    with pytest.raises(StoreConflictError) as stale:
        save_run_state(
            [Group(group_id="G999", pr_numbers=[999], repo="acme/widgets")],
            [999],
            repo="acme/widgets",
            path=path,
            expected_snapshot_version=stale_snapshot,
        )
    assert stale.value.code == "stale_snapshot_version"
    assert load_store(path)["last_pr_numbers"] == [1, 2]


def test_pipeline_recomputes_after_overlapping_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import triage.pipeline as pipeline_module

    path = tmp_path / "store.json"
    original = _publish(path, [_pr(1)])
    old_gid = original["groups"][0].group_id
    real_save = pipeline_module.save_run_state
    injected = False

    def overlap(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            decide_group(old_gid, "approve", path, idempotency_key="overlap")
        return real_save(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "save_run_state", overlap)
    refreshed = _publish(path, [_pr(1, "changed-during-refresh")])
    assert injected
    assert len(load_store(path)["decision_events"]) == 1
    assert refreshed["queue"]["known"] == []
    assert refreshed["queue"]["needs_you"]
