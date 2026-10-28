import httpx
import pytest

from app.cards import wasabi_client as wc
from app.core.security import create_access_token
from app.main import app
from app.transactions.orchestrator import build_order_no
import app.core.database as database
from app.wallet import ledger

CARD_TYPES = {"success": True, "data": [
    {"cardTypeId": 7, "status": "online", "needCardHolder": True},
    {"cardTypeId": 8, "status": "online", "needCardHolder": False},
]}


def auth(user="alice"):
    return {"Authorization": f"Bearer {create_access_token({'sub': user})}"}


def idem(key="idem-key-0001"):
    return {"Idempotency-Key": key}


@pytest.fixture
async def api(fake_db):
    await database.transactions_collection.create_index("merchant_order_no", unique=True)
    await database.webhook_events_collection.create_index("request_id", unique=True)
    await database.ledger_collection.create_index("entry_key", unique=True)
    await database.topups_collection.create_index("checkout_request_id", unique=True)
    await ledger.credit("alice", 500_000, ref="seed", kind="topup")   # KES 5,000.00 in alice's wallet
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.fixture
def wasabi(monkeypatch):
    calls = {"create_card": 0, "freeze": 0, "deposit": 0}

    async def types(): return CARD_TYPES

    async def create_card(payload):
        calls["create_card"] += 1
        calls["last_create"] = payload
        return {"success": True, "data": {"cardNo": "C123", "status": "wait_process", "orderNo": "W1"}}

    async def freeze(payload):
        calls["freeze"] += 1
        calls["last_freeze"] = payload
        return {"success": True, "data": {"status": "processing"}}

    async def deposit(payload):
        calls["deposit"] += 1
        calls["last_deposit"] = payload
        return {"success": True, "data": {"status": "processing"}}

    monkeypatch.setattr(wc, "get_card_types", types)
    monkeypatch.setattr(wc, "create_card", create_card)
    monkeypatch.setattr(wc, "freeze_card", freeze)
    monkeypatch.setattr(wc, "deposit", deposit)
    return calls


# ---------------------------------------------------------------- auth
async def test_no_token_mint_endpoint_and_auth_required(api):
    assert (await api.post("/token")).status_code == 404
    assert (await api.post("/api/v1/cards/info", json={"cardNo": "x"})).status_code in (401, 403)
    assert (await api.post("/api/v1/cards/kyc/upload")).status_code in (401, 403)


async def test_token_without_subject_rejected(api):
    bad = {"Authorization": f"Bearer {create_access_token({'role': 'admin'})}"}
    assert (await api.post("/api/v1/cards/info", json={"cardNo": "x"}, headers=bad)).status_code == 401


async def test_kyc_bulk_upload_allows_missing_back_for_passport(api, monkeypatch):
    async def upload(file_bytes, filename, content_type):
        return {"success": True, "data": {"fileId": filename}}

    monkeypatch.setattr(wc, "upload_wasabi_file", upload)
    files = {
        "front": ("front.jpg", b"\xff\xd8\xfffront", "image/jpeg"),
        "selfie": ("selfie.jpg", b"\xff\xd8\xffselfie", "image/jpeg"),
    }
    response = await api.post("/api/v1/cards/kyc/upload-bulk", files=files, headers=auth())

    assert response.status_code == 200, response.text
    assert response.json()["data"]["backId"] is None
    assert response.json()["data"]["frontId"] == "front.jpg"
    assert response.json()["data"]["selfieId"] == "selfie.jpg"


# ---------------------------------------------------------------- create card
async def test_create_card_requires_approved_holder(api, wasabi):
    body = {"cardTypeId": 7, "holderId": 55, "amount": "10.50"}
    r = await api.post("/api/v1/cards/create", json=body, headers={**auth(), **idem()})
    assert r.status_code == 404  # holder not owned / unknown
    await database.holders_collection.insert_one(
        {"holder_id": 55, "owner_id": "alice", "card_type_id": 7, "status": "wait_audit"})
    r = await api.post("/api/v1/cards/create", json=body, headers={**auth(), **idem()})
    assert r.status_code == 409 and wasabi["create_card"] == 0
    await database.holders_collection.update_one({"holder_id": 55}, {"$set": {"status": "pass_audit"}})
    r = await api.post("/api/v1/cards/create", json=body, headers={**auth(), **idem()})
    assert r.status_code == 200, r.text
    assert wasabi["last_create"]["amount"] == 10.5  # decimals preserved
    assert r.json()["cardNo"] == "C123"
    card = await database.cards_collection.find_one({"card_no": "C123"})
    assert card["owner_id"] == "alice"


