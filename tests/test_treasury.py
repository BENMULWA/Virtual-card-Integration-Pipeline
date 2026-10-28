from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException

import app.core.database as database
from app.cards import wasabi_client as wc
from app.core import config, treasury
from app.main import app
from app.partners import service as partners
from app.wallet import ledger

REAL_CLIENT = httpx.AsyncClient
ACCOUNTS = {"success": True, "data": [
    {"accountId": "1", "accountType": "WALLET", "currency": "USD", "totalBalance": "50", "availableBalance": "40", "frozenBalance": "10"},
    {"accountId": "2", "accountType": "MARGIN", "currency": "EUR", "totalBalance": "99", "availableBalance": "99", "frozenBalance": "0"}]}


@pytest.fixture
def wasabi_balance(monkeypatch):
    """Control what Wasabi says our balance is. calls counts how often it is asked."""
    state = {"response": ACCOUNTS, "calls": 0}

    async def account_info():
        state["calls"] += 1
        if isinstance(state["response"], Exception):
            raise state["response"]
        return state["response"]

    monkeypatch.setattr(wc, "account_info", account_info)
    treasury.forget_balance()
    return state


async def float_credit(kes):
    tenant, key = await partners.create_tenant("T", f"t{kes}".replace(".", "-"))
    await ledger.credit(partners.float_owner(tenant["tenant_id"]), int(kes * 100), ref=f"f-{kes}", kind="float_topup")
    return tenant


# ------------------------------------------------------------------ reading the balance
def test_only_available_balance_in_the_configured_currency_counts():
    assert treasury.parse_available_usd(ACCOUNTS["data"]) == Decimal("40")      # not frozen, not EUR
    assert treasury.parse_available_usd([]) == Decimal(0)
    assert treasury.parse_available_usd(None) == Decimal(0)


async def test_balance_is_cached_briefly_and_unknown_is_none_not_zero(wasabi_balance):
    assert await treasury.available_usd() == Decimal("40")
    assert await treasury.available_usd() == Decimal("40")
    assert wasabi_balance["calls"] == 1
    treasury.forget_balance()
    wasabi_balance["response"] = wc.WasabiUnknownOutcome("down")
    assert await treasury.available_usd() is None


# ------------------------------------------------------------------ the pre-flight
async def test_a_request_the_balance_cannot_cover_is_refused_cleanly(wasabi_balance):
    await treasury.require_balance(Decimal("40"))                               # exactly enough: allowed
    with pytest.raises(HTTPException) as exc:
        await treasury.require_balance(Decimal("40.01"))
    assert exc.value.status_code == 503 and "unavailable" in exc.value.detail.lower()
    assert "40" not in exc.value.detail                                         # never leaks our balance


async def test_the_reserve_is_never_spent(wasabi_balance, monkeypatch):
    monkeypatch.setattr(config, "TREASURY_RESERVE_USD", Decimal("35"))
    await treasury.require_balance(Decimal("5"))
    with pytest.raises(HTTPException):
        await treasury.require_balance(Decimal("6"))


async def test_when_wasabi_cannot_be_asked_the_request_is_not_blocked(wasabi_balance):
    wasabi_balance["response"] = wc.WasabiUnknownOutcome("down")
    await treasury.require_balance(Decimal("1000"))                             # fails open: Wasabi's own refusal is still handled


# ------------------------------------------------------------------ end to end: a drained Wasabi balance
async def issue(api_headers, key):
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/api/v1/partner/customers", headers=api_headers,
                     json={"externalId": "u-1", "fullName": "Jane Doe", "email": "j@d.co", "phone": "0712345678"})
        return await c.post("/api/v1/partner/customers/u-1/cards", headers={**api_headers, "Idempotency-Key": key},
                            json={"cardTypeId": 8, "amount": "10"})


@pytest.fixture
async def stack(fake_db, monkeypatch):
    for coll, field in [(database.transactions_collection, "merchant_order_no"),
                        (database.ledger_collection, "entry_key"), (database.wallets_collection, "owner_id"),
                        (database.tenants_collection, "slug"), (database.partner_customers_collection, "owner_id"),
                        (database.partner_events_collection, "event_id")]:
        await coll.create_index(field, unique=True)
    calls = {"create": 0}

    async def types(): return {"success": True, "data": [{"cardTypeId": 8, "status": "online", "needCardHolder": False, "cardPrice": "2"}]}
    async def create(payload):
        calls["create"] += 1
        return {"success": True, "data": {"cardNo": "C1", "status": "wait_process", "orderNo": "W1"}}
    monkeypatch.setattr(wc, "get_card_types", types)
    monkeypatch.setattr(wc, "create_card", create)
    tenant, key = await partners.create_tenant("Comet", "comet")
    await ledger.credit(partners.float_owner(tenant["tenant_id"]), 500_000, ref="seed", kind="float_topup")
    return tenant, {"X-API-Key": key}, calls


