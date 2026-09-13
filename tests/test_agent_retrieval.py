"""Agent evidence stays compact, traversable, and lossless without network IO."""

import json

import pytest

from triage import service
from triage.models import ChangedFile, PullRequest
from triage.pipeline import run_pipeline
from triage.service import WorkspaceService
from triage.store import (
    draft_proposal,
    edit_proposal,
    reject_proposal,
    save_file_finding,
    save_file_review,
)


def test_agent_retrieval_preserves_all_members_files_description_and_patch(tmp_path):
    path = tmp_path / "store.json"
    body = "description " * 3000 + "END OF DESCRIPTION"
    patch = "@@ -1 +1 @@\n-old\n+" + "😀" * 5000 + "\n"
    files = [ChangedFile(path=f"config/{n}.conf", patch=patch if n == 0 else
                         "@@ -1 +1 @@\n-old\n+new\n") for n in range(22)]
    prs = [PullRequest(number=n, title=f"Candidate {n}", body=body, user="fixture",
                       created_at="2026-09-13T00:00:00Z",
                       changed_files=files, evidence_source="fixtures") for n in range(1, 13)]
    run_pipeline(prs, persist=True, source="fixtures", repo="acme/widgets", store_path=path)
    service = WorkspaceService(path)
    groups = service.read("list_groups", {"repo": "acme/widgets"})
    group_id = groups["data"]["groups"][0]["group_id"]
    first = service.read("get_group", {"repo": "acme/widgets", "group_id": group_id})
    assert len(json.dumps(first).encode()) < 25_000
    assert all("paths" not in item for item in first["data"]["dispositions"].values())
    assert not first["retrieval"]["complete"]
    calls = first["retrieval"]["continuations"]
    members_call = next(call for call in calls if "member_page" in call["args"])
    more = service.read(members_call["tool"], members_call["args"])
    assert set(first["data"]["member_numbers"] + more["data"]["member_numbers"]) == set(range(1, 13))
    assert {r["pr_number"] for r in more["data"]["revision_refs"]} == set(more["data"]["member_numbers"])
    files_call = next(call for call in calls if "shared_file_page" in call["args"])
    more_files = service.read(files_call["tool"], files_call["args"])
    assert len(first["data"]["shared_files"] + more_files["data"]["shared_files"]) == 22
    pr = service.read("get_pr", {"repo": "acme/widgets", "pr": 1})
    assert pr["data"]["body"] == body
    assert pr["data"]["files_returned"] == 20
    assert pr["data"]["files_remaining"] == 2
    assert pr["data"]["missing_patch_count"] == 0
    assert pr["data"]["evidence_complete"] is True
    assert pr["retrieval"]["complete"] is False
    continuation = pr["retrieval"]["continuations"][0]
    last = service.read(continuation["tool"], continuation["args"])
    assert len(pr["data"]["files"] + last["data"]["files"]) == 22
    assert last["data"]["files_remaining"] == 0
    args = {"repo": "acme/widgets", "pr": 1, "path": "config/0.conf"}
    chunks = []
    while True:
        result = service.read("read_patch", args)
        chunks.append(result["data"]["patch"])
        if result["retrieval"]["complete"]:
            break
        args = result["retrieval"]["continuations"][0]["args"]
        assert "expected_store_version" in args and "expected_snapshot_version" in args
    assert "".join(chunks) == patch


