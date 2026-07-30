from __future__ import annotations

import inspect
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import TokenVerifier

from app.auth.mcp import ALL_SCOPES, build_mcp_auth
from app.config.settings import Settings, get_settings, parse_csv
from app.errors import register_exception_handlers
from app.github.client import GitHubClient
from app.mcp.server import GatewayRuntime, build_transport_security, create_mcp_server
from app.policy.rules import Policy
from app.single_instance import acquire_single_instance, release_single_instance
from app.storage.audit import AuditStore
from app.workspace.manager import WorkspaceManager
from app.workspace.operations import WorkspaceOperationManager

MCP_SURFACE_VERSION = "2"


def create_app(
    settings: Settings | None = None,
    *,
    token_verifier: TokenVerifier | None = None,
    enforce_single_instance: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    auth_components = build_mcp_auth(settings, token_verifier=token_verifier)
    github = GitHubClient(settings)
    policy = Policy(settings)
    audit = AuditStore(settings.audit_db_url)
    workspace_manager = WorkspaceManager(settings, github, policy)
    workspace_operation_manager = WorkspaceOperationManager(settings, recover_running=False)
    workspace_manager.set_active_workspace_provider(workspace_operation_manager.active_workspace_ids)

    runtime = GatewayRuntime(
        github=github,
        policy=policy,
        settings=settings,
        workspace_manager=workspace_manager,
        workspace_operations=workspace_operation_manager,
        audit=audit,
    )
    mcp = create_mcp_server(
        runtime,
        token_verifier=auth_components.server_token_verifier,
        auth_settings=auth_components.auth_settings,
    )
    mcp_http_app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        transport_security=build_transport_security(settings),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if enforce_single_instance:
            acquire_single_instance()
        try:
            async with mcp.session_manager.run():
                workspace_operation_manager.recover_running_operations()
                workspace_operation_manager.prune_terminal_operations()
                workspace_manager.remove_legacy_lock_files()
                yield
        finally:
            await workspace_operation_manager.shutdown()
            await github.aclose()
            for verifier in (auth_components.server_token_verifier, auth_components.static_token_verifier):
                close_verifier = getattr(verifier, "aclose", None)
                if callable(close_verifier):
                    result = close_verifier()
                    if inspect.isawaitable(result):
                        await result
            audit.close()
            if enforce_single_instance:
                release_single_instance()

    app = FastAPI(
        title="Gemini Spark GitHub MCP Gateway",
        version="2.0.0",
        description="MCP-only, workspace-first GitHub maintenance gateway for Gemini Spark.",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.github = github
    app.state.policy = policy
    app.state.audit = audit
    app.state.workspace_manager = workspace_manager
    app.state.workspace_operation_manager = workspace_operation_manager
    app.state.mcp = mcp
    app.state.mcp_token_verifier = auth_components.server_token_verifier or auth_components.static_token_verifier

    allowed_origins = parse_csv(settings.mcp_allowed_origins)
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Accept",
                "MCP-Protocol-Version",
                "MCP-Session-Id",
                "Last-Event-ID",
                "X-Request-ID",
            ],
            expose_headers=["MCP-Session-Id", "MCP-Protocol-Version", "X-Request-ID", "X-MCP-Surface-Version"],
        )

    register_exception_handlers(app)

    if settings.mcp_auth_mode == "oauth":
        protected_resource_path = f"/.well-known/oauth-protected-resource{settings.mcp_path}"

        @app.get(protected_resource_path, include_in_schema=False)
        async def oauth_protected_resource_metadata() -> JSONResponse:
            return JSONResponse(
                {
                    "resource": settings.mcp_resource_url,
                    "authorization_servers": [settings.mcp_oauth_issuer_url],
                    "scopes_supported": list(ALL_SCOPES),
                    "bearer_methods_supported": ["header"],
                }
            )

    @app.middleware("http")
    async def request_audit_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        status_code = 500
        auth_context_token = None
        try:
            if auth_components.static_token_verifier and request.url.path == settings.mcp_path and request.method != "OPTIONS":
                raw_header = request.headers.get("Authorization") or ""
                scheme, separator, bearer = raw_header.partition(" ")
                access_token = None
                if separator and scheme.lower() == "bearer":
                    access_token = await auth_components.static_token_verifier.verify_token(bearer.strip())
                if access_token is None:
                    status_code = 401
                    response = JSONResponse(
                        status_code=401,
                        content={
                            "error_code": "AUTH_FAILED",
                            "message": "Missing or invalid bearer token.",
                            "suggestion": "Send Authorization: Bearer <GATEWAY_ACTION_SECRET>.",
                            "details": {},
                        },
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    response.headers["X-Request-ID"] = request_id
                    response.headers["X-MCP-Surface-Version"] = MCP_SURFACE_VERSION
                    return response
                auth_context_token = auth_context_var.set(AuthenticatedUser(access_token))
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            response.headers["X-MCP-Surface-Version"] = MCP_SURFACE_VERSION
            return response
        finally:
            if auth_context_token is not None:
                auth_context_var.reset(auth_context_token)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            try:
                audit.record_event(
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    metadata={
                        "duration_ms": duration_ms,
                        "mcp": request.url.path == settings.mcp_path
                        or request.url.path.startswith(f"{settings.mcp_path}/"),
                    },
                )
            except Exception:
                pass

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "env": settings.app_env,
                "version": "2.0.0",
                "mcp_surface_version": MCP_SURFACE_VERSION,
                "mcp_endpoint": settings.mcp_path,
                "auth_mode": settings.mcp_auth_mode,
                "active_commands": workspace_operation_manager.active_operation_count(),
            }
        )

    @app.get("/privacy", include_in_schema=False)
    async def privacy() -> HTMLResponse:
        return HTMLResponse(
            """
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Gemini Spark GitHub Gateway 隐私说明</title>
  </head>
  <body>
    <main>
      <h1>隐私说明</h1>
      <p>本服务仅为已授权的 Gemini Spark Connected App 提供 GitHub 仓库维护能力。</p>
      <p>服务会处理调用所需的 GitHub 仓库标识、分支、提交、拉取请求、CI 状态以及最小化审计元数据。</p>
      <p>访问令牌不会写入提示词、仓库文件或审计事件。日志与工作区保留时间由部署者配置。</p>
      <p>部署者负责配置身份提供商、仓库授权范围、数据保留政策和用户支持联系方式。</p>
    </main>
  </body>
</html>
""".strip()
        )

    # Register this mount last so health and privacy routes retain precedence.
    # The MCP sub-application owns the exact /mcp path plus OAuth resource metadata routes.
    app.mount("/", mcp_http_app, name="mcp")
    return app


app = create_app()
