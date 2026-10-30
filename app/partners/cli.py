# =========================================================
# partners/cli.py - admin commands for onboarding and funding a reseller
#   python -m app.partners.cli create-tenant --name Comet --slug comet --webhook-url https://comet.example/hooks/mamlaka
#   python -m app.partners.cli credit-float  --slug comet --amount-kes 100000 --ref BANK-TRX-123
#   python -m app.partners.cli rotate-key    --slug comet
#   python -m app.partners.cli balance       --slug comet
#   python -m app.partners.cli set-webhook   --slug comet --url https://comet.example/hooks/mamlaka
#   python -m app.partners.cli treasury      (real Wasabi USD vs what partners have prepaid)
#   python -m app.partners.cli set-margin    --slug comet --pct 1.5   (omit --pct to use PARTNER_MARGIN_PCT)
#   python -m app.partners.cli pricing       (our cost of a dollar and every rate we sell at)
#   python -m app.partners.cli earnings      --days 30   (margin kept on settled card operations)
#   python -m app.partners.cli list
# =========================================================

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from app.core import config, database
from app.partners import service
from app.wallet import ledger, pricing


def print_pricing(tenants: list) -> None:
    retail = pricing.rate_card()
    print(f"USDT buy rate (KES)    : {retail['usdtRate']}   Wasabi deposit fee: {retail['depositFeePct']}%")
    print(f"Our cost of 1 USD      : {retail['costRate']} KES")
    print(f"{'Sold to':24} {'margin':>7}  {'KES/USD':>8}  {'$10 card*':>10}  {'$10 load':>9}")
    rows = [("Retail (own app)", None)] + [(f"{t['name']} ({t['slug']})", service.margin_pct(t)) for t in tenants]
    for label, margin in rows:
        card = pricing.rate_card(margin)
        issue = pricing.quote_debit(Decimal(10), Decimal(2), margin_pct=margin)
        load = pricing.quote_debit(Decimal(10), margin_pct=margin)
        print(f"{label:24} {card['marginPct']:>6}%  {card['kesPerUsd']:>8}  "
              f"{ledger.from_minor(issue['kes_minor']):>10}  {ledger.from_minor(load['kes_minor']):>9}")
    print("* includes Wasabi's $2 card fee. Resellers add their own margin on top when they price their customers.")


async def print_earnings(days: int) -> None:
    since = datetime.utcnow() - timedelta(days=days)
    names = {service.float_owner(t["tenant_id"]): t["slug"] async for t in database.tenants_collection.find({})}
    totals = defaultdict(lambda: [0, Decimal(0), Decimal(0)])   # count, KES charged/paid, margin KES
    async for op in database.transactions_collection.find(
            {"status": "SUCCESS", "funding_settled": True, "created_at": {"$gte": since}}):
        funding = op.get("funding") or {}
        quote = funding.get("quote") or {}
        if "margin_kes" not in quote:      # priced before margins were recorded
            continue
        row = totals[names.get(funding.get("owner_id"), "retail")]
        row[0] += 1
        row[1] += ledger.from_minor(funding.get("kes_minor", 0))
        row[2] += Decimal(quote["margin_kes"])
    print(f"Margin kept on settled card operations, last {days} days")
    print(f"{'Who':16} {'ops':>5}  {'KES volume':>12}  {'margin KES':>11}")
    for who, (count, volume, margin) in sorted(totals.items()):
        print(f"{who:16} {count:>5}  {volume:>12}  {margin:>11}")
    print(f"{'Total':16} {sum(r[0] for r in totals.values()):>5}  {sum((r[1] for r in totals.values()), Decimal(0)):>12}  "
          f"{sum((r[2] for r in totals.values()), Decimal(0)):>11}")


