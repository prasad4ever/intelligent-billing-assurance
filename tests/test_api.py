"""API tests for exception review. Decisions are checked on a database copy."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from billing_assurance.api import create_app
from billing_assurance.reconcile import DEFAULT_DB_PATH, reconcile


class ReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self._directory.name) / "billing.sqlite"
        shutil.copy(DEFAULT_DB_PATH, self.db_path)
        reconcile(self.db_path, run_at="2026-04-05T00:00:00Z")
        self.client = TestClient(create_app(self.db_path))
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._directory.cleanup()

    def test_summary_and_exception_list(self) -> None:
        summary = self.client.get("/summary")
        self.assertEqual(summary.status_code, 200)
        body = summary.json()
        self.assertEqual(body["currency"], "CAD")
        self.assertEqual(body["invoices"], 180)
        self.assertEqual(body["invoice_lines"], 606)
        self.assertEqual(body["clean_invoices"], 144)
        self.assertTrue(body["clean_invoices_auto_accepted"])
        self.assertEqual(body["exceptions"], 36)
        self.assertEqual(body["by_status"]["pending_approval"], 30)
        self.assertEqual(body["by_status"]["open"], 6)
        self.assertNotIn("approved", body["by_status"])

        listed = self.client.get("/exceptions")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 36)
        pending = self.client.get("/exceptions", params={"status": "pending_approval"})
        self.assertEqual(pending.json()["count"], 30)
        self.assertTrue(all(item["financial_correction_allowed"] for item in pending.json()["exceptions"]))
        unknown = self.client.get("/exceptions", params={"status": "posted"})
        self.assertEqual(unknown.status_code, 422)

    def test_approve_records_decision_and_audit_once(self) -> None:
        exception_id = self._id_for("incorrect_contractual_price")
        before = self.client.get(f"/exceptions/{exception_id}").json()
        self.assertTrue(before["financial_correction_allowed"])
        self.assertIsNone(before["investigation"])
        self.assertIsNotNone(before["expected_amount_cents"])
        self.assertEqual(
            before["difference_cents"],
            before["billed_amount_cents"] - before["expected_amount_cents"],
        )
        fingerprint = _source_fingerprint(self.db_path)

        approved = self.client.post(
            f"/exceptions/{exception_id}/approve",
            json={"reviewer": "A. Patel", "reason": "Contract price confirmed."},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        body = approved.json()
        self.assertEqual(body["status"], "approved")
        self.assertEqual(body["billed_amount_cents"], before["billed_amount_cents"])
        self.assertEqual(body["expected_amount_cents"], before["expected_amount_cents"])
        self.assertEqual(body["difference_cents"], before["difference_cents"])
        self.assertEqual(body["decision"]["reviewer"], "A. Patel")
        self.assertEqual(body["decision"]["reason"], "Contract price confirmed.")
        self.assertFalse(body["financial_correction_allowed"])

        history = self.client.get(f"/exceptions/{exception_id}/audit")
        self.assertEqual(history.status_code, 200)
        entries = history.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "approve")
        self.assertEqual(entries[0]["actor"], "A. Patel")
        self.assertEqual(entries[0]["detail"]["reason"], "Contract price confirmed.")
        self.assertEqual(entries[0]["detail"]["variance_cents"], before["difference_cents"])

        again = self.client.post(
            f"/exceptions/{exception_id}/approve",
            json={"reviewer": "B. Singh", "reason": "Second try."},
        )
        self.assertEqual(again.status_code, 409)
        flipped = self.client.post(
            f"/exceptions/{exception_id}/reject",
            json={"reviewer": "B. Singh", "reason": "Change the outcome."},
        )
        self.assertEqual(flipped.status_code, 409)
        self.assertEqual(len(self.client.get(f"/exceptions/{exception_id}/audit").json()["entries"]), 1)
        self.assertEqual(_source_fingerprint(self.db_path), fingerprint)

    def test_reject_records_a_separate_audit_entry(self) -> None:
        exception_id = self._id_for("missing_volume_discount")
        rejected = self.client.post(
            f"/exceptions/{exception_id}/reject",
            json={"reviewer": "C. Nguyen", "reason": "Discount was waived in a side letter."},
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["status"], "rejected")
        self.assertEqual(rejected.json()["decision"]["reason"], "Discount was waived in a side letter.")
        entries = self.client.get(f"/exceptions/{exception_id}/audit").json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "reject")
        self.assertEqual(entries[0]["actor"], "C. Nguyen")

    def test_ambiguous_case_stays_open_and_selects_no_rate(self) -> None:
        exception_id = self._id_for("ambiguous_contract_amendment")
        detail = self.client.get(f"/exceptions/{exception_id}")
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(body["status"], "open")
        self.assertFalse(body["financial_correction_allowed"])
        self.assertIsNone(body["expected_amount_cents"])
        self.assertIsNone(body["difference_cents"])
        investigation = body["investigation"]
        self.assertFalse(investigation["authoritative_rate_selected"])
        terms = investigation["conflicting_terms"]
        self.assertGreaterEqual(len(terms), 2)
        self.assertGreater(len({term["unit_price_e4"] for term in terms}), 1)
        self.assertTrue(all(term["amendment_note"] for term in terms))
        self.assertNotIn("selected_pricing_term_id", body)
        self.assertNotIn("selected_pricing_term_id", investigation)

        for path in ("approve", "reject"):
            response = self.client.post(
                f"/exceptions/{exception_id}/{path}",
                json={"reviewer": "A. Patel", "reason": "Use the lower addendum."},
            )
            self.assertEqual(response.status_code, 409, response.text)
        fresh = self.client.get(f"/exceptions/{exception_id}").json()
        self.assertEqual(fresh["status"], "open")
        self.assertIsNone(fresh["decision"])
        self.assertEqual(self.client.get(f"/exceptions/{exception_id}/audit").json()["entries"], [])

    def test_blank_reason_and_missing_exception(self) -> None:
        exception_id = self._id_for("duplicate_charge")
        blank = self.client.post(
            f"/exceptions/{exception_id}/approve",
            json={"reviewer": "A. Patel", "reason": "   "},
        )
        self.assertEqual(blank.status_code, 422)
        missing = self.client.get("/exceptions/999999")
        self.assertEqual(missing.status_code, 404)
        missing_audit = self.client.get("/exceptions/999999/audit")
        self.assertEqual(missing_audit.status_code, 404)

    def test_reconciliation_preserves_recorded_decisions(self) -> None:
        approved_id = self._id_for("incorrect_usage_quantity")
        rejected_id = self._id_for("duplicate_charge")
        fingerprint = _source_fingerprint(self.db_path)
        self.client.post(
            f"/exceptions/{approved_id}/approve",
            json={"reviewer": "A. Patel", "reason": "Usage total is authoritative."},
        )
        self.client.post(
            f"/exceptions/{rejected_id}/reject",
            json={"reviewer": "C. Nguyen", "reason": "Second line is a legitimate minimum fee."},
        )
        approved_before = self.client.get(f"/exceptions/{approved_id}").json()
        rejected_before = self.client.get(f"/exceptions/{rejected_id}").json()

        report = reconcile(self.db_path, run_at="2026-04-06T00:00:00Z")

        approved_after = self.client.get(f"/exceptions/{approved_id}").json()
        rejected_after = self.client.get(f"/exceptions/{rejected_id}").json()
        self.assertEqual(approved_after["status"], "approved")
        self.assertEqual(approved_after["decision"], approved_before["decision"])
        self.assertEqual(approved_after["evidence_note"], approved_before["evidence_note"])
        self.assertEqual(rejected_after["status"], "rejected")
        self.assertEqual(rejected_after["decision"]["reason"], rejected_before["decision"]["reason"])
        summary = self.client.get("/summary").json()
        self.assertEqual(summary["exceptions"], 36)
        self.assertEqual(summary["by_status"]["approved"], 1)
        self.assertEqual(summary["by_status"]["rejected"], 1)
        self.assertEqual(summary["clean_invoices"], 144)
        self.assertEqual(report.exceptions_inserted, 34)
        self.assertEqual(_source_fingerprint(self.db_path), fingerprint)
        self.assertEqual(_duplicate_exception_keys(self.db_path), [])

    def _id_for(self, exception_type: str) -> int:
        listed = self.client.get("/exceptions", params={"exception_type": exception_type})
        self.assertEqual(listed.status_code, 200)
        return listed.json()["exceptions"][0]["exception_id"]


def _source_fingerprint(db_path: Path) -> tuple:
    connection = sqlite3.connect(db_path)
    try:
        return (
            connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_amount_cents), 0) FROM invoices"
            ).fetchone(),
            connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0), COALESCE(SUM(unit_price_e4), 0) FROM invoice_lines"
            ).fetchone(),
            connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(unit_price_e4), 0) FROM pricing_terms"
            ).fetchone(),
        )
    finally:
        connection.close()


def _duplicate_exception_keys(db_path: Path) -> list[tuple]:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            """
            SELECT invoice_id, exception_type, invoice_line_id, COUNT(exception_id) AS n
            FROM billing_exceptions
            GROUP BY invoice_id, exception_type, invoice_line_id
            HAVING n > 1
            """
        ).fetchall()
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
