from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config.settings import Settings, get_settings
from app.errors import ApiError, ErrorCode

_bearer = HTTPBearer(auto_error=False)


def _constant_time_member(candidate: str, valid_values: list[str]) -> bool:
    return any(hmac.compare_digest(candidate, value) for value in valid_values)


def validate_bearer_token(token: str | None, settings: Settings) -> str:
    if not token:
        raise ApiError(
            ErrorCode.AUTH_FAILED,
            "Missing bearer token.",
            status_code=401,
            suggestion="Send Authorization: Bearer <GATEWAY_ACTION_SECRET>.",
        )
    if not settings.secrets:
        raise ApiError(
            ErrorCode.AUTH_FAILED,
            "Server is missing GATEWAY_ACTION_SECRET configuration.",
            status_code=500,
            suggestion="Set GATEWAY_ACTION_SECRET. GPT_ACTION_SECRET remains supported as a legacy alias.",
        )
    candidate = token.strip()
    if not _constant_time_member(candidate, settings.secrets):
        raise ApiError(ErrorCode.AUTH_FAILED, "Invalid bearer token.", status_code=401)
    return candidate


async def require_auth(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        return validate_bearer_token(None, settings)
    return validate_bearer_token(credentials.credentials, settings)
