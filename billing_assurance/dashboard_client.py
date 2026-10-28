"""HTTP client for the billing review dashboard.

The dashboard displays and submits what the API returns. It does not price
invoices or decide whether a correction is allowed.
"""

from __future__ import annotations

import httpx

CATEGORY_LABELS = {
    "incorrect_usage_quantity": "Incorrect usage quantity",
    "incorrect_contractual_price": "Incorrect contractual price",
    "missing_volume_discount": "Missing volume discount",
    "duplicate_charge": "Duplicate charge",
    "ambiguous_contract_amendment": "Ambiguous contract amendment",
    "unresolved_discrepancy": "Unresolved discrepancy",
}

STATUS_LABELS = {
    "pending_approval": "Pending approval",
    "open": "Open",
    "approved": "Approved",
    "rejected": "Rejected",
    "auto_cleared": "Auto-cleared",
}


class ApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def format_cad_cents(cents: int | None) -> str:
    """Format an integer cent amount as CAD dollars."""
    if cents is None:
        return "Not determined"
    sign = "-" if cents < 0 else ""
    whole, remainder = divmod(abs(int(cents)), 100)
    return f"{sign}${whole:,}.{remainder:02d}"


def format_unit_price_e4(value: int | None) -> str:
    """Format a unit price stored as CAD × 10,000."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    whole, remainder = divmod(abs(int(value)), 10_000)
    return f"{sign}${whole:,}.{remainder:04d}"


def label(mapping: dict[str, str], key: str | None) -> str:
    if key is None:
        return "—"
    return mapping.get(key, key.replace("_", " "))


class BillingClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def summary(self) -> dict:
        return self._request("GET", "/summary")

    def exceptions(self) -> dict:
        return self._request("GET", "/exceptions")

    def exception(self, exception_id: int) -> dict:
        return self._request("GET", f"/exceptions/{exception_id}")

    def audit(self, exception_id: int) -> dict:
        return self._request("GET", f"/exceptions/{exception_id}/audit")

    def approve(self, exception_id: int, reviewer: str, reason: str) -> dict:
        return self._request(
            "POST",
            f"/exceptions/{exception_id}/approve",
            {"reviewer": reviewer, "reason": reason},
        )

    def reject(self, exception_id: int, reviewer: str, reason: str) -> dict:
        return self._request(
            "POST",
            f"/exceptions/{exception_id}/reject",
            {"reviewer": reviewer, "reason": reason},
        )

    def analyze(self, exception_id: int) -> dict:
        return self._request("POST", f"/exceptions/{exception_id}/analyze", timeout=60.0)

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                json=body,
                timeout=self.timeout if timeout is None else timeout,
            )
        except httpx.RequestError as exc:
            raise ApiError(
                f"Cannot reach the billing API at {self.base_url}. "
                "Start it with: python scripts/serve.py"
            ) from exc
        if response.status_code >= 400:
            raise ApiError(_error_message(response), status_code=response.status_code)
        return response.json()


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or f"The API returned HTTP {response.status_code}"
    detail = payload.get("detail", payload)
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                parts.append(str(item.get("msg", item)))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return str(detail)
