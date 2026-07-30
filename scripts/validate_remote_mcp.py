from __future__ import annotations

import json
import os
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.mcp.tool_names import MCP_TOOL_NAMES

PROTOCOL_VERSION = "2025-06-18"


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


def parse_mcp_response(response: httpx.Response) -> dict:
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
            "clientInfo": {"name": "remote-mcp-validator", "version": "2.0.0"},
        },
    }

    with httpx.Client(timeout=30, follow_redirects=False) as client:
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

        tools_response = client.post(
            mcp_url,
            headers=session_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tools_payload = parse_mcp_response(tools_response)
        tools = tools_payload.get("result", {}).get("tools", [])
        names = {str(tool.get("name")) for tool in tools if isinstance(tool, dict)}
        if names != MCP_TOOL_NAMES:
            raise SystemExit(
                f"MCP tool surface mismatch. missing={sorted(MCP_TOOL_NAMES - names)}, extra={sorted(names - MCP_TOOL_NAMES)}"
            )

        termination = client.delete(mcp_url, headers=session_headers)
        if termination.status_code not in {200, 204}:
            raise SystemExit(f"MCP session termination failed: HTTP {termination.status_code}")

    print(f"Remote MCP validation passed: {len(names)} tools, protocol {PROTOCOL_VERSION}, session termination confirmed.")


if __name__ == "__main__":
    main()
