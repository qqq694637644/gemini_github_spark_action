from __future__ import annotations

import argparse
import getpass
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from app.auth.builtin import hash_secret, load_or_create_signing_key


def env_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a Windows personal OAuth .env for the Gemini Spark MCP gateway.")
    parser.add_argument("--public-base-url", required=True, help="Public prefix URL, e.g. https://host.example/gemini_mcp")
    parser.add_argument("--redirect-uri", action="append", required=True, help="Gemini redirect URI; repeat for multiple URIs")
    parser.add_argument("--github-username", required=True)
    parser.add_argument("--allowed-repos", required=True, help="Comma-separated owner/repo allowlist")
    parser.add_argument("--client-id", default="gemini-spark-personal")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def validate_url(value: str, *, name: str, require_https: bool = True) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise SystemExit(f"{name} must be an absolute HTTP(S) URL without query or fragment: {value}")
    if require_https and parsed.scheme != "https":
        raise SystemExit(f"{name} must use HTTPS for production: {value}")
    return normalized


def prompt_secret(label: str, *, minimum_length: int) -> str:
    first = getpass.getpass(f"{label}: ")
    if len(first) < minimum_length:
        raise SystemExit(f"{label} must contain at least {minimum_length} characters.")
    return first


def main() -> None:
    args = parse_args()
    public_base_url = validate_url(args.public_base_url, name="PUBLIC_BASE_URL")
    redirect_uris = [validate_url(value, name="redirect URI") for value in args.redirect_uri]
    allowed_repos = [item.strip() for item in args.allowed_repos.split(",") if item.strip()]
    if not allowed_repos or any("/" not in item for item in allowed_repos):
        raise SystemExit("--allowed-repos must contain one or more owner/repo values.")

    env_path = Path(args.env_file).resolve()
    if env_path.exists() and not args.force:
        raise SystemExit(f"{env_path} already exists. Re-run with --force only after backing it up.")

    github_token = prompt_secret("GitHub fine-grained PAT (input is hidden)", minimum_length=20)
    approval_password = prompt_secret("Personal OAuth approval password (input is hidden)", minimum_length=16)
    confirmation = getpass.getpass("Repeat the OAuth approval password: ")
    if approval_password != confirmation:
        raise SystemExit("OAuth approval passwords do not match.")

    client_secret = secrets.token_urlsafe(48)
    client_secret_hash = hash_secret(client_secret)
    approval_password_hash = hash_secret(approval_password)
    parsed_public = urlsplit(public_base_url)
    resource_url = f"{public_base_url}/mcp"

    values = {
        "APP_ENV": "production",
        "PUBLIC_BASE_URL": public_base_url,
        "MCP_PATH": "/mcp",
        "MCP_ALLOWED_HOSTS": parsed_public.netloc,
        "MCP_ALLOWED_ORIGINS": "",
        "MCP_AUTH_MODE": "builtin_oauth",
        "MCP_BUILTIN_OAUTH_CLIENT_ID": args.client_id,
        "MCP_BUILTIN_OAUTH_CLIENT_SECRET_HASH": client_secret_hash,
        "MCP_BUILTIN_OAUTH_ADMIN_PASSWORD_HASH": approval_password_hash,
        "MCP_BUILTIN_OAUTH_REDIRECT_URIS": ",".join(redirect_uris),
        "MCP_BUILTIN_OAUTH_SUBJECT": "personal-user",
        "MCP_BUILTIN_OAUTH_KEY_PATH": "./data/oauth-signing-key.pem",
        "MCP_BUILTIN_OAUTH_DB_PATH": "./data/oauth.db",
        "MCP_BUILTIN_OAUTH_DEFAULT_SCOPES": "github:read,github:write,github:workflow,github:merge",
        "MCP_BUILTIN_OAUTH_CODE_TTL_SECONDS": "300",
        "MCP_BUILTIN_OAUTH_ACCESS_TOKEN_TTL_SECONDS": "3600",
        "MCP_BUILTIN_OAUTH_REFRESH_TOKEN_TTL_SECONDS": "2592000",
        "MCP_OAUTH_AUDIENCE": resource_url,
        "MCP_OAUTH_REQUIRED_SCOPES": "github:read",
        "MCP_OAUTH_REPO_CLAIM": "github_repositories",
        "MCP_OAUTH_REQUIRE_REPO_CLAIM": "true",
        "MCP_OAUTH_CLOCK_SKEW_SECONDS": "30",
        "GITHUB_AUTH_MODE": "pat",
        "GITHUB_TOKEN": github_token,
        "GITHUB_GIT_USERNAME": args.github_username,
        "GITHUB_API_BASE_URL": "https://api.github.com",
        "GITHUB_API_VERSION": "2026-03-10",
        "GITHUB_USE_ENV_PROXY": "false",
        "ALLOW_ALL_REPOS": "false",
        "ALLOWED_REPOS": ",".join(allowed_repos),
        "READ_BRANCH_ALLOWLIST": "*",
        "WRITE_BRANCH_PREFIX": "spark/",
        "DEFAULT_BASE_BRANCH": "main",
        "ALLOW_WORKFLOW_EDIT": "false",
        "ALLOW_DELETE_FILES": "false",
        "WORKSPACE_ROOT": "./data/workspaces",
        "WORKSPACE_OPERATION_ROOT": "./data/operations",
        "WORKSPACE_ALLOW_NETWORK": "false",
        "WORKSPACE_SHELL": "pwsh",
        "WORKSPACE_GIT_USER_NAME": "gemini-spark-gateway",
        "WORKSPACE_GIT_USER_EMAIL": "gemini-spark-gateway@users.noreply.github.com",
        "WORKSPACE_PYTHON_VENV_ENABLED": "true",
        "WORKSPACE_PYTHON_VENV_DIR": ".venv",
        "WORKSPACE_PYTHON_AUTO_GITIGNORE": "true",
        "WORKSPACE_PYTHON_AUTO_ACTIVATE": "true",
        "AUDIT_DB_URL": "sqlite:///./data/audit.db",
        "REQUEST_TIMEOUT_SECONDS": "30",
    }

    env_path.write_text("\n".join(f"{key}={env_quote(value)}" for key, value in values.items()) + "\n", encoding="utf-8")
    load_or_create_signing_key("./data/oauth-signing-key.pem")

    credential_path = Path("./data/gemini-oauth-client.txt").resolve()
    credential_path.parent.mkdir(parents=True, exist_ok=True)
    credential_path.write_text(
        "\n".join(
            [
                f"MCP URL: {resource_url}",
                f"OAuth client ID: {args.client_id}",
                f"OAuth client secret: {client_secret}",
                f"Registered redirect URI: {redirect_uris[0]}",
                "Keep this file private. Gemini needs the client ID and client secret.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Created: {env_path}")
    print(f"Created: {credential_path}")
    print(f"MCP URL: {resource_url}")
    print(f"OAuth client ID: {args.client_id}")
    print("OAuth client secret: saved only in data/gemini-oauth-client.txt")
    print("The GitHub token, client secret, and approval password were not printed.")


if __name__ == "__main__":
    main()
