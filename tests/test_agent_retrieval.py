"""Agent evidence stays compact, traversable, and lossless without network IO."""

import json

from triage.models import ChangedFile, PullRequest
from triage.pipeline import run_pipeline
from triage.service import WorkspaceService


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
