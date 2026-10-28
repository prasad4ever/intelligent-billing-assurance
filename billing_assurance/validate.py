"""Check dataset integrity.

This is a QA tool for the synthetic database. It reads the testing-only label
file to confirm injected faults landed where intended. Discrepancy detection
must not call this module or read that file.
"""

from __future__ import annotations

import json
import re
import sqlite3
from calendar import monthrange
from collections import Counter
from pathlib import Path

from billing_assurance.generate import (
    DEFAULT_DB_PATH,
    DEFAULT_LABELS_PATH,
    LABEL_SENTINEL,
    SCENARIO_AMBIGUOUS,
    SCENARIO_DISCOUNT,
    SCENARIO_DUPLICATE,
    SCENARIO_PRICE,
    SCENARIO_USAGE,
)
from billing_assurance.money import (
    PriceTerm,
    amount_cents,
    covers_period,
    dollars_to_e4,
    price_from_term,
)

TABLES = (
    "customers",
    "contracts",
    "pricing_terms",
    "usage_records",
    "invoices",
    "invoice_lines",
    "billing_exceptions",
    "audit_log",
)
EXPECTED_COUNTS = {
    "customers": 60,
    "contracts": 60,
    "invoices": 180,
    "billing_exceptions": 0,
    "audit_log": 1,
}
EXPECTED_SCENARIOS = {
    SCENARIO_USAGE: 8,
    SCENARIO_PRICE: 8,
    SCENARIO_DISCOUNT: 8,
    SCENARIO_DUPLICATE: 6,
    SCENARIO_AMBIGUOUS: 6,
}
FORBIDDEN_TEXT = (
    LABEL_SENTINEL,
    SCENARIO_USAGE,
    SCENARIO_PRICE,
    SCENARIO_DISCOUNT,
    SCENARIO_DUPLICATE,
    SCENARIO_AMBIGUOUS,
    "injected_scenario_labels",
    "injected",
)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TAX_WORD = re.compile(r"\b(gst|hst|vat|tax|prorat\w*)\b", re.IGNORECASE)


class CheckFailure(Exception):
    """Raised when dataset QA should stop because the file cannot be read."""


class Checker:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.checks = 0

    def check(self, condition: bool, message: str) -> None:
        self.checks += 1
        if not condition:
            self.errors.append(message)

    def equal(self, actual, expected, message: str) -> None:
        self.check(
            actual == expected,
            f"{message}: expected {expected}, got {actual}",
        )


def _load_labels(labels_path: Path) -> dict:
    if not labels_path.is_file():
        raise CheckFailure(f"missing testing label file: {labels_path}")
    return json.loads(labels_path.read_text(encoding="utf-8"))


def _month_aligned(iso_date: str | None, *, start: bool) -> bool:
    if iso_date is None:
        return not start
    if ISO_DATE.fullmatch(iso_date) is None:
        return False
    year, month, day = (int(part) for part in iso_date.split("-"))
    if start:
        return day == 1
    return day == monthrange(year, month)[1]


def _price_term(row: sqlite3.Row) -> PriceTerm:
    return PriceTerm(
        pricing_term_id=row["pricing_term_id"],
        contract_id=row["contract_id"],
        charge_code=row["charge_code"],
        charge_model=row["charge_model"],
        unit_price_e4=row["unit_price_e4"],
        volume_threshold=row["volume_threshold"],
        discounted_unit_price_e4=row["discounted_unit_price_e4"],
        effective_start=row["effective_start"],
        effective_end=row["effective_end"],
        amendment_number=row["amendment_number"],
    )


def _applicable(terms: list[sqlite3.Row], contract_id: int, charge_code: str, start: str, end: str):
    found = [
        row
        for row in terms
        if row["contract_id"] == contract_id
        and row["charge_code"] == charge_code
        and covers_period(row["effective_start"], row["effective_end"], start, end)
    ]
    return sorted(found, key=lambda row: (row["amendment_number"], row["pricing_term_id"]))


