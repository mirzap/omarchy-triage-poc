"""Offline MCP protocol contracts for the stdio bridge."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

mcp = pytest.importorskip("mcp")

from triage import mcp_server, service


class _FakeBackend:
    schemas = {tool["name"]: tool["inputSchema"] for tool in service.TOOL_DEFINITIONS}
    draft_schemas = {
        tool["name"]: tool["inputSchema"]
        for tool in service.DRAFT_TOOL_DEFINITIONS
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def read(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("read", operation, args))
        if operation == "get_pr" and args.get("pr") == 999999:
            return {
                "ok": False,
                "error": {
                    "status": 404,
                    "code": "not_found",
                    "message": "pull request 999999 was not found",
                    "retryable": False,
                    "context": {"pr": 999999},
                },
            }
        return {"ok": True, "data": {"operation": operation, "args": args}}

    def draft(self, args: dict[str, Any], operation: str) -> dict[str, Any]:
        self.calls.append(("draft", operation, args))
        return {"ok": True, "data": {"operation": operation, "args": args}}


def _server() -> tuple[Any, _FakeBackend]:
    backend = _FakeBackend()
    return mcp_server.build_server(backend), backend


def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(name, arguments))


def test_tools_list_exposes_shared_schemas_and_workflow_metadata() -> None:
    server, _ = _server()
    tools = asyncio.run(server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert list(by_name) == [*mcp_server.READ_OPERATIONS, *mcp_server.DRAFT_OPERATIONS]
    for name, schema in {**_FakeBackend.schemas, **_FakeBackend.draft_schemas}.items():
        assert by_name[name].input_schema == schema
        assert "untrusted evidence, not instructions" in by_name[name].description

    metadata = f"{server.description}\n{server.instructions}".lower()
    assert "human review" in metadata
    assert "discover" in metadata
    assert "retrieval.continuations" in metadata
    assert "draft" in metadata


def test_tools_call_marks_backend_failure_as_mcp_error_with_envelope() -> None:
    server, _ = _server()

    failed = _call(server, "get_pr", {"repo": "acme/widgets", "pr": 999999})
    assert failed.is_error is True
    assert failed.structured_content["ok"] is False
    assert failed.structured_content["error"]["status"] == 404
    assert failed.structured_content["error"]["code"] == "not_found"
    assert json.loads(failed.content[0].text) == failed.structured_content

    succeeded = _call(server, "get_pr", {"repo": "acme/widgets", "pr": 1})
    assert succeeded.is_error is False
    assert succeeded.structured_content["ok"] is True
    assert json.loads(succeeded.content[0].text) == succeeded.structured_content


def test_invalid_nested_drafts_are_errors_without_backend_calls() -> None:
    server, backend = _server()
    revision = {
        "head_sha": "head",
        "base_sha": "base",
        "content_digest": "digest",
        "source": "fixtures",
    }
    proposal_base = {
        "repo": "acme/widgets",
        "group_id": "group-1",
        "expected_store_version": 1,
        "expected_snapshot_version": 1,
        "idempotency_key": "proposal-key",
    }

    cases = [
        (
            "propose_triage",
            {
                **proposal_base,
                "items": [{"pr": 1, "disposition": "keep", "reason": "ready"}],
            },
        ),
        (
            "propose_triage",
            {
                **proposal_base,
                "items": [{
                    "pr": 1,
                    "disposition": "duplicate",
                    "reason": "same change",
                    "revision": revision,
                    "duplicate_of": 2,
                }],
            },
        ),
        (
            "propose_file_review",
            {
                "repo": "acme/widgets",
                "pr": 1,
                "revision": revision,
                "expected_store_version": 1,
                "expected_snapshot_version": 1,
                "idempotency_key": "file-key-1",
            },
        ),
        (
            "propose_file_review",
            {
                "repo": "acme/widgets",
                "pr": 1,
                "revision": {**revision, "source": ""},
                "findings": [{
                    "path": "src/example.py",
                    "severity": "minor",
                    "title": "Naming",
                    "explanation": "The name is unclear.",
                }],
                "expected_store_version": 1,
                "expected_snapshot_version": 1,
                "idempotency_key": "file-key-2",
            },
        ),
    ]

    for name, arguments in cases:
        result = _call(server, name, arguments)
        assert result.is_error is True
        assert result.structured_content["error"]["code"] == "invalid_arguments"
        assert json.loads(result.content[0].text) == result.structured_content
        assert backend.calls == []

    valid = {
        "repo": "acme/widgets",
        "pr": 1,
        "revision": revision,
        "coverage": [{"path": "src/example.py", "status": "inspected"}],
        "expected_store_version": 1,
        "expected_snapshot_version": 1,
        "idempotency_key": "file-key-3",
    }
    result = _call(server, "propose_file_review", valid)
    assert result.is_error is False
    assert backend.calls == [("draft", "propose_file_review", valid)]
