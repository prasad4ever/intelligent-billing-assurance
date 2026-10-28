"""Reconcile draft invoices to contracts and usage.

Reads the operational SQLite database only. Does not import the dataset
generator or the dataset validator, and does not open
evaluation/injected_scenario_labels.json.

A charge is priced from the one contractual term that covers the billing
month. When several terms cover it, the latest effective start wins. If more
than one term shares that start and those terms produce different rates, the
case is left for a person. No rate is chosen for them.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from billing_assurance.money import PriceTerm, amount_cents, covers_period, price_from_term

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "billing_assurance.sqlite"

USAGE = "incorrect_usage_quantity"
PRICE = "incorrect_contractual_price"
DISCOUNT = "missing_volume_discount"
DUPLICATE = "duplicate_charge"
AMBIGUOUS = "ambiguous_contract_amendment"
UNRESOLVED = "unresolved_discrepancy"

PENDING = "pending_approval"
OPEN = "open"

ACTOR = "reconciliation-engine"


@dataclass(frozen=True)
class TermView:
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
    amendment_note: str

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


@dataclass(frozen=True)
class Finding:
    invoice_id: int
    invoice_line_id: int | None
    exception_type: str
    status: str
    expected_amount_cents: int | None
    invoiced_amount_cents: int | None
    variance_cents: int | None
    evidence_note: str


@dataclass(frozen=True)
class ReconciliationReport:
    invoices_checked: int
    invoice_lines_checked: int
    discrepancies_detected: int
    exceptions_inserted: int
    by_type: dict[str, int]
    audit_id: int


def resolve_terms(covering: list[TermView], quantity: int) -> tuple[TermView | None, list[TermView]]:
    """Select one term, or return the unresolved contenders.

    ``quantity`` is the measured usage for the month (1 for a fixed fee).
    It is used only to see whether contenders yield the same rate.
    """
    if not covering:
        return None, []
    latest = max(term.effective_start for term in covering)
    contenders = [term for term in covering if term.effective_start == latest]
    contenders.sort(key=lambda term: (term.amendment_number, term.pricing_term_id))
    if len(contenders) == 1:
        return contenders[0], contenders
    prices = {price_from_term(term.as_price_term(), quantity) for term in contenders}
    if len(prices) == 1:
        return contenders[0], contenders
    return None, contenders


def _financials(
    expected_amount: int | None, invoiced_amount: int | None
) -> tuple[int | None, int | None, int | None, str]:
    if expected_amount is None:
        return None, invoiced_amount, None, OPEN
    if invoiced_amount is None:
        raise ValueError("invoiced amount is required when an expected amount exists")
    return expected_amount, invoiced_amount, invoiced_amount - expected_amount, PENDING


def _term_phrase(term: TermView) -> str:
    return (
        f"term {term.pricing_term_id} amendment {term.amendment_number} "
        f"effective {term.effective_start}"
    )


def _line_phrase(line: sqlite3.Row) -> str:
    return (
        f"line {line['invoice_line_id']} (number {line['line_number']}) "
        f"quantity {line['quantity']} unit price e4 {line['unit_price_e4']} "
        f"amount {line['amount_cents']} cents"
    )


def _classify_charge(
    invoice: sqlite3.Row,
    charge_code: str,
    lines: list[sqlite3.Row],
    covering: list[TermView],
    usage_quantity: int,
) -> Finding | None:
    primary = max(lines, key=lambda line: (line["line_number"], line["invoice_line_id"]))
    models = {term.charge_model for term in covering}
    if models == {"fixed_monthly"}:
        measured_quantity = 1
        term, contenders = resolve_terms(covering, measured_quantity)
    elif len(models) <= 1:
        measured_quantity = usage_quantity
        term, contenders = resolve_terms(covering, measured_quantity)
    else:
        measured_quantity = usage_quantity
        term, contenders = None, list(covering)

    period = f"{invoice['billing_period_start']} to {invoice['billing_period_end']}"

    if len(lines) > 1:
        invoiced_amount = sum(line["amount_cents"] for line in lines)
        expected_amount = None
        evidence = [
            f"Invoice {invoice['invoice_number']} period {period}.",
            f"Charge {charge_code} is repeated on {len(lines)} lines: "
            + "; ".join(_line_phrase(line) for line in lines)
            + ".",
        ]
        if term is None:
            evidence.append("No single contractual rate was selected for the repeated charge.")
            if not contenders:
                evidence.append("No pricing term covers this billing period.")
            else:
                evidence.extend(_conflict_lines(contenders, usage_quantity))
        else:
            expected_price = price_from_term(term.as_price_term(), measured_quantity)
            expected_amount = amount_cents(measured_quantity, expected_price)
            evidence.append(
                f"{_term_phrase(term)} supports one charge of {expected_amount} cents "
                f"at unit price e4 {expected_price} for quantity {measured_quantity}."
            )
        expected_amount, invoiced_amount, variance, status = _financials(
            expected_amount, invoiced_amount
        )
        return Finding(
            invoice_id=invoice["invoice_id"],
            invoice_line_id=primary["invoice_line_id"],
            exception_type=DUPLICATE,
            status=status,
            expected_amount_cents=expected_amount,
            invoiced_amount_cents=invoiced_amount,
            variance_cents=variance,
            evidence_note=" ".join(evidence),
        )

    line = lines[0]
    if term is None:
        evidence = [
            f"Invoice {invoice['invoice_number']} period {period}.",
            f"Charge {charge_code} needs human review. No single rate was selected.",
            _line_phrase(line) + ".",
        ]
        if not contenders:
            evidence.append("No pricing term covers this billing period.")
        else:
            evidence.append("These terms cover the period and do not agree:")
            evidence.extend(_conflict_lines(contenders, usage_quantity))
        return Finding(
            invoice_id=invoice["invoice_id"],
            invoice_line_id=line["invoice_line_id"],
            exception_type=AMBIGUOUS,
            status=OPEN,
            expected_amount_cents=None,
            invoiced_amount_cents=line["amount_cents"],
            variance_cents=None,
            evidence_note=" ".join(evidence),
        )

    expected_price = price_from_term(term.as_price_term(), measured_quantity)
    expected_amount = amount_cents(measured_quantity, expected_price)
    billed_price = line["unit_price_e4"]
    billed_amount = line["amount_cents"]

    if line["quantity"] != measured_quantity and billed_price == expected_price:
        _expected, invoiced, variance, status = _financials(expected_amount, billed_amount)
        return Finding(
            invoice_id=invoice["invoice_id"],
            invoice_line_id=line["invoice_line_id"],
            exception_type=USAGE,
            status=status,
            expected_amount_cents=_expected,
            invoiced_amount_cents=invoiced,
            variance_cents=variance,
            evidence_note=(
                f"Invoice {invoice['invoice_number']} period {period}. "
                f"Billed quantity {line['quantity']} for {charge_code} does not match "
                f"usage quantity {measured_quantity}. "
                f"Unit price e4 {billed_price} matches {_term_phrase(term)}. "
                f"Expected amount {expected_amount} cents. Billed amount {billed_amount} cents. "
                f"Difference {billed_amount - expected_amount} cents."
            ),
        )

    if (
        line["quantity"] == measured_quantity
        and term.volume_threshold is not None
        and measured_quantity >= term.volume_threshold
        and billed_price == term.unit_price_e4
        and billed_price != expected_price
    ):
        _expected, invoiced, variance, status = _financials(expected_amount, billed_amount)
        return Finding(
            invoice_id=invoice["invoice_id"],
            invoice_line_id=line["invoice_line_id"],
            exception_type=DISCOUNT,
            status=status,
            expected_amount_cents=_expected,
            invoiced_amount_cents=invoiced,
            variance_cents=variance,
            evidence_note=(
                f"Invoice {invoice['invoice_number']} period {period}. "
                f"Quantity {measured_quantity} for {charge_code} meets volume threshold "
                f"{term.volume_threshold} on {_term_phrase(term)}, but the invoice uses "
                f"list unit price e4 {term.unit_price_e4} instead of volume unit price "
                f"e4 {expected_price}. Expected amount {expected_amount} cents. "
                f"Billed amount {billed_amount} cents. "
                f"Difference {billed_amount - expected_amount} cents."
            ),
        )

    if line["quantity"] == measured_quantity and (
        billed_price != expected_price or billed_amount != expected_amount
    ):
        _expected, invoiced, variance, status = _financials(expected_amount, billed_amount)
        return Finding(
            invoice_id=invoice["invoice_id"],
            invoice_line_id=line["invoice_line_id"],
            exception_type=PRICE,
            status=status,
            expected_amount_cents=_expected,
            invoiced_amount_cents=invoiced,
            variance_cents=variance,
            evidence_note=(
                f"Invoice {invoice['invoice_number']} period {period}. "
                f"Billed unit price e4 {billed_price} for {charge_code} does not match "
                f"{_term_phrase(term)} unit price e4 {expected_price}. "
                f"Quantity {line['quantity']} matches the contract quantity {measured_quantity}. "
                f"Expected amount {expected_amount} cents. Billed amount {billed_amount} cents. "
                f"Difference {billed_amount - expected_amount} cents."
            ),
        )

    if line["quantity"] != measured_quantity:
        _expected, invoiced, variance, status = _financials(expected_amount, billed_amount)
        return Finding(
            invoice_id=invoice["invoice_id"],
            invoice_line_id=line["invoice_line_id"],
            exception_type=UNRESOLVED,
            status=OPEN,
            expected_amount_cents=_expected,
            invoiced_amount_cents=invoiced,
            variance_cents=variance,
            evidence_note=(
                f"Invoice {invoice['invoice_number']} period {period}. "
                f"Billed quantity {line['quantity']} and unit price e4 {billed_price} "
                f"for {charge_code} both differ from usage quantity {measured_quantity} "
                f"and {_term_phrase(term)} unit price e4 {expected_price}. "
                "The case is left for human review."
            ),
        )

    return None


def _conflict_lines(contenders: list[TermView], quantity: int) -> list[str]:
    lines = []
    for term in contenders:
        rate = price_from_term(term.as_price_term(), quantity)
        lines.append(
            f"{_term_phrase(term)} yields unit price e4 {rate} at quantity {quantity}. "
            f"{term.amendment_note}"
        )
    return lines


def _load_terms(connection: sqlite3.Connection) -> dict[int, list[TermView]]:
    grouped: dict[int, list[TermView]] = defaultdict(list)
    rows = connection.execute(
        """
        SELECT pricing_term_id, contract_id, charge_code, charge_model,
               unit_price_e4, volume_threshold, discounted_unit_price_e4,
               effective_start, effective_end, amendment_number, amendment_note
        FROM pricing_terms
        """
    )
    for row in rows:
        grouped[row["contract_id"]].append(
            TermView(
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
                amendment_note=row["amendment_note"],
            )
        )
    return grouped


def _load_usage(connection: sqlite3.Connection) -> dict[tuple[int, str], list[tuple[str, int]]]:
    buckets: dict[tuple[int, str], list[tuple[str, int]]] = defaultdict(list)
    rows = connection.execute(
        "SELECT contract_id, meter_code, usage_date, quantity FROM usage_records"
    )
    for row in rows:
        buckets[(row["contract_id"], row["meter_code"])].append(
            (row["usage_date"], row["quantity"])
        )
    return buckets


def _usage_quantity(
    buckets: dict[tuple[int, str], list[tuple[str, int]]],
    contract_id: int,
    meter_code: str,
    period_start: str,
    period_end: str,
) -> int:
    return sum(
        quantity
        for usage_date, quantity in buckets.get((contract_id, meter_code), ())
        if period_start <= usage_date <= period_end
    )


def detect_findings(connection: sqlite3.Connection) -> tuple[list[Finding], int, int]:
    """Compare each draft invoice line with contract terms and usage."""
    terms_by_contract = _load_terms(connection)
    usage_buckets = _load_usage(connection)
    invoices = connection.execute(
        """
        SELECT invoice_id, invoice_number, contract_id, customer_id,
               billing_period_start, billing_period_end
        FROM invoices
        ORDER BY invoice_id
        """
    ).fetchall()
    lines = connection.execute(
        """
        SELECT invoice_line_id, invoice_id, line_number, charge_code,
               quantity, unit_price_e4, amount_cents
        FROM invoice_lines
        ORDER BY invoice_id, line_number
        """
    ).fetchall()
    lines_by_invoice: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for line in lines:
        lines_by_invoice[line["invoice_id"]].append(line)

    findings: list[Finding] = []
    for invoice in invoices:
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for line in lines_by_invoice.get(invoice["invoice_id"], []):
            grouped[line["charge_code"]].append(line)
        contract_terms = terms_by_contract.get(invoice["contract_id"], [])
        for charge_code in sorted(grouped):
            covering = [
                term
                for term in contract_terms
                if term.charge_code == charge_code
                and covers_period(
                    term.effective_start,
                    term.effective_end,
                    invoice["billing_period_start"],
                    invoice["billing_period_end"],
                )
            ]
            usage_quantity = _usage_quantity(
                usage_buckets,
                invoice["contract_id"],
                charge_code,
                invoice["billing_period_start"],
                invoice["billing_period_end"],
            )
            finding = _classify_charge(
                invoice,
                charge_code,
                grouped[charge_code],
                covering,
                usage_quantity,
            )
            if finding is not None:
                findings.append(finding)
    findings.sort(key=lambda finding: (finding.invoice_id, finding.invoice_line_id or 0, finding.exception_type))
    return findings, len(invoices), len(lines)


def _source_fingerprint(connection: sqlite3.Connection) -> tuple:
    """Counts and totals the engine must leave unchanged."""
    queries = (
        "SELECT COUNT(*), COALESCE(SUM(total_amount_cents), 0) FROM invoices",
        "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0), COALESCE(SUM(quantity), 0) FROM invoice_lines",
        "SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM usage_records",
        "SELECT COUNT(*), COALESCE(SUM(unit_price_e4), 0) FROM pricing_terms",
        "SELECT COUNT(*) FROM contracts",
        "SELECT COUNT(*) FROM customers",
    )
    return tuple(connection.execute(query).fetchone() for query in queries)


def reconcile(db_path: Path = DEFAULT_DB_PATH, *, run_at: str | None = None) -> ReconciliationReport:
    """Write discrepancies and one audit row. Safe to run more than once."""
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    if run_at is None:
        run_at = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN")
        before = _source_fingerprint(connection)
        findings, invoices_checked, lines_checked = detect_findings(connection)
        connection.execute(
            """
            DELETE FROM billing_exceptions
            WHERE decided_at IS NULL AND decided_by IS NULL
            """
        )
        decided = {
            (row["invoice_id"], row["exception_type"], row["invoice_line_id"])
            for row in connection.execute(
                """
                SELECT invoice_id, exception_type, invoice_line_id
                FROM billing_exceptions
                """
            )
        }
        inserted = 0
        for finding in findings:
            key = (finding.invoice_id, finding.exception_type, finding.invoice_line_id)
            if key in decided:
                continue
            connection.execute(
                """
                INSERT INTO billing_exceptions (
                    invoice_id, invoice_line_id, exception_type, status,
                    expected_amount_cents, invoiced_amount_cents, variance_cents,
                    evidence_note, created_at, decided_at, decided_by, decision_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    finding.invoice_id,
                    finding.invoice_line_id,
                    finding.exception_type,
                    finding.status,
                    finding.expected_amount_cents,
                    finding.invoiced_amount_cents,
                    finding.variance_cents,
                    finding.evidence_note,
                    run_at,
                ),
            )
            inserted += 1
        after = _source_fingerprint(connection)
        if before != after:
            raise RuntimeError("reconciliation changed contract, usage, or invoice data")
        by_type = dict(sorted(Counter(finding.exception_type for finding in findings).items()))
        detail = json.dumps(
            {
                "by_type": by_type,
                "discrepancies_detected": len(findings),
                "exceptions_inserted": inserted,
                "invoice_lines_checked": lines_checked,
                "invoices_checked": invoices_checked,
            },
            sort_keys=True,
        )
        cursor = connection.execute(
            """
            INSERT INTO audit_log (
                event_time, actor, action, entity_type, entity_id, detail
            ) VALUES (?, ?, 'reconcile', 'database', 'billing_assurance', ?)
            """,
            (run_at, ACTOR, detail),
        )
        connection.execute("COMMIT")
        return ReconciliationReport(
            invoices_checked=invoices_checked,
            invoice_lines_checked=lines_checked,
            discrepancies_detected=len(findings),
            exceptions_inserted=inserted,
            by_type=by_type,
            audit_id=int(cursor.lastrowid),
        )
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def format_report(report: ReconciliationReport) -> str:
    lines = [
        f"Invoices checked: {report.invoices_checked}",
        f"Invoice lines checked: {report.invoice_lines_checked}",
        f"Discrepancies detected: {report.discrepancies_detected}",
        f"Exception rows inserted this run: {report.exceptions_inserted}",
    ]
    for exception_type, count in report.by_type.items():
        lines.append(f"  {exception_type}: {count}")
    lines.append(f"Audit id: {report.audit_id}")
    return "\n".join(lines)


def main() -> int:
    report = reconcile(DEFAULT_DB_PATH)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
