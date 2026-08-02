# Gemini Spark GitHub MCP Gateway

A workspace-first GitHub maintenance backend for Gemini Spark custom Connected Apps. Personal deployments use static Bearer authentication plus a fine-grained PAT by default; OAuth and GitHub App support remain available for multi-user deployments.

The service exposes repository inspection, controlled edits, pull-request operations, GitHub Actions status/log/artifact operations, and guarded publishing through Model Context Protocol (MCP) Streamable HTTP at `/mcp`.

This repository is MCP-only. The former GPT Actions REST/OpenAPI surface, prompt compatibility file, OpenAI schema extensions, and shared legacy secret name have been removed.

## Capabilities

- 32 MCP-native tools for workspaces, pull requests, CI, workflow operations, logs, and artifacts.
- Separate asynchronous command tools: start, get, logs, cancel, and list.
- MCP Streamable HTTP sessions with exact `/mcp` routing and explicit termination support.
- Personal-first static Bearer authentication, with optional OAuth 2.1 resource-server metadata and discovery challenges.
- JWT/JWKS validation or RFC 7662 token introspection through an external identity provider.
- Per-tool scopes and per-user repository authorization claims.
- Optional OAuth/JWT/introspection mode for multi-user deployments.
- MCP-native structured error data preserving gateway error codes, suggestions, and details.
- Actor-aware audit events without storing access tokens.
- `.spark-artifacts` storage with a safe one-time migration from `.gpt-artifacts`.
- Container, HTTPS reverse-proxy, local protocol, and remote acceptance-validation assets.

## Architecture

```text
Gemini Spark custom Connected App
              |
       Bearer access token
              |
       MCP Streamable HTTP
              |
            /mcp
              |
  scope + repository authorization
              |
      MCP tool adapter layer
              |
 Existing workspace / PR / CI services
              |
   GitHub API + isolated Git workspaces
```

The MCP adapter calls the Python service layer directly; it does not make loopback HTTP requests to its own server.

## Requirements

- Python 3.11 or newer.
- Git.
- PowerShell 7 (`pwsh`) for workspace commands.
- A narrowly scoped fine-grained PAT for personal use, or a GitHub App installation for multi-user use.
- For production: an HTTPS hostname. An OAuth 2.1/OIDC provider is needed only for OAuth mode.

## Install for development

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
```

Use static Bearer mode locally:

```powershell
$env:APP_ENV = 'development'
$env:PUBLIC_BASE_URL = 'http://localhost:8000'
$env:MCP_AUTH_MODE = 'static_bearer'
$env:GATEWAY_ACTION_SECRET = 'replace-with-a-long-random-local-token'
$env:ALLOW_ALL_REPOS = 'false'
$env:ALLOWED_REPOS = 'owner/project-a'
$env:GITHUB_AUTH_MODE = 'pat'
$env:GITHUB_TOKEN = 'set-through-your-secret-store'
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The default workspace virtual environment command is the Python executable currently running the gateway. Override it only when the workspace must use another interpreter:

```env
# Windows without Docker
WORKSPACE_PYTHON_VENV_PYTHON=py -3.11

# Docker/Linux
WORKSPACE_PYTHON_VENV_PYTHON=python3
```

Endpoints:

- MCP: `http://localhost:8000/mcp`
- Health: `http://localhost:8000/healthz`
- Privacy statement: `http://localhost:8000/privacy`

There are no REST repository routes, Swagger UI, or exported OpenAPI action schemas.

## Personal production configuration

`.env.example` is the recommended personal deployment template. It uses:

- `MCP_AUTH_MODE=static_bearer`
- a long random `GATEWAY_ACTION_SECRET`
- `GITHUB_AUTH_MODE=pat`
- `ALLOW_ALL_REPOS=false`
- a `spark/` write-branch prefix
- workflow editing, deletion, and workspace networking disabled by default

Copy it before container deployment:

```powershell
Copy-Item .env.example .env
```

Use a fine-grained PAT that is authorized only for repositories listed in `ALLOWED_REPOS`. Static Bearer grants access to every MCP tool, so HTTPS, a strong random secret, repository allowlisting, and default-branch write protection are all required.

## Advanced OAuth configuration

Start from `deploy/oauth.env.example` and configure values through the deployment platform's secret/configuration system:

