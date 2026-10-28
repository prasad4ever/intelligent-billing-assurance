"""Human decisions on billing exceptions.

Approval records a reviewer's decision about a proposed correction. It does
not change invoice, contract, or usage amounts, and it does not choose a
rate when the contract is ambiguous.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from billing_assurance.money import covers_period, price_from_term
from billing_assurance.reconcile import AMBIGUOUS, PENDING, TermView

STATUSES = ("open", "auto_cleared", "pending_approval", "approved", "rejected")
DECISIONS = {"approve": "approved", "reject": "rejected"}


class ReviewError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _source_fingerprint(connection: sqlite3.Connection) -> tuple:
    queries = (
        "SELECT COUNT(*), COALESCE(SUM(total_amount_cents), 0) FROM invoices",
        "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0), COALESCE(SUM(unit_price_e4), 0) FROM invoice_lines",
        "SELECT COUNT(*), COALESCE(SUM(unit_price_e4), 0) FROM pricing_terms",
        "SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM usage_records",
    )
    return tuple(tuple(row) for row in (connection.execute(query).fetchone() for query in queries))


def correction_allowed(row: sqlite3.Row) -> bool:
    """A financial correction can be decided only when reconciliation proposed one."""
    return (
        row["status"] == PENDING
        and row["exception_type"] != AMBIGUOUS
        and row["expected_amount_cents"] is not None
        and row["variance_cents"] is not None
    )


def _require_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(422, f"{field} is required")
    cleaned = value.strip()
    limit = 200 if field == "reviewer" else 2_000
    if len(cleaned) > limit:
        raise ReviewError(422, f"{field} is too long")
    return cleaned


def summary(db_path: Path) -> dict:
    connection = connect(db_path)
    try:
        invoices = connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        invoice_lines = connection.execute("SELECT COUNT(*) FROM invoice_lines").fetchone()[0]
        clean_invoices = connection.execute(
            """
            SELECT COUNT(*) FROM invoices AS invoice
            WHERE NOT EXISTS (
                SELECT 1 FROM billing_exceptions AS exception
                WHERE exception.invoice_id = invoice.invoice_id
            )
            """
        ).fetchone()[0]
        by_status = {
            row["status"]: row["n"]
            for row in connection.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM billing_exceptions
                GROUP BY status
                """
            )
        }
        by_type = {
            row["exception_type"]: row["n"]
            for row in connection.execute(
                """
                SELECT exception_type, COUNT(*) AS n
                FROM billing_exceptions
                GROUP BY exception_type
                ORDER BY exception_type
                """
            )
        }
        pending_variance = connection.execute(
            """
            SELECT COALESCE(SUM(variance_cents), 0)
            FROM billing_exceptions
            WHERE status = 'pending_approval'
            """
        ).fetchone()[0]
        return {
            "currency": "CAD",
            "amounts": "cents",
            "invoices": invoices,
            "invoice_lines": invoice_lines,
            "clean_invoices": clean_invoices,
            "clean_invoices_auto_accepted": True,
            "exceptions": sum(by_status.values()),
            "by_status": by_status,
            "by_type": by_type,
            "pending_variance_cents": pending_variance,
        }
    finally:
        connection.close()


