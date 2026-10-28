"""Build a reproducible synthetic O2C billing database.

Correct draft lines are priced from the single contractual term that covers
the billing month. Volume discounts apply to the full monthly quantity when
usage reaches the term threshold. A fixed list of draft lines then departs
from that result. The departure list is written only to the testing label
file, never to SQLite.

Detection code must not import this module to discover which rows were altered.
"""

from __future__ import annotations

import json
import sqlite3
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from billing_assurance.money import (
    PriceTerm,
    amount_cents,
    covers_period,
    dollars_to_e4,
    price_from_term,
)
from billing_assurance.schema import SCHEMA_SQL

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "billing_assurance.sqlite"
DEFAULT_LABELS_PATH = ROOT / "evaluation" / "injected_scenario_labels.json"

GENERATION_SEED = 42
GENERATED_AT = "2026-04-01T12:00:00Z"
CURRENCY = "CAD"
LABEL_SENTINEL = "TESTING_ONLY_DO_NOT_USE_FOR_DETECTION"

PERIODS: tuple[tuple[str, str], ...] = (
    ("2026-01-01", "2026-01-31"),
    ("2026-02-01", "2026-02-28"),
    ("2026-03-01", "2026-03-31"),
)
SERVICE_WINDOW = (date(2026, 1, 1), date(2026, 3, 31))

CHARGE_ORDER = (
    "CORE_PLATFORM",
    "TXN_PROCESSING",
    "API_ACCESS",
    "PREMIUM_SUPPORT",
)
CHARGE_NAMES = {
    "CORE_PLATFORM": "Core platform subscription",
    "TXN_PROCESSING": "Transaction processing",
    "API_ACCESS": "API access",
    "PREMIUM_SUPPORT": "Premium support retainer",
}
CHARGE_MODELS = {
    "CORE_PLATFORM": "fixed_monthly",
    "TXN_PROCESSING": "per_unit",
    "API_ACCESS": "per_unit",
    "PREMIUM_SUPPORT": "fixed_monthly",
}
METERS = ("TXN_PROCESSING", "API_ACCESS")

TXN_VOLUME_THRESHOLD = 8_000
API_VOLUME_THRESHOLD = 40_000
PLATFORM_AMENDMENT_E4 = dollars_to_e4("250.00")
PRICE_ERROR_E4 = dollars_to_e4("350.00")

PREFIXES = (
    "Northwater",
    "Harbour",
    "Cedar",
    "Prairie",
    "Granite",
    "Lakeshore",
    "Ridgeline",
    "Silverpine",
    "Maple",
    "Redwood",
    "Clearwater",
    "Stonegate",
)
INSTITUTION_TYPES = (
    "Chartered Bank",
    "Credit Union",
    "Trust Company",
    "Payments Institution",
    "Capital Bank",
)

# Fifteen contracts receive a clean February platform amendment.
CLEAN_PLATFORM_AMENDMENTS = frozenset(range(2, 31, 2))
# Six contracts have two March transaction amendments with the same start date.
AMBIGUOUS_TXN_CUSTOMERS = frozenset({1, 7, 13, 19, 25, 31})

USAGE_FAULTS = (
    (3, "2026-01-01", "TXN_PROCESSING"),
    (9, "2026-02-01", "TXN_PROCESSING"),
    (15, "2026-03-01", "TXN_PROCESSING"),
    (21, "2026-01-01", "TXN_PROCESSING"),
    (27, "2026-02-01", "TXN_PROCESSING"),
    (33, "2026-03-01", "TXN_PROCESSING"),
    (39, "2026-01-01", "TXN_PROCESSING"),
    (45, "2026-02-01", "TXN_PROCESSING"),
)
PRICE_FAULTS = (
    (32, "2026-01-01", "CORE_PLATFORM"),
    (34, "2026-02-01", "CORE_PLATFORM"),
    (36, "2026-03-01", "CORE_PLATFORM"),
    (38, "2026-01-01", "CORE_PLATFORM"),
    (40, "2026-02-01", "CORE_PLATFORM"),
    (42, "2026-03-01", "CORE_PLATFORM"),
    (44, "2026-01-01", "CORE_PLATFORM"),
    (46, "2026-02-01", "CORE_PLATFORM"),
)
DISCOUNT_FAULTS = (
    (48, "2026-01-01", "TXN_PROCESSING"),
    (50, "2026-02-01", "TXN_PROCESSING"),
    (52, "2026-03-01", "TXN_PROCESSING"),
    (54, "2026-01-01", "TXN_PROCESSING"),
    (56, "2026-02-01", "TXN_PROCESSING"),
    (58, "2026-03-01", "TXN_PROCESSING"),
    (60, "2026-01-01", "TXN_PROCESSING"),
    (48, "2026-03-01", "TXN_PROCESSING"),
)
DUPLICATE_FAULTS = (
    (5, "2026-02-01", "CORE_PLATFORM"),
    (11, "2026-03-01", "CORE_PLATFORM"),
    (17, "2026-01-01", "CORE_PLATFORM"),
    (23, "2026-02-01", "CORE_PLATFORM"),
    (29, "2026-03-01", "CORE_PLATFORM"),
    (35, "2026-01-01", "CORE_PLATFORM"),
)
AMBIGUOUS_FAULTS = tuple(
    (customer_id, "2026-03-01", "TXN_PROCESSING")
    for customer_id in sorted(AMBIGUOUS_TXN_CUSTOMERS)
)

