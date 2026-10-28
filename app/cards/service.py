# app/cards/service.py
# Business rules: ownership, holder gating, idempotent provider operations, local persistence.

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Awaitable, Callable, Optional

from fastapi import HTTPException

from app.cards import wasabi_client as wasabi
from app.cards.wasabi_client import WasabiError, WasabiUnknownOutcome
from app.core import config, treasury
from app.core.config import LIVE_MONEY_ENABLED, MAX_SINGLE_AMOUNT
from app.core.database import cards_collection, holders_collection, transactions_collection
from app.core.money import wire_amount
from app.partners import events as partner_events
from app.transactions import orchestrator as ops
from app.wallet import ledger, pricing

logger = logging.getLogger("cards")

HOLDER_APPROVED = "pass_audit"
UNSEEN_AFTER = timedelta(minutes=30)


# =========================================================
# GUARDS
# =========================================================
def require_funding_enabled(amount: Optional[Decimal] = None):
    """Gate for operations that spend merchant funds until a wallet/ledger source is wired in."""
    if not LIVE_MONEY_ENABLED:
        raise HTTPException(503, "Card funding is not enabled yet")
    if amount is not None and amount > MAX_SINGLE_AMOUNT:
        raise HTTPException(422, f"Amount exceeds the per-operation limit of {MAX_SINGLE_AMOUNT}")


def require_allowed_card_type(card_type_id) -> None:
    if card_type_id not in config.ALLOWED_CARD_TYPE_IDS:
        raise HTTPException(422, "This card type is not offered")


async def get_owned_card(card_no: str, owner_id: str) -> dict:
    card = await cards_collection.find_one({"card_no": card_no, "owner_id": owner_id})
    if not card:  # 404 for both "missing" and "someone else's" so cards can't be enumerated
        raise HTTPException(404, "Card not found")
    return card


async def find_card_type(card_type_id) -> Optional[dict]:
    """The live Wasabi catalog entry for a card type (fee terms live there, not in our config)."""
    card_types = (await wasabi.get_card_types()).get("data") or []
    return next((c for c in card_types if c.get("cardTypeId") == card_type_id), None)


async def get_owned_holder(holder_id: int, owner_id: str) -> dict:
    holder = await holders_collection.find_one({"holder_id": holder_id, "owner_id": owner_id})
    if not holder:
        raise HTTPException(404, "Cardholder not found")
    return holder


# Wasabi holder statuses and who performs the review.
HOLDER_STATUS_LABELS = {
    "wait_audit": "Pending review",
    "under_review": "Under review",
    "pass_audit": "Approved",
    "reject": "Rejected",
}
HOLDER_REVIEWERS = {
    "admin": "Wasabi platform review",
    "channel": "Issuing bank review",
}


def holder_review_summary(holder: dict) -> dict:
    """
    Tell the client where a cardholder sits in Wasabi's approval flow and what to do next.
    Approval is Wasabi's decision - the merchant can only observe it and escalate via a work order.
    """
    status = holder.get("status")
    location = holder.get("status_flow_location")
    approved = status == HOLDER_APPROVED
    if approved:
        next_step = "You can now create a card for this holder."
    elif status == "reject":
        next_step = ("Registration was rejected. Correct the details and call "
                     "POST /api/v1/cards/cardholder/v2/update to resubmit.")
    else:
        next_step = ("Awaiting Wasabi approval. Poll GET /api/v1/cards/cardholder/{holder_id}?refresh=true "
                     "or escalate with POST /api/v1/cards/kyc/submit (tradeType=CARD_ACTIVE).")
    return {
        "status": status,
        "status_label": HOLDER_STATUS_LABELS.get(status, "Unknown"),
        "approved": approved,
        "reviewer": HOLDER_REVIEWERS.get(location, "Unknown"),
        "review_location": location,
        "description": holder.get("description"),
        "next_step": next_step,
    }


