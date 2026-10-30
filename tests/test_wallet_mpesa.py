import json

import httpx
import pytest

from app.cards import wasabi_client as wc
from app.core import config
from app.core.security import create_access_token
from app.main import app
from app.mpesa import client as mc
from app.mpesa import topup
from app.transactions import orchestrator as ops
from app.wallet import ledger, pricing
import app.core.database as database
from decimal import Decimal

SECRET = "s3cret-callback"
CARD_TYPES = {"success": True, "data": [{"cardTypeId": 8, "status": "online", "needCardHolder": False}]}


def auth(user="alice"):
    return {"Authorization": f"Bearer {create_access_token({'sub': user})}"}


def idem(key):
    return {"Idempotency-Key": key}


@pytest.fixture
async def api(fake_db):
    for coll, field in [(database.transactions_collection, "merchant_order_no"),
                        (database.webhook_events_collection, "request_id"),
                        (database.ledger_collection, "entry_key"),
                        (database.topups_collection, "checkout_request_id")]:
        await coll.create_index(field, unique=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def balance(user="alice"):
    b = await ledger.get_balance(user)
    return b["available"], b["held"]


# ------------------------------------------------------------------ pricing
def test_quote_math():
    q = pricing.quote_debit(Decimal("10"))
    assert q["kes_minor"] == 132_600           # 10 USD * 130 * 1.02 = 1,326.00 KES
    assert pricing.quote_credit(Decimal("10"))["kes_minor"] == 127_400   # * 0.98 rounds down in our favour
    assert pricing.quote_debit(Decimal("10"), Decimal("1"))["kes_minor"] == 145_860


def test_wasabi_fees_are_our_cost_and_our_margin_goes_on_top():
    # Wasabi's terms (1% rate, 0.50 USD fixed) are a cost; our 2% spread is added on top, never replaced by them.
    ct = {"rechargeFeeRate": "1", "rechargeFixedFee": "0.5", "cardPrice": "2", "cardPriceCurrency": "USD"}
    q = pricing.quote_debit(Decimal("10"), **pricing.card_type_fees(ct))
    assert q["kes_minor"] == 133_926 and q["cost_kes"] == "1313.00"   # 10 * 1.01 * 130 = 1,313.00 cost, * 1.02
    assert Decimal(q["margin_kes"]) == Decimal("26.26")
    ct_free = {**ct, "rechargeFeeRate": "0", "rechargeFixedFee": "0"}
    assert pricing.quote_debit(Decimal("10"), **pricing.card_type_fees(ct_free))["kes_minor"] == 132_600
    issue = pricing.quote_issue(Decimal("10"), ct)
    assert issue["kes_minor"] == 167_342   # ((10 + 2 card price) * 1.01 + 0.50) USD * 130 * 1.02 = 1,673.41


def test_deposit_fee_raises_our_cost_of_a_dollar(monkeypatch):
    monkeypatch.setattr(config, "WASABI_DEPOSIT_FEE_PCT", Decimal("1.5"))
    monkeypatch.setattr(config, "FX_KES_PER_USD", "129.50")
    card = pricing.rate_card(Decimal("1"))
    assert card["costRate"] == "131.47" and card["kesPerUsd"] == "132.79"   # 129.50 / 0.985, then + 1%
    q = pricing.quote_debit(Decimal("10"), margin_pct=Decimal("0"))
    assert q["cost_kes"] == "1314.72" and q["margin_kes"] == "0.01"         # selling at cost: only rounding is kept


def test_withdrawals_are_valued_at_cost_less_margin():
    q = pricing.quote_credit(Decimal("10"))
    assert q["kes_minor"] == 127_400 and q["margin_kes"] == "26.00"   # 1,300 at cost, we keep 2%


# ------------------------------------------------------------------ ledger invariants
async def test_ledger_idempotent_and_overdraft_safe(api):
    assert await ledger.credit("u", 1000, "R1", "topup") is True
    assert await ledger.credit("u", 1000, "R1", "topup") is False        # replay: no double credit
    assert await balance("u") == (Decimal("10.00"), Decimal("0.00"))
    await ledger.reserve("u", 600, "OP1")
    with pytest.raises(ledger.InsufficientFunds):
        await ledger.reserve("u", 600, "OP2")                           # only 4.00 left
    assert await ledger.reserve("u", 600, "OP1") is False               # same ref twice: no second hold
    assert await ledger.release("OP1") is True and await ledger.release("OP1") is False
    assert await ledger.capture("OP1") is False                         # released holds cannot also be spent
    assert await balance("u") == (Decimal("10.00"), Decimal("0.00"))
    assert (await ledger.verify_balance("u"))["consistent"]


async def test_cannot_release_after_capture(api):
    await ledger.credit("u", 1000, "R1", "topup")
    await ledger.reserve("u", 500, "OP")
    await ledger.capture("OP")
    assert await ledger.release("OP") is False
    assert (await ledger.verify_balance("u"))["consistent"]


# ------------------------------------------------------------------ M-Pesa top-up
@pytest.fixture
def daraja(monkeypatch):
    state = {"pushes": 0, "query": {"ResultCode": "0", "ResultDesc": "ok"}}

    async def push(phone, amount, ref, desc):
        state["pushes"] += 1
        state["last"] = (phone, amount)
        return {"MerchantRequestID": "M1", "CheckoutRequestID": f"ws_CO_{state['pushes']}", "ResponseCode": "0"}

    async def query(checkout_id):
        if isinstance(state["query"], Exception):
            raise state["query"]
        return state["query"]

    monkeypatch.setattr(mc, "stk_push", push)
    monkeypatch.setattr(mc, "stk_query", query)
    return state


CALLBACK = lambda cid, amount=500, receipt="RCP123", code=0: {"Body": {"stkCallback": {
    "MerchantRequestID": "M1", "CheckoutRequestID": cid, "ResultCode": code, "ResultDesc": "x",
    "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": amount},
                                  {"Name": "MpesaReceiptNumber", "Value": receipt}]}}}}


