"""Offline contracts for local retrieval and explicit optional enrichment."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from triage import rank
from triage.models import ChangedFile, PullRequest
from triage.store import load_store, save_store


@pytest.fixture(autouse=True)
def _fixed_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBED_PROVIDER", "together")
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)


def _item(number: int, path: str, patch: str, *, title: str = "change",
          previous_path: str = "", complete: bool = True) -> dict[str, Any]:
    changed = ChangedFile(path, patch, status="renamed" if previous_path else "modified",
                          previous_path=previous_path, patch_complete=complete)
    pr = PullRequest(number, title, "", "octo", [changed], "2026-09-12T00:00:00Z",
                     evidence_complete=complete, evidence_source="fixtures")
    revision = pr.revision_evidence()
    return {"number": number, "title": title, "body": "", "user": "octo",
            "paths": pr.paths, "files": [changed.to_dict()], "group_id": "",
            "html_url": "", "content_digest": revision.content_digest,
            "evidence_complete": revision.evidence_complete, "evidence_source": "fixtures"}


def _store(path: Path, items: list[dict[str, Any]], *, repo: str = "acme/widgets") -> Path:
    data = load_store(path)
    data.update(repo=repo, source="fixtures", store_version=7, last_prs=items,
                last_pr_numbers=[item["number"] for item in items])
    save_store(data, path)
    return path


def test_cached_get_is_offline_and_local_content_preserves_constants(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    path = _store(tmp_path / "store.json", [
        _item(1, "src/net.conf", "@@ -1 +1 @@\n-port=1000\n+port=4096"),
        _item(2, "moved/net.conf", "@@ -1 +1 @@\n-port=1000\n+port=4096", previous_path="src/net.conf"),
        _item(3, "src/net.conf", "@@ -1 +1 @@\n-port=1000\n+port=1024"),
    ])
    monkeypatch.setenv("TOGETHER_API_KEY", "must-not-be-read-for-network")
    monkeypatch.setattr(rank.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    result = rank.related_cached(1, store_path=path)
    assert result["cache_only"] is True and result["source"] == "local-content-path"
    assert result["enrichment"]["requires_explicit_consent"] is True
    assert result["enrichment"]["provider"] == "together"
    assert [row["number"] for row in result["related"][:2]] == [2, 3]
    assert result["related"][0]["evidence"]["rename_or_relocation"] is True
    assert "4096" in result["related"][0]["evidence"]["matching_constants"]


def test_late_hunk_is_retained_and_full_corpus_not_first_24(tmp_path: Path) -> None:
    filler = "\n".join(f" context line {i}" for i in range(1000))
    query_patch = "@@ -1,1000 +1,1000 @@\n" + filler + "\n@@ -2000 +2000 @@\n-old\n+rare_tail_8675309"
    items = [_item(1, "src/query.py", query_patch)]
    items.extend(_item(n, f"src/filler{n}.py", "@@ -1 +1 @@\n-old\n+unrelated") for n in range(2, 32))
    items.append(_item(99, "lib/relocated.py", "@@ -1 +1 @@\n-old\n+rare_tail_8675309"))
    result = rank.related_cached(1, store_path=_store(tmp_path / "store.json", items), k=3)
    assert result["related"][0]["number"] == 99
    assert result["corpus_count"] == 31
    assert "rare_tail_8675309" in rank.normalize_patch(query_patch, cap=500)
    document = rank._document(items[0], items[0]["files"])
    chunks, disclosure = rank._embedding_chunks(document, None, 500)
    assert any("rare_tail_8675309" in chunk for chunk in chunks)
    assert all(len(chunk.encode("utf-8")) <= 500 for chunk in chunks)
    assert disclosure["chunks_embedded"] <= rank.MAX_CHUNKS_PER_PR


@pytest.mark.parametrize("mutator", [
    lambda rows: [rows[0], {**rows[1], "index": 0}],
    lambda rows: [{**rows[0], "embedding": [1.0]}, rows[1]],
    lambda rows: [{**rows[0], "embedding": [float("nan"), 1.0]}, rows[1]],
    lambda rows: [{**rows[0], "embedding": [0.0, 0.0]}, rows[1]],
    lambda rows: [{**rows[0], "embedding": [True, 1.0]}, rows[1]],
    lambda rows: [{**rows[0], "embedding": [10 ** 4_000, 1.0]}, rows[1]],
])
def test_embedding_response_rejects_invalid_vectors(mutator: Any) -> None:
    cfg = {"model": "fixed", "dim": 2}
    rows = [{"index": 0, "embedding": [1.0, 0.0]},
            {"index": 1, "embedding": [0.0, 1.0]}]
    with pytest.raises(rank.RankError):
        rank._validate_vectors({"model": "fixed", "data": mutator(rows)}, 2, cfg)
    with pytest.raises(rank.RankError, match="model"):
        rank._validate_vectors({"model": "wrong", "data": rows}, 2, cfg)


def test_cosine_is_finite_for_extreme_valid_vectors() -> None:
    cases = [
        ([1e308, 1e308], [1e308, 1e308], 1.0),
        ([1e308, 1e308], [-1e308, -1e308], -1.0),
        ([5e-324, 5e-324], [5e-324, 5e-324], 1.0),
        ([3.0, 4.0], [4.0, 3.0], 0.96),
    ]
    for left, right, expected in cases:
        score = rank.cosine(left, right)
        assert math.isfinite(score)
        assert -1.0 <= score <= 1.0
        assert score == pytest.approx(expected)


def test_cosine_rejects_unrepresentable_integer_without_raising() -> None:
    assert rank._valid_vector([10 ** 4_000, 1], 2) is False
    assert rank.cosine([10 ** 4_000, 1], [1, 1]) == 0.0


def test_e5_roles_are_distinct_and_documents_have_no_prefix() -> None:
    cfg = rank.PROVIDERS["together"]
    assert rank._preprocess("code", "query", cfg).startswith("Instruct:")
    assert "\nQuery: code" in rank._preprocess("code", "query", cfg)
    assert rank._preprocess("code", "document", cfg) == "code"
    query = rank._vector_identity("a/b", "digest", "together", cfg["model"],
                                  "query", cfg["preprocessing"], None)[0]
    document = rank._vector_identity("a/b", "digest", "together", cfg["model"],
                                     "document", cfg["preprocessing"], None)[0]
    other_repo = rank._vector_identity("x/y", "digest", "together", cfg["model"],
                                       "query", cfg["preprocessing"], None)[0]
    other_model = rank._vector_identity("a/b", "digest", "together", "fixed-v2",
                                        "query", cfg["preprocessing"], None)[0]
    other_preprocessing = rank._vector_identity("a/b", "digest", "together", cfg["model"],
                                                "query", "preprocess-v2", None)[0]
    assert len({query, document, other_repo, other_model, other_preprocessing}) == 5


def test_e5_chunks_bound_the_complete_preprocessed_ascii_input() -> None:
    cfg = rank.PROVIDERS["together"]
    assert cfg["max_bytes"] == 480
    stored = _item(1, "src/large.py", "@@ -0,0 +1 @@\n+" + "ascii_code " * 2_000)
    document = rank._document(stored, stored["files"])
    overhead = len(rank._preprocess("", "query", cfg).encode("utf-8"))
    query_chunks, _ = rank._embedding_chunks(
        document, None, int(cfg["max_bytes"]) - overhead
    )
    document_chunks, _ = rank._embedding_chunks(document, None, int(cfg["max_bytes"]))
    assert query_chunks and document_chunks
    assert all(len(rank._preprocess(chunk, "query", cfg).encode("utf-8")) <= 480
               for chunk in query_chunks)
    assert all(len(rank._preprocess(chunk, "document", cfg).encode("utf-8")) <= 480
               for chunk in document_chunks)


def test_invalid_provider_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBED_PROVIDER", "typo-provider")
    with pytest.raises(rank.RankError, match="not supported"):
        rank.provider()
    monkeypatch.setenv("EMBED_PROVIDER", "together")
    monkeypatch.setenv("EMBED_MODEL", "unknown/dimension")
    with pytest.raises(rank.RankError, match="model override"):
        rank.provider()


def test_github_evidence_is_pinned_and_revision_mismatch_never_egresses(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundles: dict[int, dict[str, Any]] = {}
    records = []
    for number, patch in ((1, "@@ -1 +1 @@\n-old\n+shared"),
                          (2, "@@ -1 +1 @@\n-old\n+shared")):
        changed = ChangedFile("src/a.py", patch, status="modified", additions=1, deletions=1)
        pr = PullRequest(number, "github", "", "octo", [changed], "2026-09-12T00:00:00Z",
            head_sha=f"head-{number}", base_sha="base", updated_at="2026-09-12T00:00:00Z",
            additions=1, deletions=1, evidence_source="github")
        revision = pr.revision_evidence()
        records.append({"number": number, "title": pr.title, "user": pr.user, "paths": pr.paths,
            "head_sha": pr.head_sha, "base_sha": pr.base_sha, "updated_at": pr.updated_at,
            "additions": 1, "deletions": 1, "content_digest": revision.content_digest,
            "evidence_complete": True, "cache_snapshot_id": "snapshot-7"})
        bundles[number] = {"snapshot_id": "snapshot-7", "meta": {"number": number,
            "title": pr.title, "body": "", "user": pr.user, "repository": "acme/widgets",
            "head_sha": pr.head_sha, "base_sha": pr.base_sha, "updated_at": pr.updated_at,
            "evidence_complete": True}, "files": [changed.to_dict()]}
    path = tmp_path / "store.json"
    data = load_store(path)
    data.update(repo="acme/widgets", source="github", store_version=3,
                last_prs=records, last_pr_numbers=[1, 2])
    save_store(data, path)
    seen = []
    def cached(_owner: str, _repo: str, number: int, _cache_dir: Any = None,
               *, snapshot_id: str | None = None) -> dict[str, Any]:
        seen.append((number, snapshot_id))
        return bundles[number]
    monkeypatch.setattr(rank.gh, "cached_pr_evidence", cached)
    assert rank.related_cached(1, store_path=path)["related"][0]["number"] == 2
    assert set(seen) == {(1, "snapshot-7"), (2, "snapshot-7")}

    # A newer/different cache payload cannot be sent under consent for the old store.
    bundles[1]["meta"]["head_sha"] = "newer-head"
    rank._corpus_cache.clear()
    monkeypatch.setenv("TOGETHER_API_KEY", "not-used")
    monkeypatch.setattr(rank.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("egress")))
    result = rank.enrich_related(1, repo="acme/widgets", store_path=path,
                                 expected_version=int(load_store(path)["store_version"]))
    assert "complete revision-bound" in result["reason"]


def test_missing_key_is_retryable_and_never_creates_poisoned_result(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    path = _store(tmp_path / "store.json", [
        _item(1, "a.py", "@@ -1 +1 @@\n-old\n+same"),
        _item(2, "b.py", "@@ -1 +1 @@\n-old\n+same"),
    ])
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    version = int(load_store(path)["store_version"])
    result = rank.enrich_related(1, repo="acme/widgets", store_path=path, expected_version=version)
    assert "can be retried" in result["reason"]
    assert rank.load_cache(rank.cache_path(path))["results"] == {}


def test_explicit_enrichment_key_prefers_env_then_workspace_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "store.json"
    keyfile = tmp_path / "together.key"
    keyfile.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    assert rank.api_key(store) == "file-secret"
    monkeypatch.setenv("TOGETHER_API_KEY", "env-secret")
    assert rank.api_key(store) == "env-secret"
    monkeypatch.delenv("TOGETHER_API_KEY")
    keyfile.unlink()
    keyfile.symlink_to(tmp_path / "missing")
    assert rank.api_key(store) == ""


def test_workspace_key_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "together.key"
    os.mkfifo(fifo)
    code = ("from pathlib import Path; from triage.rank import api_key; import sys; "
            "print(api_key(Path(sys.argv[1])))")
    env = dict(os.environ)
    env["EMBED_PROVIDER"] = "together"
    env.pop("TOGETHER_API_KEY", None)
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "store.json")],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
        text=True, timeout=2, check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == "\n"


def test_patch_headers_are_removed_but_signed_code_inside_hunks_is_preserved() -> None:
    patch = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n"
             "@@ -1 +1 @@\n--- removed_setting\n+++ added_setting\n")
    normalized = rank.normalize_patch(patch)
    assert "--- a/x" not in normalized and "+++ b/x" not in normalized
    assert "--- removed_setting" in normalized and "+++ added_setting" in normalized
    tokens = rank._changed_tokens([{"path": "x", "patch": patch}])
    assert "del:removed_setting" in tokens
    assert "add:added_setting" in tokens


def test_body_only_store_update_invalidates_local_corpus_facts(tmp_path: Path) -> None:
    query = _item(1, "q.py", "@@ -1 +1 @@\n-qold\n+qnew", title="query")
    first_match = _item(2, "two.py", "@@ -1 +1 @@\n-two-old\n+two-new", title="two")
    second_match = _item(3, "three.py", "@@ -1 +1 @@\n-three-old\n+three-new", title="three")
    query["body"], first_match["body"], second_match["body"] = "rare_body_token", "rare_body_token", "other"
    path = _store(tmp_path / "store.json", [query, first_match, second_match])
    assert rank.related_cached(1, store_path=path)["related"][0]["number"] == 2

    data = load_store(path)
    by_number = {item["number"]: item for item in data["last_prs"]}
    by_number[2]["body"], by_number[3]["body"] = "other", "rare_body_token"
    save_store(data, path)
    assert rank.related_cached(1, store_path=path)["related"][0]["number"] == 3


def test_cached_enrichment_preserves_query_truncation(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    query_patch = "@@ -1 +1 @@\n-old\n+" + ("long_ascii_identifier_" * 500)
    path = _store(tmp_path / "store.json", [
        _item(1, "shared.py", query_patch, title="query"),
        _item(2, "shared.py", "@@ -1 +1 @@\n-old\n+other", title="candidate"),
    ])
    monkeypatch.setenv("TOGETHER_API_KEY", "synthetic-secret")
    monkeypatch.setattr(rank, "_post_embed", lambda texts, _key, **_kwargs:
                        [[1.0] + [0.0] * 1023 for _ in texts])
    version = int(load_store(path)["store_version"])
    enriched = rank.enrich_related(
        1, repo="acme/widgets", store_path=path, limit=1, expected_version=version
    )
    cached = rank.related_cached(1, store_path=path, k=1)
    assert enriched["query_input_truncation"]["omitted_bytes"] > 0
    assert cached["source"] == "cached-embedding-rerank"
    assert cached["query_input_truncation"] == enriched["query_input_truncation"]


def test_rank_cache_prunes_before_read_cliff_and_keeps_recent_records(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "rank.json"
    monkeypatch.setattr(rank, "MAX_CACHE_BYTES", 1_800)
    monkeypatch.setattr(rank, "MAX_CACHE_ITEMS", 100)
    for index in range(6):
        rank.save_cache({"schema_version": rank.CACHE_SCHEMA_VERSION,
            "items": {f"item-{index}": {"value": index, "padding": "x" * 600}},
            "results": {}}, path)
    cached = rank.load_cache(path)
    assert path.stat().st_size <= rank.MAX_CACHE_BYTES
    assert cached["items"]
    assert "item-5" in cached["items"]
    assert len(cached["items"]) < 6


def test_provider_redirects_are_not_followed_and_errors_are_sanitized(
        monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "never-disclose-this-key"
    request = rank.urllib.request.Request(
        rank.PROVIDERS["together"]["url"], headers={"Authorization": f"Bearer {secret}"}
    )
    redirected = rank._NoCredentialRedirectHandler().redirect_request(
        request, None, 302, "Found", {"Location": "https://attacker.invalid/steal"},
        "https://attacker.invalid/steal",
    )
    assert redirected is None

    def failed(_request: Any, timeout: float) -> Any:
        raise rank.urllib.error.HTTPError(
            "https://attacker.invalid/secret-body", 302, f"provider said {secret}", {}, None
        )

    monkeypatch.setattr(rank, "_open_provider_request", failed)
    with pytest.raises(rank.RankError) as error:
        rank._post_embed(["document"], secret, timeout=.2)
    assert secret not in str(error.value)
    assert "attacker.invalid" not in str(error.value)


def test_provider_read_has_absolute_deadline_and_retains_slot_until_worker_finishes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowResponse:
        headers: dict[str, str] = {}
        def __enter__(self) -> SlowResponse:
            return self
        def __exit__(self, *_args: Any) -> None:
            return None
        def read(self, _limit: int) -> bytes:
            time.sleep(.2)
            body = {"model": rank.PROVIDERS["together"]["model"],
                    "data": [{"index": 0, "embedding": [1.0] + [0.0] * 1023}]}
            return json.dumps(body).encode()

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(rank, "_provider_slots", slots)
    monkeypatch.setattr(rank, "_open_provider_request", lambda *_args, **_kwargs: SlowResponse())
    started = time.monotonic()
    with pytest.raises(rank.RankError, match="time budget"):
        rank._post_embed(["document"], "synthetic-secret", timeout=.01)
    assert time.monotonic() - started < .15
    assert slots.acquire(blocking=False) is False
    time.sleep(.25)
    assert slots.acquire(blocking=False) is True
    slots.release()


def test_provider_slot_wait_uses_same_deadline_and_starts_no_late_worker(
        monkeypatch: pytest.MonkeyPatch) -> None:
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    opened = 0
    def forbidden_open(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal opened
        opened += 1
        raise AssertionError("provider worker started after its deadline")
    monkeypatch.setattr(rank, "_provider_slots", slots)
    monkeypatch.setattr(rank, "_open_provider_request", forbidden_open)
    started = time.monotonic()
    with pytest.raises(rank.RankError, match="busy"):
        rank._post_embed(["document"], "synthetic-secret", timeout=.02)
    assert time.monotonic() - started < .1
    assert opened == 0
    slots.release()


def test_local_document_sampling_bounds_patch_tokens_and_corpus_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = [{"path": f"src/{index}.py", "previous_path": "",
              "patch": "@@ -0,0 +1 @@\n+" + (f"token_{index} " * 1_000),
              "patch_complete": True} for index in range(40)]
    stored = {"number": 1, "title": "title", "body": "body", "user": "u",
              "paths": [item["path"] for item in files], "content_digest": "digest",
              "evidence_complete": True}
    document = rank._document(stored, files)
    disclosure = document["local_input_truncation"]
    assert len(document["files"]) <= rank.MAX_LOCAL_PATCH_FILES
    assert sum(len(item["patch"].encode("utf-8")) for item in document["files"]) <= rank.LOCAL_TEXT_CAP
    assert len(document["content_tokens"]) <= rank.MAX_LOCAL_TOKENS
    assert disclosure["omitted_files"] == 16
    assert disclosure["omitted_patch_bytes"] > 0

    rank._corpus_cache.clear()
    rank._corpus_cache_sizes.clear()
    monkeypatch.setattr(rank, "MAX_CORPUS_CACHE_BYTES", 1)
    rank._remember_corpus("oversized", ([document], {"incomplete": 0}))
    assert "oversized" not in rank._corpus_cache


def test_inflight_requests_are_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    lock = threading.Lock()
    def fake(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(.05)
        return {"query": 1, "related": []}
    monkeypatch.setattr(rank, "_enrich_once", fake)
    results: list[dict[str, Any]] = []
    threads = [threading.Thread(target=lambda: results.append(rank.enrich_related(
        1, repo="acme/widgets", store_path=Path("ignored"), limit=5))) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert calls == 1 and len(results) == 4


def test_stale_cache_writers_merge_records(tmp_path: Path) -> None:
    path = tmp_path / "rank.json"
    code = ("from pathlib import Path; from triage.rank import save_cache; "
            "import sys,time; time.sleep(.05); "
            "save_cache({'schema_version':2,'items':{sys.argv[2]:{'value':sys.argv[2]}},'results':{}},Path(sys.argv[1]))")
    root = Path(__file__).resolve().parents[1]
    processes = [subprocess.Popen([sys.executable, "-c", code, str(path), key], cwd=root)
                 for key in ("a", "b")]
    assert all(process.wait(timeout=5) == 0 for process in processes)
    assert set(rank.load_cache(path)["items"]) == {"a", "b"}


def test_overlap_and_duplicate_claims_require_complete_canonical_hunks() -> None:
    from triage.models import Group
    from triage.overlap import file_overlap
    from triage.summarize import suggest_decision
    bad = "@@ -1,2 +1,2 @@\n-old\n+new"  # declared context is absent
    prs = [PullRequest(n, "same", "", "u", [ChangedFile("a", bad)], "now",
                       evidence_source="fixtures") for n in (1, 2)]
    row = file_overlap(prs)["matrix"][0]
    assert row["same_patch"] is False and row["same_patch_status"] == "unknown"
    assert suggest_decision(Group("G", [1, 2]), {pr.number: pr for pr in prs}) != "duplicate"
    good = "@@ -1 +1 @@\n-old\n+new"
    complete = [PullRequest(n, "same", "", "u", [ChangedFile("a", good)], "now",
                            evidence_source="fixtures") for n in (1, 2)]
    assert file_overlap(complete)["matrix"][0]["same_patch_status"] == "same"
    assert suggest_decision(Group("G", [1, 2]), {pr.number: pr for pr in complete}) == "duplicate"
