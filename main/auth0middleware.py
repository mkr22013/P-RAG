"""
Auth0 JWT Validation Middleware
--------------------------------
Validates every incoming request against Auth0 before it reaches any endpoint.

Flow:
    Request → AuthMiddleware → validates Bearer token → passes to endpoint
                            → missing/invalid token  → 401 Unauthorized

Configuration (environment variables):
    AUTH0_DOMAIN    — your Auth0 tenant, e.g. "your-tenant.auth0.com"
    AUTH0_AUDIENCE  — the API audience registered in Auth0 for this service
    AUTH0_ALGORITHMS — comma-separated, default "RS256"
    AUTH_EXCLUDED_PATHS — comma-separated paths to skip auth, default "/health"

Auth0 signs tokens with RS256 using a private key.
We fetch the public keys (JWKS) from Auth0 and cache them to validate signatures.
JWKS is refreshed automatically when a key ID is not found (key rotation).
"""

import os
from config import settings
import logging
import time
from typing import Optional

import httpx
import jwt
from jwt import PyJWKClient, ExpiredSignatureError, InvalidTokenError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
AUTH0_DOMAIN = settings.AUTH0_DOMAIN
AUTH0_AUDIENCE = settings.AUTH0_AUDIENCE
AUTH0_ALGORITHMS = settings.AUTH0_ALGORITHMS.split(",")
AUTH_EXCLUDED_PATHS = set(settings.AUTH_EXCLUDED_PATHS.split(","))

# JWKS client — caches public keys from Auth0, handles key rotation automatically
_jwks_client: Optional[PyJWKClient] = None


def _get_jwks_client() -> PyJWKClient:
    """
    Returns a cached PyJWKClient pointed at Auth0's JWKS endpoint.
    PyJWKClient handles caching and automatic refresh on key rotation.
    """
    global _jwks_client
    if _jwks_client is None:
        if not AUTH0_DOMAIN:
            raise RuntimeError(
                "AUTH0_DOMAIN environment variable is not set. "
                "Set it to your Auth0 tenant domain, e.g. 'your-tenant.auth0.com'"
            )
        jwks_url = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
        _jwks_client = PyJWKClient(jwks_url, cache_keys=True)
        logger.info("[auth] JWKS client initialised: %s", jwks_url)
    return _jwks_client


def _validate_token(token: str) -> dict:
    """
    Validates a JWT token against Auth0 public keys.

    Checks:
    - Signature valid (RS256 against Auth0 JWKS)
    - Token not expired
    - Audience matches AUTH0_AUDIENCE
    - Issuer matches AUTH0_DOMAIN

    Returns the decoded token payload on success.
    Raises jwt.InvalidTokenError on any validation failure.
    """
    client = _get_jwks_client()
    signing_key = client.get_signing_key_from_jwt(token)

    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=AUTH0_ALGORITHMS,
        audience=AUTH0_AUDIENCE,
        issuer=f"https://{AUTH0_DOMAIN}/",
        options={"verify_exp": True},
    )
    return payload


class Auth0Middleware(BaseHTTPMiddleware):
    """
    Starlette middleware that validates Auth0 JWT tokens on every request.

    Excluded paths (e.g. /health) bypass validation.
    All other paths require a valid Bearer token in the Authorization header.

    Usage in main.py:
        from auth_middleware import Auth0Middleware
        app.add_middleware(Auth0Middleware)
    """

    async def dispatch(self, request: Request, call_next):
        # Skip auth for excluded paths (health checks, etc.)
        if request.url.path in AUTH_EXCLUDED_PATHS:
            return await call_next(request)

        # Skip auth if Auth0 is not configured (local development)
        if not AUTH0_DOMAIN or not AUTH0_AUDIENCE:
            logger.warning(
                "[auth] AUTH0_DOMAIN or AUTH0_AUDIENCE not set — "
                "skipping token validation (development mode)"
            )
            return await call_next(request)

        # Extract Bearer token from Authorization header
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning(
                "[auth] Missing or malformed Authorization header for %s",
                request.url.path,
            )
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "Missing or malformed Authorization header. "
                    "Expected: Authorization: Bearer <token>",
                },
            )

        token = auth_header[len("Bearer ") :]

        try:
            start = time.monotonic()
            payload = _validate_token(token)
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "[auth] Token valid for sub=%s duration=%dms path=%s",
                payload.get("sub", "unknown"),
                duration_ms,
                request.url.path,
            )
            # Attach decoded payload to request state
            # Endpoints can access it via: request.state.token_payload
            request.state.token_payload = payload
            return await call_next(request)

        except ExpiredSignatureError:
            logger.warning("[auth] Expired token for path=%s", request.url.path)
            return JSONResponse(
                status_code=401,
                content={
                    "error": "token_expired",
                    "message": "Access token has expired. Please log in again.",
                },
            )

        except InvalidTokenError as exc:
            logger.warning(
                "[auth] Invalid token for path=%s: %s", request.url.path, exc
            )
            return JSONResponse(
                status_code=401,
                content={
                    "error": "invalid_token",
                    "message": "Token validation failed.",
                },
            )

        except Exception as exc:
            logger.error("[auth] Unexpected error during token validation: %s", exc)
            return JSONResponse(
                status_code=500,
                content={
                    "error": "auth_error",
                    "message": "Authentication service error.",
                },
            )
