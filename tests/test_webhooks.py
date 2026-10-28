import base64

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from app.cards import wasabi_client as wc
from app.main import app
import app.core.database as database
import app.webhooks.router as webhook_router

ACK = {"success": True, "code": 200, "msg": None, "data": None}


@pytest.fixture
async def api(fake_db):
    await database.transactions_collection.create_index("merchant_order_no", unique=True)
    await database.webhook_events_collection.create_index("request_id", unique=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def hdr(request_id="r1", category="card_holder", sig=""):
    return {"X-WSB-REQUEST-ID": request_id, "X-WSB-CATEGORY": category, "X-WSB-SIGNATURE": sig}


async def test_holder_status_comes_from_wasabi_not_the_payload(api, monkeypatch):
    await database.holders_collection.insert_one({"holder_id": 5, "owner_id": "alice", "status": "wait_audit"})

    async def query(payload):
        return {"success": True, "data": {"records": [{"holderId": 5, "status": "pass_audit"}]}}

    monkeypatch.setattr(wc, "query_cardholders", query)
    # forged payload claims "reject"; stored status must follow Wasabi's own API answer
    r = await api.post("/api/v1/webhooks/wasabi", json={"holderId": 5, "status": "reject"}, headers=hdr())
    assert r.json() == ACK
    assert (await database.holders_collection.find_one({"holder_id": 5}))["status"] == "pass_audit"


async def test_duplicates_are_acked_without_reprocessing(api, monkeypatch):
    calls = {"n": 0}

    async def query(payload):
        calls["n"] += 1
        return {"success": True, "data": {"records": []}}

    await database.holders_collection.insert_one({"holder_id": 5, "owner_id": "alice", "status": "x"})
    monkeypatch.setattr(wc, "query_cardholders", query)
    for _ in range(3):
        r = await api.post("/api/v1/webhooks/wasabi", json={"holderId": 5}, headers=hdr("same-id"))
        assert r.json() == ACK
    assert calls["n"] == 1
    assert await database.webhook_events_collection.count_documents({}) == 1


async def test_failed_processing_returns_500_so_wasabi_retries(api, monkeypatch):
    await database.holders_collection.insert_one({"holder_id": 5, "owner_id": "alice", "status": "x"})

    async def boom(payload):
        raise wc.WasabiUnknownOutcome("down")

    monkeypatch.setattr(wc, "query_cardholders", boom)
    r = await api.post("/api/v1/webhooks/wasabi", json={"holderId": 5}, headers=hdr("retry-me"))
    assert r.status_code == 500

    async def ok(payload):
        return {"success": True, "data": {"records": [{"status": "pass_audit"}]}}

    monkeypatch.setattr(wc, "query_cardholders", ok)
    r = await api.post("/api/v1/webhooks/wasabi", json={"holderId": 5}, headers=hdr("retry-me"))
    assert r.json() == ACK  # retry is processed, not dropped as a duplicate


async def test_card_transaction_finalizes_operation_from_wasabi(api, monkeypatch):
    from app.transactions import orchestrator as ops
    await ops.begin_operation("ORDER1", "FREEZE", "alice", {})
    await ops.finish_operation("ORDER1", ops.PENDING)
    await database.cards_collection.insert_one({"card_no": "C1", "owner_id": "alice", "status": "Freezing"})

    async def operations(payload):
        return {"success": True, "data": {"records": [{"orderNo": "W", "status": "success"}]}}

    async def info(payload):
        return {"success": True, "data": {"status": "Freeze", "blocked": False}}

    monkeypatch.setattr(wc, "card_operations", operations)
    monkeypatch.setattr(wc, "card_info", info)
    r = await api.post("/api/v1/webhooks/wasabi", json={"merchantOrderNo": "ORDER1", "cardNo": "C1"},
                       headers=hdr("t1", "card_transaction"))
    assert r.json() == ACK
    assert (await database.transactions_collection.find_one({"merchant_order_no": "ORDER1"}))["status"] == "SUCCESS"
    assert (await database.cards_collection.find_one({"card_no": "C1"}))["status"] == "Freeze"


async def test_signature_enforced_when_enabled(api, monkeypatch, wasabi_private_key):
    monkeypatch.setattr(webhook_router, "ENABLE_WEBHOOK_SIGNATURE_VALIDATION", True)
    body = b'{"holderId":5}'
    r = await api.post("/api/v1/webhooks/wasabi", content=body, headers={**hdr("s0"), "Content-Type": "application/json"})
    assert r.status_code == 401
    good = base64.b64encode(wasabi_private_key.sign(body, padding.PKCS1v15(), hashes.SHA256())).decode()
    r = await api.post("/api/v1/webhooks/wasabi", content=body,
                       headers={**hdr("s1", sig=good), "Content-Type": "application/json"})
    assert r.json() == ACK
    r = await api.post("/api/v1/webhooks/wasabi", content=b'{"holderId":6}',
                       headers={**hdr("s2", sig=good), "Content-Type": "application/json"})
    assert r.status_code == 401  # signature for a different body


async def test_sensitive_fields_not_stored(api):
    await api.post("/api/v1/webhooks/wasabi", json={"holderId": 9, "email": "a@b.co", "firstName": "J"},
                   headers=hdr("p1"))
    event = await database.webhook_events_collection.find_one({"request_id": "p1"})
    assert "email" not in event["payload"] and "firstName" not in event["payload"]
