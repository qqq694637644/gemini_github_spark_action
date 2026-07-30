# Gemini Spark GitHub MCP Gateway

A workspace-first GitHub maintenance backend for Gemini Spark custom Connected Apps.

The service exposes GitHub repository, pull request, and GitHub Actions operations through Model Context Protocol (MCP) Streamable HTTP at `/mcp`. The original REST/OpenAPI endpoints are retained as a compatibility surface while the migration is completed.

## Migration status

Implemented in the first migration stage:

- MCP Streamable HTTP endpoint mounted into the existing FastAPI service.
- The same 28 public maintenance operations exposed as MCP tools.
- Tool annotations for read-only, local-write, remote-write, and destructive merge operations.
- A `github_repository_maintenance` MCP prompt backed by `SPARK_SKILL.md`.
- Static Bearer authentication suitable for Gemini Spark's manual credential configuration.
- Legacy `GPT_ACTION_SECRET` and REST/OpenAPI compatibility.
- MCP tool-surface validation in CI.

Not implemented yet:

- OAuth 2.1 authorization-server discovery and Dynamic Client Registration.
- Per-user scopes or identities.
- Splitting the multi-action `workspaceCommand` tool into separate MCP tools.
- Removing the legacy OpenAPI/GPT Actions compatibility layer.

## Architecture

```text
Gemini Spark custom Connected App
              |
       MCP Streamable HTTP
              |
            /mcp
              |
     MCP tool adapter layer
              |
 Existing workspace / PR / CI services
              |
   GitHub REST API + local Git workspaces
```

The MCP adapter calls the existing Python service layer directly. It does not make loopback HTTP calls to the service's own REST API.

## Requirements

- Python 3.11 or newer.
- PowerShell 7 available as `pwsh` for workspace commands.
- Git available to the service process.
- A GitHub personal access token or GitHub App installation credentials.
- An HTTPS deployment reachable by Gemini Spark.

## Install

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
Copy-Item .env.example .env
```

Edit `.env` before starting the service.

## Minimum configuration

```env
APP_ENV=production
PUBLIC_BASE_URL=https://gateway.example.com
GATEWAY_ACTION_SECRET=replace-with-a-long-random-secret
MCP_PATH=/mcp
MCP_ALLOWED_HOSTS=gateway.example.com

GITHUB_AUTH_MODE=pat
GITHUB_TOKEN=replace-with-your-github-personal-access-token

ALLOW_ALL_REPOS=false
ALLOWED_REPOS=owner/project-a
WRITE_BRANCH_PREFIX=spark/
```

`GATEWAY_ACTION_SECRET` may contain a comma-separated set of accepted values to support safe token rotation. `GPT_ACTION_SECRET` remains accepted as a deprecated migration alias.

For production, use a narrowly scoped GitHub token or GitHub App, restrict `ALLOWED_REPOS`, keep workflow editing disabled unless required, and place the service behind HTTPS.

## Run

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Local endpoints:

- MCP: `http://localhost:8000/mcp`
- Health: `http://localhost:8000/healthz`
- Legacy OpenAPI: `http://localhost:8000/openapi.json`
- Legacy Swagger UI: `http://localhost:8000/docs`

All MCP requests require:

```http
Authorization: Bearer <GATEWAY_ACTION_SECRET>
```

## Connect Gemini Spark

1. Deploy this service at a trusted HTTPS URL.
2. In Gemini Spark custom Connected Apps, add the MCP server URL, for example `https://gateway.example.com/mcp`.
3. Configure the custom app to send the Bearer value from `GATEWAY_ACTION_SECRET` using the advanced/manual credential option.
4. Create a Spark Skill using the contents of `SPARK_SKILL.md`.
5. Start with a read-only task and verify that `prepareWorkspace`, `workspaceInspect`, and `workspaceReadFiles` are available before enabling write workflows.

Static Bearer authentication is the migration MVP. It authenticates the connected app but does not identify individual users. A multi-user public deployment should add OAuth 2.1 and per-user authorization before production use.

## MCP tools

### Workspace

- `prepareWorkspace`
- `workspaceCommand`
- `workspaceInspect`
- `workspaceSearch`
- `workspaceReadFiles`
- `workspaceStatus`
- `workspaceDiff`
- `workspaceApplyPatch`
- `workspaceWriteFile`
- `workspaceCommitAndPush`
- `syncRunArtifactsToWorkspace`

### Pull requests

- `createPullRequest`
- `getPullRequest`
- `listPullRequests`
- `getPullRequestFiles`
- `updatePullRequest`
- `mergePullRequest`
- `commentPullRequest`

### CI and workflow

- `queryCiStatus`
- `dispatchWorkflow`
- `queryFailedCiLog`
- `getCiRun`
- `rerunWorkflowRun`
- `getCiJobs`
- `rerunWorkflowJob`
- `getJobLog`
- `getRunLog`
- `listArtifacts`

The two GitHub Actions cache operations remain intentionally excluded from both public surfaces.

### MCP input shape

Each tool has explicit `owner` and `repo` parameters. Operations with a REST request model expose that model under `request`.

Example conceptual call:

```json
{
  "owner": "octocat",
  "repo": "example",
  "request": {
    "base_ref": "main",
    "branch": "spark/fix-readme",
    "idempotency_key": "prepare-fix-readme-001"
  }
}
```

Workspace-specific tools also require the server-returned `workspace_id` as a top-level parameter.

## Skill and prompt migration

`SPARK_SKILL.md` is the authoritative Gemini Spark instruction set. It keeps workflow, safety, validation, merge, and evidence rules at the model layer.

Detailed parameter constraints live in Pydantic request models and MCP tool schemas. Remote-write and destructive semantics are also declared through MCP tool annotations, but annotations are only hints; backend policy remains the enforcement layer.

`PROMPT.md` is retained only to satisfy legacy REST/OpenAPI clients and their existing prompt-version check.

## Validate

```powershell
pytest -q
python scripts/validate_mcp.py
$env:PUBLIC_BASE_URL = 'https://gateway.example.com'
python scripts/export_openapi.py
```

For interactive protocol testing, install the MCP CLI extra already included by the project dependency and run an MCP Inspector against a local authenticated endpoint.

## Security notes

- The service has access to real Git repositories and GitHub credentials.
- Use `ALLOWED_REPOS`; do not expose unrestricted repository access by default.
- Keep `ALLOW_WORKFLOW_EDIT=false` and `ALLOW_DELETE_FILES=false` unless explicitly needed.
- `mergePullRequest` is marked destructive and should only be called after explicit user instruction plus a fresh head-SHA check.
- `workspaceCommitAndPush` requires `expected_head_sha` to prevent accidental overwrite of a changed remote branch.
- Never put secrets in prompts, tool arguments, repository files, logs, or audit metadata.
- Replace the placeholder privacy page before a public deployment.

## Legacy REST compatibility

The previous `/repos/{owner}/{repo}/...` routes, OpenAPI export script, 28-operation allowlist, and `x-openai-isConsequential` extension remain available. They are not used by Gemini Spark's MCP connection and can be removed in a later breaking release after migration consumers are confirmed.
