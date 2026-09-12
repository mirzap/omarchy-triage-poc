# Local operations

This runbook is for one local Omarchy triage workspace on macOS or Linux. It is not a deployment guide for a shared or internet-facing service.

## Workspace layout

Choose one working directory per repository, always start sync/server commands
from that directory, and keep its files together:

```text
/path/to/workspace/
└── .triage/
    ├── store.json
    ├── store.json.bak
    ├── .store.json.lock
    ├── rank-cache.json
    ├── together.key or openrouter.key   # optional; selected provider only
    └── cache/OWNER/REPO/
```

The default store and cache are resolved relative to the current working
directory. A custom `--store` changes the store and rank-cache/key-file
location, but it does not change the GitHub cache root. Therefore, `cd` to the
same repository workspace before every cache load, refresh, or server start.
GitHub evidence snapshots are under
`.triage/cache/OWNER/REPO/snapshots/`. The store records the immutable snapshot
identifier that its evidence came from; do not manually repoint or edit cache
manifests.

The JSON store supports cooperating local writer processes through POSIX `flock`, optimistic store/snapshot versions, fsync, and atomic replacement. Python exposes `flock` only on Unix platforms ([Python `fcntl` documentation](https://docs.python.org/3/library/fcntl.html)). Parent-directory aliases resolve to one lock identity, but the store file, explicit backup destination, and restore source must not themselves be symbolic links. Store and snapshot counters are limited to JavaScript's maximum safe integer; an exhausted counter fails without replacing the file. Do not put the store on a filesystem whose locking or atomic-rename behavior is unknown. It is not a shared network database.

## Start and stop

```bash
cd /path/to/workspace
triage serve --host 127.0.0.1 --port 8741 --store /path/to/workspace/.triage/store.json
```

Stop with Ctrl-C. The server intentionally refuses wildcard and non-loopback binds. Do not put it behind a reverse proxy or tunnel: the stdlib server is not production-grade ([Python `http.server` documentation](https://docs.python.org/3/library/http.server.html)), and this application has no team authentication, roles, TLS termination, or repository ACLs.

## Cache-only load and explicit refresh

Cache-only is the default and performs no network or authentication probe:

```bash
cd /path/to/workspace
triage run --source gh --repo owner/repo --cached \
  --store /path/to/workspace/.triage/store.json
```

Use explicit refresh for the initial synchronization or when upstream state should be checked:

```bash
cd /path/to/workspace
triage run --source gh --repo owner/repo --refresh \
  --store /path/to/workspace/.triage/store.json
```

For `--source gh`, authenticate GitHub CLI before refresh. For `--source github`, refresh prefers GitHub CLI and otherwise requires `GITHUB_TOKEN`. Both adapters are read-only and share the same cache format.

A populated workspace is bound to both its repository and normalized source (`gh` and `github` are the same GitHub source). A fixture run or replay cannot replace a GitHub workspace, even when the repository name matches; choose a separate `--store`.

`--limit` bounds new file downloads but never truncates open-PR eligibility. GitHub documents that a pull-files response itself is capped at 3,000 files ([GitHub REST documentation](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files)); capped evidence remains incomplete. A refresh fetches and revalidates the complete open list, stages immutable files, then swaps the cache pointer. Direct HTTPS transport refuses redirects while credentials are present, bounds response/error bytes, and bounds pagination by page count and total time. Exceeding a bound fails the refresh instead of publishing a partial eligible list. If later pipeline publication conflicts, the store continues to point to its older immutable snapshot.

If refresh fails:

1. Read the error; do not delete the cache or store.
2. Continue with the last store-pinned snapshot if available.
3. Resolve authentication, rate-limit, or connectivity issues.
4. Retry with `--refresh`. Cached mode never repairs a missing cache by contacting GitHub.

## Backup

Create a validated backup before upgrades, manual filesystem work, or a restore:

```bash
triage backup \
  --store /path/to/workspace/.triage/store.json \
  --output /safe/location/store-2026-09-12.json
```

The command parses and structurally validates the current schema before atomically writing the backup. A corrupt primary is reported; it is not silently replaced with an older file. Ordinary successful store writes also retain the preceding valid state as `store.json.bak`.

`triage backup` backs up the JSON store only. A complete GitHub evidence backup
must also retain every immutable cache snapshot referenced by
`last_prs[].cache_snapshot_id`, under the same relative
`.triage/cache/OWNER/REPO/snapshots/` layout. Backing up metadata alone cannot
recreate GitHub patches. It is acceptable to archive the entire repository
cache namespace after stopping writers; verify that the referenced snapshot
directories and their `pulls.json`, `fetch-metadata.json`, and file records are
present. Preserve the same workspace-relative cache location when restoring.

Keep provider key files outside backups and exports. The store and rank cache should not contain the credential, but may contain repository metadata and patch-derived content; protect them as source-code review data.

## Restore drill

Always restore the JSON and its referenced immutable cache snapshots into a new
temporary workspace first:

```bash
mkdir -p /tmp/triage-restore-drill
cd /tmp/triage-restore-drill
# Restore the archived .triage/cache/OWNER/REPO snapshot tree here first.
triage restore /safe/location/store-2026-09-12.json \
  --store /tmp/triage-restore-drill/.triage/store.json \
  --confirm-restore /tmp/triage-restore-drill/.triage/store.json

triage history --store /tmp/triage-restore-drill/.triage/store.json
triage list-groups --store /tmp/triage-restore-drill/.triage/store.json
triage serve --no-open --host 127.0.0.1 --port 8742 \
  --store /tmp/triage-restore-drill/.triage/store.json
```

Inspect the repository, group counts, pending queue, decision history, and
representative revision evidence. Missing referenced cache snapshots must be
reported as unavailable/incomplete evidence, never substituted from a newer
active snapshot. Stop the drill server before proceeding.

For the real restore, repeat the exact target path in `--confirm-restore`:

```bash
triage restore /safe/location/store-2026-09-12.json \
  --store /path/to/workspace/.triage/store.json \
  --confirm-restore /path/to/workspace/.triage/store.json
```

Restore validates the source, retains existing target bytes as `store.json.pre-restore`, and assigns a newer JavaScript-safe store version. Open browser tabs must reload; stale decisions will conflict. If validation fails, the target is not replaced.

## Legacy store handling

Schema version 2 is current. Future unknown schema versions fail explicitly. Older JSON can be read for compatibility, but rules without revision provenance are marked legacy/unverified and do not approve current code. Before allowing any transaction to persist normalized legacy data:

1. Create a validated backup.
2. Run `triage history` and record counts.
3. Load the same repository into the workspace.
4. Confirm legacy entries remain unverified and current revisions remain pending.
5. Repeat the load once to verify it is stable; never edit schema fields by hand.

Legacy pull bodies and patches may be shown only as explicitly unverified previews; they are never substituted from the current active snapshot. A legacy group with no persisted revision binding cannot record any disposition. Reload from Cached evidence or explicitly Refresh first, review the newly bound group, and then decide.

There is no SQLite migration in this release.

## External ranking

External embedding work is off unless a user invokes the explicit enrichment action. Local/cache-only ranking does not read credentials.

Select exactly one supported provider:

```bash
# Together, fixed model intfloat/multilingual-e5-large-instruct
export EMBED_PROVIDER=together
export TOGETHER_API_KEY='...'

# Or OpenRouter, fixed model voyageai/voyage-code-4
export EMBED_PROVIDER=openrouter
export OPENROUTER_API_KEY='...'
```

Instead of an environment variable, put `together.key` or `openrouter.key` beside the selected store. Only the selected provider's key location is read. The file must be regular, bounded, UTF-8 text; symlinks are refused. Invalid provider names and nonmatching `EMBED_MODEL` overrides fail without fallback.

The external request sends bounded patch-derived query/candidate text. Treat provider configuration as a privacy and cost decision. Together and OpenRouter document their respective embedding request contracts ([Together](https://docs.together.ai/docs/inference/embeddings/embeddings), [OpenRouter](https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings)).

## Incident checklist

### Store corruption

1. Stop every process using that store.
2. Preserve the corrupt file; do not overwrite it.
3. Validate `store.json.bak` with `triage history --store ...` or restore a known backup into a drill path.
4. Restore explicitly and retain the generated `.pre-restore` copy.
5. Review the last immutable decision events and reload browser state.

### Stale decision conflict

Reload state, inspect the new store version and group snapshot, then decide again only after reviewing the current revisions. Do not replay an old request with a new expected version without renewed review.

### Incomplete evidence

Do not approve. Refresh explicitly if evidence may be recoverable. Binary/missing/truncated/capped evidence may remain incomplete by design; record only an appropriate limited disposition or review upstream directly.

### Repository mismatch

Do not replace the workspace. Choose another `--store` path. Repository identity is part of decision and cache identity.

## Team-use gate

Before any LAN, hosted, or multi-user use, design and verify authentication, roles, reviewer identities, repository authorization, TLS, audit access, background-job ownership, backup retention, and conflict UX. Until then, run one loopback-only server per local workspace.
