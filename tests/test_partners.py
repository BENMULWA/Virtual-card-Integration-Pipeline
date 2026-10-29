import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest

import app.core.database as database
from app.cards import wasabi_client as wc
from app.main import app
from app.partners import events
from app.partners import service as partners
from app.wallet import ledger

CARD_TYPES = {"success": True, "data": [{"cardTypeId": 8, "status": "online", "needCardHolder": False},
                                        {"cardTypeId": 7, "status": "online", "needCardHolder": True}]}
HOLDER = {
    "cardTypeId": 7, "areaCode": "+254", "mobile": "712345678", "email": "a@b.co",
    "firstName": "Jane", "lastName": "Doe", "birthday": "1990-01-01", "nationality": "KE",
    "country": "KE", "town": "Nairobi", "address": "1 Main St", "postCode": "00100", "gender": "F",
    "idType": "DLN", "idNumber": "12345678", "issueDate": "2020-01-01", "idNoExpiryDate": "2030-01-01",
    "idFrontId": "f1", "idBackId": "b1", "idHoldId": "h1",
}
REAL_CLIENT = httpx.AsyncClient


def idem(key):
    return {"Idempotency-Key": key}


@pytest.fixture
async def api(fake_db):
    for coll, field in [(database.transactions_collection, "merchant_order_no"),
                        (database.webhook_events_collection, "request_id"),
                        (database.ledger_collection, "entry_key"),
                        (database.wallets_collection, "owner_id"),
                        (database.tenants_collection, "slug"),
                        (database.partner_customers_collection, "owner_id"),
                        (database.partner_events_collection, "event_id")]:
        await coll.create_index(field, unique=True)
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.fixture
async def comet(api):
    tenant, key = await partners.create_tenant("Comet", "comet", webhook_url="https://comet.test/hooks")
    return tenant, {"X-API-Key": key}


@pytest.fixture
def wasabi(monkeypatch):
    s = {"create": 0}

    async def types(): return CARD_TYPES

    async def create(payload):
        s["create"] += 1
        return {"success": True, "data": {"cardNo": "C1", "status": "wait_process", "orderNo": "W1"}}

    async def create_holder(payload):
        return {"success": True, "data": {"holderId": 900, "status": "wait_audit"}}

    monkeypatch.setattr(wc, "get_card_types", types)
    monkeypatch.setattr(wc, "create_card", create)
    monkeypatch.setattr(wc, "create_cardholder", create_holder)
    return s


