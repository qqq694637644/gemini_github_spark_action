from __future__ import annotations

import base64
import hashlib
import re
from argparse import Namespace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.auth.builtin import hash_secret, verify_secret
from app.config.settings import Settings
from app.main import create_app
from scripts import setup_windows_builtin_oauth


def builtin_settings(tmp_path: Path) -> Settings:
    return Settings(
        public_base_url="http://testserver/gemini_mcp",
        mcp_auth_mode="builtin_oauth",
        mcp_builtin_oauth_client_id="gemini-personal",
        mcp_builtin_oauth_client_secret_hash=hash_secret("client-secret"),
        mcp_builtin_oauth_admin_password_hash=hash_secret("approval-password"),
        mcp_builtin_oauth_redirect_uris="https://gemini.google.com/oauth/callback",
        mcp_builtin_oauth_key_path=str(tmp_path / "oauth-key.pem"),
        mcp_builtin_oauth_db_path=str(tmp_path / "oauth.db"),
        allow_all_repos=True,
        workspace_root=str(tmp_path / "workspaces"),
        workspace_operation_root=str(tmp_path / "operations"),
        audit_db_url=f"sqlite:///{tmp_path / 'audit.db'}",
    )


def pkce() -> tuple[str, str]:
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def request_id_from_html(body: str) -> str:
    match = re.search(r'name="request_id" value="([^"]+)"', body)
    assert match
    return match.group(1)


@pytest.mark.asyncio
async def test_builtin_oauth_authorization_code_refresh_and_revocation(tmp_path: Path) -> None:
    settings = builtin_settings(tmp_path)
    app = create_app(settings, enforce_single_instance=False)
    verifier, challenge = pkce()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        metadata = await client.get("/.well-known/oauth-authorization-server")
        authorize = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "gemini-personal",
                "redirect_uri": "https://gemini.google.com/oauth/callback",
                "scope": "github:read github:write github:workflow github:merge",
                "state": "state-123",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": settings.mcp_resource_url,
            },
        )
        request_id = request_id_from_html(authorize.text)
        approval = await client.post(
            "/oauth/authorize",
            data={"request_id": request_id, "admin_password": "approval-password", "decision": "approve"},
            follow_redirects=False,
        )
        redirect = urlsplit(approval.headers["location"])
        query = parse_qs(redirect.query)
        code = query["code"][0]

        token = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "gemini-personal",
                "client_secret": "client-secret",
                "redirect_uri": "https://gemini.google.com/oauth/callback",
                "code": code,
                "code_verifier": verifier,
            },
        )
        token_payload = token.json()
        verified = await app.state.mcp_token_verifier.verify_token(token_payload["access_token"])

        refreshed = await client.post(
            "/oauth/token",
            auth=("gemini-personal", "client-secret"),
            data={"grant_type": "refresh_token", "refresh_token": token_payload["refresh_token"]},
        )
        refreshed_payload = refreshed.json()
        replay = await client.post(
            "/oauth/token",
            auth=("gemini-personal", "client-secret"),
            data={"grant_type": "refresh_token", "refresh_token": token_payload["refresh_token"]},
        )
        revoked = await client.post(
            "/oauth/revoke",
            auth=("gemini-personal", "client-secret"),
            data={"token": refreshed_payload["refresh_token"]},
        )
        revoked_use = await client.post(
            "/oauth/token",
            auth=("gemini-personal", "client-secret"),
            data={"grant_type": "refresh_token", "refresh_token": refreshed_payload["refresh_token"]},
        )

    await app.state.github.aclose()
    app.state.audit.close()

    assert metadata.status_code == 200
    assert metadata.json()["issuer"] == "http://testserver/gemini_mcp"
    assert metadata.json()["code_challenge_methods_supported"] == ["S256"]
    assert authorize.status_code == 200
    assert "个人授权密码" in authorize.text
    assert approval.status_code == 302
    assert query["state"] == ["state-123"]
    assert query["iss"] == ["http://testserver/gemini_mcp"]
    assert token.status_code == 200
    assert token_payload["token_type"] == "Bearer"
    assert token_payload["refresh_token"]
    assert verified is not None
    assert verified.subject == "personal-user"
    assert verified.client_id == "gemini-personal"
    assert verified.claims["github_repositories"] == ["*"]
    assert refreshed.status_code == 200
    assert refreshed_payload["refresh_token"] != token_payload["refresh_token"]
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    assert revoked.status_code == 200
    assert revoked_use.status_code == 400
    assert revoked_use.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_builtin_oauth_rejects_wrong_password_and_unregistered_redirect(tmp_path: Path) -> None:
    settings = builtin_settings(tmp_path)
    app = create_app(settings, enforce_single_instance=False)
    _, challenge = pkce()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        bad_redirect = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "gemini-personal",
                "redirect_uri": "https://evil.example/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        authorize = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "gemini-personal",
                "redirect_uri": "https://gemini.google.com/oauth/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        wrong = await client.post(
            "/oauth/authorize",
            data={
                "request_id": request_id_from_html(authorize.text),
                "admin_password": "wrong-password",
                "decision": "approve",
            },
        )

    await app.state.github.aclose()
    app.state.audit.close()

    assert bad_redirect.status_code == 400
    assert "not registered" in bad_redirect.text
    assert wrong.status_code == 401
    assert "授权密码错误" in wrong.text