async def test_topup_credits_exactly_once_even_if_callback_replayed(api, daraja):
    r = await api.post("/api/v1/wallet/topup", json={"phone": "0712 345 678", "amountKes": 500},
                       headers={**auth(), **idem("topup-0001")})
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    assert daraja["last"] == ("254712345678", 500)
    cid = r.json()["checkout_request_id"]
    assert await balance() == (0, 0)                              # nothing credited until M-Pesa confirms

    for _ in range(3):                                            # Safaricom retries / duplicates
        cb = await api.post(f"/api/v1/mpesa/callback/{SECRET}", json=CALLBACK(cid))
        assert cb.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
    assert await balance() == (Decimal("500.00"), 0)
    t = (await api.get(f"/api/v1/wallet/topups/{cid}", headers=auth())).json()
    assert t["status"] == "SUCCESS" and t["mpesa_receipt"] == "RCP123"
    assert (await ledger.verify_balance("alice"))["consistent"]


async def test_topup_replay_sends_one_prompt(api, daraja):
    for _ in range(3):
        r = await api.post("/api/v1/wallet/topup", json={"phone": "0712345678", "amountKes": 100},
                           headers={**auth(), **idem("topup-same-1")})
        assert r.status_code == 200
    assert daraja["pushes"] == 1


async def test_forged_callback_cannot_credit(api, daraja):
    r = await api.post("/api/v1/wallet/topup", json={"phone": "0712345678", "amountKes": 500},
                       headers={**auth(), **idem("topup-0002")})
    cid = r.json()["checkout_request_id"]
    assert (await api.post("/api/v1/mpesa/callback/wrong-secret", json=CALLBACK(cid))).status_code == 404

    # attacker knows the URL but Daraja's own query says the prompt failed: nothing is credited
    daraja["query"] = {"ResultCode": "1032", "ResultDesc": "Request cancelled by user"}
    await api.post(f"/api/v1/mpesa/callback/{SECRET}", json=CALLBACK(cid))
    assert await balance() == (0, 0)
    assert (await api.get(f"/api/v1/wallet/topups/{cid}", headers=auth())).json()["status"] == "FAILED"


