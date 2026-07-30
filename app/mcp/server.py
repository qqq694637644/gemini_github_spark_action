from __future__ import annotations

import sysconfig
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, TypeVar
from urllib.parse import urlparse

from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from app.auth.mcp import (
    MERGE_SCOPE,
    READ_SCOPE,
    WORKFLOW_SCOPE,
    WRITE_SCOPE,
    AuthIdentity,
    authorize_repository,
    current_identity,
)
from app.config.settings import Settings, parse_csv
from app.errors import ApiError
from app.github.client import GitHubClient
from app.models.ci import (
    CIStatusQueryRequest,
    CIStatusResponse,
    DispatchWorkflowRequest,
    DispatchWorkflowResponse,
    FailedCILogResponse,
    FailedLogQueryRequest,
    GetCiJobsRequest,
    GetCiJobsResponse,
    GetCiRunRequest,
    GetCiRunResponse,
    GetJobLogRequest,
    GetRunLogRequest,
    JobLogResponse,
    ListArtifactsRequest,
    ListArtifactsResponse,
    RerunWorkflowJobRequest,
    RerunWorkflowJobResponse,
    RerunWorkflowRunRequest,
    RerunWorkflowRunResponse,
    RunLogResponse,
    SyncRunArtifactsToWorkspaceRequest,
    SyncRunArtifactsToWorkspaceResponse,
)
from app.models.pulls import (
    CommentPullRequestRequest,
    CommentPullRequestResponse,
    CreatePullRequestRequest,
    CreatePullRequestResponse,
    GetPullRequestRequest,
    GetPullRequestResponse,
    ListPullRequestsRequest,
    ListPullRequestsResponse,
    MergePullRequestRequest,
    MergePullRequestResponse,
    PullRequestFilesRequest,
    PullRequestFilesResponse,
    UpdatePullRequestRequest,
    UpdatePullRequestResponse,
)
from app.models.workspaces import (
    PrepareWorkspaceRequest,
    PrepareWorkspaceResponse,
    WorkspaceApplyPatchRequest,
    WorkspaceApplyPatchResponse,
    WorkspaceCommandCancelRequest,
    WorkspaceCommandGetRequest,
    WorkspaceCommandListRequest,
    WorkspaceCommandLogsRequest,
    WorkspaceCommandResponse,
    WorkspaceCommandStartRequest,
    WorkspaceCommitAndPushRequest,
    WorkspaceCommitAndPushResponse,
    WorkspaceDiffRequest,
    WorkspaceDiffResponse,
    WorkspaceInspectRequest,
    WorkspaceInspectResponse,
    WorkspaceReadFilesRequest,
    WorkspaceReadFilesResponse,
    WorkspaceSearchRequest,
    WorkspaceSearchResponse,
    WorkspaceStatusRequest,
    WorkspaceStatusResponse,
    WorkspaceWriteFileRequest,
    WorkspaceWriteFileResponse,
)
from app.policy.rules import Policy
from app.services.ci import CIService
from app.services.pulls import PullRequestService
from app.services.workspaces import WorkspaceService
from app.storage.audit import AuditStore
from app.workspace.manager import WorkspaceManager
from app.workspace.operations import WorkspaceOperationManager

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
REMOTE_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
DESTRUCTIVE_REMOTE_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class GatewayRuntime:
    github: GitHubClient
    policy: Policy
    settings: Settings
    workspace_manager: WorkspaceManager
    workspace_operations: WorkspaceOperationManager
    audit: AuditStore

    def workspace_service(self, *, with_operations: bool = False) -> WorkspaceService:
        operations = self.workspace_operations if with_operations else None
        return WorkspaceService(self.github, self.policy, self.settings, self.workspace_manager, self.audit, operations)

    def pull_request_service(self) -> PullRequestService:
        return PullRequestService(self.github, self.policy)

    def ci_service(self, *, with_audit: bool = False) -> CIService:
        return CIService(self.github, self.policy, self.settings, self.audit if with_audit else None)


