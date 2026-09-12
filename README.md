# Omarchy PR triage

Omarchy PR triage is a local-first proof of concept for turning a large pull-request backlog into reviewable file-set groups. Grouping is advisory. A human decision is recorded against the exact repository, group membership, head/base revisions, and available diff content that was reviewed.

The application never writes to GitHub. It reads pull-request metadata and files, stores local JSON state, and serves a loopback-only dashboard. It does not merge, label, comment on, or approve a GitHub pull request.

## Safety model

- PRs are grouped only by deterministic changed-file-set overlap. Titles, embeddings, SimHash, and coarse fingerprints do not create approval edges.
- Every undecided or changed revision stays pending. A previous decision applies only while its reviewed revision and membership snapshot still match.
- Approval requires complete evidence for every group member. Missing, binary, malformed, truncated, renamed-without-source, or GitHub-capped evidence is shown as incomplete and fails closed.
- Reject, hardware, and upgrade dispositions may be recorded against incomplete evidence, but the event retains that limitation.
- Legacy rules remain visible as unverified history. They are not promoted into revision-verified approval.
- Concurrent cooperating processes use a lock, optimistic versions, and atomic JSON replacement. This is one local store, not a network database.

The GitHub pull-files endpoint has a 3,000-file response ceiling, so reaching that ceiling is explicitly incomplete rather than silently complete ([GitHub REST documentation](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files)).

## Requirements and installation

- Python 3.11 or newer
- macOS or Linux/POSIX; persistence uses Unix `flock` through Python's [`fcntl`](https://docs.python.org/3/library/fcntl.html)
- No runtime Python dependencies outside the standard library

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
```

A wheel contains the dashboard, nested virtualizer bundle, and fixture data. The fixture files live inside the `triage` package, following setuptools' package-data model ([setuptools data-files documentation](https://setuptools.pypa.io/en/latest/userguide/datafiles.html)).

## Offline demo

```bash
triage demo
```

The demo uses `.triage/demo-store.json`, never `.triage/store.json`, performs no network calls, and leaves all newcomers at the human-review gate. If that demo store already exists, the command refuses to replace it.

To deliberately reset a named demo store, both the path and reset flag are required. The previous valid store is backed up first:

```bash
triage demo --store /tmp/omarchy-demo/store.json --reset
```

Fixture replay is isolated in the same way:

```bash
triage replay --store /tmp/omarchy-demo/store.json
```

## GitHub cache and refresh

GitHub sources are cache-only by default. A cached command never probes authentication and never turns a cache miss into a network request:

```bash
triage run --source gh --repo omacom/omarchy --cached
triage run --source github --repo omacom/omarchy --cached
```

An initial or updated synchronization requires explicit `--refresh`:

```bash
# Preferred when GitHub CLI is authenticated
triage run --source gh --repo omacom/omarchy --refresh

# Prefer gh; otherwise use GITHUB_TOKEN through the canonical REST adapter
triage run --source github --repo omacom/omarchy --refresh
```

`--limit N` limits file-evidence downloads, not the open-PR listing. Every PR from the complete fresh open list remains present. Outside the limit, same-revision verified evidence is reused; otherwise the PR remains as an incomplete pending stub. Refresh stages an immutable cache snapshot, revalidates the listing, and publishes it atomically. A failed refresh preserves the prior usable store view.

Use a separate store for each repository. Commands refuse to replace a nonempty workspace with another repository:

```bash
triage run --source gh --repo owner/repo --refresh \
  --store /path/to/repo-workspace/store.json
