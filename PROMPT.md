# Legacy REST Prompt Compatibility

Prompt version: 3.2

This file is retained only for clients that still consume the legacy REST/OpenAPI surface.

Gemini Spark deployments should use [`SPARK_SKILL.md`](SPARK_SKILL.md). The MCP server also exposes the same instructions through the `github_repository_maintenance` MCP prompt.

The authoritative operating rules are:

- Prepare a backend workspace before repository reads or edits.
- Inspect before editing and review `workspaceDiff` before publishing.
- Follow asynchronous workspace commands to a terminal state.
- Verify commits, pull requests, CI, logs, artifacts, and merges through tool results.
- Merge or close pull requests only after an explicit user request.
- Never request, expose, or record credentials or secret values.
