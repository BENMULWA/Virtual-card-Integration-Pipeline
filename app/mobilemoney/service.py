# =========================================================
# mobilemoney/service.py - wallet top-up (collection) and withdrawal (payout) through the gateway
# =========================================================
#
# Every operation is recorded BEFORE the gateway is called, keyed by a deterministic externalId, so a
# retried request finds the record instead of sending a second prompt or payout. Wallet effects go through
# the ledger (unique entry_key), so a replayed callback credits / captures / releases at most once.
#   top-up   : SUCCESS -> credit the wallet.            FAILED -> nothing moves.
#   withdraw : hold the funds first; SUCCESS -> capture, FAILED -> release. An unknown outcome KEEPS the hold.

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.core import config, database
from app.mobilemoney import client as gw
from app.mpesa.topup import normalize_phone
from app.wallet import ledger

logger = logging.getLogger("mobilemoney")

PENDING, SUCCESS, FAILED, REVIEW = "PENDING", "SUCCESS", "FAILED", "REVIEW"
MIN_AGE = timedelta(minutes=2)       # give the normal callback a chance before polling
STUCK_AFTER = timedelta(minutes=30)  # no gateway reference this long after we asked: a human must look


def _ref(prefix: str, owner_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{owner_id}:{prefix}:{idempotency_key}".encode()).hexdigest()[:24].upper()
    return f"{prefix}{digest}"


def _mask(phone: str) -> str:
    return phone[:5] + "****" + phone[-3:]


def _provider(provider: str) -> str:
    provider = (provider or "").lower()
    if provider not in gw.PROVIDERS:
        raise HTTPException(422, f"provider must be one of {sorted(gw.PROVIDERS)}")
    return provider


def _require_gateway():
    if not gw.configured():
        raise HTTPException(503, "Mobile money gateway is not configured")


def public_payout(doc: dict) -> dict:
    return {"reference": doc["external_id"], "amount_kes": doc["amount_kes"], "status": doc["status"],
            "provider": doc["provider"], "phone_masked": doc["phone_masked"], "receipt": doc.get("receipt"),
            "result_desc": doc.get("result_desc"), "created_at": doc["created_at"]}


# =========================================================
# TOP-UP (collection)
# =========================================================
async def initiate_topup(owner_id: str, idempotency_key: str, phone: str, amount_kes: int, provider: str) -> dict:
    from app.mpesa import topup  # lazy: topup.settle also calls back into this module

    _require_gateway()
    provider = _provider(provider)
    if not (config.AIRTEL_MIN_TOPUP_KES <= amount_kes <= config.AIRTEL_MAX_TOPUP_KES):
        raise HTTPException(422, f"Amount must be between {config.AIRTEL_MIN_TOPUP_KES} and "
                                 f"{config.AIRTEL_MAX_TOPUP_KES} KES")
    phone = normalize_phone(phone)
    external_id = _ref("TUP", owner_id, idempotency_key)

    doc = {"idem_key": f"{owner_id}:{idempotency_key}", "owner_id": owner_id, "channel": "gateway",
           "provider": provider, "checkout_request_id": external_id, "external_id": external_id,
           "phone_masked": _mask(phone), "amount_kes": amount_kes, "status": PENDING,
           "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
    try:
        await database.topups_collection.insert_one(doc)
    except DuplicateKeyError:  # replayed request: never send a second prompt
        return topup.public(await database.topups_collection.find_one({"checkout_request_id": external_id}))

    try:
        response = await gw.initiate_collection(phone, amount_kes, provider, external_id)
    except gw.GatewayError as exc:
        await _set(database.topups_collection, external_id, status=FAILED, result_desc=str(exc))
        raise HTTPException(422, f"Payment provider rejected the request: {exc}")
    except gw.GatewayUnknownOutcome:
        raise HTTPException(502, "Could not reach the payment provider. Check your phone before retrying; "
                                 "do not pay twice.")
    secure_id = response.get("secureId") or response.get("transactionId")
    await _set(database.topups_collection, external_id, secure_id=secure_id)
    doc["secure_id"] = secure_id
    return topup.public(doc)


async def _set(collection, external_id: str, **fields):
    key = "checkout_request_id" if collection is database.topups_collection else "external_id"
    await collection.update_one({key: external_id}, {"$set": {**fields, "updated_at": datetime.utcnow()}})


async def apply_topup_result(doc: dict, result: dict) -> dict:
    """Credit once, on a confirmed success whose amount is the amount we asked for."""
    ref = doc["external_id"]
    if doc["status"] not in (PENDING, REVIEW) or result["state"] == "PENDING":
        return doc
    if doc.get("secure_id") and result.get("secure_id") and doc["secure_id"] != result["secure_id"]:
        logger.error("topup %s: gateway reference mismatch - ignored", ref)
        return doc
    if result["state"] == "SUCCESS":
        if result["amount"] is not None and int(float(result["amount"])) != doc["amount_kes"]:
            logger.error("topup %s amount mismatch: requested %s, gateway %s - holding for review",
                         ref, doc["amount_kes"], result["amount"])
            await _set(database.topups_collection, ref, status=REVIEW, result_desc="amount mismatch")
        else:
            await ledger.credit(doc["owner_id"], doc["amount_kes"] * 100, ref=ref, kind="topup",
                                meta={"channel": "gateway", "provider": doc["provider"], "receipt": result["receipt"]})
            await _set(database.topups_collection, ref, status=SUCCESS, mpesa_receipt=result["receipt"],
                       result_desc="completed")
    else:
        await _set(database.topups_collection, ref, status=FAILED, result_desc=result["reason"] or "failed")
    return await database.topups_collection.find_one({"checkout_request_id": ref})


async def settle_topup(doc: dict) -> dict:
    """Pull the gateway's own answer for a top-up whose callback has not arrived."""
    if doc["status"] != PENDING:
        return doc
    if not doc.get("secure_id"):
        if datetime.utcnow() - doc["created_at"] > STUCK_AFTER:  # we never learned if a prompt went out
            await _set(database.topups_collection, doc["external_id"], status=REVIEW,
                       result_desc="no gateway reference - check manually")
            logger.error("topup %s has no gateway reference after %s - needs review", doc["external_id"], STUCK_AFTER)
        return doc
    try:
        result = await gw.get_status(doc["secure_id"])
    except (gw.GatewayError, gw.GatewayUnknownOutcome):
        logger.warning("gateway status unavailable for %s; will retry", doc["external_id"])
        return doc
    return await apply_topup_result(doc, result)


# =========================================================
# WITHDRAWAL (payout)
# =========================================================
async def initiate_withdrawal(user: dict, idempotency_key: str, phone: str, amount_kes: int, provider: str) -> dict:
    if not config.WALLET_WITHDRAW_ENABLED:
        raise HTTPException(503, "Withdrawals are not enabled yet")
    _require_gateway()
    provider = _provider(provider)
    if not (config.WITHDRAW_MIN_KES <= amount_kes <= config.WITHDRAW_MAX_KES):
        raise HTTPException(422, f"Amount must be between {config.WITHDRAW_MIN_KES} and {config.WITHDRAW_MAX_KES} KES")
    phone = normalize_phone(phone)
    try:
        registered = normalize_phone(user.get("phone", ""))
    except HTTPException:
        raise HTTPException(422, "Add a valid phone number to your profile before withdrawing")
    if phone != registered:  # money only ever goes to the number the customer registered with
        raise HTTPException(422, "Withdrawals can only be sent to the phone number on your account")

    owner_id = user["user_id"]
    external_id = _ref("WDR", owner_id, idempotency_key)
    doc = {"idem_key": f"{owner_id}:{idempotency_key}", "owner_id": owner_id, "external_id": external_id,
           "provider": provider, "phone": phone, "phone_masked": _mask(phone), "amount_kes": amount_kes,
           "status": PENDING, "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
    try:
        await database.payouts_collection.insert_one(doc)
    except DuplicateKeyError:
        return public_payout(await database.payouts_collection.find_one({"external_id": external_id}))

    try:
        await ledger.reserve(owner_id, amount_kes * 100, external_id, {"type": "withdraw"})
    except ledger.InsufficientFunds:
        await _set(database.payouts_collection, external_id, status=FAILED, result_desc="insufficient balance")
        raise HTTPException(402, "Insufficient wallet balance")

    try:
        response = await gw.initiate_transfer(phone, amount_kes, provider, external_id)
    except gw.GatewayError as exc:
        await ledger.release(external_id)
        await _set(database.payouts_collection, external_id, status=FAILED, result_desc=str(exc))
        raise HTTPException(422, f"Payment provider rejected the payout: {exc}")
    except gw.GatewayUnknownOutcome:
        # The payout may have gone out: KEEP the hold; the callback or the reconcile job settles it.
        logger.warning("payout %s outcome unknown - hold kept", external_id)
        return public_payout(doc)

    secure_id = response.get("secureId") or response.get("transactionId")
    await _set(database.payouts_collection, external_id, secure_id=secure_id)
    doc["secure_id"] = secure_id
    immediate = gw.normalize_status(response)
    if immediate["state"] != "PENDING":
        immediate["amount"] = None  # an acceptance response is not a settlement amount
        return public_payout(await apply_payout_result(doc, immediate))
    return public_payout(doc)


async def apply_payout_result(doc: dict, result: dict) -> dict:
    ref = doc["external_id"]
    if doc["status"] not in (PENDING, REVIEW) or result["state"] == "PENDING":
        return doc
    if doc.get("secure_id") and result.get("secure_id") and doc["secure_id"] != result["secure_id"]:
        logger.error("payout %s: gateway reference mismatch - ignored", ref)
        return doc
    if result["amount"] is not None and int(float(result["amount"])) != doc["amount_kes"]:
        logger.error("payout %s amount mismatch: requested %s, gateway %s - holding for review",
                     ref, doc["amount_kes"], result["amount"])
        await _set(database.payouts_collection, ref, status=REVIEW, result_desc="amount mismatch")
    elif result["state"] == "SUCCESS":
        await ledger.capture(ref)
        await _set(database.payouts_collection, ref, status=SUCCESS, receipt=result["receipt"], result_desc="completed")
    else:
        await ledger.release(ref)
        await _set(database.payouts_collection, ref, status=FAILED, result_desc=result["reason"] or "failed")
    return await database.payouts_collection.find_one({"external_id": ref})


async def settle_payout(doc: dict) -> dict:
    if doc["status"] != PENDING:
        return doc
    if not doc.get("secure_id"):
        if datetime.utcnow() - doc["created_at"] > STUCK_AFTER:
            await _set(database.payouts_collection, doc["external_id"], status=REVIEW,
                       result_desc="no gateway reference - hold kept, check manually")
            logger.error("payout %s has no gateway reference after %s - hold kept, needs review",
                         doc["external_id"], STUCK_AFTER)
        return doc
    try:
        result = await gw.get_status(doc["secure_id"])
    except (gw.GatewayError, gw.GatewayUnknownOutcome):
        logger.warning("gateway status unavailable for %s; will retry", doc["external_id"])
        return doc
    return await apply_payout_result(doc, result)


# =========================================================
# CALLBACK + RECONCILE
# =========================================================
async def handle_callback(payload: dict) -> Optional[str]:
    """Apply a verified gateway callback. Returns the matched externalId, or None if it is not ours."""
    result = gw.normalize_status(payload if isinstance(payload, dict) else {})
    ref = result["external_id"]
    if not ref:
        return None
    topup_doc = await database.topups_collection.find_one({"checkout_request_id": ref, "channel": "gateway"})
    if topup_doc:
        await apply_topup_result(topup_doc, result)
        return ref
    payout_doc = await database.payouts_collection.find_one({"external_id": ref})
    if payout_doc:
        await apply_payout_result(payout_doc, result)
        return ref
    logger.warning("gateway callback for unknown externalId")
    return None


async def settle_pending_payouts() -> int:
    """Reconcile job: settle payouts whose callback never arrived (gateway top-ups go through topup.settle)."""
    count = 0
    async for doc in database.payouts_collection.find(
            {"status": PENDING, "created_at": {"$lt": datetime.utcnow() - MIN_AGE}}):
        await settle_payout(doc)
        count += 1
    return count
