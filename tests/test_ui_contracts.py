"""Dependency-free UI and lazy-pagination contract checks."""

from __future__ import annotations

import subprocess
from pathlib import Path

from triage.models import ChangedFile, PullRequest
from triage.pipeline import run_pipeline
from triage.store import overlap_for_group, prs_for_path


ROOT = Path(__file__).parents[1]
APP = ROOT / "triage" / "web" / "app.js"
NODE_CONTRACTS = ROOT / "tests" / "ui_contracts.cjs"


def _pr(number: int, paths: list[str]) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"PR {number}",
        body="fixture",
        user="fixture",
        changed_files=[
            ChangedFile(
                path=file_path,
                patch="@@ -1 +1 @@\n-old\n+new\n",
                additions=1,
                deletions=1,
            )
            for file_path in paths
        ],
        created_at="2026-09-12T00:00:00Z",
        evidence_source="fixtures",
    )


def test_javascript_syntax_and_security_contracts() -> None:
    result = subprocess.run(
        ["node", "--check", str(APP)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    source = APP.read_text(encoding="utf-8")
    assert 'fetch("/api/session"' in source
    assert '"X-CSRF-Token": token' in source
    assert "expected_version: state.storeVersion" in source
    assert "idempotency_key: idempotencyKey" in source
    assert "allow_external: true" in source
    assert "function loadRelatedIfOpen" in source
    assert 'if (!block || !block.open)' in source
    assert 'requestOnce("body", key' in source
    assert "function drawGraph" not in source
    assert "function runForceAndDraw" not in source
    assert "_willUpdate =" not in source
    assert "virtualizer._didMount()" in source
    assert "cleanup()" in source


def test_javascript_behavior_contracts() -> None:
    result = subprocess.run(
        ["node", str(NODE_CONTRACTS), str(APP)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_file_and_overlap_pages_are_independently_traversable(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    common = "common.conf"
    paths = [common] + [f"shared/{index}.conf" for index in range(30)]
    prs = [_pr(number, paths) for number in range(1, 31)]
    state = run_pipeline(
        prs,
        persist=True,
        store_path=store,
        source="fixtures",
        repo="acme/widgets",
        incremental=False,
    )
    group = next(group for group in state["groups"] if len(group.pr_numbers) == 30)

    first = prs_for_path(common, path=store, page=1, page_size=7)
    second = prs_for_path(common, path=store, page=2, page_size=7)
    assert first["pr_count"] == 30
    assert first["next_page"] == 2
    assert {item["number"] for item in first["prs"]}.isdisjoint(
        item["number"] for item in second["prs"]
    )

    member_page = overlap_for_group(
        group.group_id,
        path=store,
        member_page=2,
        member_page_size=7,
        row_page=1,
        row_page_size=5,
    )
    row_page = overlap_for_group(
        group.group_id,
        path=store,
        member_page=2,
        member_page_size=7,
        row_page=2,
        row_page_size=5,
    )
    assert member_page["member_page"] == 2
    assert member_page["next_member_page"] == 3
    assert len(member_page["pr_numbers"]) == 7
    assert member_page["matrix_n"] == 31
    assert {row["path"] for row in member_page["matrix"]}.isdisjoint(
        row["path"] for row in row_page["matrix"]
    )
    assert all(row["same_patch_status"] == "unknown" for row in member_page["matrix"])
    assert all(row["same_patch"] is None for row in member_page["matrix"])


def test_empty_path_overlap_has_the_canonical_jaccard(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    state = run_pipeline(
        [_pr(1, [])], persist=True, store_path=store, source="fixtures",
        repo="acme/widgets", incremental=False,
    )
    overlap = overlap_for_group(state["groups"][0].group_id, path=store)
    assert overlap["jaccard"] == 1.0
    assert overlap["matrix"] == []
