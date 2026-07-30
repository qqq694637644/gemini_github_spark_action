from __future__ import annotations

import asyncio

from mcp import Client

from app.api.public_operations import PUBLIC_OPERATION_IDS
from app.main import app


async def validate() -> None:
    async with Client(app.state.mcp) as client:
        tool_result = await client.list_tools()
        tool_names = {tool.name for tool in tool_result.tools}
        expected = set(PUBLIC_OPERATION_IDS)
        if tool_names != expected:
            missing = sorted(expected - tool_names)
            extra = sorted(tool_names - expected)
            raise SystemExit(f"MCP tool surface mismatch. missing={missing}, extra={extra}")

        prompt_result = await client.list_prompts()
        prompt_names = {prompt.name for prompt in prompt_result.prompts}
        if "github_repository_maintenance" not in prompt_names:
            raise SystemExit("MCP maintenance prompt is missing.")

    print(f"Validated {len(tool_names)} MCP tools and the repository-maintenance prompt.")


if __name__ == "__main__":
    asyncio.run(validate())
