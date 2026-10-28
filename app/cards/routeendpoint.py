# =========================================================
# routeendpoint.py - Card API endpoints
# =========================================================
#
# Every route requires auth. Mutations require an `Idempotency-Key` header; the Wasabi
# merchantOrderNo is derived from (user, operation, key) so a retried request cannot execute twice.

from decimal import Decimal
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile

from app.cards import service
from app.cards import wasabi_client as wasabi
from app.core import config
from app.core.config import MAX_UPLOAD_BYTES, TRUST_PROXY_HEADERS
from app.core.database import cards_collection, holders_collection, kyc_documents_collection
from app.core.security import auth_guard
from app.journey.events import publish_journey_update
from app.models import (
    CardAmountRequest,
    CardNoRequest,
    CreateCardholderRequest,
    CreateCardRequest,
    KYCSubmissionRequest,
)
from app.transactions.orchestrator import build_order_no
from app.wallet import pricing

router = APIRouter()

ALLOWED_UPLOADS = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "application/pdf": (b"%PDF-",),
}

# The three KYC slots Wasabi expects: idFrontId, idBackId and idHoldId (selfie).
KYC_KINDS = {"front", "back", "selfie"}

# Wasabi's upload response has carried the file id under different names over time.
_FILE_ID_KEYS = ("fileId", "file_id", "id", "objectId", "key", "url")


def _extract_file_id(response: dict) -> Optional[str]:
    """Pull the uploaded file's id out of Wasabi's response, whatever field it used."""
    data = (response or {}).get("data")
    if isinstance(data, str):
        return data
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return None
    for key in _FILE_ID_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# =========================================================
# HELPERS
# =========================================================
def idempotency_key(idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=64)):
    return idempotency_key


def order_no_for(prefix: str, user: dict, key: str) -> str:
    return build_order_no(prefix, user["id"], key)


def client_ip(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def strip_private(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "_id"}


# =========================================================
# CATALOG
# =========================================================
@router.post("/supported-BINS/types")
async def card_types(current_user: dict = Depends(auth_guard)):
    """Live Wasabi catalog, limited to the card types Mamlaka offers (ALLOWED_CARD_TYPE_IDS)."""
    response = await wasabi.get_card_types()
    response["data"] = [c for c in (response.get("data") or []) if c.get("cardTypeId") in config.ALLOWED_CARD_TYPE_IDS]
    return response


@router.get("/diagnostic/kenya-cities")
async def lookup_kenya_cities(current_user: dict = Depends(auth_guard)):
    return await wasabi.get_wasabi_cities("KE")


@router.get("/occupations")
async def occupations(current_user: dict = Depends(auth_guard)):
    return await wasabi.get_occupations()


# =========================================================
# MY CARDS (local, owner-scoped - Wasabi's list is merchant-wide and must not be exposed)
# =========================================================
@router.get("/list-BINS")
async def list_cards(
    page: int = Query(1, ge=1),
    size: int = Query(10, alias="pageSize", ge=1, le=100),
    current_user: dict = Depends(auth_guard),
):
    query = {"owner_id": current_user["id"]}
    total = await cards_collection.count_documents(query)
    cursor = cards_collection.find(query).sort("created_at", -1).skip((page - 1) * size).limit(size)
    return {"total": total, "records": [strip_private(c) async for c in cursor]}


# =========================================================
# KYC FILES
# =========================================================
@router.post("/kyc/upload")
async def upload_kyc_file(
    file: UploadFile = File(...),
    kind: str = Form("selfie", description="Which document this is: front, back or selfie"),
    current_user: dict = Depends(auth_guard),
):
    """
    Upload a KYC document (JPG, PNG or PDF, max 2 MB - Wasabi's limit).
    `kind` tells the journey tracker which slot the file fills.
    """
    if kind not in KYC_KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(KYC_KINDS)}")
    if file.content_type not in ALLOWED_UPLOADS:
        raise HTTPException(415, "Only JPG, PNG and PDF files are supported")
    file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File exceeds the 2 MB limit")
    if not any(file_bytes.startswith(sig) for sig in ALLOWED_UPLOADS[file.content_type]):
        raise HTTPException(415, "File content does not match its declared type")

    response = await wasabi.upload_wasabi_file(file_bytes, file.filename, file.content_type)
    file_id = _extract_file_id(response)

    if file_id:
        await kyc_documents_collection.update_one(
            {"owner_id": current_user["id"], "file_id": file_id},
            {"$setOnInsert": {"created_at": datetime.utcnow()},
             "$set": {"owner_id": current_user["id"], "file_id": file_id, "kind": kind,
                      "filename": file.filename, "updated_at": datetime.utcnow()}},
            upsert=True,
        )
        await publish_journey_update(current_user["id"], f"kyc_{kind}_uploaded")

    result = dict(response)
    result["kind"] = kind
    result["fileId"] = file_id
    return result


