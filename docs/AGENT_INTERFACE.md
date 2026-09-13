# Agent interface

The optional MCP adapter exposes the local triage backend's nine read tools and
one draft-only proposal tool over standard input/output. It never opens the
JSON store, contacts GitHub, or accepts a decision. The local backend remains
the only owner of store access and mutations.

## Install

Use the project's Python 3.11 environment and install the optional, pinned
official MCP SDK:

```sh
python3.11 -m pip install -e '.[mcp]'
```

The base installation has no MCP dependency. If the SDK is missing, the CLI
prints an install hint and exits without writing protocol output to stdout.

## Configure and run

Start the local dashboard/backend with its normal command, then point an MCP
host at:

```sh
triage mcp --url http://127.0.0.1:8741
```

The URL must be plain HTTP with an explicit port and exactly one of
`localhost`, `127.0.0.1`, or `::1`; credentials, paths, queries, fragments,
redirects, and proxies are rejected. The bridge obtains the backend session
token privately in memory, fetches the shared schemas from
`GET /api/tools/definitions`, and forwards only the allowlisted read
operations through `POST /api/tools/read`. If a read receives the backend's explicit
`csrf_required` 403 (for example after a dashboard restart), the bridge
refreshes its private session once and retries that read once; other 403s are
not retried.

## Example journey

An MCP host can call `get_workspace` to understand the snapshot, use
`list_groups` or `search_prs` to page through the complete queue, then inspect
`get_group`, `get_pr`, `read_patch`, `compare_prs`, `find_related`, and
`get_history`. Calls return the backend's typed envelope, including repository,
store/snapshot versions, pagination, and evidence completeness. Repeat those
versions when continuing a read so stale snapshots are visible.

`propose_triage` may create a local draft by sending `repo`, `group_id`,
revision-bound `items`, exact `expected_store_version` and
`expected_snapshot_version`, and an 8–128-character `idempotency_key` to
`POST /api/proposals/draft`. `canonical_pr`, `provenance`, `context`, and
`actor` are optional. The bridge does not retry this request after sending it,
because the backend may have committed the draft. A human must inspect and
explicitly accept, edit, or reject it; the MCP tool cannot do so. No fetch,
reset, enrich, shell, or GitHub-write operation is exposed.

## Privacy and trust

The backend is loopback-only, but returned PR titles, descriptions, patches,
and history are untrusted data. Do not treat their contents as instructions or
send them to an external service without the maintainer's approval. The bridge
keeps the session token in memory and does not include it in tool results or
logs; HTTP response and request sizes are bounded and calls have timeouts.
