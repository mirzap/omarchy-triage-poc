"""Reproducible offline benchmark for file-set grouping."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triage.cluster import cluster_prs
from triage.dedupe import apply_fingerprints
from triage.models import ChangedFile, PullRequest
from triage.pipeline import run_pipeline

EXPECTED_MEMBERSHIP_SHA256 = (
    "51a49276acbb025566df94ead3538611b8d9aaaa0dba9ee473526fcd661908dd"
)


def corpus(count: int = 2_200, areas: int = 200) -> list[PullRequest]:
    return [
        PullRequest(
            number=index + 1,
            title=f"Change area {index % areas}",
            body="Review synthetic change. " * 20,
            user=f"user{index % 25}",
            created_at="2026-01-01",
            changed_files=[
                ChangedFile(
                    f"area/{index % areas}/config",
                    f"@@ -1 +1 @@\n-old={index}\n+new={index + 1}",
                ),
                ChangedFile(
                    f"area/{index % areas}/helper",
                    "@@ -2 +2 @@\n-disabled\n+enabled",
                ),
            ],
        )
        for index in range(count)
    ]


def hotspot_corpus(count: int = 2_200) -> list[PullRequest]:
    return [
        PullRequest(
            number=index + 1,
            title=f"Hotspot {index}",
            body="",
            user="synthetic",
            created_at="2026-01-01",
            changed_files=[
                ChangedFile("shared/hotspot", "@@ -1 +1 @@\n-old\n+new"),
                ChangedFile(
                    f"unique/{index}",
                    "@@ -1 +1 @@\n-disabled\n+enabled",
                ),
            ],
        )
        for index in range(count)
    ]


def main() -> int:
    prs = corpus()
    with tempfile.TemporaryDirectory(prefix="triage-benchmark-") as directory:
        started = time.perf_counter()
        result = run_pipeline(
            prs,
            persist=False,
            incremental=False,
            repo="synthetic/benchmark",
            store_path=Path(directory) / "store.json",
        )
        elapsed = time.perf_counter() - started
    memberships = sorted(sorted(group.pr_numbers) for group in result["groups"])
    digest = hashlib.sha256(json.dumps(memberships).encode("utf-8")).hexdigest()

    stats: dict[str, int] = {}
    hotspot_started = time.perf_counter()
    hotspot_groups = cluster_prs(apply_fingerprints(hotspot_corpus()), stats=stats)
    hotspot_elapsed = time.perf_counter() - hotspot_started

    print(f"corpus_prs={len(prs)} groups={len(memberships)} seconds={elapsed:.9f}")
    print(f"membership_sha256={digest}")
    print(
        f"hotspot_prs=2200 groups={len(hotspot_groups)} "
        f"candidate_checks={stats['candidate_checks']} seconds={hotspot_elapsed:.9f}"
    )
    if digest != EXPECTED_MEMBERSHIP_SHA256:
        print(f"ERROR expected membership {EXPECTED_MEMBERSHIP_SHA256}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