SCENARIO_USAGE = "incorrect_usage_quantity"
SCENARIO_PRICE = "incorrect_contractual_price"
SCENARIO_DISCOUNT = "missing_volume_discount"
SCENARIO_DUPLICATE = "duplicate_charge"
SCENARIO_AMBIGUOUS = "ambiguous_contract_amendment"


@dataclass(frozen=True)
class TermRecord:
    pricing_term_id: int
    contract_id: int
    charge_code: str
    charge_name: str
    charge_model: str
    unit_price_e4: int
    volume_threshold: int | None
    discounted_unit_price_e4: int | None
    effective_start: str
    effective_end: str | None
    amendment_number: int
    amendment_note: str
    created_at: str

    def as_price_term(self) -> PriceTerm:
        return PriceTerm(
            pricing_term_id=self.pricing_term_id,
            contract_id=self.contract_id,
            charge_code=self.charge_code,
            charge_model=self.charge_model,
            unit_price_e4=self.unit_price_e4,
            volume_threshold=self.volume_threshold,
            discounted_unit_price_e4=self.discounted_unit_price_e4,
            effective_start=self.effective_start,
            effective_end=self.effective_end,
            amendment_number=self.amendment_number,
        )


def daily_quantity(customer_id: int, meter_code: str, usage_day: date) -> int:
    """Deterministic daily aggregate. The seed changes the series."""
    if meter_code == "TXN_PROCESSING":
        base = 520 if customer_id % 2 == 0 else 140
        spread = 21
    elif meter_code == "API_ACCESS":
        base = 700 if customer_id % 3 == 0 else 2_400
        spread = 81
    else:
        raise ValueError(meter_code)
    wobble = (
        GENERATION_SEED * 17 + customer_id * 31 + usage_day.toordinal() * 13
    ) % spread - spread // 2
    quantity = base + wobble
    if usage_day.weekday() >= 5:
        quantity = quantity * 55 // 100
    return max(quantity, 1)


def fault_index() -> dict[tuple[int, str, str], str]:
    """Map (customer, period start, charge) to one injected scenario."""
    grouped = (
        (USAGE_FAULTS, SCENARIO_USAGE),
        (PRICE_FAULTS, SCENARIO_PRICE),
        (DISCOUNT_FAULTS, SCENARIO_DISCOUNT),
        (DUPLICATE_FAULTS, SCENARIO_DUPLICATE),
        (AMBIGUOUS_FAULTS, SCENARIO_AMBIGUOUS),
    )
    index: dict[tuple[int, str, str], str] = {}
    for faults, scenario in grouped:
        for customer_id, period_start, charge_code in faults:
            key = (customer_id, period_start, charge_code)
            if key in index:
                raise RuntimeError(f"overlapping injected fault: {key}")
            index[key] = scenario
    ambiguous_customers = {
        customer_id
        for (customer_id, _period, _charge), scenario in index.items()
        if scenario == SCENARIO_AMBIGUOUS
    }
    if ambiguous_customers != AMBIGUOUS_TXN_CUSTOMERS:
        raise RuntimeError("ambiguous faults do not match the amendment set")
    if CLEAN_PLATFORM_AMENDMENTS & AMBIGUOUS_TXN_CUSTOMERS:
        raise RuntimeError("amendment sets overlap")
    return index