def test_proposal_discovery_and_file_review_history_are_paged_and_pinned(tmp_path):
    path = tmp_path / "store.json"
    pr = PullRequest(
        number=1, title="Candidate", body="", user="fixture",
        created_at="2026-09-13T00:00:00Z",
        changed_files=[ChangedFile(path="src/app.py", patch="@@ -1 +1 @@\n-old\n+new\n")],
    )
    run_pipeline([pr], persist=True, source="fixtures", repo="acme/widgets", store_path=path)
    service = WorkspaceService(path)
    group_id = service.read("list_groups", {"repo": "acme/widgets"})["data"]["groups"][0]["group_id"]
    revision = service.read("get_pr", {"repo": "acme/widgets", "pr": 1})["data"]["revision"]
    item = {"pr": 1, "disposition": "pending", "reason": "needs review", "revision": revision}
    for index in range(2):
        draft_proposal(
            repo="acme/widgets", group_id=group_id, items=[{**item, "reason": f"reason {index}"}],
            path=path,
        )

    # Exercise the real proposal writers. Future events must carry repository
    # provenance without a client-side repair step.
    stored = json.loads(path.read_text(encoding="utf-8"))
    first_proposal_id = stored["proposals"][0]["proposal_id"]
    second_proposal_id = stored["proposals"][1]["proposal_id"]
    edit_proposal(
        first_proposal_id, items=[{**item, "reason": "edited reason"}],
        path=path, repo="acme/widgets", actor="reviewer",
    )
    reject_proposal(
        first_proposal_id, path=path, repo="acme/widgets",
        reason="rejected reason", actor="reviewer",
    )
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert all(event.get("repo") == "acme/widgets" for event in stored["proposal_events"])
    stored["proposal_events"].append({
        "event_id": "foreign-collision", "proposal_id": first_proposal_id,
        "repo": "other/repo", "action": "accept", "at": "9999-01-01T00:00:00Z",
    })
    path.write_text(json.dumps(stored), encoding="utf-8")

    first = service.read("list_proposals", {
        "repo": "acme/widgets", "page": 1, "page_size": 1,
    })
    assert first["data"]["proposals"][0]["state_kind"] == "current_proposal"
    assert first["page"]["next_page"] == 2
    continuation = first["retrieval"]["continuations"][0]["args"]
    assert continuation["expected_store_version"] == first["context"]["store_version"]
    second = service.read("list_proposals", continuation)
    assert second["data"]["proposals"][0]["proposal_id"] != first["data"]["proposals"][0]["proposal_id"]

    collision_checked = service.read("get_proposal", {
        "repo": "acme/widgets", "proposal_id": first_proposal_id,
    })
    assert collision_checked["data"]["current_state"]["event_count"] == 3
    assert all(event["event_id"] != "foreign-collision" for event in collision_checked["data"]["events"])

    # A legacy unscoped event remains readable only when its proposal ID is
    # unique to the active repository.
    stored = json.loads(path.read_text(encoding="utf-8"))
    legacy_event = next(event for event in stored["proposal_events"] if event["proposal_id"] == second_proposal_id)
    legacy_event.pop("repo")
    path.write_text(json.dumps(stored), encoding="utf-8")
    legacy = service.read("get_proposal", {
        "repo": "acme/widgets", "proposal_id": second_proposal_id,
    })
    assert legacy["data"]["current_state"]["event_count"] == 1
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["proposals"].append({
        **stored["proposals"][1], "repo": "other/repo",
    })
    path.write_text(json.dumps(stored), encoding="utf-8")
    legacy_collision = service.read("get_proposal", {
        "repo": "acme/widgets", "proposal_id": second_proposal_id,
    })
    assert legacy_collision["data"]["current_state"]["event_count"] == 0
    collision_list = service.read("list_proposals", {"repo": "acme/widgets"})
    collision_summary = next(
        row for row in collision_list["data"]["proposals"]
        if row["proposal_id"] == second_proposal_id
    )
    assert collision_summary["event_count"] == 0

    proposal = service.read("get_proposal", {
        "repo": "acme/widgets",
        "proposal_id": first["data"]["proposals"][0]["proposal_id"],
    })
    assert proposal["data"]["state_kind"] == "current_proposal_with_events"
    assert proposal["data"]["proposal"]["items"][0]["revision"] == revision
    assert proposal["data"]["events"][0]["history_kind"] == "proposal_event"
    assert proposal["data"]["current_state"]["event_count"] == (
        3 if proposal["data"]["proposal"]["proposal_id"] == first_proposal_id else 1
    )
    assert all(event["event_id"] != "foreign-collision" for event in proposal["data"]["events"])

    save_file_review(
        store_path=path, repo="acme/widgets", pr=1, path="src/app.py",
        reviewed=True, revision=revision, actor="reviewer",
    )
    save_file_finding(
        store_path=path, repo="acme/widgets", pr=1, path="src/app.py",
        severity="minor", title="check this", explanation="evidence",
        revision=revision, actor="reviewer",
    )
    history = service.read("get_file_review_history", {
        "repo": "acme/widgets", "pr": 1, "page_size": 1,
    })
    assert history["data"]["state_kind"] == "current_state_plus_file_review_events"
    assert history["data"]["current_state"]["available"] is True
    assert history["data"]["events"][0]["history_kind"] == "file_review_event"
    assert history["data"]["events"][0]["revision_state"] == "current"
    assert history["retrieval"]["continuations"]
    follow_up = service.read("get_file_review_history", history["retrieval"]["continuations"][0]["args"])
    assert follow_up["data"]["events"][0]["event_id"] != history["data"]["events"][0]["event_id"]

    with pytest.raises(Exception, match="continuation reads require"):
        service.read("get_file_review_history", {"repo": "acme/widgets", "pr": 1, "page": 2})


def test_shared_discovery_has_descriptions_defaults_and_exact_draft_revisions():
    by_name = {tool["name"]: tool for tool in service.TOOL_DEFINITIONS}
    assert {"list_proposals", "get_proposal", "get_file_review_history"} <= set(by_name)
    for tool in by_name.values():
        properties = tool["inputSchema"]["properties"]
        assert all(isinstance(spec.get("description"), str) and spec["description"] for spec in properties.values())
    proposal_revision = service.DRAFT_TOOL_DEFINITIONS[0]["inputSchema"]["properties"]["items"]["items"]["properties"]["revision"]
    file_revision = service.DRAFT_TOOL_DEFINITIONS[1]["inputSchema"]["properties"]["revision"]
    file_schema = service.DRAFT_TOOL_DEFINITIONS[1]["inputSchema"]
    assert file_schema["properties"]["findings"]["minItems"] == 1
    assert file_schema["properties"]["coverage"]["minItems"] == 1
    assert file_schema["anyOf"] == [
        {"required": ["findings"]}, {"required": ["coverage"]},
    ]
    for schema in (proposal_revision, file_revision):
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) >= {
            "head_sha", "base_sha", "content_digest", "source",
        }
        assert schema["properties"]["source"]["minLength"] == 1
        assert "default" not in schema["properties"]["source"]
        minimal_without_source = {
            "head_sha": "", "base_sha": "", "content_digest": "digest",
        }
        # The shared schema must reject the same minimal identity that the
        # writer would otherwise normalize to an empty source.
        assert set(schema["required"]) - minimal_without_source.keys() == {"source"}
