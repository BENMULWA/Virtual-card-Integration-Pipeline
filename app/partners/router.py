# =========================================================
# partners/router.py - Partner (reseller) API: customers, KYC, float, cards
# =========================================================
#
# Authenticated by the reseller's API key (X-API-Key). Every route resolves the customer inside the caller's
# own tenant, so one reseller can never read or act on another's customers. Mutations require an
# `Idempotency-Key` header, exactly like the first-party API. Cards are paid for from the reseller's float.

from datetime import datetime
from decimal import Decimal

import logging

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, EmailStr, Field

from app.cards import service
from app.cards import wasabi_client as wasabi
from app.cards.routeendpoint import ALLOWED_UPLOADS, KYC_KINDS, _extract_file_id, client_ip, strip_private
from app.core import database
from app.core.config import MAX_UPLOAD_BYTES
from app.core.money import parse_amount
from app.models import CardAmountRequest, CardNoRequest, CreateCardholderRequest, CreateCardRequest
from app.partners import service as partners
from app.transactions.orchestrator import build_order_no
from app.wallet import ledger, pricing

logger = logging.getLogger("partner")
router = APIRouter()
NO_FLOAT = "Insufficient float balance. Top up your Mamlaka float and try again."


def idempotency_key(key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=64)):
    return key


class CustomerCreate(BaseModel):
    externalId: str = Field(..., description="Your own id for this customer")
    fullName: str = Field(..., min_length=2, max_length=80)
    email: EmailStr
    phone: str = Field(..., min_length=7, max_length=20)


# ---------------------------------------------------------------- float
@router.get("/balance")
async def float_balance(tenant: dict = Depends(partners.tenant_guard)):
    """Your prepaid float (KES). Card issuance and card deposits are debited from it."""
    return await ledger.get_balance(partners.float_owner(tenant["tenant_id"]))


# ---------------------------------------------------------------- customers
@router.post("/customers", status_code=201)
async def create_customer(payload: CustomerCreate, tenant: dict = Depends(partners.tenant_guard)):
    customer, created = await partners.create_customer(
        tenant, payload.externalId, payload.fullName, payload.email, payload.phone)
    return {**partners.public_customer(customer), "created": created}


@router.get("/customers/{external_id}")
async def get_customer(external_id: str, tenant: dict = Depends(partners.tenant_guard)):
    return partners.public_customer(await partners.get_customer(tenant, external_id))


# ---------------------------------------------------------------- KYC
@router.post("/customers/{external_id}/kyc/documents")
async def upload_kyc_document(external_id: str, file: UploadFile = File(...), kind: str = Form(...),
                              tenant: dict = Depends(partners.tenant_guard)):
    """Upload one KYC image (JPG, PNG or PDF, max 2 MB). kind = front | back | selfie. Returns the fileId to use in /kyc."""
    customer = await partners.get_customer(tenant, external_id)
    if kind not in KYC_KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(KYC_KINDS)}")
    if file.content_type not in ALLOWED_UPLOADS:
        raise HTTPException(415, "Only JPG, PNG and PDF files are supported")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File exceeds the 2 MB limit")
    if not any(content.startswith(sig) for sig in ALLOWED_UPLOADS[file.content_type]):
        raise HTTPException(415, "File content does not match its declared type")
    response = await wasabi.upload_wasabi_file(content, file.filename, file.content_type)
    file_id = _extract_file_id(response)
    if not file_id:
        raise HTTPException(502, "The upload was not accepted by the card processor")
    await database.kyc_documents_collection.update_one(
        {"owner_id": customer["owner_id"], "file_id": file_id},
        {"$setOnInsert": {"created_at": datetime.utcnow()},
         "$set": {"owner_id": customer["owner_id"], "file_id": file_id, "kind": kind,
                  "filename": file.filename, "updated_at": datetime.utcnow()}},
        upsert=True)
    return {"kind": kind, "fileId": file_id}


@router.post("/customers/{external_id}/kyc")
async def submit_kyc(external_id: str, payload: CreateCardholderRequest, request: Request,
                     key: str = Depends(idempotency_key), tenant: dict = Depends(partners.tenant_guard)):
    """Register the customer as a cardholder. Approval is decided by the card processor and arrives as a
    `kyc.status_changed` webhook (or poll GET /customers/{id}/kyc)."""
    customer = await partners.get_customer(tenant, external_id)
    partners.require_card_type(tenant, payload.cardTypeId)
    order_no = build_order_no("HLD", customer["owner_id"], key)
    data = payload.model_dump(exclude_none=True)
    data["merchantOrderNo"] = order_no
    data["ipAddress"] = client_ip(request)
    return await service.register_holder(customer["owner_id"], order_no, data)


