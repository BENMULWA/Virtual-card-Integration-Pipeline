# =========================================================
# ledger.py - KES wallet with holds (integer minor units = cents)
# =========================================================
#
# available : spendable KES            held : reserved for in-flight card operations
# Every change is a ledger entry with a UNIQUE entry_key ("<ref>:<kind>"), so credit / reserve /
# capture / release are each applied at most once however often a callback or retry replays them.
# Wallet counters are the fast, atomically-guarded view; the ledger is the audit trail, and
# `verify_balance` recomputes the counters from the ledger for reconciliation.

from datetime import datetime
from decimal import Decimal, ROUND_UP
from typing import Optional

from pymongo.errors import DuplicateKeyError

from app.core import database


class InsufficientFunds(Exception):
    pass


def to_minor(amount: Decimal) -> int:
    return int((Decimal(amount) * 100).to_integral_value(rounding=ROUND_UP))


def from_minor(minor: int) -> Decimal:
    return (Decimal(minor) / 100).quantize(Decimal("0.01"))


async def _insert_entry(owner_id: str, ref: str, kind: str, available: int, held: int, meta: Optional[dict] = None):
    """Returns False when this (ref, kind) was already recorded."""
    try:
        await database.ledger_collection.insert_one({
            "entry_key": f"{ref}:{kind}",
            "owner_id": owner_id,
            "ref": ref,
            "kind": kind,
            "available": available,   # signed deltas, minor units
            "held": held,
            "meta": meta or {},
            "created_at": datetime.utcnow(),
        })
        return True
    except DuplicateKeyError:
        return False


async def _apply(owner_id: str, available: int, held: int):
    await database.wallets_collection.update_one(
        {"owner_id": owner_id},
        {"$inc": {"available": available, "held": held}, "$set": {"updated_at": datetime.utcnow()},
         "$setOnInsert": {"currency": "KES", "created_at": datetime.utcnow()}},
        upsert=True,
    )


async def get_balance(owner_id: str) -> dict:
    wallet = await database.wallets_collection.find_one({"owner_id": owner_id}) or {}
    return {"currency": "KES", "available": from_minor(wallet.get("available", 0)),
            "held": from_minor(wallet.get("held", 0))}


# ---------------------------------------------------------------- credits
async def credit(owner_id: str, amount_minor: int, ref: str, kind: str, meta: Optional[dict] = None) -> bool:
    """Add funds (M-Pesa top-up, card withdrawal). Idempotent on (ref, kind)."""
    if amount_minor <= 0:
        raise ValueError("credit must be positive")
    if not await _insert_entry(owner_id, ref, kind, amount_minor, 0, meta):
        return False
    await _apply(owner_id, amount_minor, 0)
    return True


# ---------------------------------------------------------------- holds
async def reserve(owner_id: str, amount_minor: int, ref: str, meta: Optional[dict] = None) -> bool:
    """Move available -> held atomically. Returns False if already reserved for this ref."""
    if await database.ledger_collection.find_one({"entry_key": f"{ref}:reserve"}):
        return False
    guard = await database.wallets_collection.update_one(
        {"owner_id": owner_id, "available": {"$gte": amount_minor}},   # atomic overdraft guard
        {"$inc": {"available": -amount_minor, "held": amount_minor}, "$set": {"updated_at": datetime.utcnow()}},
    )
    if guard.modified_count != 1:
        raise InsufficientFunds()
    if not await _insert_entry(owner_id, ref, "reserve", -amount_minor, amount_minor, meta):
        await _apply(owner_id, amount_minor, -amount_minor)  # lost a race on the same ref: undo
        return False
    return True


async def _reserved_amount(ref: str) -> Optional[dict]:
    return await database.ledger_collection.find_one({"entry_key": f"{ref}:reserve"})


async def capture(ref: str) -> bool:
    """The card operation succeeded: the held funds are spent."""
    reserved = await _reserved_amount(ref)
    if not reserved:
        return False
    if await database.ledger_collection.find_one({"entry_key": f"{ref}:release"}):
        return False  # a hold is spent OR returned, never both
    amount = reserved["held"]
    if not await _insert_entry(reserved["owner_id"], ref, "capture", 0, -amount):
        return False
    await _apply(reserved["owner_id"], 0, -amount)
    return True


async def release(ref: str) -> bool:
    """The card operation failed: give the held funds back."""
    reserved = await _reserved_amount(ref)
    if not reserved:
        return False
    if await database.ledger_collection.find_one({"entry_key": f"{ref}:capture"}):
        return False  # never release something already captured
    amount = reserved["held"]
    if not await _insert_entry(reserved["owner_id"], ref, "release", amount, -amount):
        return False
    await _apply(reserved["owner_id"], amount, -amount)
    return True


async def verify_balance(owner_id: str) -> dict:
    """Recompute from the ledger and compare with the wallet counters (reconciliation check)."""
    available = held = 0
    async for entry in database.ledger_collection.find({"owner_id": owner_id}):
        available += entry["available"]
        held += entry["held"]
    wallet = await database.wallets_collection.find_one({"owner_id": owner_id}) or {}
    return {"ledger": (available, held),
            "wallet": (wallet.get("available", 0), wallet.get("held", 0)),
            "consistent": (available, held) == (wallet.get("available", 0), wallet.get("held", 0))}
