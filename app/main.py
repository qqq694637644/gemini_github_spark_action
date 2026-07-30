from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.api.public_operations import filter_and_mark_public_operations
from app.api.routes import router as gateway_router
from app.auth.dependencies import validate_bearer_token
from app.config.settings import get_settings
from app.errors import ApiError, register_exception_handlers
from app.github.client import GitHubClient
from app.mcp.server import GatewayRuntime, build_transport_security, create_mcp_server
from app.policy.rules import Policy
from app.single_instance import acquire_single_instance, release_single_instance
from app.storage.audit import AuditStore
from app.workspace.manager import WorkspaceManager
from app.workspace.operations import WorkspaceOperationManager

SCHEMA_VERSION = "3"
MINIMUM_PROMPT_VERSION = "3.2"
MCP_SURFACE_VERSION = "1"


def _bearer_from_header(value: str | None) -> str | None:
    if not value:
        return None
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    return token.strip() or None


def create_app() -> FastAPI:
    settings = get_settings()
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
    mcp = create_mcp_server(runtime)
    mcp_http_app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        transport_security=build_transport_security(settings),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
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
            audit.close()
            release_single_instance()

    app = FastAPI(
        title="Gemini Spark GitHub MCP Gateway",
        version="1.0.0",
        description=(
            "Workspace-first GitHub maintenance gateway for Gemini Spark over MCP Streamable HTTP. "
            "The legacy REST/OpenAPI surface remains available for compatibility."
        ),
        servers=[{"url": settings.public_base_url.rstrip("/")}],
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.github = github
    app.state.policy = policy
    app.state.audit = audit
    app.state.workspace_manager = workspace_manager
    app.state.workspace_operation_manager = workspace_operation_manager
    app.state.mcp = mcp

    register_exception_handlers(app)
    app.include_router(gateway_router)

    generated_openapi = app.openapi

    def versioned_openapi() -> dict:
        schema = generated_openapi()
        schema["info"]["x-gateway-schema-version"] = SCHEMA_VERSION
        schema["info"]["x-minimum-prompt-version"] = MINIMUM_PROMPT_VERSION
        filter_and_mark_public_operations(schema)
        return schema

    app.openapi = versioned_openapi  # type: ignore[method-assign]

    @app.middleware("http")
    async def request_audit_and_mcp_auth_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        status_code = 500
        response = None
        try:
            mcp_prefix = settings.mcp_path.rstrip("/") or "/mcp"
            is_mcp_request = request.url.path == mcp_prefix or request.url.path.startswith(f"{mcp_prefix}/")
            if is_mcp_request and request.method != "OPTIONS":
                try:
                    validate_bearer_token(_bearer_from_header(request.headers.get("Authorization")), settings)
                except ApiError as exc:
                    status_code = exc.status_code
                    response = JSONResponse(
                        status_code=exc.status_code,
                        content=exc.as_response().model_dump(),
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    response.headers["X-Request-ID"] = request_id
                    response.headers["X-Gateway-Schema-Version"] = SCHEMA_VERSION
                    return response

            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Gateway-Schema-Version"] = SCHEMA_VERSION
            response.headers["X-MCP-Surface-Version"] = MCP_SURFACE_VERSION
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            try:
                app.state.audit.record_event(
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    metadata={"duration_ms": duration_ms, "mcp": request.url.path.startswith(settings.mcp_path)},
                )
            except Exception:
                pass

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "env": settings.app_env,
                "version": "1.0.0",
                "schema_version": SCHEMA_VERSION,
                "mcp_surface_version": MCP_SURFACE_VERSION,
                "mcp_endpoint": settings.mcp_path,
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
    <title>隐私政策</title>
  </head>
  <body>
    <main>
      <h1>隐私政策</h1>
      <p>这是一个占位页面，用于外部平台填写隐私政策地址。</p>
      <p>当前版本未收集额外个人信息；正式发布前请替换为真实隐私政策内容。</p>
    </main>
  </body>
</html>
""".strip()
        )

    # Keep the MCP protocol path exact. Mounting the sub-application directly
    # at /mcp would make Starlette redirect POST /mcp to /mcp/, which some
    # remote MCP clients do not follow. This catch-all mount is deliberately
    # registered last so FastAPI's REST, health, docs, and privacy routes keep
    # their normal precedence.
    app.mount("/", mcp_http_app, name="mcp")

    return app


app = create_app()
