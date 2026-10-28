"""Pricing arithmetic used to construct consistent draft invoices.

Amounts are integer cents. Unit prices are integer ten-thousandths of a
Canadian dollar (CAD × 10,000): $2,500.00 is 25_000_000 and $0.0850 is 850.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


def dollars_to_e4(amount: str) -> int:
    """Convert a CAD decimal string to the unit-price scale."""
    scaled = (Decimal(amount) * Decimal(10_000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(scaled)


def amount_cents(quantity: int, unit_price_e4: int) -> int:
    """Extend quantity × unit price and round half up to cents.

    ``unit_price_e4 / 10_000`` is the CAD unit price, so
    ``quantity * unit_price_e4 / 100`` is the exact amount in cents.
    """
    if quantity < 0 or unit_price_e4 < 0:
        raise ValueError("quantity and unit price must be non-negative")
    exact_cents = Decimal(quantity) * Decimal(unit_price_e4) / Decimal(100)
    return int(exact_cents.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def covers_period(
    effective_start: str,
    effective_end: str | None,
    period_start: str,
    period_end: str,
) -> bool:
    """Return whether a term covers an entire billing month.

    Dates are ISO ``YYYY-MM-DD``. An empty effective end means the term is
    open-ended. Partial months are not billable in this prototype.
    """
    if effective_start > period_start:
        return False
    if effective_end is None:
        return True
    return effective_end >= period_end


@dataclass(frozen=True)
class PriceTerm:
    pricing_term_id: int
    contract_id: int
    charge_code: str
    charge_model: str
    unit_price_e4: int
    volume_threshold: int | None
    discounted_unit_price_e4: int | None
    effective_start: str
    effective_end: str | None
    amendment_number: int


def price_from_term(term: PriceTerm, quantity: int) -> int:
    """Unit price for one term.

    A volume discount, when present, applies to the whole monthly quantity
    once the threshold is met. It is not a graduated schedule. Fixed fees
    ignore quantity and never carry a volume price.
    """
    if term.charge_model == "fixed_monthly":
        return term.unit_price_e4
    if term.charge_model != "per_unit":
        raise ValueError(f"unsupported charge model: {term.charge_model}")
    threshold = term.volume_threshold
    if threshold is not None and quantity >= threshold:
        if term.discounted_unit_price_e4 is None:
            raise ValueError("volume threshold is missing a discounted price")
        return term.discounted_unit_price_e4
    return term.unit_price_e4