def list_exceptions(
    db_path: Path,
    *,
    status: str | None = None,
    exception_type: str | None = None,
) -> list[dict]:
    if status is not None and status not in STATUSES:
        raise ReviewError(422, f"unknown status {status}")
    clauses: list[str] = []
    parameters: list[str] = []
    if status is not None:
        clauses.append("exception.status = ?")
        parameters.append(status)
    if exception_type is not None:
        clauses.append("exception.exception_type = ?")
        parameters.append(exception_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = connect(db_path)
    try:
        rows = connection.execute(
            f"""
            SELECT
                exception.exception_id,
                exception.exception_type,
                exception.status,
                exception.expected_amount_cents,
                exception.invoiced_amount_cents,
                exception.variance_cents,
                exception.decided_by,
                exception.decided_at,
                invoice.invoice_number,
                invoice.billing_period_start,
                invoice.currency_code,
                customer.customer_code,
                line.charge_code
            FROM billing_exceptions AS exception
            JOIN invoices AS invoice ON invoice.invoice_id = exception.invoice_id
            JOIN customers AS customer ON customer.customer_id = invoice.customer_id
            LEFT JOIN invoice_lines AS line ON line.invoice_line_id = exception.invoice_line_id
            {where}
            ORDER BY exception.exception_id
            """,
            parameters,
        ).fetchall()
        return [_list_item(row) for row in rows]
    finally:
        connection.close()


def get_exception(db_path: Path, exception_id: int) -> dict:
    connection = connect(db_path)
    try:
        row = _fetch_exception(connection, exception_id)
        if row is None:
            raise ReviewError(404, f"exception {exception_id} was not found")
        return _detail(connection, row)
    finally:
        connection.close()


def audit_history(db_path: Path, exception_id: int) -> dict:
    connection = connect(db_path)
    try:
        row = _fetch_exception(connection, exception_id)
        if row is None:
            raise ReviewError(404, f"exception {exception_id} was not found")
        entries = connection.execute(
            """
            SELECT audit_id, event_time, actor, action, detail
            FROM audit_log
            WHERE entity_type = 'billing_exception' AND entity_id = ?
            ORDER BY audit_id
            """,
            (str(exception_id),),
        ).fetchall()
        return {
            "exception_id": exception_id,
            "entries": [_audit_entry(entry) for entry in entries],
        }
    finally:
        connection.close()


def record_decision(
    db_path: Path,
    exception_id: int,
    decision: str,
    reviewer: str,
    reason: str,
    *,
    decided_at: str,
) -> dict:
    if decision not in DECISIONS:
        raise ReviewError(422, f"unknown decision {decision}")
    reviewer = _require_text(reviewer, "reviewer")
    reason = _require_text(reason, "reason")
    new_status = DECISIONS[decision]
    connection = connect(db_path)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            before = _source_fingerprint(connection)
            row = _fetch_exception(connection, exception_id)
            if row is None:
                raise ReviewError(404, f"exception {exception_id} was not found")
            _ensure_transition(row, decision)
            updated = connection.execute(
                """
                UPDATE billing_exceptions
                SET status = ?, decided_at = ?, decided_by = ?, decision_note = ?
                WHERE exception_id = ?
                  AND status = 'pending_approval'
                  AND decided_at IS NULL
                  AND decided_by IS NULL
                  AND expected_amount_cents IS NOT NULL
                  AND exception_type != ?
                """,
                (new_status, decided_at, reviewer, reason, exception_id, AMBIGUOUS),
            )
            if updated.rowcount != 1:
                raise ReviewError(409, "The decision could not be recorded")
            detail = json.dumps(
                {
                    "decision": new_status,
                    "previous_status": row["status"],
                    "reason": reason,
                    "exception_type": row["exception_type"],
                    "invoice_id": row["invoice_id"],
                    "expected_amount_cents": row["expected_amount_cents"],
                    "invoiced_amount_cents": row["invoiced_amount_cents"],
                    "variance_cents": row["variance_cents"],
                },
                sort_keys=True,
            )
            connection.execute(
                """
                INSERT INTO audit_log (
                    event_time, actor, action, entity_type, entity_id, detail
                ) VALUES (?, ?, ?, 'billing_exception', ?, ?)
                """,
                (decided_at, reviewer, decision, str(exception_id), detail),
            )
            after = _source_fingerprint(connection)
            if before != after:
                raise RuntimeError("review decision changed invoice or contract data")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        refreshed = _fetch_exception(connection, exception_id)
        if refreshed is None:
            raise ReviewError(404, f"exception {exception_id} was not found")
        return _detail(connection, refreshed)
    finally:
        connection.close()


def _ensure_transition(row: sqlite3.Row, decision: str) -> None:
    status = row["status"]
    if status in ("approved", "rejected"):
        raise ReviewError(
            409,
            f"Exception {row['exception_id']} is already {status} and cannot be changed",
        )
    if row["exception_type"] == AMBIGUOUS or row["expected_amount_cents"] is None:
        raise ReviewError(
            409,
            "No financial correction has been determined. The case stays open for investigation",
        )
    if status != PENDING:
        raise ReviewError(
            409,
            f"Cannot {decision} an exception with status {status}",
        )


def _fetch_exception(connection: sqlite3.Connection, exception_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT
            exception.exception_id,
            exception.invoice_id,
            exception.invoice_line_id,
            exception.exception_type,
            exception.status,
            exception.expected_amount_cents,
            exception.invoiced_amount_cents,
            exception.variance_cents,
            exception.evidence_note,
            exception.created_at,
            exception.decided_at,
            exception.decided_by,
            exception.decision_note,
            invoice.invoice_number,
            invoice.contract_id,
            invoice.billing_period_start,
            invoice.billing_period_end,
            invoice.currency_code,
            customer.customer_code,
            customer.customer_name,
            line.charge_code,
            line.line_number,
            line.description,
            line.quantity,
            line.unit_price_e4
        FROM billing_exceptions AS exception
        JOIN invoices AS invoice ON invoice.invoice_id = exception.invoice_id
        JOIN customers AS customer ON customer.customer_id = invoice.customer_id
        LEFT JOIN invoice_lines AS line
            ON line.invoice_line_id = exception.invoice_line_id
        WHERE exception.exception_id = ?
        """,
        (exception_id,),
    ).fetchone()


def _list_item(row: sqlite3.Row) -> dict:
    return {
        "exception_id": row["exception_id"],
        "invoice_number": row["invoice_number"],
        "customer_code": row["customer_code"],
        "billing_period_start": row["billing_period_start"],
        "charge_code": row["charge_code"],
        "exception_type": row["exception_type"],
        "status": row["status"],
        "currency": row["currency_code"],
        "billed_amount_cents": row["invoiced_amount_cents"],
        "expected_amount_cents": row["expected_amount_cents"],
        "difference_cents": row["variance_cents"],
        "financial_correction_allowed": correction_allowed(row),
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"],
    }


def _detail(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    item = _list_item(row)
    decision = None
    if row["decided_at"] is not None:
        decision = {
            "status": row["status"],
            "reviewer": row["decided_by"],
            "decided_at": row["decided_at"],
            "reason": row["decision_note"],
        }
    investigation = None
    if row["exception_type"] == AMBIGUOUS:
        investigation = _investigation(connection, row)
    item.update(
        {
            "customer_name": row["customer_name"],
            "billing_period_end": row["billing_period_end"],
            "line_number": row["line_number"],
            "description": row["description"],
            "billed_quantity": row["quantity"],
            "billed_unit_price_e4": row["unit_price_e4"],
            "evidence_note": row["evidence_note"],
            "decision": decision,
            "investigation": investigation,
        }
    )
    return item


def _investigation(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """Show every term that covers the period. Do not select one."""
    terms = _covering_terms(connection, row)
    if terms and all(term.charge_model == "fixed_monthly" for term in terms):
        measured = 1
    else:
        measured = connection.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM usage_records
            WHERE contract_id = ?
              AND meter_code = ?
              AND usage_date >= ?
              AND usage_date <= ?
            """,
            (
                row["contract_id"],
                row["charge_code"],
                row["billing_period_start"],
                row["billing_period_end"],
            ),
        ).fetchone()[0]
    return {
        "authoritative_rate_selected": False,
        "measured_quantity": measured,
        "conflicting_terms": [
            {
                "pricing_term_id": term.pricing_term_id,
                "amendment_number": term.amendment_number,
                "amendment_note": term.amendment_note,
                "effective_start": term.effective_start,
                "effective_end": term.effective_end,
                "charge_model": term.charge_model,
                "unit_price_e4": term.unit_price_e4,
                "volume_threshold": term.volume_threshold,
                "discounted_unit_price_e4": term.discounted_unit_price_e4,
                "rate_under_this_term_e4": price_from_term(term.as_price_term(), measured),
            }
            for term in terms
        ],
    }


def _covering_terms(connection: sqlite3.Connection, row: sqlite3.Row) -> list[TermView]:
    if row["charge_code"] is None:
        return []
    stored = connection.execute(
        """
        SELECT pricing_term_id, contract_id, charge_code, charge_model,
               unit_price_e4, volume_threshold, discounted_unit_price_e4,
               effective_start, effective_end, amendment_number, amendment_note
        FROM pricing_terms
        WHERE contract_id = ? AND charge_code = ?
        ORDER BY amendment_number, pricing_term_id
        """,
        (row["contract_id"], row["charge_code"]),
    ).fetchall()
    covering = []
    for term in stored:
        view = TermView(
            pricing_term_id=term["pricing_term_id"],
            contract_id=term["contract_id"],
            charge_code=term["charge_code"],
            charge_model=term["charge_model"],
            unit_price_e4=term["unit_price_e4"],
            volume_threshold=term["volume_threshold"],
            discounted_unit_price_e4=term["discounted_unit_price_e4"],
            effective_start=term["effective_start"],
            effective_end=term["effective_end"],
            amendment_number=term["amendment_number"],
            amendment_note=term["amendment_note"],
        )
        if covers_period(
            view.effective_start,
            view.effective_end,
            row["billing_period_start"],
            row["billing_period_end"],
        ):
            covering.append(view)
    return covering


def _audit_entry(row: sqlite3.Row) -> dict:
    try:
        detail = json.loads(row["detail"])
    except json.JSONDecodeError:
        detail = row["detail"]
    return {
        "audit_id": row["audit_id"],
        "event_time": row["event_time"],
        "actor": row["actor"],
        "action": row["action"],
        "detail": detail,
    }
