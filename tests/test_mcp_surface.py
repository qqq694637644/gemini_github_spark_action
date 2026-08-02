from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client, MCPError
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken, TokenVerifier

from app.auth.mcp import (
    READ_SCOPE,
    WRITE_SCOPE,
    OAuthTokenVerifier,
    StaticBearerTokenVerifier,
    authorize_repository,
)
from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.main import create_app
from app.mcp.server import _invoke_tool
from app.mcp.tool_names import MCP_TOOL_NAMES


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "public_base_url": "http://testserver",
        "workspace_root": str(tmp_path / "workspaces"),
        "workspace_operation_root": str(tmp_path / "operations"),
        "audit_db_url": f"sqlite:///{tmp_path / 'audit.db'}",
        "allow_all_repos": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def mcp_json(response: httpx.Response) -> dict[str, Any]:
    if "application/json" in response.headers.get("content-type", ""):
        payload = response.json()
        assert isinstance(payload, dict)
        return payload
    for line in response.text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line.removeprefix("data:").strip())
            assert isinstance(payload, dict)
            return payload
    raise AssertionError(f"MCP response has no JSON payload: {response.text[:500]}")


class AcceptingTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "valid-oauth-token":
            return None
        return AccessToken(
            token=token,
            client_id="spark-client",
            subject="user-123",
            scopes=[READ_SCOPE, WRITE_SCOPE],
            resource="http://testserver/mcp",
            claims={"github_repositories": ["acme/*"]},
        )


class ReadOnlyTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "read-only-token":
            return None
        return AccessToken(
            token=token,
            client_id="read-only-client",
            subject="read-only-user",
            scopes=[READ_SCOPE],
            resource="http://testserver/mcp",
            claims={"github_repositories": ["acme/demo"]},
        )


class CapturingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


@pytest.mark.asyncio
async def test_mcp_exposes_native_tool_surface_and_skill_prompt(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, gateway_action_secret="test-token"), enforce_single_instance=False)
    try:
        async with Client(app.state.mcp) as client:
            tool_result = await client.list_tools()
            prompt_result = await client.list_prompts()
            prompt = await client.get_prompt("github_repository_maintenance")
    finally:
        await app.state.github.aclose()
        app.state.audit.close()

    names = {tool.name for tool in tool_result.tools}
    assert names == MCP_TOOL_NAMES
    assert len(names) == 32
    assert "workspaceCommand" not in names
    assert {
        "workspaceCommandStart",
        "workspaceCommandGet",
        "workspaceCommandLogs",
        "workspaceCommandCancel",
        "workspaceCommandList",
    } <= names
    assert {item.name for item in prompt_result.prompts} == {"github_repository_maintenance"}
    assert "Merge or close a pull request only when the user explicitly requests it." in prompt.messages[0].content.text


@pytest.mark.asyncio
async def test_command_tools_have_flat_mcp_native_schemas(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, gateway_action_secret="test-token"), enforce_single_instance=False)
    try:
        async with Client(app.state.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    finally:
        await app.state.github.aclose()
        app.state.audit.close()

    start = tools["workspaceCommandStart"].input_schema
    logs = tools["workspaceCommandLogs"].input_schema
    assert "request" not in start["properties"]
    assert {"owner", "repo", "workspace_id", "idempotency_key", "script"} <= set(start["properties"])
    assert {"owner", "repo", "workspace_id", "operation_id", "stdout_offset", "stderr_offset"} <= set(
        logs["properties"]
    )


@pytest.mark.asyncio
async def test_static_streamable_http_auth_initialize_and_session_termination(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, gateway_action_secret="http-test-secret"), enforce_single_instance=False)
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
    base_headers = {
        "Authorization": "Bearer http-test-secret",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    async with app.state.mcp.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            unauthenticated = await client.post("/mcp", json=initialize)
            authenticated = await client.post("/mcp", headers=base_headers, json=initialize)
            session_id = authenticated.headers.get("mcp-session-id")
            session_headers = {
                **base_headers,
                "MCP-Session-Id": session_id or "",
                "MCP-Protocol-Version": "2025-06-18",
            }
            await client.post(
                "/mcp",
                headers=session_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            tool_error = await client.post(
                "/mcp",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "workspaceStatus",
                        "arguments": {
                            "owner": "acme",
                            "repo": "demo",
                            "workspace_id": "ws_missing",
                            "request": {},
                        },
                    },
                },
            )
            termination = await client.delete(
                "/mcp",
                headers=session_headers,
            )
            health = await client.get("/healthz")

    await app.state.github.aclose()
    app.state.audit.close()

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.history == []
    assert session_id
    assert mcp_json(tool_error)["error"]["data"]["error_code"] == str(ErrorCode.WORKSPACE_NOT_FOUND)
    assert termination.status_code in {200, 204}
    assert health.status_code == 200
    assert health.json()["auth_mode"] == "static_bearer"


