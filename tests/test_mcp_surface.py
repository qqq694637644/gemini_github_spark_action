from pathlib import Path

import httpx
import pytest
from mcp import Client

from app.api.public_operations import PUBLIC_OPERATION_IDS
from app.auth.dependencies import validate_bearer_token
from app.config.settings import Settings, get_settings
from app.errors import ApiError
from app.main import _bearer_from_header, app, create_app


@pytest.mark.asyncio
async def test_mcp_exposes_the_public_gateway_operations_and_skill_prompt() -> None:
    async with Client(app.state.mcp) as client:
        tool_result = await client.list_tools()
        prompt_result = await client.list_prompts()
        prompt = await client.get_prompt("github_repository_maintenance")

    assert {tool.name for tool in tool_result.tools} == set(PUBLIC_OPERATION_IDS)
    assert len(tool_result.tools) == 28
    assert {item.name for item in prompt_result.prompts} == {"github_repository_maintenance"}
    assert "Merge or close a pull request only when the user explicitly requests it." in prompt.messages[0].content.text


def test_mcp_tools_keep_nested_request_models_and_explicit_repo_identity() -> None:
    async def read_tools():
        async with Client(app.state.mcp) as client:
            return (await client.list_tools()).tools

    import asyncio

    tools = {tool.name: tool for tool in asyncio.run(read_tools())}
    prepare_schema = tools["prepareWorkspace"].input_schema
    status_schema = tools["workspaceStatus"].input_schema

    assert {"owner", "repo", "request"} <= set(prepare_schema["properties"])
    assert {"owner", "repo", "workspace_id", "request"} <= set(status_schema["properties"])


def test_gateway_secret_precedes_and_deduplicates_legacy_secret(tmp_path) -> None:
    settings = Settings(
        workspace_root=str(tmp_path / "workspaces"),
        workspace_operation_root=str(tmp_path / "operations"),
        audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}",
        gateway_action_secret="new-token,shared-token",
        gpt_action_secret="shared-token,legacy-token",
    )

    assert settings.secrets == ["new-token", "shared-token", "legacy-token"]
    assert validate_bearer_token("new-token", settings) == "new-token"
    assert validate_bearer_token("legacy-token", settings) == "legacy-token"

    with pytest.raises(ApiError, match="Invalid bearer token"):
        validate_bearer_token("wrong-token", settings)


def test_bearer_header_parser_accepts_only_bearer_scheme() -> None:
    assert _bearer_from_header("Bearer secret") == "secret"
    assert _bearer_from_header("bearer secret") == "secret"
    assert _bearer_from_header("Basic secret") is None
    assert _bearer_from_header("Bearer") is None
    assert _bearer_from_header(None) is None


def test_spark_skill_is_the_authoritative_instruction_file() -> None:
    skill = (Path(__file__).parents[1] / "SPARK_SKILL.md").read_text(encoding="utf-8")
    legacy_prompt = (Path(__file__).parents[1] / "PROMPT.md").read_text(encoding="utf-8")

    assert "Skill version: 1.0" in skill
    assert "Gemini Spark GitHub MCP Gateway" in skill
    assert "merge pull requests only when the user explicitly asks" in skill
    assert "Prompt version: 3.2" in legacy_prompt
    assert "SPARK_SKILL.md" in legacy_prompt


@pytest.mark.asyncio
async def test_streamable_http_mcp_endpoint_is_exact_and_authenticated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GATEWAY_ACTION_SECRET", "http-test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("WORKSPACE_OPERATION_ROOT", str(tmp_path / "operations"))
    monkeypatch.setenv("AUDIT_DB_URL", f"sqlite:///{tmp_path / 'audit.db'}")
    get_settings.cache_clear()
    test_app = create_app()
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }
    headers = {
        "Authorization": "Bearer http-test-secret",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    try:
        async with test_app.state.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=test_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                unauthenticated = await client.post("/mcp", json=initialize)
                authenticated = await client.post("/mcp", headers=headers, json=initialize)
                health = await client.get("/healthz")

        assert unauthenticated.status_code == 401
        assert authenticated.status_code == 200
        assert authenticated.history == []
        assert authenticated.headers["mcp-session-id"]
        assert health.status_code == 200
    finally:
        await test_app.state.github.aclose()
        test_app.state.audit.close()
        get_settings.cache_clear()
