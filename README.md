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
it does not fetch anything. Selecting an existing workspace loads it immediately;
use **Settings** → **Save** to update that repository's source or cap,
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

The CLI examples above write a group-only rule. The dashboard keeps three
separate concepts, and none of them implies another:

| Concept | Scope | What it records |
|---|---|---|
| File review | one path at one exact PR revision | that you looked at that file |
| Finding | one path at one exact PR revision | one concrete concern |
| PR decision | the whole pull request at one revision | Keep/Duplicate/Reject/… |

In the dashboard, use **Decide PR #N** in the pull-request header to record an
individual decision (called a **disposition** in the API):

| Decision | Meaning |
|---|---|
| Pending | Undecided or waiting for more information |
| Keep | Retain this candidate for further review; not approval to merge |
| Duplicate | Link to another PR in the group as the preferred candidate |
| Reject | Do not pursue this change |
| Needs hardware | Requires validation on relevant hardware |
| Upgrade risk | Requires upgrade-compatibility review |

The dialog is titled *Overall decision for PR #N · all X changed files* so the
scope is unambiguous. Saved badges and group progress change only when a human
explicitly saves or accepts: **Save PR decision**, **Save PR decisions** in a
bulk review, or accepting an agent proposal. Editing in the dialog, marking
files reviewed, recording findings, and adopting a drafted finding never move
them. Enter a reason and save explicitly. **Duplicate
of pull request** is a pull-request-level relation; it does not claim any file
is equivalent and it does not Keep the other PR. Keep and Duplicate require
complete revision evidence, and no checkbox substitutes for missing evidence.
Keeping a PR that still has unresolved findings or incomplete file coverage
additionally requires an explicit acknowledgement. Saving one PR does not
decide its siblings, and resetting to pending stays available.

### File review, findings, and coverage

The **File review** bar above the diff works on the selected file only:

- **Mark file reviewed** records that you looked at that exact file revision.
  It is not approval, it is not proof the file is correct, and it never changes
  a PR decision. It requires complete patch bytes for *that* file; a missing
  patch elsewhere in the same pull request is irrelevant.
- **Add finding** records one concern with a severity, optional line/hunk,
  explanation, evidence, and suggested fix. A finding about a *missing* patch
  is explicitly allowed. Resolving, reopening, or dismissing a finding changes
  that finding and nothing else.
- Human review coverage and agent inspection coverage are reported separately,
  as in "You reviewed 1/3 files, Agent inspected 2/3 files". Agent inspection
  never counts as your review, and a file with no recorded coverage makes no
  claim either way.

Every record is bound to one exact revision. When the revision changes, older
file reviews and findings are shown as stale and need re-review; they are never
silently rebound. Marking every file reviewed never auto-approves a PR.

Finding bodies stay collapsed until you open them, and the file list, finding
bodies, agent drafts, and one selected draft findings are paged independently,
each with its own explicit load-more.

### Bulk PR review

**Bulk PR review** in the context column replaces the old front-and-centre
group decision buttons. Choose the affected pull requests, read the current to
new preview with its explicit overwrite warning, give a required reason,
confirm, and the dashboard saves one individual decision per pull request
atomically through the existing `save_dispositions` writer, sharing one event
for provenance. A batch is capped at 200 pull requests, the confirmation resets
whenever the chosen set or decision changes, and a Keep batch names every pull
request that still has unresolved findings or unreviewed files in the
acknowledgement copy. Accepting an agent proposal that Keeps such a pull
request asks for the same named acknowledgement, and it adopts no agent file
findings. None of these acknowledgements weakens the evidence gate: Keep and
Duplicate still require complete revision evidence.

Historic group-only rules are displayed read-only and clearly labelled; they
are never backfilled into individual decisions, and the dashboard no longer
offers a control that writes one. The `triage decide` CLI command and
`POST /api/decide` remain available for compatibility.

The workbench opens the selected pull-request diff first. Choose a file, then use
**Compare with…** to add another group member. **Save & next pending** saves
the individual decision and advances within the current filtered group scope.
Bulk review, agent file findings, the overall PR proposal, descriptions, and
evidence details live in the context column, which stays collapsed by default
and overlays the diff instead of resizing it. On small screens, switch between
**Work list**, **Review**, and **Context**; workspace controls are under
**Workspaces**.

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
Selecting an existing workspace loads it immediately; **Settings** → **Save** only
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