def build_transport_security(settings: Settings) -> TransportSecuritySettings:
    """Build an explicit Host/Origin allowlist for the remote MCP endpoint."""
    configured_hosts = set(parse_csv(settings.mcp_allowed_hosts))
    parsed = urlparse(settings.public_base_url)
    if parsed.netloc:
        configured_hosts.add(parsed.netloc)
    if parsed.hostname:
        configured_hosts.add(parsed.hostname)
        configured_hosts.add(f"{parsed.hostname}:*")

    configured_hosts.update(
        {
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "[::1]",
            "[::1]:*",
            "testserver",
            "testserver:*",
        }
    )
    return TransportSecuritySettings(
        allowed_hosts=sorted(configured_hosts),
        allowed_origins=parse_csv(settings.mcp_allowed_origins),
    )


async def _invoke_tool(
    runtime: GatewayRuntime,
    *,
    operation: str,
    owner: str,
    repo: str,
    required_scopes: set[str],
    call: Callable[[], Awaitable[T]],
) -> T:
    identity: AuthIdentity | None = None
    try:
        identity = authorize_repository(runtime.settings, owner, repo, required_scopes)
        result = await call()
    except ApiError as exc:
        if identity is None:
            try:
                identity = current_identity(runtime.settings)
            except ApiError:
                identity = None
        metadata = {
            "operation": operation,
            "error_code": exc.error_code,
            "suggestion": exc.suggestion,
            **(identity.audit_metadata() if identity else {}),
        }
        _record_tool_audit(runtime, operation, owner, repo, exc.status_code, metadata)
        raise MCPError(
            -32001,
            exc.message,
            data={
                "error_code": exc.error_code,
                "status_code": exc.status_code,
                "suggestion": exc.suggestion,
                "details": exc.details,
            },
        ) from exc
    except Exception as exc:
        metadata = {
            "operation": operation,
            "exception_type": type(exc).__name__,
            **(identity.audit_metadata() if identity else {}),
        }
        _record_tool_audit(runtime, operation, owner, repo, 500, metadata)
        raise
    _record_tool_audit(
        runtime,
        operation,
        owner,
        repo,
        200,
        {"operation": operation, **identity.audit_metadata()},
    )
    return result


def _record_tool_audit(
    runtime: GatewayRuntime,
    operation: str,
    owner: str,
    repo: str,
    status_code: int,
    metadata: dict,
) -> None:
    try:
        runtime.audit.record_event(
            request_id=None,
            method="MCP",
            path=f"mcp://tools/{operation}",
            status_code=status_code,
            repo=f"{owner}/{repo}",
            metadata=metadata,
        )
    except Exception:
        pass