@pytest.fixture
def hooks(monkeypatch):
    """Capture what we POST to the reseller. `reply` is the status code it answers with."""
    box = {"posts": [], "reply": 200}

    def handler(request: httpx.Request):
        box["posts"].append(request)
        return httpx.Response(box["reply"])

    monkeypatch.setattr(events.httpx, "AsyncClient",
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return box


async def customer(api, headers, ext="u-1"):
    r = await api.post("/api/v1/partner/customers", headers=headers,
                       json={"externalId": ext, "fullName": "Jane Doe", "email": "j@d.co", "phone": "0712345678"})
    assert r.status_code == 201, r.text
    return r.json()


async def float_of(tenant):
    b = await ledger.get_balance(partners.float_owner(tenant["tenant_id"]))
    return b["available"], b["held"]


# ------------------------------------------------------------------ auth + isolation
async def test_api_key_is_required_and_must_be_valid(api, comet):
    assert (await api.get("/api/v1/partner/balance")).status_code == 401
    assert (await api.get("/api/v1/partner/balance", headers={"X-API-Key": "mk_live_nope"})).status_code == 401
    assert (await api.get("/api/v1/partner/balance", headers={"X-API-Key": "garbage"})).status_code == 401
    assert (await api.get("/api/v1/partner/balance", headers=comet[1])).status_code == 200


async def test_the_key_is_stored_only_as_a_hash(api, comet):
    tenant, headers = comet
    assert headers["X-API-Key"] not in json.dumps(tenant, default=str)
    assert tenant["key_hash"] == partners.hash_key(headers["X-API-Key"])


async def test_rotating_a_key_invalidates_the_old_one(api, comet):
    new = await partners.rotate_key("comet")
    assert (await api.get("/api/v1/partner/balance", headers=comet[1])).status_code == 401
    assert (await api.get("/api/v1/partner/balance", headers={"X-API-Key": new})).status_code == 200


async def test_customer_creation_is_idempotent_on_external_id(api, comet):
    first = await customer(api, comet[1])
    again = await api.post("/api/v1/partner/customers", headers=comet[1],
                           json={"externalId": "u-1", "fullName": "Other Name", "email": "x@y.co", "phone": "0700000000"})
    assert first["created"] is True and again.json()["created"] is False
    assert again.json()["fullName"] == "Jane Doe"          # the original record wins


async def test_a_reseller_cannot_see_or_use_another_resellers_customer(api, comet):
    await customer(api, comet[1])
    _, other_key = await partners.create_tenant("Other", "other")
    other = {"X-API-Key": other_key}
    assert (await api.get("/api/v1/partner/customers/u-1", headers=other)).status_code == 404
    assert (await api.get("/api/v1/partner/customers/u-1/cards", headers=other)).status_code == 404
    assert (await api.get("/api/v1/partner/customers/u-1", headers=comet[1])).status_code == 200


async def test_external_id_cannot_smuggle_the_owner_separator(api, comet):
    r = await api.post("/api/v1/partner/customers", headers=comet[1],
                       json={"externalId": "a:b", "fullName": "Jane Doe", "email": "j@d.co", "phone": "0712345678"})
    assert r.status_code == 422


# ------------------------------------------------------------------ cards are paid from the float
async def test_card_is_paid_from_the_float_not_the_customers_wallet(api, comet, wasabi):
    tenant, headers = comet
    await customer(api, headers)
    await ledger.credit(partners.float_owner(tenant["tenant_id"]), 200_000, ref="bank-1", kind="float_topup")
    r = await api.post("/api/v1/partner/customers/u-1/cards", headers={**headers, **idem("card-key-001")},
                       json={"cardTypeId": 8, "amount": "10"})
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    available, held = await float_of(tenant)
    assert held > 0 and available + held == 2000                         # 2000 KES float, part of it now held
    owner = partners.customer_owner(tenant["tenant_id"], "u-1")
    assert (await ledger.get_balance(owner))["available"] == 0           # the customer's own wallet is untouched
    card = (await api.get("/api/v1/partner/customers/u-1/cards", headers=headers)).json()["records"][0]
    assert card["card_no"] == "C1" and "owner_id" not in card


async def test_issuance_without_enough_float_is_refused_and_nothing_is_sent(api, comet, wasabi):
    await customer(api, comet[1])
    r = await api.post("/api/v1/partner/customers/u-1/cards", headers={**comet[1], **idem("card-key-002")},
                       json={"cardTypeId": 8, "amount": "10"})
    assert r.status_code == 402 and "float" in r.json()["detail"].lower()
    assert wasabi["create"] == 0


async def test_card_issuance_replay_creates_one_card(api, comet, wasabi):
    tenant, headers = comet
    await customer(api, headers)
    await ledger.credit(partners.float_owner(tenant["tenant_id"]), 500_000, ref="bank-1", kind="float_topup")
    for _ in range(3):
        await api.post("/api/v1/partner/customers/u-1/cards", headers={**headers, **idem("card-key-003")},
                       json={"cardTypeId": 8, "amount": "10"})
    assert wasabi["create"] == 1


async def test_tenant_limits_card_types_and_amounts(api, wasabi):
    tenant, key = await partners.create_tenant("Small", "small", allowed_card_type_ids=[7], max_single_amount="20")
    headers = {"X-API-Key": key}
    await customer(api, headers)
    await ledger.credit(partners.float_owner(tenant["tenant_id"]), 5_000_000, ref="b", kind="float_topup")
    wrong_type = await api.post("/api/v1/partner/customers/u-1/cards", headers={**headers, **idem("limit-key-01")},
                                json={"cardTypeId": 8, "amount": "10"})
    assert wrong_type.status_code == 403
    over = await api.post("/api/v1/partner/customers/u-1/cards", headers={**headers, **idem("limit-key-02")},
                          json={"cardTypeId": 7, "amount": "50"})
    assert over.status_code == 422


# ------------------------------------------------------------------ KYC
async def test_kyc_registers_the_holder_under_the_customer(api, comet, wasabi):
    tenant, headers = comet
    await customer(api, headers)
    r = await api.post("/api/v1/partner/customers/u-1/kyc", headers={**headers, **idem("kyc-key-0001")}, json=HOLDER)
    assert r.status_code == 200, r.text
    holder = await database.holders_collection.find_one({"holder_id": 900})
    assert holder["owner_id"] == partners.customer_owner(tenant["tenant_id"], "u-1")
    status = (await api.get("/api/v1/partner/customers/u-1/kyc", headers=headers)).json()["records"][0]
    assert status["status"] == "wait_audit" and "owner_id" not in status and status["review"]["approved"] is False


async def test_a_kyc_decision_from_wasabi_becomes_a_signed_webhook(api, comet, wasabi, hooks, monkeypatch):
    tenant, headers = comet
    await customer(api, headers)
    await api.post("/api/v1/partner/customers/u-1/kyc", headers={**headers, **idem("kyc-key-0002")}, json=HOLDER)

    async def approved(payload):
        return {"success": True, "data": {"records": [{"status": "pass_audit", "description": "SUCCESS"}]}}

    monkeypatch.setattr(wc, "query_cardholders", approved)
    r = await api.post("/api/v1/webhooks/wasabi", json={"holderId": 900},
                       headers={"X-WSB-CATEGORY": "card_holder", "X-WSB-REQUEST-ID": "req-1"})
    assert r.status_code == 200
    await asyncio.sleep(0.1)                                              # the immediate delivery runs in the background

    assert len(hooks["posts"]) == 1
    sent = hooks["posts"][0]
    assert sent.url == "https://comet.test/hooks"
    assert sent.headers["x-mamlaka-signature"] == events.sign(sent.content, tenant["signing_secret"])
    body = json.loads(sent.content)
    assert body["type"] == "kyc.status_changed" and body["id"] == sent.headers["x-mamlaka-event-id"]
    assert body["data"] == {"holderId": 900, "status": "pass_audit", "approved": True, "reason": None, "customerId": "u-1"}


# ------------------------------------------------------------------ outbound webhook delivery
async def test_failed_delivery_is_retried_and_eventually_delivered(api, comet, hooks):
    tenant, _ = comet
    hooks["reply"] = 500
    event = await events.emit(tenant["tenant_id"], "card.updated", {"cardNo": "C1"})
    await asyncio.sleep(0.1)
    stored = await database.partner_events_collection.find_one({"event_id": event["event_id"]})
    assert stored["status"] == "PENDING" and stored["attempts"] == 1 and stored["next_attempt_at"] > datetime.utcnow()
    assert await events.deliver_due() == 0                                # not due yet: no hammering

    hooks["reply"] = 200
    await database.partner_events_collection.update_one(
        {"event_id": event["event_id"]}, {"$set": {"next_attempt_at": datetime.utcnow() - timedelta(seconds=1)}})
    assert await events.deliver_due() == 1
    assert (await database.partner_events_collection.find_one({"event_id": event["event_id"]}))["status"] == "DELIVERED"


async def test_events_for_non_reseller_users_are_ignored(api, comet, hooks):
    assert await events.emit_for_owner("some-direct-user", "card.updated", {}) is None
    await asyncio.sleep(0.05)
    assert hooks["posts"] == []


async def test_event_without_a_configured_endpoint_is_skipped_not_retried(api, hooks):
    tenant, _ = await partners.create_tenant("NoHook", "nohook")
    event = await events.emit(tenant["tenant_id"], "card.updated", {})
    await asyncio.sleep(0.1)
    assert (await database.partner_events_collection.find_one({"event_id": event["event_id"]}))["status"] == "SKIPPED"
    assert hooks["posts"] == []


async def test_slug_and_webhook_url_are_validated(api):
    for bad in ("commet wallet", "Comet", "a", "x/y"):
        with pytest.raises(ValueError):
            await partners.create_tenant("Comet", bad)
    with pytest.raises(ValueError):
        await partners.create_tenant("Comet", "comet", webhook_url="http://comet.test/hook")
    tenant, _ = await partners.create_tenant("Commet Wallet", "commet-wallet")
    await partners.set_webhook("commet-wallet", "https://comet.test/hooks")
    assert (await partners.get_tenant_by_slug("commet-wallet"))["webhook_url"] == "https://comet.test/hooks"
    with pytest.raises(ValueError):
        await partners.set_webhook("nobody", "https://x.test")


# ------------------------------------------------------------------ what a reseller needs to settle its own customer
async def issue_one(api, comet, wasabi, key="op-key-0001"):
    tenant, headers = comet
    await customer(api, headers)
    await ledger.credit(partners.float_owner(tenant["tenant_id"]), 500_000, ref="bank-op", kind="float_topup")
    r = await api.post("/api/v1/partner/customers/u-1/cards", headers={**headers, **idem(key)},
                       json={"cardTypeId": 8, "amount": "10"})
    return r.json()["merchantOrderNo"]


async def test_operation_status_is_scoped_to_the_reseller(api, comet, wasabi):
    ref = await issue_one(api, comet, wasabi)
    ok = await api.get(f"/api/v1/partner/operations/{ref}", headers=comet[1])
    assert ok.status_code == 200 and ok.json()["status"] == "PENDING" and ok.json()["customerId"] == "u-1"
    _, other_key = await partners.create_tenant("Other", "other")
    assert (await api.get(f"/api/v1/partner/operations/{ref}", headers={"X-API-Key": other_key})).status_code == 404
    assert (await api.get("/api/v1/partner/operations/NOPE", headers=comet[1])).status_code == 404


async def test_final_outcome_reaches_the_reseller_as_a_webhook_and_via_refresh(api, comet, wasabi, hooks, monkeypatch):
    ref = await issue_one(api, comet, wasabi)

    async def operations(payload):
        return {"success": True, "data": {"records": [{"orderNo": "W1", "status": "success"}]}}

    async def info(payload): return {"success": True, "data": {"status": "Normal"}}
    monkeypatch.setattr(wc, "card_operations", operations)
    monkeypatch.setattr(wc, "card_info", info)
    r = await api.post("/api/v1/webhooks/wasabi", json={"merchantOrderNo": ref, "cardNo": "C1"},
                       headers={"X-WSB-CATEGORY": "card_transaction", "X-WSB-REQUEST-ID": "req-op-1"})
    assert r.status_code == 200
    await asyncio.sleep(0.15)
    types = [json.loads(p.content)["type"] for p in hooks["posts"]]
    assert "operation.updated" in types
    sent = next(json.loads(p.content) for p in hooks["posts"] if json.loads(p.content)["type"] == "operation.updated")
    assert sent["data"] == {"reference": ref, "type": "CREATE_CARD", "status": "SUCCESS", "customerId": "u-1"}
    assert (await api.get(f"/api/v1/partner/operations/{ref}", headers=comet[1])).json()["status"] == "SUCCESS"


async def test_card_balance_and_details_are_owner_scoped_and_not_cached(api, comet, wasabi, monkeypatch):
    tenant, headers = comet
    await issue_one(api, comet, wasabi)
    await database.cards_collection.update_one({"card_no": "C1"}, {"$set": {"owner_id": partners.customer_owner(tenant["tenant_id"], "u-1")}})

    async def balance(payload): return {"success": True, "data": {"amount": "10.00", "currency": "USD"}}
    async def sensitive(payload): return {"success": True, "data": {"cardNumber": "4111111111111111", "cvv": "123", "expireDate": "12/29"}}
    monkeypatch.setattr(wc, "card_balance", balance)
    monkeypatch.setattr(wc, "card_sensitive_decrypt", sensitive)

    assert (await api.get("/api/v1/partner/customers/u-1/cards/C1/balance", headers=headers)).json()["balance"] == "10.00"
    r = await api.post("/api/v1/partner/customers/u-1/cards/details", headers=headers, json={"cardNo": "C1"})
    assert r.status_code == 200 and r.json()["cvv"] == "123" and r.headers["cache-control"] == "no-store"

    _, other_key = await partners.create_tenant("Other", "other")
    other = {"X-API-Key": other_key}
    await customer(api, other, ext="u-1")                                  # same externalId, different reseller
    assert (await api.post("/api/v1/partner/customers/u-1/cards/details", headers=other, json={"cardNo": "C1"})).status_code == 404
    assert (await api.get("/api/v1/partner/customers/u-1/cards/C1/balance", headers=other)).status_code == 404


async def test_local_http_webhook_is_allowed_but_remote_http_is_not(api):
    await partners.create_tenant("Local", "local", webhook_url="http://localhost:8000/hooks")
    with pytest.raises(ValueError):
        await partners.create_tenant("Remote", "remote", webhook_url="http://example.com/hooks")


async def test_quote_matches_what_issuance_then_costs_the_float(api, comet, wasabi):
    tenant, headers = comet
    q = (await api.get("/api/v1/partner/quote?kind=issue&amount=10&cardTypeId=8", headers=headers)).json()
    await customer(api, headers)
    await ledger.credit(partners.float_owner(tenant["tenant_id"]), 500_000, ref="bank-q", kind="float_topup")
    await api.post("/api/v1/partner/customers/u-1/cards", headers={**headers, **idem("quote-key-01")},
                   json={"cardTypeId": 8, "amount": "10"})
    available, held = await float_of(tenant)
    assert str(held) == q["kes"] and q["usd"] == "10.00"
    assert (await api.get("/api/v1/partner/quote?kind=issue&amount=10.555&cardTypeId=8", headers=headers)).status_code == 422
    assert (await api.get("/api/v1/partner/quote?kind=issue&amount=10&cardTypeId=99", headers=headers)).status_code == 422
    assert (await api.get("/api/v1/partner/quote?kind=bad&amount=10&cardTypeId=8", headers=headers)).status_code == 422


async def test_card_leaves_pending_without_the_webhook(api, comet, wasabi, monkeypatch):
    from datetime import datetime, timedelta
    from app.jobs import reconcile
    ref = await issue_one(api, comet, wasabi)

    async def operations(payload):
        return {"success": True, "data": {"records": [{"orderNo": "W1", "cardNo": "C1", "status": "success"}]}}

    async def info(payload): return {"success": True, "data": {"status": "Normal"}}
    monkeypatch.setattr(wc, "card_operations", operations)
    monkeypatch.setattr(wc, "card_info", info)

    # the reseller's refresh settles the operation and the card together
    assert (await api.get(f"/api/v1/partner/operations/{ref}?refresh=true", headers=comet[1])).json()["status"] == "SUCCESS"
    assert (await database.cards_collection.find_one({"card_no": "C1"}))["status"] == "Normal"

    # a card left "pending" after its operation settled is picked up by the reconcile job
    await database.cards_collection.update_one(
        {"card_no": "C1"}, {"$set": {"status": "pending", "created_at": datetime.utcnow() - timedelta(minutes=5)}})
    assert (await reconcile.run_once())["cards"] == 1
    assert (await database.cards_collection.find_one({"card_no": "C1"}))["status"] == "Normal"
