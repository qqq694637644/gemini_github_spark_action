# Gemini Spark Migration Completion Record

## Stage 1: MCP compatibility baseline — complete

- Migrated the workspace-first GitHub maintenance implementation and tests.
- Added the official MCP Python SDK and Streamable HTTP endpoint at `/mcp`.
- Exposed repository, pull-request, CI, workflow, log, and artifact services as MCP tools.
- Added MCP read/write/destructive annotations, a Spark Skill, and an MCP prompt.

## Stage 2: Deployment and Gemini Spark acceptance assets — implementation complete

- Added a non-root production Docker image with Git and PowerShell 7.
- Added Docker Compose and Caddy HTTPS reverse-proxy examples.
- Added a remote MCP acceptance validator for health, initialization, protocol negotiation, tool discovery, session termination, and optional real read-only GitHub/workspace tool calls.
- Added a manually dispatched remote-validation workflow using a repository secret.
- Added a Windows-native, no-Docker personal OAuth authorization server with one-command setup, fixed-client credentials, PKCE, JWT/JWKS, refresh-token rotation, revocation, and a fine-grained PAT backend.
- Kept external OAuth/GitHub App configuration under `deploy/` for multi-user deployments.
- Documented the exact Gemini Spark Connected App and Skill setup sequence.

Environment-owned execution remains necessary after review: provision the HTTPS hostname, GitHub credential, acceptance token, and Gemini Spark Connected App. An identity-provider application and GitHub App installation are needed only when the advanced multi-user mode is selected. These external resources cannot be created by repository code.

## Stage 3: MCP-native ergonomics — complete

- Replaced the combined `workspaceCommand` envelope with five MCP-native tools.
- Flattened high-frequency command parameters.
- Added structured MCP error mapping with gateway error code, HTTP-equivalent status, suggestion, and details.
- Added protocol-level tests for exact `/mcp`, session creation/termination, prompt and tool discovery, and conditional CORS.
- Declared the authoritative 32-tool surface in `app/mcp/tool_names.py`.

## Stage 4: Production authorization — complete

- Added OAuth resource-server metadata and discovery challenges through the MCP SDK.
- Added a built-in single-user OAuth Authorization Code server for Gemini custom Connected Apps on Windows.
- Added JWT/JWKS validation with issuer, audience, algorithm, lifetime, and clock-skew checks.
- Added RFC 7662 introspection for opaque tokens and immediate revocation enforcement.
- Added configurable baseline OAuth scopes plus per-tool scopes: `github:read`, `github:write`, `github:workflow`, and `github:merge`.
- Added configurable per-user repository authorization claims.
- Added authenticated actor/client/scopes to MCP audit metadata without storing access tokens.
- Retained static Bearer mode only as an explicit local diagnostic/non-Gemini fallback.

## Stage 5: Compatibility retirement — complete

- Removed REST repository routes, OpenAPI export, Swagger/OpenAPI endpoints, action operation allowlists, and OpenAI schema extensions.
- Removed `PROMPT.md`, `GPT_ACTION_SECRET`, and the legacy combined command request model.
- Renamed the validation workflow to MCP terminology.
- Changed artifact storage to `.spark-artifacts` with a safe migration from existing `.gpt-artifacts` workspace data.
- Updated package, documentation, configuration, tests, and CI to version 2.0.0.

## Completion criteria

Repository migration is complete when the migration PR has:

- full unit and local Git integration tests passing;
- lint passing;
- MCP/authentication type checks passing;
- MCP surface validation passing;
- production container build passing;
- GitHub Actions completed successfully.

Deployment acceptance is complete for a specific environment only after `scripts/validate_remote_mcp.py` succeeds against its HTTPS endpoint and a Gemini Spark user confirms the Connected App authorization flow. The repository provides those checks but intentionally contains no deployment credentials or provider-specific secrets.
