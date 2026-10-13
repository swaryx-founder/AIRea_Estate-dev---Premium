"""API authentication — Bearer / X-API-Key against API_AUTH_TOKEN."""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import config

# Paths that stay open (probes + dashboard shell). All other /api/* require the token
# when API_AUTH_TOKEN is set.
_PUBLIC_PATHS = frozenset({"/", "/api/health"})


def api_auth_token() -> str:
    return (config.get_env("API_AUTH_TOKEN") or "").strip()


def auth_required() -> bool:
    return bool(api_auth_token())


def extract_request_token(request: Request) -> Optional[str]:
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    key = (request.headers.get("X-API-Key") or "").strip()
    if key:
        return key
    # Browser <a href> / recording redirects cannot set headers.
    q = (request.query_params.get("api_key") or "").strip()
    return q or None


def tokens_match(provided: Optional[str], expected: str) -> bool:
    if not provided or not expected:
        return False
    try:
        return secrets.compare_digest(provided, expected)
    except (TypeError, ValueError):
        return False


class ApiAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path.rstrip("/") or "/"
        # Normalize: /api/health/ → treat as /api/health for public check
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        if path in _PUBLIC_PATHS or not path.startswith("/api"):
            return await call_next(request)

        expected = api_auth_token()
        if not expected:
            # Dev convenience: open API when token unset.
            return await call_next(request)

        provided = extract_request_token(request)
        if not tokens_match(provided, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized — set Authorization: Bearer <API_AUTH_TOKEN> or X-API-Key"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


def require_api_auth(request: Request) -> None:
    """Optional Depends()-style check for routes that always need auth when configured."""
    expected = api_auth_token()
    if not expected:
        return
    if not tokens_match(extract_request_token(request), expected):
        raise HTTPException(401, "Unauthorized — set Authorization: Bearer <API_AUTH_TOKEN> or X-API-Key")
