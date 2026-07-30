# Gemini Spark Migration Plan

## Stage 1: MCP compatibility baseline

Status: implemented on the migration branch.

- Copy the workspace-first GitHub gateway implementation and tests.
- Add the official MCP Python SDK.
- Mount a Streamable HTTP endpoint at `/mcp`.
- Expose the existing 28 public operations as MCP tools.
- Add MCP read/write/destructive annotations.
- Add `SPARK_SKILL.md` and an MCP prompt.
- Add static Bearer authentication with legacy secret compatibility.
- Keep REST/OpenAPI available during migration.
- Add tool-surface tests and CI validation.

## Stage 2: Gemini Spark integration validation

- Deploy behind HTTPS using a dedicated hostname.
- Connect a Gemini Spark custom app with the MCP URL.
- Configure manual Bearer credentials.
- Verify initialization, tool listing, read-only workspace flow, local edit flow, commit/push, PR creation, and CI query.
- Confirm how Gemini Spark presents nested Pydantic `request` parameters.
- Record any tool-selection or schema issues before changing the service contract.

## Stage 3: MCP-native ergonomics

- Split `workspaceCommand` into start/get/logs/cancel/list MCP tools while retaining the REST envelope.
- Consider flattening high-frequency MCP tool parameters if Spark struggles with nested request objects.
- Add MCP-specific error mapping that preserves gateway error codes and suggestions.
- Add protocol-level tests over Streamable HTTP, including session creation and termination.
- Add explicit CORS only when a browser MCP client is required.

## Stage 4: Production authorization

- Replace shared static Bearer authentication with OAuth 2.1 resource-server support.
- Publish protected-resource metadata and authorization-server discovery.
- Integrate an external identity provider.
- Add per-user identity, scopes, repository authorization, revocation, and audit attribution.
- Validate Gemini Spark credential discovery and consent behavior.

## Stage 5: Compatibility retirement

- Confirm no remaining GPT Actions/OpenAPI consumers.
- Remove `x-openai-isConsequential`, OpenAPI export constraints, and prompt-version compatibility metadata.
- Remove `GPT_ACTION_SECRET` after a documented deprecation window.
- Rename remaining `.gpt-artifacts` paths only through a backward-compatible migration.
- Rename the validation workflow file after the content migration has stabilized.
