import hashlib
import hmac
import json
from datetime import datetime, timedelta

import httpx
import pytest

import app.core.database as database
from app.core import config
from app.core.security import create_access_token
from app.main import app
from app.mobilemoney import client as gw
from app.mobilemoney import service
from app.wallet import ledger

SECRET = "gateway-signing-secret"
CB = "/api/v1/webhooks/mamlaka"


def auth(user="u1"):
    return {"Authorization": f"Bearer {create_access_token({'sub': user})}"}


def idem(key):
    return {"Idempotency-Key": key}


def signed(payload: dict, secret=SECRET):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"content": body, "headers": {"X-Mamlaka-Signature": sig, "Content-Type": "application/json"}}


@pytest.fixture
async def api(fake_db):
    for coll, field in [(database.topups_collection, "checkout_request_id"),
                        (database.payouts_collection, "external_id"),
                        (database.ledger_collection, "entry_key"),
                        (database.wallets_collection, "owner_id")]:
        await coll.create_index(field, unique=True)
    await database.users_collection.insert_one(
        {"user_id": "u1", "email": "a@b.co", "phone": "0712345678", "email_verified": True, "status": "active"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.fixture
def gateway(monkeypatch):
    """Stub the gateway: records calls, answers with a secureId."""
    calls = {"collect": [], "transfer": [], "status": []}
    state = {"transfer_error": None, "status": "PENDING"}

    async def collect(phone, amount, provider, external_id):
        calls["collect"].append((phone, amount, provider, external_id))
        return {"secureId": "SEC-" + external_id[-6:]}

    async def transfer(phone, amount, provider, external_id):
        calls["transfer"].append((phone, amount, provider, external_id))
        if state["transfer_error"]:
            raise state["transfer_error"]
        return {"secureId": "SEC-" + external_id[-6:], "status": "PENDING"}

    async def status(secure_id):
        calls["status"].append(secure_id)
        return {"external_id": None, "secure_id": secure_id, "amount": None, "state": state["status"],
                "receipt": "RCPT1", "reason": None}

    monkeypatch.setattr(gw, "initiate_collection", collect)
    monkeypatch.setattr(gw, "initiate_transfer", transfer)
    monkeypatch.setattr(gw, "get_status", status)
    return calls, state


async def topup_now(api, amount=500, provider="airtel", key="topup-key-1"):
    r = await api.post("/api/v1/wallet/topup", headers={**auth(), **idem(key)},
                       json={"phone": "0733000111", "amountKes": amount, "provider": provider})
    assert r.status_code == 200, r.text
    return r.json()["checkout_request_id"]


async def bal():
    b = await ledger.get_balance("u1")
    return b["available"], b["held"]


# ------------------------------------------------------------------ signature + payload parsing
def test_signature_accepts_only_the_exact_hmac():
    body = b'{"externalId":"X"}'
    good = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert gw.verify_signature(body, good, "k")
    assert not gw.verify_signature(body, good, "other")
    assert not gw.verify_signature(body + b" ", good, "k")
    assert not gw.verify_signature(body, "", "k")
    assert not gw.verify_signature(body, good, "")


def test_status_is_read_from_top_level_or_nested_transaction():
    flat = gw.normalize_status({"externalId": "A", "amount": 10, "transactionStatus": "COMPLETE"})
    nested = gw.normalize_status({"transaction": {"external_id": "A", "amount": "10", "status": "failed"}})
    assert flat["state"] == "SUCCESS" and flat["external_id"] == "A"
    assert nested["state"] == "FAILED" and nested["external_id"] == "A"
    assert gw.normalize_status({"externalId": "A", "status": "processing"})["state"] == "PENDING"


async def test_client_logs_in_with_basic_auth_then_sends_the_collection(monkeypatch):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"token": "TKN", "expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(200, json={"secureId": "S1"})

    monkeypatch.setattr(gw, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    gw._token.update(value=None, expires=0.0)
    out = await gw.initiate_collection("254733000111", 500, "airtel", "TUPX")
    assert out["secureId"] == "S1"
    login, post = seen
    assert login.headers["authorization"].startswith("Basic ")
    assert post.headers["authorization"] == "Bearer TKN" and post.url.path.endswith("/mobile/initiate")
    body = json.loads(post.content)
    assert body["mobileMoneySP"] == "Airtel" and body["payerPhone"] == "254733000111"
    assert body["amount"] == 500 and body["externalId"] == "TUPX" and body["callbackUrl"].endswith(CB)


# ------------------------------------------------------------------ top-up
async def test_topup_credits_once_when_the_signed_callback_is_replayed(api, gateway):
    calls, _ = gateway
    ref = await topup_now(api)
    assert calls["collect"][0][:3] == ("254733000111", 500, "airtel")
    assert await bal() == (0, 0)                                  # nothing credited before payment

    cb = {"externalId": ref, "amount": 500, "transactionStatus": "COMPLETE", "transactionReceipt": "R9"}
    for _ in range(3):                                            # the gateway retries
        assert (await api.post(CB, **signed(cb))).status_code == 200
    assert await bal() == (500, 0)
    doc = (await api.get(f"/api/v1/wallet/topups/{ref}", headers=auth())).json()
    assert doc["status"] == "SUCCESS" and doc["mpesa_receipt"] == "R9"


async def test_replayed_topup_request_sends_one_prompt(api, gateway):
    calls, _ = gateway
    first = await topup_now(api, key="same-key-123")
    second = await topup_now(api, key="same-key-123")
    assert first == second and len(calls["collect"]) == 1


async def test_callback_with_a_bad_or_missing_signature_is_rejected(api, gateway):
    ref = await topup_now(api)
    cb = {"externalId": ref, "amount": 500, "transactionStatus": "COMPLETE"}
    assert (await api.post(CB, **signed(cb, secret="wrong"))).status_code == 401
    assert (await api.post(CB, json=cb)).status_code == 401
    assert await bal() == (0, 0)


async def test_callback_amount_mismatch_is_held_for_review_not_credited(api, gateway):
    ref = await topup_now(api, amount=500)
    await api.post(CB, **signed({"externalId": ref, "amount": 50000, "transactionStatus": "COMPLETE"}))
    assert await bal() == (0, 0)
    assert (await api.get(f"/api/v1/wallet/topups/{ref}", headers=auth())).json()["status"] == "REVIEW"


async def test_failed_collection_credits_nothing(api, gateway):
    ref = await topup_now(api)
    await api.post(CB, **signed({"externalId": ref, "amount": 500, "transactionStatus": "FAILED", "reason": "cancelled"}))
    assert await bal() == (0, 0)
    assert (await api.get(f"/api/v1/wallet/topups/{ref}", headers=auth())).json()["status"] == "FAILED"


async def test_refresh_recovers_a_missed_callback_from_the_gateway_status(api, gateway):
    _, state = gateway
    ref = await topup_now(api)
    state["status"] = "SUCCESS"
    r = await api.get(f"/api/v1/wallet/topups/{ref}?refresh=true", headers=auth())
    assert r.json()["status"] == "SUCCESS" and await bal() == (500, 0)


async def test_unknown_reference_is_acknowledged_without_effect(api, gateway):
    assert (await api.post(CB, **signed({"externalId": "NOPE", "amount": 1, "transactionStatus": "COMPLETE"}))).status_code == 200
    assert await bal() == (0, 0)


# ------------------------------------------------------------------ withdraw
async def fund(amount_kes):
    await ledger.credit("u1", amount_kes * 100, ref="seed", kind="topup")


async def withdraw(api, amount=200, phone="0712345678", key="wd-key-0001"):
    return await api.post("/api/v1/wallet/withdraw", headers={**auth(), **idem(key)},
                          json={"phone": phone, "amountKes": amount, "provider": "mpesa"})


async def test_withdraw_holds_funds_then_captures_on_success(api, gateway):
    calls, _ = gateway
    await fund(1000)
    r = await withdraw(api)
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    assert calls["transfer"][0][:3] == ("254712345678", 200, "mpesa")
    assert await bal() == (800, 200)                              # reserved, not yet spent

    ref = r.json()["reference"]
    await api.post(CB, **signed({"externalId": ref, "amount": 200, "transactionStatus": "COMPLETE"}))
    await api.post(CB, **signed({"externalId": ref, "amount": 200, "transactionStatus": "COMPLETE"}))
    assert await bal() == (800, 0)                                # captured exactly once


async def test_failed_payout_releases_the_hold(api, gateway):
    await fund(1000)
    ref = (await withdraw(api)).json()["reference"]
    await api.post(CB, **signed({"externalId": ref, "amount": 200, "transactionStatus": "FAILED"}))
    assert await bal() == (1000, 0)
    assert (await api.get(f"/api/v1/wallet/withdrawals/{ref}", headers=auth())).json()["status"] == "FAILED"


async def test_gateway_rejection_releases_immediately(api, gateway):
    _, state = gateway
    state["transfer_error"] = gw.GatewayError("insufficient merchant float")
    await fund(1000)
    r = await withdraw(api)
    assert r.status_code == 422 and await bal() == (1000, 0)


async def test_unknown_payout_outcome_keeps_the_hold(api, gateway):
    _, state = gateway
    state["transfer_error"] = gw.GatewayUnknownOutcome("timeout")
    await fund(1000)
    r = await withdraw(api)
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    assert await bal() == (800, 200)                              # never auto-released: the money may have gone out


async def test_replayed_withdraw_request_pays_once(api, gateway):
    calls, _ = gateway
    await fund(1000)
    a = await withdraw(api, key="same-wd-key-1")
    b = await withdraw(api, key="same-wd-key-1")
    assert a.json()["reference"] == b.json()["reference"]
    assert len(calls["transfer"]) == 1 and await bal() == (800, 200)


async def test_withdraw_guards(api, gateway, monkeypatch):
    await fund(100)
    assert (await withdraw(api, amount=500)).status_code == 402                       # more than the wallet holds
    assert (await withdraw(api, phone="0799999999", key="wd-key-0002")).status_code == 422   # not the registered phone
    assert (await withdraw(api, amount=5, key="wd-key-0003")).status_code == 422      # below the minimum
    monkeypatch.setattr(config, "WALLET_WITHDRAW_ENABLED", False)
    assert (await withdraw(api, amount=50, key="wd-key-0004")).status_code == 503


async def test_reconcile_settles_a_payout_whose_callback_never_arrived(api, gateway):
    _, state = gateway
    await fund(1000)
    await withdraw(api)
    await database.payouts_collection.update_many({}, {"$set": {"created_at": datetime.utcnow() - timedelta(minutes=10)}})
    state["status"] = "SUCCESS"
    assert await service.settle_pending_payouts() == 1
    assert await bal() == (800, 0)


async def test_mpesa_without_daraja_keys_runs_through_the_gateway(api, gateway, monkeypatch):
    calls, _ = gateway
    monkeypatch.setattr(config, "MPESA_CONSUMER_KEY", "")           # direct Daraja not configured
    r = await api.post("/api/v1/wallet/topup", headers={**auth(), **idem("mpesa-gw-key1")},
                       json={"phone": "0712345678", "amountKes": 300})   # no provider/channel: what the UI sends
    assert r.status_code == 200
    assert calls["collect"][0][:3] == ("254712345678", 300, "mpesa")
