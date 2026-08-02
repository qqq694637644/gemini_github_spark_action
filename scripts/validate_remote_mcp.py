from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.mcp.tool_names import MCP_TOOL_NAMES

PROTOCOL_VERSION = "2025-06-18"


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


def optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def parse_mcp_response(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = response.json()
        if not isinstance(payload, dict):
            raise SystemExit("MCP response JSON is not an object.")
        return payload

    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = json.loads(line.removeprefix("data:").strip())
        if isinstance(payload, dict):
            return payload
    raise SystemExit("MCP response did not contain a JSON or SSE data object.")


def health_url(mcp_url: str) -> str:
    parsed = urlsplit(mcp_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/healthz", "", ""))


def rpc(
    client: httpx.Client,
    mcp_url: str,
    headers: dict[str, str],
    *,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        mcp_url,
        headers=headers,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    payload = parse_mcp_response(response)
    if "error" in payload:
        raise SystemExit(f"MCP {method} failed: {payload['error']}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise SystemExit(f"MCP {method} result is not an object: {payload}")
    return result


def call_tool(
    client: httpx.Client,
    mcp_url: str,
    headers: dict[str, str],
    *,
    request_id: int,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    result = rpc(
        client,
        mcp_url,
        headers,
        request_id=request_id,
        method="tools/call",
        params={"name": name, "arguments": arguments},
    )
    if result.get("isError") is True:
        raise SystemExit(f"MCP tool {name} returned an error result: {result}")

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured

    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise SystemExit(f"MCP tool {name} did not return structured object content.")


def read_only_acceptance_config() -> tuple[str, str, str] | None:
    owner = optional_env("MCP_TEST_OWNER")
    repo = optional_env("MCP_TEST_REPO")
    ref = optional_env("MCP_TEST_REF") or "main"
    if owner is None and repo is None:
        return None
    if owner is None or repo is None:
        raise SystemExit("MCP_TEST_OWNER and MCP_TEST_REPO must be set together.")
    return owner, repo, ref


def run_read_only_github_acceptance(
    client: httpx.Client,
    mcp_url: str,
    headers: dict[str, str],
    *,
    owner: str,
    repo: str,
    ref: str,
    request_id: int,
) -> int:
    key_material = f"{owner}/{repo}:{ref}:{time.time_ns()}".encode()
    idempotency_key = f"remote-read-{hashlib.sha256(key_material).hexdigest()[:20]}"
    prepared = call_tool(
        client,
        mcp_url,
        headers,
        request_id=request_id,
        name="prepareWorkspace",
        arguments={
            "owner": owner,
            "repo": repo,
            "request": {
                "mode": "prepare_ref",
                "base_ref": ref,
                "idempotency_key": idempotency_key,
            },
        },
    )
    workspace_id = prepared.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise SystemExit(f"prepareWorkspace did not return workspace_id: {prepared}")
    if prepared.get("branch") != ref:
        raise SystemExit(f"prepareWorkspace returned unexpected ref: {prepared}")

    inspected = call_tool(
        client,
        mcp_url,
        headers,
        request_id=request_id + 1,
        name="workspaceInspect",
        arguments={
            "owner": owner,
            "repo": repo,
            "workspace_id": workspace_id,
            "request": {
                "paths": ["."],
                "queries": [],
                "max_depth": 2,
                "max_tree_entries": 100,
                "max_read_files": 0,
            },
        },
    )
    if inspected.get("workspace_id") != workspace_id or not isinstance(inspected.get("tree"), list):
        raise SystemExit(f"workspaceInspect returned an unexpected response: {inspected}")

    status = call_tool(
        client,
        mcp_url,
        headers,
        request_id=request_id + 2,
        name="workspaceStatus",
        arguments={
            "owner": owner,
            "repo": repo,
            "workspace_id": workspace_id,
            "request": {"refresh": False},
        },
    )
    if status.get("workspace_id") != workspace_id or status.get("dirty") is not False:
        raise SystemExit(f"workspaceStatus did not confirm a clean read-only workspace: {status}")

    ci = call_tool(
        client,
        mcp_url,
        headers,
        request_id=request_id + 3,
        name="queryCiStatus",
        arguments={"owner": owner, "repo": repo, "request": {"branch": ref}},
    )
    if not isinstance(ci.get("workflow_runs"), list) or not isinstance(ci.get("status"), str):
        raise SystemExit(f"queryCiStatus returned an unexpected response: {ci}")

    print(
        "Read-only GitHub acceptance passed: "
        f"repo={owner}/{repo}, ref={ref}, workspace={workspace_id}, "
        f"tree_entries={len(inspected['tree'])}, ci_status={ci['status']}."
    )
    return request_id + 4


def main() -> None:
    mcp_url = require_env("MCP_SERVER_URL").rstrip("/")
    token = require_env("MCP_ACCESS_TOKEN")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "remote-mcp-validator", "version": "2.1.0"},
        },
    }

    with httpx.Client(timeout=60, follow_redirects=False) as client:
        health = client.get(health_url(mcp_url))
        health.raise_for_status()
        health_payload = health.json()
        if health_payload.get("ok") is not True or health_payload.get("mcp_surface_version") != "2":
            raise SystemExit(f"Unexpected health response: {health_payload}")

        init_response = client.post(mcp_url, headers=headers, json=initialize)
        if init_response.history or init_response.status_code != 200:
            raise SystemExit(f"MCP initialize failed without an exact 200 response: HTTP {init_response.status_code}")
        init_payload = parse_mcp_response(init_response)
        if init_payload.get("result", {}).get("protocolVersion") != PROTOCOL_VERSION:
            raise SystemExit(f"Unexpected protocol negotiation response: {init_payload}")
        session_id = init_response.headers.get("mcp-session-id")
        if not session_id:
            raise SystemExit("MCP initialize did not return MCP-Session-Id.")

        session_headers = {
            **headers,
            "MCP-Session-Id": session_id,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        initialized = client.post(
            mcp_url,
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        if initialized.status_code not in {200, 202, 204}:
            raise SystemExit(f"MCP initialized notification failed: HTTP {initialized.status_code}")

        tools = rpc(
            client,
            mcp_url,
            session_headers,
            request_id=2,
            method="tools/list",
            params={},
        ).get("tools", [])
        names = {str(tool.get("name")) for tool in tools if isinstance(tool, dict)}
        if names != MCP_TOOL_NAMES:
            raise SystemExit(
                f"MCP tool surface mismatch. missing={sorted(MCP_TOOL_NAMES - names)}, extra={sorted(names - MCP_TOOL_NAMES)}"
            )

        acceptance = read_only_acceptance_config()
        if acceptance is not None:
            owner, repo, ref = acceptance
            run_read_only_github_acceptance(
                client,
                mcp_url,
                session_headers,
                owner=owner,
                repo=repo,
                ref=ref,
                request_id=3,
            )

        termination = client.delete(mcp_url, headers=session_headers)
        if termination.status_code not in {200, 204}:
            raise SystemExit(f"MCP session termination failed: HTTP {termination.status_code}")

    suffix = " with read-only GitHub tool calls" if acceptance is not None else ""
    print(
        f"Remote MCP validation passed: {len(names)} tools, protocol {PROTOCOL_VERSION}, "
        f"session termination confirmed{suffix}."
    )


if __name__ == "__main__":
    main()