@router.post("/kyc/upload-bulk")
async def upload_kyc_bulk(
    front: UploadFile = File(..., description="Front of ID"),
    back: Optional[UploadFile] = File(None, description="Back of ID (not needed for passport)"),
    selfie: UploadFile = File(..., description="Selfie with ID"),
    current_user: dict = Depends(auth_guard),
):
    """
    Upload all three KYC documents in a single request.
    Returns a payload dictionary with the three Wasabi file IDs.
    """
    results = {}
    for kind, upload in (("front", front), ("back", back), ("selfie", selfie)):
        if upload is None:
            results[kind] = {"fileId": None, "fileName": None}
            continue
        if kind not in KYC_KINDS:
            raise HTTPException(422, f"Unknown kind: {kind}")

        if upload.content_type not in ALLOWED_UPLOADS:
            raise HTTPException(415, f"{kind}: only JPG, PNG and PDF are supported")

        file_bytes = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{kind}: file exceeds the 2 MB limit")
        if not any(file_bytes.startswith(sig) for sig in ALLOWED_UPLOADS[upload.content_type]):
            raise HTTPException(415, f"{kind}: file content does not match its declared type")

        response = await wasabi.upload_wasabi_file(file_bytes, upload.filename, upload.content_type)
        file_id = _extract_file_id(response)

        if file_id:
            await kyc_documents_collection.update_one(
                {"owner_id": current_user["id"], "file_id": file_id},
                {"$setOnInsert": {"created_at": datetime.utcnow()},
                 "$set": {"owner_id": current_user["id"], "file_id": file_id, "kind": kind,
                          "filename": upload.filename, "updated_at": datetime.utcnow()}},
                upsert=True,
            )
            await publish_journey_update(current_user["id"], f"kyc_{kind}_uploaded")

        results[kind] = {"fileId": file_id, "fileName": upload.filename}

    return {
        "success": True,
        "data": {
            "frontId": results.get("front", {}).get("fileId"),
            "backId": results.get("back", {}).get("fileId"),
            "selfieId": results.get("selfie", {}).get("fileId"),
            "details": results,
        },
    }


# =========================================================
# CARDHOLDERS
# =========================================================
@router.get("/kyc/documents")
async def list_kyc_documents(current_user: dict = Depends(auth_guard)):
    """
    The caller's uploaded KYC documents, with the file ids Wasabi needs for
    idFrontId / idBackId / idHoldId. Used to pre-fill the KYC form.
    """
    cursor = kyc_documents_collection.find({"owner_id": current_user["id"]}).sort("created_at", 1)
    records = [
        {"kind": doc.get("kind"), "fileId": doc.get("file_id"),
         "filename": doc.get("filename"), "createdAt": doc.get("created_at")}
        async for doc in cursor
    ]
    return {"records": records}


@router.post("/cardholder/register")
async def register_holder(
    payload: CreateCardholderRequest,
    request: Request,
    key: str = Depends(idempotency_key),
    current_user: dict = Depends(auth_guard),
):
    order_no = order_no_for("HLD", current_user, key)
    data = payload.model_dump(exclude_none=True)
    data["merchantOrderNo"] = order_no
    data["ipAddress"] = client_ip(request)  # real client IP, never a placeholder
    result = await service.register_holder(current_user["id"], order_no, data)
    await publish_journey_update(current_user["id"], "kyc_submitted")
    return result


@router.put("/cardholder/{holder_id}")
async def update_holder(
    holder_id: int,
    payload: CreateCardholderRequest,
    request: Request,
    key: str = Depends(idempotency_key),
    current_user: dict = Depends(auth_guard),
):
    """
    Correct and resubmit a REJECTED cardholder's KYC (Wasabi v2 update).
    Only a rejected holder can be updated - Wasabi rejects updates in any other status.
    """
    order_no = order_no_for("HLD", current_user, key)
    data = payload.model_dump(exclude_none=True)
    data["merchantOrderNo"] = order_no
    data["ipAddress"] = client_ip(request)
    return await service.update_holder(current_user["id"], holder_id, order_no, data)


@router.get("/cardholder/{holder_id}")
async def get_holder(holder_id: int, refresh: bool = False, current_user: dict = Depends(auth_guard)):
    holder = await service.get_owned_holder(holder_id, current_user["id"])
    if refresh:
        await service.refresh_holder(holder_id)
        holder = await service.get_owned_holder(holder_id, current_user["id"])
    result = strip_private(holder)
    result["review"] = service.holder_review_summary(holder)
    return result


@router.get("/cardholders")
async def my_holders(current_user: dict = Depends(auth_guard)):
    cursor = holders_collection.find({"owner_id": current_user["id"]}).sort("created_at", -1)
    records = []
    async for h in cursor:
        item = strip_private(h)
        item["review"] = service.holder_review_summary(h)
        records.append(item)
    return {"records": records}


# =========================================================
# KYC WORK TICKET
# =========================================================
@router.post("/kyc/submit")
async def submit_kyc(
    payload: KYCSubmissionRequest,
    key: str = Depends(idempotency_key),
    current_user: dict = Depends(auth_guard),
):
    if payload.cardNo:
        await service.get_owned_card(payload.cardNo, current_user["id"])
    order_no = order_no_for("WRK", current_user, key)
    return await service.process_work_order(current_user["id"], order_no, payload.model_dump(exclude_none=True))


