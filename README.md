# Omarchy PR group-triage (POC)

Local proof-of-concept for clustering lookalike Omarchy pull requests, letting a human bless a **group** once, and auto-classifying later PRs that match a blessed shape.

GitHub has no PR-group primitive. Omarchy ([omacom/omarchy](https://github.com/omacom/omarchy)) has a large open-PR backlog; this tool explores grouping without merging stranger PRs. Agents never merge: they only apply a blessed shape as a local label (`auto:approved-shape` vs `needs-human`).

**This POC never writes to GitHub.** Ingest is GET-only (`gh api` or urllib). **Bless / Reject never write to GitHub** — they only update `.triage/store.json`.

## How the gate works

1. **Ingest** PRs from fixtures (default), local `gh` CLI, or GitHub REST (GET pulls + files).
2. **Static de-dupe** fingerprints each PR from normalized title + sorted paths + hunk headers. Exact fingerprints collapse together.
3. **Embeddings** use character n-gram TF-IDF + cosine similarity over title, body, and paths (stdlib only; deterministic; no API keys).
4. **Cluster** greedily: join a cluster if max similarity to its centroid is >= 0.72 (configurable); static-exact matches always share a cluster.
5. **Summaries** are templates (count, shared files, title variants, suggested decision: duplicate / related-theme / unique). `--llm` is a stub unless you only want a note about `OPENAI_API_KEY`.
6. **Human gate**: `triage decide <group_id> approve|reject` (or the dashboard Bless/Reject buttons) persists a trusted rule. Rejected groups are remembered so they are not auto-approved.
7. **Auto-classify**: a new PR matching an APPROVED rule (exact fingerprint **or** cosine >= 0.85 to centroid **and** overlapping files) gets `auto:approved-shape` in the local store only. Strangers stay `needs-human`. Never merge.

State lives in `.triage/store.json` (gitignored). PR file cache lives under `.triage/cache/` (also gitignored). Fixtures stay in-repo.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
triage demo
pytest
```

For live ingest via `gh` (preferred): install [GitHub CLI](https://cli.github.com/) and run `gh auth login`. No `GITHUB_TOKEN` required for `--source gh`.

Optional one-time frontend build (already committed under `triage/web/vendor/`):

```bash
npm install
npm run build   # → triage/web/vendor/virtual.js (TanStack Virtual)
```

`triage serve` needs **no npm at runtime** — the vendor bundle is committed.

## CLI

```bash
triage demo
triage run --source fixtures
triage run --source gh --repo omacom/omarchy            # default: all open PRs
triage run --source gh --repo omacom/omarchy --limit 80
triage run --source github --repo omacom/omarchy --limit 20   # gh first, else GITHUB_TOKEN
triage list-groups
triage decide G001 approve
triage decide G001 reject
triage replay
triage serve --host 127.0.0.1 --port 8741
triage serve --no-open --port 8741
```

`demo` is the offline wow path: cluster fixtures, auto-approve the largest duplicate group, then classify two newcomers (one matching hyprland shape, one stranger).

### Dashboard (`triage serve`)

Local stdlib web UI (no npm/CDN at runtime): **Groups | All PRs** toggle on the left (TanStack Virtual for long lists), selected group detail in the center, **neighborhood graph** for that group (not a giant all-PR hairball). Optional group-overview toggle.

- Fetch with `fixtures` (instant) or `gh` (live). Default **limit is 0 = all open PRs**. First `gh` fetch is slow (~minutes for ~2200 PRs + files) then cache under `.triage/cache/` makes reloads fast.
- Fetch is **async**: POST `/api/fetch` returns immediately; poll GET `/api/progress` (UI does this every 400ms). Status shows e.g. `files 142/2204`. When ready, UI reloads state and shows `N PRs · M groups`.
- **All PRs** tab lists every ingested PR (virtualized). Click a row → select its group and highlight that PR. Pairwise graph edges may stay capped (~400) for viz only; **no PR is dropped from `last_prs`**.
- Bless still local-only (never writes to GitHub).
- Group detail shows an **overlap matrix** (jaccard / shared / unique) and **stacked diffs** when you click a file (patches capped at 8k chars in the store). Member lists with >40 PRs are virtualized.

## Layout

- `triage/` — `cli`, `dedupe`, `embed`, `cluster`, `summarize`, `store`, `github`, `gh`, `graph`, `overlap`, `pipeline`, `models`, `server`, `web/` (+ `vendor/virtual.js`)
- `web-src/virtual.js` + `package.json` — esbuild source for the TanStack Virtual vendor bundle
- `fixtures/prs.json` — ~12 Omarchy-ish PRs
- `fixtures/newcomers.json` — matching newcomer + stranger
- `tests/` — pytest, no network (gh subprocess mocked)

## Constraints

Python 3.11+, stdlib + pytest only (runtime). Node is only needed to rebuild the committed vendor bundle. No sentence-transformers, no OpenAI client, no git clone of Omarchy. No machineId / user-computer writes from this POC tree.