# =========================================================
# GENERIC IDEMPOTENT OPERATION RUNNER
# =========================================================
async def settle_funding(op: Optional[dict]):
    """
    Apply the wallet effect of a finished operation exactly once (every ledger write is idempotent):
      debit : SUCCESS -> capture the hold, FAILED -> release it.   PENDING/UNKNOWN keep the hold.
      credit: SUCCESS -> add the proceeds to the wallet.
    """
    if not op or not op.get("funding") or op.get("funding_settled"):
        return
    if op["status"] not in ops.FINAL_STATUSES:
        return
    funding, ref = op["funding"], op["merchant_order_no"]
    if funding["kind"] == "debit":
        await (ledger.capture(ref) if op["status"] == ops.SUCCESS else ledger.release(ref))
    elif op["status"] == ops.SUCCESS:
        await ledger.credit(funding.get("owner_id", op["owner_id"]), funding["kes_minor"], ref=ref, kind="withdraw_credit",
                            meta={"cardNo": (op.get("summary") or {}).get("cardNo")})
    await transactions_collection.update_one({"merchant_order_no": ref}, {"$set": {"funding_settled": True}})


async def run_operation(
    *,
    order_no: str,
    tx_type: str,
    owner_id: str,
    summary: dict,
    call: Callable[[], Awaitable[dict]],
    funding: Optional[dict] = None,
) -> dict:
    """
    Record intent -> (reserve wallet funds) -> call Wasabi once -> record outcome -> settle funds.
      - Reused idempotency key: return the stored state, never call Wasabi (or reserve) again.
      - WasabiError: definitive failure -> FAILED, hold released, re-raised.
      - WasabiUnknownOutcome: UNKNOWN, hold KEPT, re-raised; resolved later by webhook/reconcile.
    """
    record, created = await ops.begin_operation(order_no, tx_type, owner_id, summary, funding)
    if not created:
        if record.get("owner_id") != owner_id:
            raise HTTPException(409, "Reference already in use")
        return {"merchantOrderNo": order_no, "status": record["status"], "duplicate": True}

    if funding and funding["kind"] == "debit":
        # funding["owner_id"] lets a reseller's float pay for its customer's operation
        try:
            await ledger.reserve(funding.get("owner_id", owner_id), funding["kes_minor"], order_no, {"type": tx_type})
        except ledger.InsufficientFunds:
            await ops.finish_operation(order_no, ops.FAILED, {"reason": "insufficient wallet balance"})
            raise HTTPException(402, funding.get("insufficient_detail",
                                                 "Insufficient wallet balance. Top up with M-Pesa and try again."))

    try:
        response = await call()
    except WasabiError as exc:
        await settle_funding(await ops.finish_operation(order_no, ops.FAILED, {"code": exc.code, "msg": exc.message}))
        raise
    except WasabiUnknownOutcome:
        await ops.finish_operation(order_no, ops.UNKNOWN)
        raise

    data = response.get("data") or {}
    status = ops.map_provider_status(data.get("status"))
    updated = await ops.finish_operation(order_no, status, {"orderNo": data.get("orderNo"), "status": data.get("status")})
    await settle_funding(updated)
    return {"merchantOrderNo": order_no, "status": status, "data": data}


async def reconcile_operation(order_no: str) -> Optional[str]:
    """
    Ask Wasabi for the authoritative result of an operation we sent (used by webhooks and the reconcile job).
    An operation Wasabi has no record of, long after we sent it, never arrived: fail it and release the hold.
    """
    op = await ops.get_operation(order_no)
    if not op:
        return None
    if op["status"] in ops.FINAL_STATUSES:
        await settle_funding(op)
        return op["status"]
    response = await wasabi.card_operations({"pageNum": 1, "pageSize": 1, "merchantOrderNo": order_no})
    records = (response.get("data") or {}).get("records") or []
    if records:
        status = ops.map_provider_status(records[0].get("status"))
        data = {"orderNo": records[0].get("orderNo"), "status": records[0].get("status")}
    elif op["status"] in (ops.UNKNOWN, ops.INITIATED) and datetime.utcnow() - op["created_at"] > UNSEEN_AFTER:
        status, data = ops.FAILED, {"reason": "not found at provider"}
    else:
        return op["status"]
    updated = await ops.finish_operation(order_no, status, data)
    await settle_funding(updated)
    if updated and status in ops.FINAL_STATUSES:  # a reseller waits on this to settle its own customer's money
        await partner_events.emit_for_owner(op["owner_id"], "operation.updated", {
            "reference": order_no, "type": op["type"], "status": status})
    return status


