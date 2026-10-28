# =========================================================
# tests/test_users_journey.py - accounts, authentication, email verification
#                              and the live application journey
# =========================================================

import asyncio
import json

import httpx
import pytest

from app.core import ratelimit
from app.core.security import create_access_token
from app.main import app
import app.core.database as database
from app.users import service as user_service

REGISTER = {
    "email": "jane@example.com",
    "password": "Str0ng!Passw0rd",
    "fullName": "Jane Doe",
    "phone": "0712345678",
    "acceptedTerms": True,
}


@pytest.fixture(autouse=True)
def reset_limiters():
    """Rate limiters are module-level; clear them so tests do not bleed into each other."""
    for limiter in (ratelimit.login_limiter, ratelimit.register_limiter, ratelimit.verify_limiter):
        limiter._buckets.clear()
    yield


@pytest.fixture
async def api(fake_db):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def register_user(api, **overrides):
    body = {**REGISTER, **overrides}
    r = await api.post("/api/v1/users/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def bearer(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': user_id})}"}


async def verify_email_for(api, user_id: str):
    """Complete the email verification step for a user."""
    token = await user_service.issue_token(user_id, "email_verification")
    r = await api.post("/api/v1/users/verify-email", json={"token": token})
    assert r.status_code == 200
    return token


# =========================================================
# REGISTRATION
# =========================================================
async def test_register_creates_account_and_returns_token(api):
    data = await register_user(api)
    assert data["token_type"] == "bearer"
    assert data["access_token"]
    assert data["user"]["email"] == REGISTER["email"]
    assert data["user"]["emailVerified"] is False
    assert data["user"]["status"] == "pending_verification"
    assert "password" not in data["user"]
    assert "password_hash" not in data["user"]

    stored = await database.users_collection.find_one({"email": REGISTER["email"]})
    assert stored["password_hash"] != REGISTER["password"]      # hashed, not plaintext
    assert stored["password_hash"].startswith("$2b$")            # bcrypt


async def test_register_rejects_duplicate_email(api):
    await register_user(api)
    r = await api.post("/api/v1/users/register", json=REGISTER)
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


@pytest.mark.parametrize("password", [
    "short1!",        # too short
    "alllowercase1!", # no upper case
    "ALLUPPERCASE1!", # no lower case
    "NoDigitsHere!",  # no number
    "NoSpecial123",   # no special character
])
async def test_register_enforces_password_policy(api, password):
    r = await api.post("/api/v1/users/register", json={**REGISTER, "password": password})
    assert r.status_code == 422


async def test_register_requires_accepted_terms(api):
    r = await api.post("/api/v1/users/register", json={**REGISTER, "acceptedTerms": False})
    assert r.status_code == 422


async def test_register_rejects_invalid_email(api):
    r = await api.post("/api/v1/users/register", json={**REGISTER, "email": "not-an-email"})
    assert r.status_code == 422


# =========================================================
# LOGIN
# =========================================================
async def test_login_succeeds_with_correct_credentials(api):
    created = await register_user(api)
    r = await api.post("/api/v1/users/login", json={"email": REGISTER["email"],
                                                    "password": REGISTER["password"]})
    assert r.status_code == 200
    assert r.json()["access_token"]
    assert r.json()["user"]["userId"] == created["user"]["userId"]


async def test_login_rejects_wrong_password(api):
    await register_user(api)
    r = await api.post("/api/v1/users/login", json={"email": REGISTER["email"], "password": "Wr0ng!Pass"})
    assert r.status_code == 401
    assert "Invalid email or password" in r.json()["detail"]