def _expected_quantity(charge_model: str, customer_id: int, period_start: str, meter: str, sums) -> int:
    if charge_model == "fixed_monthly":
        return 1
    return sums[(customer_id, period_start, meter)]


def _referenced_line_ids(case: dict) -> list[int]:
    if "invoice_line_ids" in case:
        return list(case["invoice_line_ids"])
    return [case["invoice_line_id"]]


def _forbidden_tokens(table_name: str, column: str, action: str | None) -> tuple[str, ...]:
    """Scenario names are allowed only as reconciliation output.

    The answer-key file and the word "injected" stay forbidden everywhere.
    Classification names may appear on ``billing_exceptions.exception_type``
    and on a reconcile audit entry, because those are engine results.
    """
    engine_output = column == "exception_type" or (
        table_name == "audit_log" and column == "detail" and action == "reconcile"
    )
    if engine_output:
        return (LABEL_SENTINEL, "injected_scenario_labels", "injected")
    return FORBIDDEN_TEXT


def _scan_database_text(connection: sqlite3.Connection, checker: Checker) -> None:
    """Reject testing labels, tax, and proration language anywhere in the database."""
    violations: list[str] = []
    names = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (table_name,) in names:
        if not re.fullmatch(r"[a-z_]+", table_name):
            violations.append(f"unexpected table name {table_name}")
            continue
        columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        text_columns = [
            column[1]
            for column in columns
            if "TEXT" in column[2].upper() or "CHAR" in column[2].upper()
        ]
        if not text_columns:
            continue
        action_sql = ", action" if table_name == "audit_log" else ""
        quoted = ", ".join(text_columns)
        for row in connection.execute(f"SELECT {quoted}{action_sql} FROM {table_name}"):
            action = row[-1] if table_name == "audit_log" else None
            values = row[:-1] if table_name == "audit_log" else row
            for column, value in zip(text_columns, values):
                if not isinstance(value, str):
                    continue
                for token in _forbidden_tokens(table_name, column, action):
                    if token in value:
                        violations.append(f"{table_name}.{column} contains testing token {token}")
                if TAX_WORD.search(value):
                    violations.append(
                        f"{table_name} contains a tax or proration word in {value!r}"
                    )
    checker.check(not violations, "; ".join(violations[:8]))


