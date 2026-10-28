"""Dashboard client tests against a live API and a copy of the database."""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from billing_assurance.dashboard_client import ApiError, BillingClient, format_cad_cents, format_unit_price_e4
from billing_assurance.reconcile import DEFAULT_DB_PATH

ROOT = Path(__file__).resolve().parents[1]


class DisplayTests(unittest.TestCase):
    def test_cad_amounts_use_cent_arithmetic(self) -> None:
        self.assertEqual(format_cad_cents(250_000), "$2,500.00")
        self.assertEqual(format_cad_cents(50), "$0.50")
        self.assertEqual(format_cad_cents(-125), "-$1.25")
        self.assertEqual(format_cad_cents(None), "Not determined")
        self.assertEqual(format_unit_price_e4(1_000), "$0.1000")
        self.assertEqual(format_unit_price_e4(25_000_000), "$2,500.0000")
        self.assertEqual(format_unit_price_e4(None), "—")


class DashboardApiTests(unittest.TestCase):
    def test_dashboard_client_reviews_a_copy_and_leaves_the_original_database(self) -> None:
        original_approved = _approved_count(DEFAULT_DB_PATH)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "billing.sqlite"
            shutil.copy(DEFAULT_DB_PATH, db_path)
            port = _free_port()
            process = _start_api(db_path, port)
            try:
                client = BillingClient(f"http://127.0.0.1:{port}")
                _wait_for_summary(client)
                totals = client.summary()
                self.assertEqual(totals["invoices"], 180)
                self.assertEqual(totals["clean_invoices"], 144)
                self.assertEqual(totals["exceptions"], 36)
                self.assertIn("pending_variance_cents", totals)

                pending = [
                    item
                    for item in client.exceptions()["exceptions"]
                    if item["financial_correction_allowed"]
                ]
                ambiguous = [
                    item
                    for item in client.exceptions()["exceptions"]
                    if item["exception_type"] == "ambiguous_contract_amendment"
                ]
                self.assertTrue(pending)
                self.assertTrue(ambiguous)

                detail = client.exception(ambiguous[0]["exception_id"])
                self.assertFalse(detail["financial_correction_allowed"])
                self.assertIsNone(detail["expected_amount_cents"])
                terms = detail["investigation"]["conflicting_terms"]
                self.assertGreaterEqual(len(terms), 2)
                self.assertFalse(detail["investigation"]["authoritative_rate_selected"])
                with self.assertRaises(ApiError) as blocked:
                    client.approve(ambiguous[0]["exception_id"], "Dashboard Tester", "Pick a rate.")
                self.assertEqual(blocked.exception.status_code, 409)
                self.assertEqual(client.audit(ambiguous[0]["exception_id"])["entries"], [])

                target = pending[0]["exception_id"]
                approved = client.approve(target, "Dashboard Tester", "Confirmed from the contract.")
                self.assertEqual(approved["status"], "approved")
                self.assertEqual(client.exception(target)["status"], "approved")
                entries = client.audit(target)["entries"]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["action"], "approve")
                self.assertEqual(entries[0]["detail"]["reason"], "Confirmed from the contract.")

                other = next(item["exception_id"] for item in pending if item["exception_id"] != target)
                rejected = client.reject(other, "Dashboard Tester", "Not a billing error.")
                self.assertEqual(rejected["status"], "rejected")
                self.assertEqual(client.audit(other)["entries"][0]["action"], "reject")
            finally:
                _stop(process)
        self.assertEqual(_approved_count(DEFAULT_DB_PATH), original_approved)


def _approved_count(db_path: Path) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT COUNT(*) FROM billing_exceptions WHERE status = 'approved'"
        ).fetchone()[0]
    finally:
        connection.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_api(db_path: Path, port: int) -> subprocess.Popen:
    script = (
        "import os\n"
        "from pathlib import Path\n"
        "import uvicorn\n"
        "from billing_assurance.api import create_app\n"
        "uvicorn.run(create_app(Path(os.environ['BILLING_DB'])), "
        "host='127.0.0.1', port=int(os.environ['BILLING_PORT']), log_level='warning')\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={**os.environ, "BILLING_DB": str(db_path), "BILLING_PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_summary(client: BillingClient) -> None:
    last_error = None
    for _ in range(50):
        try:
            client.summary()
            return
        except ApiError as exc:
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"API did not start: {last_error}")


def _stop(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


class DashboardStartupTests(unittest.TestCase):
    def test_streamlit_starts_and_reaches_the_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "billing.sqlite"
            shutil.copy(DEFAULT_DB_PATH, db_path)
            api_port = _free_port()
            ui_port = _free_port()
            api = _start_api(db_path, api_port)
            ui = None
            try:
                _wait_for_summary(BillingClient(f"http://127.0.0.1:{api_port}"))
                ui = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "streamlit",
                        "run",
                        str(ROOT / "streamlit_app.py"),
                        "--server.headless",
                        "true",
                        "--server.port",
                        str(ui_port),
                        "--browser.gatherUsageStats",
                        "false",
                    ],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "BILLING_API_URL": f"http://127.0.0.1:{api_port}",
                        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
                    },
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                health = _wait_for_url(f"http://127.0.0.1:{ui_port}/_stcore/health").strip().lower()
                self.assertIn(health, {"ok", "healthy"})
                page = _wait_for_url(f"http://127.0.0.1:{ui_port}/")
                self.assertIn("streamlit", page.lower())
            finally:
                if ui is not None:
                    _stop(ui)
                _stop(api)


def _wait_for_url(url: str) -> str:
    last_error = None
    for _ in range(80):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - retry until the server accepts connections
            last_error = exc
            time.sleep(0.25)
    raise AssertionError(f"{url} did not respond: {last_error}")


if __name__ == "__main__":
    unittest.main()
