# Triage Workspace

Triage Workspace is a local-first proof of concept for turning a large pull-request backlog into reviewable file-set groups. Grouping is advisory. The dashboard supports patch comparison, revision-bound group and per-PR decisions, and agent-created proposals that require human acceptance. Local agents can connect through MCP; supported browsers can expose WebMCP tools.

The application never writes to GitHub. It reads pull-request metadata and files, stores local JSON state, and serves a loopback-only dashboard. It does not merge, label, comment on, or approve a GitHub pull request.

## Quick start: sync from the UI

After [installation](#requirements-and-installation), activate your environment
and run this from the repository directory:

```bash
triage serve --host 127.0.0.1 --port 8741
```

Open <http://127.0.0.1:8741> and click **New workspace**. Choose the source,
enter an `owner/repo` (for example, `omacom/omarchy`), choose the file-evidence
cap, and click **Create & sync**. The first creation saves the profile in this
browser and starts an explicit, refreshing Sync; authenticate GitHub first
with `gh auth login` if needed. No separate `triage run` command is required.

The server uses a separate workspace for each repository by default. Starting
it does not fetch anything. Existing workspaces appear in the selector: click
**Open**, use **Settings** → **Save** to update that repository's source or cap,
then click **Sync** when ready. **Refresh** is checked by default; uncheck it to
load existing cached evidence without network access. Merely opening an
unknown repository never creates a server store.

Source and file-cap profiles are saved locally in browser storage for this
origin. The durable server store is separate: it lives under
`.triage/workspaces/<owner>/<repo>/store.json`, while immutable GitHub patch
cache remains under `.triage/cache/<owner>/<repo>/`. In the UI, file cap `0`
means the server maximum of 5,000; direct CLI `--limit 0` remains unlimited.

## Safety model

- PRs are grouped only by deterministic changed-file-set overlap. Titles, embeddings, SimHash, and coarse fingerprints do not create approval edges.
- Every undecided or changed revision stays pending. Group decisions require the reviewed membership snapshot to match; per-PR decisions survive regrouping only while their repository, source, and exact revision identity remain valid. Duplicate decisions also require the referenced PR revision to remain valid.
- Approval requires complete evidence for every group member. Missing, binary, malformed, truncated, renamed-without-source, or GitHub-capped evidence is shown as incomplete and fails closed.
- Reject, hardware, and upgrade dispositions may be recorded against incomplete evidence, but the event retains that limitation.
- Legacy rules remain visible as unverified history. They are not promoted into revision-verified approval.
- Agent proposals are drafts, not approvals. MCP cannot directly accept a proposal, save a human decision, trigger a refresh, or write to GitHub. PR text and tool results are untrusted content, not instructions.
- Concurrent cooperating processes use a lock, optimistic versions, and atomic JSON replacement. This is one local store, not a network database.

The GitHub pull-files endpoint has a 3,000-file response ceiling, so reaching that ceiling is explicitly incomplete rather than silently complete ([GitHub REST documentation](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files)).

## Requirements and installation

- Python 3.11 or newer
- macOS or Linux/POSIX; persistence uses Unix `flock` through Python's [`fcntl`](https://docs.python.org/3/library/fcntl.html)
- The base app uses only the Python standard library; optional MCP support installs the pinned official SDK and its dependencies

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
triage serve --store .triage/demo-store.json
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

When a PR changes during reconciliation, it remains a pending incomplete stub
until its complete head/base revision is bound; the bounded refresh makes up to
two additional reconciliation rounds for changing revisions. Repeatedly moving
or unknown revisions remain incomplete pending stubs, and old patch evidence is
never attached.
The dashboard keeps loading through listing, file download, reconciliation,
grouping, saving, state loading, and rendering, with progress describing the
current phase.

For direct CLI runs, use a separate fixed store for each repository. Commands
refuse to replace a nonempty workspace with another repository:

```bash
triage run --source gh --repo owner/repo --refresh \
  --store /path/to/repo-workspace/store.json
```

The GitHub cache remains `.triage/cache` relative to the current working
directory even when `--store` is custom. Run refresh, cached load, and the
server from the same repository workspace so stored snapshot references resolve
to the intended immutable cache. A JSON-store backup alone does not include
GitHub patch snapshots; see [Backup and restore](#backup-and-restore).

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

The CLI examples above decide entire groups. In the dashboard, select a PR to
record an individual decision (called a **disposition** in the current UI):

| Decision | Meaning |
|---|---|
| Pending | Undecided or waiting for more information |
| Keep | Retain this candidate for further review; not approval to merge |
| Duplicate | Link to another PR in the group as the preferred candidate |
| Reject | Do not pursue this change |
| Needs hardware | Requires validation on relevant hardware |
| Can break upgrade | Requires upgrade-compatibility review |

Enter a reason and save explicitly. **Canonical PR** means the preferred PR
and is selectable only for a duplicate decision. Keep and duplicate decisions
require complete evidence. Saving one PR does not decide its siblings.

## Dashboard

```bash
triage serve --host 127.0.0.1 --port 8741 --store /path/to/store.json
```

The server accepts only literal loopback or `localhost` binding, validates Host/Origin/session tokens for mutations, and serves static files from the installed package. It is a local review tool, not a production or team server. Python documents `http.server` as unsuitable for production because it provides only basic security checks ([Python documentation](https://docs.python.org/3/library/http.server.html)). Do not expose this process through a LAN bind, reverse proxy, tunnel, or public hostname. Authentication, roles, TLS, and repository authorization are release gates for team use.

The dashboard separates cache-only loading from explicit Refresh. It shows the complete all-open list, persistent pending queues, revision/evidence status, paged file comparisons, and advisory related-PR results. Approval is disabled when the selected group lacks complete evidence.

### Per-repository workspaces

An ordinary `triage serve` runs in multi-workspace mode. Its default root is
`.triage/workspaces`, with stores at
`.triage/workspaces/<owner>/<repo>/store.json`; owner and repository names are
normalized to lower case. The GitHub cache remains in the existing
`.triage/cache/<owner>/<repo>/` namespace. Use `--workspace-root PATH` to
choose another workspace root.

An existing `.triage/store.json` is an unmoved legacy workspace when its JSON
contains a valid repository identity. It remains at that path and is used for
that repository; the server never moves or copies it into the namespaced root.
If no valid legacy store exists, the stable default is `omacom/omarchy`.

The dashboard keeps browser-local profile drafts separate from the displayed
state. **New workspace** asks for source, owner/repo, and file cap, then
**Create & sync** saves that profile and starts the first refreshing Sync.
Existing workspaces use the selector and **Open**; **Settings** → **Save** only
updates that repository's browser-local profile, while **Sync** remains a
separate explicit action. Switching repositories does not carry over
decisions, proposals, progress, or cached evidence from the previous
workspace. An unsaved switch is confirmed before the active workspace changes.

To keep the server on one fixed store for compatibility or embedding, pass an
explicit `--store PATH`:

```bash
triage serve --store /path/to/repo-workspace/store.json
```

`--store` and `--workspace-root` are mutually exclusive. Fixed-store mode
retains the existing repository guard and does not turn one JSON store into a
multi-repository store.

**Queue** organizes work into actionable piles; **Groups** browses the file-set
groups; **All PRs** lists individual contributions. Group lists sort by PR count,
which helps comparison but is not a risk or urgency ranking. The UI remembers a
valid review position and supports saving a chosen group decision and moving to
the next pending group.

## MCP and WebMCP

Install the optional MCP extra in the same environment:

```bash
python -m pip install -e '.[mcp]'
# Keep the dashboard backend running in a separate terminal.
triage mcp --url http://127.0.0.1:8741
```

This command runs a stdio bridge, not an HTTP MCP endpoint. Configure your MCP
client to launch it. The bridge talks only to the existing loopback backend;
it does not open the store directly or start a dashboard or synchronization.

Nine read tools expose the active workspace and cached evidence:
`get_workspace`, `list_groups`, `search_prs`, `get_group`, `get_pr`,
`read_patch`, `compare_prs`, `find_related`, and `get_history`.
Results include repository/snapshot context, pagination, and evidence limits.

The tenth tool, `propose_triage`, creates a revision-bound draft with proposed
per-PR decisions, reasons, and optional canonical references. In the dashboard,
**Recent proposals** lists recent proposals for the selected group. It stays
empty until a proposal is created; simply opening a group does not run AI.
A human can inspect, edit, accept, or reject a draft. Standalone MCP does not
expose proposal acceptance or a dedicated proposal-list/inspection tool.

### Pi and OMP

Copy [`.mcp.example.json`](.mcp.example.json) to `.mcp.json` and replace the
`command` and `cwd` paths with this checkout's absolute paths. The example uses
`.venv/bin/python` and port **8741**. `.mcp.json` is ignored by Git so local paths
and configuration stay private.

- **OMP:** launch from this repo. In an existing session, run `/mcp reload`,
  then `/mcp test omarchy-triage`.
- **Pi:** install the pinned repo-local adapter with
  `pi install npm:pi-mcp-adapter@2.33.0 --local`, restart Pi, and run `/mcp`.
  Review and approve project-local configuration if prompted. The package is
  recorded in `.pi/settings.json`; installed files stay in ignored `.pi/npm/`,
  separate from the app's npm dependencies.

### Browser tools

On browsers with `document.modelContext` support, the app registers the nine
read tools plus `get_view_context`, `set_filters`, `open_target`, and
`show_proposal`. View actions reject stale context. WebMCP does not expose
decision acceptance, Fetch, or reset. Unsupported browsers retain the normal
dashboard without agent tools.

Connecting an agent does not establish that a human examined the evidence.
Before using a hosted model, consider that retrieved PR content may be sent to
that model by its client; the local MCP bridge is not a privacy boundary for
the agent's subsequent use of results.

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

Backup validates the source JSON before writing. Restore validates the backup, requires exact target confirmation, retains the prior target as `store.json.pre-restore`, and advances the store version so stale browser tabs cannot submit decisions.

The backup command copies **JSON only**. To preserve patch evidence, stop
writers and also archive the repository's `.triage/cache/OWNER/REPO/` namespace,
including immutable snapshots referenced by the store. Keep the same relative
cache layout when restoring. First restore into a separate temporary workspace
and verify groups, history, and representative patches. Missing evidence must
remain unavailable, not be replaced with a newer snapshot. Snapshot retention
is currently operator-managed; do not delete snapshots still needed by reviews.

## Development and validation

```bash
pytest
node --check triage/web/app.js
node --check triage/web/webmcp.js
node tests/ui_contracts.cjs triage/web/app.js
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
- `.mcp.example.json` — MCP client configuration template
- `.pi/settings.json` — pinned repo-local Pi adapter package

Internal review notes and runbooks under `docs/` are local-only and ignored by
Git. Public installation and operating instructions are kept in this README.
