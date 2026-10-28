# =========================================================
# reconcile.py - recover from missed callbacks (run every few minutes: cron / k8s CronJob)
#     python -m app.jobs.reconcile
# =========================================================

import asyncio
import logging
from datetime import datetime, timedelta

from app.cards import service
from app.cards import wasabi_client
from app.core import database, treasury
from app.mobilemoney import client as gateway_client
from app.mobilemoney import service as mobilemoney
from app.mpesa import client as mpesa_client
from app.partners import events as partner_events
from app.mpesa import topup
from app.transactions import orchestrator as ops

logger = logging.getLogger("reconcile")
MIN_AGE = timedelta(minutes=2)  # give the normal callback a chance first


async def run_once() -> dict:
    now = datetime.utcnow()
    stats = {"ops": 0, "settled": 0, "topups": 0, "payouts": 0, "partner_events": 0, "partner_kyc": 0, "treasury": None, "ledger_mismatch": 0}

    # 1. Wasabi operations still in flight
    async for op in database.transactions_collection.find(
            {"status": {"$in": [ops.INITIATED, ops.PENDING, ops.UNKNOWN]}, "created_at": {"$lt": now - MIN_AGE}}):
        try:
            await service.reconcile_operation(op["merchant_order_no"])
            stats["ops"] += 1
        except Exception:  # noqa: BLE001
            logger.exception("reconcile failed for %s", op["merchant_order_no"])

    # 2. Final operations whose wallet effect never got applied (crash between steps)
    async for op in database.transactions_collection.find(
            {"status": {"$in": list(ops.FINAL_STATUSES)}, "funding": {"$ne": None}, "funding_settled": False}):
        await service.settle_funding(op)
        stats["settled"] += 1

    # 3. M-Pesa prompts that never got a callback
    async for t in database.topups_collection.find(
            {"status": topup.PENDING, "created_at": {"$lt": now - MIN_AGE}}):
        await topup.settle(t["checkout_request_id"])
        stats["topups"] += 1

    # 4. Wallet withdrawals (gateway payouts) whose callback never arrived
    stats["payouts"] = await mobilemoney.settle_pending_payouts()

    # 5. Reseller webhooks: retry undelivered events, and catch KYC decisions whose Wasabi webhook was missed
    stats["partner_kyc"] = await partner_events.sync_pending_holders()
    stats["partner_events"] = await partner_events.deliver_due()

    # 6. Real USD at Wasabi vs partner floats (throttled; alerts only on change)
    stats["treasury"] = (await treasury.check_and_alert()).get("status")

    # 7. Wallet counters must equal the ledger
    from app.wallet import ledger
    for owner in await database.wallets_collection.distinct("owner_id"):
        if not (await ledger.verify_balance(owner))["consistent"]:
            logger.error("LEDGER MISMATCH for owner %s", owner)
            stats["ledger_mismatch"] += 1
    return stats


async def main():
    logging.basicConfig(level=logging.INFO)
    try:
        print(await run_once())
    finally:
        await wasabi_client.close_client()
        await mpesa_client.close_client()
        await gateway_client.close_client()


if __name__ == "__main__":
    asyncio.run(main())
