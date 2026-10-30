# =========================================================
# partners/service.py - resellers (tenants): API keys, customers, float
# =========================================================
#
# A reseller's customer maps onto the existing engine as owner_id = "<tenant_id>:<external_id>", so wallets,
# cards, cardholders and operations are isolated per reseller with no change to their code. The reseller pays
# for card issuance from its own float: ledger owner "float:<tenant_id>" (KES, same ledger as customer wallets).

import hashlib
import re
import secrets
import uuid
from datetime import datetime
from typing import Optional

from fastapi import Header, HTTPException, Request
from pymongo.errors import DuplicateKeyError

from decimal import Decimal, InvalidOperation

from app.core import config, database
from app.core.ratelimit import RateLimiter

KEY_PREFIX = "mk_live_"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")       # typed in admin commands: no spaces, lowercase
EXTERNAL_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")  # no ":" - it separates tenant and customer in owner_id

tenant_limiter = RateLimiter(limit=600, window_seconds=60)   # requests per tenant per minute


def hash_key(key: str) -> str:
    # API keys are 256-bit random values, so a plain SHA-256 is enough (no slow KDF needed).
    return hashlib.sha256(key.encode()).hexdigest()


def float_owner(tenant_id: str) -> str:
    return f"float:{tenant_id}"


def customer_owner(tenant_id: str, external_id: str) -> str:
    return f"{tenant_id}:{external_id}"


# ---------------------------------------------------------------- tenants (admin side)
async def create_tenant(name: str, slug: str, webhook_url: Optional[str] = None,
                        allowed_card_type_ids: Optional[list] = None, max_single_amount: Optional[str] = None):
    """Returns (tenant, plaintext_api_key). The key is shown once; only its hash is stored."""
    if not SLUG.match(slug or ""):
        raise ValueError("Slug must be 2-31 characters: lowercase letters, digits and '-', no spaces (e.g. commet-wallet)")
    check_webhook_url(webhook_url)
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    tenant = {
        "tenant_id": uuid.uuid4().hex[:16],
        "name": name,
        "slug": slug,
        "key_hash": hash_key(key),
        "key_hint": key[:12] + "...",
        "webhook_url": webhook_url,
        "signing_secret": "whsec_" + secrets.token_urlsafe(32),   # signs OUR webhooks to the reseller
        "allowed_card_type_ids": allowed_card_type_ids or [],      # empty = every card type we offer
        "max_single_amount": max_single_amount,                    # USD cap per card load; None = platform default
        "status": "active",
        "created_at": datetime.utcnow(),
    }
    try:
        await database.tenants_collection.insert_one(tenant)
    except DuplicateKeyError:
        raise ValueError(f"A tenant with slug '{slug}' already exists")
    return tenant, key


def margin_pct(tenant: dict) -> Decimal:
    """Our margin on this reseller's card operations: its own rate if set, else PARTNER_MARGIN_PCT."""
    value = tenant.get("margin_pct")
    return Decimal(str(value)) if value is not None else config.PARTNER_MARGIN_PCT


async def set_margin(slug: str, pct: Optional[str]) -> None:
    """Set (or with None, clear back to the default) the margin we charge this reseller, in percent."""
    if pct is not None:
        try:
            value = Decimal(pct)
        except InvalidOperation:
            raise ValueError("Margin must be a number of percent, e.g. 1.5")
        if not Decimal(0) <= value <= Decimal(20):
            raise ValueError("Margin must be between 0 and 20 percent")
        pct = str(value)
    result = await database.tenants_collection.update_one({"slug": slug}, {"$set": {"margin_pct": pct}})
    if not result.matched_count:
        raise ValueError(f"No tenant '{slug}'")


async def rotate_key(slug: str) -> str:
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    res = await database.tenants_collection.update_one(
        {"slug": slug}, {"$set": {"key_hash": hash_key(key), "key_hint": key[:12] + "..."}})
    if res.matched_count != 1:
        raise ValueError(f"No tenant '{slug}'")
    return key


def check_webhook_url(url: Optional[str]) -> None:
    """https only; plain http is allowed for a local address so a reseller can be tested on one machine."""
    if url and not (url.startswith("https://") or url.startswith(("http://localhost", "http://127.0.0.1"))):
        raise ValueError("The webhook URL must start with https:// (http://localhost is allowed for local testing)")


async def set_webhook(slug: str, url: Optional[str]) -> None:
    check_webhook_url(url)
    res = await database.tenants_collection.update_one({"slug": slug}, {"$set": {"webhook_url": url or None}})
    if res.matched_count != 1:
        raise ValueError(f"No tenant '{slug}'")


async def get_tenant_by_slug(slug: str) -> Optional[dict]:
    return await database.tenants_collection.find_one({"slug": slug})


async def authenticate(key: str) -> Optional[dict]:
    if not key or not key.startswith(KEY_PREFIX):
        return None
    return await database.tenants_collection.find_one({"key_hash": hash_key(key), "status": "active"})


async def tenant_guard(request: Request, x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> dict:
    """Authenticates a reseller's server. `X-API-Key: mk_live_...` (or `Authorization: Bearer mk_live_...`)."""
    key = x_api_key
    if not key:
        bearer = request.headers.get("authorization", "")
        key = bearer[7:] if bearer.lower().startswith("bearer ") else None
    tenant = await authenticate(key or "")
    if not tenant:
        raise HTTPException(401, "Invalid or missing API key")
    await tenant_limiter.check(tenant["tenant_id"])
    return tenant


def require_card_type(tenant: dict, card_type_id) -> None:
    allowed = tenant.get("allowed_card_type_ids") or []
    if allowed and card_type_id not in allowed:
        raise HTTPException(403, "This card type is not enabled for your account")


# ---------------------------------------------------------------- customers
async def create_customer(tenant: dict, external_id: str, full_name: str, email: str, phone: str):
    """Idempotent on (tenant, external_id). Returns (customer, created)."""
    if not EXTERNAL_ID.match(external_id or ""):
        raise HTTPException(422, "externalId must be 1-64 characters: letters, digits, '_', '-' or '.'")
    doc = {"tenant_id": tenant["tenant_id"], "external_id": external_id,
           "owner_id": customer_owner(tenant["tenant_id"], external_id),
           "full_name": full_name.strip(), "email": email.lower().strip(), "phone": phone.strip(),
           "created_at": datetime.utcnow()}
    try:
        await database.partner_customers_collection.insert_one(doc)
        return doc, True
    except DuplicateKeyError:
        return await database.partner_customers_collection.find_one(
            {"tenant_id": tenant["tenant_id"], "external_id": external_id}), False


async def get_customer(tenant: dict, external_id: str) -> dict:
    customer = await database.partner_customers_collection.find_one(
        {"tenant_id": tenant["tenant_id"], "external_id": external_id})
    if not customer:  # also the answer for another reseller's customer: nothing is enumerable
        raise HTTPException(404, "Customer not found")
    return customer


def public_customer(doc: dict) -> dict:
    return {"externalId": doc["external_id"], "fullName": doc["full_name"], "email": doc["email"],
            "phone": doc["phone"], "createdAt": doc["created_at"].isoformat()}
