# =========================================================
# money.py - Decimal-safe amount handling
# =========================================================

from decimal import Decimal, InvalidOperation, ROUND_DOWN

TWO_PLACES = Decimal("0.01")


def parse_amount(value) -> Decimal:
    """Parse an amount without going through float. Rejects more than 2 decimal places."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid amount")
    if not amount.is_finite():
        raise ValueError("Invalid amount")
    if amount != amount.quantize(TWO_PLACES, rounding=ROUND_DOWN):
        raise ValueError("Amount supports at most 2 decimal places")
    return amount.quantize(TWO_PLACES)


def wire_amount(amount: Decimal):
    """
    JSON number for the Wasabi payload. A 2dp Decimal below 1e13 round-trips through float
    repr exactly ("10.5", "0.01"), and the signature is computed over the same serialized string.
    """
    return float(amount)