# =========================================================
# CARD CREATION (spends merchant funds -> gated by LIVE_MONEY_ENABLED)
# =========================================================
@router.post("/create")
async def create(
    payload: CreateCardRequest,
    key: str = Depends(idempotency_key),
    current_user: dict = Depends(auth_guard),
):
    result = await service.issue_card(current_user["id"], order_no_for("CRD", current_user, key), payload)
    await publish_journey_update(current_user["id"], "card_requested")
    return result


# =========================================================
# READS (ownership-checked)
# =========================================================
@router.post("/info")
async def info(payload: CardNoRequest, current_user: dict = Depends(auth_guard)):
    await service.get_owned_card(payload.cardNo, current_user["id"])
    response = await wasabi.card_info({"cardNo": payload.cardNo, "onlySimpleInfo": False})
    await service.refresh_card(payload.cardNo)
    return response


@router.post("/balance")
async def balance(payload: CardNoRequest, current_user: dict = Depends(auth_guard)):
    await service.get_owned_card(payload.cardNo, current_user["id"])
    return await wasabi.card_balance({"cardNo": payload.cardNo})


@router.post("/sensitive")
async def sensitive(payload: CardNoRequest, current_user: dict = Depends(auth_guard)):
    """Returns Wasabi's encrypted PAN/CVV/expiry, decrypted with the merchant's private key."""
    await service.get_owned_card(payload.cardNo, current_user["id"])
    return await wasabi.card_sensitive_decrypt({"cardNo": payload.cardNo})


# =========================================================
# CARD OPERATIONS (async at Wasabi: final state arrives via webhook / reconcile)
# =========================================================
@router.post("/freeze")
async def freeze(payload: CardNoRequest, key: str = Depends(idempotency_key), current_user: dict = Depends(auth_guard)):
    return await service.card_operation(
        current_user["id"], order_no_for("FRZ", current_user, key), "FREEZE", payload.cardNo, wasabi.freeze_card,
        client_remark=payload.clientRemark,
    )


@router.post("/unfreeze")
async def unfreeze(payload: CardNoRequest, key: str = Depends(idempotency_key), current_user: dict = Depends(auth_guard)):
    return await service.card_operation(
        current_user["id"], order_no_for("UFZ", current_user, key), "UNFREEZE", payload.cardNo, wasabi.unfreeze_card,
        client_remark=payload.clientRemark,
    )


@router.post("/cancel")
async def cancel(payload: CardNoRequest, key: str = Depends(idempotency_key), current_user: dict = Depends(auth_guard)):
    return await service.card_operation(
        current_user["id"], order_no_for("CNL", current_user, key), "CANCEL", payload.cardNo, wasabi.cancel_card,
        client_remark=payload.clientRemark,
    )


@router.post("/deposit")
async def deposit_to_card(payload: CardAmountRequest, key: str = Depends(idempotency_key),
                          current_user: dict = Depends(auth_guard)):
    service.require_funding_enabled(payload.amount)
    card = await service.get_owned_card(payload.cardNo, current_user["id"])
    card_type = await service.find_card_type(card.get("card_type_id"))
    quote = pricing.quote_debit(payload.amount, **pricing.card_type_fees(card_type))
    return await service.card_operation(
        current_user["id"], order_no_for("DEP", current_user, key), "DEPOSIT",
        payload.cardNo, wasabi.deposit, payload.amount,
        funding={"kind": "debit", "kes_minor": quote["kes_minor"], "quote": quote},
        client_remark=payload.clientRemark,
    )


@router.post("/withdraw")
async def withdraw_from_card(payload: CardAmountRequest, key: str = Depends(idempotency_key),
                             current_user: dict = Depends(auth_guard)):
    service.require_funding_enabled(payload.amount)

    # Look up the card's type to get Wasabi's fee rate + balance retention quota.
    card = await service.get_owned_card(payload.cardNo, current_user["id"])
    card_type = await service.find_card_type(card.get("card_type_id"))
    retention_quota = None
    if card_type and card_type.get("balanceRetentionQuota") is not None:
        retention_quota = Decimal(str(card_type["balanceRetentionQuota"]))

    # Balance retention: Wasabi requires a minimum balance to remain on the card after withdrawal.
    if retention_quota is not None and retention_quota > 0:
        balance_resp = await wasabi.card_balance({"cardNo": payload.cardNo})
        card_balance = Decimal(str((balance_resp.get("data") or {}).get("amount", 0)))
        if card_balance - payload.amount < retention_quota:
            raise HTTPException(422, f"Withdrawal would breach the minimum balance retention of {retention_quota} USD")

    quote = pricing.quote_credit(payload.amount, **pricing.card_type_fees(card_type))
    return await service.card_operation(
        current_user["id"], order_no_for("WTH", current_user, key), "WITHDRAW",
        payload.cardNo, wasabi.withdraw, payload.amount,
        funding={"kind": "credit", "kes_minor": quote["kes_minor"], "quote": quote},
        client_remark=payload.clientRemark,
    )
