# =========================================================
# security.py - JWT authentication layer
# =========================================================

from datetime import datetime, timedelta, timezone  # Added timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import JWT_ALGORITHM, JWT_SECRET

# =========================================================
# HTTP Bearer Scheme
# =========================================================

security = HTTPBearer()


# =========================================================
# CREATE JWT TOKEN
# =========================================================
def create_access_token(data: dict, expires_delta: int = 3600):
    payload = data.copy()

    # Fixed: Use timezone-aware UTC datetime
    payload["exp"] = datetime.now(timezone.utc) + timedelta(
        seconds=expires_delta
    )

    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# =========================================================
# VERIFY JWT TOKEN
# =========================================================
def verify_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")

    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# =========================================================
# AUTH GUARD
# =========================================================
def auth_guard(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    return verify_token(token)
