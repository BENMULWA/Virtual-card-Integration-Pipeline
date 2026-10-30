"""
Reseller credentials helper.   Run (from the app folder):   .venv/bin/python tests/resellers_keys.py

Menu: create a reseller (generates its API key), regenerate a key, fund a float, check a balance, list resellers,
set a reseller's margin, and see pricing and earnings.
Uses this project's .env, so it talks to the same database as the API.
Direct use also works:   python tests/resellers_keys.py create-tenant --name Comet --slug comet
"""

import asyncio
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # app root, so `app` imports work from any folder

from app.partners import cli  # noqa: E402


# menu for CLI use to create resellers, regenerate keys, etc

MENU = """
  Mamlaka resellers
  1) Create a reseller + generate API key
  2) Regenerate a reseller's API key (old key stops working)
  3) Credit a reseller's float
  4) Show a reseller's float balance
  5) List resellers
  6) Set / change a reseller's webhook URL
  7) Treasury: real Wasabi USD vs partner floats
  8) Pricing: our cost of a dollar and the rate each reseller pays
  9) Set a reseller's margin
  10) Earnings: margin kept on card operations
  11) Float statement: every movement on a reseller's float
  q) Quit
"""


def ask(prompt: str) -> str:
    return input(f"  {prompt}").strip()


def run(**args):
    defaults = dict(name=None, slug=None, webhook_url=None, card_types=None, max_amount=None,
                    amount_kes=None, ref=None, url=None, pct=None, days=30, limit=50)
    asyncio.run(cli.run(Namespace(**{**defaults, **args})))


def main():
    if len(sys.argv) > 1:          # python tests/resellers_keys.py <cli arguments>
        cli.main()
        return
    while True:
        print(MENU)
        choice = ask("Choose: ").lower()
        try:
            if choice == "1":
                run(cmd="create-tenant", name=ask("Name (e.g. Comet): "), slug=ask("Slug, lowercase (e.g. comet): "),
                    webhook_url=ask("Their webhook URL (https://..., blank for now): ") or None,
                    card_types=ask("Allowed card type ids, comma separated (blank = all): ") or None,
                    max_amount=ask("Max USD per card load (blank = platform default): ") or None)
            elif choice == "2":
                slug = ask("Reseller slug: ")
                if ask(f"Regenerate the key for '{slug}'? Their current key stops working. [y/N] ").lower() == "y":
                    run(cmd="rotate-key", slug=slug)
            elif choice == "3":
                run(cmd="credit-float", slug=ask("Reseller slug: "), amount_kes=float(ask("Amount in KES: ")),
                    ref=ask("Unique payment reference (bank / M-Pesa ref): "))
            elif choice == "4":
                run(cmd="balance", slug=ask("Reseller slug: "))
            elif choice == "5":
                run(cmd="list")
            elif choice == "6":
                run(cmd="set-webhook", slug=ask("Reseller slug: "), url=ask("Webhook URL (https://..., blank clears it): ") or None)
            elif choice == "7":
                run(cmd="treasury")
            elif choice == "8":
                run(cmd="pricing")
            elif choice == "9":
                run(cmd="set-margin", slug=ask("Reseller slug: "),
                    pct=ask("Margin in percent, e.g. 1.5 (blank = default): ") or None)
            elif choice == "10":
                run(cmd="earnings", days=int(ask("Last how many days? [30] ") or 30))
            elif choice == "11":
                run(cmd="statement", slug=ask("Reseller slug: "))
            elif choice == "q":
                return
            else:
                print("  Unknown choice")
        except ValueError as exc:      # e.g. a slug that already exists, or a bad amount
            print(f"  Error: {exc}")


if __name__ == "__main__":
    main()
