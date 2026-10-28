# =========================================================
# security.py - JWT authentication layer
# =========================================================

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import JWT_ALGORITHM, JWT_EXPIRE_MINUTES, JWT_SECRET

ALLOWED_ALGORITHMS = {"HS256", "HS384", "HS512"}

security = HTTPBearer(auto_error=True)
# For Server-Sent Events: the browser's EventSource cannot set an Authorization
# header, so the token may arrive as a query parameter instead. auto_error=False
# lets the SSE guard fall back to the query string instead of rejecting first.
optional_security = HTTPBearer(auto_error=False)


def validate_jwt_config():
    """Called at startup: refuse to run with a missing/weak signing config."""
    if len(JWT_SECRET) < 32:
        raise RuntimeError("JWT_SECRET must be set to at least 32 characters")
    if JWT_ALGORITHM not in ALLOWED_ALGORITHMS:
        raise RuntimeError(f"JWT_ALGORITHM must be one of {sorted(ALLOWED_ALGORITHMS)}")


def create_access_token(data: dict, expires_delta: Optional[int] = None) -> str:
    """
    Mint a JWT. `exp` comes from JWT_EXPIRE_MINUTES unless overridden.
    A unique `jti` is embedded so a token can be singled out later (revocation,
    audit correlation) without keeping a server-side session store.
    """
    payload = data.copy()
    if expires_delta is None:
        expires_delta = JWT_EXPIRE_MINUTES * 60
    payload["exp"] = datetime.now(timezone.utc) + timedelta(seconds=expires_delta)
    payload["iat"] = datetime.now(timezone.utc)
    payload["jti"] = str(uuid.uuid4())
    payload.setdefault("typ", "access")
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str, expected_type: Optional[str] = "access"):
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    if expected_type and claims.get("typ") != expected_type:
        raise HTTPException(status_code=401, detail="Invalid token type")
    return claims


def auth_guard(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """
    Returns the token claims plus a stable `id` (the JWT subject). Every route and ownership check
    uses `id`; without a subject the request is rejected instead of falling back to a shared user.
    """
    claims = verify_token(credentials.credentials)
    subject = claims.get("sub")
    if not subject:
        raise HTTPException(status_code=401, detail="Token has no subject")
    return {**claims, "id": str(subject)}


async def current_user_guard(claims: dict = Depends(auth_guard)) -> dict:
    """
    The authenticated user, loaded from the database. Confirms the account still exists
    and is not suspended, so a token issued before a suspension stops working.
    """
    from app.users import service as user_service

    user = await user_service.get_user(claims["id"])
    if not user:
        raise HTTPException(status_code=401, detail="Account no longer exists")
    if user.get("status") == user_service.STATUS_SUSPENDED:
        raise HTTPException(status_code=403, detail="This account has been suspended")
    return user


async def verified_email_guard(user: dict = Depends(current_user_guard)) -> dict:
    """
    Requires a confirmed email address. Steps that send documents to Wasabi or start an
    M-Pesa prompt need a contactable user, so they sit behind this guard.
    """
    if not user.get("email_verified"):
        raise HTTPException(
            status_code=403,
            detail="Verify your email address before continuing. Check your inbox for the verification link.",
        )
    return user


async def sse_auth_guard(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
) -> dict:
    """
    Authentication for Server-Sent Events.

    The browser's EventSource API cannot set an Authorization header, so the token
    may also arrive as a ?token= query parameter. The header is still preferred and
    checked first. This is scoped to the SSE endpoint only - query strings end up in
    access logs, so never use this pattern for ordinary calls.
    """
    token = credentials.credentials if credentials else None
    if not token:
        token = request.query_params.get('token')
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    claims = verify_token(token)
    subject = claims.get("sub")
    if not subject:
        raise HTTPException(status_code=401, detail="Token has no subject")

    from app.users import service as user_service

    user = await user_service.get_user(str(subject))
    if not user:
        raise HTTPException(status_code=401, detail="Account no longer exists")
    if user.get("status") == user_service.STATUS_SUSPENDED:
        raise HTTPException(status_code=403, detail="This account has been suspended")
    return {**claims, "id": str(subject), "user_id": str(subject)}