# =========================================================
# CARDHOLDERS
# =========================================================
async def register_holder(owner_id: str, order_no: str, payload: dict) -> dict:
    require_allowed_card_type(payload.get("cardTypeId"))
    result = await run_operation(
        order_no=order_no,
        tx_type="CREATE_HOLDER",
        owner_id=owner_id,
        summary={"cardTypeId": payload.get("cardTypeId")},  # no KYC values in our records
        call=lambda: wasabi.create_cardholder(payload),
    )
    data = result.get("data") or {}
    if data.get("holderId"):
        await holders_collection.update_one(
            {"holder_id": int(data["holderId"])},
            {
                "$setOnInsert": {"created_at": datetime.utcnow()},
                "$set": {
                    "owner_id": owner_id,
                    "card_type_id": payload.get("cardTypeId"),
                    "merchant_order_no": order_no,
                    "status": data.get("status"),
                    "description": data.get("description"),
                    # "admin" = Wasabi platform review, "channel" = issuing bank review
                    "status_flow_location": data.get("statusFlowLocation"),
                    "updated_at": datetime.utcnow(),
                },
            },
            upsert=True,
        )
    return result


async def update_holder(owner_id: str, holder_id: int, order_no: str, payload: dict) -> dict:
    """
    Resubmit a rejected cardholder's KYC (Wasabi v2 update).
    Wasabi only accepts an update when the holder status is "reject" - any other
    status is a hard error, so we check before spending a call.
    """
    holder = await get_owned_holder(holder_id, owner_id)
    if holder.get("status") != "reject":
        raise HTTPException(
            409,
            f"Cardholder can only be updated after a rejection (current status: {holder.get('status')})",
        )
    require_allowed_card_type(payload.get("cardTypeId"))

    data = {**payload, "holderId": holder_id, "merchantOrderNo": order_no}
    result = await run_operation(
        order_no=order_no,
        tx_type="UPDATE_HOLDER",
        owner_id=owner_id,
        summary={"cardTypeId": payload.get("cardTypeId"), "holderId": holder_id},
        call=lambda: wasabi.update_cardholder(data),
    )

    result_data = result.get("data") or {}
    await holders_collection.update_one(
        {"holder_id": holder_id},
        {"$set": {"status": result_data.get("status") or holder.get("status"),
                  "description": result_data.get("description"),
                  "status_flow_location": result_data.get("statusFlowLocation") or holder.get("status_flow_location"),
                  "updated_at": datetime.utcnow()}},
    )
    return result


async def refresh_holder(holder_id: int) -> Optional[dict]:
    """Authoritative holder status straight from Wasabi (used by webhook + manual refresh)."""
    response = await wasabi.query_cardholders({"pageNum": 1, "pageSize": 1, "holderId": holder_id})
    records = (response.get("data") or {}).get("records") or []
    if not records:
        return None
    record = records[0]
    await holders_collection.update_one(
        {"holder_id": holder_id},
        {"$set": {"status": record.get("status"), "description": record.get("description"),
                  "status_flow_location": record.get("statusFlowLocation"),
                  "resp_msg": record.get("respMsg"),
                  "updated_at": datetime.utcnow()}},
    )
    return record