async def test_credit_uses_requested_amount_and_flags_mismatch(api, daraja):
    r = await api.post("/api/v1/wallet/topup", json={"phone": "0712345678", "amountKes": 100},
                       headers={**auth(), **idem("topup-0003")})
    cid = r.json()["checkout_request_id"]
    await api.post(f"/api/v1/mpesa/callback/{SECRET}", json=CALLBACK(cid, amount=99999))   # claims more
    assert await balance() == (0, 0)
    assert (await api.get(f"/api/v1/wallet/topups/{cid}", headers=auth())).json()["status"] == "REVIEW"


async def test_pending_stays_pending_and_refresh_recovers_missed_callback(api, daraja):
    r = await api.post("/api/v1/wallet/topup", json={"phone": "0712345678", "amountKes": 200},
                       headers={**auth(), **idem("topup-0004")})
    cid = r.json()["checkout_request_id"]
    daraja["query"] = mc.MpesaUnknownOutcome("being processed")        # prompt still open
    t = (await api.get(f"/api/v1/wallet/topups/{cid}?refresh=true", headers=auth())).json()
    assert t["status"] == "PENDING" and await balance() == (0, 0)
    daraja["query"] = {"ResultCode": "0", "ResultDesc": "paid"}         # customer paid, callback was lost
    t = (await api.get(f"/api/v1/wallet/topups/{cid}?refresh=true", headers=auth())).json()
    assert t["status"] == "SUCCESS" and await balance() == (Decimal("200.00"), 0)


async def test_topup_validation_and_isolation(api, daraja):
    bad = [("12345", 100), ("0612345678", 100), ("0712345678", 0), ("0712345678", 9), ("0712345678", 150001)]
    for i, (phone, amount) in enumerate(bad):
        r = await api.post("/api/v1/wallet/topup", json={"phone": phone, "amountKes": amount},
                           headers={**auth(), **idem(f"bad-topup-{i}")})
        assert r.status_code == 422, (phone, amount)
    assert daraja["pushes"] == 0
    r = await api.post("/api/v1/wallet/topup", json={"phone": "0712345678", "amountKes": 100},
                       headers={**auth(), **idem("topup-0005")})
    cid = r.json()["checkout_request_id"]
    assert (await api.get(f"/api/v1/wallet/topups/{cid}", headers=auth("mallory"))).status_code == 404


async def test_daraja_request_shape(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        if request.url.path == "/oauth/v1/generate":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": "3599"})
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"ResponseCode": "0", "CheckoutRequestID": "c1", "MerchantRequestID": "m1"})

    mc._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mc._token.update(value=None, expires=0)
    await mc.stk_push("254712345678", 500, "MamlakaWallet", "Wallet topup")
    b = seen["body"]
    assert seen["auth"] == "Bearer tok" and b["Amount"] == 500 and isinstance(b["Amount"], int)
    assert b["PartyA"] == b["PhoneNumber"] == "254712345678" and b["BusinessShortCode"] == "174379"
    assert b["CallBackURL"] == f"https://api.example.test/api/v1/mpesa/callback/{SECRET}"
    import base64
    assert base64.b64decode(b["Password"]).decode() == "174379pk" + b["Timestamp"]


# ------------------------------------------------------------------ wallet-funded card operations
@pytest.fixture
def wasabi(monkeypatch):
    s = {"create": 0, "status": "wait_process"}

    async def types(): return CARD_TYPES

    async def create(payload):
        s["create"] += 1
        return {"success": True, "data": {"cardNo": "C1", "status": s["status"], "orderNo": "W1"}}

    monkeypatch.setattr(wc, "get_card_types", types)
    monkeypatch.setattr(wc, "create_card", create)
    return s


async def fund(user, kes):
    await ledger.credit(user, int(kes * 100), ref=f"seed-{user}", kind="topup")


async def test_create_card_holds_funds_then_captures_on_webhook(api, wasabi, monkeypatch):
    await fund("alice", 2000)
    r = await api.post("/api/v1/cards/create", json={"cardTypeId": 8, "amount": "10"},
                       headers={**auth(), **idem("card-hold-001")})
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    assert await balance() == (Decimal("674.00"), Decimal("1326.00"))     # 2000 - 1326 held

    async def operations(payload):
        return {"success": True, "data": {"records": [{"orderNo": "W1", "status": "success"}]}}

    async def info(payload): return {"success": True, "data": {"status": "Normal"}}
    monkeypatch.setattr(wc, "card_operations", operations)
    monkeypatch.setattr(wc, "card_info", info)
    order = r.json()["merchantOrderNo"]
    for rid in ("a", "a", "b"):                                            # duplicate + extra callbacks
        await api.post("/api/v1/webhooks/wasabi", json={"merchantOrderNo": order, "cardNo": "C1"},
                       headers={"X-WSB-REQUEST-ID": rid, "X-WSB-CATEGORY": "card_transaction"})
    assert await balance() == (Decimal("674.00"), Decimal("0.00"))        # captured once, spent
    assert (await ledger.verify_balance("alice"))["consistent"]