@router.put("/customers/{external_id}/kyc/{holder_id}")
async def resubmit_kyc(external_id: str, holder_id: int, payload: CreateCardholderRequest, request: Request,
                       key: str = Depends(idempotency_key), tenant: dict = Depends(partners.tenant_guard)):
    """Correct and resubmit a REJECTED cardholder."""
    customer = await partners.get_customer(tenant, external_id)
    partners.require_card_type(tenant, payload.cardTypeId)
    order_no = build_order_no("HLD", customer["owner_id"], key)
    data = payload.model_dump(exclude_none=True)
    data["merchantOrderNo"] = order_no
    data["ipAddress"] = client_ip(request)
    return await service.update_holder(customer["owner_id"], holder_id, order_no, data)


@router.get("/customers/{external_id}/kyc")
async def kyc_status(external_id: str, refresh: bool = False, tenant: dict = Depends(partners.tenant_guard)):
    customer = await partners.get_customer(tenant, external_id)
    records = []
    async for holder in database.holders_collection.find({"owner_id": customer["owner_id"]}).sort("created_at", -1):
        if refresh:
            await service.refresh_holder(holder["holder_id"])
            holder = await database.holders_collection.find_one({"holder_id": holder["holder_id"]})
        item = strip_private(holder)
        item.pop("owner_id", None)
        item["review"] = service.holder_review_summary(holder)
        records.append(item)
    return {"records": records}


# ---------------------------------------------------------------- cards
@router.post("/customers/{external_id}/cards")
async def create_card(external_id: str, payload: CreateCardRequest, key: str = Depends(idempotency_key),
                      tenant: dict = Depends(partners.tenant_guard)):
    """Issue a card for an approved cardholder. The opening load plus fees is debited from your float."""
    customer = await partners.get_customer(tenant, external_id)
    partners.require_card_type(tenant, payload.cardTypeId)
    cap = tenant.get("max_single_amount")
    if cap and payload.amount > Decimal(str(cap)):
        raise HTTPException(422, f"Amount exceeds your per-operation limit of {cap} USD")
    order_no = build_order_no("CRD", customer["owner_id"], key)
    return await service.issue_card(customer["owner_id"], order_no, payload,
                                    funding_owner=partners.float_owner(tenant["tenant_id"]),
                                    insufficient_detail=NO_FLOAT)


@router.get("/customers/{external_id}/cards")
async def list_cards(external_id: str, tenant: dict = Depends(partners.tenant_guard)):
    customer = await partners.get_customer(tenant, external_id)
    cursor = database.cards_collection.find({"owner_id": customer["owner_id"]}).sort("created_at", -1)
    records = []
    async for card in cursor:
        item = strip_private(card)
        item.pop("owner_id", None)
        records.append(item)
    return {"records": records}


async def _card_op(tenant, external_id, key, prefix, op_type, call, payload):
    customer = await partners.get_customer(tenant, external_id)
    order_no = build_order_no(prefix, customer["owner_id"], key)
    return await service.card_operation(customer["owner_id"], order_no, op_type, payload.cardNo, call,
                                        client_remark=payload.clientRemark)


@router.post("/customers/{external_id}/cards/freeze")
async def freeze_card(external_id: str, payload: CardNoRequest, key: str = Depends(idempotency_key),
                      tenant: dict = Depends(partners.tenant_guard)):
    return await _card_op(tenant, external_id, key, "FRZ", "FREEZE", wasabi.freeze_card, payload)


@router.post("/customers/{external_id}/cards/unfreeze")
async def unfreeze_card(external_id: str, payload: CardNoRequest, key: str = Depends(idempotency_key),
                        tenant: dict = Depends(partners.tenant_guard)):
    return await _card_op(tenant, external_id, key, "UFZ", "UNFREEZE", wasabi.unfreeze_card, payload)


