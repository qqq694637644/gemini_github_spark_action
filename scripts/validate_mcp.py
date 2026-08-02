from __future__ import annotations

import asyncio

from mcp import Client

from app.main import app
from app.mcp.tool_names import MCP_TOOL_NAMES


async def validate() -> None:
    async with Client(app.state.mcp) as client:
        tool_result = await client.list_tools()
        tool_names = {tool.name for tool in tool_result.tools}
        if tool_names != MCP_TOOL_NAMES:
            missing = sorted(MCP_TOOL_NAMES - tool_names)
            extra = sorted(tool_names - MCP_TOOL_NAMES)
            raise SystemExit(f"MCP tool surface mismatch. missing={missing}, extra={extra}")

        prompt_result = await client.list_prompts()
        prompt_names = {prompt.name for prompt in prompt_result.prompts}
        if "github_repository_maintenance" not in prompt_names:
            raise SystemExit("MCP maintenance prompt is missing.")

        split_command_tools = {
            "workspaceCommandStart",
            "workspaceCommandGet",
            "workspaceCommandLogs",
            "workspaceCommandCancel",
            "workspaceCommandList",
        }
        if "workspaceCommand" in tool_names or not split_command_tools <= tool_names:
            raise SystemExit("MCP-native workspace command tools are incomplete.")

    print(f"Validated {len(tool_names)} MCP tools and the repository-maintenance prompt.")


if __name__ == "__main__":
    asyncio.run(validate())
