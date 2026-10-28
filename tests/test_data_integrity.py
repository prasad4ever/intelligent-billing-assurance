"""Integrity tests for the synthetic billing database."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from billing_assurance.generate import (
    GENERATION_SEED,
    PLATFORM_AMENDMENT_E4,
    build_database,
    daily_quantity,
)
from billing_assurance.money import (
    PriceTerm,
    amount_cents,
    covers_period,
    dollars_to_e4,
    price_from_term,
)
from billing_assurance.validate import LABEL_SENTINEL, validate_database


class MoneyTests(unittest.TestCase):
    def test_fixed_fee_extends_without_rounding(self) -> None:
        self.assertEqual(amount_cents(1, dollars_to_e4("2500.00")), 250_000)

    def test_usage_extension_is_exact_in_cents(self) -> None:
        self.assertEqual(amount_cents(10_000, dollars_to_e4("0.0800")), 80_000)

    def test_half_up_rounding_to_cents(self) -> None:
        self.assertEqual(amount_cents(1, 150), 2)
        self.assertEqual(amount_cents(1, 50), 1)
        self.assertEqual(amount_cents(1, 1), 0)

    def test_volume_price_applies_to_the_whole_quantity(self) -> None:
        term = PriceTerm(
            pricing_term_id=1,
            contract_id=1,
            charge_code="TXN_PROCESSING",
            charge_model="per_unit",
            unit_price_e4=1_000,
            volume_threshold=8_000,
            discounted_unit_price_e4=800,
            effective_start="2026-01-01",
            effective_end=None,
            amendment_number=1,
        )
        self.assertEqual(price_from_term(term, 7_999), 1_000)
        self.assertEqual(price_from_term(term, 8_000), 800)

    def test_open_ended_term_covers_a_later_month_only(self) -> None:
        self.assertFalse(
            covers_period("2026-03-01", None, "2026-02-01", "2026-02-28")
        )
        self.assertTrue(
            covers_period("2026-03-01", None, "2026-03-01", "2026-03-31")
        )
        self.assertFalse(
            covers_period("2026-01-01", "2026-01-31", "2026-02-01", "2026-02-28")
        )


class DatasetTests(unittest.TestCase):
    def test_database_passes_validation_and_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first_root = Path(first_dir)
            second_root = Path(second_dir)
            first_db = first_root / "billing.sqlite"
            second_db = second_root / "billing.sqlite"
            first_labels = first_root / "labels.json"
            second_labels = second_root / "labels.json"
            build_database(first_db, first_labels)
            build_database(second_db, second_labels)

            checker = validate_database(first_db, first_labels)
            self.assertEqual(checker.errors, [], "\n".join(checker.errors))
            self.assertGreater(checker.checks, 100)
            self.assertEqual(
                first_labels.read_text(encoding="utf-8"),
                second_labels.read_text(encoding="utf-8"),
            )
            self.assertEqual(_dump(first_db), _dump(second_db))
            self.assertNotIn(LABEL_SENTINEL.encode("ascii"), first_db.read_bytes())

    def test_seeded_daily_usage_is_stored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "billing.sqlite"
            build_database(db_path, root / "labels.json")
            expected = daily_quantity(1, "TXN_PROCESSING", date(2026, 1, 1))
            connection = sqlite3.connect(db_path)
            try:
                stored = connection.execute(
                    """
                    SELECT quantity FROM usage_records
                    WHERE customer_id = 1 AND meter_code = 'TXN_PROCESSING'
                      AND usage_date = '2026-01-01'
                    """
                ).fetchone()[0]
                amendment = connection.execute(
                    """
                    SELECT february.unit_price_e4 - january.unit_price_e4
                    FROM pricing_terms AS january
                    JOIN pricing_terms AS february
                      ON february.contract_id = january.contract_id
                     AND february.charge_code = january.charge_code
                    WHERE january.contract_id = 2
                      AND january.charge_code = 'CORE_PLATFORM'
                      AND january.effective_start = '2026-01-01'
                      AND february.effective_start = '2026-02-01'
                    """
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(stored, expected)
            self.assertEqual(GENERATION_SEED, 42)
            self.assertEqual(amendment, PLATFORM_AMENDMENT_E4)
            self.assertEqual(PLATFORM_AMENDMENT_E4, dollars_to_e4("250.00"))

    def test_designed_population_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "billing.sqlite"
            build_database(db_path, root / "labels.json")
            expected_terms = (
                60
                + 45
                + 15 * 2
                + 54
                + 6 * 3
                + 60
                + 20
            )
            expected_lines = 60 * 3 * 3 + 20 * 3 + 6
            expected_usage = 60 * 2 * (31 + 28 + 31)
            connection = sqlite3.connect(db_path)
            try:
                counts = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "pricing_terms",
                        "invoice_lines",
                        "usage_records",
                        "billing_exceptions",
                    )
                }
            finally:
                connection.close()
            self.assertEqual(counts["pricing_terms"], expected_terms)
            self.assertEqual(counts["invoice_lines"], expected_lines)
            self.assertEqual(counts["usage_records"], expected_usage)
            self.assertEqual(counts["billing_exceptions"], 0)


def _dump(db_path: Path) -> dict[str, list[tuple]]:
    tables = (
        "customers",
        "contracts",
        "pricing_terms",
        "usage_records",
        "invoices",
        "invoice_lines",
        "billing_exceptions",
        "audit_log",
    )
    connection = sqlite3.connect(db_path)
    try:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in tables
        }
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
