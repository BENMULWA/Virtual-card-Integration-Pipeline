# =========================================================
# mpesa/client.py - Safaricom Daraja (OAuth, STK Push, STK Query)
# =========================================================

import base64
import logging
import time
from datetime import datetime
from typing import Optional

import httpx

from app.core import config

logger = logging.getLogger("mpesa")


class MpesaError(Exception):
    """Daraja rejected the request (definitive)."""


class MpesaUnknownOutcome(Exception):
    """Timeout / 5xx: the prompt may or may not have been sent."""


_http: Optional[httpx.AsyncClient] = None
_token = {"value": None, "expires": 0.0}


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=30)
    return _http


async def close_client():
    global _http
    if _http is not None and not _http.is_closed:
        await _http.aclose()
    _http = None


def configured() -> bool:
    return all([config.MPESA_CONSUMER_KEY, config.MPESA_CONSUMER_SECRET, config.MPESA_SHORTCODE,
                config.MPESA_PASSKEY, config.MPESA_CALLBACK_BASE_URL, config.MPESA_CALLBACK_SECRET])


def callback_url() -> str:
    return f"{config.MPESA_CALLBACK_BASE_URL}/api/v1/mpesa/callback/{config.MPESA_CALLBACK_SECRET}"


def _password(timestamp: str) -> str:
    return base64.b64encode(f"{config.MPESA_SHORTCODE}{config.MPESA_PASSKEY}{timestamp}".encode()).decode()


async def _access_token() -> str:
    if _token["value"] and time.time() < _token["expires"] - 30:
        return _token["value"]
    try:
        r = await _client().get(f"{config.MPESA_BASE_URL}/oauth/v1/generate",
                                params={"grant_type": "client_credentials"},
                                auth=(config.MPESA_CONSUMER_KEY, config.MPESA_CONSUMER_SECRET))
    except httpx.HTTPError as exc:
        raise MpesaUnknownOutcome(f"oauth: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise MpesaError(f"oauth failed (HTTP {r.status_code})")
    body = r.json()
    _token.update(value=body["access_token"], expires=time.time() + int(body.get("expires_in", 3599)))
    return _token["value"]


async def _post(path: str, payload: dict) -> dict:
    token = await _access_token()
    try:
        r = await _client().post(f"{config.MPESA_BASE_URL}{path}", json=payload,
                                 headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise MpesaUnknownOutcome(f"{path}: {type(exc).__name__}") from exc
    if r.status_code >= 500:
        raise MpesaUnknownOutcome(f"{path}: HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        raise MpesaUnknownOutcome(f"{path}: non-JSON response")
    if r.status_code >= 400:
        raise MpesaError(f"{path}: {body.get('errorCode')} {body.get('errorMessage')}")
    return body


async def stk_push(phone: str, amount_kes: int, account_ref: str, description: str) -> dict:
    """phone is 2547XXXXXXXX; amount_kes is whole shillings (Daraja rejects fractions)."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    body = await _post("/mpesa/stkpush/v1/processrequest", {
        "BusinessShortCode": config.MPESA_SHORTCODE,
        "Password": _password(timestamp),
        "Timestamp": timestamp,
        "TransactionType": config.MPESA_TRANSACTION_TYPE,
        "Amount": amount_kes,
        "PartyA": phone,
        "PartyB": config.MPESA_PARTY_B,
        "PhoneNumber": phone,
        "CallBackURL": callback_url(),
        "AccountReference": account_ref[:12],
        "TransactionDesc": description[:13],
    })
    if str(body.get("ResponseCode")) != "0":
        raise MpesaError(f"stk push rejected: {body.get('ResponseDescription')}")
    return body  # MerchantRequestID, CheckoutRequestID


async def stk_query(checkout_request_id: str) -> dict:
    """Authoritative result of a prompt. ResultCode '0' = paid; other codes are final failures."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return await _post("/mpesa/stkpushquery/v1/query", {
        "BusinessShortCode": config.MPESA_SHORTCODE,
        "Password": _password(timestamp),
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    })