@pytest.mark.asyncio
async def test_oauth_protected_resource_metadata_and_challenge(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_auth_mode="oauth",
        mcp_oauth_issuer_url="https://id.example.com/tenant",
        mcp_oauth_required_scopes="github:read,github:write",
    )
    app = create_app(settings, token_verifier=AcceptingTokenVerifier(), enforce_single_instance=False)
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
        "Authorization": "Bearer valid-oauth-token",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    async with app.state.mcp.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            challenge = await client.post("/mcp", json=initialize)
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
            authenticated = await client.post("/mcp", headers=headers, json=initialize)
            session_headers = {
                **headers,
                "MCP-Session-Id": authenticated.headers.get("mcp-session-id", ""),
                "MCP-Protocol-Version": "2025-06-18",
            }
            await client.post(
                "/mcp",
                headers=session_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            tool_error = await client.post(
                "/mcp",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "workspaceStatus",
                        "arguments": {
                            "owner": "acme",
                            "repo": "demo",
                            "workspace_id": "ws_missing",
                            "request": {},
                        },
                    },
                },
            )

    await app.state.github.aclose()
    app.state.audit.close()

    assert challenge.status_code == 401
    assert "resource_metadata=" in challenge.headers["www-authenticate"]
    assert metadata.status_code == 200
    payload = metadata.json()
    assert payload["resource"] == "http://testserver/mcp"
    assert payload["authorization_servers"] == ["https://id.example.com/tenant"]
    assert payload["scopes_supported"] == ["github:read", "github:write", "github:workflow", "github:merge"]
    assert authenticated.status_code == 200
    assert authenticated.headers["mcp-session-id"]
    assert mcp_json(tool_error)["error"]["data"]["error_code"] == str(ErrorCode.WORKSPACE_NOT_FOUND)


@pytest.mark.asyncio
async def test_prepare_workspace_requires_write_scope_for_any_writable_target(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_auth_mode="oauth",
        mcp_oauth_issuer_url="https://id.example.com",
        mcp_oauth_required_scopes="github:read",
    )
    app = create_app(settings, token_verifier=ReadOnlyTokenVerifier(), enforce_single_instance=False)
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
        "Authorization": "Bearer read-only-token",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    async with app.state.mcp.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            initialized = await client.post("/mcp", headers=headers, json=initialize)
            session_headers = {
                **headers,
                "MCP-Session-Id": initialized.headers["mcp-session-id"],
                "MCP-Protocol-Version": "2025-06-18",
            }
            await client.post(
                "/mcp",
                headers=session_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            response = await client.post(
                "/mcp",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "prepareWorkspace",
                        "arguments": {
                            "owner": "acme",
                            "repo": "demo",
                            "request": {
                                "mode": "prepare_ref",
                                "branch": "spark/test",
                                "idempotency_key": "scope-check-001",
                            },
                        },
                    },
                },
            )

    await app.state.github.aclose()
    app.state.audit.close()

    error = mcp_json(response)["error"]
    assert error["data"]["error_code"] == str(ErrorCode.AUTH_FAILED)
    assert error["data"]["status_code"] == 403
    assert error["data"]["details"]["missing_scopes"] == [WRITE_SCOPE]