def test_secret_hashing_and_signing_key_are_persistent(tmp_path: Path) -> None:
    encoded = hash_secret("correct horse battery staple")
    assert verify_secret("correct horse battery staple", encoded)
    assert not verify_secret("wrong", encoded)

    first = create_app(builtin_settings(tmp_path), enforce_single_instance=False)
    first_key = first.state.mcp_token_verifier.key_id
    first.state.audit.close()
    second = create_app(builtin_settings(tmp_path), enforce_single_instance=False)
    second_key = second.state.mcp_token_verifier.key_id
    second.state.audit.close()

    assert first_key == second_key
    assert (tmp_path / "oauth-key.pem").is_file()
    assert (tmp_path / "oauth.db").is_file()


@pytest.mark.asyncio
async def test_builtin_oauth_issued_token_initializes_real_mcp_session(tmp_path: Path) -> None:
    settings = builtin_settings(tmp_path)
    app = create_app(settings, enforce_single_instance=False)
    verifier, challenge = pkce()
    transport = httpx.ASGITransport(app=app)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "builtin-oauth-test", "version": "1.0"},
        },
    }

    async with app.state.mcp.session_manager.run():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            challenge_response = await client.head("/mcp")
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
            authorize = await client.get(
                "/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": "gemini-personal",
                    "redirect_uri": "https://gemini.google.com/oauth/callback",
                    "scope": "github:read github:write github:workflow github:merge",
                    "state": "mcp-state",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "resource": settings.mcp_resource_url,
                },
            )
            approval = await client.post(
                "/oauth/authorize",
                data={
                    "request_id": request_id_from_html(authorize.text),
                    "admin_password": "approval-password",
                    "decision": "approve",
                },
                follow_redirects=False,
            )
            code = parse_qs(urlsplit(approval.headers["location"]).query)["code"][0]
            token_response = await client.post(
                "/oauth/token",
                auth=("gemini-personal", "client-secret"),
                data={
                    "grant_type": "authorization_code",
                    "redirect_uri": "https://gemini.google.com/oauth/callback",
                    "code": code,
                    "code_verifier": verifier,
                    "resource": settings.mcp_resource_url,
                },
            )
            access_token = token_response.json()["access_token"]
            initialized = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json=initialize,
            )
            session_id = initialized.headers.get("mcp-session-id")
            terminated = await client.delete(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                    "MCP-Session-Id": session_id or "",
                },
            )

    await app.state.github.aclose()
    app.state.audit.close()

    assert challenge_response.status_code == 401
    assert "resource_metadata=" in challenge_response.headers["www-authenticate"]
    assert "gemini_mcp/mcp" in challenge_response.headers["www-authenticate"]
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == "http://testserver/gemini_mcp/mcp"
    assert metadata.json()["authorization_servers"] == ["http://testserver/gemini_mcp"]
    assert token_response.status_code == 200
    assert initialized.status_code == 200
    assert session_id
    assert terminated.status_code in {200, 204}


def test_windows_setup_writes_hashed_oauth_config_without_printing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        setup_windows_builtin_oauth,
        "parse_args",
        lambda: Namespace(
            public_base_url="https://gateway.example.com/gemini_mcp",
            redirect_uri=["https://gemini.google.com/oauth/callback"],
            github_username="octocat",
            allowed_repos="",
            client_id="gemini-personal",
            env_file=".env",
            force=False,
        ),
    )
    answers = iter(["github-token-value-with-enough-length", "approval-password-strong", "approval-password-strong"])
    monkeypatch.setattr(setup_windows_builtin_oauth.getpass, "getpass", lambda prompt: next(answers))

    setup_windows_builtin_oauth.main()

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    credentials = (tmp_path / "data" / "gemini-oauth-client.txt").read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert 'MCP_AUTH_MODE="builtin_oauth"' in env_text
    assert 'PUBLIC_BASE_URL="https://gateway.example.com/gemini_mcp"' in env_text
    assert 'MCP_OAUTH_AUDIENCE="https://gateway.example.com/gemini_mcp/mcp"' in env_text
    assert 'ALLOW_ALL_REPOS="true"' in env_text
    assert 'ALLOWED_REPOS=""' in env_text
    assert 'ALLOW_WORKFLOW_EDIT="true"' in env_text
    assert 'ALLOW_DELETE_FILES="true"' in env_text
    assert 'WORKSPACE_ALLOW_NETWORK="true"' in env_text
    assert 'WORKSPACE_SHELL="powershell.exe"' in env_text
    assert "github-token-value-with-enough-length" in env_text
    assert "approval-password-strong" not in env_text
    assert "OAuth client secret:" in credentials
    assert "github-token-value-with-enough-length" not in credentials
    assert "approval-password-strong" not in credentials
    client_secret = credentials.split("OAuth client secret: ", 1)[1].splitlines()[0]
    assert client_secret not in output
    assert "github-token-value-with-enough-length" not in output
    assert (tmp_path / "data" / "oauth-signing-key.pem").is_file()


def test_windows_setup_optional_repository_allowlist_still_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        setup_windows_builtin_oauth,
        "parse_args",
        lambda: Namespace(
            public_base_url="https://gateway.example.com/gemini_mcp",
            redirect_uri=["https://gemini.google.com/oauth/callback"],
            github_username="octocat",
            allowed_repos="octocat/demo,octocat/other",
            client_id="gemini-personal",
            env_file=".env",
            force=False,
        ),
    )
    answers = iter(["github-token-value-with-enough-length", "approval-password-strong", "approval-password-strong"])
    monkeypatch.setattr(setup_windows_builtin_oauth.getpass, "getpass", lambda prompt: next(answers))

    setup_windows_builtin_oauth.main()

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert 'ALLOW_ALL_REPOS="false"' in env_text
    assert 'ALLOWED_REPOS="octocat/demo,octocat/other"' in env_text
