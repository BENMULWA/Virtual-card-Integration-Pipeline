# =========================================================
# mpesa/router.py - Daraja callback (unsigned -> secret URL + confirmed via STK Query)
# =========================================================

import hmac
import logging

from fastapi import APIRouter, HTTPException, Request

from app.core import config
from app.mpesa import topup

logger = logging.getLogger("mpesa")
router = APIRouter()

ACK = {"ResultCode": 0, "ResultDesc": "Accepted"}


@router.post("/callback/{secret}")
async def stk_callback(secret: str, request: Request):
    if not config.MPESA_CALLBACK_SECRET or not hmac.compare_digest(secret, config.MPESA_CALLBACK_SECRET):
        raise HTTPException(404)  # indistinguishable from a missing route
    try:
        body = await request.json()
    except ValueError:
        return ACK
    hint = topup.parse_callback(body)
    if hint:
        try:
            await topup.settle(hint["checkout_request_id"], hint)
        except Exception:  # noqa: BLE001 - never make Safaricom retry-storm; refresh/reconcile will settle
            logger.exception("topup settle failed for %s", hint["checkout_request_id"])
    return ACK