def _discount_price(list_price_e4: int) -> int:
    """20 percent volume discount, rounded half up to the price scale."""
    value = (Decimal(list_price_e4) * Decimal(80) / Decimal(100)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(value)


def _platform_list_e4(customer_id: int) -> int:
    prices = ("1800.00", "2500.00", "4200.00", "6500.00")
    return dollars_to_e4(prices[customer_id % 4])


def _txn_list_e4(customer_id: int) -> int:
    prices = ("0.1000", "0.1200", "0.0850")
    return dollars_to_e4(prices[customer_id % 3])


def _api_list_e4(customer_id: int) -> int:
    prices = ("0.0180", "0.0220", "0.0150")
    return dollars_to_e4(prices[customer_id % 3])


def _customer_name(customer_id: int) -> tuple[str, str]:
    prefix = PREFIXES[(customer_id - 1) // 5]
    institution_type = INSTITUTION_TYPES[(customer_id - 1) % 5]
    return f"{prefix} {institution_type}", institution_type


def _iter_service_days():
    current, last = SERVICE_WINDOW
    while current <= last:
        yield current
        current += timedelta(days=1)


def _period_start_for(usage_day: date) -> str:
    iso = usage_day.isoformat()
    for start, end in PERIODS:
        if start <= iso <= end:
            return start
    raise ValueError(f"{iso} is outside the billing window")


def _usage_price_pair(list_price_e4: int, threshold: int) -> tuple[int, int, int]:
    discounted = _discount_price(list_price_e4)
    if discounted >= list_price_e4:
        raise RuntimeError("volume price must be below the list price")
    return list_price_e4, threshold, discounted


def _build_terms(customer_id: int, next_term_id: list[int]) -> list[TermRecord]:
    terms: list[TermRecord] = []

    def add(
        charge_code: str,
        unit_price_e4: int,
        effective_start: str,
        effective_end: str | None,
        amendment_number: int,
        amendment_note: str,
        volume_threshold: int | None = None,
        discounted_unit_price_e4: int | None = None,
    ) -> TermRecord:
        term = TermRecord(
            pricing_term_id=next_term_id[0],
            contract_id=customer_id,
            charge_code=charge_code,
            charge_name=CHARGE_NAMES[charge_code],
            charge_model=CHARGE_MODELS[charge_code],
            unit_price_e4=unit_price_e4,
            volume_threshold=volume_threshold,
            discounted_unit_price_e4=discounted_unit_price_e4,
            effective_start=effective_start,
            effective_end=effective_end,
            amendment_number=amendment_number,
            amendment_note=amendment_note,
            created_at=GENERATED_AT,
        )
        next_term_id[0] += 1
        terms.append(term)
        return term

    platform = _platform_list_e4(customer_id)
    prior_platform = platform - dollars_to_e4("200.00")
    add(
        "CORE_PLATFORM",
        prior_platform,
        "2025-01-01",
        "2025-12-31",
        0,
        "Initial 2025 platform schedule.",
    )
    if customer_id in CLEAN_PLATFORM_AMENDMENTS:
        add(
            "CORE_PLATFORM",
            platform,
            "2026-01-01",
            "2026-01-31",
            1,
            "Initial 2026 platform schedule.",
        )
        add(
            "CORE_PLATFORM",
            platform + PLATFORM_AMENDMENT_E4,
            "2026-02-01",
            None,
            2,
            "Amendment 2: platform subscription fee revised effective 1 February 2026.",
        )
    else:
        add(
            "CORE_PLATFORM",
            platform,
            "2026-01-01",
            None,
            1,
            "Initial 2026 platform schedule.",
        )

    txn_list, txn_threshold, txn_discount = _usage_price_pair(
        _txn_list_e4(customer_id), TXN_VOLUME_THRESHOLD
    )
    if customer_id in AMBIGUOUS_TXN_CUSTOMERS:
        add(
            "TXN_PROCESSING",
            txn_list,
            "2026-01-01",
            "2026-02-28",
            1,
            "Initial 2026 transaction processing schedule, including the monthly volume price.",
            txn_threshold,
            txn_discount,
        )
        lower = txn_list - dollars_to_e4("0.0200")
        higher = txn_list + dollars_to_e4("0.0150")
        add(
            "TXN_PROCESSING",
            lower,
            "2026-03-01",
            None,
            2,
            "Addendum A filed by sales: transaction price revised effective 1 March 2026. Final countersignature still outstanding.",
            txn_threshold,
            _discount_price(lower),
        )
        add(
            "TXN_PROCESSING",
            higher,
            "2026-03-01",
            None,
            3,
            "Addendum B recorded by billing operations: alternate transaction price effective 1 March 2026. Supersession of addendum A was not confirmed.",
            txn_threshold,
            _discount_price(higher),
        )
    else:
        add(
            "TXN_PROCESSING",
            txn_list,
            "2026-01-01",
            None,
            1,
            "Initial 2026 transaction processing schedule, including the monthly volume price.",
            txn_threshold,
            txn_discount,
        )

    api_list, api_threshold, api_discount = _usage_price_pair(
        _api_list_e4(customer_id), API_VOLUME_THRESHOLD
    )
    add(
        "API_ACCESS",
        api_list,
        "2026-01-01",
        None,
        1,
        "Initial 2026 API access schedule, including the monthly volume price.",
        api_threshold,
        api_discount,
    )
    if customer_id % 3 == 0:
        add(
            "PREMIUM_SUPPORT",
            dollars_to_e4("900.00"),
            "2026-01-01",
            None,
            1,
            "Initial 2026 premium support retainer.",
        )
    return terms


def _assert_volume_bands(sums: dict[tuple[int, str, str], int]) -> None:
    txn_low = []
    txn_high = []
    api_low = []
    api_high = []
    for (customer_id, _period, meter_code), quantity in sums.items():
        if meter_code == "TXN_PROCESSING":
            (txn_high if customer_id % 2 == 0 else txn_low).append(quantity)
        elif customer_id % 3 == 0:
            api_low.append(quantity)
        else:
            api_high.append(quantity)
    if max(txn_low) >= TXN_VOLUME_THRESHOLD or min(txn_high) < TXN_VOLUME_THRESHOLD:
        raise RuntimeError(
            "transaction volume bands are not separated by "
            f"{TXN_VOLUME_THRESHOLD}: low max {max(txn_low)}, high min {min(txn_high)}"
        )
    if max(api_low) >= API_VOLUME_THRESHOLD or min(api_high) < API_VOLUME_THRESHOLD:
        raise RuntimeError(
            "API volume bands are not separated by "
            f"{API_VOLUME_THRESHOLD}: low max {max(api_low)}, high min {min(api_high)}"
        )


def _applicable(
    terms: list[TermRecord],
    customer_id: int,
    charge_code: str,
    period_start: str,
    period_end: str,
) -> list[TermRecord]:
    found = [
        term
        for term in terms
        if term.contract_id == customer_id
        and term.charge_code == charge_code
        and covers_period(
            term.effective_start, term.effective_end, period_start, period_end
        )
    ]
    return sorted(found, key=lambda term: (term.amendment_number, term.pricing_term_id))


def _line_description(charge_code: str, period_start: str, quantity: int) -> str:
    period = period_start[:7]
    if charge_code == "CORE_PLATFORM":
        return f"Core platform subscription for {period}"
    if charge_code == "PREMIUM_SUPPORT":
        return f"Premium support retainer for {period}"
    if charge_code == "TXN_PROCESSING":
        return f"Transaction processing for {quantity} transactions"
    if charge_code == "API_ACCESS":
        return f"API access for {quantity} calls"
    raise ValueError(charge_code)


def _inflated_quantity(actual_quantity: int) -> int:
    return actual_quantity + max(400, actual_quantity // 8)


def _expected_quantity(charge_code: str, customer_id: int, period_start: str, sums) -> int:
    if CHARGE_MODELS[charge_code] == "fixed_monthly":
        return 1
    return sums[(customer_id, period_start, charge_code)]


def _validate_calendar_date(iso_date: str, *, month_end: bool) -> None:
    year, month, day = (int(part) for part in iso_date.split("-"))
    if month_end and day != monthrange(year, month)[1]:
        raise RuntimeError(f"{iso_date} is not a month end")
    if not month_end and day != 1:
        raise RuntimeError(f"{iso_date} is not a month start")


def _build_dataset() -> tuple[dict[str, list[tuple]], dict]:
    faults = fault_index()
    customers = []
    contracts = []
    terms: list[TermRecord] = []
    next_term_id = [1]
    for customer_id in range(1, 61):
        customer_name, institution_type = _customer_name(customer_id)
        customer_code = f"FI-{customer_id:03d}"
        customers.append(
            (
                customer_id,
                customer_code,
                customer_name,
                institution_type,
                "active",
                GENERATED_AT,
            )
        )
        contracts.append(
            (
                customer_id,
                customer_id,
                f"CA-2025-{customer_id:04d}",
                CURRENCY,
                "2025-01-01",
                "2026-12-31",
                "active",
                GENERATED_AT,
            )
        )
        terms.extend(_build_terms(customer_id, next_term_id))

    usage_rows = []
    sums: dict[tuple[int, str, str], int] = defaultdict(int)
    usage_id = 1
    for customer_id in range(1, 61):
        for meter_code in METERS:
            for usage_day in _iter_service_days():
                quantity = daily_quantity(customer_id, meter_code, usage_day)
                usage_rows.append(
                    (
                        usage_id,
                        customer_id,
                        customer_id,
                        usage_day.isoformat(),
                        meter_code,
                        quantity,
                        "daily",
                        GENERATED_AT,
                    )
                )
                usage_id += 1
                period_start = _period_start_for(usage_day)
                sums[(customer_id, period_start, meter_code)] += quantity
    _assert_volume_bands(sums)

    invoices = []
    invoice_lines = []
    labels = []
    line_id = 1
    for customer_id in range(1, 61):
        customer_code = f"FI-{customer_id:03d}"
        contract_number = f"CA-2025-{customer_id:04d}"
        customer_terms = [term for term in terms if term.contract_id == customer_id]
        for period_index, (period_start, period_end) in enumerate(PERIODS):
            invoice_id = (customer_id - 1) * 3 + period_index + 1
            invoice_number = f"INV-{period_start[:4]}{period_start[5:7]}-{customer_code}"
            charge_codes = [
                charge_code
                for charge_code in CHARGE_ORDER
                if _applicable(
                    customer_terms, customer_id, charge_code, period_start, period_end
                )
            ]
            pending_lines = []
            line_number = 1
            for charge_code in charge_codes:
                applicable = _applicable(
                    customer_terms, customer_id, charge_code, period_start, period_end
                )
                actual_quantity = _expected_quantity(
                    charge_code, customer_id, period_start, sums
                )
                scenario = faults.get((customer_id, period_start, charge_code))
                if scenario is None:
                    if len(applicable) != 1:
                        raise RuntimeError(
                            f"clean charge {charge_code} for customer {customer_id} "
                            f"{period_start} has {len(applicable)} terms"
                        )
                    unit_price = price_from_term(
                        applicable[0].as_price_term(), actual_quantity
                    )
                    quantities = [actual_quantity]
                elif scenario == SCENARIO_USAGE:
                    unit_price = price_from_term(
                        applicable[0].as_price_term(), actual_quantity
                    )
                    invoiced_quantity = _inflated_quantity(actual_quantity)
                    threshold = applicable[0].volume_threshold
                    if threshold is not None and (
                        actual_quantity >= threshold or invoiced_quantity >= threshold
                    ):
                        raise RuntimeError("usage fault crossed a volume threshold")
                    quantities = [invoiced_quantity]
                    amount = amount_cents(invoiced_quantity, unit_price)
                    labels.append(
                        {
                            "scenario": scenario,
                            "customer_code": customer_code,
                            "contract_number": contract_number,
                            "invoice_number": invoice_number,
                            "billing_period_start": period_start,
                            "billing_period_end": period_end,
                            "charge_code": charge_code,
                            "invoice_line_id": line_id,
                            "line_number": line_number,
                            "usage_quantity": actual_quantity,
                            "invoiced_quantity": invoiced_quantity,
                            "unit_price_e4": unit_price,
                            "amount_cents": amount,
                        }
                    )
                elif scenario == SCENARIO_PRICE:
                    contractual = price_from_term(
                        applicable[0].as_price_term(), actual_quantity
                    )
                    unit_price = contractual + PRICE_ERROR_E4
                    quantities = [actual_quantity]
                    labels.append(
                        {
                            "scenario": scenario,
                            "customer_code": customer_code,
                            "contract_number": contract_number,
                            "invoice_number": invoice_number,
                            "billing_period_start": period_start,
                            "billing_period_end": period_end,
                            "charge_code": charge_code,
                            "invoice_line_id": line_id,
                            "line_number": line_number,
                            "quantity": actual_quantity,
                            "contractual_unit_price_e4": contractual,
                            "invoiced_unit_price_e4": unit_price,
                            "contractual_amount_cents": amount_cents(
                                actual_quantity, contractual
                            ),
                            "invoiced_amount_cents": amount_cents(
                                actual_quantity, unit_price
                            ),
                        }
                    )
                elif scenario == SCENARIO_DISCOUNT:
                    term = applicable[0]
                    if (
                        term.volume_threshold is None
                        or actual_quantity < term.volume_threshold
                    ):
                        raise RuntimeError("discount fault is below the volume threshold")
                    unit_price = term.unit_price_e4
                    quantities = [actual_quantity]
                    labels.append(
                        {
                            "scenario": scenario,
                            "customer_code": customer_code,
                            "contract_number": contract_number,
                            "invoice_number": invoice_number,
                            "billing_period_start": period_start,
                            "billing_period_end": period_end,
                            "charge_code": charge_code,
                            "invoice_line_id": line_id,
                            "line_number": line_number,
                            "quantity": actual_quantity,
                            "volume_threshold": term.volume_threshold,
                            "list_unit_price_e4": term.unit_price_e4,
                            "discounted_unit_price_e4": term.discounted_unit_price_e4,
                            "invoiced_unit_price_e4": unit_price,
                            "invoiced_amount_cents": amount_cents(
                                actual_quantity, unit_price
                            ),
                            "contractual_amount_cents": amount_cents(
                                actual_quantity, term.discounted_unit_price_e4
                            ),
                        }
                    )
                elif scenario == SCENARIO_DUPLICATE:
                    unit_price = price_from_term(
                        applicable[0].as_price_term(), actual_quantity
                    )
                    quantities = [actual_quantity, actual_quantity]
                elif scenario == SCENARIO_AMBIGUOUS:
                    if len(applicable) != 2:
                        raise RuntimeError("ambiguous month does not have two terms")
                    chosen = next(
                        term for term in applicable if term.amendment_number == 2
                    )
                    unit_price = price_from_term(chosen.as_price_term(), actual_quantity)
                    candidate_prices = [
                        price_from_term(term.as_price_term(), actual_quantity)
                        for term in applicable
                    ]
                    if len(set(candidate_prices)) != 2:
                        raise RuntimeError("ambiguous terms resolve to the same price")
                    quantities = [actual_quantity]
                    labels.append(
                        {
                            "scenario": scenario,
                            "customer_code": customer_code,
                            "contract_number": contract_number,
                            "invoice_number": invoice_number,
                            "billing_period_start": period_start,
                            "billing_period_end": period_end,
                            "charge_code": charge_code,
                            "invoice_line_id": line_id,
                            "line_number": line_number,
                            "quantity": actual_quantity,
                            "invoiced_unit_price_e4": unit_price,
                            "invoiced_from_pricing_term_id": chosen.pricing_term_id,
                            "invoiced_from_amendment_number": chosen.amendment_number,
                            "applicable_pricing_term_ids": [
                                term.pricing_term_id for term in applicable
                            ],
                            "applicable_amendment_numbers": [
                                term.amendment_number for term in applicable
                            ],
                            "applicable_unit_prices_e4": candidate_prices,
                            "amendment_notes": [
                                term.amendment_note for term in applicable
                            ],
                        }
                    )
                else:
                    raise RuntimeError(scenario)

                created_line_ids = []
                created_line_numbers = []
                for quantity in quantities:
                    description = _line_description(charge_code, period_start, quantity)
                    amount = amount_cents(quantity, unit_price)
                    pending_lines.append(
                        (
                            line_id,
                            invoice_id,
                            line_number,
                            charge_code,
                            description,
                            quantity,
                            unit_price,
                            amount,
                            GENERATED_AT,
                        )
                    )
                    created_line_ids.append(line_id)
                    created_line_numbers.append(line_number)
                    line_id += 1
                    line_number += 1
                if scenario == SCENARIO_DUPLICATE:
                    amount = amount_cents(actual_quantity, unit_price)
                    labels.append(
                        {
                            "scenario": scenario,
                            "customer_code": customer_code,
                            "contract_number": contract_number,
                            "invoice_number": invoice_number,
                            "billing_period_start": period_start,
                            "billing_period_end": period_end,
                            "charge_code": charge_code,
                            "invoice_line_ids": created_line_ids,
                            "line_numbers": created_line_numbers,
                            "quantity": actual_quantity,
                            "unit_price_e4": unit_price,
                            "amount_cents": amount,
                            "extra_amount_cents": amount,
                        }
                    )

            total_amount = sum(row[7] for row in pending_lines)
            invoices.append(
                (
                    invoice_id,
                    invoice_number,
                    customer_id,
                    customer_id,
                    period_start,
                    period_end,
                    period_end,
                    "draft",
                    CURRENCY,
                    total_amount,
                    GENERATED_AT,
                )
            )
            invoice_lines.extend(pending_lines)

    for start, end in PERIODS:
        _validate_calendar_date(start, month_end=False)
        _validate_calendar_date(end, month_end=True)

    detail = json.dumps(
        {
            "billing_periods": ["2026-01", "2026-02", "2026-03"],
            "currency": CURRENCY,
            "note": (
                "Synthetic draft invoices initialized. Scenario labels are "
                "stored outside this database."
            ),
            "seed": GENERATION_SEED,
        },
        sort_keys=True,
    )
    audit_rows = [
        (
            1,
            GENERATED_AT,
            "dataset-generator",
            "initialize",
            "database",
            "billing_assurance",
            detail,
        )
    ]
    label_document = {
        "access_policy": LABEL_SENTINEL,
        "purpose": (
            "Testing only. Discrepancy detection must not read this file "
            "or use it as an input."
        ),
        "seed": GENERATION_SEED,
        "currency": CURRENCY,
        "scenarios": labels,
    }
    rows = {
        "customers": customers,
        "contracts": contracts,
        "pricing_terms": [
            (
                term.pricing_term_id,
                term.contract_id,
                term.charge_code,
                term.charge_name,
                term.charge_model,
                term.unit_price_e4,
                term.volume_threshold,
                term.discounted_unit_price_e4,
                term.effective_start,
                term.effective_end,
                term.amendment_number,
                term.amendment_note,
                term.created_at,
            )
            for term in terms
        ],
        "usage_records": usage_rows,
        "invoices": invoices,
        "invoice_lines": invoice_lines,
        "audit_log": audit_rows,
    }
    stats = {
        "customers": len(customers),
        "contracts": len(contracts),
        "pricing_terms": len(terms),
        "usage_records": len(usage_rows),
        "invoices": len(invoices),
        "invoice_lines": len(invoice_lines),
        "billing_exceptions": 0,
        "audit_log": len(audit_rows),
        "injected_cases": len(labels),
    }
    return rows, {"stats": stats, "labels": label_document}


_INSERTS = {
    "customers": """
        INSERT INTO customers (
            customer_id, customer_code, customer_name, institution_type,
            status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
    """,
    "contracts": """
        INSERT INTO contracts (
            contract_id, customer_id, contract_number, currency_code,
            start_date, end_date, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
    "pricing_terms": """
        INSERT INTO pricing_terms (
            pricing_term_id, contract_id, charge_code, charge_name, charge_model,
            unit_price_e4, volume_threshold, discounted_unit_price_e4,
            effective_start, effective_end, amendment_number, amendment_note,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    "usage_records": """
        INSERT INTO usage_records (
            usage_record_id, customer_id, contract_id, usage_date, meter_code,
            quantity, aggregation_level, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
    "invoices": """
        INSERT INTO invoices (
            invoice_id, invoice_number, customer_id, contract_id,
            billing_period_start, billing_period_end, invoice_date, status,
            currency_code, total_amount_cents, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    "invoice_lines": """
        INSERT INTO invoice_lines (
            invoice_line_id, invoice_id, line_number, charge_code, description,
            quantity, unit_price_e4, amount_cents, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    "audit_log": """
        INSERT INTO audit_log (
            audit_id, event_time, actor, action, entity_type, entity_id, detail
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
}


def build_database(
    db_path: Path = DEFAULT_DB_PATH,
    labels_path: Path = DEFAULT_LABELS_PATH,
) -> dict:
    """Create the SQLite file and the separate testing-only label file."""
    rows, payload = _build_dataset()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA_SQL)
        for table_name, sql in _INSERTS.items():
            connection.executemany(sql, rows[table_name])
        connection.commit()
    finally:
        connection.close()
    labels_path.write_text(
        json.dumps(payload["labels"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload["stats"]
