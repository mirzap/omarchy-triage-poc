"""CLI entrypoint: python -m triage ... / triage ..."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from triage.gh import GhError
from triage.github import GitHubError
from triage.pipeline import (
    FIXTURES_DIR,
    format_groups,
    ingest,
    load_prs_from_json,
    run_pipeline,
)
from triage.store import (
    DEFAULT_STORE_PATH,
    StoreConflictError,
    StoreCorruptionError,
    backup_store,
    decide_group,
    load_groups,
    load_rules,
    load_store,
    restore_store,
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


def _normalized_source(source: str) -> str:
    normalized = source.strip().lower()
    return "github" if normalized in {"gh", "github"} else normalized


def _workspace_matches_store(store: Path, repo: str, source: str) -> bool:
    if not store.exists():
        return True
    data = load_store(store)
    current = str(data.get("repo") or "").strip().strip("/").lower()
    wanted = repo.strip().strip("/").lower()
    current_source = _normalized_source(str(data.get("source") or ""))
    wanted_source = _normalized_source(source)
    populated = bool(
        data.get("last_prs")
        or data.get("last_groups")
        or data.get("trusted_rules")
        or current
        or current_source
    )
    if current and current != wanted:
        print(
            f"Error: {store} contains repository {current}, not {wanted}. "
            "Choose a separate --store for the other repository.",
            file=sys.stderr,
        )
        return False
    if populated and current_source != wanted_source:
        print(
            f"Error: {store} contains source {current_source or 'unknown'}, "
            f"not {wanted_source}. "
            "Choose a separate --store for the other source.",
            file=sys.stderr,
        )
        return False
    return True


def cmd_run(args: argparse.Namespace) -> int:
    if args.source == "fixtures":
        store = _isolated_store(args, "fixture run")
        if store is None:
            return 2
    else:
        raw_store = getattr(args, "store", None)
        store = Path(raw_store).expanduser() if raw_store else DEFAULT_STORE_PATH

    try:
        if args.source == "fixtures" and (args.refresh or args.cached):
            raise ValueError("--refresh/--cached apply only to GitHub sources")
        if not _workspace_matches_store(store, args.repo, args.source):
            return 2
        progress: dict = {}
        prs = ingest(
            source=args.source,
            repo=args.repo,
            limit=args.limit,
            refresh=bool(args.refresh),
            progress=progress,
        )
        result = run_pipeline(
            prs,
            persist=True,
            store_path=store,
            source=args.source,
            repo=args.repo,
        )
    except (
        GhError,
        GitHubError,
        StoreConflictError,
        StoreCorruptionError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Ingested {len(result['prs'])} PRs from {args.source}")
    if args.source in {"gh", "github"}:
        status = progress.get("cache_status") or "cached"
        fetched_at = progress.get("fetched_at") or "unknown time"
        print(f"Snapshot status: {status}; fetched at {fetched_at}")
    print(format_groups(result["groups"], result["prs"]))
    print(f"Stored groups in {store}")
    return 0


def cmd_list_groups(args: argparse.Namespace) -> int:
    store = Path(args.store).expanduser()
    groups = load_groups(store)
    if not groups:
        print("No groups in store. Run `triage run` or `triage demo` first.")
        return 0
    data = load_store(store)
    print(f"Last run PR numbers: {data.get('last_pr_numbers', [])}")
    # Reconstruct minimal PR stubs for labels if needed
    print(format_groups(groups))
    rules = load_rules(store)
    if rules:
        print("Trusted rules:")
        for r in rules:
            print(
                f"  {r.rule_id}: {r.decision} group={r.group_id} "
                f"prs={r.created_from_prs} fps={len(r.fingerprints)}"
            )
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    store = Path(args.store).expanduser()
    try:
        state = load_store(store)
        rule = decide_group(
            args.group_id,
            args.decision,
            path=store,
            expected_repo=str(state.get("repo") or ""),
            expected_version=int(state.get("store_version", 0)),
            idempotency_key=args.idempotency_key or f"cli-{uuid.uuid4()}",
            actor=args.actor,
        )
    except (KeyError, ValueError, StoreConflictError, StoreCorruptionError) as exc:
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
    try:
        if not _workspace_matches_store(store, "omacom/omarchy", "fixtures"):
            return 2
    except (OSError, ValueError, StoreCorruptionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    base = load_prs_from_json(FIXTURES_DIR / "prs.json")
    newcomers = load_prs_from_json(FIXTURES_DIR / "newcomers.json")
    all_prs = base + newcomers
    result = run_pipeline(
        all_prs,
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
    """Run the real local workflow against isolated fixtures."""
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
        try:
            if not _workspace_matches_store(
                store, "omacom/omarchy", "fixtures"
            ):
                return 2
        except (OSError, ValueError, StoreCorruptionError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
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
        saved = backup_store(store)
        store.unlink()
        print(f"Backed up previous demo store to {saved}")
    store.parent.mkdir(parents=True, exist_ok=True)

    print("=== Omarchy PR group-triage DEMO ===")
    print("Stage 1: ingest fixtures")
    prs = ingest(source="fixtures")
    result = run_pipeline(
        prs,
        persist=True,
        store_path=store,
        apply_rules=False,
        source="fixtures",
        repo="omacom/omarchy",
    )
    groups = result["groups"]
    print(f"Loaded {len(prs)} PRs; formed {len(groups)} groups\n")
    print(format_groups(groups, result["prs"]))

    print("Stage 2: second ingest — newcomers remain at the human gate")
    newcomers = load_prs_from_json(FIXTURES_DIR / "newcomers.json")
    second = run_pipeline(
        list(prs) + list(newcomers),
        persist=True,
        store_path=store,
        apply_rules=True,
        source="fixtures",
        repo="omacom/omarchy",
    )

    print("Newcomer results:")
    by_number = {pr.number: pr for pr in second["prs"]}
    for newcomer in newcomers:
        pr = by_number[newcomer.number]
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


def cmd_backup(args: argparse.Namespace) -> int:
    store = Path(args.store).expanduser()
    destination = Path(args.output).expanduser() if args.output else None
    try:
        saved = backup_store(store, destination)
    except (OSError, ValueError, StoreCorruptionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Validated backup written to {saved}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    store = Path(args.store).expanduser()
    source = Path(args.source).expanduser()
    if args.confirm_restore != str(store):
        print(
            "Error: --confirm-restore must exactly match the --store target.",
            file=sys.stderr,
        )
        return 2
    try:
        previous = restore_store(store, source)
    except (OSError, ValueError, StoreCorruptionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Restored {source} to {store}; prior target retained at {previous}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    try:
        data = load_store(Path(args.store).expanduser())
    except StoreCorruptionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    events = data.get("decision_events") or []
    legacy = data.get("legacy_decision_history") or []
    for event in events:
        print(
            f"{event.get('decided_at', '')} {event.get('actor', '')} "
            f"{event.get('repo', '')} {event.get('group_id', '')} "
            f"{event.get('decision', '')} {event.get('snapshot_digest', '')}"
        )
    for rule in legacy:
        print(
            f"legacy-unverified {rule.get('repo', '')} "
            f"{rule.get('group_id', '')} {rule.get('decision', '')}"
        )
    if not events and not legacy:
        print("No decision history.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triage",
        description="Omarchy PR group-triage POC (local classify only; no merges)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="Offline wow path with fixtures + newcomers")
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
        help="Max PR file scopes to refresh (0 = all); all open PRs remain visible.",
    )
    cache_mode = p_run.add_mutually_exclusive_group()
    cache_mode.add_argument(
        "--refresh",
        action="store_true",
        help="Explicitly refresh GitHub; cached mode is the default.",
    )
    cache_mode.add_argument(
        "--cached",
        action="store_true",
        help="Use local cache only (default); never contacts GitHub.",
    )
    p_run.add_argument(
        "--store",
        help=(
            f"Store path (fixtures default: {DEMO_STORE_PATH}; "
            f"gh/github default: {DEFAULT_STORE_PATH})"
        ),
    )
    p_run.set_defaults(func=cmd_run)

    p_list = sub.add_parser("list-groups", help="Show last run groups")
    p_list.add_argument("--store", default=str(DEFAULT_STORE_PATH))
    p_list.set_defaults(func=cmd_list_groups)

    p_decide = sub.add_parser("decide", help="Approve or reject a group")
    p_decide.add_argument("group_id")
    p_decide.add_argument(
        "decision", choices=["approve", "reject", "hardware", "upgrade"]
    )
    p_decide.add_argument("--store", default=str(DEFAULT_STORE_PATH))
    p_decide.add_argument("--actor", default="local-cli")
    p_decide.add_argument("--idempotency-key")
    p_decide.set_defaults(func=cmd_decide)

    p_replay = sub.add_parser(
        "replay",
        help="Re-run fixtures + newcomers against persisted rules",
    )
    p_replay.add_argument(
        "--store",
        help=f"Isolated store path (default: {DEMO_STORE_PATH})",
    )
    p_replay.set_defaults(func=cmd_replay)

    p_serve = sub.add_parser("serve", help="Local group-review dashboard")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8741)
    p_serve.add_argument("--no-open", action="store_true", help="Do not open browser")
    p_serve.add_argument("--store", default=str(DEFAULT_STORE_PATH))
    p_serve.set_defaults(func=cmd_serve)

    p_backup = sub.add_parser("backup", help="Create a validated store backup")
    p_backup.add_argument("--store", default=str(DEFAULT_STORE_PATH))
    p_backup.add_argument("--output")
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="Restore a validated store backup")
    p_restore.add_argument("source")
    p_restore.add_argument("--store", required=True, help="Explicit restore target")
    p_restore.add_argument(
        "--confirm-restore",
        required=True,
        metavar="TARGET",
        help="Must exactly repeat the --store target",
    )
    p_restore.set_defaults(func=cmd_restore)

    p_history = sub.add_parser("history", help="Show immutable decision history")
    p_history.add_argument("--store", default=str(DEFAULT_STORE_PATH))
    p_history.set_defaults(func=cmd_history)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
