# =========================================================
# router.py - Wasabi Webhook Receiver
# =========================================================
#
# Trust model: a callback payload is only a *hint* about which holder/card/operation changed.
# State is never copied from the payload; the handler re-reads the authoritative record from
# Wasabi's signed API. So a forged callback can at worst trigger an extra read.
# On top of that, signature verification is enforced once ENABLE_WEBHOOK_SIGNATURE_VALIDATION is on
# (Wasabi has not documented the algorithm - confirm, then enable).

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pymongo.errors import DuplicateKeyError

from app.cards import service
from app.cards import wasabi_client as wasabi
from app.core import config
from app.core.config import ENABLE_WEBHOOK_SIGNATURE_VALIDATION
from app.core.database import cards_collection, holders_collection, webhook_events_collection
from app.core.signature import verify_wasabi_signature
from app.journey.events import publish_journey_update
from app.mobilemoney import client as gateway
from app.mobilemoney import service as mobilemoney
from app.partners import events as partner_events
from app.transactions import orchestrator as ops

logger = logging.getLogger("webhooks")
router = APIRouter()

ACK = {"success": True, "code": 200, "msg": None, "data": None}
SENSITIVE_FIELDS = {"email", "firstName", "lastName", "mobile", "values", "giftCardActivateUrl", "content"}


def _redact(payload):
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if k not in SENSITIVE_FIELDS}


# =========================================================
# HANDLERS (idempotent: safe to run more than once)
# =========================================================
async def handle_card_holder(payload: dict) -> Optional[str]:
    holder_id = payload.get("holderId")
    if holder_id is None:
        return None
    holder = await holders_collection.find_one({"holder_id": int(holder_id)})
    if not holder:
        return None
    record = await service.refresh_holder(int(holder_id))
    if record:  # a reseller's customer: announce the decision on its own webhook
        await partner_events.emit_kyc(holder["owner_id"], int(holder_id), record)
    # Push the new status to the applicant's browser immediately.
    await publish_journey_update(holder["owner_id"], "kyc_status_changed")
    return holder["owner_id"]


async def handle_card_transaction(payload: dict) -> Optional[str]:
    order_no = payload.get("merchantOrderNo")
    card_no = payload.get("cardNo")
    owner_id = None

    if order_no:
        op = await ops.get_operation(order_no)
        if op:
            owner_id = op["owner_id"]
        await service.reconcile_operation(order_no)   # authoritative lookup + wallet settlement

    if card_no:
        card = await cards_collection.find_one({"card_no": card_no})
        if card:
            owner_id = owner_id or card["owner_id"]
            await service.refresh_card(card_no)

    if owner_id:
        await partner_events.emit_for_owner(owner_id, "card.updated", {"cardNo": card_no, "orderNo": order_no})
        await publish_journey_update(owner_id, "card_status_changed")
    return owner_id


async def handle_work_ticket(payload: dict) -> Optional[str]:
    """A ticket we raised with Wasabi was resolved."""
    order_no = payload.get("merchantOrderNo")
    if not order_no:
        return None
    op = await ops.get_operation(order_no)
    if not op:
        return None
    await publish_journey_update(op["owner_id"], "work_order_resolved")
    return op["owner_id"]


HANDLERS = {
    "card_holder": handle_card_holder,
    "card_transaction": handle_card_transaction,
    "work": handle_work_ticket,
    # card_transfer, card_auth_transaction, card_fee_patch, card_3ds, ... are stored for now.
}


# =========================================================
# WASABI WEBHOOK
# =========================================================
@router.post("/wasabi")
async def wasabi_webhook(request: Request):
    raw_body = await request.body()
    category = request.headers.get("X-WSB-CATEGORY", "")
    request_id = request.headers.get("X-WSB-REQUEST-ID", "")
    signature = request.headers.get("X-WSB-SIGNATURE", "")

    if ENABLE_WEBHOOK_SIGNATURE_VALIDATION and not verify_wasabi_signature(raw_body, signature):
        logger.warning("webhook rejected: bad signature (request_id=%s)", request_id)
        raise HTTPException(401, "Invalid signature")
    if not request_id:
        raise HTTPException(400, "Missing X-WSB-REQUEST-ID")

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "Invalid JSON")

    # Dedupe: Wasabi retries up to 7 times. Already-processed events are simply acknowledged.
    try:
        await webhook_events_collection.insert_one({
            "request_id": request_id,
            "category": category,
            "payload": _redact(payload),
            "state": "received",
            "received_at": datetime.utcnow(),
        })
    except DuplicateKeyError:
        existing = await webhook_events_collection.find_one({"request_id": request_id})
        if existing and existing.get("state") == "processed":
            return ACK

    handler = HANDLERS.get(category)
    try:
        owner_id = None
        if handler:
            owner_id = await handler(payload if isinstance(payload, dict) else {})
        update = {"state": "processed", "processed_at": datetime.utcnow()}
        if owner_id:
            update["owner_id"] = owner_id   # links the event to the customer's journey
        await webhook_events_collection.update_one({"request_id": request_id}, {"$set": update})
    except Exception as exc:  # noqa: BLE001 - any failure must trigger a Wasabi retry
        logger.exception("webhook processing failed (request_id=%s category=%s)", request_id, category)
        await webhook_events_collection.update_one(
            {"request_id": request_id}, {"$set": {"state": "failed", "error": type(exc).__name__}})
        return JSONResponse(status_code=500, content={"success": False, "code": 500, "msg": "retry"})

    return ACK


# =========================================================
# MAMLAKA GATEWAY CALLBACK (top-up collections and withdrawal payouts)
# =========================================================
# Authenticated by the gateway's HMAC over the raw body. Unsigned or wrongly signed requests never reach the ledger.
@router.post("/mamlaka")
async def mamlaka_callback(request: Request):
    secret = config.AIRTEL_CALLBACK_SIGNING_SECRET
    if not secret:
        raise HTTPException(503, "Gateway callback verification is not configured")
    raw_body = await request.body()
    if not gateway.verify_signature(raw_body, request.headers.get("X-Mamlaka-Signature", ""), secret):
        logger.warning("gateway callback rejected: bad signature")
        raise HTTPException(401, "Invalid signature")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    try:
        await mobilemoney.handle_callback(payload)
    except Exception:  # noqa: BLE001 - everything is idempotent, so a retry by the gateway is safe
        logger.exception("gateway callback processing failed")
        return JSONResponse(status_code=500, content={"success": False, "msg": "retry"})
    return {"success": True}