```env
APP_ENV=production
PUBLIC_BASE_URL=https://gateway.example.com
MCP_PATH=/mcp
MCP_ALLOWED_HOSTS=gateway.example.com

MCP_AUTH_MODE=oauth
MCP_OAUTH_ISSUER_URL=https://identity.example.com/tenant
MCP_OAUTH_AUDIENCE=https://gateway.example.com/mcp
MCP_OAUTH_REQUIRED_SCOPES=github:read
MCP_OAUTH_REPO_CLAIM=github_repositories
MCP_OAUTH_REQUIRE_REPO_CLAIM=true
```

### JWT access tokens

For signed JWT access tokens, the gateway discovers `jwks_uri` from OAuth Authorization Server Metadata or OpenID Provider Configuration. An explicit endpoint can be supplied:

```env
MCP_OAUTH_JWKS_URL=https://identity.example.com/tenant/keys
MCP_OAUTH_ALGORITHMS=RS256,ES256
```

JWT validation checks signature, allowed algorithm, issuer, audience, expiration, issued-at time, and configured clock skew.

### Opaque access tokens and revocation

For opaque tokens or immediate server-side revocation checks, configure RFC 7662 introspection:

```env
MCP_OAUTH_INTROSPECTION_URL=https://identity.example.com/tenant/introspect
MCP_OAUTH_INTROSPECTION_CLIENT_ID=github-mcp-resource-server
MCP_OAUTH_INTROSPECTION_CLIENT_SECRET=set-through-your-secret-store
```

When introspection is configured, inactive/revoked tokens are rejected. JWT-only mode relies on token expiry and signing-key lifecycle unless the identity provider offers another revocation mechanism.

### Token claims

Access tokens should contain:

- `sub`: user identity.
- `client_id`, `azp`, or `cid`: Connected App client identity.
- `scope` or `scp`: granted MCP tool scopes.
- `aud`: the configured MCP resource URL.
- `github_repositories` by default: an array or comma-separated list of allowed repository patterns, such as `owner/project-a` or `owner/*`.

The repository claim name is configurable with `MCP_OAUTH_REPO_CLAIM`. Set `MCP_OAUTH_REQUIRE_REPO_CLAIM=false` only when repository authorization is enforced by a trusted upstream component.

## MCP scopes

`MCP_OAUTH_REQUIRED_SCOPES` defines baseline scopes required for every OAuth-authenticated tool call; the default is `github:read`. Each tool then adds its operation-specific scope requirements.

| Scope | Operations |
|---|---|
| `github:read` | default baseline; repository inspection, status/diff, PR reads, CI/log/artifact reads, command status/log reads |
| `github:write` | branch preparation, workspace commands/edits, commit/push, PR creation/update/comment, artifact sync; baseline scopes also apply |
| `github:workflow` | workflow dispatch and workflow/job reruns; baseline scopes also apply |
| `github:merge` | pull-request merge; also requires `github:write` and baseline scopes |

The server enforces scopes and repository claims before calling GitHub services. Tool annotations remain hints for clients; backend authorization is authoritative.

## MCP tools

### Workspace

- `prepareWorkspace`
- `workspaceCommandStart`
- `workspaceCommandGet`
- `workspaceCommandLogs`
- `workspaceCommandCancel`
- `workspaceCommandList`
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

GitHub Actions cache deletion is intentionally not exposed.

High-frequency command operations use flat MCP schemas. Other operations keep a typed `request` object so the existing validated Pydantic/service contracts remain authoritative.

### Write-branch safety

- Direct writes to the repository's default branch are always rejected by backend policy.
- New or directly selected writable maintenance branches must start with `WRITE_BRANCH_PREFIX` (`spark/` by default).
- A non-prefixed existing branch is writable only when the workspace is prepared through `source_pr_number` for a same-repository pull request.
- `workspaceCommitAndPush` rechecks this policy using workspace metadata before every push.
- Changes reach the default branch only through the explicit `mergePullRequest` operation.

## Gemini Spark Skill

`SPARK_SKILL.md` is the only agent instruction artifact. The MCP server also exposes it through the `github_repository_maintenance` prompt.

The Skill documents workspace lifecycle, command terminal-state handling, evidence requirements, CI workflow, artifact handling, scope boundaries, and the rule that merging requires an explicit user request.

