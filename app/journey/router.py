# =========================================================
# journey/router.py - the customer's application tracker
# =========================================================
#
# GET  /api/v1/journey         -> the full ordered journey (poll once on page load)
# GET  /api/v1/journey/stream  -> Server-Sent Events: live updates as things happen
#
# The stream is what makes the application feel alive: when Wasabi's webhook says
# the cardholder was approved, the browser updates without the user refreshing.

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.core.security import current_user_guard, sse_auth_guard
from app.journey import state as journey_state
from app.journey.events import bus, user_topic

logger = logging.getLogger("journey")
router = APIRouter()

# Keep-alive comment every N seconds so nginx/proxies do not drop an idle connection.
HEARTBEAT_SECONDS = 20


@router.get("")
async def get_journey(user: dict = Depends(current_user_guard)):
    """The ordered application journey for the signed-in user."""
    return await journey_state.get_journey(user["user_id"])


@router.get("/stream", response_class=StreamingResponse)
async def journey_stream(request: Request, claims: dict = Depends(sse_auth_guard)):
    """
    Server-Sent Events stream of journey updates.

    Emits:
      event: journey  - the complete journey snapshot
      event: ping    - keep-alive
    """
    topic = user_topic(claims["id"])
    user_id = claims["id"]

    async def event_stream():
        # Send the current state immediately so the client renders without waiting.
        snapshot = await journey_state.get_journey(user_id)
        yield _sse("journey", {"reason": "initial", "journey": snapshot})

        try:
            async for event in bus.subscribe(topic):
                if await request.is_disconnected():
                    break
                yield _sse(event.get("event", "update"), event.get("data", {}))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let a stream error kill the response
            logger.exception("journey stream failed for %s", user_id)
        finally:
            logger.info("journey stream closed for %s", user_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Stop nginx from buffering the stream into uselessness.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/events/history")
async def journey_events(request: Request, user: dict = Depends(current_user_guard)):
    """
    Recent journey events, for clients that cannot hold an SSE connection
    (or for auditing what the user was told and when).
    """
    from app.core.database import webhook_events_collection

    events = []
    async for doc in webhook_events_collection.find(
            {"owner_id": user["user_id"]}
    ).sort("received_at", -1).limit(50):
        events.append({
            "requestId": doc.get("request_id"),
            "category": doc.get("category"),
            "state": doc.get("state"),
            "receivedAt": doc.get("received_at").isoformat() if doc.get("received_at") else None,
            "processedAt": doc.get("processed_at").isoformat() if doc.get("processed_at") else None,
        })
    return {"records": events}


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
