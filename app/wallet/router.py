# =========================================================
# wallet/router.py - balance, M-Pesa top-up, price quote
# =========================================================

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.cards import service
from app.core import config, database
from app.core.money import parse_amount
from app.core.security import auth_guard, verified_email_guard
from app.mobilemoney import service as mobilemoney
from app.mpesa import client as mpesa
from app.mpesa import topup
from app.wallet import ledger, pricing

router = APIRouter()


class TopupRequest(BaseModel):
    phone: str = Field(..., description="Safaricom number, e.g. 0712345678 or 254712345678")
    amountKes: int = Field(..., gt=0, description="Whole shillings (STK Push does not accept cents)")
    provider: str = Field("mpesa", description="mpesa or airtel")
    channel: str = Field("auto", description="auto, daraja (direct Safaricom) or gateway (Mamlaka gateway). "
                                             "auto uses the gateway unless direct Daraja is configured; airtel always uses the gateway")


class WithdrawRequest(BaseModel):
    phone: str = Field(..., description="Must be the phone number on your account")
    amountKes: int = Field(..., gt=0, description="Whole shillings")
    provider: str = Field("mpesa", description="mpesa or airtel")


@router.get("")
async def wallet(current_user: dict = Depends(auth_guard)):
    return await ledger.get_balance(current_user["id"])


@router.post("/topup")
async def start_topup(payload: TopupRequest,
                      idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=64),
                      current_user: dict = Depends(auth_guard)):
    channel = payload.channel.lower()
    if channel == "auto":  # no Daraja keys -> M-Pesa runs through the gateway too
        channel = "daraja" if mpesa.configured() else "gateway"
    if payload.provider.lower() == "airtel" or channel == "gateway":
        return await mobilemoney.initiate_topup(current_user["id"], idempotency_key, payload.phone,
                                                payload.amountKes, payload.provider)
    return await topup.initiate(current_user["id"], idempotency_key, payload.phone, payload.amountKes)


@router.get("/topups/{checkout_request_id}")
async def get_topup(checkout_request_id: str, refresh: bool = False, current_user: dict = Depends(auth_guard)):
    doc = await database.topups_collection.find_one(
        {"checkout_request_id": checkout_request_id, "owner_id": current_user["id"]})
    if not doc:
        raise HTTPException(404, "Top-up not found")
    if refresh and doc["status"] == topup.PENDING:   # missed-callback recovery
        doc = await topup.settle(checkout_request_id) or doc
    return topup.public(doc)


@router.get("/quote")
async def quote(usd: str = Query(..., description="Card amount in USD"),
                kind: str = Query("fund", pattern="^(fund|withdraw|issue)$"),
                cardTypeId: Optional[int] = Query(None, description="Defaults to the first offered card type"),
                current_user: dict = Depends(auth_guard)):
    """The KES price the matching card operation will charge - same maths as the operation itself."""
    try:
        amount = parse_amount(usd)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    type_id = cardTypeId if cardTypeId is not None else min(config.ALLOWED_CARD_TYPE_IDS)
    service.require_allowed_card_type(type_id)
    card_type = await service.find_card_type(type_id)
    if kind == "issue":
        if not card_type:
            raise HTTPException(422, "Card type is not available")
        q = pricing.quote_issue(amount, card_type)
    elif kind == "fund":
        q = pricing.quote_debit(amount, **pricing.card_type_fees(card_type))
    else:
        q = pricing.quote_credit(amount, **pricing.card_type_fees(card_type))
    q["kes"] = str(ledger.from_minor(q.pop("kes_minor")))
    return q


# ---------------------------------------------------------------- withdraw to mobile money
@router.post("/withdraw")
async def withdraw(payload: WithdrawRequest,
                   idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=64),
                   user: dict = Depends(verified_email_guard)):
    """Send wallet KES to the customer's own phone. The funds are held first and released if the payout fails."""
    return await mobilemoney.initiate_withdrawal(user, idempotency_key, payload.phone, payload.amountKes,
                                                 payload.provider)


@router.get("/withdrawals/{reference}")
async def get_withdrawal(reference: str, refresh: bool = False, current_user: dict = Depends(auth_guard)):
    doc = await database.payouts_collection.find_one({"external_id": reference, "owner_id": current_user["id"]})
    if not doc:
        raise HTTPException(404, "Withdrawal not found")
    if refresh and doc["status"] == mobilemoney.PENDING:   # missed-callback recovery
        doc = await mobilemoney.settle_payout(doc)
    return mobilemoney.public_payout(doc)