async def test_other_users_holder_cannot_be_used(api, wasabi):
    await database.holders_collection.insert_one(
        {"holder_id": 55, "owner_id": "alice", "card_type_id": 7, "status": "pass_audit"})
    r = await api.post("/api/v1/cards/create", json={"cardTypeId": 7, "holderId": 55, "amount": "10"},
                       headers={**auth("mallory"), **idem()})
    assert r.status_code == 404 and wasabi["create_card"] == 0


async def test_idempotency_key_prevents_double_create(api, wasabi):
    body = {"cardTypeId": 8, "amount": "10"}
    for _ in range(3):
        r = await api.post("/api/v1/cards/create", json=body, headers={**auth(), **idem()})
        assert r.status_code == 200
    assert wasabi["create_card"] == 1
    assert (await api.post("/api/v1/cards/create", json=body, headers=auth())).status_code == 422  # key required


async def test_amount_validation_and_limit(api, wasabi):
    for amount in ("1.234", "0", "-5", "abc"):
        r = await api.post("/api/v1/cards/create", json={"cardTypeId": 8, "amount": amount},
                           headers={**auth(), **idem(f"k-{amount}-0000")})
        assert r.status_code == 422, amount
    r = await api.post("/api/v1/cards/create", json={"cardTypeId": 8, "amount": "101"},
                       headers={**auth(), **idem("over-limit-1")})
    assert r.status_code == 422 and wasabi["create_card"] == 0


async def test_funding_disabled_blocks_money_routes(api, wasabi, monkeypatch):
    from app.cards import service
    monkeypatch.setattr(service, "LIVE_MONEY_ENABLED", False)
    r = await api.post("/api/v1/cards/create", json={"cardTypeId": 8, "amount": "10"},
                       headers={**auth(), **idem("disabled-0001")})
    assert r.status_code == 503 and wasabi["create_card"] == 0


# ---------------------------------------------------------------- card ops
async def test_freeze_sends_cardNo_and_order_and_enforces_owner(api, wasabi):
    await database.cards_collection.insert_one({"card_no": "C9", "owner_id": "alice", "status": "Normal"})
    r = await api.post("/api/v1/cards/freeze", json={"cardNo": "C9"}, headers={**auth("mallory"), **idem()})
    assert r.status_code == 404 and wasabi["freeze"] == 0
    r = await api.post("/api/v1/cards/freeze", json={"cardNo": "C9"}, headers={**auth(), **idem()})
    assert r.status_code == 200 and wasabi["freeze"] == 1
    assert wasabi["last_freeze"] == {"cardNo": "C9", "merchantOrderNo": build_order_no("FRZ", "alice", "idem-key-0001")}
    assert r.json()["status"] == "PENDING"  # processing != frozen


async def test_deposit_keeps_decimals(api, wasabi):
    await database.cards_collection.insert_one({"card_no": "C9", "owner_id": "alice", "status": "Normal"})
    r = await api.post("/api/v1/cards/deposit", json={"cardNo": "C9", "amount": "12.34"},
                       headers={**auth(), **idem("dep-0000001")})
    assert r.status_code == 200 and wasabi["last_deposit"]["amount"] == 12.34


async def test_my_cards_only_lists_own(api):
    await database.cards_collection.insert_many([
        {"card_no": "A", "owner_id": "alice"}, {"card_no": "B", "owner_id": "bob"}])
    r = await api.get("/api/v1/cards/list-BINS", headers=auth())
    assert [c["card_no"] for c in r.json()["records"]] == ["A"]