@router.post("/customers/{external_id}/cards/deposit")
async def deposit_to_card(external_id: str, payload: CardAmountRequest, key: str = Depends(idempotency_key),
                          tenant: dict = Depends(partners.tenant_guard)):
    """Add funds to a card. Debited from your float."""
    service.require_funding_enabled(payload.amount)
    customer = await partners.get_customer(tenant, external_id)
    cap = tenant.get("max_single_amount")
    if cap and payload.amount > Decimal(str(cap)):
        raise HTTPException(422, f"Amount exceeds your per-operation limit of {cap} USD")
    card = await service.get_owned_card(payload.cardNo, customer["owner_id"])
    card_type = await service.find_card_type(card.get("card_type_id"))
    quote = pricing.quote_debit(payload.amount, **pricing.card_type_fees(card_type))
    return await service.card_operation(
        customer["owner_id"], build_order_no("DEP", customer["owner_id"], key), "DEPOSIT", payload.cardNo,
        wasabi.deposit, payload.amount,
        funding={"kind": "debit", "kes_minor": quote["kes_minor"], "quote": quote,
                 "owner_id": partners.float_owner(tenant["tenant_id"]), "insufficient_detail": NO_FLOAT},
        client_remark=payload.clientRemark)


# ---------------------------------------------------------------- operation outcome, balance, card details
@router.get("/operations/{reference}")
async def operation_status(reference: str, refresh: bool = False, tenant: dict = Depends(partners.tenant_guard)):
    """Outcome of an issuance / deposit / freeze you started (`reference` = the merchantOrderNo it returned).
    PENDING and UNKNOWN are not final: wait for the `operation.updated` webhook, or poll with refresh=true."""
    op = await database.transactions_collection.find_one({"merchant_order_no": reference})
    if not op or not str(op.get("owner_id", "")).startswith(tenant["tenant_id"] + ":"):
        raise HTTPException(404, "Operation not found")      # also the answer for another reseller's operation
    if refresh and op["status"] not in ("SUCCESS", "FAILED"):
        await service.reconcile_operation(reference)
        op = await database.transactions_collection.find_one({"merchant_order_no": reference})
    return {"reference": reference, "type": op["type"], "status": op["status"],
            "customerId": op["owner_id"].split(":", 1)[1], "updatedAt": op["updated_at"].isoformat()}


@router.get("/customers/{external_id}/cards/{card_no}/balance")
async def card_balance(external_id: str, card_no: str, tenant: dict = Depends(partners.tenant_guard)):
    customer = await partners.get_customer(tenant, external_id)
    await service.get_owned_card(card_no, customer["owner_id"])
    data = (await wasabi.card_balance({"cardNo": card_no})).get("data") or {}
    return {"cardNo": card_no, "balance": data.get("amount"), "currency": data.get("currency", "USD")}


@router.post("/customers/{external_id}/cards/details")
async def card_details(external_id: str, payload: CardNoRequest, response: Response,
                       tenant: dict = Depends(partners.tenant_guard)):
    """Full card number, expiry and CVV. SENSITIVE: show it to the cardholder only, over TLS, after you have
    re-authenticated them; never store, cache or log it. Using this puts your systems in card-data (PCI) scope."""
    customer = await partners.get_customer(tenant, external_id)
    await service.get_owned_card(payload.cardNo, customer["owner_id"])
    # Audit who revealed which card and when - never the values themselves.
    logger.warning("card details revealed tenant=%s customer=%s card=%s", tenant["slug"], external_id, payload.cardNo)
    response.headers["Cache-Control"] = "no-store"
    return (await wasabi.card_sensitive_decrypt({"cardNo": payload.cardNo}))["data"]


@router.get("/quote")
async def quote(kind: str = Query(..., pattern="^(issue|deposit)$"), amount: str = Query(..., description="USD"),
                cardTypeId: int = Query(...), tenant: dict = Depends(partners.tenant_guard)):
    """What an issuance or a deposit will cost your float, in KES, before you commit. Price your own customer
    from this figure (plus your margin) so a rate change on our side can never leave you out of pocket."""
    partners.require_card_type(tenant, cardTypeId)
    try:
        usd = parse_amount(amount)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    card_type = await service.find_card_type(cardTypeId)
    if not card_type or str(card_type.get("status", "")).lower() != "online":
        raise HTTPException(422, "Card type is not available")
    q = (pricing.quote_issue(usd, card_type) if kind == "issue"
         else pricing.quote_debit(usd, **pricing.card_type_fees(card_type)))
    return {"kind": kind, "usd": str(usd), "kes": str(ledger.from_minor(q["kes_minor"])),
            "cardFeeUsd": q.get("provider_fee_usd") if kind == "issue" else None}