async def run(args) -> int:
    await database.ensure_indexes()
    if args.cmd == "create-tenant":
        tenant, key = await service.create_tenant(
            args.name, args.slug, args.webhook_url,
            [int(x) for x in args.card_types.split(",")] if args.card_types else None, args.max_amount)
        print(f"Tenant created: {tenant['name']} ({tenant['tenant_id']})")
        print(f"  API key         : {key}")
        print(f"  Webhook secret  : {tenant['signing_secret']}")
        print("Give both to the reseller over a secure channel. The API key cannot be shown again.")
    elif args.cmd == "set-webhook":
        await service.set_webhook(args.slug, args.url)
        print(f"Webhook for '{args.slug}' set to: {args.url or '(none)'}")
    elif args.cmd == "treasury":
        from app.core import treasury
        snap = await treasury.snapshot()
        print(f"Status                 : {snap['status'] or 'UNKNOWN (could not read Wasabi)'}")
        print(f"Wasabi available (USD) : {snap['wasabiAvailableUsd']}")
        print(f"Partner floats (KES)   : {snap['partnerFloatsKes']}  (~{snap['partnerFloatsUsd']} USD)")
        print(f"Alert below (USD)      : {snap['alertBelowUsd']}   Reserve (USD): {snap['reserveUsd']}")
        if snap["status"] == treasury.UNDERFUNDED:
            print("!! Partners have prepaid more than the real USD you hold at Wasabi. Top up Wasabi.")
        elif snap["status"] == treasury.LOW:
            print("!  The Wasabi balance is below the alert threshold. Top up soon.")
    elif args.cmd == "rotate-key":
        print("New API key:", await service.rotate_key(args.slug), "\n(the old key stopped working)")
    elif args.cmd == "set-margin":
        await service.set_margin(args.slug, args.pct)
        print(f"Margin for '{args.slug}': {args.pct + '%' if args.pct else f'default ({config.PARTNER_MARGIN_PCT}%)'}")
    elif args.cmd == "pricing":
        print_pricing([t async for t in database.tenants_collection.find({})])
    elif args.cmd == "earnings":
        await print_earnings(args.days)
    else:
        if args.cmd == "list":
            async for t in database.tenants_collection.find({}):
                print(f"{t['slug']:16} {t['tenant_id']}  {t['status']:8} key={t['key_hint']}  webhook={t.get('webhook_url')}")
            return 0
        tenant = await service.get_tenant_by_slug(args.slug)
        if not tenant:
            print(f"No tenant '{args.slug}'")
            return 1
        owner = service.float_owner(tenant["tenant_id"])
        if args.cmd == "credit-float":
            credited = await ledger.credit(owner, int(round(args.amount_kes * 100)), ref=args.ref, kind="float_topup",
                                           meta={"by": "admin-cli"})
            print("Credited." if credited else f"Reference {args.ref} was already applied; nothing changed.")
        print("Float:", await ledger.get_balance(owner))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="partners")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-tenant")
    c.add_argument("--name", required=True)
    c.add_argument("--slug", required=True)
    c.add_argument("--webhook-url")
    c.add_argument("--card-types", help="comma separated cardTypeIds; empty = all we offer")
    c.add_argument("--max-amount", help="USD cap per card load")
    f = sub.add_parser("credit-float")
    f.add_argument("--slug", required=True)
    f.add_argument("--amount-kes", type=float, required=True)
    f.add_argument("--ref", required=True, help="unique reference (bank / payment reference); replaying it is a no-op")
    for name in ("rotate-key", "balance"):
        sub.add_parser(name).add_argument("--slug", required=True)
    w = sub.add_parser("set-webhook")
    w.add_argument("--slug", required=True)
    w.add_argument("--url", help="https endpoint on the reseller's server; omit to clear")
    m = sub.add_parser("set-margin")
    m.add_argument("--slug", required=True)
    m.add_argument("--pct", help="our margin on this reseller, in percent; omit to use PARTNER_MARGIN_PCT")
    sub.add_parser("treasury")
    sub.add_parser("pricing")
    e = sub.add_parser("earnings")
    e.add_argument("--days", type=int, default=30)
    sub.add_parser("list")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
