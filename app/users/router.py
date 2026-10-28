# =========================================================
# users/router.py - registration, login, email verification, profile
# =========================================================
#
# This is the front door. Everything behind it is scoped to `user_id` from the
# signed JWT, so a user can only ever see and act on their own cards, wallet
# and application.

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core import mail
from app.core.config import ENABLE_DEV_EMAIL_PREVIEW, JWT_EXPIRE_MINUTES
from app.core.ratelimit import client_key, login_limiter, register_limiter, verify_limiter
from app.core.security import create_access_token, current_user_guard
from app.journey.events import publish_journey_update
from app.users import service
from app.users.models import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResendVerificationRequest,
    TokenResponse,
    UserResponse,
    VerifyEmailRequest,
)

logger = logging.getLogger("users")
router = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=201, tags=["Authentication"])
async def register(payload: RegisterRequest, request: Request):
    """
    Create an account. Returns a signed access token so the user is signed in
    immediately - no second round trip to log in.
    """
    await register_limiter.check(client_key(request, "register"))
    if not payload.acceptedTerms:
        raise HTTPException(422, "You must accept the card terms to continue")

    user = await service.create_user(
        email=payload.email,
        password=payload.password,
        full_name=payload.fullName,
        phone=payload.phone,
    )
    token = await service.issue_token(user["user_id"], "email_verification")
    await mail.send_verification_email(user["email"], user.get("full_name", ""), token)

    await publish_journey_update(user["user_id"], "account_created")
    return TokenResponse(
        access_token=create_access_token({"sub": user["user_id"], "email": user["email"]}),
        expires_in=JWT_EXPIRE_MINUTES * 60,
        user=UserResponse(**service.public_user(user)),
    )


@router.post("/login", response_model=TokenResponse, tags=["Authentication"])
async def login(payload: LoginRequest, request: Request):
    """Sign in with email and password."""
    key = client_key(request, payload.email.lower().strip())
    await login_limiter.check(key)
    user = await service.authenticate(payload.email, payload.password)
    await login_limiter.reset(key)

    await publish_journey_update(user["user_id"], "logged_in")
    return TokenResponse(
        access_token=create_access_token({"sub": user["user_id"], "email": user["email"]}),
        expires_in=JWT_EXPIRE_MINUTES * 60,
        user=UserResponse(**service.public_user(user)),
    )


@router.post("/verify-email", response_model=UserResponse, tags=["Authentication"])
async def verify_email(payload: VerifyEmailRequest, request: Request):
    """Confirm an email address with the token from the verification link."""
    await verify_limiter.check(client_key(request, "verify"))
    user = await service.verify_email(payload.token)
    await publish_journey_update(user["user_id"], "email_verified")
    return UserResponse(**service.public_user(user))


@router.post("/resend-verification", tags=["Authentication"])
async def resend_verification(payload: ResendVerificationRequest, request: Request):
    """
    Send a new verification link. Always returns 200 so the response cannot be
    used to discover which email addresses have accounts.
    """
    await verify_limiter.check(client_key(request, "resend"))
    user = await service.get_user_by_email(payload.email)
    if user and not user.get("email_verified"):
        token = await service.issue_token(user["user_id"], "email_verification")
        await mail.send_verification_email(user["email"], user.get("full_name", ""), token)
    return {"message": "If that address needs verification, a new link is on its way."}


@router.get("/me", response_model=UserResponse, tags=["Authentication"])
async def me(user: dict = Depends(current_user_guard)):
    """The signed-in user's profile."""
    return UserResponse(**service.public_user(user))


@router.post("/change-password", tags=["Authentication"])
async def change_password(payload: ChangePasswordRequest,
                          user: dict = Depends(current_user_guard)):
    """Change the account password (current password required)."""
    await service.change_password(user["user_id"], payload.currentPassword, payload.newPassword)
    return {"message": "Password updated"}


@router.post("/logout", tags=["Authentication"])
async def logout(user: dict = Depends(current_user_guard)):
    """
    Client-side logout. Tokens are stateless, so the client discards its token.
    The `jti` in the token makes future server-side revocation possible.
    """
    return {"message": "Signed out"}


@router.get("/dev/verification-link", tags=["Development"])
async def dev_verification_link(email: str):
    """
    DEVELOPMENT ONLY - returns the email verification link so the flow can be tested
    without a mail provider. Hard-blocked in production (ENABLE_DEV_EMAIL_PREVIEW is
    forced off when APP_ENV is production).
    """
    if not ENABLE_DEV_EMAIL_PREVIEW:
        raise HTTPException(404)
    user = await service.get_user_by_email(email)
    if not user:
        raise HTTPException(404, "No account for that address")
    token = await service.issue_token(user["user_id"], "email_verification")
    return {
        "userId": user["user_id"],
        "emailVerified": bool(user.get("email_verified")),
        "verificationLink": mail.verification_link(token),
        "token": token,
    }