async def test_login_does_not_leak_whether_email_exists(api):
    """Unknown email and wrong password must be indistinguishable."""
    await register_user(api)
    unknown = await api.post("/api/v1/users/login", json={"email": "nobody@example.com",
                                                          "password": "Wr0ng!Pass"})
    wrong = await api.post("/api/v1/users/login", json={"email": REGISTER["email"],
                                                        "password": "Wr0ng!Pass"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_account_locks_after_repeated_failures(api):
    await register_user(api)
    for _ in range(5):
        r = await api.post("/api/v1/users/login", json={"email": REGISTER["email"],
                                                        "password": "Wr0ng!Pass"})
        assert r.status_code == 401
    # Sixth attempt: locked out even with the right password.
    r = await api.post("/api/v1/users/login", json={"email": REGISTER["email"],
                                                    "password": REGISTER["password"]})
    assert r.status_code == 423
    assert "locked" in r.json()["detail"].lower()


# =========================================================
# EMAIL VERIFICATION
# =========================================================
async def test_email_verification_flow(api, monkeypatch):
    sent = {}

    async def fake_send(to_email, full_name, token):
        sent["email"] = to_email
        sent["token"] = token
        return True

    monkeypatch.setattr("app.users.router.mail.send_verification_email", fake_send)
    created = await register_user(api)
    assert sent["email"] == REGISTER["email"]
    assert sent["token"]

    r = await api.post("/api/v1/users/verify-email", json={"token": sent["token"]})
    assert r.status_code == 200
    assert r.json()["emailVerified"] is True
    assert r.json()["status"] == "active"

    # The token is single-use.
    r = await api.post("/api/v1/users/verify-email", json={"token": sent["token"]})
    assert r.status_code == 400

    user = await database.users_collection.find_one({"user_id": created["user"]["userId"]})
    assert user["email_verified"] is True


async def test_resend_verification_does_not_leak_account_existence(api, monkeypatch):
    calls = []

    async def fake_send(to_email, full_name, token):
        calls.append(to_email)
        return True

    monkeypatch.setattr("app.users.router.mail.send_verification_email", fake_send)

    r = await api.post("/api/v1/users/resend-verification", json={"email": "nobody@example.com"})
    assert r.status_code == 200
    assert not calls            # no account -> no email sent

    await register_user(api)
    calls.clear()               # ignore the registration email itself

    r = await api.post("/api/v1/users/resend-verification", json={"email": REGISTER["email"]})
    assert r.status_code == 200
    assert calls == [REGISTER["email"]]

    # An already-verified account gets no new email.
    await verify_email_for(api, (await database.users_collection.find_one(
        {"email": REGISTER["email"]}))["user_id"])
    calls.clear()
    r = await api.post("/api/v1/users/resend-verification", json={"email": REGISTER["email"]})
    assert r.status_code == 200
    assert not calls


async def test_me_requires_a_real_account(api):
    """A token for a user that does not exist must not work."""
    r = await api.get("/api/v1/users/me", headers=bearer("does-not-exist"))
    assert r.status_code == 401


async def test_me_returns_the_signed_in_user(api):
    created = await register_user(api)
    r = await api.get("/api/v1/users/me", headers=bearer(created["user"]["userId"]))
    assert r.status_code == 200
    assert r.json()["email"] == REGISTER["email"]


async def test_change_password_requires_the_current_one(api):
    created = await register_user(api)
    headers = bearer(created["user"]["userId"])

    r = await api.post("/api/v1/users/change-password",
                       json={"currentPassword": "Wr0ng!Pass", "newPassword": "N3w!Passw0rd"},
                       headers=headers)
    assert r.status_code == 401

    r = await api.post("/api/v1/users/change-password",
                       json={"currentPassword": REGISTER["password"], "newPassword": "N3w!Passw0rd"},
                       headers=headers)
    assert r.status_code == 200

    # The new password works.
    r = await api.post("/api/v1/users/login", json={"email": REGISTER["email"],
                                                    "password": "N3w!Passw0rd"})
    assert r.status_code == 200


# =========================================================
# THE JOURNEY
# =========================================================
async def test_journey_starts_at_the_first_step(api):
    created = await register_user(api)
    r = await api.get("/api/v1/journey", headers=bearer(created["user"]["userId"]))
    assert r.status_code == 200
    journey = r.json()
    assert journey["currentStep"] == "VERIFY_EMAIL"
    assert journey["progress"] == 14          # 1 of 7 steps complete
    assert journey["completedSteps"] == 1
    assert journey["totalSteps"] == 7
    assert [s["key"] for s in journey["steps"]][:3] == ["ACCOUNT", "VERIFY_EMAIL", "KYC_DOCUMENTS"]
    assert journey["steps"][0]["status"] == "completed"
    assert journey["steps"][1]["status"] == "in_progress"
    assert "Verify your email" in journey["summary"]


async def test_journey_advances_as_the_application_progresses(api, monkeypatch):
    created = await register_user(api)
    user_id = created["user"]["userId"]

    async def fake_send(to_email, full_name, token):
        return True

    monkeypatch.setattr("app.users.router.mail.send_verification_email", fake_send)
    await verify_email_for(api, user_id)

    # Verified -> KYC documents become the current step.
    journey = (await api.get("/api/v1/journey", headers=bearer(user_id))).json()
    assert journey["currentStep"] == "KYC_DOCUMENTS"
    assert journey["emailVerified"] is True

    # Upload all three documents.
    for kind in ("front", "back", "selfie"):
        await database.kyc_documents_collection.insert_one(
            {"owner_id": user_id, "file_id": f"file-{kind}", "kind": kind})

    journey = (await api.get("/api/v1/journey", headers=bearer(user_id))).json()
    assert journey["currentStep"] == "KYC_SUBMISSION"
    assert journey["kycDocuments"] == ["back", "front", "selfie"]

    # Cardholder registered and pending review.
    await database.holders_collection.insert_one({
        "holder_id": 501, "owner_id": user_id, "card_type_id": 111078,
        "status": "wait_audit", "status_flow_location": "admin"})

    journey = (await api.get("/api/v1/journey", headers=bearer(user_id))).json()
    assert journey["currentStep"] == "KYC_REVIEW"
    assert journey["holder"]["status"] == "wait_audit"
    assert "compliance team" in journey["summary"]

    # Approved -> card issuance becomes the current step.
    await database.holders_collection.update_one(
        {"holder_id": 501}, {"$set": {"status": "pass_audit"}})
    journey = (await api.get("/api/v1/journey", headers=bearer(user_id))).json()
    assert journey["currentStep"] == "CARD_ISSUANCE"
    assert journey["steps"][4]["status"] == "completed"

    # Card issued -> funding becomes the current step.
    await database.cards_collection.insert_one({
        "card_no": "CARD1", "owner_id": user_id, "status": "Normal"})
    journey = (await api.get("/api/v1/journey", headers=bearer(user_id))).json()
    assert journey["currentStep"] == "FUNDING"
    assert journey["card"]["cardNo"] == "CARD1"

    # Funded -> complete.
    await database.wallets_collection.insert_one(
        {"owner_id": user_id, "available": 100000, "held": 0})
    journey = (await api.get("/api/v1/journey", headers=bearer(user_id))).json()
    assert journey["currentStep"] is None
    assert journey["progress"] == 100
    assert journey["funded"] is True


async def test_journey_marks_a_rejected_holder_as_blocked(api):
    created = await register_user(api)
    user_id = created["user"]["userId"]
    await verify_email_for(api, user_id)
    await database.holders_collection.insert_one({
        "holder_id": 502, "owner_id": user_id, "status": "reject"})

    journey = (await api.get("/api/v1/journey", headers=bearer(user_id))).json()
    assert journey["currentStep"] == "KYC_REVIEW"
    assert journey["steps"][4]["status"] == "blocked"
    assert "rejected" in journey["summary"].lower()


# =========================================================
# REALTIME
# =========================================================
async def test_event_bus_delivers_to_subscribers():
    """A published event must reach a live subscriber's queue."""
    from app.journey.events import bus, user_topic

    topic = user_topic("user-x")
    received = []

    async def listener():
        async for event in bus.subscribe(topic):
            received.append(event)
            return

    task = asyncio.create_task(listener())
    # Give the subscriber a moment to register before publishing.
    for _ in range(50):
        if await bus.subscriber_count(topic):
            break
        await asyncio.sleep(0.01)
    assert await bus.subscriber_count(topic) == 1

    await bus.publish(topic, {"type": "journey", "reason": "kyc_status_changed"})
    await asyncio.wait_for(task, timeout=5)

    assert received
    assert received[0]["event"] == "journey"
    assert received[0]["data"]["reason"] == "kyc_status_changed"
    assert "ts" in received[0]["data"]          # every event is timestamped


async def test_event_bus_isolates_topics():
    """One user's events must never leak to another user's stream."""
    from app.journey.events import bus, user_topic

    mine, theirs = [], []

    async def listener(topic, sink):
        async for event in bus.subscribe(topic):
            sink.append(event)
            return

    t1 = asyncio.create_task(listener(user_topic("u1"), mine))
    t2 = asyncio.create_task(listener(user_topic("u2"), theirs))
    for _ in range(50):
        if await bus.subscriber_count(user_topic("u1")) and await bus.subscriber_count(user_topic("u2")):
            break
        await asyncio.sleep(0.01)

    await bus.publish(user_topic("u1"), {"type": "journey", "secret": "mine"})
    await asyncio.wait_for(t1, timeout=5)
    await asyncio.sleep(0.05)

    assert mine and mine[0]["data"]["secret"] == "mine"
    assert not theirs                      # u2 received nothing


async def test_sse_frame_formatting():
    """A Server-Sent Event frame must be `event:` + `data:` + blank line."""
    from app.journey.router import _sse

    frame = _sse("journey", {"reason": "kyc_status_changed", "journey": {"progress": 50}})
    assert frame.startswith("event: journey\n")
    assert "\ndata: " in frame
    assert frame.endswith("\n\n")
    body = json.loads(frame.split("data: ", 1)[1].strip())
    assert body["reason"] == "kyc_status_changed"
    assert body["journey"]["progress"] == 50


async def test_journey_endpoint_is_built_for_sse():
    """The stream endpoint must return a StreamingResponse (Server-Sent Events)."""
    from fastapi.responses import StreamingResponse

    from app.main import app

    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/v1/journey/stream")
    assert route.response_class is StreamingResponse
    # The route must require authentication.
    assert any(getattr(d, "call", None) is not None for d in route.dependant.dependencies)


async def test_journey_events_history_links_webhooks_to_the_user(api):
    created = await register_user(api)
    user_id = created["user"]["userId"]
    await database.webhook_events_collection.insert_one({
        "request_id": "req-1", "category": "card_holder", "state": "processed",
        "owner_id": user_id})

    r = await api.get("/api/v1/journey/events/history", headers=bearer(user_id))
    assert r.status_code == 200
    records = r.json()["records"]
    assert len(records) == 1
    assert records[0]["requestId"] == "req-1"
    assert records[0]["category"] == "card_holder"


# =========================================================
# DEV-ONLY EMAIL PREVIEW
# =========================================================
async def test_dev_verification_link_is_available_in_development(api, monkeypatch):
    monkeypatch.setattr("app.users.router.ENABLE_DEV_EMAIL_PREVIEW", True)
    await register_user(api)
    r = await api.get(f"/api/v1/users/dev/verification-link?email={REGISTER['email']}")
    assert r.status_code == 200
    body = r.json()
    assert body["token"]
    assert body["verificationLink"].startswith("http")
    assert "token=" in body["verificationLink"]


async def test_dev_verification_link_is_hidden_when_disabled(api, monkeypatch):
    monkeypatch.setattr("app.users.router.ENABLE_DEV_EMAIL_PREVIEW", False)
    await register_user(api)
    r = await api.get(f"/api/v1/users/dev/verification-link?email={REGISTER['email']}")
    assert r.status_code == 404


# =========================================================
# SSE AUTHENTICATION
# =========================================================
async def test_sse_stream_accepts_the_token_in_the_query_string(api, monkeypatch):
    """
    The browser's EventSource cannot set an Authorization header, so the SSE
    endpoint must also accept ?token=. A bad or missing token must be rejected.

    The guard is exercised directly: requesting the stream through the ASGI test
    transport would buffer forever, because the SSE generator never finishes.
    """
    from fastapi import HTTPException

    from app.core.security import sse_auth_guard

    created = await register_user(api)
    user_id = created["user"]["userId"]

    class FakeRequest:
        def __init__(self, query):
            self.query_params = query

    valid = create_access_token({"sub": user_id})

    # Token in the query string is accepted.
    claims = await sse_auth_guard(FakeRequest({"token": valid}), None)
    assert claims["id"] == user_id

    # A token in the header is accepted too.
    from fastapi.security import HTTPAuthorizationCredentials

    header_creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=valid)
    claims = await sse_auth_guard(FakeRequest({}), header_creds)
    assert claims["id"] == user_id

    # No token at all is rejected.
    with pytest.raises(HTTPException) as exc:
        await sse_auth_guard(FakeRequest({}), None)
    assert exc.value.status_code == 401

    # A token for a user that does not exist is rejected.
    ghost = create_access_token({"sub": "does-not-exist"})
    with pytest.raises(HTTPException) as exc:
        await sse_auth_guard(FakeRequest({"token": ghost}), None)
    assert exc.value.status_code == 401


async def test_sse_stream_endpoint_requires_authentication(api):
    """The stream route must be protected by the SSE guard, not open."""
    from app.main import app

    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/v1/journey/stream")
    assert route is not None
    # Rejecting an anonymous request must not hang: the guard fails before the
    # stream generator starts.
    r = await api.get("/api/v1/journey/stream")
    assert r.status_code == 401


# =========================================================
# KYC DOCUMENT TRACKING
# =========================================================
async def test_kyc_documents_endpoint_lists_the_callers_uploads(api, monkeypatch):
    from app.cards import wasabi_client as wc

    async def upload(file_bytes, filename, content_type):
        return {"success": True, "data": {"fileId": f"file-{filename}"}}

    monkeypatch.setattr(wc, "upload_wasabi_file", upload)
    created = await register_user(api)
    user_id = created["user"]["userId"]
    headers = bearer(user_id)

    r = await api.post("/api/v1/cards/kyc/upload", headers=headers,
                       files={"file": ("id.png", b"\x89PNG\r\n\x1a\nrest", "image/png")},
                       data={"kind": "front"})
    assert r.status_code == 200
    assert r.json()["kind"] == "front"
    assert r.json()["fileId"]

    r = await api.get("/api/v1/cards/kyc/documents", headers=headers)
    assert r.status_code == 200
    records = r.json()["records"]
    assert len(records) == 1
    assert records[0]["kind"] == "front"
    assert records[0]["fileId"]


async def test_kyc_upload_rejects_an_unknown_kind(api):
    created = await register_user(api)
    r = await api.post("/api/v1/cards/kyc/upload", headers=bearer(created["user"]["userId"]),
                       files={"file": ("id.png", b"\x89PNG\r\n\x1a\nrest", "image/png")},
                       data={"kind": "sideways"})
    assert r.status_code == 422


# =========================================================
# RATE LIMITING
# =========================================================
async def test_login_is_rate_limited(api):
    await register_user(api)
    for _ in range(8):
        await api.post("/api/v1/users/login", json={"email": REGISTER["email"],
                                                    "password": "Wr0ng!Pass"})
    r = await api.post("/api/v1/users/login", json={"email": REGISTER["email"],
                                                    "password": "Wr0ng!Pass"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