```

The GitHub cache remains `.triage/cache` relative to the current working
directory even when `--store` is custom. Run refresh, cached load, and the
server from the same repository workspace so stored snapshot references resolve
to the intended immutable cache. A JSON-store backup alone does not include
GitHub patch snapshots; the operations runbook covers complete evidence backup.

## Decisions and history

```bash
triage list-groups --store /path/to/store.json
triage decide G001 approve --store /path/to/store.json --actor reviewer
triage decide G002 reject --store /path/to/store.json --actor reviewer
triage decide G003 hardware --store /path/to/store.json --actor reviewer
triage decide G004 upgrade --store /path/to/store.json --actor reviewer
triage history --store /path/to/store.json
```

CLI and HTTP decisions use the current store version and an idempotency key. Stale submissions conflict instead of overwriting newer work. Decision events retain actor, time, repository, reviewed revisions, membership snapshot, and evidence completeness.

## Dashboard

```bash
triage serve --host 127.0.0.1 --port 8741 --store /path/to/store.json
```

The server accepts only literal loopback or `localhost` binding, validates Host/Origin/session tokens for mutations, and serves static files from the installed package. It is a local review tool, not a production or team server. Python documents `http.server` as unsuitable for production because it provides only basic security checks ([Python documentation](https://docs.python.org/3/library/http.server.html)). Do not expose this process through a LAN bind, reverse proxy, tunnel, or public hostname. Authentication, roles, TLS, and repository authorization are release gates for team use.

The dashboard separates cache-only loading from explicit Refresh. It shows the complete all-open list, persistent pending queues, revision/evidence status, paged file comparisons, and advisory related-PR results. Approval is disabled when the selected group lacks complete evidence.

## Optional external ranking

Local related-PR retrieval is always available and performs no provider request. External embedding reranking occurs only after a user presses the explicit external-enrichment action for the active repository and current store version. Bounded patch-derived text for the query and up to 24 candidates is then sent to the selected provider. Results remain advisory and are revision-keyed.

Supported configurations are fixed pairs; arbitrary provider or model overrides fail clearly:

| `EMBED_PROVIDER` | Fixed model | Credential environment | Optional key file beside the store |
|---|---|---|---|
| `together` (default) | `intfloat/multilingual-e5-large-instruct` | `TOGETHER_API_KEY` | `together.key` |
| `openrouter` | `voyageai/voyage-code-4` | `OPENROUTER_API_KEY` | `openrouter.key` |

`EMBED_MODEL`, when set, must exactly match the selected provider's fixed model. Only the selected provider's environment variable or bounded regular key file is read. Symlinked key files are refused. Provider requests follow the documented embedding endpoints for [Together](https://docs.together.ai/docs/inference/embeddings/embeddings) and [OpenRouter](https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings).

## Backup and restore

```bash
triage backup --store /path/to/store.json --output /safe/store-backup.json

triage restore /safe/store-backup.json \
  --store /path/to/store.json \
  --confirm-restore /path/to/store.json
```

Backup validates the source JSON before writing. Restore validates the backup, requires exact target confirmation, retains the prior target as `store.json.pre-restore`, and advances the store version so stale browser tabs cannot submit decisions. See [Operations](docs/OPERATIONS.md) for the restore drill and failure handling.

## Development and validation

```bash
pytest
node --check triage/web/app.js
npm ci
npm run build
git diff --exit-code -- triage/web/vendor/virtual.js
python -m build
```

`npm ci` requires the committed lockfile and refuses package/lock mismatch ([npm documentation](https://docs.npmjs.com/cli/commands/npm-ci/)). Node is build-time only; the committed bundle is used at runtime. Tests use temporary stores and mocked external transports. They must not use a live `.triage` directory or real provider credentials.

## Layout

- `triage/` — grouping, revision evidence, persistence, sync, retrieval, CLI, and server
- `triage/fixtures/` — installed offline fixture data
- `triage/web/` — installed dashboard assets, including `vendor/virtual.js`
- `scripts/` — deterministic local performance/retrieval benchmarks
- `tests/` — offline Python and JavaScript contract tests
- `docs/OPERATIONS.md` — local backup, restore, refresh, and incident procedures
- `docs/REVIEW_REMEDIATION.md` — R1–R14 implementation/evidence matrix