# =========================================================
# CARD CREATION
# =========================================================
async def issue_card(owner_id: str, order_no: str, req, funding_owner: Optional[str] = None,
                     insufficient_detail: Optional[str] = None) -> dict:
    require_allowed_card_type(req.cardTypeId)
    require_funding_enabled(req.amount)

    card_types = (await wasabi.get_card_types()).get("data") or []
    card_type = next((c for c in card_types if c.get("cardTypeId") == req.cardTypeId), None)
    if not card_type or str(card_type.get("status", "")).lower() != "online":
        raise HTTPException(422, "Card type is not available")

    holder_id = None
    if card_type.get("needCardHolder"):
        if not req.holderId:
            raise HTTPException(422, "This card type requires an approved cardholder (holderId)")
        holder = await get_owned_holder(req.holderId, owner_id)
        if holder.get("card_type_id") != req.cardTypeId:
            raise HTTPException(422, "Cardholder was registered for a different card type")
        if holder.get("status") != HOLDER_APPROVED:
            location = holder.get("status_flow_location")
            reviewer = {
                "admin": "Wasabi platform review",
                "channel": "issuing bank review",
            }.get(location, "review pending")
            raise HTTPException(
                409,
                f"Cardholder is not approved yet (status: {holder.get('status')}, "
                f"awaiting: {reviewer}). Approval is granted by Wasabi, not by the merchant.",
            )
        holder_id = req.holderId

    quote = pricing.quote_issue(req.amount, card_type)  # raises 503 if no FX rate is configured
    if not await ops.get_operation(order_no):  # a replay of an existing operation must still return its state
        await treasury.require_balance(req.amount + Decimal(quote.get("provider_fee_usd") or 0))
    funding = {"kind": "debit", "kes_minor": quote["kes_minor"], "quote": quote}
    if funding_owner:
        funding["owner_id"] = funding_owner
    if insufficient_detail:
        funding["insufficient_detail"] = insufficient_detail

    payload = {
        "merchantOrderNo": order_no,
        "cardTypeId": req.cardTypeId,
        "holderId": holder_id,
        "amount": wire_amount(req.amount),
        "designId": req.designId,
    }
    result = await run_operation(
        order_no=order_no,
        tx_type="CREATE_CARD",
        owner_id=owner_id,
        summary={"cardTypeId": req.cardTypeId, "amount": str(req.amount)},
        call=lambda: wasabi.create_card(payload),
        funding=funding,
    )

    data = result.get("data") or {}
    if data.get("cardNo"):  # v2 always returns cardNo; ownership is recorded immediately
        await cards_collection.update_one(
            {"card_no": data["cardNo"]},
            {
                "$setOnInsert": {"created_at": datetime.utcnow()},
                "$set": {
                    "owner_id": owner_id,
                    "holder_id": holder_id,
                    "card_type_id": req.cardTypeId,
                    "merchant_order_no": order_no,
                    "status": "pending",
                    "updated_at": datetime.utcnow(),
                },
            },
            upsert=True,
        )
        result["cardNo"] = data["cardNo"]
    return result


async def refresh_card(card_no: str) -> Optional[dict]:
    """Authoritative card status from Wasabi (pending/Normal/Freeze/cancel/blocked ...)."""
    response = await wasabi.card_info({"cardNo": card_no})
    data = response.get("data") or {}
    if not data:
        return None
    await cards_collection.update_one(
        {"card_no": card_no},
        {"$set": {"status": data.get("status"), "blocked": data.get("blocked"),
                  "updated_at": datetime.utcnow()}},
    )
    return data


# =========================================================
# CARD OPERATIONS (all ownership-checked, idempotent)
# =========================================================
async def card_operation(owner_id: str, order_no: str, op_type: str, card_no: str,
                         call: Callable[[dict], Awaitable[dict]], amount: Optional[Decimal] = None,
                         funding: Optional[dict] = None, client_remark: Optional[str] = None) -> dict:
    await get_owned_card(card_no, owner_id)
    payload = {"cardNo": card_no, "merchantOrderNo": order_no}
    summary = {"cardNo": card_no}
    if amount is not None:
        payload["amount"] = wire_amount(amount)
        summary["amount"] = str(amount)
    if client_remark:
        payload["clientRemark"] = client_remark
        summary["clientRemark"] = client_remark
    if op_type == "DEPOSIT" and amount is not None and not await ops.get_operation(order_no):
        await treasury.require_balance(amount)
    return await run_operation(
        order_no=order_no, tx_type=op_type, owner_id=owner_id, summary=summary,
        call=lambda: call(payload), funding=funding,
    )


# =========================================================
# WORK ORDER (KYC ticket)
# =========================================================
async def process_work_order(owner_id: str, order_no: str, payload: dict) -> dict:
    payload = {**payload, "merchantOrderNo": order_no}
    return await run_operation(
        order_no=order_no,
        tx_type="SUBMIT_WORK_ORDER",
        owner_id=owner_id,
        summary={"tradeType": payload.get("tradeType")},
        call=lambda: wasabi.submit_kyc_work_order(payload),
    )