def _check_scenario(checker: Checker, case: dict, line_rows: list[sqlite3.Row], applicable, sums) -> None:
    scenario = case["scenario"]
    customer_id = line_rows[0]["customer_id"]
    period_start = case["billing_period_start"]
    charge_code = case["charge_code"]
    if charge_code in ("TXN_PROCESSING", "API_ACCESS"):
        actual_quantity = sums[(customer_id, period_start, charge_code)]
    else:
        actual_quantity = 1

    if scenario == SCENARIO_USAGE:
        checker.equal(len(line_rows), 1, f"{case['invoice_number']} usage lines")
        checker.equal(len(applicable), 1, f"{case['invoice_number']} usage terms")
        line = line_rows[0]
        term = _price_term(applicable[0])
        expected_price = price_from_term(term, actual_quantity)
        checker.equal(case["usage_quantity"], actual_quantity, "label usage quantity")
        checker.equal(line["quantity"], case["invoiced_quantity"], "invoiced quantity")
        checker.check(
            line["quantity"] >= actual_quantity + 400,
            f"{case['invoice_number']} usage delta is not material",
        )
        checker.equal(line["unit_price_e4"], expected_price, "usage fault unit price")
        threshold = term.volume_threshold
        if threshold is not None:
            checker.check(
                actual_quantity < threshold and line["quantity"] < threshold,
                f"{case['invoice_number']} usage fault crossed the volume threshold",
            )
    elif scenario == SCENARIO_PRICE:
        checker.equal(len(line_rows), 1, f"{case['invoice_number']} price lines")
        checker.equal(len(applicable), 1, f"{case['invoice_number']} price terms")
        line = line_rows[0]
        expected_price = price_from_term(_price_term(applicable[0]), actual_quantity)
        checker.equal(line["quantity"], actual_quantity, "price fault quantity")
        checker.equal(
            case["contractual_unit_price_e4"],
            expected_price,
            "label contractual price",
        )
        checker.equal(line["unit_price_e4"], case["invoiced_unit_price_e4"], "invoiced price")
        checker.check(
            line["unit_price_e4"] != expected_price,
            f"{case['invoice_number']} price matches the contract",
        )
        checker.check(
            abs(line["unit_price_e4"] - expected_price) >= dollars_to_e4("100.00"),
            f"{case['invoice_number']} price delta is below $100",
        )
    elif scenario == SCENARIO_DISCOUNT:
        checker.equal(len(line_rows), 1, f"{case['invoice_number']} discount lines")
        checker.equal(len(applicable), 1, f"{case['invoice_number']} discount terms")
        line = line_rows[0]
        term = _price_term(applicable[0])
        checker.equal(line["quantity"], actual_quantity, "discount fault quantity")
        checker.check(
            term.volume_threshold is not None and actual_quantity >= term.volume_threshold,
            f"{case['invoice_number']} is below the volume threshold",
        )
        checker.equal(line["unit_price_e4"], term.unit_price_e4, "discount fault uses list price")
        checker.equal(
            price_from_term(term, actual_quantity),
            term.discounted_unit_price_e4,
            "contractual volume price",
        )
        checker.check(
            line["unit_price_e4"] != term.discounted_unit_price_e4,
            f"{case['invoice_number']} already uses the volume price",
        )
        difference = amount_cents(actual_quantity, term.unit_price_e4) - amount_cents(
            actual_quantity, term.discounted_unit_price_e4
        )
        checker.check(
            difference >= 10_000,
            f"{case['invoice_number']} missed discount is below $100",
        )
    elif scenario == SCENARIO_DUPLICATE:
        checker.equal(len(line_rows), 2, f"{case['invoice_number']} duplicate lines")
        checker.equal(len(applicable), 1, f"{case['invoice_number']} duplicate terms")
        expected_price = price_from_term(_price_term(applicable[0]), actual_quantity)
        first, second = line_rows
        checker.equal(first["quantity"], second["quantity"], "duplicate quantities")
        checker.equal(first["unit_price_e4"], second["unit_price_e4"], "duplicate prices")
        checker.equal(first["amount_cents"], second["amount_cents"], "duplicate amounts")
        checker.equal(first["description"], second["description"], "duplicate descriptions")
        checker.equal(first["quantity"], actual_quantity, "duplicate quantity")
        checker.equal(first["unit_price_e4"], expected_price, "duplicate contractual price")
        checker.check(first["amount_cents"] > 0, "duplicate amount is zero")
    elif scenario == SCENARIO_AMBIGUOUS:
        checker.equal(len(line_rows), 1, f"{case['invoice_number']} ambiguous lines")
        checker.equal(len(applicable), 2, f"{case['invoice_number']} applicable terms")
        line = line_rows[0]
        checker.equal(line["quantity"], actual_quantity, "ambiguous quantity")
        resolved = [
            price_from_term(_price_term(row), actual_quantity) for row in applicable
        ]
        checker.equal(len(set(resolved)), 2, "ambiguous candidate prices")
        checker.equal(
            {row["pricing_term_id"] for row in applicable},
            set(case["applicable_pricing_term_ids"]),
            "ambiguous term ids",
        )
        chosen = next(
            row
            for row in applicable
            if row["pricing_term_id"] == case["invoiced_from_pricing_term_id"]
        )
        checker.equal(
            line["unit_price_e4"],
            price_from_term(_price_term(chosen), actual_quantity),
            "ambiguous invoiced price",
        )
        checker.check(
            line["unit_price_e4"] != resolved[0] or line["unit_price_e4"] != resolved[1],
            "ambiguous invoice matches both prices",
        )
        notes = [row["amendment_note"] for row in applicable]
        checker.check(
            notes[0] and notes[1] and notes[0] != notes[1],
            f"{case['invoice_number']} amendment notes do not conflict",
        )
        checker.equal(
            applicable[0]["effective_start"],
            applicable[1]["effective_start"],
            "ambiguous terms share an effective start",
        )
    else:
        checker.check(False, f"unknown scenario {scenario}")

    for line in line_rows:
        checker.equal(
            line["amount_cents"],
            amount_cents(line["quantity"], line["unit_price_e4"]),
            f"line {line['invoice_line_id']} amount",
        )


