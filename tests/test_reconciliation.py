"""Tests for draft-invoice reconciliation.

Accuracy against known faults is checked here, after the engine runs.
The engine itself does not read the testing label file.
"""

from __future__ import annotations

import ast
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from billing_assurance.money import amount_cents, dollars_to_e4
from billing_assurance.reconcile import (
    AMBIGUOUS,
    DISCOUNT,
    DUPLICATE,
    PRICE,
    USAGE,
    DEFAULT_DB_PATH,
    reconcile,
)
from billing_assurance.schema import SCHEMA_SQL

ROOT = Path(__file__).resolve().parents[1]
LABELS_PATH = ROOT / "evaluation" / "injected_scenario_labels.json"
CREATED = "2026-04-01T12:00:00Z"


class EngineBoundaryTests(unittest.TestCase):
    def test_engine_does_not_import_generator_or_open_labels(self) -> None:
        source_path = ROOT / "billing_assurance" / "reconcile.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
        self.assertFalse(any("generate" in name or "validate" in name for name in modules))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for constant in _string_constants(node):
                    self.assertNotIn("injected_scenario_labels", constant)
                    self.assertNotIn("evaluation", constant)


class RuleTests(unittest.TestCase):
    def test_rules_and_rerun_on_a_small_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "fixture.sqlite"
            _build_fixture(db_path)
            before = _fingerprint(db_path)
            first = reconcile(db_path, run_at="2026-04-02T00:00:00Z")
            detected = _exceptions_by_invoice(db_path)
            self.assertEqual(
                detected,
                {
                    "INV-FEB": {USAGE},
                    "INV-MAR": {PRICE},
                    "INV-APR": {AMBIGUOUS},
                    "INV-MAY": {DISCOUNT},
                    "INV-JUN": {DUPLICATE},
                },
            )
            self.assertEqual(first.discrepancies_detected, 5)
            self.assertIsNone(_field(db_path, AMBIGUOUS, "expected_amount_cents"))
            self.assertIsNone(_field(db_path, AMBIGUOUS, "variance_cents"))
            self.assertEqual(_field(db_path, AMBIGUOUS, "status"), "open")
            self.assertEqual(_field(db_path, PRICE, "status"), "pending_approval")
            self.assertEqual(_field(db_path, DISCOUNT, "variance_cents"), 400)
            self.assertEqual(_field(db_path, DUPLICATE, "variance_cents"), 110_000)
            self.assertIsNone(_field(db_path, USAGE, "decided_by"))

            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    UPDATE billing_exceptions
                    SET decided_at = ?, decided_by = ?, decision_note = ?
                    WHERE exception_type = ?
                    """,
                    ("2026-04-03T00:00:00Z", "analyst", "kept for review", USAGE),
                )
                connection.commit()
            finally:
                connection.close()

            second = reconcile(db_path, run_at="2026-04-04T00:00:00Z")
            self.assertEqual(second.exceptions_inserted, 4)
            self.assertEqual(_exceptions_by_invoice(db_path), detected)
            self.assertEqual(_duplicate_keys(db_path), [])
            self.assertEqual(_field(db_path, USAGE, "decision_note"), "kept for review")
            self.assertEqual(_audit_count(db_path), 2)
            self.assertEqual(_fingerprint(db_path), before)


class DatasetEvaluationTests(unittest.TestCase):
    def test_detection_matches_testing_labels_without_false_flags(self) -> None:
        self.assertTrue(DEFAULT_DB_PATH.is_file(), "run scripts/init_db.py before this test")
        self.assertTrue(LABELS_PATH.is_file())
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "billing.sqlite"
            shutil.copy(DEFAULT_DB_PATH, db_path)
            before = _fingerprint(db_path)
            reconcile_audits = _audit_actions(db_path).count("reconcile")
            report = reconcile(db_path, run_at="2026-04-02T00:00:00Z")
            labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
            expected = {
                (case["invoice_number"], case["charge_code"]): case["scenario"]
                for case in labels["scenarios"]
            }
            detected = _detected_map(db_path)
            missed = set(expected) - set(detected)
            false_flags = set(detected) - set(expected)
            self.assertEqual(missed, set(), f"missed faults: {sorted(missed)}")
            self.assertEqual(false_flags, set(), f"false flags: {sorted(false_flags)}")
            mismatches = {
                key: (detected[key], expected[key])
                for key in expected
                if detected[key] != expected[key]
            }
            self.assertEqual(mismatches, {})
            self.assertEqual(report.invoices_checked, 180)
            self.assertEqual(report.invoice_lines_checked, 606)
            self.assertEqual(report.discrepancies_detected, 36)
            self.assertEqual(
                report.by_type,
                {
                    AMBIGUOUS: 6,
                    DISCOUNT: 8,
                    DUPLICATE: 6,
                    PRICE: 8,
                    USAGE: 8,
                },
            )
            self.assertEqual(_clean_invoices_with_exceptions(db_path, expected), [])
            self.assertEqual(_duplicate_keys(db_path), [])
            self.assertEqual(_status_problems(db_path), [])
            self.assertEqual(_fingerprint(db_path), before)

            again = reconcile(db_path, run_at="2026-04-03T00:00:00Z")
            self.assertEqual(again.discrepancies_detected, 36)
            self.assertEqual(again.exceptions_inserted, 36)
            self.assertEqual(_detected_map(db_path), detected)
            self.assertEqual(_duplicate_keys(db_path), [])
            self.assertEqual(_audit_actions(db_path).count("reconcile"), reconcile_audits + 2)
            self.assertEqual(_fingerprint(db_path), before)


def _string_constants(function: ast.FunctionDef):
    for node in ast.walk(function):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _fingerprint(db_path: Path) -> tuple:
    connection = _connect(db_path)
    try:
        return (
            connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_amount_cents), 0) FROM invoices"
            ).fetchone(),
            connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0) FROM invoice_lines"
            ).fetchone(),
            connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM usage_records"
            ).fetchone(),
            connection.execute("SELECT COUNT(*), COALESCE(SUM(unit_price_e4), 0) FROM pricing_terms").fetchone(),
            connection.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM contracts").fetchone()[0],
        )
    finally:
        connection.close()


def _exceptions_by_invoice(db_path: Path) -> dict[str, set[str]]:
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT invoice.invoice_number, exception.exception_type
            FROM billing_exceptions AS exception
            JOIN invoices AS invoice ON invoice.invoice_id = exception.invoice_id
            """
        ).fetchall()
    finally:
        connection.close()
    grouped: dict[str, set[str]] = {}
    for row in rows:
        grouped.setdefault(row["invoice_number"], set()).add(row["exception_type"])
    return grouped


