# =========================================================
# pricing.py - KES cost of a USD card operation
# =========================================================

from decimal import Decimal, ROUND_UP
from typing import Optional

from fastapi import HTTPException

from app.core import config
from app.wallet.ledger import to_minor


def fx_rate() -> Decimal:
    if not config.FX_KES_PER_USD:
        raise HTTPException(503, "Card funding is not configured (FX rate missing)")
    rate = Decimal(config.FX_KES_PER_USD)
    if rate <= 0:
        raise HTTPException(503, "Card funding is not configured (invalid FX rate)")
    return rate


def quote_debit(usd_amount: Decimal, extra_usd: Decimal = Decimal("0"),
                fee_rate_pct: Optional[Decimal] = None, fixed_fee_usd: Optional[Decimal] = None) -> dict:
    """KES the customer pays to put `usd_amount` on a card (+ any provider fee passed on)."""
    rate = fx_rate()
    usd_total = usd_amount + extra_usd
    # Use Wasabi's per-card-type fee rate if provided, else fall back to config spread.
    spread = fee_rate_pct if fee_rate_pct is not None else config.FX_SPREAD_PCT
    # Wasabi's fixed fee is quoted in USD; our configured fallback is already KES.
    fixed_fee_kes = fixed_fee_usd * rate if fixed_fee_usd is not None else config.CARD_FUNDING_FEE_KES
    kes = usd_total * rate * (1 + spread / 100) + fixed_fee_kes
    return {"usd": str(usd_amount), "provider_fee_usd": str(extra_usd), "rate": str(rate),
            "spread_pct": str(spread), "fixed_fee_kes": str(fixed_fee_kes),
            "kes_minor": to_minor(kes.quantize(Decimal("0.01"), ROUND_UP))}


def quote_credit(usd_amount: Decimal, fee_rate_pct: Optional[Decimal] = None) -> dict:
    """KES returned to the wallet when `usd_amount` is withdrawn from a card."""
    rate = fx_rate()
    spread = fee_rate_pct if fee_rate_pct is not None else config.FX_SPREAD_PCT
    kes = usd_amount * rate * (1 - spread / 100)
    return {"usd": str(usd_amount), "rate": str(rate), "spread_pct": str(spread),
            "kes_minor": int((kes * 100).to_integral_value(rounding="ROUND_DOWN"))}


def card_type_fees(card_type: Optional[dict]) -> dict:
    """Wasabi's per-card-type fee terms, as kwargs for quote_debit/quote_credit (empty when unknown)."""
    if not card_type:
        return {}
    def dec(key):
        value = card_type.get(key)
        return Decimal(str(value)) if value is not None else None
    return {"fee_rate_pct": dec("rechargeFeeRate")}


def card_type_fixed_fee(card_type: Optional[dict]) -> Optional[Decimal]:
    value = (card_type or {}).get("rechargeFixedFee")
    return Decimal(str(value)) if value is not None else None


def quote_issue(usd_amount: Decimal, card_type: dict) -> dict:
    """KES to issue a card: initial load plus Wasabi's card price, on the card type's own fee terms."""
    extra_usd = Decimal(str(card_type.get("cardPrice") or 0)) if card_type.get("cardPriceCurrency", "USD") == "USD" else Decimal(0)
    return quote_debit(usd_amount, extra_usd, fixed_fee_usd=card_type_fixed_fee(card_type), **card_type_fees(card_type))
