# =========================================================
# users/service.py - account lifecycle, credentials, email verification
# =========================================================

import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException

from app.core import config, database
from app.core.passwords import dummy_verify, hash_password, verify_password

logger = logging.getLogger("users")

VERIFICATION_TTL = timedelta(hours=24)
RESET_TTL = timedelta(minutes=30)

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending_verification"
STATUS_SUSPENDED = "suspended"


# =========================================================
# SERIALISATION
# =========================================================
def public_user(doc: dict) -> dict:
    """Never expose the password hash or internal fields."""
    return {
        "userId": doc["user_id"],
        "email": doc["email"],
        "fullName": doc.get("full_name", ""),
        "phone": doc.get("phone", ""),
        "emailVerified": bool(doc.get("email_verified")),
        "status": doc.get("status", STATUS_ACTIVE),
        "createdAt": doc.get("created_at").isoformat() if doc.get("created_at") else None,
        "lastLoginAt": doc.get("last_login_at").isoformat() if doc.get("last_login_at") else None,
    }


# =========================================================
# REGISTRATION
# =========================================================
async def create_user(email: str, password: str, full_name: str, phone: str) -> dict:
    email = email.lower().strip()
    existing = await database.users_collection.find_one({"email": email})
    if existing:
        # Do not confirm whether an account exists beyond the obvious: registration
        # legitimately has to say "already registered".
        raise HTTPException(409, "An account with this email already exists. Try signing in instead.")

    doc = {
        "user_id": str(uuid.uuid4()),
        "email": email,
        "password_hash": hash_password(password),
        "full_name": full_name.strip(),
        "phone": phone.strip(),
        "email_verified": config.DEV_AUTO_VERIFY_EMAIL,
        "status": STATUS_ACTIVE if config.DEV_AUTO_VERIFY_EMAIL else STATUS_PENDING,
        "failed_logins": 0,
        "locked_until": None,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "last_login_at": None,
    }
    await database.users_collection.insert_one(doc)
    logger.info("user registered: %s", doc["user_id"])
    return doc


# =========================================================
# AUTHENTICATION
# =========================================================
async def authenticate(email: str, password: str) -> dict:
    """
    Verify credentials. Always the same error for "no such user" and "wrong password"
    so the response cannot be used to enumerate accounts.
    """
    email = email.lower().strip()
    user = await database.users_collection.find_one({"email": email})
    if not user:
        dummy_verify()  # equalize timing
        raise HTTPException(401, "Invalid email or password")

    # Locked out by repeated failures?
    locked_until = user.get("locked_until")
    if locked_until and locked_until > datetime.utcnow():
        retry_after = int((locked_until - datetime.utcnow()).total_seconds()) + 1
        raise HTTPException(423, f"Account temporarily locked. Try again in {retry_after}s.",
                            headers={"Retry-After": str(retry_after)})

    if not verify_password(password, user.get("password_hash", "")):
        await _register_failed_login(user)
        raise HTTPException(401, "Invalid email or password")

    if user.get("status") == STATUS_SUSPENDED:
        raise HTTPException(403, "This account has been suspended. Contact support.")

    now = datetime.utcnow()
    update = {"failed_logins": 0, "locked_until": None, "last_login_at": now, "updated_at": now}
    if config.DEV_AUTO_VERIFY_EMAIL and not user.get("email_verified"):
        update.update({"email_verified": True, "status": STATUS_ACTIVE})  # also fixes accounts made earlier
        user.update({"email_verified": True, "status": STATUS_ACTIVE})
    await database.users_collection.update_one({"user_id": user["user_id"]}, {"$set": update})
    user["last_login_at"] = now
    user["failed_logins"] = 0
    return user


async def _register_failed_login(user: dict) -> None:
    """Count the failure and lock the account after too many consecutive misses."""
    failures = int(user.get("failed_logins", 0)) + 1
    update = {"failed_logins": failures, "updated_at": datetime.utcnow()}
    if failures >= 5:
        update["locked_until"] = datetime.utcnow() + timedelta(minutes=15)
        logger.warning("user %s locked after %s failed logins", user["user_id"], failures)
    await database.users_collection.update_one({"user_id": user["user_id"]}, {"$set": update})


async def get_user(user_id: str) -> Optional[dict]:
    return await database.users_collection.find_one({"user_id": user_id})


async def get_user_by_email(email: str) -> Optional[dict]:
    return await database.users_collection.find_one({"email": email.lower().strip()})


async def change_password(user_id: str, current_password: str, new_password: str) -> None:
    user = await get_user(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if not verify_password(current_password, user.get("password_hash", "")):
        raise HTTPException(401, "Current password is incorrect")
    await database.users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"password_hash": hash_password(new_password),
                  "updated_at": datetime.utcnow()}},
    )


# =========================================================
# EMAIL VERIFICATION
# =========================================================
async def issue_token(user_id: str, purpose: str) -> str:
    """
    Create a single-use, hashed-at-rest token. The plaintext is returned to the
    caller (which emails it); only its SHA-256 digest is stored, so a database leak
    does not hand out working tokens.
    """
    import hashlib

    token = secrets.token_urlsafe(48)
    digest = hashlib.sha256(token.encode()).hexdigest()
    await database.verification_tokens_collection.update_many(
        {"user_id": user_id, "purpose": purpose, "used_at": None},
        {"$set": {"used_at": datetime.utcnow()}},
    )
    await database.verification_tokens_collection.insert_one({
        "token_hash": digest,
        "user_id": user_id,
        "purpose": purpose,
        "expires_at": datetime.utcnow() + VERIFICATION_TTL,
        "used_at": None,
        "created_at": datetime.utcnow(),
    })
    return token


async def consume_token(token: str, purpose: str) -> Optional[dict]:
    """Validate and burn a token. Returns the owning user, or None."""
    import hashlib

    digest = hashlib.sha256(token.encode()).hexdigest()
    record = await database.verification_tokens_collection.find_one(
        {"token_hash": digest, "purpose": purpose, "used_at": None})
    if not record:
        return None
    if record["expires_at"] < datetime.utcnow():
        return None
    await database.verification_tokens_collection.update_one(
        {"token_hash": digest}, {"$set": {"used_at": datetime.utcnow()}})
    return await get_user(record["user_id"])


async def verify_email(token: str) -> dict:
    user = await consume_token(token, "email_verification")
    if not user:
        raise HTTPException(400, "This verification link is invalid or has expired")
    await database.users_collection.update_one(
        {"user_id": user["user_id"]},
        {"$set": {"email_verified": True, "status": STATUS_ACTIVE,
                  "updated_at": datetime.utcnow()}},
    )
    user["email_verified"] = True
    user["status"] = STATUS_ACTIVE
    logger.info("email verified for user %s", user["user_id"])
    return user
