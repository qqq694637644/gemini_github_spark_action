from __future__ import annotations

import re
import sys
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?b?)?\s*$", re.IGNORECASE)
_SIZE_MULTIPLIERS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
}


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_size_to_bytes(value: int | str | None, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ValueError("size value is required")
        return default
    if isinstance(value, int):
        return value
    match = _SIZE_RE.match(str(value))
    if not match:
        raise ValueError(f"Invalid size value: {value!r}")
    number, suffix = match.groups()
    return int(float(number) * _SIZE_MULTIPLIERS[(suffix or "").lower()])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    public_base_url: str = "http://localhost:8000"
    mcp_auth_mode: Literal["static_bearer", "builtin_oauth", "oauth"] = "static_bearer"
    gateway_action_secret: str = ""
    mcp_path: str = "/mcp"
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""
    mcp_oauth_issuer_url: str = ""
    mcp_oauth_jwks_url: str = ""
    mcp_oauth_introspection_url: str = ""
    mcp_oauth_introspection_client_id: str = ""
    mcp_oauth_introspection_client_secret: str = ""
    mcp_oauth_audience: str = ""
    mcp_oauth_algorithms: str = "RS256,ES256"
    mcp_oauth_required_scopes: str = "github:read"
    mcp_oauth_repo_claim: str = "github_repositories"
    mcp_oauth_require_repo_claim: bool = True
    mcp_oauth_http_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    mcp_oauth_jwks_cache_seconds: int = Field(default=300, ge=1, le=86_400)
    mcp_oauth_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    mcp_builtin_oauth_client_id: str = ""
    mcp_builtin_oauth_client_secret_hash: str = ""
    mcp_builtin_oauth_admin_password_hash: str = ""
    mcp_builtin_oauth_redirect_uris: str = ""
    mcp_builtin_oauth_subject: str = "personal-user"
    mcp_builtin_oauth_key_path: str = "./data/oauth-signing-key.pem"
    mcp_builtin_oauth_db_path: str = "./data/oauth.db"
    mcp_builtin_oauth_default_scopes: str = "github:read,github:write,github:workflow,github:merge"
    mcp_builtin_oauth_code_ttl_seconds: int = Field(default=300, ge=60, le=900)
    mcp_builtin_oauth_access_token_ttl_seconds: int = Field(default=3600, ge=300, le=86_400)
    mcp_builtin_oauth_refresh_token_ttl_seconds: int = Field(default=2_592_000, ge=3600, le=31_536_000)

    github_auth_mode: Literal["pat", "github_app"] = "pat"
    github_api_base_url: str = "https://api.github.com"
    github_api_version: str = "2026-03-10"
    github_use_env_proxy: bool = False
    github_token: str | None = None
    github_git_username: str | None = None
    github_app_id: str | None = None
    github_app_private_key: str | None = None
    github_installation_id: str | None = None

    allowed_repos: str = ""
    allow_all_repos: bool = False
    read_branch_allowlist: str = "*"
    write_branch_prefix: str = "spark/"
    default_base_branch: str = "main"

    allow_workflow_edit: bool = False
    allow_delete_files: bool = False

    max_log_bytes: int = Field(default=80_000)
    max_log_lines: int = 500
    max_blob_read_bytes: int = Field(default=2 * 1024 * 1024)

    workspace_root: str = "./data/workspaces"
    workspace_operation_root: str = "./data/operations"
    workspace_default_timeout_seconds: int = 60
    workspace_max_timeout_seconds: int = 300
    workspace_command_kill_grace_seconds: int = 5
    workspace_command_reader_grace_seconds: int = 2
    workspace_command_shutdown_seconds: int = 10
    workspace_operation_progress_flush_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    workspace_operation_ttl_hours: int = 168
    workspace_max_output_bytes: int = Field(default=80_000)
    workspace_max_diff_bytes: int = Field(default=200_000)
    workspace_max_patch_bytes: int = Field(default=200_000)
    workspace_max_write_bytes: int = Field(default=200_000)
    workspace_max_changed_files: int = 200
    workspace_ttl_hours: int = 48
    workspace_allow_network: bool = False
    workspace_shell: str = "pwsh"
    workspace_git_user_name: str = "gemini-spark-gateway"
    workspace_git_user_email: str = "gemini-spark-gateway@users.noreply.github.com"
    workspace_python_venv_enabled: bool = True
    workspace_python_venv_dir: str = ".venv"
    workspace_python_venv_python: str = Field(default_factory=lambda: sys.executable)
    workspace_python_auto_gitignore: bool = True
    workspace_python_auto_activate: bool = True

    audit_db_url: str = "sqlite:///./data/audit.db"
    request_timeout_seconds: float = 30.0


    @field_validator(
        "max_log_bytes",
        "max_blob_read_bytes",
        "workspace_max_output_bytes",
        "workspace_max_diff_bytes",
        "workspace_max_patch_bytes",
        "workspace_max_write_bytes",
        mode="before",
    )
    @classmethod
    def _parse_size_fields(cls, value: int | str | None) -> int:
        return parse_size_to_bytes(value)

    @field_validator("workspace_python_venv_dir")
    @classmethod
    def _normalize_workspace_python_venv_dir(cls, value: str) -> str:
        raw = str(value).strip().replace("\\", "/")
        if not raw:
            raise ValueError("workspace_python_venv_dir must not be empty")
        if raw.startswith("/") or raw.startswith("//") or re.match(r"^[A-Za-z]:", raw):
            raise ValueError("workspace_python_venv_dir must be a repository-relative path")
        normalized = raw.rstrip("/")
        parts = normalized.split("/")
        if not parts or any(part in {"", ".", ".."} or ":" in part for part in parts):
            raise ValueError("workspace_python_venv_dir must be a relative path without traversal, empty segments, or drive syntax")
        return "/".join(parts)

    @field_validator("workspace_python_venv_python")
    @classmethod
    def _validate_workspace_python_venv_python(cls, value: str) -> str:
        command = str(value).strip()
        if not command:
            raise ValueError("workspace_python_venv_python must not be empty")
        return command

    @field_validator("write_branch_prefix", "default_base_branch")
    @classmethod
    def _validate_branch_policy_values(cls, value: str, info) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be empty")
        if normalized != value:
            raise ValueError(f"{info.field_name} must not have leading or trailing whitespace")
        return normalized

    @field_validator("mcp_path")
    @classmethod
    def _normalize_mcp_path(cls, value: str) -> str:
        path = str(value).strip()
        if not path.startswith("/") or path == "/" or "?" in path or "#" in path:
            raise ValueError("mcp_path must be an absolute non-root URL path without query or fragment")
        return path.rstrip("/")

    @model_validator(mode="after")
    def _validate_auth_and_public_urls(self) -> Settings:
        public = urlsplit(self.public_base_url)
        if public.scheme not in {"http", "https"} or not public.netloc or public.query or public.fragment:
            raise ValueError("public_base_url must be an absolute HTTP(S) URL without query or fragment")
        if self.app_env.lower() == "production" and public.scheme != "https":
            raise ValueError("production public_base_url must use HTTPS")

        if self.mcp_auth_mode == "static_bearer":
            if self.app_env.lower() == "production" and not self.static_bearer_tokens:
                raise ValueError("GATEWAY_ACTION_SECRET is required for production static_bearer mode")
            return self

        if self.mcp_auth_mode == "builtin_oauth":
            missing = [
                name
                for name, value in (
                    ("MCP_BUILTIN_OAUTH_CLIENT_ID", self.mcp_builtin_oauth_client_id),
                    ("MCP_BUILTIN_OAUTH_CLIENT_SECRET_HASH", self.mcp_builtin_oauth_client_secret_hash),
                    ("MCP_BUILTIN_OAUTH_ADMIN_PASSWORD_HASH", self.mcp_builtin_oauth_admin_password_hash),
                    ("MCP_BUILTIN_OAUTH_REDIRECT_URIS", self.mcp_builtin_oauth_redirect_uris),
                )
                if not value.strip()
            ]
            if missing:
                raise ValueError(f"Built-in OAuth configuration is incomplete: {', '.join(missing)}")
            if not self.builtin_oauth_default_scope_list:
                raise ValueError("MCP_BUILTIN_OAUTH_DEFAULT_SCOPES must contain at least one scope")
            if self.app_env.lower() == "production":
                if self.allow_all_repos:
                    raise ValueError("production built-in OAuth requires ALLOW_ALL_REPOS=false")
                if not self.allowed_repo_set:
                    raise ValueError("production built-in OAuth requires at least one ALLOWED_REPOS entry")
            for redirect_uri in self.builtin_oauth_redirect_uri_list:
                parsed_redirect = urlsplit(redirect_uri)
                if parsed_redirect.scheme not in {"http", "https"} or not parsed_redirect.netloc or parsed_redirect.fragment:
                    raise ValueError("Each MCP_BUILTIN_OAUTH_REDIRECT_URIS value must be an absolute HTTP(S) URL")
                if self.app_env.lower() == "production" and parsed_redirect.scheme != "https":
                    raise ValueError("production built-in OAuth redirect URIs must use HTTPS")
            return self

        issuer = urlsplit(self.mcp_oauth_issuer_url)
        if issuer.scheme not in {"http", "https"} or not issuer.netloc or issuer.query or issuer.fragment:
            raise ValueError("MCP_OAUTH_ISSUER_URL must be an absolute HTTP(S) URL without query or fragment")
        if self.app_env.lower() == "production" and issuer.scheme != "https":
            raise ValueError("production MCP_OAUTH_ISSUER_URL must use HTTPS")
        if not self.oauth_required_scope_list:
            raise ValueError("MCP_OAUTH_REQUIRED_SCOPES must contain at least one baseline scope")
        if not self.oauth_algorithm_list and not self.mcp_oauth_introspection_url:
            raise ValueError("MCP_OAUTH_ALGORITHMS is required for JWT mode")
        asymmetric_algorithms = {
            "RS256",
            "RS384",
            "RS512",
            "PS256",
            "PS384",
            "PS512",
            "ES256",
            "ES384",
            "ES512",
            "EdDSA",
        }
        unsupported = sorted(set(self.oauth_algorithm_list) - asymmetric_algorithms)
        if unsupported:
            raise ValueError(f"MCP_OAUTH_ALGORITHMS contains unsupported or symmetric algorithms: {unsupported}")
        for field_name, value in (
            ("MCP_OAUTH_JWKS_URL", self.mcp_oauth_jwks_url),
            ("MCP_OAUTH_INTROSPECTION_URL", self.mcp_oauth_introspection_url),
        ):
            if not value:
                continue
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError(f"{field_name} must be an absolute HTTP(S) URL without query or fragment")
            if self.app_env.lower() == "production" and parsed.scheme != "https":
                raise ValueError(f"production {field_name} must use HTTPS")
        return self

    @property
    def static_bearer_tokens(self) -> list[str]:
        return list(dict.fromkeys(parse_csv(self.gateway_action_secret)))

    @property
    def mcp_resource_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}{self.mcp_path}"

    @property
    def oauth_required_scope_list(self) -> list[str]:
        return parse_csv(self.mcp_oauth_required_scopes)

    @property
    def oauth_algorithm_list(self) -> list[str]:
        return parse_csv(self.mcp_oauth_algorithms)

    @property
    def oauth_audience(self) -> str:
        return self.mcp_oauth_audience.strip() or self.mcp_resource_url

    @property
    def oauth_issuer_url(self) -> str:
        if self.mcp_auth_mode == "builtin_oauth":
            return self.public_base_url.rstrip("/")
        return self.mcp_oauth_issuer_url.rstrip("/")

    @property
    def builtin_oauth_redirect_uri_list(self) -> list[str]:
        return parse_csv(self.mcp_builtin_oauth_redirect_uris)

    @property
    def builtin_oauth_default_scope_list(self) -> list[str]:
        return parse_csv(self.mcp_builtin_oauth_default_scopes)

    @property
    def allowed_repo_set(self) -> set[str]:
        return {repo.lower() for repo in parse_csv(self.allowed_repos)}

    @property
    def read_branch_patterns(self) -> list[str]:
        return parse_csv(self.read_branch_allowlist)



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