def _detected_map(db_path: Path) -> dict[tuple[str, str], str]:
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT invoice.invoice_number, line.charge_code, exception.exception_type
            FROM billing_exceptions AS exception
            JOIN invoices AS invoice ON invoice.invoice_id = exception.invoice_id
            JOIN invoice_lines AS line ON line.invoice_line_id = exception.invoice_line_id
            """
        ).fetchall()
    finally:
        connection.close()
    return {(row["invoice_number"], row["charge_code"]): row["exception_type"] for row in rows}


def _clean_invoices_with_exceptions(db_path: Path, expected: dict[tuple[str, str], str]) -> list[str]:
    labeled = {invoice_number for invoice_number, _charge in expected}
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT invoice.invoice_number
            FROM billing_exceptions AS exception
            JOIN invoices AS invoice ON invoice.invoice_id = exception.invoice_id
            """
        ).fetchall()
    finally:
        connection.close()
    return sorted(row["invoice_number"] for row in rows if row["invoice_number"] not in labeled)


def _field(db_path: Path, exception_type: str, column: str):
    connection = _connect(db_path)
    try:
        row = connection.execute(
            f"SELECT {column} FROM billing_exceptions WHERE exception_type = ?",
            (exception_type,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else row[0]


def _status_problems(db_path: Path) -> list[tuple]:
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT exception_type, status, expected_amount_cents, variance_cents, decided_by
            FROM billing_exceptions
            WHERE decided_by IS NOT NULL
               OR status NOT IN ('open', 'pending_approval')
               OR (
                    exception_type = ?
                    AND (
                        status != 'open'
                        OR expected_amount_cents IS NOT NULL
                        OR variance_cents IS NOT NULL
                    )
               )
               OR (
                    exception_type != ?
                    AND (
                        status != 'pending_approval'
                        OR expected_amount_cents IS NULL
                        OR variance_cents IS NULL
                        OR variance_cents != invoiced_amount_cents - expected_amount_cents
                    )
               )
            """,
            (AMBIGUOUS, AMBIGUOUS),
        ).fetchall()
    finally:
        connection.close()
    return [tuple(row) for row in rows]


def _duplicate_keys(db_path: Path) -> list[tuple]:
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT invoice_id, exception_type, invoice_line_id, COUNT(*) AS n
            FROM billing_exceptions
            GROUP BY invoice_id, exception_type, invoice_line_id
            HAVING n > 1
            """
        ).fetchall()
    finally:
        connection.close()
    return [tuple(row) for row in rows]


def _audit_count(db_path: Path) -> int:
    connection = _connect(db_path)
    try:
        return connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    finally:
        connection.close()


def _audit_actions(db_path: Path) -> list[str]:
    connection = _connect(db_path)
    try:
        return [row[0] for row in connection.execute("SELECT action FROM audit_log ORDER BY audit_id")]
    finally:
        connection.close()


def _build_fixture(db_path: Path) -> None:
    platform_jan = dollars_to_e4("1000.00")
    platform_later = dollars_to_e4("1100.00")
    list_price = 1_000
    discount_price = 800
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO customers (
                customer_id, customer_code, customer_name, institution_type, status, created_at
            ) VALUES (1, 'FI-900', 'Fixture Credit Union', 'Credit Union', 'active', ?)
            """,
            (CREATED,),
        )
        connection.execute(
            """
            INSERT INTO contracts (
                contract_id, customer_id, contract_number, currency_code,
                start_date, end_date, status, created_at
            ) VALUES (1, 1, 'CA-FIXTURE', 'CAD', '2026-01-01', '2026-12-31', 'active', ?)
            """,
            (CREATED,),
        )
        terms = [
            (1, "CORE_PLATFORM", "Core platform subscription", "fixed_monthly", platform_jan, None, None, "2026-01-01", "2026-01-31", 1, "January platform fee."),
            (2, "CORE_PLATFORM", "Core platform subscription", "fixed_monthly", platform_later, None, None, "2026-02-01", None, 2, "Platform fee revised effective 1 February 2026."),
            (3, "TXN_PROCESSING", "Transaction processing", "per_unit", list_price, 100, discount_price, "2026-01-01", None, 1, "Transaction schedule with a monthly volume price."),
            (4, "TXN_PROCESSING", "Transaction processing", "per_unit", 900, 100, 720, "2026-04-01", "2026-04-30", 2, "Addendum A effective 1 April 2026."),
            (5, "TXN_PROCESSING", "Transaction processing", "per_unit", 1_400, 100, 1_120, "2026-04-01", "2026-04-30", 3, "Addendum B effective 1 April 2026."),
            (6, "TXN_PROCESSING", "Transaction processing", "per_unit", 700, 100, 560, "2026-07-01", "2026-07-31", 4, "July schedule copy A."),
            (7, "TXN_PROCESSING", "Transaction processing", "per_unit", 700, 100, 560, "2026-07-01", "2026-07-31", 5, "July schedule copy B."),
        ]
        connection.executemany(
            """
            INSERT INTO pricing_terms (
                pricing_term_id, contract_id, charge_code, charge_name, charge_model,
                unit_price_e4, volume_threshold, discounted_unit_price_e4,
                effective_start, effective_end, amendment_number, amendment_note, created_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(*term, CREATED) for term in terms],
        )
        usage = [
            ("2026-01-15", 150),
            ("2026-02-15", 50),
            ("2026-03-15", 40),
            ("2026-04-15", 10),
            ("2026-05-15", 200),
            ("2026-06-15", 30),
            ("2026-07-15", 10),
        ]
        connection.executemany(
            """
            INSERT INTO usage_records (
                customer_id, contract_id, usage_date, meter_code, quantity,
                aggregation_level, created_at
            ) VALUES (1, 1, ?, 'TXN_PROCESSING', ?, 'daily', ?)
            """,
            [(day, quantity, CREATED) for day, quantity in usage],
        )
        invoices = _fixture_invoices(platform_jan, platform_later, list_price, discount_price)
        for invoice in invoices:
            connection.execute(
                """
                INSERT INTO invoices (
                    invoice_id, invoice_number, customer_id, contract_id,
                    billing_period_start, billing_period_end, invoice_date, status,
                    currency_code, total_amount_cents, created_at
                ) VALUES (?, ?, 1, 1, ?, ?, ?, 'draft', 'CAD', ?, ?)
                """,
                (
                    invoice["invoice_id"],
                    invoice["number"],
                    invoice["start"],
                    invoice["end"],
                    invoice["end"],
                    sum(line[6] for line in invoice["lines"]),
                    CREATED,
                ),
            )
            connection.executemany(
                """
                INSERT INTO invoice_lines (
                    invoice_line_id, invoice_id, line_number, charge_code, description,
                    quantity, unit_price_e4, amount_cents, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(*line, CREATED) for line in invoice["lines"]],
            )
        connection.commit()
    finally:
        connection.close()


def _fixture_invoices(platform_jan: int, platform_later: int, list_price: int, discount_price: int) -> list[dict]:
    def line(line_id, invoice_id, number, charge, quantity, price):
        return (
            line_id,
            invoice_id,
            number,
            charge,
            f"{charge} {quantity}",
            quantity,
            price,
            amount_cents(quantity, price),
        )

    return [
        {
            "invoice_id": 1,
            "number": "INV-JAN",
            "start": "2026-01-01",
            "end": "2026-01-31",
            "lines": [
                line(1, 1, 1, "CORE_PLATFORM", 1, platform_jan),
                line(2, 1, 2, "TXN_PROCESSING", 150, discount_price),
            ],
        },
        {
            "invoice_id": 2,
            "number": "INV-FEB",
            "start": "2026-02-01",
            "end": "2026-02-28",
            "lines": [
                line(3, 2, 1, "CORE_PLATFORM", 1, platform_later),
                line(4, 2, 2, "TXN_PROCESSING", 80, list_price),
            ],
        },
        {
            "invoice_id": 3,
            "number": "INV-MAR",
            "start": "2026-03-01",
            "end": "2026-03-31",
            "lines": [
                line(5, 3, 1, "CORE_PLATFORM", 1, platform_later),
                line(6, 3, 2, "TXN_PROCESSING", 40, 1_500),
            ],
        },
        {
            "invoice_id": 4,
            "number": "INV-APR",
            "start": "2026-04-01",
            "end": "2026-04-30",
            "lines": [
                line(7, 4, 1, "CORE_PLATFORM", 1, platform_later),
                line(8, 4, 2, "TXN_PROCESSING", 10, 900),
            ],
        },
        {
            "invoice_id": 5,
            "number": "INV-MAY",
            "start": "2026-05-01",
            "end": "2026-05-31",
            "lines": [
                line(9, 5, 1, "CORE_PLATFORM", 1, platform_later),
                line(10, 5, 2, "TXN_PROCESSING", 200, list_price),
            ],
        },
        {
            "invoice_id": 6,
            "number": "INV-JUN",
            "start": "2026-06-01",
            "end": "2026-06-30",
            "lines": [
                line(11, 6, 1, "CORE_PLATFORM", 1, platform_later),
                line(12, 6, 2, "CORE_PLATFORM", 1, platform_later),
                line(13, 6, 3, "TXN_PROCESSING", 30, list_price),
            ],
        },
        {
            "invoice_id": 7,
            "number": "INV-JUL",
            "start": "2026-07-01",
            "end": "2026-07-31",
            "lines": [
                line(14, 7, 1, "CORE_PLATFORM", 1, platform_later),
                line(15, 7, 2, "TXN_PROCESSING", 10, 700),
            ],
        },
    ]


if __name__ == "__main__":
    unittest.main()