async def test_insufficient_wallet_blocks_before_wasabi(api, wasabi):
    await fund("alice", 100)
    r = await api.post("/api/v1/cards/create", json={"cardTypeId": 8, "amount": "10"},
                       headers={**auth(), **idem("card-poor-001")})
    assert r.status_code == 402 and wasabi["create"] == 0
    assert await balance() == (Decimal("100.00"), 0)


async def test_rejected_card_releases_hold(api, wasabi, monkeypatch):
    await fund("alice", 2000)

    async def reject(payload): raise wc.WasabiError(1, "rejected")
    monkeypatch.setattr(wc, "create_card", reject)
    r = await api.post("/api/v1/cards/create", json={"cardTypeId": 8, "amount": "10"},
                       headers={**auth(), **idem("card-rej-0001")})
    assert r.status_code == 422
    assert await balance() == (Decimal("2000.00"), 0)                      # fully restored


async def test_unknown_outcome_keeps_hold_then_reconciles(api, wasabi, monkeypatch):
    from app.cards import service
    await fund("alice", 2000)

    async def lost(payload): raise wc.WasabiUnknownOutcome("timeout")
    monkeypatch.setattr(wc, "create_card", lost)
    r = await api.post("/api/v1/cards/create", json={"cardTypeId": 8, "amount": "10"},
                       headers={**auth(), **idem("card-unk-0001")})
    assert r.status_code == 202
    assert await balance() == (Decimal("674.00"), Decimal("1326.00"))      # customer funds stay held
    order = ops.build_order_no("CRD", "alice", "card-unk-0001")

    async def none_found(payload): return {"success": True, "data": {"records": []}}
    monkeypatch.setattr(wc, "card_operations", none_found)
    assert await service.reconcile_operation(order) == "UNKNOWN"           # too early to give up
    await database.transactions_collection.update_one(
        {"merchant_order_no": order}, {"$set": {"created_at": __import__("datetime").datetime(2020, 1, 1)}})
    assert await service.reconcile_operation(order) == "FAILED"            # never reached Wasabi
    assert await balance() == (Decimal("2000.00"), 0)


async def test_withdraw_credits_wallet_only_on_success(api, wasabi, monkeypatch):
    from app.cards import service
    await database.cards_collection.insert_one({"card_no": "C1", "owner_id": "alice", "status": "Normal"})

    async def withdraw(payload): return {"success": True, "data": {"status": "processing"}}
    monkeypatch.setattr(wc, "withdraw", withdraw)
    r = await api.post("/api/v1/cards/withdraw", json={"cardNo": "C1", "amount": "10"},
                       headers={**auth(), **idem("wd-0000001")})
    assert r.json()["status"] == "PENDING" and await balance() == (0, 0)   # nothing yet

    async def done(payload): return {"success": True, "data": {"records": [{"status": "success"}]}}
    monkeypatch.setattr(wc, "card_operations", done)
    order = r.json()["merchantOrderNo"]
    await service.reconcile_operation(order)
    await service.reconcile_operation(order)
    assert await balance() == (Decimal("1274.00"), 0)                      # 10 * 130 * 0.98, once


async def test_funding_off_without_fx_rate(api, wasabi, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "FX_KES_PER_USD", "")
    await fund("alice", 2000)
    r = await api.post("/api/v1/cards/create", json={"cardTypeId": 8, "amount": "10"},
                       headers={**auth(), **idem("card-nofx-001")})
    assert r.status_code == 503 and wasabi["create"] == 0


async def test_reconcile_job_settles_everything(api, wasabi, monkeypatch, daraja=None):
    from app.jobs import reconcile
    stats = await reconcile.run_once()
    assert stats["ledger_mismatch"] == 0
