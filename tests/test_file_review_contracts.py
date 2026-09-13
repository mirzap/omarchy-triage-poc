"""File-level review contracts: independence, identity, and draft isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from triage.models import ChangedFile, PullRequest
from triage.pipeline import run_pipeline
from triage import mcp_server, service
from triage.store import (
    StoreConflictError,
    UnknownReviewPathError,
    adopt_draft_finding,
    draft_file_review,
    file_review_state,
    load_store,
    save_dispositions,
    save_file_finding,
    save_file_review,
    ui_state,
)

REPO = "acme/widgets"
COMPLETE = "src/complete.py"
MISSING = "src/missing.bin"


def _pr(number: int) -> PullRequest:
    """One PR whose second file deliberately has no patch evidence."""
    return PullRequest(
        number=number,
        title=f"change {number}",
        body="",
        user="fixture",
        changed_files=[
            ChangedFile(
                path=COMPLETE,
                patch="@@ -1 +1 @@\n-old\n+new\n",
                additions=1,
                deletions=1,
            ),
            ChangedFile(path=MISSING, patch="", patch_complete=False),
        ],
        created_at="2026-09-01T00:00:00Z",
        evidence_source="fixtures",
    )


def _publish(store: Path, prs: list[PullRequest]) -> dict:
    return run_pipeline(
        prs, persist=True, source="fixtures", repo=REPO, store_path=store,
        incremental=False,
    )


def _revision(store: Path, number: int) -> dict:
    return file_review_state(store_path=store, repo=REPO, pr=number)["revision"]


def test_file_review_is_independent_of_the_pull_request_decision(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store.json"
    _publish(store, [_pr(1), _pr(2)])
    revision = _revision(store, 1)

    save_file_review(
        store_path=store, repo=REPO, pr=1, path=COMPLETE, reviewed=True,
        revision=revision, actor="reviewer",
    )
    state = ui_state(store)
    row = next(item for item in state["prs"] if item["number"] == 1)
    # A file review records coverage only. It never becomes a decision.
    assert row["review_counts"]["files_reviewed"] == 1
    assert row["disposition"] == "pending"
    assert row["disposition_stale"] is False
    assert not load_store(store)["dispositions"]

    save_dispositions(
        [{"pr": 1, "disposition": "reject", "reason": "superseded",
          "revision": revision}],
        path=store, repo=REPO, actor="reviewer",
    )
    after = file_review_state(store_path=store, repo=REPO, pr=1)
    # And a PR decision never marks a file reviewed.
    assert after["coverage"]["human_reviewed"] == 1
    assert after["coverage"]["files_total"] == 2
    decided = next(item for item in ui_state(store)["prs"] if item["number"] == 1)
    assert decided["disposition"] == "reject"
    assert decided["review_counts"]["files_reviewed"] == 1


def test_missing_patch_blocks_review_but_allows_a_finding(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    _publish(store, [_pr(1)])
    revision = _revision(store, 1)

    # A complete file stays reviewable even though a sibling file has no patch.
    save_file_review(
        store_path=store, repo=REPO, pr=1, path=COMPLETE, reviewed=True,
        revision=revision, actor="reviewer",
    )
    with pytest.raises(Exception) as missing:
        save_file_review(
            store_path=store, repo=REPO, pr=1, path=MISSING, reviewed=True,
            revision=revision, actor="reviewer",
        )
    assert "complete patch evidence" in str(missing.value)

    result = save_file_finding(
        store_path=store, repo=REPO, pr=1, path=MISSING, severity="major",
        title="no patch evidence", explanation="the provider returned no bytes",
        revision=revision, actor="reviewer",
    )
    assert result["finding"]["about_missing_patch"] is True


def test_wrong_repository_path_and_stale_revision_are_rejected(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store.json"
    _publish(store, [_pr(1)])
    revision = _revision(store, 1)

    with pytest.raises(StoreConflictError) as repo_conflict:
        save_file_review(
            store_path=store, repo="other/repo", pr=1, path=COMPLETE,
            reviewed=True, revision=revision,
        )
    assert repo_conflict.value.code == "repository_conflict"

    with pytest.raises(UnknownReviewPathError):
        save_file_review(
            store_path=store, repo=REPO, pr=1, path="src/not-in-this-pr.py",
            reviewed=True, revision=revision,
        )

    stale = {**revision, "content_digest": "0" * 64}
    with pytest.raises(StoreConflictError) as revision_conflict:
        save_file_review(
            store_path=store, repo=REPO, pr=1, path=COMPLETE, reviewed=True,
            revision=stale,
        )
    assert revision_conflict.value.code == "revision_conflict"


def test_idempotency_key_is_bound_to_the_submitted_revision(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    _publish(store, [_pr(1)])
    revision = _revision(store, 1)
    arguments = {
        "store_path": store, "repo": REPO, "pr": 1, "path": COMPLETE,
        "reviewed": True, "actor": "reviewer", "idempotency_key": "review-key-0001",
    }

    first = save_file_review(revision=revision, **arguments)
    replay = save_file_review(revision=revision, **arguments)
    assert replay == first

    with pytest.raises(StoreConflictError) as reuse:
        save_file_review(revision={**revision, "head_sha": "deadbeef"}, **arguments)
    assert reuse.value.code == "idempotency_key_reused"


def test_agent_drafts_are_isolated_until_a_human_adopts_them(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store.json"
    _publish(store, [_pr(1)])
    revision = _revision(store, 1)

    drafted = draft_file_review(
        store_path=store, repo=REPO, pr=1, revision=revision, actor="agent-1",
        findings=[{
            "path": COMPLETE, "severity": "minor", "title": "naming",
            "explanation": "the new name reads oddly",
        }],
        coverage=[
            {"path": COMPLETE, "status": "inspected"},
            {"path": MISSING, "status": "missing", "note": "no patch bytes"},
        ],
    )["draft"]

    data = load_store(store)
    # Drafting stores no human review and no decision of any kind.
    assert data["file_reviews"] == []
    assert data["file_findings"] == []
    assert data["dispositions"] == []

    state = file_review_state(store_path=store, repo=REPO, pr=1)
    assert state["coverage"]["agent_inspected"] == 1
    assert state["coverage"]["agent_missing"] == 1
    # Agent inspection is never counted as human review.
    assert state["coverage"]["human_reviewed"] == 0
    assert state["findings"] == []

    finding_id = drafted["findings"][0]["draft_finding_id"]
    adopted = adopt_draft_finding(
        drafted["draft_id"], finding_id, "accept", store_path=store, repo=REPO,
        actor="reviewer",
    )
    assert adopted["finding"]["origin"] == "agent_draft"
    assert adopted["finding"]["author"] == "agent-1"
    assert adopted["finding"]["adopted_by"] == "reviewer"

    after = load_store(store)
    # Adoption creates a finding and nothing else: no review, no disposition.
    assert len(after["file_findings"]) == 1
    assert after["file_reviews"] == []
    assert after["dispositions"] == []


def test_legacy_store_and_pipeline_upsert_preserve_review_records(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store.json"
    _publish(store, [_pr(1)])
    revision = _revision(store, 1)
    save_file_review(
        store_path=store, repo=REPO, pr=1, path=COMPLETE, reviewed=True,
        revision=revision, actor="reviewer",
    )
    save_file_finding(
        store_path=store, repo=REPO, pr=1, path=COMPLETE, severity="nit",
        title="spacing", revision=revision, actor="reviewer",
    )

    # An ordinary pipeline run republishes last_prs and must not disturb the
    # additive review arrays that live beside it.
    _publish(store, [_pr(1)])
    data = load_store(store)
    assert len(data["file_reviews"]) == 1
    assert len(data["file_findings"]) == 1
    assert file_review_state(
        store_path=store, repo=REPO, pr=1
    )["coverage"]["human_reviewed"] == 1


def test_old_store_without_review_fields_loads_unchanged(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    _publish(store, [_pr(1)])
    raw = load_store(store)
    for key in ("file_reviews", "file_findings", "file_coverage",
                "file_review_drafts", "file_review_events"):
        raw.pop(key, None)
    raw.pop("file_review_idempotency", None)
    store.write_text(json.dumps(raw), encoding="utf-8")

    reloaded = load_store(store)
    assert reloaded["file_reviews"] == []
    assert reloaded["file_findings"] == []
    assert reloaded["file_review_idempotency"] == {}
    # The legacy snapshot itself is untouched: no PR, group, or decision data
    # is dropped when the additive arrays are materialized.
    assert [item["number"] for item in reloaded["last_prs"]] == [1]
    assert reloaded["last_groups"] == raw["last_groups"]


def test_agent_tool_surface_cannot_review_adopt_or_decide() -> None:
    """The exposed agent contract stays read-only plus two draft-only writes."""
    read_names = [tool["name"] for tool in service.TOOL_DEFINITIONS]
    assert "get_file_review" in read_names
    assert all(tool["read_only"] is True for tool in service.TOOL_DEFINITIONS)
    assert read_names == list(service.READ_OPERATIONS)
    assert list(mcp_server.READ_OPERATIONS) == read_names

    draft_names = [tool["name"] for tool in service.DRAFT_TOOL_DEFINITIONS]
    assert draft_names == ["propose_triage", "propose_file_review"]
    assert list(mcp_server.DRAFT_OPERATIONS) == draft_names
    for tool in service.DRAFT_TOOL_DEFINITIONS:
        assert tool["read_only"] is False
        assert tool["draft_only"] is True

    exposed = set(read_names) | set(draft_names)
    # No agent-reachable tool can mark a file reviewed, adopt a drafted
    # finding, or record a pull-request decision.
    for forbidden in ("save_file_review", "adopt_draft_finding", "accept_proposal",
                      "save_dispositions", "decide_group", "update_file_finding"):
        assert forbidden not in exposed
    assert set(mcp_server.DRAFT_ROUTES) == set(draft_names)


def test_findings_and_drafts_page_independently(tmp_path: Path) -> None:
    """No single response has to carry every finding or every drafted body."""
    store = tmp_path / "store.json"
    _publish(store, [_pr(1)])
    revision = _revision(store, 1)
    for index in range(3):
        save_file_finding(
            store_path=store, repo=REPO, pr=1, path=COMPLETE, severity="nit",
            title=f"finding {index}", revision=revision, actor="reviewer",
        )
    drafted = draft_file_review(
        store_path=store, repo=REPO, pr=1, revision=revision, actor="agent-1",
        findings=[
            {"path": COMPLETE, "severity": "minor", "title": f"draft {index}",
             "explanation": "detail"}
            for index in range(3)
        ],
    )["draft"]

    first = file_review_state(
        store_path=store, repo=REPO, pr=1, finding_page=1, finding_page_size=2,
    )
    assert len(first["findings"]) == 2
    assert first["next_finding_page"] == 2
    # Draft summaries carry counters only; bodies need an explicit draft_id.
    assert first["draft_findings"] == []
    assert first["drafts"][0]["finding_count"] == 3

    second = file_review_state(
        store_path=store, repo=REPO, pr=1, finding_page=2, finding_page_size=2,
    )
    assert len(second["findings"]) == 1
    assert second["next_finding_page"] is None
    assert not {row["finding_id"] for row in first["findings"]} & {
        row["finding_id"] for row in second["findings"]
    }

    bodies = file_review_state(
        store_path=store, repo=REPO, pr=1, draft_id=drafted["draft_id"],
        draft_finding_page=1, draft_finding_page_size=2,
    )
    assert len(bodies["draft_findings"]) == 2
    assert bodies["next_draft_finding_page"] == 2


def test_focus_path_scopes_findings_without_rewriting_the_page(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store.json"
    _publish(store, [_pr(1)])
    revision = _revision(store, 1)
    save_file_finding(
        store_path=store, repo=REPO, pr=1, path=MISSING, severity="major",
        title="no patch", revision=revision, actor="reviewer",
    )
    scoped = file_review_state(
        store_path=store, repo=REPO, pr=1, path=MISSING, page=1, page_size=1,
    )
    # A focused read reports where the file lives but leaves paging monotone.
    assert scoped["page"] == 1
    assert scoped["focus_path"] == MISSING
    assert scoped["focus_page"] == 2
    assert [row["path"] for row in scoped["findings"]] == [MISSING]

    with pytest.raises(UnknownReviewPathError):
        file_review_state(store_path=store, repo=REPO, pr=1, path="src/absent.py")