async def test_low_wasabi_balance_stops_issuance_before_anything_is_held(stack, wasabi_balance):
    tenant, headers, calls = stack
    wasabi_balance["response"] = {"success": True, "data": [{"currency": "USD", "availableBalance": "5"}]}
    r = await issue(headers, "low-bal-key1")
    assert r.status_code == 503
    assert calls["create"] == 0
    b = await ledger.get_balance(partners.float_owner(tenant["tenant_id"]))
    assert (b["available"], b["held"]) == (Decimal("5000.00"), Decimal("0.00"))    # nothing reserved from the partner


async def test_enough_balance_lets_issuance_through_and_a_replay_is_never_blocked(stack, wasabi_balance):
    tenant, headers, calls = stack
    wasabi_balance["response"] = {"success": True, "data": [{"currency": "USD", "availableBalance": "12"}]}
    assert (await issue(headers, "ok-bal-key-1")).status_code == 200
    wasabi_balance["response"] = {"success": True, "data": [{"currency": "USD", "availableBalance": "0"}]}
    treasury.forget_balance()
    again = await issue(headers, "ok-bal-key-1")                                   # same key, balance now empty
    assert again.status_code == 200 and again.json().get("duplicate") is True
    assert calls["create"] == 1


# ------------------------------------------------------------------ status + alerts
async def test_status_compares_real_usd_with_what_partners_prepaid(fake_db, wasabi_balance, monkeypatch):
    monkeypatch.setattr(config, "FX_KES_PER_USD", "100")
    monkeypatch.setattr(config, "TREASURY_ALERT_BELOW_USD", Decimal("20"))
    wasabi_balance["response"] = {"success": True, "data": [{"currency": "USD", "availableBalance": "500"}]}
    assert (await treasury.snapshot())["status"] == treasury.OK
    wasabi_balance["response"] = {"success": True, "data": [{"currency": "USD", "availableBalance": "15"}]}
    assert (await treasury.snapshot())["status"] == treasury.LOW
    await float_credit(5000)                                                         # 5,000 KES = 50 USD prepaid
    snap = await treasury.snapshot()
    assert snap["status"] == treasury.UNDERFUNDED and snap["partnerFloatsUsd"] == "50.00"
    wasabi_balance["response"] = wc.WasabiUnknownOutcome("down")
    assert (await treasury.snapshot())["status"] is None                            # unknown, not "empty"


async def test_alerts_fire_on_change_not_on_every_pass(fake_db, wasabi_balance, monkeypatch):
    sent = []

    async def alert(message): sent.append(message)
    monkeypatch.setattr(treasury, "_send_alert", alert)
    monkeypatch.setattr(config, "TREASURY_ALERT_BELOW_USD", Decimal("100"))
    low = {"success": True, "data": [{"currency": "USD", "availableBalance": "15"}]}
    fine = {"success": True, "data": [{"currency": "USD", "availableBalance": "900"}]}
    t0 = datetime.utcnow()

    wasabi_balance["response"] = low
    await treasury.check_and_alert(now=t0)
    assert len(sent) == 1 and "LOW" in sent[0]
    calls = wasabi_balance["calls"]
    await treasury.check_and_alert(now=t0 + timedelta(minutes=5))                    # throttled: no call, no alert
    assert (wasabi_balance["calls"], len(sent)) == (calls, 1)
    await treasury.check_and_alert(now=t0 + timedelta(minutes=20))                   # still low, inside the reminder window
    assert len(sent) == 1
    await treasury.check_and_alert(now=t0 + timedelta(hours=7))                      # reminder
    assert len(sent) == 2
    wasabi_balance["response"] = fine
    await treasury.check_and_alert(now=t0 + timedelta(hours=8))
    assert len(sent) == 3 and "Recovered" in sent[2]
    await treasury.check_and_alert(now=t0 + timedelta(hours=9))
    assert len(sent) == 3                                                            # healthy stays quiet


async def test_an_unreadable_balance_never_raises_an_alert(fake_db, wasabi_balance, monkeypatch):
    sent = []

    async def alert(message): sent.append(message)
    monkeypatch.setattr(treasury, "_send_alert", alert)
    wasabi_balance["response"] = wc.WasabiUnknownOutcome("down")
    await treasury.check_and_alert(now=datetime.utcnow())
    assert sent == []
