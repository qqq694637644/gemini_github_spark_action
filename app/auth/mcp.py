from __future__ import annotations

import asyncio
import fnmatch
import hmac
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode

READ_SCOPE = "github:read"
WRITE_SCOPE = "github:write"
WORKFLOW_SCOPE = "github:workflow"
MERGE_SCOPE = "github:merge"
ALL_SCOPES = (READ_SCOPE, WRITE_SCOPE, WORKFLOW_SCOPE, MERGE_SCOPE)


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    actor: str
    client_id: str
    subject: str | None
    scopes: frozenset[str]
    repositories: tuple[str, ...]
    auth_mode: str

    def audit_metadata(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "client_id": self.client_id,
            "subject": self.subject,
            "auth_mode": self.auth_mode,
            "scopes": sorted(self.scopes),
        }


@dataclass(frozen=True, slots=True)
class McpAuthComponents:
    server_token_verifier: TokenVerifier | None
    auth_settings: AuthSettings | None
    static_token_verifier: TokenVerifier | None


class StaticBearerTokenVerifier(TokenVerifier):
    """Verify the optional local/shared-token mode without exposing token values."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tuple(tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate = token.strip()
        if not candidate or not any(hmac.compare_digest(candidate, valid) for valid in self._tokens):
            return None
        return AccessToken(
            token=candidate,
            client_id="gemini-spark-static",
            scopes=list(ALL_SCOPES),
            subject="static-client",
            claims={"auth_mode": "static_bearer", "github_repositories": ["*"]},
        )


class OAuthTokenVerifier(TokenVerifier):
    """Validate JWT access tokens or RFC 7662 introspection responses from an external IdP."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=settings.mcp_oauth_http_timeout_seconds,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._cache_lock = asyncio.Lock()
        self._metadata: tuple[float, dict[str, Any]] | None = None
        self._jwks: tuple[float, dict[str, Any]] | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate = token.strip()
        if not candidate:
            return None
        try:
            if self.settings.mcp_oauth_introspection_url:
                claims = await self._introspect(candidate)
            else:
                claims = await self._verify_jwt(candidate)
        except (httpx.HTTPError, jwt.PyJWTError, KeyError, TypeError, ValueError):
            return None
        if not claims:
            return None
        return self._access_token(candidate, claims)

    async def _introspect(self, token: str) -> dict[str, Any] | None:
        auth: httpx.BasicAuth | None = None
        client_id = self.settings.mcp_oauth_introspection_client_id.strip()
        client_secret = self.settings.mcp_oauth_introspection_client_secret
        if client_id:
            auth = httpx.BasicAuth(client_id, client_secret)
        request_data = {"token": token, "token_type_hint": "access_token"}
        if auth is None:
            response = await self._client.post(
                self.settings.mcp_oauth_introspection_url,
                data=request_data,
                headers={"Accept": "application/json"},
            )
        else:
            response = await self._client.post(
                self.settings.mcp_oauth_introspection_url,
                data=request_data,
                auth=auth,
                headers={"Accept": "application/json"},
            )
        response.raise_for_status()
        claims = response.json()
        if not isinstance(claims, dict) or claims.get("active") is not True:
            return None
        if not self._issuer_matches(claims.get("iss")):
            return None
        if not _audience_matches(claims.get("aud"), self.settings.oauth_audience):
            return None
        expires_at = claims.get("exp")
        if expires_at is not None and int(expires_at) <= int(time.time()) - self.settings.mcp_oauth_clock_skew_seconds:
            return None
        return claims

    async def _verify_jwt(self, token: str) -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        algorithm = str(header.get("alg") or "")
        if algorithm not in self.settings.oauth_algorithm_list:
            raise ValueError("JWT algorithm is not allowed")
        jwks = await self._get_jwks()
        key_data = _select_jwk(jwks, kid=header.get("kid"), algorithm=algorithm)
        signing_key = jwt.PyJWK.from_dict(key_data, algorithm=algorithm).key
        claims = jwt.decode(
            token,
            key=signing_key,
            algorithms=self.settings.oauth_algorithm_list,
            audience=self.settings.oauth_audience,
            issuer=self.settings.mcp_oauth_issuer_url,
            leeway=self.settings.mcp_oauth_clock_skew_seconds,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
        if not isinstance(claims, dict):
            raise ValueError("JWT claims must be an object")
        return claims

    async def _get_jwks(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._jwks and self._jwks[0] > now:
            return self._jwks[1]
        async with self._cache_lock:
            now = time.monotonic()
            if self._jwks and self._jwks[0] > now:
                return self._jwks[1]
            jwks_url = self.settings.mcp_oauth_jwks_url.strip()
            if not jwks_url:
                metadata = await self._get_authorization_server_metadata()
                jwks_url = str(metadata.get("jwks_uri") or "").strip()
            if not jwks_url:
                raise ValueError("Authorization server metadata does not provide jwks_uri")
            response = await self._client.get(jwks_url, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
                raise ValueError("JWKS response is invalid")
            self._jwks = (now + self.settings.mcp_oauth_jwks_cache_seconds, payload)
            return payload

    async def _get_authorization_server_metadata(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._metadata and self._metadata[0] > now:
            return self._metadata[1]
        failures: list[str] = []
        for url in _authorization_server_metadata_urls(self.settings.mcp_oauth_issuer_url):
            try:
                response = await self._client.get(url, headers={"Accept": "application/json"})
                if response.status_code != 200:
                    failures.append(f"{url}: HTTP {response.status_code}")
                    continue
                payload = response.json()
                if not isinstance(payload, dict):
                    failures.append(f"{url}: response is not an object")
                    continue
                if str(payload.get("issuer") or "").rstrip("/") != self.settings.mcp_oauth_issuer_url.rstrip("/"):
                    failures.append(f"{url}: issuer mismatch")
                    continue
                self._metadata = (now + self.settings.mcp_oauth_jwks_cache_seconds, payload)
                return payload
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(f"{url}: {type(exc).__name__}")
        raise ValueError(f"Authorization server discovery failed: {failures}")

    def _issuer_matches(self, issuer: Any) -> bool:
        return issuer is None or str(issuer).rstrip("/") == self.settings.mcp_oauth_issuer_url.rstrip("/")

    def _access_token(self, token: str, claims: dict[str, Any]) -> AccessToken:
        client_id = str(claims.get("client_id") or claims.get("azp") or claims.get("cid") or "unknown-client")
        subject_value = claims.get("sub")
        subject = str(subject_value) if subject_value is not None else None
        scopes = sorted(_scope_values(claims))
        expires_at = int(claims["exp"]) if claims.get("exp") is not None else None
        resource = claims.get("resource")
        if isinstance(resource, list):
            resource = self.settings.oauth_audience if self.settings.oauth_audience in resource else None
        elif resource is not None:
            resource = str(resource)
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            resource=resource or self.settings.oauth_audience,
            subject=subject,
            claims=claims,
        )


def build_mcp_auth(
    settings: Settings,
    *,
    token_verifier: TokenVerifier | None = None,
) -> McpAuthComponents:
    if settings.mcp_auth_mode == "static_bearer":
        return McpAuthComponents(
            server_token_verifier=None,
            auth_settings=None,
            static_token_verifier=token_verifier or StaticBearerTokenVerifier(settings.static_bearer_tokens),
        )
    if not settings.mcp_oauth_issuer_url.strip():
        raise ValueError("MCP_OAUTH_ISSUER_URL is required when MCP_AUTH_MODE=oauth")
    verifier = token_verifier or OAuthTokenVerifier(settings)
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(settings.mcp_oauth_issuer_url),
        resource_server_url=AnyHttpUrl(settings.mcp_resource_url),
        # Scope enforcement is tool-specific. The resource metadata endpoint
        # advertises all supported scopes while authorize_repository applies
        # the configured baseline plus the selected tool's scopes.
        required_scopes=[],
    )
    return McpAuthComponents(
        server_token_verifier=verifier,
        auth_settings=auth,
        static_token_verifier=None,
    )


def current_identity(settings: Settings) -> AuthIdentity:
    token = get_access_token()
    if token is None:
        raise ApiError(
            ErrorCode.AUTH_FAILED,
            "No authenticated MCP identity is available for this tool call.",
            status_code=401,
        )
    claims = token.claims or {}
    repositories = _repository_values(claims.get(settings.mcp_oauth_repo_claim))
    auth_mode = str(claims.get("auth_mode") or settings.mcp_auth_mode)
    actor = token.subject or token.client_id
    return AuthIdentity(
        actor=actor,
        client_id=token.client_id,
        subject=token.subject,
        scopes=frozenset(token.scopes),
        repositories=repositories,
        auth_mode=auth_mode,
    )


def authorize_repository(settings: Settings, owner: str, repo: str, required_scopes: set[str]) -> AuthIdentity:
    identity = current_identity(settings)
    effective_scopes = set(required_scopes)
    if settings.mcp_auth_mode == "oauth":
        effective_scopes.update(settings.oauth_required_scope_list)
    missing = sorted(effective_scopes - identity.scopes)
    if missing:
        raise ApiError(
            ErrorCode.AUTH_FAILED,
            "The access token does not grant the scopes required by this MCP tool.",
            status_code=403,
            suggestion="Authorize the Connected App again with the required scopes.",
            details={"required_scopes": sorted(effective_scopes), "missing_scopes": missing},
        )
    if settings.mcp_auth_mode == "oauth" and settings.mcp_oauth_require_repo_claim:
        full_name = f"{owner}/{repo}".lower()
        if not identity.repositories:
            raise ApiError(
                ErrorCode.REPO_NOT_ALLOWED,
                "The OAuth token does not include a repository authorization claim.",
                status_code=403,
                details={"claim": settings.mcp_oauth_repo_claim, "repo": full_name},
            )
        if not any(fnmatch.fnmatchcase(full_name, pattern.lower()) for pattern in identity.repositories):
            raise ApiError(
                ErrorCode.REPO_NOT_ALLOWED,
                f"The authenticated user is not authorized for repository {owner}/{repo}.",
                status_code=403,
                details={"repo": full_name, "claim": settings.mcp_oauth_repo_claim},
            )
    return identity


def current_actor() -> str | None:
    token = get_access_token()
    if token is None:
        return None
    return token.subject or token.client_id


def _scope_values(claims: dict[str, Any]) -> set[str]:
    raw = claims.get("scope", claims.get("scp", []))
    if isinstance(raw, str):
        return {item for item in raw.replace(",", " ").split() if item}
    if isinstance(raw, (list, tuple, set, frozenset)):
        return {str(item).strip() for item in raw if str(item).strip()}
    return set()


def _repository_values(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        values = raw.replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = [str(item) for item in raw]
    else:
        return ()
    return tuple(dict.fromkeys(item.strip() for item in values if item.strip()))


def _audience_matches(raw: Any, expected: str) -> bool:
    if raw is None:
        return False
    if isinstance(raw, str):
        return hmac.compare_digest(raw, expected)
    if isinstance(raw, (list, tuple, set, frozenset)):
        return any(hmac.compare_digest(str(item), expected) for item in raw)
    return False


def _select_jwk(jwks: dict[str, Any], *, kid: Any, algorithm: str) -> dict[str, Any]:
    keys = [item for item in jwks.get("keys", []) if isinstance(item, dict)]
    if kid is not None:
        keys = [item for item in keys if item.get("kid") == kid]
    algorithm_keys = [item for item in keys if item.get("alg") in {None, algorithm}]
    if len(algorithm_keys) != 1:
        raise ValueError("Unable to select exactly one signing key")
    return algorithm_keys[0]


def _authorization_server_metadata_urls(issuer: str) -> list[str]:
    parsed = urlsplit(issuer.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("OAuth issuer must be an absolute HTTP(S) URL")
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    issuer_path = parsed.path.rstrip("/")
    if issuer_path:
        return [
            f"{origin}/.well-known/oauth-authorization-server{issuer_path}",
            f"{origin}/.well-known/openid-configuration{issuer_path}",
            f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        ]
    return [
        f"{origin}/.well-known/oauth-authorization-server",
        f"{origin}/.well-known/openid-configuration",
    ]