@pytest.mark.asyncio
async def test_cors_is_only_enabled_for_configured_origins(tmp_path: Path) -> None:
    app = create_app(
        make_settings(
            tmp_path,
            gateway_action_secret="cors-token",
            mcp_allowed_origins="https://spark.example.com",
        ),
        enforce_single_instance=False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        allowed = await client.options(
            "/mcp",
            headers={
                "Origin": "https://spark.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,mcp-session-id",
            },
        )
        denied = await client.options(
            "/mcp",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
    await app.state.github.aclose()
    app.state.audit.close()

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://spark.example.com"
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.asyncio
async def test_legacy_rest_and_openapi_surfaces_are_retired(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, gateway_action_secret="retirement-token"), enforce_single_instance=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        openapi = await client.get("/openapi.json")
        docs = await client.get("/docs")
        legacy_repo_route = await client.get("/repos/acme/demo/pulls")
    await app.state.github.aclose()
    app.state.audit.close()

    assert openapi.status_code == 404
    assert docs.status_code == 404
    assert legacy_repo_route.status_code == 404


@pytest.mark.asyncio
async def test_static_token_verifier_uses_constant_time_membership() -> None:
    verifier = StaticBearerTokenVerifier(["alpha", "beta"])
    accepted = await verifier.verify_token("beta")
    rejected = await verifier.verify_token("gamma")
    assert accepted is not None
    assert accepted.subject == "static-client"
    assert rejected is None


def _b64url_uint(value: int) -> str:
    width = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(width, "big")).rstrip(b"=").decode("ascii")


@pytest.mark.asyncio
async def test_oauth_jwt_verifier_discovers_jwks_and_validates_claims(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "key-1",
                "use": "sig",
                "alg": "RS256",
                "n": _b64url_uint(numbers.n),
                "e": _b64url_uint(numbers.e),
            }
        ]
    }
    now = int(time.time())
    claims = {
        "iss": "https://id.example.com/tenant",
        "aud": "https://gateway.example.com/mcp",
        "sub": "user-42",
        "client_id": "gemini-spark",
        "scope": "github:read github:write",
        "github_repositories": ["acme/demo"],
        "iat": now,
        "exp": now + 300,
    }
    encoded = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server/tenant":
            return httpx.Response(
                200,
                json={"issuer": "https://id.example.com/tenant", "jwks_uri": "https://id.example.com/jwks"},
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = make_settings(
        tmp_path,
        public_base_url="https://gateway.example.com",
        mcp_auth_mode="oauth",
        mcp_oauth_issuer_url="https://id.example.com/tenant",
    )
    verifier = OAuthTokenVerifier(settings, client=client)
    try:
        token = await verifier.verify_token(encoded)
        invalid = await verifier.verify_token(f"{encoded}x")
    finally:
        await client.aclose()

    assert token is not None
    assert token.client_id == "gemini-spark"
    assert token.subject == "user-42"
    assert token.scopes == ["github:read", "github:write"]
    assert invalid is None


@pytest.mark.asyncio
async def test_oauth_introspection_rejects_revoked_tokens(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        if "active-token" in body:
            return httpx.Response(
                200,
                json={
                    "active": True,
                    "iss": "https://id.example.com",
                    "aud": "https://gateway.example.com/mcp",
                    "sub": "user-1",
                    "client_id": "spark-client",
                    "scope": "github:read",
                    "exp": int(time.time()) + 300,
                },
            )
        return httpx.Response(200, json={"active": False})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    verifier = OAuthTokenVerifier(
        make_settings(
            tmp_path,
            public_base_url="https://gateway.example.com",
            mcp_auth_mode="oauth",
            mcp_oauth_issuer_url="https://id.example.com",
            mcp_oauth_introspection_url="https://id.example.com/introspect",
        ),
        client=client,
    )
    try:
        active = await verifier.verify_token("active-token")
        revoked = await verifier.verify_token("revoked-token")
    finally:
        await client.aclose()

    assert active is not None
    assert active.subject == "user-1"
    assert revoked is None


def test_per_user_scope_and_repository_authorization(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_auth_mode="oauth",
        mcp_oauth_issuer_url="https://id.example.com",
        mcp_oauth_repo_claim="github_repositories",
        mcp_oauth_require_repo_claim=True,
    )
    token = AccessToken(
        token="opaque",
        client_id="spark-client",
        subject="user-7",
        scopes=[READ_SCOPE],
        claims={"github_repositories": ["acme/*"]},
    )
    context_token = auth_context_var.set(AuthenticatedUser(token))
    try:
        identity = authorize_repository(settings, "acme", "demo", {READ_SCOPE})
        assert identity.actor == "user-7"
        with pytest.raises(ApiError) as wrong_scope:
            authorize_repository(settings, "acme", "demo", {WRITE_SCOPE})
        with pytest.raises(ApiError) as wrong_repo:
            authorize_repository(settings, "other", "demo", {READ_SCOPE})
    finally:
        auth_context_var.reset(context_token)

    assert wrong_scope.value.status_code == 403
    assert wrong_repo.value.error_code == str(ErrorCode.REPO_NOT_ALLOWED)


@pytest.mark.asyncio
async def test_mcp_error_mapping_preserves_gateway_code_and_audits_actor(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, gateway_action_secret="unused")
    audit = CapturingAudit()
    runtime = SimpleNamespace(settings=settings, audit=audit)
    access_token = AccessToken(
        token="opaque",
        client_id="spark-client",
        subject="user-9",
        scopes=[READ_SCOPE],
        claims={"auth_mode": "static_bearer", "github_repositories": ["*"]},
    )

    async def fail() -> Any:
        raise ApiError(
            ErrorCode.WORKSPACE_NOT_FOUND,
            "Workspace missing.",
            status_code=404,
            suggestion="Prepare the workspace first.",
            details={"workspace_id": "ws_missing"},
        )

    context_token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        with pytest.raises(MCPError) as caught:
            await _invoke_tool(
                runtime,
                operation="workspaceStatus",
                owner="acme",
                repo="demo",
                required_scopes={READ_SCOPE},
                call=fail,
            )
    finally:
        auth_context_var.reset(context_token)

    assert caught.value.data["error_code"] == str(ErrorCode.WORKSPACE_NOT_FOUND)
    assert caught.value.data["status_code"] == 404
    assert caught.value.data["suggestion"] == "Prepare the workspace first."
    assert audit.events[-1]["metadata"]["actor"] == "user-9"
    assert audit.events[-1]["status_code"] == 404


def test_spark_skill_is_the_only_agent_instruction_artifact() -> None:
    root = Path(__file__).parents[1]
    skill = (root / "SPARK_SKILL.md").read_text(encoding="utf-8")
    assert "Skill version: 2.0" in skill
    assert "Gemini Spark GitHub MCP Gateway" in skill
    assert not (root / "PROMPT.md").exists()
