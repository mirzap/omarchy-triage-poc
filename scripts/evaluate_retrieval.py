#!/usr/bin/env python3
"""Report recall@K and precision@K for curated synthetic retrieval cases."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from triage.models import ChangedFile, PullRequest
from triage.rank import related_cached
from triage.store import load_store, save_store


def item(raw: dict) -> dict:
    changed = ChangedFile(raw["path"], raw["patch"], previous_path=raw.get("previous_path", ""),
                          status="renamed" if raw.get("previous_path") else "modified")
    pr = PullRequest(raw["number"], raw.get("title", "synthetic change"), "", "synthetic",
                     [changed], "2026-09-12T00:00:00Z", evidence_source="fixtures")
    revision = pr.revision_evidence()
    return {"number": pr.number, "title": pr.title, "body": "", "user": pr.user,
            "paths": pr.paths, "files": [changed.to_dict()], "content_digest": revision.content_digest,
            "evidence_complete": revision.evidence_complete, "evidence_source": "fixtures"}


def main() -> None:
    fixture = json.loads((ROOT / "tests/fixtures/retrieval_cases.json").read_text())
    k, recalls, precisions = int(fixture["k"]), [], []
    total_hits = total_relevant = total_returned = total_available = 0
    with tempfile.TemporaryDirectory() as directory:
        for case in fixture["cases"]:
            path = Path(directory) / (case["name"].replace(" ", "_") + ".json")
            data = load_store(path)
            records = [item(case["query"]), *map(item, case["candidates"])]
            data.update(repo="synthetic/retrieval", source="fixtures", store_version=1,
                        last_prs=records, last_pr_numbers=[row["number"] for row in records])
            save_store(data, path)
            result = related_cached(case["query"]["number"], store_path=path, k=k)
            found = [row["number"] for row in result["related"]]
            relevant = set(case["relevant"])
            hits = len(relevant & set(found[:k]))
            total_hits += hits
            total_relevant += len(relevant)
            total_returned += len(found[:k])
            total_available += int(result["truncation"]["available"])
            recalls.append(hits / len(relevant) if relevant else 1.0)
            precisions.append(hits / k)
            print(f"{case['name']}: hits={hits}/{len(relevant)} returned={len(found[:k])}/{k} "
                  f"available={result['truncation']['available']} omitted={result['truncation']['omitted']} "
                  f"found={found[:k]} relevant={sorted(relevant)}")
    print(f"{fixture['label']}")
    print(f"recall@{k}={sum(recalls)/len(recalls):.3f} precision@{k}={sum(precisions)/len(precisions):.3f}")
    print(f"raw: cases={len(recalls)} hits={total_hits} relevant={total_relevant} "
          f"returned={total_returned} precision_slots={len(recalls)*k} available={total_available}")


if __name__ == "__main__":
    main()