The **Files** navigator shares the left pane with the work list. Selecting a
group automatically opens Files on desktop.
You can switch back to **Work list** to choose another group. Search loaded
file paths, browse directory groups, and use **Load more files** for large PRs;
the displayed counts distinguish loaded files from the full manifest. On mobile,
Files opens a full-screen picker and selecting a file returns to its diff.
Comparison stays in the diff header. Opening a file does not record a decision,
and loading manifest pages does not recover patches missing from the cached source.

After choosing **Compare with**, choose **Auto layout**, **Side by side**, or
**Stacked**. Auto uses two columns when the diff area (not the entire window) is
at least 740 CSS pixels wide. The layout choice is remembered in this browser;
Side by side forces two columns even in a narrower window.

The right sidebar starts collapsed to an icon rail at every width; each icon opens its section in an overlay drawer without resizing the diff or changing the saved collapsed state. Close the drawer with its Close button, Escape, or a click outside it.
A saved visibility preference still wins and is applied before first paint.
The PR description sits below the title and starts collapsed for each selected PR.

## MCP and WebMCP

Agent reads return complete JSON, not silently shortened text. Large manifests
and patches are paginated: `retrieval.continuations` provides ready-to-call tool
arguments with the required store/snapshot versions. Follow the continuations
needed for the review; for a known file, call `read_patch` directly instead of
enumerating the whole group file list. Source evidence completeness is separate
from whether all pages/chunks have been read. PR descriptions are returned in full.

`get_group` defaults to 10 members and 20 shared files per page; its disposition
records contain decisions, not duplicated file manifests. Use `get_pr` for a PR's
complete paged file manifest (20 files by default, up to 80). `files_remaining`
counts manifest rows still to retrieve; `missing_patch_count` and
`source_incomplete_reasons` describe cached source gaps that pagination cannot
repair. A large PR is not inherently incomplete. External agent hosts may impose their own output or
tool-round limits; this app has no six-round agent loop. If a host clips a result,
request a smaller supported page/chunk instead of repeating the same call.

Install the optional MCP extra in the same environment:

```bash
python -m pip install -e '.[mcp]'
# Keep the dashboard backend running in a separate terminal.
triage mcp --url http://127.0.0.1:8741
```

This command runs a stdio bridge, not an HTTP MCP endpoint. Configure your MCP
client to launch it. The bridge talks only to the existing loopback backend;
it does not open the store directly or start a dashboard or synchronization.

Ten read tools expose the active workspace and cached evidence:
`get_workspace`, `list_groups`, `search_prs`, `get_group`, `get_pr`,
`read_patch`, `compare_prs`, `find_related`, `get_history`, and
`get_file_review`.
Results include repository/snapshot context, pagination, and evidence limits.

`get_file_review` returns, for one pull-request revision, the per-file human
review state, the separate agent inspection coverage, finding bodies, and agent
draft summaries. Its file manifest, finding list, draft list, and one selected
draft findings page independently (`page`, `finding_page`, `draft_page`,
`draft_finding_page`), so no single response has to carry every finding.
Passing `path` scopes the finding list to one file and reports which manifest
page holds it through `focus_page`; it never rewrites `page`, so following
`retrieval.continuations` always advances. An unknown path is an explicit
`not_found`.

Two draft-only write tools exist, and neither can approve anything:

- `propose_triage` creates a revision-bound draft with proposed per-PR
  decisions, reasons, and optional canonical references. In the dashboard,
  **Overall PR proposal** lists recent proposals for the selected group.
- `propose_file_review` drafts file findings and explicit inspection coverage
  (`inspected`, `skipped`, or `missing`) for one pull-request revision. Agent
  coverage is never inferred from the absence of a drafted finding, and clean
  files do not need a finding. In the dashboard, **Agent file findings** lists
  these drafts.

There are no drafts until an agent creates one; opening a group or a file runs
no AI. An external agent can read review context and submit a draft. It can
never mark a file human-reviewed, adopt a drafted finding, or finalize a pull
request decision: those live only behind explicit, CSRF-guarded dashboard
controls. Standalone MCP also does not expose proposal acceptance or a
dedicated proposal-list/inspection tool.

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

On browsers with `document.modelContext` support, the app registers the ten
read tools, the view actions `get_view_context`, `set_filters`, `open_target`,
and `show_proposal`, and the single draft-only write `propose_file_review`.
`open_target` may also open the file review or agent file findings panel;
opening a panel is not a review, an adoption, or a decision. View and draft
actions reject stale context. WebMCP does not expose marking a file reviewed,
adopting a drafted finding, saving a pull-request decision, Fetch, or reset.
Unsupported browsers retain the normal dashboard without agent tools.

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
