"""
Build the MHS Virtual Cards Partner Integration Guide as a PDF: one general guide for every partner.

    .venv/bin/python docs/partner-guide/build_guide.py --usdt 128.00

Prices are computed with the same code the API uses (app/wallet/pricing.py) at the standard partner margin, so the
guide matches GET /quote for an account on standard terms. Use the USDT rate set on the SERVER (FX_KES_PER_USD).
The PDF is written to docs/partner-guide/out/ (git-ignored) and printed with headless Chrome.
"""

import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))   # app root, so `app` imports work from any folder

from app.core import config  # noqa: E402
from app.wallet import ledger, pricing  # noqa: E402

VERSION = "2.0"
BASE_URL = "https://virtual.cards.mamlakapsp.com/api/v1/partner"
CARD_FEE_USD = Decimal(2)
CHROMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

DETAILS_ROW = ('      <tr><td>Card number, expiry and CVV</td><td><code>POST .../cards/details</code></td>'
               '<td><code>{cardNo}</code></td></tr>')
DETAILS_SECTION = """  <h3>Showing the card number and CVV</h3>
  <div class="endpoint"><span class="m POST">POST</span><span class="p">/customers/{externalId}/cards/details</span></div>
  <p>Returns the full card number, expiry date and CVV so the cardholder can use the card online.</p>
  <div class="callout warn"><b>Sensitive data</b> Show these values only to the cardholder, in your app, over TLS, and only after
    re-authenticating them (for example a one-time code plus an authenticator app). Never store, cache or log them, and hide them again
    after a short time. Calling this endpoint puts the systems that handle the response in card-data (PCI DSS) scope. Mamlaka records
    every reveal (who and when, never the values).</div>"""
DETAILS_APPENDIX = ('      <tr><td><code>POST</code></td><td><code>/customers/{externalId}/cards/details</code></td>'
                    '<td>Card number, expiry, CVV (sensitive)</td><td></td></tr>')
DETAILS_CHECK = ('      <tr><td>&#9744;</td><td>Card details shown only after re-authentication, never stored or logged</td></tr>')
NOT_AVAILABLE = ["Withdrawals from a card back to your float.",
                 "Cancelling a card through the API (contact Mamlaka).",
                 "Per-customer wallet top-ups: your customers pay you, and you fund one float."]


def kes(minor: int) -> str:
    return f"{ledger.from_minor(minor):,.2f}"


def prices(usdt: Decimal, margin: Decimal, deposit_fee: Decimal) -> dict:
    config.FX_KES_PER_USD = str(usdt)
    config.WASABI_DEPOSIT_FEE_PCT = deposit_fee
    config.CARD_FUNDING_FEE_KES = Decimal(0)
    rate = pricing.rate_card(margin)["kesPerUsd"]
    rows = []
    for label, usd, fee in (("New card with a 10 USD opening load", 10, CARD_FEE_USD),
                            ("New card with a 50 USD opening load", 50, CARD_FEE_USD),
                            ("Deposit of 10 USD to a card", 10, Decimal(0)),
                            ("Deposit of 100 USD to a card", 100, Decimal(0))):
        q = pricing.quote_debit(Decimal(usd), fee, margin_pct=margin)
        charged = f"{usd} + {fee} card fee" if fee else f"{usd}"
        rows.append(f'      <tr><td>{label}</td><td class="num">{charged}</td><td class="num">{kes(q["kes_minor"])}</td></tr>')
    issue10 = pricing.quote_debit(Decimal(10), CARD_FEE_USD, margin_pct=margin)
    return {"RATE": rate, "EXAMPLE_ROWS": "\n".join(rows), "QUOTE_ISSUE_10": f'{ledger.from_minor(issue10["kes_minor"])}'}


def render(args) -> str:
    template = (HERE / "guide.html").read_text()
    with_details = not args.no_card_details
    not_available = ([] if with_details else ["Viewing the card number or CVV through the API."]) + NOT_AVAILABLE
    values = {
        "DATE": args.date or date.today().strftime("%d %B %Y"),
        "VERSION": VERSION,
        "BASE_URL": BASE_URL,
        "MAX_USD": str(args.max_usd),
        "DETAILS_GLANCE": ", show card details" if with_details else "",
        "DETAILS_ROW": DETAILS_ROW if with_details else "",
        "DETAILS_SECTION": DETAILS_SECTION if with_details else "",
        "DETAILS_APPENDIX": DETAILS_APPENDIX if with_details else "",
        "DETAILS_CHECK": DETAILS_CHECK if with_details else "",
        "NOT_AVAILABLE": "\n".join(f"    <li>{item}</li>" for item in not_available),
        "CONTACT": (f'  <p class="version">Questions or a leaked key: contact Mamlaka at {html.escape(args.contact)}.</p>'
                    if args.contact else ""),
        **prices(Decimal(args.usdt), Decimal(args.margin), Decimal(args.deposit_fee)),
    }
    out = re.sub(r"\{\{([A-Z0-9_]+)\}\}", lambda m: values[m.group(1)], template)
    return out


def print_pdf(source: Path, target: Path) -> None:
    chrome = next((shutil.which(c) for c in CHROMES if shutil.which(c)), None)
    if not chrome:
        sys.exit("Google Chrome or Chromium is needed to print the PDF.")
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--no-sandbox",
                    f"--print-to-pdf={target}", source.as_uri()],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--usdt", default=config.FX_KES_PER_USD, help="KES per USDT, as on the server (FX_KES_PER_USD)")
    parser.add_argument("--margin", default=str(config.PARTNER_MARGIN_PCT), help="standard partner margin, percent")
    parser.add_argument("--deposit-fee", default=str(config.WASABI_DEPOSIT_FEE_PCT), help="Wasabi's USDT deposit fee, percent")
    parser.add_argument("--max-usd", default=str(config.MAX_SINGLE_AMOUNT), help="default per-operation limit in USD")
    parser.add_argument("--contact", help="support contact printed at the end, e.g. an email address")
    parser.add_argument("--no-card-details", action="store_true", help="leave out the card number / CVV endpoint")
    parser.add_argument("--date", help="date printed on the guide (default: today)")
    parser.add_argument("--out", help="output PDF path")
    parser.add_argument("--html", action="store_true", help="also keep the rendered HTML next to the PDF")
    args = parser.parse_args()
    if not args.usdt:
        sys.exit("Set --usdt (the server's FX_KES_PER_USD).")

    target = Path(args.out) if args.out else HERE / "out" / "MHS-Virtual-Cards-Partner-Guide.pdf"
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "guide.html"
        source.write_text(render(args))
        print_pdf(source, target.resolve())
        if args.html:
            shutil.copy(source, target.with_suffix(".html"))
    print(f"Written: {target}")


if __name__ == "__main__":
    main()
