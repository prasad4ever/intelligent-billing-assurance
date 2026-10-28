"""AI investigation tests. The language model is mocked except for one optional live call."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from billing_assurance.api import create_app
from billing_assurance.investigate import (
    RATE_STATEMENT,
    InvestigationError,
    parse_model_output,
    system_prompt,
)
from billing_assurance.reconcile import DEFAULT_DB_PATH, reconcile


class InvestigationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self._directory.name) / "billing.sqlite"
        shutil.copy(DEFAULT_DB_PATH, self.db_path)
        reconcile(self.db_path, run_at="2026-04-05T00:00:00Z")
        self.prompts: list[tuple[str, str]] = []
        self.client = TestClient(create_app(self.db_path, completer=self._complete))
        self.client.__enter__()
        self.reply = "valid"

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._directory.cleanup()

    def test_mocked_analysis_explains_conflict_without_changing_records(self) -> None:
        exception_id = self._id_for("ambiguous_contract_amendment")
        before = self.client.get(f"/exceptions/{exception_id}").json()
        digest = _digest(self.db_path)

        response = self.client.post(f"/exceptions/{exception_id}/analyze")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["human_review_required"])
        self.assertFalse(body["authoritative_rate_selected"])
        self.assertEqual(body["rate_statement"], RATE_STATEMENT)
        self.assertIn("Addendum A", body["explanation"])
        self.assertIn(RATE_STATEMENT, body["uncertainty"])
        self.assertTrue(body["recommended_steps"])
        notes = [
            item["amendment_note"]
            for item in body["evidence"]
            if item["kind"] == "contract_term"
        ]
        self.assertGreaterEqual(len(notes), 2)
        self.assertTrue(any("Addendum A" in note for note in notes))
        self.assertTrue(any("Addendum B" in note for note in notes))
        self.assertTrue(any(item["cited"] for item in body["evidence"]))
        self.assertNotIn("selected_unit_price_e4", body)
        self.assertIsNone(body["model"])

        system, user = self.prompts[0]
        self.assertNotIn("injected", user.lower())
        self.assertNotIn("ambiguous_contract_amendment", user)
        self.assertIn("Addendum A", user)
        self.assertIn("measured_quantity", user)
        self.assertIn("do not choose", system_prompt().lower())
        self.assertIn(RATE_STATEMENT, system)

        after = self.client.get(f"/exceptions/{exception_id}").json()
        self.assertEqual(after["status"], "open")
        self.assertEqual(after["billed_amount_cents"], before["billed_amount_cents"])
        self.assertIsNone(after["expected_amount_cents"])
        self.assertIsNone(after["difference_cents"])
        self.assertEqual(after["evidence_note"], before["evidence_note"])
        self.assertEqual(after["investigation"], before["investigation"])
        self.assertEqual(self.client.get(f"/exceptions/{exception_id}/audit").json()["entries"], [])
        self.assertEqual(_digest(self.db_path), digest)

    def test_invalid_json_is_rejected(self) -> None:
        self.reply = "invalid-json"
        exception_id = self._id_for("ambiguous_contract_amendment")
        digest = _digest(self.db_path)
        response = self.client.post(f"/exceptions/{exception_id}/analyze")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("not valid JSON", response.json()["detail"])
        self.assertEqual(_digest(self.db_path), digest)

    def test_rate_selection_is_discarded(self) -> None:
        self.reply = "selects-rate"
        exception_id = self._id_for("ambiguous_contract_amendment")
        digest = _digest(self.db_path)
        response = self.client.post(f"/exceptions/{exception_id}/analyze")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("select a contractual rate", response.json()["detail"])
        fresh = self.client.get(f"/exceptions/{exception_id}").json()
        self.assertEqual(fresh["status"], "open")
        self.assertIsNone(fresh["expected_amount_cents"])
        self.assertEqual(_digest(self.db_path), digest)

    def test_invented_evidence_is_rejected(self) -> None:
        self.reply = "invented-evidence"
        exception_id = self._id_for("ambiguous_contract_amendment")
        response = self.client.post(f"/exceptions/{exception_id}/analyze")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("was not supplied", response.json()["detail"])

    def test_missing_api_key_does_not_call_a_model(self) -> None:
        exception_id = self._id_for("ambiguous_contract_amendment")
        client = TestClient(create_app(self.db_path))
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            response = client.post(f"/exceptions/{exception_id}/analyze")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("OPENAI_API_KEY is not configured", response.json()["detail"])
        self.assertIn("unavailable", response.json()["detail"])

    def test_other_exception_types_are_not_analyzed(self) -> None:
        exception_id = self._id_for("incorrect_contractual_price")
        response = self.client.post(f"/exceptions/{exception_id}/analyze")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.prompts, [])

    def test_missing_exception(self) -> None:
        response = self.client.post("/exceptions/999999/analyze")
        self.assertEqual(response.status_code, 404)

    def _complete(self, system: str, user: str) -> str:
        self.prompts.append((system, user))
        packet = json.loads(user)
        ids = [item["evidence_id"] for item in packet["evidence"]]
        if self.reply == "invalid-json":
            return "this is not json"
        if self.reply == "selects-rate":
            return json.dumps(
                {
                    "explanation": "Use amendment 2.",
                    "uncertainty": RATE_STATEMENT,
                    "recommended_steps": ["Confirm the file."],
                    "evidence_cited": ids[:1],
                    "selected_unit_price_e4": 1000,
                }
            )
        if self.reply == "invented-evidence":
            return json.dumps(
                {
                    "explanation": "A side letter sets the price.",
                    "uncertainty": RATE_STATEMENT,
                    "recommended_steps": ["Find the side letter."],
                    "evidence_cited": ["side-letter"],
                }
            )
        return json.dumps(
            {
                "explanation": (
                    "Addendum A and Addendum B both start on 2026-03-01 and state different prices. "
                    "Addendum A still lacks a countersignature, and Addendum B does not confirm that it replaced Addendum A."
                ),
                "uncertainty": "It is unclear which addendum was executed.",
                "recommended_steps": [
                    "Ask sales whether Addendum A was countersigned.",
                    "Ask billing operations whether Addendum B replaced Addendum A.",
                ],
                "evidence_cited": ids,
            }
        )

    def _id_for(self, exception_type: str) -> int:
        listed = self.client.get("/exceptions", params={"exception_type": exception_type})
        self.assertEqual(listed.status_code, 200)
        return listed.json()["exceptions"][0]["exception_id"]


class ParserTests(unittest.TestCase):
    def test_fenced_json_and_missing_rate_sentence(self) -> None:
        raw = """```json
        {"explanation": "The notes disagree.", "uncertainty": "The signature is missing.",
         "recommended_steps": ["Request the executed copy."], "evidence_cited": ["invoice"]}
        ```"""
        parsed = parse_model_output(raw, {"invoice"})
        self.assertIn(RATE_STATEMENT, parsed["uncertainty"])
        self.assertEqual(parsed["evidence_cited"], ["invoice"])

    def test_empty_and_non_object_responses(self) -> None:
        with self.assertRaises(InvestigationError):
            parse_model_output("   ", {"invoice"})
        with self.assertRaises(InvestigationError):
            parse_model_output("[]", {"invoice"})
        with self.assertRaises(InvestigationError):
            parse_model_output(
                json.dumps(
                    {
                        "explanation": "Notes disagree.",
                        "uncertainty": RATE_STATEMENT,
                        "recommended_steps": [],
                        "evidence_cited": ["invoice"],
                    }
                ),
                {"invoice"},
            )


@unittest.skipUnless(os.environ.get("OPENAI_API_KEY", "").strip(), "OPENAI_API_KEY is not configured")
class LiveInvestigationTests(unittest.TestCase):
    def test_one_live_analysis_does_not_change_records(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db_path = Path(directory.name) / "billing.sqlite"
        shutil.copy(DEFAULT_DB_PATH, db_path)
        reconcile(db_path, run_at="2026-04-05T00:00:00Z")
        client = TestClient(create_app(db_path))
        self.addCleanup(client.close)
        listed = client.get("/exceptions", params={"exception_type": "ambiguous_contract_amendment"})
        exception_id = listed.json()["exceptions"][0]["exception_id"]
        digest = _digest(db_path)
        response = client.post(f"/exceptions/{exception_id}/analyze")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["human_review_required"])
        self.assertFalse(body["authoritative_rate_selected"])
        self.assertEqual(body["rate_statement"], RATE_STATEMENT)
        self.assertTrue(body["explanation"].strip())
        self.assertIn(RATE_STATEMENT, body["uncertainty"])
        self.assertTrue(body["recommended_steps"])
        self.assertTrue(body["model"])
        self.assertEqual(client.get(f"/exceptions/{exception_id}").json()["status"], "open")
        self.assertEqual(_digest(db_path), digest)


def _digest(db_path: Path) -> tuple:
    connection = sqlite3.connect(db_path)
    try:
        return (
            connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_amount_cents), 0) FROM invoices"
            ).fetchone(),
            connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0), COALESCE(SUM(unit_price_e4), 0), "
                "COALESCE(SUM(quantity), 0) FROM invoice_lines"
            ).fetchone(),
            connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(unit_price_e4), 0) FROM pricing_terms"
            ).fetchone(),
            connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM usage_records"
            ).fetchone(),
            connection.execute(
                "SELECT status, COUNT(*) FROM billing_exceptions GROUP BY status ORDER BY status"
            ).fetchall(),
            connection.execute("SELECT COUNT(*), COALESCE(SUM(audit_id), 0) FROM audit_log").fetchone(),
        )
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