## Container deployment

Build the image:

```powershell
docker build -t gemini-spark-github-gateway:2.0.0 .
```

The image runs as a non-root user and stores workspaces, operations, and audit data under `/data`.

An HTTPS example is included:

```powershell
Copy-Item .env.example .env
$env:GATEWAY_HOSTNAME = 'gateway.example.com'
docker compose -f deploy/docker-compose.example.yml up --build -d
```

`deploy/Caddyfile.example` provides TLS termination, streaming-friendly reverse proxying, security headers, and JSON access logs. Review retention, backup, host firewall, identity-provider policy, and GitHub App permissions before production use.

## Connect Gemini Spark

1. Deploy the gateway at a trusted HTTPS hostname.
2. For personal mode, configure the static Bearer credential. For OAuth mode, configure the external identity provider and required scopes.
3. Add the HTTPS MCP URL, for example `https://gateway.example.com/mcp`, as a Gemini Spark custom Connected App.
4. Configure the Bearer credential or complete the provider authorization flow.
5. Create a Spark Skill from `SPARK_SKILL.md`.
6. Start with a read-only repository task, then validate write, PR, and CI operations using a non-production repository.

The repository cannot create the external hostname, identity-provider tenant/client, GitHub App installation, or Gemini account configuration on its own; those are environment-owned deployment inputs.

## Validation

Local validation:

```powershell
pytest -q
ruff check app tests scripts
mypy app/auth/mcp.py app/mcp/server.py app/main.py
python scripts/validate_mcp.py
docker build -t gemini-spark-github-gateway:test .
```

Remote protocol acceptance after deployment:

```powershell
$env:MCP_SERVER_URL = 'https://gateway.example.com/mcp'
$env:MCP_ACCESS_TOKEN = 'access-token-from-the-configured-provider'
$env:MCP_TEST_OWNER = 'owner'
$env:MCP_TEST_REPO = 'project-a'
$env:MCP_TEST_REF = 'main'
python scripts/validate_remote_mcp.py
```

Without `MCP_TEST_OWNER` and `MCP_TEST_REPO`, the validator checks health, exact initialization, protocol negotiation, all 32 tools, and session termination. When the repository variables are present, it additionally performs a real read-only chain through `prepareWorkspace`, `workspaceInspect`, `workspaceStatus`, and `queryCiStatus`. This validates Bearer/OAuth → MCP → GitHub credentials → GitHub API/Git clone → workspace without creating branches or modifying the repository.

A manually dispatched GitHub Actions workflow, `.github/workflows/remote-mcp-validation.yml`, runs the same acceptance test using the `MCP_ACCEPTANCE_ACCESS_TOKEN` repository secret.

## Artifact path migration

New artifact syncs use:

```text
.spark-artifacts/runs/<run_id>/
```

When a workspace contains the former `.gpt-artifacts` root and the new root does not exist, the gateway renames it atomically before syncing. Symbolic-link roots are rejected. Both names are added to the workspace's local Git exclude file during the transition, and neither path is exposed through workspace inspection.

## Security notes

- For personal use, prefer a fine-grained PAT limited to the repositories in `ALLOWED_REPOS`; use a GitHub App for multi-user deployments.
- Keep `ALLOW_ALL_REPOS=false` and configure `ALLOWED_REPOS` as a second server-side boundary.
- Keep workflow editing and file deletion disabled unless explicitly required.
- Use short-lived OAuth access tokens and introspection when immediate revocation is required.
- `workspaceCommitAndPush` requires an expected remote head SHA and never force-pushes.
- Direct pushes to the default branch are rejected, even when the GitHub credential itself has that permission.
- `mergePullRequest` is destructive and requires explicit user intent, a fresh PR read, expected head SHA, and `github:merge`.
- Access tokens and GitHub secrets are removed from workspace subprocess environments and are not written to audit records.
- Replace the generic privacy statement with deployment-specific controller, retention, and support details where required.

## Migration completion

The target repository no longer contains the GPT Actions/OpenAPI transport, `x-openai-isConsequential`, `PROMPT.md`, `GPT_ACTION_SECRET`, or the combined `workspaceCommand` MCP tool. Compatibility is limited to the safe on-disk `.gpt-artifacts` rename path so existing workspaces are not discarded.
