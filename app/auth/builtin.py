from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.auth.provider import AccessToken, TokenVerifier

from app.config.settings import Settings

BUILTIN_SCOPES = ("github:read", "github:write", "github:workflow", "github:merge")
_SECRET_SCHEME = "pbkdf2_sha256"
_SECRET_ITERATIONS = 600_000
_PKCE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def hash_secret(value: str, *, iterations: int = _SECRET_ITERATIONS) -> str:
    if not value:
        raise ValueError("Secret must not be empty")
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, iterations)
    return f"{_SECRET_SCHEME}${iterations}${_b64url(salt)}${_b64url(digest)}"


def verify_secret(value: str, encoded: str) -> bool:
    try:
        scheme, iterations_raw, salt_raw, digest_raw = encoded.split("$", 3)
        if scheme != _SECRET_SCHEME:
            return False
        iterations = int(iterations_raw)
        if iterations < 100_000 or iterations > 5_000_000:
            return False
        salt = _b64url_decode(salt_raw)
        expected = _b64url_decode(digest_raw)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def load_or_create_signing_key(path_value: str) -> rsa.RSAPrivateKey:
    path = Path(path_value).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return _load_private_key(path)

    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=3072)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _load_private_key(path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return private_key


def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
    loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise ValueError(f"Built-in OAuth signing key is not an RSA private key: {path}")
    return loaded


class BuiltinOAuthStore:
    def __init__(self, path_value: str) -> None:
        self.path = Path(path_value).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_pending (
                    request_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0,
                    used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_oauth_pending_expiry ON oauth_pending(expires_at);
                CREATE INDEX IF NOT EXISTS idx_oauth_codes_expiry ON oauth_codes(expires_at);
                CREATE INDEX IF NOT EXISTS idx_oauth_refresh_expiry ON oauth_refresh_tokens(expires_at);
                """
            )

    def create_pending(self, payload: dict[str, Any], expires_at: int) -> str:
        request_id = secrets.token_urlsafe(32)
        with self._lock, self._connect() as connection:
            self._prune(connection)
            connection.execute(
                "INSERT INTO oauth_pending(request_id, payload, expires_at) VALUES (?, ?, ?)",
                (request_id, json.dumps(payload, separators=(",", ":")), expires_at),
            )
        return request_id

    def get_pending(self, request_id: str) -> tuple[dict[str, Any], int] | None:
        now = int(time.time())
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload, failures FROM oauth_pending WHERE request_id = ? AND used_at IS NULL AND expires_at > ?",
                (request_id, now),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"]), int(row["failures"])

    def record_pending_failure(self, request_id: str, *, max_failures: int = 5) -> None:
        now = int(time.time())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE oauth_pending
                   SET failures = failures + 1,
                       used_at = CASE WHEN failures + 1 >= ? THEN ? ELSE used_at END
                 WHERE request_id = ? AND used_at IS NULL
                """,
                (max_failures, now, request_id),
            )

    def consume_pending(self, request_id: str) -> dict[str, Any] | None:
        return self._consume("oauth_pending", "request_id", request_id)

    def create_code(self, code: str, payload: dict[str, Any], expires_at: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO oauth_codes(code_hash, payload, expires_at) VALUES (?, ?, ?)",
                (_token_hash(code), json.dumps(payload, separators=(",", ":")), expires_at),
            )

    def consume_code(self, code: str) -> dict[str, Any] | None:
        return self._consume("oauth_codes", "code_hash", _token_hash(code))

    def create_refresh(self, token: str, payload: dict[str, Any], expires_at: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO oauth_refresh_tokens(token_hash, payload, expires_at) VALUES (?, ?, ?)",
                (_token_hash(token), json.dumps(payload, separators=(",", ":")), expires_at),
            )

    def consume_refresh(self, token: str) -> dict[str, Any] | None:
        now = int(time.time())
        token_hash = _token_hash(token)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT payload FROM oauth_refresh_tokens
                 WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE oauth_refresh_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (now, token_hash),
            )
            connection.commit()
        return json.loads(row["payload"])

    def revoke_refresh(self, token: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE oauth_refresh_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE token_hash = ?",
                (int(time.time()), _token_hash(token)),
            )

    def _consume(self, table: str, key_name: str, key_value: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE {key_name} = ? AND used_at IS NULL AND expires_at > ?",  # noqa: S608
                (key_value, now),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            connection.execute(
                f"UPDATE {table} SET used_at = ? WHERE {key_name} = ? AND used_at IS NULL",  # noqa: S608
                (now, key_value),
            )
            connection.commit()
        return json.loads(row["payload"])

    @staticmethod
    def _prune(connection: sqlite3.Connection) -> None:
        cutoff = int(time.time()) - 86_400
        connection.execute("DELETE FROM oauth_pending WHERE expires_at < ? OR used_at < ?", (cutoff, cutoff))
        connection.execute("DELETE FROM oauth_codes WHERE expires_at < ? OR used_at < ?", (cutoff, cutoff))
        connection.execute("DELETE FROM oauth_refresh_tokens WHERE expires_at < ? OR revoked_at < ?", (cutoff, cutoff))


class BuiltinOAuthServer(TokenVerifier):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.private_key = load_or_create_signing_key(settings.mcp_builtin_oauth_key_path)
        self.public_key = self.private_key.public_key()
        self.store = BuiltinOAuthStore(settings.mcp_builtin_oauth_db_path)
        der = self.public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.key_id = hashlib.sha256(der).hexdigest()[:32]

    @property
    def issuer(self) -> str:
        return self.settings.oauth_issuer_url

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.issuer}/oauth/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/oauth/token"

    @property
    def jwks_uri(self) -> str:
        return f"{self.issuer}/oauth/jwks"

    @property
    def revocation_endpoint(self) -> str:
        return f"{self.issuer}/oauth/revoke"

    def router(self) -> APIRouter:
        router = APIRouter()
        router.add_api_route(
            "/.well-known/oauth-authorization-server",
            self.authorization_server_metadata,
            methods=["GET"],
            response_model=None,
        )
        router.add_api_route("/oauth/authorize", self.authorize_get, methods=["GET"], response_model=None)
        router.add_api_route("/oauth/authorize", self.authorize_post, methods=["POST"], response_model=None)
        router.add_api_route("/oauth/token", self.token, methods=["POST"], response_model=None)
        router.add_api_route("/oauth/jwks", self.jwks, methods=["GET"], response_model=None)
        router.add_api_route("/oauth/revoke", self.revoke, methods=["POST"], response_model=None)
        return router

    async def authorization_server_metadata(self) -> JSONResponse:
        return _oauth_json(
            {
                "issuer": self.issuer,
                "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint,
                "jwks_uri": self.jwks_uri,
                "revocation_endpoint": self.revocation_endpoint,
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "revocation_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "scopes_supported": list(BUILTIN_SCOPES),
                "authorization_response_iss_parameter_supported": True,
            }
        )

    async def jwks(self) -> JSONResponse:
        numbers = self.public_key.public_numbers()
        return _oauth_json(
            {
                "keys": [
                    {
                        "kty": "RSA",
                        "use": "sig",
                        "alg": "RS256",
                        "kid": self.key_id,
                        "n": _b64url(_int_bytes(numbers.n)),
                        "e": _b64url(_int_bytes(numbers.e)),
                    }
                ]
            }
        )

    async def authorize_get(self, request: Request) -> HTMLResponse | RedirectResponse:
        query = request.query_params
        redirect_uri = query.get("redirect_uri", "")
        state = query.get("state")
        error = self._validate_authorization_request(
            response_type=query.get("response_type", ""),
            client_id=query.get("client_id", ""),
            redirect_uri=redirect_uri,
            code_challenge=query.get("code_challenge", ""),
            code_challenge_method=query.get("code_challenge_method", ""),
            resource=query.get("resource"),
            scope=query.get("scope"),
        )
        if error:
            if redirect_uri in self.settings.builtin_oauth_redirect_uri_list:
                return self._authorization_redirect(redirect_uri, state, error=error[0], error_description=error[1])
            return self._html_error(error[1], status_code=400)

        scopes = self._requested_scopes(query.get("scope"))
        payload = {
            "client_id": self.settings.mcp_builtin_oauth_client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": query["code_challenge"],
            "resource": query.get("resource") or self.settings.mcp_resource_url,
            "scopes": scopes,
        }
        request_id = self.store.create_pending(payload, int(time.time()) + self.settings.mcp_builtin_oauth_code_ttl_seconds)
        return self._approval_page(request_id, payload)

    async def authorize_post(self, request: Request) -> HTMLResponse | RedirectResponse:
        form = await _parse_form(request)
        request_id = form.get("request_id", "")
        pending = self.store.get_pending(request_id)
        if pending is None:
            return self._html_error("授权请求不存在、已使用或已过期。请返回 Gemini 重新连接。", status_code=400)
        payload, failures = pending
        decision = form.get("decision", "deny")
        if decision != "approve":
            self.store.consume_pending(request_id)
            return self._authorization_redirect(
                payload["redirect_uri"],
                payload.get("state"),
                error="access_denied",
                error_description="The resource owner denied the request.",
            )

        password = form.get("admin_password", "")
        if not verify_secret(password, self.settings.mcp_builtin_oauth_admin_password_hash):
            self.store.record_pending_failure(request_id)
            remaining = max(0, 4 - failures)
            return self._approval_page(
                request_id,
                payload,
                error=f"授权密码错误。剩余尝试次数：{remaining}",
                status_code=401,
            )

        consumed = self.store.consume_pending(request_id)
        if consumed is None:
            return self._html_error("授权请求已被使用或已过期。请返回 Gemini 重新连接。", status_code=400)
        code = secrets.token_urlsafe(48)
        self.store.create_code(code, consumed, int(time.time()) + self.settings.mcp_builtin_oauth_code_ttl_seconds)
        return self._authorization_redirect(consumed["redirect_uri"], consumed.get("state"), code=code)

    async def token(self, request: Request) -> JSONResponse:
        form = await _parse_form(request)
        if not self._authenticate_client(request, form):
            return _oauth_error("invalid_client", "Client authentication failed.", status_code=401, authenticate=True)
        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            return self._authorization_code_token(form)
        if grant_type == "refresh_token":
            return self._refresh_token(form)
        return _oauth_error("unsupported_grant_type", "Only authorization_code and refresh_token are supported.")

    async def revoke(self, request: Request) -> JSONResponse:
        form = await _parse_form(request)
        if not self._authenticate_client(request, form):
            return _oauth_error("invalid_client", "Client authentication failed.", status_code=401, authenticate=True)
        token = form.get("token", "")
        if token:
            self.store.revoke_refresh(token)
        return _oauth_json({}, status_code=200)

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate = token.strip()
        if not candidate:
            return None
        try:
            claims = jwt.decode(
                candidate,
                key=self.public_key,
                algorithms=["RS256"],
                audience=self.settings.oauth_audience,
                issuer=self.issuer,
                leeway=self.settings.mcp_oauth_clock_skew_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]},
            )
        except jwt.PyJWTError:
            return None
        if not isinstance(claims, dict) or claims.get("client_id") != self.settings.mcp_builtin_oauth_client_id:
            return None
        scopes = _scope_list(str(claims.get("scope") or ""))
        return AccessToken(
            token=candidate,
            client_id=self.settings.mcp_builtin_oauth_client_id,
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.settings.oauth_audience,
            subject=str(claims["sub"]),
            claims=claims,
        )

    def _authorization_code_token(self, form: dict[str, str]) -> JSONResponse:
        code = form.get("code", "")
        payload = self.store.consume_code(code)
        if payload is None:
            return _oauth_error("invalid_grant", "Authorization code is invalid, expired, or already used.")
        if form.get("redirect_uri", "") != payload["redirect_uri"]:
            return _oauth_error("invalid_grant", "redirect_uri does not match the authorization request.")
        if form.get("resource") and form["resource"] != payload["resource"]:
            return _oauth_error("invalid_target", "resource does not match the authorization request.")
        verifier = form.get("code_verifier", "")
        if not _valid_pkce_verifier(verifier):
            return _oauth_error("invalid_grant", "A valid PKCE code_verifier is required.")
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        if not hmac.compare_digest(challenge, str(payload["code_challenge"])):
            return _oauth_error("invalid_grant", "PKCE verification failed.")
        return self._issue_token_response(payload)

    def _refresh_token(self, form: dict[str, str]) -> JSONResponse:
        refresh_token = form.get("refresh_token", "")
        payload = self.store.consume_refresh(refresh_token)
        if payload is None:
            return _oauth_error("invalid_grant", "Refresh token is invalid, expired, revoked, or already rotated.")
        if form.get("resource") and form["resource"] != payload["resource"]:
            return _oauth_error("invalid_target", "resource does not match the original grant.")
        original_scopes = set(payload["scopes"])
        requested = set(_scope_list(form.get("scope", ""))) if form.get("scope") else original_scopes
        if not requested or not requested <= original_scopes:
            return _oauth_error("invalid_scope", "Requested refresh scope exceeds the original grant.")
        payload["scopes"] = sorted(requested)
        return self._issue_token_response(payload)

    def _issue_token_response(self, payload: dict[str, Any]) -> JSONResponse:
        now = int(time.time())
        scopes = [scope for scope in payload["scopes"] if scope in BUILTIN_SCOPES]
        repositories = ["*"] if self.settings.allow_all_repos else sorted(self.settings.allowed_repo_set)
        claims = {
            "iss": self.issuer,
            "aud": self.settings.oauth_audience,
            "sub": self.settings.mcp_builtin_oauth_subject,
            "client_id": self.settings.mcp_builtin_oauth_client_id,
            "scope": " ".join(scopes),
            self.settings.mcp_oauth_repo_claim: repositories,
            "auth_mode": "builtin_oauth",
            "resource": self.settings.mcp_resource_url,
            "iat": now,
            "exp": now + self.settings.mcp_builtin_oauth_access_token_ttl_seconds,
            "jti": secrets.token_urlsafe(24),
        }
        access_token = jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": self.key_id})
        refresh_token = secrets.token_urlsafe(64)
        refresh_payload = {
            "client_id": self.settings.mcp_builtin_oauth_client_id,
            "subject": self.settings.mcp_builtin_oauth_subject,
            "scopes": scopes,
            "resource": self.settings.mcp_resource_url,
        }
        self.store.create_refresh(
            refresh_token,
            refresh_payload,
            now + self.settings.mcp_builtin_oauth_refresh_token_ttl_seconds,
        )
        return _oauth_json(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": self.settings.mcp_builtin_oauth_access_token_ttl_seconds,
                "refresh_token": refresh_token,
                "scope": " ".join(scopes),
            }
        )

    def _authenticate_client(self, request: Request, form: dict[str, str]) -> bool:
        client_id = form.get("client_id", "")
        client_secret = form.get("client_secret", "")
        authorization = request.headers.get("Authorization", "")
        scheme, separator, encoded = authorization.partition(" ")
        if separator and scheme.lower() == "basic":
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                basic_id, basic_secret = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError):
                return False
            if client_id and client_id != basic_id:
                return False
            client_id, client_secret = basic_id, basic_secret
        return hmac.compare_digest(client_id, self.settings.mcp_builtin_oauth_client_id) and verify_secret(
            client_secret,
            self.settings.mcp_builtin_oauth_client_secret_hash,
        )

    def _validate_authorization_request(
        self,
        *,
        response_type: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        resource: str | None,
        scope: str | None,
    ) -> tuple[str, str] | None:
        if response_type != "code":
            return "unsupported_response_type", "Only response_type=code is supported."
        if not hmac.compare_digest(client_id, self.settings.mcp_builtin_oauth_client_id):
            return "unauthorized_client", "Unknown OAuth client_id."
        if redirect_uri not in self.settings.builtin_oauth_redirect_uri_list:
            return "invalid_request", "redirect_uri is not registered."
        if code_challenge_method != "S256" or not _valid_pkce_challenge(code_challenge):
            return "invalid_request", "PKCE S256 code_challenge is required."
        if resource and resource != self.settings.mcp_resource_url:
            return "invalid_target", "The requested resource does not match this MCP server."
        try:
            self._requested_scopes(scope)
        except ValueError as exc:
            return "invalid_scope", str(exc)
        return None

    def _requested_scopes(self, raw_scope: str | None) -> list[str]:
        requested = _scope_list(raw_scope or "") or self.settings.builtin_oauth_default_scope_list
        unsupported = sorted(set(requested) - set(BUILTIN_SCOPES))
        if unsupported:
            raise ValueError(f"Unsupported scopes: {', '.join(unsupported)}")
        return list(dict.fromkeys(requested))

    def _authorization_redirect(
        self,
        redirect_uri: str,
        state: str | None,
        *,
        code: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> RedirectResponse:
        params: list[tuple[str, str]] = []
        if code:
            params.append(("code", code))
        if error:
            params.append(("error", error))
        if error_description:
            params.append(("error_description", error_description))
        if state:
            params.append(("state", state))
        params.append(("iss", self.issuer))
        parsed = urlsplit(redirect_uri)
        query = parse_qsl(parsed.query, keep_blank_values=True) + params
        target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    def _approval_page(
        self,
        request_id: str,
        payload: dict[str, Any],
        *,
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        scopes = "、".join(html.escape(scope) for scope in payload["scopes"])
        repositories = "、".join(html.escape(repo) for repo in sorted(self.settings.allowed_repo_set)) or "无"
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        body = f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>授权 Gemini Spark</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background:#111827; color:#f9fafb; margin:0; padding:32px; }}
    main {{ max-width:680px; margin:auto; background:#1f2937; border-radius:16px; padding:28px; }}
    code {{ word-break:break-all; color:#bfdbfe; }}
    label {{ display:block; margin-top:20px; font-weight:600; }}
    input {{ width:100%; box-sizing:border-box; padding:12px; margin-top:8px; border-radius:8px; border:1px solid #4b5563; }}
    .actions {{ display:flex; gap:12px; margin-top:24px; }}
    button {{ padding:12px 20px; border:0; border-radius:8px; cursor:pointer; }}
    .approve {{ background:#2563eb; color:white; }}
    .deny {{ background:#4b5563; color:white; }}
    .error {{ background:#7f1d1d; padding:12px; border-radius:8px; }}
  </style>
</head>
<body>
<main>
  <h1>授权 Gemini Spark 访问 GitHub MCP</h1>
  {error_html}
  <p>客户端：<code>{html.escape(payload['client_id'])}</code></p>
  <p>权限：{scopes}</p>
  <p>允许仓库：{repositories}</p>
  <p>回调地址：<code>{html.escape(payload['redirect_uri'])}</code></p>
  <form method="post" action="{html.escape(self.authorization_endpoint)}">
    <input type="hidden" name="request_id" value="{html.escape(request_id)}" />
    <label for="admin_password">个人授权密码</label>
    <input id="admin_password" name="admin_password" type="password" autocomplete="current-password" required />
    <div class="actions">
      <button class="approve" type="submit" name="decision" value="approve">允许</button>
      <button class="deny" type="submit" name="decision" value="deny" formnovalidate>拒绝</button>
    </div>
  </form>
</main>
</body>
</html>
""".strip()
        return HTMLResponse(body, status_code=status_code, headers=_html_security_headers())

    @staticmethod
    def _html_error(message: str, *, status_code: int) -> HTMLResponse:
        body = f"""
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>OAuth 错误</title></head>
<body><h1>OAuth 请求失败</h1><p>{html.escape(message)}</p></body></html>
""".strip()
        return HTMLResponse(body, status_code=status_code, headers=_html_security_headers())


async def _parse_form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > 65_536:
        return {}
    try:
        decoded = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return {}
    parsed = parse_qs(decoded, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def _oauth_json(payload: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _oauth_error(
    error: str,
    description: str,
    *,
    status_code: int = 400,
    authenticate: bool = False,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if authenticate:
        headers["WWW-Authenticate"] = 'Basic realm="oauth-token"'
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers=headers,
    )


def _html_security_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }


def _valid_pkce_verifier(value: str) -> bool:
    return 43 <= len(value) <= 128 and all(character in _PKCE_CHARS for character in value)


def _valid_pkce_challenge(value: str) -> bool:
    return len(value) == 43 and all(character.isalnum() or character in "-_" for character in value)


def _scope_list(raw: str) -> list[str]:
    return [part for part in raw.replace(",", " ").split() if part]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _int_bytes(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
