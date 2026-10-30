# =========================================================
# pricing.py - KES cost of a USD card operation
# =========================================================

from decimal import Decimal, ROUND_UP
from typing import Optional

from fastapi import HTTPException

from app.core import config
from app.wallet.ledger import to_minor


def fx_rate() -> Decimal:
    """KES we pay for one USDT (the configured buy rate)."""
    if not config.FX_KES_PER_USD:
        raise HTTPException(503, "Card funding is not configured (FX rate missing)")
    rate = Decimal(config.FX_KES_PER_USD)
    if rate <= 0:
        raise HTTPException(503, "Card funding is not configured (invalid FX rate)")
    return rate


def cost_rate() -> Decimal:
    """KES one usable dollar at Wasabi costs us: Wasabi keeps WASABI_DEPOSIT_FEE_PCT of every USDT deposit."""
    return fx_rate() / (1 - config.WASABI_DEPOSIT_FEE_PCT / 100)


def rate_card(margin_pct: Optional[Decimal] = None) -> dict:
    """Our cost of a dollar and the rate we sell it at, for display (2 dp)."""
    margin = config.FX_SPREAD_PCT if margin_pct is None else margin_pct
    cost = cost_rate()
    return {"usdtRate": str(fx_rate()), "depositFeePct": str(config.WASABI_DEPOSIT_FEE_PCT),
            "costRate": str(cost.quantize(Decimal("0.01"))), "marginPct": str(margin),
            "kesPerUsd": str((cost * (1 + margin / 100)).quantize(Decimal("0.01"), ROUND_UP))}


def quote_debit(usd_amount: Decimal, extra_usd: Decimal = Decimal("0"), fee_rate_pct: Optional[Decimal] = None,
                fixed_fee_usd: Optional[Decimal] = None, margin_pct: Optional[Decimal] = None) -> dict:
    """KES the customer pays to put `usd_amount` on a card (+ any provider fee passed on).
    Wasabi's own fees (rate and fixed) are our cost; our margin (`margin_pct`, else FX_SPREAD_PCT) goes on top."""
    rate = cost_rate()
    margin = config.FX_SPREAD_PCT if margin_pct is None else margin_pct
    provider_pct = fee_rate_pct or Decimal(0)
    usd_cost = (usd_amount + extra_usd) * (1 + provider_pct / 100) + (fixed_fee_usd or Decimal(0))
    cost_kes = usd_cost * rate
    kes = (cost_kes * (1 + margin / 100) + config.CARD_FUNDING_FEE_KES).quantize(Decimal("0.01"), ROUND_UP)
    return {"usd": str(usd_amount), "provider_fee_usd": str(extra_usd), "rate": str(rate * (1 + margin / 100)),
            "cost_rate": str(rate), "spread_pct": str(margin), "provider_fee_pct": str(provider_pct),
            "fixed_fee_kes": str((fixed_fee_usd or Decimal(0)) * rate), "cost_kes": str(cost_kes.quantize(Decimal("0.01"))),
            "margin_kes": str((kes - cost_kes).quantize(Decimal("0.01"))), "kes_minor": to_minor(kes)}


def quote_credit(usd_amount: Decimal, fee_rate_pct: Optional[Decimal] = None,
                 margin_pct: Optional[Decimal] = None) -> dict:
    """KES returned to the wallet when `usd_amount` is withdrawn from a card (valued at our cost, less our margin)."""
    rate = cost_rate()
    margin = config.FX_SPREAD_PCT if margin_pct is None else margin_pct
    provider_pct = fee_rate_pct or Decimal(0)
    value_kes = usd_amount * (1 - provider_pct / 100) * rate
    kes = value_kes * (1 - margin / 100)
    kes_minor = int((kes * 100).to_integral_value(rounding="ROUND_DOWN"))
    return {"usd": str(usd_amount), "rate": str(rate * (1 - margin / 100)), "cost_rate": str(rate),
            "spread_pct": str(margin), "provider_fee_pct": str(provider_pct),
            "margin_kes": str((value_kes - Decimal(kes_minor) / 100).quantize(Decimal("0.01"))), "kes_minor": kes_minor}


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


def quote_issue(usd_amount: Decimal, card_type: dict, margin_pct: Optional[Decimal] = None) -> dict:
    """KES to issue a card: initial load plus Wasabi's card price, on the card type's own fee terms."""
    extra_usd = Decimal(str(card_type.get("cardPrice") or 0)) if card_type.get("cardPriceCurrency", "USD") == "USD" else Decimal(0)
    return quote_debit(usd_amount, extra_usd, fixed_fee_usd=card_type_fixed_fee(card_type), margin_pct=margin_pct,
                       **card_type_fees(card_type))
