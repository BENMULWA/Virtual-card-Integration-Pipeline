# =========================================================
# wasabi_client.py - Wasabi API transport + endpoint wrappers
# =========================================================
#
# Error model (callers must treat these differently):
#   WasabiError           - Wasabi answered and said no (definitive failure, safe to mark FAILED).
#   WasabiUnknownOutcome  - timeout / network / 5xx / unparsable body. The request MAY have been
#                           processed. Keep the operation PENDING and reconcile; never blindly retry.

import logging
from typing import Optional

import httpx

from app.core.config import WASABI_API_KEY, WASABI_BASE_URL, WASABI_TIMEOUT_SECONDS
from app.core.signature import canonical_json, decrypt_message, generate_signature

logger = logging.getLogger("wasabi")


class WasabiError(Exception):
    def __init__(self, code, message, response: Optional[dict] = None):
        super().__init__(f"Wasabi error {code}: {message}")
        self.code = code
        self.message = message
        self.response = response or {}


class WasabiUnknownOutcome(Exception):
    pass


_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=WASABI_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        )
    return _client


async def close_client():
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def clean_payload(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if v is not None}


def _parse(response: httpx.Response, endpoint: str) -> dict:
    if response.status_code >= 500:
        raise WasabiUnknownOutcome(f"{endpoint}: HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise WasabiUnknownOutcome(f"{endpoint}: non-JSON response (HTTP {response.status_code})")
    if not isinstance(body, dict):
        raise WasabiUnknownOutcome(f"{endpoint}: unexpected response shape")
    if response.status_code >= 400 or not body.get("success"):
        raise WasabiError(body.get("code", response.status_code), body.get("msg"), body)
    return body


async def post(endpoint: str, payload: Optional[dict] = None) -> dict:
    """Signed JSON POST. The exact serialized string is what gets signed and what gets sent."""
    clean = clean_payload(payload or {})
    signature, body = generate_signature(clean)
    headers = {
        "Content-Type": "application/json",
        "X-WSB-API-KEY": WASABI_API_KEY,
        "X-WSB-SIGNATURE": signature,
    }

    order_ref = clean.get("merchantOrderNo", "-")
    try:
        response = await _get_client().post(f"{WASABI_BASE_URL}{endpoint}", content=body, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("wasabi %s order=%s unknown outcome: %s", endpoint, order_ref, type(exc).__name__)
        raise WasabiUnknownOutcome(f"{endpoint}: {type(exc).__name__}") from exc

    # Never log bodies: they carry KYC data and card details.
    logger.info("wasabi %s order=%s http=%s", endpoint, order_ref, response.status_code)
    return _parse(response, endpoint)


# =========================================================
# COMMON
# =========================================================
async def get_card_types():
    return await post("/merchant/core/mcb/card/v2/cardTypes", {})


async def get_wasabi_cities(region_code: str = "KE"):
    return await post("/merchant/core/mcb/common/city", {"regionCode": str(region_code).upper()})


async def get_occupations():
    return await post("/merchant/core/mcb/card/holder/occupations", {})


async def upload_wasabi_file(file_bytes: bytes, filename: str, content_type: str):
    signature, _ = generate_signature({})
    headers = {"X-WSB-API-KEY": WASABI_API_KEY, "X-WSB-SIGNATURE": signature}
    try:
        response = await _get_client().post(
            f"{WASABI_BASE_URL}/merchant/core/mcb/common/file/upload",
            headers=headers,
            data={"category": "card"},
            files={"file": (filename, file_bytes, content_type)},
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        raise WasabiUnknownOutcome(f"file upload: {type(exc).__name__}") from exc
    return _parse(response, "file upload")


async def submit_kyc_work_order(payload: dict):
    return await post("/merchant/core/mcb/work/submit", payload)


# =========================================================
# CARDHOLDERS
# =========================================================
async def create_cardholder(payload: dict):
    return await post("/merchant/core/mcb/card/holder/v2/create", payload)


async def update_cardholder(payload: dict):
    return await post("/merchant/core/mcb/card/holder/v2/update", payload)


async def query_cardholders(payload: dict):
    return await post("/merchant/core/mcb/card/holder/query", payload)


# =========================================================
# CARDS
# =========================================================
async def create_card(payload: dict):
    return await post("/merchant/core/mcb/card/v2/createCard", payload)


async def card_info(payload: dict):
    return await post("/merchant/core/mcb/card/info", payload)


async def card_sensitive(payload: dict):
    return await post("/merchant/core/mcb/card/sensitive", payload)


async def card_sensitive_decrypt(payload: dict) -> dict:
    """
    Fetch Wasabi's encrypted card details (PAN/CVV/expiry/activateUrl) and decrypt
    each field with the merchant's RSA private key. Returns plaintext fields.
    """
    response = await card_sensitive(payload)
    data = response.get("data") or {}
    decrypted = {}
    for field in ("cardNumber", "cvv", "expireDate", "activateUrl"):
        if data.get(field):
            try:
                decrypted[field] = decrypt_message(data[field])
            except ValueError:
                # Leave the field encrypted if decryption fails; caller can decide.
                decrypted[field] = data[field]
    decrypted["cardNo"] = payload.get("cardNo")
    return {"success": True, "data": decrypted}


async def card_balance(payload: dict):
    return await post("/merchant/core/mcb/card/balanceInfo", payload)


async def card_list(payload: dict):
    return await post("/merchant/core/mcb/card/list", payload)


async def card_operations(payload: dict):
    """Card operation transactions v2 (create/deposit/withdraw/Freeze/UnFreeze/cancel ...)."""
    return await post("/merchant/core/mcb/card/v2/transaction", payload)


async def freeze_card(payload: dict):
    return await post("/merchant/core/mcb/card/v2/freeze", payload)


async def unfreeze_card(payload: dict):
    return await post("/merchant/core/mcb/card/v2/unfreeze", payload)


async def deposit(payload: dict):
    return await post("/merchant/core/mcb/card/deposit", payload)


async def withdraw(payload: dict):
    return await post("/merchant/core/mcb/card/withdraw", payload)


async def cancel_card(payload: dict):
    return await post("/merchant/core/mcb/card/cancel", payload)


# =========================================================
# MERCHANT ACCOUNT (our own balance at Wasabi: this is what card issuance and card deposits spend)
# =========================================================
async def account_info():
    """Merchant accounts with total / available / frozen balance per currency."""
    return await post("/merchant/core/mcb/account/info", {})
