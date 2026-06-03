# =========================================================
# router.py - Wasabi Webhook Receiver
# =========================================================

from fastapi import APIRouter, Request, HTTPException
from datetime import datetime

from app.core.database import transactions_collection

router = APIRouter()


# =========================================================
# WASABI WEBHOOK
# =========================================================
@router.post("/wasabi")
async def wasabi_webhook(request: Request):

    payload = await request.json()

    # Store raw webhook event
    await transactions_collection.insert_one({
        "type": "WEBHOOK",
        "payload": payload,
        "created_at": datetime.utcnow()
    })

    return {"success": True}