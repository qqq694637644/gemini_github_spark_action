from pathlib import Path


def test_validation_workflow_push_runs_only_for_main_branch() -> None:
    workflow = Path(".github/workflows/mcp-validation.yml").read_text(encoding="utf-8")
    push_block = workflow.split("pull_request:", 1)[0]

    assert "export_openapi" not in workflow
    assert "docker build" in workflow
    assert "push:" in push_block
    assert "branches:" in push_block
    assert "- main" in push_block


def test_remote_validation_workflow_requires_an_explicit_endpoint_and_secret() -> None:
    workflow = Path(".github/workflows/remote-mcp-validation.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch" in workflow
    assert "mcp_server_url" in workflow
    assert "MCP_ACCEPTANCE_ACCESS_TOKEN" in workflow
    assert "scripts/validate_remote_mcp.py" in workflow
