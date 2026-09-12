"""CLI entrypoint: python -m triage ... / triage ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from triage.dedupe import apply_fingerprints
from triage.embed import Embedder
from triage.pipeline import (
    FIXTURES_DIR,
    NEEDS_HUMAN_LABEL,
    format_groups,
    ingest,
    load_prs_from_json,
    run_pipeline,
)
from triage.store import (
    DEFAULT_STORE_PATH,
    decide_group,
    load_groups,
    load_rules,
    load_store,
)


DEMO_STORE_PATH = DEFAULT_STORE_PATH.with_name("demo-store.json")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _is_live_store(path: Path) -> bool:
    """Return whether path names the normal, non-fixture store."""
    candidate = path.expanduser()
    live_paths = (
        DEFAULT_STORE_PATH.expanduser(),
        _project_root() / DEFAULT_STORE_PATH,
    )
    resolved = candidate.resolve()
    if any(resolved == live.resolve() for live in live_paths):
        return True
    if candidate.exists():
        for live in live_paths:
            if not live.exists():
                continue
            try:
                if candidate.samefile(live):
                    return True
            except OSError:
                continue
    return False


def _isolated_store(args: argparse.Namespace, command: str) -> Path | None:
    """Resolve a fixture-only store, refusing the normal live store."""
    raw_store = getattr(args, "store", None)
    store = Path(raw_store).expanduser() if raw_store else DEMO_STORE_PATH
    if _is_live_store(store):
        print(
            f"Error: {command} cannot use the live store {DEFAULT_STORE_PATH}; "
            f"choose an isolated path such as {DEMO_STORE_PATH}.",
            file=sys.stderr,
        )
        return None
    return store


def cmd_run(args: argparse.Namespace) -> int:
    if args.source == "fixtures":
        store = _isolated_store(args, "fixture run")
        if store is None:
            return 2
    else:
        raw_store = getattr(args, "store", None)
        store = Path(raw_store).expanduser() if raw_store else DEFAULT_STORE_PATH

    prs = ingest(source=args.source, repo=args.repo, limit=args.limit)
    result = run_pipeline(
        prs,
        use_llm=args.llm,
        persist=True,
        store_path=store,
        source=args.source,
        repo=args.repo,
    )
    print(f"Ingested {len(result['prs'])} PRs from {args.source}")
    print(format_groups(result["groups"], result["prs"]))
    print(f"Stored groups in {store}")
    return 0


def cmd_list_groups(_args: argparse.Namespace) -> int:
    groups = load_groups()
    if not groups:
        print("No groups in store. Run `triage run` or `triage demo` first.")
        return 0
    data = load_store()
    print(f"Last run PR numbers: {data.get('last_pr_numbers', [])}")
    # Reconstruct minimal PR stubs for labels if needed
    print(format_groups(groups))
    rules = load_rules()
    if rules:
        print("Trusted rules:")
        for r in rules:
            print(
                f"  {r.rule_id}: {r.decision} group={r.group_id} "
                f"prs={r.created_from_prs} fps={len(r.fingerprints)}"
            )
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    try:
        rule = decide_group(args.group_id, args.decision)
    except (KeyError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Persisted rule {rule.rule_id}: decision={rule.decision} "
        f"for group {rule.group_id} (PRs {rule.created_from_prs})"
    )
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-run fixtures + newcomers against persisted rules."""
    store = _isolated_store(args, "fixture replay")
    if store is None:
        return 2
    base = load_prs_from_json(FIXTURES_DIR / "prs.json")
    newcomers = load_prs_from_json(FIXTURES_DIR / "newcomers.json")
    all_prs = base + newcomers
    result = run_pipeline(
        all_prs,
        use_llm=args.llm,
        persist=True,
        store_path=store,
        apply_rules=True,
        source="fixtures",
        repo="omacom/omarchy",
    )
    print(f"Replay: {len(base)} fixtures + {len(newcomers)} newcomers")
    print(format_groups(result["groups"], result["prs"]))
    print("Newcomer classification (manual review enforced):")
    newcomer_nums = {p.number for p in newcomers}
    for pr in result["prs"]:
        if pr.number in newcomer_nums:
            print(f"  #{pr.number} {pr.title!r} -> {pr.label}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """
    Wow path: fixtures pipeline, human-approve largest duplicate group,
    then queue two newcomers for manual review. Offline, zero env.
    """
    store = _isolated_store(args, "demo")
    if store is None:
        return 2
    explicit_store = bool(getattr(args, "store", None))
    reset = bool(getattr(args, "reset", False))
    if reset and not explicit_store:
        print(
            "Error: --reset requires an explicit --store path.",
            file=sys.stderr,
        )
        return 2
    if store.exists():
        if not reset:
            print(
                f"Error: demo store already exists: {store}. "
                "Pass both --store PATH and --reset to replace it.",
                file=sys.stderr,
            )
            return 2
        if store.is_dir():
            print(f"Error: demo store path is a directory: {store}", file=sys.stderr)
            return 2
        store.unlink()
    store.parent.mkdir(parents=True, exist_ok=True)

    print("=== Omarchy PR group-triage DEMO ===")
    print("Stage 1: ingest fixtures")
    prs = ingest(source="fixtures")
    result = run_pipeline(
        prs,
        use_llm=args.llm,
        persist=True,
        store_path=store,
        apply_rules=False,
        source="fixtures",
        repo="omacom/omarchy",
    )
    groups = result["groups"]
    print(f"Loaded {len(prs)} PRs; formed {len(groups)} groups\n")
    print(format_groups(groups, result["prs"]))

    # Pick largest duplicate (or largest) group to bless
    dupes = [g for g in groups if g.suggested_decision == "duplicate"]
    if not dupes:
        dupes = sorted(groups, key=lambda g: len(g.pr_numbers), reverse=True)
    target = max(dupes, key=lambda g: len(g.pr_numbers))
    print(f"Stage 2: human gate — approve largest duplicate group {target.group_id}")
    print(f"  titles: {target.title_variants}")
    print(f"  PRs: {target.pr_numbers}")
    rule = decide_group(target.group_id, "approve", path=store)
    print(f"  persisted {rule.rule_id}\n")

    print("Stage 3: second ingest — 2 newcomers; auto-approval disabled")
    newcomers = load_prs_from_json(FIXTURES_DIR / "newcomers.json")
    # Keep the stored rule centroid reproducible in a joint fixture corpus,
    # while leaving every newcomer queued for the human gate.
    combined = apply_fingerprints(list(prs) + list(newcomers))
    docs = [p.text_for_embed for p in combined]
    embedder = Embedder(n=3)
    vectors = embedder.fit_transform(docs)

    # Recompute blessed centroid in the joint space from the approved group's PRs
    approved_nums = set(rule.created_from_prs)
    member_vecs = [
        vectors[i] for i, p in enumerate(combined) if p.number in approved_nums
    ]
    if member_vecs:
        from triage.embed import mean_centroid

        rule.centroid = mean_centroid(member_vecs)
        from triage.store import upsert_rule

        upsert_rule(rule, store)

    print("Newcomer results:")
    for pr in newcomers:
        pr.label = NEEDS_HUMAN_LABEL
        print(f"  #{pr.number} {pr.title!r}")
        print(f"    label={pr.label}  => NEEDS-HUMAN (manual review required)")

    print("\n=== Demo complete (no GitHub writes) ===")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from triage.server import serve

    serve(
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        store_path=Path(args.store) if args.store else DEFAULT_STORE_PATH,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triage",
        description="Omarchy PR group-triage POC (local classify only; no merges)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="Offline wow path with fixtures + newcomers")
    p_demo.add_argument("--llm", action="store_true", help="Enable LLM stub note")
    p_demo.add_argument(
        "--store",
        help=f"Isolated store path (default: {DEMO_STORE_PATH})",
    )
    p_demo.add_argument(
        "--reset",
        action="store_true",
        help="Replace an existing explicitly named demo store",
    )
    p_demo.set_defaults(func=cmd_demo)

    p_run = sub.add_parser("run", help="Run pipeline and print groups")
    p_run.add_argument(
        "--source",
        choices=["fixtures", "gh", "github"],
        default="fixtures",
    )
    p_run.add_argument("--repo", default="omacom/omarchy")
    p_run.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max open PRs to fetch (0 = all, default).",
    )
    p_run.add_argument("--llm", action="store_true")
    p_run.add_argument(
        "--store",
        help=(
            f"Store path (fixtures default: {DEMO_STORE_PATH}; "
            f"gh/github default: {DEFAULT_STORE_PATH})"
        ),
    )
    p_run.set_defaults(func=cmd_run)

    p_list = sub.add_parser("list-groups", help="Show last run groups")
    p_list.set_defaults(func=cmd_list_groups)

    p_decide = sub.add_parser("decide", help="Approve or reject a group")
    p_decide.add_argument("group_id")
    p_decide.add_argument("decision", choices=["approve", "reject"])
    p_decide.set_defaults(func=cmd_decide)

    p_replay = sub.add_parser(
        "replay",
        help="Re-run fixtures + newcomers against persisted rules",
    )
    p_replay.add_argument("--llm", action="store_true")
    p_replay.add_argument(
        "--store",
        help=f"Isolated store path (default: {DEMO_STORE_PATH})",
    )
    p_replay.set_defaults(func=cmd_replay)

    p_serve = sub.add_parser("serve", help="Local group-queue + neighborhood dashboard")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8741)
    p_serve.add_argument("--no-open", action="store_true", help="Do not open browser")
    p_serve.add_argument("--store", default=str(DEFAULT_STORE_PATH))
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