def create_mcp_server(
    runtime: GatewayRuntime,
    *,
    token_verifier: TokenVerifier | None,
    auth_settings: AuthSettings | None,
) -> MCPServer:
    mcp = MCPServer(
        "Gemini Spark GitHub Gateway",
        title="Gemini Spark GitHub Gateway",
        description="Auditable GitHub repository maintenance tools for Gemini Spark.",
        version="2.0.0",
        token_verifier=token_verifier,
        auth=auth_settings,
    )

    @mcp.prompt(title="GitHub repository maintenance")
    def github_repository_maintenance() -> str:
        """Load the repository-maintenance operating instructions."""
        candidates = (
            Path(__file__).resolve().parents[2] / "SPARK_SKILL.md",
            Path(sysconfig.get_path("data")) / "share" / "gemini-spark-github-gateway" / "SPARK_SKILL.md",
        )
        for skill_path in candidates:
            if skill_path.is_file():
                return skill_path.read_text(encoding="utf-8")
        return "Use the GitHub MCP tools conservatively, verify every remote state, and merge only on explicit user request."

    @mcp.tool(name="prepareWorkspace", title="Prepare Git workspace", annotations=LOCAL_WRITE)
    async def prepare_workspace(owner: str, repo: str, request: PrepareWorkspaceRequest) -> PrepareWorkspaceResponse:
        """Prepare a backend Git workspace. Branch creation requires github:write; read-only preparation requires github:read."""
        scopes = {WRITE_SCOPE} if request.mode == "create_or_prepare_branch" else {READ_SCOPE}
        return await _invoke_tool(
            runtime,
            operation="prepareWorkspace",
            owner=owner,
            repo=repo,
            required_scopes=scopes,
            call=lambda: runtime.workspace_service().prepare(owner, repo, request),
        )

    @mcp.tool(name="workspaceCommandStart", title="Start workspace command", annotations=LOCAL_WRITE)
    async def workspace_command_start(
        owner: str,
        repo: str,
        workspace_id: str,
        idempotency_key: Annotated[str, Field(min_length=8, max_length=200)],
        script: Annotated[str, Field(min_length=1, max_length=20_000)],
        timeout_seconds: Annotated[int | None, Field(ge=1)] = None,
        max_output_bytes: Annotated[int | None, Field(ge=1)] = None,
        allow_network: bool = False,
        plain_output: bool = False,
        utf8_output: bool = True,
    ) -> WorkspaceCommandResponse:
        """Start an asynchronous PowerShell 7 validation or diagnostic command. Requires github:write."""
        request = WorkspaceCommandStartRequest(
            action="start",
            idempotency_key=idempotency_key,
            script=script,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            allow_network=allow_network,
            plain_output=plain_output,
            utf8_output=utf8_output,
        )
        return await _invoke_tool(
            runtime,
            operation="workspaceCommandStart",
            owner=owner,
            repo=repo,
            required_scopes={WRITE_SCOPE},
            call=lambda: runtime.workspace_service(with_operations=True).command(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceCommandGet", title="Get workspace command", annotations=READ_ONLY)
    async def workspace_command_get(
        owner: str,
        repo: str,
        workspace_id: str,
        operation_id: Annotated[str, Field(pattern=r"^op_[0-9a-f]{16}$")],
    ) -> WorkspaceCommandResponse:
        """Read the latest state of an asynchronous workspace command. Requires github:read."""
        request = WorkspaceCommandGetRequest(action="get", operation_id=operation_id)
        return await _invoke_tool(
            runtime,
            operation="workspaceCommandGet",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.workspace_service(with_operations=True).command(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceCommandLogs", title="Read workspace command logs", annotations=READ_ONLY)
    async def workspace_command_logs(
        owner: str,
        repo: str,
        workspace_id: str,
        operation_id: Annotated[str, Field(pattern=r"^op_[0-9a-f]{16}$")],
        stdout_offset: Annotated[int, Field(ge=0)] = 0,
        stderr_offset: Annotated[int, Field(ge=0)] = 0,
        max_bytes: Annotated[int, Field(ge=1, le=500_000)] = 50_000,
    ) -> WorkspaceCommandResponse:
        """Read bounded incremental stdout and stderr for a workspace command. Requires github:read."""
        request = WorkspaceCommandLogsRequest(
            action="logs",
            operation_id=operation_id,
            stdout_offset=stdout_offset,
            stderr_offset=stderr_offset,
            max_bytes=max_bytes,
        )
        return await _invoke_tool(
            runtime,
            operation="workspaceCommandLogs",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.workspace_service(with_operations=True).command(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceCommandCancel", title="Cancel workspace command", annotations=LOCAL_WRITE)
    async def workspace_command_cancel(
        owner: str,
        repo: str,
        workspace_id: str,
        operation_id: Annotated[str, Field(pattern=r"^op_[0-9a-f]{16}$")],
    ) -> WorkspaceCommandResponse:
        """Cancel a running workspace command and its process tree. Requires github:write."""
        request = WorkspaceCommandCancelRequest(action="cancel", operation_id=operation_id)
        return await _invoke_tool(
            runtime,
            operation="workspaceCommandCancel",
            owner=owner,
            repo=repo,
            required_scopes={WRITE_SCOPE},
            call=lambda: runtime.workspace_service(with_operations=True).command(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceCommandList", title="List workspace commands", annotations=READ_ONLY)
    async def workspace_command_list(
        owner: str,
        repo: str,
        workspace_id: str,
        state: Literal["running", "succeeded", "failed", "timed_out", "canceled", "interrupted"] | None = None,
    ) -> WorkspaceCommandResponse:
        """List workspace commands, optionally filtering by terminal or running state. Requires github:read."""
        request = WorkspaceCommandListRequest(action="list", state=state)
        return await _invoke_tool(
            runtime,
            operation="workspaceCommandList",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.workspace_service(with_operations=True).command(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceInspect", title="Inspect workspace", annotations=READ_ONLY)
    async def workspace_inspect(owner: str, repo: str, workspace_id: str, request: WorkspaceInspectRequest) -> WorkspaceInspectResponse:
        """Inspect the workspace tree, search matches, and related UTF-8 file snippets. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="workspaceInspect",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.workspace_service().inspect(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceSearch", title="Search workspace", annotations=READ_ONLY)
    async def workspace_search(owner: str, repo: str, workspace_id: str, request: WorkspaceSearchRequest) -> WorkspaceSearchResponse:
        """Search workspace text with ripgrep without starting a shell command. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="workspaceSearch",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.workspace_service().search(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceReadFiles", title="Read workspace files", annotations=READ_ONLY)
    async def workspace_read_files(
        owner: str,
        repo: str,
        workspace_id: str,
        request: WorkspaceReadFilesRequest,
    ) -> WorkspaceReadFilesResponse:
        """Read known UTF-8 workspace files with line numbers and bounded output. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="workspaceReadFiles",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.workspace_service().read_files(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceStatus", title="Inspect workspace status", annotations=READ_ONLY)
    async def workspace_status(owner: str, repo: str, workspace_id: str, request: WorkspaceStatusRequest) -> WorkspaceStatusResponse:
        """Inspect branch, HEAD, dirty state, conflicts, changed files, and active commands. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="workspaceStatus",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.workspace_service(with_operations=True).status(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceDiff", title="Read workspace diff", annotations=READ_ONLY)
    async def workspace_diff(owner: str, repo: str, workspace_id: str, request: WorkspaceDiffRequest) -> WorkspaceDiffResponse:
        """Read the current auditable workspace diff before publishing. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="workspaceDiff",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.workspace_service().diff(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceApplyPatch", title="Apply text patch", annotations=LOCAL_WRITE)
    async def workspace_apply_patch(
        owner: str,
        repo: str,
        workspace_id: str,
        request: WorkspaceApplyPatchRequest,
    ) -> WorkspaceApplyPatchResponse:
        """Apply a controlled text patch inside a local workspace. Requires github:write."""
        return await _invoke_tool(
            runtime,
            operation="workspaceApplyPatch",
            owner=owner,
            repo=repo,
            required_scopes={WRITE_SCOPE},
            call=lambda: runtime.workspace_service().apply_patch(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceWriteFile", title="Write workspace file", annotations=LOCAL_WRITE)
    async def workspace_write_file(
        owner: str,
        repo: str,
        workspace_id: str,
        request: WorkspaceWriteFileRequest,
    ) -> WorkspaceWriteFileResponse:
        """Create or replace one UTF-8 text file inside a local workspace. Requires github:write."""
        return await _invoke_tool(
            runtime,
            operation="workspaceWriteFile",
            owner=owner,
            repo=repo,
            required_scopes={WRITE_SCOPE},
            call=lambda: runtime.workspace_service().write_file(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="workspaceCommitAndPush", title="Commit and push workspace", annotations=REMOTE_WRITE)
    async def workspace_commit_and_push(
        owner: str,
        repo: str,
        workspace_id: str,
        request: WorkspaceCommitAndPushRequest,
    ) -> WorkspaceCommitAndPushResponse:
        """Commit selected changes and push using an expected remote head SHA. Requires github:write."""
        return await _invoke_tool(
            runtime,
            operation="workspaceCommitAndPush",
            owner=owner,
            repo=repo,
            required_scopes={WRITE_SCOPE},
            call=lambda: runtime.workspace_service().commit_and_push(owner, repo, workspace_id, request),
        )

    @mcp.tool(name="createPullRequest", title="Create pull request", annotations=REMOTE_WRITE)
    async def create_pull_request(owner: str, repo: str, request: CreatePullRequestRequest) -> CreatePullRequestResponse:
        """Create or reuse an open pull request for the same head/base branches. Requires github:write."""
        return await _invoke_tool(
            runtime,
            operation="createPullRequest",
            owner=owner,
            repo=repo,
            required_scopes={WRITE_SCOPE},
            call=lambda: runtime.pull_request_service().create_pull_request(owner, repo, request),
        )

    @mcp.tool(name="getPullRequest", title="Get pull request", annotations=READ_ONLY)
    async def get_pull_request(owner: str, repo: str, request: GetPullRequestRequest) -> GetPullRequestResponse:
        """Read current pull request metadata before review, updates, or merge. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="getPullRequest",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.pull_request_service().get_pull_request(owner, repo, request),
        )

    @mcp.tool(name="listPullRequests", title="List pull requests", annotations=READ_ONLY)
    async def list_pull_requests(owner: str, repo: str, request: ListPullRequestsRequest) -> ListPullRequestsResponse:
        """List pull requests with state and branch filters. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="listPullRequests",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.pull_request_service().list_pull_requests(owner, repo, request),
        )

    @mcp.tool(name="getPullRequestFiles", title="List pull request files", annotations=READ_ONLY)
    async def get_pull_request_files(owner: str, repo: str, request: PullRequestFilesRequest) -> PullRequestFilesResponse:
        """List files changed by a pull request for review and risk assessment. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="getPullRequestFiles",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.pull_request_service().get_pull_request_files(owner, repo, request),
        )

    @mcp.tool(name="updatePullRequest", title="Update pull request", annotations=REMOTE_WRITE)
    async def update_pull_request(owner: str, repo: str, request: UpdatePullRequestRequest) -> UpdatePullRequestResponse:
        """Update pull request title, body, state, or base branch. Requires github:write."""
        return await _invoke_tool(
            runtime,
            operation="updatePullRequest",
            owner=owner,
            repo=repo,
            required_scopes={WRITE_SCOPE},
            call=lambda: runtime.pull_request_service().update_pull_request(owner, repo, request),
        )

    @mcp.tool(name="mergePullRequest", title="Merge pull request", annotations=DESTRUCTIVE_REMOTE_WRITE)
    async def merge_pull_request(owner: str, repo: str, request: MergePullRequestRequest) -> MergePullRequestResponse:
        """Merge only after explicit user request and a fresh expected-head-SHA check. Requires github:write and github:merge."""
        return await _invoke_tool(
            runtime,
            operation="mergePullRequest",
            owner=owner,
            repo=repo,
            required_scopes={WRITE_SCOPE, MERGE_SCOPE},
            call=lambda: runtime.pull_request_service().merge_pull_request(owner, repo, request),
        )

    @mcp.tool(name="commentPullRequest", title="Comment on pull request", annotations=REMOTE_WRITE)
    async def comment_pull_request(owner: str, repo: str, request: CommentPullRequestRequest) -> CommentPullRequestResponse:
        """Post a review or status comment on a pull request. Requires github:write."""
        return await _invoke_tool(
            runtime,
            operation="commentPullRequest",
            owner=owner,
            repo=repo,
            required_scopes={WRITE_SCOPE},
            call=lambda: runtime.pull_request_service().comment_pull_request(owner, repo, request),
        )

    @mcp.tool(name="queryCiStatus", title="Query CI status", annotations=READ_ONLY)
    async def query_ci_status(owner: str, repo: str, request: CIStatusQueryRequest) -> CIStatusResponse:
        """Query workflow-run status by commit, branch, pull request, workflow, event, or creation time. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="queryCiStatus",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.ci_service().get_ci_status(owner, repo, request),
        )

    @mcp.tool(name="dispatchWorkflow", title="Dispatch workflow", annotations=REMOTE_WRITE)
    async def dispatch_workflow(owner: str, repo: str, request: DispatchWorkflowRequest) -> DispatchWorkflowResponse:
        """Trigger workflow_dispatch and return a query hint for the resulting run. Requires github:workflow."""
        return await _invoke_tool(
            runtime,
            operation="dispatchWorkflow",
            owner=owner,
            repo=repo,
            required_scopes={WORKFLOW_SCOPE},
            call=lambda: runtime.ci_service(with_audit=True).dispatch_workflow(owner, repo, request),
        )

    @mcp.tool(name="queryFailedCiLog", title="Read failed CI summary", annotations=READ_ONLY)
    async def query_failed_ci_log(owner: str, repo: str, request: FailedLogQueryRequest) -> FailedCILogResponse:
        """Read a bounded summary of failed CI logs. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="queryFailedCiLog",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.ci_service().get_failed_ci_log(owner, repo, request),
        )

    @mcp.tool(name="getCiRun", title="Get CI run", annotations=READ_ONLY)
    async def get_ci_run(owner: str, repo: str, request: GetCiRunRequest) -> GetCiRunResponse:
        """Read one workflow run and optionally include jobs. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="getCiRun",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.ci_service().get_ci_run(owner, repo, request),
        )

    @mcp.tool(name="rerunWorkflowRun", title="Rerun workflow run", annotations=REMOTE_WRITE)
    async def rerun_workflow_run(owner: str, repo: str, request: RerunWorkflowRunRequest) -> RerunWorkflowRunResponse:
        """Rerun an entire workflow only for evidence-backed transient failures. Requires github:workflow."""
        return await _invoke_tool(
            runtime,
            operation="rerunWorkflowRun",
            owner=owner,
            repo=repo,
            required_scopes={WORKFLOW_SCOPE},
            call=lambda: runtime.ci_service(with_audit=True).rerun_workflow_run(owner, repo, request),
        )

    @mcp.tool(name="getCiJobs", title="List CI jobs", annotations=READ_ONLY)
    async def get_ci_jobs(owner: str, repo: str, request: GetCiJobsRequest) -> GetCiJobsResponse:
        """List jobs for a workflow run. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="getCiJobs",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.ci_service().get_ci_jobs(owner, repo, request),
        )

    @mcp.tool(name="rerunWorkflowJob", title="Rerun workflow job", annotations=REMOTE_WRITE)
    async def rerun_workflow_job(owner: str, repo: str, request: RerunWorkflowJobRequest) -> RerunWorkflowJobResponse:
        """Rerun one workflow job in preference to rerunning an entire workflow. Requires github:workflow."""
        return await _invoke_tool(
            runtime,
            operation="rerunWorkflowJob",
            owner=owner,
            repo=repo,
            required_scopes={WORKFLOW_SCOPE},
            call=lambda: runtime.ci_service(with_audit=True).rerun_workflow_job(owner, repo, request),
        )

    @mcp.tool(name="getJobLog", title="Read job log", annotations=READ_ONLY)
    async def get_job_log(owner: str, repo: str, request: GetJobLogRequest) -> JobLogResponse:
        """Read a bounded workflow job log, optionally limited to one step. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="getJobLog",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.ci_service().get_job_log(owner, repo, request),
        )

    @mcp.tool(name="getRunLog", title="Read run log archive", annotations=READ_ONLY)
    async def get_run_log(owner: str, repo: str, request: GetRunLogRequest) -> RunLogResponse:
        """Read selected text files from a workflow run log archive. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="getRunLog",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.ci_service().get_run_log(owner, repo, request),
        )

    @mcp.tool(name="listArtifacts", title="List run artifacts", annotations=READ_ONLY)
    async def list_artifacts(owner: str, repo: str, request: ListArtifactsRequest) -> ListArtifactsResponse:
        """List downloadable artifacts attached to a workflow run. Requires github:read."""
        return await _invoke_tool(
            runtime,
            operation="listArtifacts",
            owner=owner,
            repo=repo,
            required_scopes={READ_SCOPE},
            call=lambda: runtime.ci_service().list_artifacts(owner, repo, request),
        )

    @mcp.tool(name="syncRunArtifactsToWorkspace", title="Sync artifacts to workspace", annotations=LOCAL_WRITE)
    async def sync_run_artifacts_to_workspace(
        owner: str,
        repo: str,
        workspace_id: str,
        request: SyncRunArtifactsToWorkspaceRequest,
    ) -> SyncRunArtifactsToWorkspaceResponse:
        """Safely sync run artifacts into .spark-artifacts/runs/<run_id>. Requires github:write."""
        return await _invoke_tool(
            runtime,
            operation="syncRunArtifactsToWorkspace",
            owner=owner,
            repo=repo,
            required_scopes={WRITE_SCOPE},
            call=lambda: runtime.workspace_service().sync_run_artifacts_to_workspace(owner, repo, workspace_id, request),
        )

    return mcp
