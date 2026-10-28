# =========================================================
# treasury.py - our real USD at Wasabi vs what partners have prepaid
# =========================================================
#
# Three different balances exist (see the money-flow notes):
#   * Wasabi merchant balance (USD, real)      - what card issuance / card deposits actually spend
#   * partner floats (KES, a ledger number)    - what partners have prepaid us
#   * customer wallets                         - not our money
# A partner float is only a promise unless real USD backs it. This module keeps the two honest:
#   require_balance()  refuses a request the Wasabi balance cannot cover, BEFORE any partner hold is taken
#   snapshot()         the numbers, with a status: OK | LOW | UNDERFUNDED
#   check_and_alert()  throttled check for the reconcile job; alerts on change, not on every pass

import logging
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

import httpx
from fastapi import HTTPException

from app.cards import wasabi_client as wasabi
from app.core import config, database

logger = logging.getLogger("treasury")

CACHE_SECONDS = 60.0
CHECK_EVERY = timedelta(minutes=15)      # each check is a call to Wasabi
REALERT_AFTER = timedelta(hours=6)       # while it stays bad, remind this often
OK, LOW, UNDERFUNDED = "OK", "LOW", "UNDERFUNDED"

_cache: dict = {"at": 0.0, "value": None}


def parse_available_usd(data) -> Decimal:
    """Sum the AVAILABLE (not frozen) balance of the accounts in the configured currencies."""
    rows = data if isinstance(data, list) else ([data] if data else [])
    total = Decimal(0)
    for account in rows:
        if str(account.get("currency", "")).upper() in config.TREASURY_CURRENCIES:
            total += Decimal(str(account.get("availableBalance") or 0))
    return total


async def available_usd(max_age: float = CACHE_SECONDS) -> Optional[Decimal]:
    """None when Wasabi cannot be asked: callers must not treat that as 'empty'."""
    if _cache["value"] is not None and time.monotonic() - _cache["at"] < max_age:
        return _cache["value"]
    try:
        value = parse_available_usd((await wasabi.account_info()).get("data"))
    except (wasabi.WasabiError, wasabi.WasabiUnknownOutcome) as exc:
        logger.warning("could not read the Wasabi balance: %s", type(exc).__name__)
        return None
    _cache.update(at=time.monotonic(), value=value)
    return value


def forget_balance():
    """Drop the cached balance (after we spend, or in tests)."""
    _cache.update(at=0.0, value=None)


async def require_balance(needed_usd: Decimal) -> None:
    """Refuse cleanly when the real balance cannot cover `needed_usd` (plus the reserve).
    Fails OPEN when the balance is unknown: Wasabi's own refusal is still handled (and refunded) downstream."""
    available = await available_usd()
    if available is None:
        return
    if available - config.TREASURY_RESERVE_USD < needed_usd:
        logger.error("issuance refused: Wasabi balance %s USD cannot cover %s USD (reserve %s)",
                     available, needed_usd, config.TREASURY_RESERVE_USD)
        raise HTTPException(503, "Card issuance is temporarily unavailable. Please try again later.")


# ---------------------------------------------------------------- the numbers
async def partner_floats_kes() -> Decimal:
    """Everything partners have prepaid and not yet spent (available + held), in KES."""
    from app.wallet import ledger

    minor = 0
    async for wallet in database.wallets_collection.find({"owner_id": {"$regex": "^float:"}}):
        minor += int(wallet.get("available", 0)) + int(wallet.get("held", 0))
    return ledger.from_minor(minor)


async def snapshot() -> dict:
    available = await available_usd(max_age=0)
    floats_kes = await partner_floats_kes()
    rate = Decimal(config.FX_KES_PER_USD) if config.FX_KES_PER_USD else None
    floats_usd = (floats_kes / rate).quantize(Decimal("0.01")) if rate else None
    if available is None:
        status = None
    elif floats_usd is not None and available < floats_usd:
        status = UNDERFUNDED   # partners have prepaid more than we hold at Wasabi: we cannot honour it all
    elif available < config.TREASURY_ALERT_BELOW_USD:
        status = LOW
    else:
        status = OK
    return {"status": status, "wasabiAvailableUsd": None if available is None else str(available),
            "partnerFloatsKes": str(floats_kes), "partnerFloatsUsd": None if floats_usd is None else str(floats_usd),
            "alertBelowUsd": str(config.TREASURY_ALERT_BELOW_USD), "reserveUsd": str(config.TREASURY_RESERVE_USD)}


# ---------------------------------------------------------------- alerts
async def _send_alert(message: str) -> None:
    logger.error("TREASURY ALERT: %s", message)
    if not config.ALERT_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(config.ALERT_WEBHOOK_URL, json={"text": f"[Mamlaka cards] {message}"})
    except httpx.HTTPError as exc:
        logger.warning("alert webhook failed: %s", type(exc).__name__)


def _describe(snap: dict) -> str:
    return (f"Wasabi balance {snap['wasabiAvailableUsd']} USD; partner floats {snap['partnerFloatsKes']} KES "
            f"(~{snap['partnerFloatsUsd']} USD); alert threshold {snap['alertBelowUsd']} USD.")


async def check_and_alert(now: Optional[datetime] = None) -> dict:
    """Throttled (each check calls Wasabi). Alerts when the status gets worse and every few hours while it stays
    bad, and says once when it recovers; it never repeats the same alert on every pass."""
    now = now or datetime.utcnow()
    state = await database.system_state_collection.find_one({"_id": "treasury"}) or {}
    last_checked = state.get("lastCheckedAt")
    if last_checked and now - last_checked < CHECK_EVERY:
        return {"status": state.get("lastStatus"), "checked": False}

    snap = await snapshot()
    status, previous = snap["status"], state.get("lastStatus")
    update = {"lastCheckedAt": now}
    if status is None:                                  # could not read Wasabi: keep the previous judgement
        await database.system_state_collection.update_one({"_id": "treasury"}, {"$set": update}, upsert=True)
        return {**snap, "checked": True}

    bad = status != OK
    if bad and (status != previous or not state.get("lastAlertAt") or now - state["lastAlertAt"] > REALERT_AFTER):
        await _send_alert(f"{status}: {_describe(snap)}")
        update["lastAlertAt"] = now
    elif not bad and previous in (LOW, UNDERFUNDED):
        await _send_alert(f"Recovered (OK): {_describe(snap)}")
        update["lastAlertAt"] = None
    update["lastStatus"] = status
    await database.system_state_collection.update_one({"_id": "treasury"}, {"$set": update}, upsert=True)
    return {**snap, "checked": True}
