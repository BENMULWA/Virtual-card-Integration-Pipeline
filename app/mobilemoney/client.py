# =========================================================
# mobilemoney/client.py - Mamlaka payments gateway (collections, payouts, status, callback signature)
# =========================================================
#
# Error model (same as the Wasabi and Daraja clients):
#   GatewayError           - the gateway answered and refused (definitive: safe to mark FAILED).
#   GatewayUnknownOutcome  - timeout / network / 5xx / unparsable body. The request MAY have been processed:
#                            keep the operation PENDING and settle it from the callback or a status query.

import hashlib
import hmac
import logging
import time
from datetime import datetime
from typing import Optional

import httpx

from app.core import config

logger = logging.getLogger("gateway")

# Values the gateway expects in mobileMoneySP. Confirm against the sandbox if a provider is rejected.
PROVIDERS = {"mpesa": "M-Pesa", "airtel": "Airtel"}


class GatewayError(Exception):
    pass


class GatewayUnknownOutcome(Exception):
    pass


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
    _token.update(value=None, expires=0.0)


def configured() -> bool:
    return all([config.AIRTEL_API_BASE_URL, config.AIRTEL_API_USERNAME, config.AIRTEL_API_PASSWORD,
                config.AIRTEL_CALLBACK_BASE_URL, config.AIRTEL_CALLBACK_SIGNING_SECRET])


def callback_url() -> str:
    return f"{config.AIRTEL_CALLBACK_BASE_URL}/api/v1/webhooks/mamlaka"


def provider_name(provider: str) -> str:
    try:
        return PROVIDERS[(provider or "").lower()]
    except KeyError:
        raise GatewayError(f"Unsupported provider: {provider}")


def verify_signature(raw_body: bytes, header: str, secret: str) -> bool:
    """`X-Mamlaka-Signature: sha256=<hex HMAC-SHA256 of the exact raw body>`."""
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.strip().encode(), expected.encode())


def _expiry(body: dict) -> float:
    raw = str(body.get("expires_at") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time() + 600


async def _access_token(force: bool = False) -> str:
    if not force and _token["value"] and time.time() < _token["expires"] - 30:
        return _token["value"]
    try:
        r = await _client().get(f"{config.AIRTEL_API_BASE_URL}/",
                                auth=(config.AIRTEL_API_USERNAME, config.AIRTEL_API_PASSWORD), follow_redirects=True)
    except httpx.HTTPError as exc:
        raise GatewayUnknownOutcome(f"auth: {type(exc).__name__}") from exc
    if r.status_code >= 500:
        raise GatewayUnknownOutcome(f"auth: HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        raise GatewayUnknownOutcome("auth: non-JSON response")
    token = body.get("token")
    if r.status_code != 200 or not token:
        raise GatewayError(f"gateway login failed (HTTP {r.status_code})")
    _token.update(value=token, expires=_expiry(body))
    return token


async def _request(method: str, path: str, payload: Optional[dict] = None, params: Optional[dict] = None,
                   retry: bool = True) -> dict:
    token = await _access_token()
    try:
        r = await _client().request(method, f"{config.AIRTEL_API_BASE_URL}{path}", json=payload, params=params,
                                    headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise GatewayUnknownOutcome(f"{path}: {type(exc).__name__}") from exc
    if r.status_code == 401 and retry:
        await _access_token(force=True)
        return await _request(method, path, payload, params, retry=False)
    if r.status_code >= 500:
        raise GatewayUnknownOutcome(f"{path}: HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        raise GatewayUnknownOutcome(f"{path}: non-JSON response")
    if r.status_code >= 400:
        # Never log the body: it can carry phone numbers.
        logger.warning("gateway %s refused: HTTP %s", path, r.status_code)
        raise GatewayError(str(body.get("message") or body.get("error") or f"HTTP {r.status_code}"))
    return body


async def initiate_collection(phone: str, amount_kes: int, provider: str, external_id: str) -> dict:
    """STK prompt on the customer's phone. phone is 2547XXXXXXXX. Returns the gateway body (secureId, ...)."""
    return await _request("POST", "/mobile/initiate", {
        "impalaMerchantId": config.AIRTEL_API_USERNAME,
        "displayName": "Mamlaka Wallet",
        "currency": "KES",
        "amount": amount_kes,
        "payerPhone": phone,
        "mobileMoneySP": provider_name(provider),
        "externalId": external_id,
        "callbackUrl": callback_url(),
    })


async def initiate_transfer(phone: str, amount_kes: int, provider: str, external_id: str) -> dict:
    """B2C payout from the merchant float to the customer's phone."""
    return await _request("POST", "/mobile/transfer", {
        "impalaMerchantId": config.AIRTEL_API_USERNAME,
        "currency": "KES",
        "amount": amount_kes,
        "recipientPhone": phone,
        "mobileMoneySP": provider_name(provider),
        "externalId": external_id,
        "callbackUrl": callback_url(),
    })


def normalize_status(payload: dict) -> dict:
    """Flatten a callback or status body: the fields may sit at the top level or under `transaction`."""
    txn = payload.get("transaction") if isinstance(payload.get("transaction"), dict) else payload
    raw = str(txn.get("transactionStatus") or txn.get("transaction_status") or txn.get("status") or "").upper()
    state = "SUCCESS" if raw in ("COMPLETE", "COMPLETED", "SUCCESS") else \
            "FAILED" if raw in ("FAILED", "FAIL", "CANCELLED", "CANCELED") else "PENDING"
    amount = txn.get("amount")
    return {
        "external_id": txn.get("externalId") or txn.get("external_id"),
        "secure_id": txn.get("secureId") or txn.get("secure_id"),
        "amount": amount,
        "state": state,
        "receipt": txn.get("transactionReceipt") or txn.get("transaction_receipt") or txn.get("reference"),
        "reason": txn.get("reason") or txn.get("transactionReport") or txn.get("transaction_report"),
    }


async def get_status(secure_id: str) -> dict:
    """Authoritative state of one transaction, used when a callback never arrived."""
    body = await _request("GET", "/transaction", params={"merchant": config.AIRTEL_API_USERNAME, "secureId": secure_id})
    return normalize_status(body)
