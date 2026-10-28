# =========================================================
# users/models.py - request and response schemas for authentication
# =========================================================

from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


def _strength_ok(value: str) -> str:
    """Minimum viable password policy. Longer beats complex; we only enforce the basics."""
    if len(value) < 10:
        raise ValueError("Password must be at least 10 characters")
    if value.lower() == value or value.upper() == value:
        raise ValueError("Password must mix upper and lower case letters")
    if not any(c.isdigit() for c in value):
        raise ValueError("Password must contain at least one number")
    if not any(c in "!@#$%^&*()-_=+[]{};:,.?/" for c in value):
        raise ValueError("Password must contain at least one special character")
    return value


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=10, max_length=72)
    fullName: str = Field(..., min_length=2, max_length=100)
    phone: str = Field(..., min_length=9, max_length=20, description="Mobile number, e.g. 0712345678")
    acceptedTerms: bool = Field(..., description="Must be true - the user accepted the card terms")

    @field_validator("password")
    @classmethod
    def _check_password(cls, v):
        return _strength_ok(v)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=72)


class ChangePasswordRequest(BaseModel):
    currentPassword: str = Field(..., min_length=1, max_length=72)
    newPassword: str = Field(..., min_length=10, max_length=72)

    @field_validator("newPassword")
    @classmethod
    def _check_password(cls, v):
        return _strength_ok(v)


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class VerifyEmailRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=128)


class UserResponse(BaseModel):
    userId: str
    email: EmailStr
    fullName: str
    phone: str
    emailVerified: bool
    status: str
    createdAt: Optional[str] = None
    lastLoginAt: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse
