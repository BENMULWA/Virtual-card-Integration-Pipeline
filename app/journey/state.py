# =========================================================
# journey/state.py - the application journey for a customer
# =========================================================
#
# Turns the scattered state (user record, uploaded KYC files, Wasabi cardholder,
# Wasabi card, M-Pesa top-ups) into ONE ordered list of steps with a status and a
# concrete next action. The frontend renders this directly; the user always knows
# what is done, what is in progress, and exactly what to do next.

import logging
from datetime import datetime, timezone
from typing import Optional

from app.core import database

logger = logging.getLogger("journey")

# Ordered steps of the journey. `key` is stable - clients may rely on it.
STEP_ORDER = [
    "ACCOUNT",
    "VERIFY_EMAIL",
    "KYC_DOCUMENTS",
    "KYC_SUBMISSION",
    "KYC_REVIEW",
    "CARD_ISSUANCE",
    "FUNDING",
]

STEP_META = {
    "ACCOUNT": {
        "title": "Create your account",
        "description": "Register with your email, phone number and a strong password.",
        "action": "POST /api/v1/users/register",
    },
    "VERIFY_EMAIL": {
        "title": "Verify your email",
        "description": "Confirm your email address. We send a verification link and Wasabi sends "
                       "your KYC verification code to the same address.",
        "action": "POST /api/v1/users/verify-email",
    },
    "KYC_DOCUMENTS": {
        "title": "Upload your ID documents",
        "description": "Upload a photo of the front of your ID, the back of your ID, and a selfie. "
                       "JPG, PNG or PDF, 2 MB max each.",
        "action": "POST /api/v1/cards/kyc/upload",
    },
    "KYC_SUBMISSION": {
        "title": "Submit your KYC details",
        "description": "We send your identity details to Wasabi for compliance review. You will "
                       "receive a verification code by email.",
        "action": "POST /api/v1/cards/cardholder/register",
    },
    "KYC_REVIEW": {
        "title": "Wait for approval",
        "description": "Wasabi's compliance team reviews your submission. This usually takes a few "
                       "hours. We will update you the moment the status changes.",
        "action": "POST /api/v1/cards/kyc/submit  (only if the review is stuck)",
    },
    "CARD_ISSUANCE": {
        "title": "Get your virtual card",
        "description": "Once approved, we issue your virtual card and load it with your first deposit.",
        "action": "POST /api/v1/cards/create",
    },
    "FUNDING": {
        "title": "Fund your card",
        "description": "Top up your Mamlaka wallet with M-Pesa, then deposit onto your card. "
                       "Your card is ready to spend once it has a balance.",
        "action": "POST /api/v1/wallet/topup  then  POST /api/v1/cards/deposit",
    },
}

COMPLETED = "completed"
IN_PROGRESS = "in_progress"
PENDING = "pending"
BLOCKED = "blocked"

# Wasabi cardholder statuses.
HOLDER_APPROVED = "pass_audit"
HOLDER_REJECTED = "reject"

# Card statuses that mean the card is live and spendable.
CARD_LIVE_STATUSES = {"Normal", "pending", "un_activated"}


async def _holder_for(user_id: str) -> Optional[dict]:
    cursor = database.holders_collection.find({"owner_id": user_id}).sort("created_at", -1)
    async for doc in cursor:
        return doc
    return None


async def _card_for(user_id: str) -> Optional[dict]:
    cursor = database.cards_collection.find({"owner_id": user_id}).sort("created_at", -1)
    async for doc in cursor:
        return doc
    return None


async def _kyc_kinds(user_id: str) -> set:
    kinds = set()
    async for doc in database.kyc_documents_collection.find({"owner_id": user_id}):
        kind = doc.get("kind")
        if kind:
            kinds.add(kind)
    return kinds


async def _funded(user_id: str) -> bool:
    """Funded = the wallet holds money (top-up landed) or the card already has a balance."""
    wallet = await database.wallets_collection.find_one({"owner_id": user_id}) or {}
    if int(wallet.get("available", 0)) > 0:
        return True
    async for _ in database.topups_collection.find({"owner_id": user_id, "status": "SUCCESS"}):
        return True
    return False


async def get_journey(user_id: str) -> dict:
    """Build the full ordered journey for a user."""
    user = await database.users_collection.find_one({"user_id": user_id}) or {}
    holder = await _holder_for(user_id)
    card = await _card_for(user_id)
    kyc_kinds = await _kyc_kinds(user_id)
    funded = await _funded(user_id)

    email_verified = bool(user.get("email_verified"))
    holder_status = (holder or {}).get("status")
    card_status = (card or {}).get("status")
    card_no = (card or {}).get("card_no")

    # Per-step status, computed from the real state of each system.
    status_by_step = {
        "ACCOUNT": COMPLETED if user else IN_PROGRESS,
        "VERIFY_EMAIL": COMPLETED if email_verified else (
            IN_PROGRESS if user else PENDING),
        "KYC_DOCUMENTS": COMPLETED if {"front", "back", "selfie"} <= kyc_kinds else (
            IN_PROGRESS if email_verified else PENDING),
        "KYC_SUBMISSION": COMPLETED if holder else (
            IN_PROGRESS if {"front", "back", "selfie"} <= kyc_kinds else PENDING),
        "KYC_REVIEW": PENDING,
        "CARD_ISSUANCE": PENDING,
        "FUNDING": PENDING,
    }

    # The approval step only starts once the cardholder exists.
    if holder:
        # A holder record proves the user got past both uploading documents and
        # submitting the application - even if our document tracking is incomplete
        # (older accounts, or uploads made before tracking existed).
        status_by_step["KYC_DOCUMENTS"] = COMPLETED
        status_by_step["KYC_SUBMISSION"] = COMPLETED
        if holder_status == HOLDER_APPROVED:
            status_by_step["KYC_REVIEW"] = COMPLETED
            status_by_step["CARD_ISSUANCE"] = IN_PROGRESS if not card else COMPLETED
            status_by_step["FUNDING"] = IN_PROGRESS if (card and not funded) else (
                COMPLETED if funded else PENDING)
        elif holder_status == HOLDER_REJECTED:
            status_by_step["KYC_REVIEW"] = BLOCKED
            status_by_step["CARD_ISSUANCE"] = BLOCKED
            status_by_step["FUNDING"] = BLOCKED
        else:  # wait_audit / under_review
            status_by_step["KYC_REVIEW"] = IN_PROGRESS
            status_by_step["CARD_ISSUANCE"] = PENDING
            status_by_step["FUNDING"] = PENDING

    steps = []
    for key in STEP_ORDER:
        meta = STEP_META[key]
        step = {
            "key": key,
            "title": meta["title"],
            "description": meta["description"],
            "status": status_by_step[key],
            "action": meta["action"],
        }
        steps.append(step)

    # The current step is the first one that is not completed.
    current = next((s for s in steps if s["status"] != COMPLETED), None)
    completed = sum(1 for s in steps if s["status"] == COMPLETED)
    progress = int(round(completed / len(steps) * 100))

    # A short, human summary of where the application actually is.
    if not user:
        summary = "Start by creating your account."
    elif not email_verified:
        summary = "Verify your email address to continue."
    elif holder:
        # A holder record means the KYC was submitted, whatever our document tracking says.
        if holder_status == HOLDER_REJECTED:
            summary = "Your KYC was rejected. Correct the details and resubmit."
        elif holder_status != HOLDER_APPROVED:
            summary = ("Your KYC is with Wasabi's compliance team. "
                       "We will notify you when it is approved.")
        elif not card:
            summary = "You are approved. Request your virtual card to continue."
        elif not funded:
            summary = "Your card is ready. Top up your wallet to load it."
        else:
            summary = "Your card is active and funded. You are all set."
    elif not {"front", "back", "selfie"} <= kyc_kinds:
        missing = [k for k in ("front", "back", "selfie") if k not in kyc_kinds]
        summary = f"Upload your {', '.join(missing)} document(s) to continue."
    else:
        summary = "Submit your KYC details so we can start the compliance review."

    return {
        "userId": user_id,
        "steps": steps,
        "currentStep": current["key"] if current else None,
        "currentTitle": current["title"] if current else "Complete",
        "nextAction": current["action"] if current else None,
        "progress": progress,
        "completedSteps": completed,
        "totalSteps": len(steps),
        "summary": summary,
        "emailVerified": email_verified,
        "kycDocuments": sorted(kyc_kinds),
        "holder": {
            "holderId": (holder or {}).get("holder_id"),
            "status": holder_status,
            "statusFlowLocation": (holder or {}).get("status_flow_location"),
            "description": (holder or {}).get("description"),
        } if holder else None,
        "card": {
            "cardNo": card_no,
            "status": card_status,
            "blocked": (card or {}).get("blocked", False),
        } if card else None,
        "funded": funded,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
