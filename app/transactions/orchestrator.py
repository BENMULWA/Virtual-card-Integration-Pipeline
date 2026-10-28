# =========================================================
# orchestrator.py - idempotent provider-operation lifecycle
# =========================================================
#
# Every Wasabi mutation is recorded BEFORE the call, keyed by a deterministic merchant_order_no
# (derived from owner + client Idempotency-Key). A unique index on merchant_order_no means a
# retried request finds the existing record instead of calling Wasabi a second time.

import hashlib
import uuid
from datetime import datetime
from typing import Optional

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.database import transactions_collection

INITIATED = "INITIATED"   # recorded locally, provider not yet answered
PENDING = "PENDING"       # provider accepted, final result via webhook/reconcile
UNKNOWN = "UNKNOWN"       # timeout/5xx: provider may have processed it - reconcile, do not resend
SUCCESS = "SUCCESS"
FAILED = "FAILED"
FINAL_STATUSES = {SUCCESS, FAILED}

_PROVIDER_STATUS = {
    "wait_process": PENDING,
    "processing": PENDING,
    "success": SUCCESS,
    "fail": FAILED,
}


def map_provider_status(status: Optional[str]) -> str:
    return _PROVIDER_STATUS.get((status or "").lower(), PENDING)


def build_order_no(prefix: str, owner_id: str, idempotency_key: str) -> str:
    """Deterministic, 27 chars for a 3-char prefix (within Wasabi's 20-40 / 15-65 limits)."""
    digest = hashlib.sha256(f"{owner_id}:{prefix}:{idempotency_key}".encode()).hexdigest()[:24].upper()
    return f"{prefix}{digest}"


async def begin_operation(merchant_order_no: str, tx_type: str, owner_id: str, summary: dict,
                          funding: Optional[dict] = None):
    """Returns (record, created). created=False means this idempotency key was already used."""
    record = {
        "transaction_id": str(uuid.uuid4()),
        "merchant_order_no": merchant_order_no,
        "type": tx_type,
        "owner_id": owner_id,
        "status": INITIATED,
        "summary": summary,  # non-sensitive request facts only (never KYC data or card details)
        "funding": funding,  # {"kind": "debit"|"credit", "kes_minor": int} - wallet effect of this op
        "funding_settled": False,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    try:
        await transactions_collection.insert_one(record)
        return record, True
    except DuplicateKeyError:
        existing = await transactions_collection.find_one({"merchant_order_no": merchant_order_no})
        return existing, False


async def finish_operation(merchant_order_no: str, status: str, provider_data: Optional[dict] = None):
    """Returns the updated record, or None if it was already final (so callers settle funding once)."""
    update = {"status": status, "updated_at": datetime.utcnow()}
    if provider_data is not None:
        update["provider_data"] = provider_data
    # Never move a final record backwards (e.g. a late PENDING after a webhook already said SUCCESS).
    return await transactions_collection.find_one_and_update(
        {"merchant_order_no": merchant_order_no, "status": {"$nin": list(FINAL_STATUSES)}},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )


async def get_operation(merchant_order_no: str):
    return await transactions_collection.find_one({"merchant_order_no": merchant_order_no})