# ---------------------------------------------------------------- failure semantics
async def test_timeout_is_unknown_not_failed_and_not_resent(api, wasabi, monkeypatch):
    attempts = {"n": 0}

    async def slow(payload):
        attempts["n"] += 1
        raise wc.WasabiUnknownOutcome("timeout")

    monkeypatch.setattr(wc, "create_card", slow)
    body = {"cardTypeId": 8, "amount": "10"}
    r1 = await api.post("/api/v1/cards/create", json=body, headers={**auth(), **idem("timeout-0001")})
    assert r1.status_code == 202 and r1.json()["status"] == "UNKNOWN"
    r2 = await api.post("/api/v1/cards/create", json=body, headers={**auth(), **idem("timeout-0001")})
    assert r2.json()["status"] == "UNKNOWN" and attempts["n"] == 1  # no second charge


async def test_provider_rejection_is_failed(api, wasabi, monkeypatch):
    async def reject(payload):
        raise wc.WasabiError(1234, "insufficient balance")

    monkeypatch.setattr(wc, "create_card", reject)
    r = await api.post("/api/v1/cards/create", json={"cardTypeId": 8, "amount": "10"},
                       headers={**auth(), **idem("reject-0001")})
    assert r.status_code == 422
    op = await database.transactions_collection.find_one({"type": "CREATE_CARD"})
    assert op["status"] == "FAILED"


# ---------------------------------------------------------------- cardholder
HOLDER = {
    "cardTypeId": 7, "areaCode": "+254", "mobile": "712345678", "email": "a@b.co",
    "firstName": "Jane", "lastName": "Doe", "birthday": "1990-01-01", "nationality": "KE",
    "country": "KE", "town": "Nairobi", "address": "1 Main St", "postCode": "00100", "gender": "F",
    "idType": "DLN", "idNumber": "12345678", "issueDate": "2020-01-01", "idNoExpiryDate": "2030-01-01",
    "idFrontId": "f1", "idHoldId": "h1",
}


async def test_holder_requires_id_back_unless_passport_and_uses_real_ip(api, monkeypatch):
    r = await api.post("/api/v1/cards/cardholder/register", json=HOLDER, headers={**auth(), **idem()})
    assert r.status_code == 422  # DLN without idBackId

    sent = {}

    async def create(payload):
        sent.update(payload)
        return {"success": True, "data": {"holderId": 77, "status": "wait_audit"}}

    monkeypatch.setattr(wc, "create_cardholder", create)
    r = await api.post("/api/v1/cards/cardholder/register", json={**HOLDER, "idBackId": "b1", "ipAddress": "6.6.6.6"},
                       headers={**auth(), **idem()})
    assert r.status_code == 200, r.text
    assert sent["ipAddress"] and sent["ipAddress"] != "6.6.6.6"  # server-set; client value ignored
    assert "annualSalary" not in sent  # no fabricated defaults
    holder = await database.holders_collection.find_one({"holder_id": 77})
    assert holder["owner_id"] == "alice" and "firstName" not in holder


async def test_holder_review_summary_explains_who_approves(api, monkeypatch):
    """Wasabi approves, not the merchant: the API must say so and name the reviewer."""
    async def create(payload):
        return {"success": True, "data": {"holderId": 78, "status": "wait_audit",
                                          "statusFlowLocation": "admin"}}

    async def types():
        return CARD_TYPES

    monkeypatch.setattr(wc, "create_cardholder", create)
    monkeypatch.setattr(wc, "get_card_types", types)
    await api.post("/api/v1/cards/cardholder/register", json={**HOLDER, "idBackId": "b1"},
                   headers={**auth(), **idem()})

    r = await api.get("/api/v1/cards/cardholder/78", headers=auth())
    assert r.status_code == 200
    review = r.json()["review"]
    assert review["approved"] is False
    assert review["status"] == "wait_audit"
    assert review["status_label"] == "Pending review"
    assert review["reviewer"] == "Wasabi platform review"
    assert "Wasabi" in review["next_step"]

    # The blocked-create error must name the reviewer, not just the raw status.
    r = await api.post("/api/v1/cards/create", json={"cardTypeId": 7, "holderId": 78, "amount": "10"},
                       headers={**auth(), **idem("blocked-create-1")})
    assert r.status_code == 409
    assert "Wasabi" in r.json()["detail"]

    # Once Wasabi approves, the summary flips to approved.
    await database.holders_collection.update_one(
        {"holder_id": 78}, {"$set": {"status": "pass_audit"}})
    r = await api.get("/api/v1/cards/cardholder/78", headers=auth())
    review = r.json()["review"]
    assert review["approved"] is True
    assert review["status_label"] == "Approved"
    assert "create a card" in review["next_step"]


