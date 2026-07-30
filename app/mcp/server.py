from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from app.config.settings import Settings, parse_csv
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
    WorkspaceCommandRequest,
    WorkspaceCommandResponse,
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


def create_mcp_server(runtime: GatewayRuntime) -> MCPServer:
    mcp = MCPServer("Gemini Spark GitHub Gateway")

    @mcp.prompt(title="GitHub repository maintenance")
    def github_repository_maintenance() -> str:
        """Load the repository-maintenance operating instructions."""
        skill_path = Path(__file__).resolve().parents[2] / "SPARK_SKILL.md"
        if skill_path.is_file():
            return skill_path.read_text(encoding="utf-8")
        return "Use the GitHub MCP tools conservatively, verify every remote state, and merge only on explicit user request."

    @mcp.tool(name="prepareWorkspace", title="Prepare Git workspace", annotations=LOCAL_WRITE)
    async def prepare_workspace(owner: str, repo: str, request: PrepareWorkspaceRequest) -> PrepareWorkspaceResponse:
        """Prepare a backend Git workspace. The server generates the workspace ID; reuse the returned ID for later tools."""
        return await runtime.workspace_service().prepare(owner, repo, request)

    @mcp.tool(name="workspaceCommand", title="Manage workspace command", annotations=LOCAL_WRITE)
    async def workspace_command(owner: str, repo: str, workspace_id: str, request: WorkspaceCommandRequest) -> WorkspaceCommandResponse:
        """Start, inspect, read logs from, cancel, or list asynchronous PowerShell workspace commands."""
        return await runtime.workspace_service(with_operations=True).command(owner, repo, workspace_id, request)

    @mcp.tool(name="workspaceInspect", title="Inspect workspace", annotations=READ_ONLY)
    async def workspace_inspect(owner: str, repo: str, workspace_id: str, request: WorkspaceInspectRequest) -> WorkspaceInspectResponse:
        """Inspect the workspace tree, search matches, and related UTF-8 file snippets before making changes."""
        return await runtime.workspace_service().inspect(owner, repo, workspace_id, request)

    @mcp.tool(name="workspaceSearch", title="Search workspace", annotations=READ_ONLY)
    async def workspace_search(owner: str, repo: str, workspace_id: str, request: WorkspaceSearchRequest) -> WorkspaceSearchResponse:
        """Search workspace text with ripgrep without starting a shell command."""
        return await runtime.workspace_service().search(owner, repo, workspace_id, request)

    @mcp.tool(name="workspaceReadFiles", title="Read workspace files", annotations=READ_ONLY)
    async def workspace_read_files(owner: str, repo: str, workspace_id: str, request: WorkspaceReadFilesRequest) -> WorkspaceReadFilesResponse:
        """Read known UTF-8 workspace files with line numbers and bounded output."""
        return await runtime.workspace_service().read_files(owner, repo, workspace_id, request)

    @mcp.tool(name="workspaceStatus", title="Inspect workspace status", annotations=READ_ONLY)
    async def workspace_status(owner: str, repo: str, workspace_id: str, request: WorkspaceStatusRequest) -> WorkspaceStatusResponse:
        """Inspect branch, HEAD, dirty state, conflicts, changed files, and active workspace commands."""
        return await runtime.workspace_service(with_operations=True).status(owner, repo, workspace_id, request)

    @mcp.tool(name="workspaceDiff", title="Read workspace diff", annotations=READ_ONLY)
    async def workspace_diff(owner: str, repo: str, workspace_id: str, request: WorkspaceDiffRequest) -> WorkspaceDiffResponse:
        """Read the current auditable workspace diff. Always use this before committing and pushing."""
        return await runtime.workspace_service().diff(owner, repo, workspace_id, request)

    @mcp.tool(name="workspaceApplyPatch", title="Apply text patch", annotations=LOCAL_WRITE)
    async def workspace_apply_patch(owner: str, repo: str, workspace_id: str, request: WorkspaceApplyPatchRequest) -> WorkspaceApplyPatchResponse:
        """Apply a controlled text patch inside a workspace. This changes only the local workspace, not GitHub."""
        return await runtime.workspace_service().apply_patch(owner, repo, workspace_id, request)

    @mcp.tool(name="workspaceWriteFile", title="Write workspace file", annotations=LOCAL_WRITE)
    async def workspace_write_file(owner: str, repo: str, workspace_id: str, request: WorkspaceWriteFileRequest) -> WorkspaceWriteFileResponse:
        """Create or replace one UTF-8 text file inside a workspace. This does not publish the file remotely."""
        return await runtime.workspace_service().write_file(owner, repo, workspace_id, request)

    @mcp.tool(name="workspaceCommitAndPush", title="Commit and push workspace", annotations=REMOTE_WRITE)
    async def workspace_commit_and_push(
        owner: str, repo: str, workspace_id: str, request: WorkspaceCommitAndPushRequest
    ) -> WorkspaceCommitAndPushResponse:
        """Commit selected changes and push to a remote branch after diff review and validation. Requires the expected remote head SHA."""
        return await runtime.workspace_service().commit_and_push(owner, repo, workspace_id, request)

    @mcp.tool(name="createPullRequest", title="Create pull request", annotations=REMOTE_WRITE)
    async def create_pull_request(owner: str, repo: str, request: CreatePullRequestRequest) -> CreatePullRequestResponse:
        """Create a pull request or reuse an existing open pull request for the same head and base branches."""
        return await runtime.pull_request_service().create_pull_request(owner, repo, request)

    @mcp.tool(name="getPullRequest", title="Get pull request", annotations=READ_ONLY)
    async def get_pull_request(owner: str, repo: str, request: GetPullRequestRequest) -> GetPullRequestResponse:
        """Read current pull request metadata before review, updates, or merge."""
        return await runtime.pull_request_service().get_pull_request(owner, repo, request)

    @mcp.tool(name="listPullRequests", title="List pull requests", annotations=READ_ONLY)
    async def list_pull_requests(owner: str, repo: str, request: ListPullRequestsRequest) -> ListPullRequestsResponse:
        """List pull requests with optional state, head-branch, and base-branch filters."""
        return await runtime.pull_request_service().list_pull_requests(owner, repo, request)

    @mcp.tool(name="getPullRequestFiles", title="List pull request files", annotations=READ_ONLY)
    async def get_pull_request_files(owner: str, repo: str, request: PullRequestFilesRequest) -> PullRequestFilesResponse:
        """List files changed by a pull request for review and risk assessment."""
        return await runtime.pull_request_service().get_pull_request_files(owner, repo, request)

    @mcp.tool(name="updatePullRequest", title="Update pull request", annotations=REMOTE_WRITE)
    async def update_pull_request(owner: str, repo: str, request: UpdatePullRequestRequest) -> UpdatePullRequestResponse:
        """Update pull request title, body, state, or base branch. Closing a pull request does not delete its branch."""
        return await runtime.pull_request_service().update_pull_request(owner, repo, request)

    @mcp.tool(name="mergePullRequest", title="Merge pull request", annotations=DESTRUCTIVE_REMOTE_WRITE)
    async def merge_pull_request(owner: str, repo: str, request: MergePullRequestRequest) -> MergePullRequestResponse:
        """Merge an open, non-draft pull request only after explicit user request and a fresh expected-head-SHA check."""
        return await runtime.pull_request_service().merge_pull_request(owner, repo, request)

    @mcp.tool(name="commentPullRequest", title="Comment on pull request", annotations=REMOTE_WRITE)
    async def comment_pull_request(owner: str, repo: str, request: CommentPullRequestRequest) -> CommentPullRequestResponse:
        """Post a review or status comment on a pull request."""
        return await runtime.pull_request_service().comment_pull_request(owner, repo, request)

    @mcp.tool(name="queryCiStatus", title="Query CI status", annotations=READ_ONLY)
    async def query_ci_status(owner: str, repo: str, request: CIStatusQueryRequest) -> CIStatusResponse:
        """Query GitHub Actions workflow-run status by commit, branch, pull request, workflow, event, or creation time."""
        return await runtime.ci_service().get_ci_status(owner, repo, request)

    @mcp.tool(name="dispatchWorkflow", title="Dispatch workflow", annotations=REMOTE_WRITE)
    async def dispatch_workflow(owner: str, repo: str, request: DispatchWorkflowRequest) -> DispatchWorkflowResponse:
        """Trigger a workflow_dispatch workflow. Use the returned query hint to locate the resulting run."""
        return await runtime.ci_service(with_audit=True).dispatch_workflow(owner, repo, request)

    @mcp.tool(name="queryFailedCiLog", title="Read failed CI summary", annotations=READ_ONLY)
    async def query_failed_ci_log(owner: str, repo: str, request: FailedLogQueryRequest) -> FailedCILogResponse:
        """Read a bounded summary of failed CI logs before requesting deeper job or run logs."""
        return await runtime.ci_service().get_failed_ci_log(owner, repo, request)

    @mcp.tool(name="getCiRun", title="Get CI run", annotations=READ_ONLY)
    async def get_ci_run(owner: str, repo: str, request: GetCiRunRequest) -> GetCiRunResponse:
        """Read one workflow run and optionally include its jobs."""
        return await runtime.ci_service().get_ci_run(owner, repo, request)

    @mcp.tool(name="rerunWorkflowRun", title="Rerun workflow run", annotations=REMOTE_WRITE)
    async def rerun_workflow_run(owner: str, repo: str, request: RerunWorkflowRunRequest) -> RerunWorkflowRunResponse:
        """Rerun an entire workflow run only when evidence indicates a transient runner, network, or platform failure."""
        return await runtime.ci_service(with_audit=True).rerun_workflow_run(owner, repo, request)

    @mcp.tool(name="getCiJobs", title="List CI jobs", annotations=READ_ONLY)
    async def get_ci_jobs(owner: str, repo: str, request: GetCiJobsRequest) -> GetCiJobsResponse:
        """List jobs for a workflow run when job-level status is needed."""
        return await runtime.ci_service().get_ci_jobs(owner, repo, request)

    @mcp.tool(name="rerunWorkflowJob", title="Rerun workflow job", annotations=REMOTE_WRITE)
    async def rerun_workflow_job(owner: str, repo: str, request: RerunWorkflowJobRequest) -> RerunWorkflowJobResponse:
        """Rerun one workflow job. Prefer this over rerunning the entire workflow when only one job was transiently affected."""
        return await runtime.ci_service(with_audit=True).rerun_workflow_job(owner, repo, request)

    @mcp.tool(name="getJobLog", title="Read job log", annotations=READ_ONLY)
    async def get_job_log(owner: str, repo: str, request: GetJobLogRequest) -> JobLogResponse:
        """Read a bounded workflow job log, optionally limited to a named step."""
        return await runtime.ci_service().get_job_log(owner, repo, request)

    @mcp.tool(name="getRunLog", title="Read run log archive", annotations=READ_ONLY)
    async def get_run_log(owner: str, repo: str, request: GetRunLogRequest) -> RunLogResponse:
        """Read selected text files from a workflow run log archive."""
        return await runtime.ci_service().get_run_log(owner, repo, request)

    @mcp.tool(name="listArtifacts", title="List run artifacts", annotations=READ_ONLY)
    async def list_artifacts(owner: str, repo: str, request: ListArtifactsRequest) -> ListArtifactsResponse:
        """List downloadable artifacts attached to a workflow run."""
        return await runtime.ci_service().list_artifacts(owner, repo, request)

    @mcp.tool(name="syncRunArtifactsToWorkspace", title="Sync artifacts to workspace", annotations=LOCAL_WRITE)
    async def sync_run_artifacts_to_workspace(
        owner: str, repo: str, workspace_id: str, request: SyncRunArtifactsToWorkspaceRequest
    ) -> SyncRunArtifactsToWorkspaceResponse:
        """Download and safely extract workflow-run artifacts into .gpt-artifacts/runs/<run_id>/ inside a prepared workspace."""
        return await runtime.workspace_service().sync_run_artifacts_to_workspace(owner, repo, workspace_id, request)

    return mcp
