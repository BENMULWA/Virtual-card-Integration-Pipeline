# =========================================================
# mpesa/topup.py - STK top-up lifecycle: initiate -> (callback hint) -> confirm via STK Query -> credit
# =========================================================

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException

from app.core import config, database
from app.mpesa import client as mpesa
from app.wallet import ledger

logger = logging.getLogger("topup")

PENDING, SUCCESS, FAILED = "PENDING", "SUCCESS", "FAILED"
# Daraja ResultCode 4999 = still under processing. Other non-zero codes are final (1 insufficient funds,
# 1032 cancelled, 1037 phone unreachable, 2001 wrong PIN ...). While a prompt is open STK Query itself
# answers HTTP 500 "being processed", which the client reports as an unknown outcome -> stays PENDING.
STILL_PROCESSING = {"4999"}
STALE_AFTER = timedelta(minutes=10)


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"[\s\-+]", "", raw or "")
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    elif digits[:1] in ("7", "1") and len(digits) == 9:
        digits = "254" + digits
    if not re.fullmatch(r"254[17]\d{8}", digits):
        raise HTTPException(422, "Enter a valid Safaricom number, e.g. 0712345678")
    return digits


async def initiate(owner_id: str, idempotency_key: str, phone: str, amount_kes: int) -> dict:
    if not mpesa.configured():
        raise HTTPException(503, "M-Pesa is not configured")
    if not (config.MPESA_MIN_TOPUP_KES <= amount_kes <= config.MPESA_MAX_TOPUP_KES):
        raise HTTPException(422, f"Amount must be between {config.MPESA_MIN_TOPUP_KES} and {config.MPESA_MAX_TOPUP_KES} KES")
    phone = normalize_phone(phone)

    key = f"{owner_id}:{idempotency_key}"
    existing = await database.topups_collection.find_one({"idem_key": key})
    if existing:  # replayed request: never send a second prompt
        return public(existing)

    try:
        response = await mpesa.stk_push(phone, amount_kes, "MamlakaWallet", "Wallet topup")
    except mpesa.MpesaError as exc:
        raise HTTPException(422, f"M-Pesa rejected the request: {exc}")
    except mpesa.MpesaUnknownOutcome:
        raise HTTPException(502, "Could not reach M-Pesa. Check your phone before retrying; do not pay twice.")

    doc = {
        "idem_key": key,
        "owner_id": owner_id,
        "checkout_request_id": response["CheckoutRequestID"],
        "merchant_request_id": response.get("MerchantRequestID"),
        "phone_masked": phone[:5] + "****" + phone[-3:],
        "amount_kes": amount_kes,
        "status": PENDING,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    await database.topups_collection.insert_one(doc)
    return public(doc)


def public(doc: dict) -> dict:
    return {k: doc.get(k) for k in ("checkout_request_id", "amount_kes", "status", "phone_masked", "mpesa_receipt",
                                    "result_desc", "created_at", "provider")}


async def settle(checkout_request_id: str, callback_hint: Optional[dict] = None) -> Optional[dict]:
    """
    Resolve a top-up from Daraja's own STK Query. The callback body is only a hint (it is unsigned):
    it supplies the receipt number, but the amount credited is always the amount WE requested.
    """
    topup = await database.topups_collection.find_one({"checkout_request_id": checkout_request_id})
    if not topup:
        return None
    if topup.get("channel") == "gateway":  # Airtel / gateway M-Pesa: settled by the gateway's own status
        from app.mobilemoney import service as gateway
        return await gateway.settle_topup(topup)
    if topup["status"] != PENDING:
        return topup

    try:
        result = await mpesa.stk_query(checkout_request_id)
    except (mpesa.MpesaError, mpesa.MpesaUnknownOutcome):
        logger.warning("stk query unavailable for %s; will retry", checkout_request_id)
        return topup  # stays PENDING; callback retry / refresh / reconcile job will settle it

    code = str(result.get("ResultCode", ""))
    desc = result.get("ResultDesc")
    if code in STILL_PROCESSING or code == "":
        if datetime.utcnow() - topup["created_at"] > STALE_AFTER and code == "":
            code = "TIMEOUT"
        else:
            return topup

    if code == "0":
        receipt = (callback_hint or {}).get("receipt")
        hint_amount = (callback_hint or {}).get("amount")
        if hint_amount is not None and int(float(hint_amount)) != topup["amount_kes"]:
            logger.error("topup %s amount mismatch: requested %s, callback %s - holding for review",
                         checkout_request_id, topup["amount_kes"], hint_amount)
            await database.topups_collection.update_one(
                {"checkout_request_id": checkout_request_id},
                {"$set": {"status": "REVIEW", "result_desc": "amount mismatch", "updated_at": datetime.utcnow()}})
            return await database.topups_collection.find_one({"checkout_request_id": checkout_request_id})
        credited = await ledger.credit(
            topup["owner_id"], topup["amount_kes"] * 100, ref=checkout_request_id, kind="topup",
            meta={"channel": "mpesa", "receipt": receipt})
        update = {"status": SUCCESS, "result_desc": desc, "updated_at": datetime.utcnow()}
        if receipt:
            update["mpesa_receipt"] = receipt
        await database.topups_collection.update_one(
            {"checkout_request_id": checkout_request_id, "status": PENDING}, {"$set": update})
        logger.info("topup %s credited=%s", checkout_request_id, credited)
    else:
        await database.topups_collection.update_one(
            {"checkout_request_id": checkout_request_id, "status": PENDING},
            {"$set": {"status": FAILED, "result_desc": desc, "result_code": code, "updated_at": datetime.utcnow()}})

    return await database.topups_collection.find_one({"checkout_request_id": checkout_request_id})


def parse_callback(body: dict) -> Optional[dict]:
    """Extract (CheckoutRequestID, receipt, amount) from Daraja's stkCallback envelope."""
    cb = ((body or {}).get("Body") or {}).get("stkCallback")
    if not isinstance(cb, dict) or not cb.get("CheckoutRequestID"):
        return None
    items = {i.get("Name"): i.get("Value") for i in (cb.get("CallbackMetadata") or {}).get("Item", [])}
    return {"checkout_request_id": cb["CheckoutRequestID"], "result_code": cb.get("ResultCode"),
            "receipt": items.get("MpesaReceiptNumber"), "amount": items.get("Amount")}
