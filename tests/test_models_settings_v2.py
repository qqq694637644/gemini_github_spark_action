import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.models.branches import CreateWorkBranchRequest
from app.models.workspaces import PrepareWorkspaceRequest


def make_settings(tmp_path, **kwargs) -> Settings:
    return Settings(workspace_root=str(tmp_path / "w"), workspace_operation_root=str(tmp_path / "operations"), audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}", **kwargs)


def test_workspace_python_settings_describe_current_bootstrap_surface(tmp_path):
    settings = make_settings(tmp_path)

    assert settings.workspace_python_venv_enabled is True
    assert settings.workspace_python_venv_dir == ".venv"
    assert settings.workspace_python_venv_python == "py -3.13"
    assert settings.workspace_python_auto_gitignore is True
    assert settings.workspace_python_auto_activate is True
    assert {name for name in Settings.model_fields if name.startswith("workspace_python_")} == {
        "workspace_python_venv_enabled",
        "workspace_python_venv_dir",
        "workspace_python_venv_python",
        "workspace_python_auto_gitignore",
        "workspace_python_auto_activate",
    }


def test_default_read_branch_allowlist_allows_all_refs(tmp_path):
    settings = make_settings(tmp_path)

    assert settings.read_branch_allowlist == "*"
    assert settings.read_branch_patterns == ["*"]


def test_oauth_settings_derive_the_mcp_resource_and_scope_lists(tmp_path):
    settings = make_settings(
        tmp_path,
        public_base_url="https://gateway.example.com/root/",
        mcp_path="/mcp/",
        mcp_auth_mode="oauth",
        mcp_oauth_issuer_url="https://id.example.com",
        mcp_oauth_required_scopes="github:read, github:write",
        mcp_oauth_algorithms="RS256,ES256",
    )

    assert settings.mcp_path == "/mcp"
    assert settings.mcp_resource_url == "https://gateway.example.com/mcp"
    assert settings.oauth_audience == "https://gateway.example.com/mcp"
    assert settings.oauth_required_scope_list == ["github:read", "github:write"]
    assert settings.oauth_algorithm_list == ["RS256", "ES256"]


def test_production_auth_configuration_requires_https_and_credentials(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, app_env="production", public_base_url="http://gateway.example.com")

    with pytest.raises(ValidationError):
        make_settings(
            tmp_path,
            app_env="production",
            public_base_url="https://gateway.example.com",
            mcp_auth_mode="static_bearer",
            gateway_action_secret="",
        )

    with pytest.raises(ValidationError):
        make_settings(
            tmp_path,
            app_env="production",
            public_base_url="https://gateway.example.com",
            mcp_auth_mode="oauth",
            mcp_oauth_issuer_url="http://id.example.com",
        )


def test_oauth_settings_reject_symmetric_access_token_algorithms(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(
            tmp_path,
            mcp_auth_mode="oauth",
            mcp_oauth_issuer_url="https://id.example.com",
            mcp_oauth_algorithms="HS256",
        )


def test_create_work_branch_request_has_current_base_ref_shape():
    schema = CreateWorkBranchRequest.model_json_schema()
    properties = schema["properties"]

    assert "base_ref" in properties
    assert "base_sha" in properties
    assert "purpose_slug" in properties


def test_prepare_workspace_request_requires_idempotency_and_has_no_workspace_id():
    schema = PrepareWorkspaceRequest.model_json_schema()
    assert "workspace_id" not in schema["properties"]
    assert "idempotency_key" in schema["required"]


@pytest.mark.parametrize(
    "value",
    [
        r"C:\temp\venv",
        "C:/temp/venv",
        "C:temp/venv",
        "/tmp/venv",
        r"\\server\share\venv",
        "tools//venv",
        "tools:venv",
        "../venv",
    ],
)
def test_workspace_python_venv_dir_rejects_absolute_drive_and_invalid_paths(tmp_path, value: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, workspace_python_venv_dir=value)


def test_workspace_python_venv_dir_normalizes_relative_trailing_slash(tmp_path) -> None:
    settings = make_settings(tmp_path, workspace_python_venv_dir="tools/.venv/")

    assert settings.workspace_python_venv_dir == "tools/.venv"