def validate_database(
    db_path: Path = DEFAULT_DB_PATH,
    labels_path: Path = DEFAULT_LABELS_PATH,
) -> Checker:
    """Run integrity checks. The returned checker lists any failures."""
    checker = Checker()
    if not db_path.is_file():
        checker.check(False, f"missing database {db_path}")
        return checker
    labels = _load_labels(labels_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        checker.equal(integrity, "ok", "integrity_check")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        checker.equal(len(foreign_keys), 0, "foreign_key_check")
        checker.equal(
            connection.execute("PRAGMA user_version").fetchone()[0],
            1,
            "schema user_version",
        )

        counts = {}
        for table in TABLES:
            counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table, expected in EXPECTED_COUNTS.items():
            checker.equal(counts[table], expected, table)
        checker.check(
            200 <= counts["pricing_terms"] <= 400,
            f"pricing_terms {counts['pricing_terms']} outside 200-400",
        )
        checker.check(
            5_000 <= counts["usage_records"] <= 15_000,
            f"usage_records {counts['usage_records']} outside 5,000-15,000",
        )
        checker.check(
            500 <= counts["invoice_lines"] <= 1_200,
            f"invoice_lines {counts['invoice_lines']} outside 500-1,200",
        )
        checker.equal(
            connection.execute(
                "SELECT COUNT(DISTINCT billing_period_start) FROM invoices"
            ).fetchone()[0],
            3,
            "billing periods",
        )

        mismatched_customers = connection.execute(
            """
            SELECT COUNT(*) FROM usage_records AS usage
            JOIN contracts AS contract ON contract.contract_id = usage.contract_id
            WHERE contract.customer_id != usage.customer_id
            """
        ).fetchone()[0]
        checker.equal(mismatched_customers, 0, "usage customer/contract alignment")
        mismatched_invoices = connection.execute(
            """
            SELECT COUNT(*) FROM invoices AS invoice
            JOIN contracts AS contract ON contract.contract_id = invoice.contract_id
            WHERE contract.customer_id != invoice.customer_id
            """
        ).fetchone()[0]
        checker.equal(mismatched_invoices, 0, "invoice customer/contract alignment")

        totals = connection.execute(
            """
            SELECT invoice.invoice_id,
                   invoice.total_amount_cents,
                   COALESCE(SUM(line.amount_cents), 0) AS line_total
            FROM invoices AS invoice
            LEFT JOIN invoice_lines AS line ON line.invoice_id = invoice.invoice_id
            GROUP BY invoice.invoice_id
            HAVING invoice.total_amount_cents != line_total
            """
        ).fetchall()
        checker.equal(len(totals), 0, "invoice totals match lines")

        day_counts = connection.execute(
            """
            SELECT customer_id, meter_code, COUNT(*) AS days
            FROM usage_records
            GROUP BY customer_id, meter_code
            HAVING days != 90
            """
        ).fetchall()
        checker.equal(len(day_counts), 0, "each customer meter has 90 daily rows")

        terms = connection.execute("SELECT * FROM pricing_terms").fetchall()
        for term in terms:
            checker.check(
                _month_aligned(term["effective_start"], start=True),
                f"term {term['pricing_term_id']} does not start on a month boundary",
            )
            checker.check(
                _month_aligned(term["effective_end"], start=False),
                f"term {term['pricing_term_id']} does not end on a month boundary",
            )
            checker.check(bool(term["amendment_note"]), "blank amendment note")

        for invoice in connection.execute("SELECT * FROM invoices"):
            checker.check(
                _month_aligned(invoice["billing_period_start"], start=True)
                and _month_aligned(invoice["billing_period_end"], start=False)
                and invoice["billing_period_start"][:7] == invoice["billing_period_end"][:7],
                f"{invoice['invoice_number']} is not a full calendar month",
            )
            checker.equal(invoice["currency_code"], "CAD", invoice["invoice_number"])
            checker.equal(invoice["status"], "draft", invoice["invoice_number"])
            checker.equal(
                invoice["invoice_date"],
                invoice["billing_period_end"],
                f"{invoice['invoice_number']} invoice date",
            )

        sums = {
            (row["customer_id"], row["period_start"], row["meter_code"]): row["quantity"]
            for row in connection.execute(
                """
                SELECT customer_id, meter_code,
                       CASE
                           WHEN usage_date BETWEEN '2026-01-01' AND '2026-01-31' THEN '2026-01-01'
                           WHEN usage_date BETWEEN '2026-02-01' AND '2026-02-28' THEN '2026-02-01'
                           WHEN usage_date BETWEEN '2026-03-01' AND '2026-03-31' THEN '2026-03-01'
                       END AS period_start,
                       SUM(quantity) AS quantity
                FROM usage_records
                GROUP BY customer_id, meter_code, period_start
                """
            )
            if row["period_start"] is not None
        }
        outside = connection.execute(
            """
            SELECT COUNT(*) FROM usage_records
            WHERE usage_date < '2026-01-01' OR usage_date > '2026-03-31'
            """
        ).fetchone()[0]
        checker.equal(outside, 0, "usage outside the three billing months")

        checker.equal(labels.get("access_policy"), LABEL_SENTINEL, "label access policy")
        scenarios = labels.get("scenarios", [])
        scenario_counts = Counter(case["scenario"] for case in scenarios)
        checker.equal(dict(scenario_counts), EXPECTED_SCENARIOS, "injected scenario counts")
        invoice_numbers = [case["invoice_number"] for case in scenarios]
        checker.equal(
            len(invoice_numbers),
            len(set(invoice_numbers)),
            "one injected fault per invoice",
        )
        checker.check(
            counts["invoices"] - len(set(invoice_numbers)) >= int(counts["invoices"] * 0.75),
            "fewer than 75 percent of invoices are clean",
        )

        lines_by_id = {
            row["invoice_line_id"]: row
            for row in connection.execute(
                """
                SELECT line.*, invoice.customer_id, invoice.contract_id,
                       invoice.invoice_number, invoice.billing_period_start,
                       invoice.billing_period_end
                FROM invoice_lines AS line
                JOIN invoices AS invoice ON invoice.invoice_id = line.invoice_id
                """
            )
        }
        lines_by_invoice: dict[str, list[sqlite3.Row]] = {}
        for line in lines_by_id.values():
            lines_by_invoice.setdefault(line["invoice_number"], []).append(line)

        referenced = set()
        labels_by_invoice_charge = {}
        for case in scenarios:
            key = (case["invoice_number"], case["charge_code"])
            checker.check(key not in labels_by_invoice_charge, f"duplicate label {key}")
            labels_by_invoice_charge[key] = case
            for line_id in _referenced_line_ids(case):
                checker.check(line_id in lines_by_id, f"label line {line_id} is missing")
                referenced.add(line_id)

        for invoice_number, invoice_lines in lines_by_invoice.items():
            grouped: dict[str, list[sqlite3.Row]] = {}
            for line in invoice_lines:
                grouped.setdefault(line["charge_code"], []).append(line)
            sample = invoice_lines[0]
            period_start = sample["billing_period_start"]
            period_end = sample["billing_period_end"]
            contract_id = sample["contract_id"]
            for charge_code, charge_lines in grouped.items():
                charge_lines = sorted(charge_lines, key=lambda row: row["line_number"])
                applicable = _applicable(
                    terms, contract_id, charge_code, period_start, period_end
                )
                case = labels_by_invoice_charge.get((invoice_number, charge_code))
                if case is None:
                    checker.equal(
                        len(charge_lines),
                        1,
                        f"{invoice_number} {charge_code} line count",
                    )
                    checker.equal(
                        len(applicable),
                        1,
                        f"{invoice_number} {charge_code} term count",
                    )
                    line = charge_lines[0]
                    checker.check(
                        line["invoice_line_id"] not in referenced,
                        f"line {line['invoice_line_id']} is both clean and labeled",
                    )
                    model = applicable[0]["charge_model"]
                    expected_quantity = _expected_quantity(
                        model,
                        sample["customer_id"],
                        period_start,
                        charge_code,
                        sums,
                    )
                    expected_price = price_from_term(
                        _price_term(applicable[0]), expected_quantity
                    )
                    checker.equal(
                        line["quantity"],
                        expected_quantity,
                        f"{invoice_number} {charge_code} quantity",
                    )
                    checker.equal(
                        line["unit_price_e4"],
                        expected_price,
                        f"{invoice_number} {charge_code} unit price",
                    )
                    checker.equal(
                        line["amount_cents"],
                        amount_cents(line["quantity"], line["unit_price_e4"]),
                        f"{invoice_number} {charge_code} amount",
                    )
                else:
                    labeled_ids = set(_referenced_line_ids(case))
                    checker.equal(
                        {line["invoice_line_id"] for line in charge_lines},
                        labeled_ids,
                        f"{invoice_number} {charge_code} labeled lines",
                    )
                    _check_scenario(checker, case, charge_lines, applicable, sums)

        overlap_pairs = 0
        invoices = connection.execute("SELECT * FROM invoices").fetchall()
        seen_contracts = {row["contract_id"] for row in invoices}
        for contract_id in seen_contracts:
            for period_start, period_end in (
                ("2026-01-01", "2026-01-31"),
                ("2026-02-01", "2026-02-28"),
                ("2026-03-01", "2026-03-31"),
            ):
                codes = {
                    row["charge_code"]
                    for row in terms
                    if row["contract_id"] == contract_id
                    and covers_period(
                        row["effective_start"],
                        row["effective_end"],
                        period_start,
                        period_end,
                    )
                }
                for charge_code in codes:
                    applicable = _applicable(
                        terms, contract_id, charge_code, period_start, period_end
                    )
                    if len(applicable) == 2:
                        overlap_pairs += 1
                    checker.check(
                        len(applicable) in (1, 2),
                        f"contract {contract_id} {charge_code} {period_start} has "
                        f"{len(applicable)} terms",
                    )
        checker.equal(overlap_pairs, EXPECTED_SCENARIOS[SCENARIO_AMBIGUOUS], "overlapping terms")

        _scan_database_text(connection, checker)
        raw_text = db_path.read_bytes()
        checker.check(
            LABEL_SENTINEL.encode("ascii") not in raw_text,
            "database file contains the testing-only sentinel",
        )
        checker.stats = counts
        return checker
    finally:
        connection.close()


def format_report(checker: Checker, db_path: Path, labels_path: Path) -> str:
    counts = getattr(checker, "stats", {})
    lines = [
        f"Database: {db_path}",
        f"Testing-only labels: {labels_path}",
        "",
    ]
    for table in TABLES:
        if table in counts:
            lines.append(f"{table:<22} {counts[table]:>6}")
    lines.append("")
    if checker.errors:
        lines.append(f"Validation failed: {len(checker.errors)} error(s), {checker.checks} checks")
        lines.extend(f"- {error}" for error in checker.errors)
    else:
        lines.append(f"Validation passed: {checker.checks} checks")
    return "\n".join(lines)


def main() -> int:
    checker = validate_database()
    print(format_report(checker, DEFAULT_DB_PATH, DEFAULT_LABELS_PATH))
    return 1 if checker.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
