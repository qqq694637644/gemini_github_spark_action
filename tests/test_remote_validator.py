from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts import validate_remote_mcp


def test_read_only_acceptance_calls_real_gateway_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_call_tool(*args, name: str, arguments: dict[str, Any], **kwargs) -> dict[str, Any]:
        calls.append((name, arguments))
        if name == "prepareWorkspace":
            return {"workspace_id": "ws_readonly", "branch": "main"}
        if name == "workspaceInspect":
            return {"workspace_id": "ws_readonly", "tree": [{"path": "README.md", "type": "file"}]}
        if name == "workspaceStatus":
            return {"workspace_id": "ws_readonly", "dirty": False}
        if name == "queryCiStatus":
            return {"status": "completed", "workflow_runs": []}
        raise AssertionError(name)

    monkeypatch.setattr(validate_remote_mcp, "call_tool", fake_call_tool)

    next_id = validate_remote_mcp.run_read_only_github_acceptance(
        object(),  # type: ignore[arg-type]
        "https://gateway.example.com/mcp",
        {"Authorization": "Bearer redacted"},
        owner="acme",
        repo="demo",
        ref="main",
        request_id=10,
    )

    assert next_id == 14
    assert [name for name, _ in calls] == [
        "prepareWorkspace",
        "workspaceInspect",
        "workspaceStatus",
        "queryCiStatus",
    ]
    prepare = calls[0][1]
    assert prepare["request"]["mode"] == "prepare_ref"
    assert prepare["request"]["base_ref"] == "main"
    assert "branch" not in prepare["request"]


def test_read_only_acceptance_environment_requires_owner_and_repo_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TEST_OWNER", "acme")
    monkeypatch.delenv("MCP_TEST_REPO", raising=False)

    with pytest.raises(SystemExit, match="must be set together"):
        validate_remote_mcp.read_only_acceptance_config()


def test_personal_template_uses_builtin_oauth_and_external_oauth_is_advanced() -> None:
    root = Path(__file__).parents[1]
    personal = (root / ".env.example").read_text(encoding="utf-8")
    oauth = (root / "deploy" / "oauth.env.example").read_text(encoding="utf-8")

    assert "MCP_AUTH_MODE=builtin_oauth" in personal
    assert "MCP_BUILTIN_OAUTH_CLIENT_SECRET_HASH=" in personal
    assert "GITHUB_AUTH_MODE=pat" in personal
    assert "ALLOW_ALL_REPOS=true" in personal
    assert "ALLOWED_REPOS=" in personal
    assert "ALLOW_WORKFLOW_EDIT=true" in personal
    assert "ALLOW_DELETE_FILES=true" in personal
    assert "WORKSPACE_ALLOW_NETWORK=true" in personal
    assert "WORKSPACE_SHELL=powershell.exe" in personal
    assert "WRITE_BRANCH_PREFIX=spark/" in personal
    assert "MCP_AUTH_MODE=oauth" in oauth
    assert "GITHUB_AUTH_MODE=github_app" in oauth