async def test_holder_update_only_allowed_after_rejection(api, monkeypatch):
    """Wasabi v2 update is only valid when the holder was rejected."""
    await database.holders_collection.insert_one(
        {"holder_id": 90, "owner_id": "alice", "card_type_id": 7, "status": "wait_audit"})

    # Not rejected yet -> blocked before any provider call.
    r = await api.put("/api/v1/cards/cardholder/90", json={**HOLDER, "idBackId": "b1"},
                      headers={**auth(), **idem("upd-not-rejected")})
    assert r.status_code == 409 and "rejection" in r.json()["detail"]

    # Rejected -> allowed, and the payload carries holderId + server-set order no + real IP.
    sent = {}

    async def update(payload):
        sent.update(payload)
        return {"success": True, "data": {"holderId": 90, "status": "wait_audit",
                                          "statusFlowLocation": "admin"}}

    monkeypatch.setattr(wc, "update_cardholder", update)
    await database.holders_collection.update_one({"holder_id": 90}, {"$set": {"status": "reject"}})
    r = await api.put("/api/v1/cards/cardholder/90", json={**HOLDER, "idBackId": "b1"},
                      headers={**auth(), **idem("upd-rejected")})
    assert r.status_code == 200, r.text
    assert sent["holderId"] == 90
    assert sent["merchantOrderNo"].startswith("HLD")
    assert sent["ipAddress"]

    # Another user's holder is invisible.
    r = await api.put("/api/v1/cards/cardholder/90", json={**HOLDER, "idBackId": "b1"},
                      headers={**auth("mallory"), **idem("upd-other-user")})
    assert r.status_code == 404


# ---------------------------------------------------------------- upload
async def test_upload_validation(api):
    files = {"file": ("x.png", b"not really a png", "image/png")}
    assert (await api.post("/api/v1/cards/kyc/upload", files=files, headers=auth())).status_code == 415
    files = {"file": ("x.gif", b"GIF89a", "image/gif")}
    assert (await api.post("/api/v1/cards/kyc/upload", files=files, headers=auth())).status_code == 415
    files = {"file": ("x.pdf", b"%PDF-" + b"0" * (2 * 1024 * 1024), "application/pdf")}
    assert (await api.post("/api/v1/cards/kyc/upload", files=files, headers=auth())).status_code == 413


# ---------------------------------------------------------------- allowed card types
async def test_only_configured_card_types_can_be_used(api, wasabi):
    r = await api.post("/api/v1/cards/create", json={"cardTypeId": 111028, "amount": "10"},
                       headers={**auth(), **idem("type-block-01")})
    assert r.status_code == 422 and wasabi["create_card"] == 0
    r = await api.post("/api/v1/cards/cardholder/register", json={**HOLDER, "cardTypeId": 111028, "idBackId": "b"},
                       headers={**auth(), **idem("type-block-02")})
    assert r.status_code == 422


async def test_catalog_only_lists_offered_card_types(api, wasabi):
    r = await api.post("/api/v1/cards/supported-BINS/types", headers=auth())
    assert r.status_code == 200
    assert [c["cardTypeId"] for c in r.json()["data"]] == [8, 7] or sorted(c["cardTypeId"] for c in r.json()["data"]) == [7, 8]
    assert all(c["cardTypeId"] in {7, 8, 111078} for c in r.json()["data"])
