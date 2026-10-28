# =========================================================
# partners/events.py - signed outbound webhooks to resellers (outbox + retries)
# =========================================================
#
# Every event is stored first, then delivered; a failed delivery is retried with backoff by the reconcile job,
# so a reseller that is down for a while still gets every event. Same scheme our own gateway uses with us:
#   X-Mamlaka-Signature: sha256=<hex HMAC-SHA256 of the exact raw body, keyed with the tenant's signing secret>
# Consumers must treat events as at-least-once and dedupe on `id`.

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

import httpx

from app.core import database

logger = logging.getLogger("partner-events")

BACKOFF_SECONDS = [60, 300, 1800, 7200, 21600, 86400, 86400]   # then FAILED after the last one
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
_tasks: set = set()   # keep references so fire-and-forget deliveries are not garbage collected


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def emit(tenant_id: str, event_type: str, data: dict) -> dict:
    event = {"event_id": uuid.uuid4().hex, "tenant_id": tenant_id, "type": event_type, "data": data,
             "status": "PENDING", "attempts": 0, "created_at": datetime.utcnow(),
             "next_attempt_at": datetime.utcnow()}
    await database.partner_events_collection.insert_one(event)
    try:  # try right away without delaying the caller; the reconcile job is the safety net
        task = asyncio.get_running_loop().create_task(deliver(event))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except RuntimeError:
        pass
    return event


async def emit_for_owner(owner_id: str, event_type: str, data: dict) -> Optional[dict]:
    """No-op unless owner_id belongs to a reseller's customer. Adds the reseller's own customer id."""
    customer = await database.partner_customers_collection.find_one({"owner_id": owner_id})
    if not customer:
        return None
    return await emit(customer["tenant_id"], event_type, {**data, "customerId": customer["external_id"]})


def _body(event: dict) -> bytes:
    return json.dumps({"id": event["event_id"], "type": event["type"],
                       "createdAt": event["created_at"].isoformat() + "Z", "data": event["data"]},
                      separators=(",", ":"), sort_keys=True, default=str).encode()


async def deliver(event: dict) -> bool:
    """One delivery attempt. 2xx = delivered; anything else schedules a retry."""
    current = await database.partner_events_collection.find_one({"event_id": event["event_id"]})
    if not current or current["status"] != "PENDING":
        return current is not None and current["status"] == "DELIVERED"
    tenant = await database.tenants_collection.find_one({"tenant_id": current["tenant_id"]})
    if not tenant or not tenant.get("webhook_url"):
        await _mark(current, status="SKIPPED", error="no webhook endpoint configured")
        return False

    body = _body(current)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(tenant["webhook_url"], content=body, headers={
                "Content-Type": "application/json", "X-Mamlaka-Event-Id": current["event_id"],
                "X-Mamlaka-Signature": sign(body, tenant["signing_secret"])})
        ok, error = r.is_success, None if r.is_success else f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        ok, error = False, type(exc).__name__

    attempts = current["attempts"] + 1
    if ok:
        await _mark(current, status="DELIVERED", attempts=attempts)
    elif attempts >= MAX_ATTEMPTS:
        logger.error("partner event %s gave up after %s attempts (%s)", current["event_id"], attempts, error)
        await _mark(current, status="FAILED", attempts=attempts, error=error)
    else:
        delay = BACKOFF_SECONDS[attempts - 1]
        await _mark(current, attempts=attempts, error=error, next_attempt_at=datetime.utcnow() + timedelta(seconds=delay))
    return ok


async def _mark(event: dict, **fields):
    await database.partner_events_collection.update_one(
        {"event_id": event["event_id"]}, {"$set": {**fields, "updated_at": datetime.utcnow()}})


async def deliver_due(limit: int = 50) -> int:
    count = 0
    async for event in database.partner_events_collection.find(
            {"status": "PENDING", "next_attempt_at": {"$lte": datetime.utcnow()}}).limit(limit):
        await deliver(event)
        count += 1
    return count


async def sync_pending_holders() -> int:
    """Safety net for a missed Wasabi webhook: re-read reseller KYC still in review, announce any change."""
    from app.cards import service  # lazy: the Wasabi webhook router imports this module

    changed = 0
    async for holder in database.holders_collection.find({"status": {"$in": ["wait_audit", "under_review"]}}):
        if not await database.partner_customers_collection.find_one({"owner_id": holder["owner_id"]}):
            continue
        record = await service.refresh_holder(holder["holder_id"])
        if record and record.get("status") != holder.get("status"):
            await emit_kyc(holder["owner_id"], holder["holder_id"], record)
            changed += 1
    return changed


async def emit_kyc(owner_id: str, holder_id: int, record: dict):
    status = record.get("status")
    return await emit_for_owner(owner_id, "kyc.status_changed", {
        "holderId": holder_id, "status": status, "approved": status == "pass_audit",
        "reason": record.get("description") if status == "reject" else None})
