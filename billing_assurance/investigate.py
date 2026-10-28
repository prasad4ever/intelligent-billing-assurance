"""Read-only investigation help for conflicting contract amendments.

The language model explains evidence a reviewer already has. It does not
select a rate, change amounts, or record an approval.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from billing_assurance.reconcile import AMBIGUOUS
from billing_assurance.review import ReviewError, get_exception

DEFAULT_MODEL = "gpt-4o-mini"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
RATE_STATEMENT = (
    "The correct contractual rate cannot be determined from the supplied evidence."
)
_LEAKED_LABELS = (
    "injected",
    "injected_scenario_labels",
    "testing_only_do_not_use_for_detection",
    "incorrect_usage_quantity",
    "incorrect_contractual_price",
    "missing_volume_discount",
    "duplicate_charge",
    "ambiguous_contract_amendment",
    "unresolved_discrepancy",
)
_RATE_KEYS = (
    "selected_rate",
    "selected_unit_price_e4",
    "authoritative_unit_price_e4",
    "chosen_amendment_number",
    "chosen_pricing_term_id",
    "recommended_amount_cents",
    "recommended_unit_price_e4",
)


class InvestigationError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def analyze_amendment(
    db_path: Path,
    exception_id: int,
    *,
    completer=None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """Return an explanation for one conflicting-amendment exception.

    ``completer`` is a test double. When it is omitted, the OpenAI API is
    called with ``OPENAI_API_KEY`` and ``OPENAI_MODEL``.
    """
    try:
        detail = get_exception(db_path, exception_id)
    except ReviewError:
        raise
    if detail.get("exception_type") != AMBIGUOUS or not detail.get("investigation"):
        raise InvestigationError(
            409,
            "AI analysis is only available for conflicting contract amendments",
        )
    packet = build_packet(detail)
    term_count = sum(1 for item in packet["evidence"] if item["kind"] == "contract_term")
    if term_count < 2:
        raise InvestigationError(
            409,
            "AI analysis needs at least two conflicting contract terms",
        )
    model_name = None
    if completer is None:
        key = _api_key(api_key)
        if not key:
            raise InvestigationError(
                503,
                "AI analysis is unavailable because OPENAI_API_KEY is not configured",
            )
        model_name = _model_name(model)
        completer = OpenAIChatClient(api_key=key, model=model_name)
    user_message = json.dumps(packet, sort_keys=True)
    _assert_safe_text(user_message)
    try:
        raw = completer(system_prompt(), user_message)
    except InvestigationError:
        raise
    except httpx.HTTPError as exc:
        raise InvestigationError(502, "The language model request failed") from exc
    parsed = parse_model_output(raw, {item["evidence_id"] for item in packet["evidence"]})
    cited = set(parsed["evidence_cited"])
    return {
        "exception_id": exception_id,
        "human_review_required": True,
        "authoritative_rate_selected": False,
        "rate_statement": RATE_STATEMENT,
        "explanation": parsed["explanation"],
        "uncertainty": parsed["uncertainty"],
        "recommended_steps": parsed["recommended_steps"],
        "evidence": [
            {**item, "cited": item["evidence_id"] in cited}
            for item in packet["evidence"]
        ],
        "evidence_cited": parsed["evidence_cited"],
        "model": model_name,
    }


def build_packet(detail: dict) -> dict:
    """Evidence sent to the model. Amounts stay in their stored integer units."""
    investigation = detail["investigation"]
    evidence = [
        {
            "evidence_id": "invoice",
            "kind": "invoice",
            "invoice_number": detail.get("invoice_number"),
            "customer_code": detail.get("customer_code"),
            "customer_name": detail.get("customer_name"),
            "billing_period_start": detail.get("billing_period_start"),
            "billing_period_end": detail.get("billing_period_end"),
            "charge_code": detail.get("charge_code"),
            "line_number": detail.get("line_number"),
            "description": detail.get("description"),
            "billed_quantity": detail.get("billed_quantity"),
            "billed_unit_price_e4": detail.get("billed_unit_price_e4"),
            "billed_amount_cents": detail.get("billed_amount_cents"),
            "currency": detail.get("currency"),
        },
        {
            "evidence_id": "usage",
            "kind": "usage",
            "meter_code": detail.get("charge_code"),
            "billing_period_start": detail.get("billing_period_start"),
            "billing_period_end": detail.get("billing_period_end"),
            "measured_quantity": investigation.get("measured_quantity"),
        },
    ]
    for term in investigation.get("conflicting_terms") or []:
        evidence.append(
            {
                "evidence_id": f"term-{term['pricing_term_id']}",
                "kind": "contract_term",
                "pricing_term_id": term["pricing_term_id"],
                "amendment_number": term["amendment_number"],
                "amendment_note": term.get("amendment_note") or "",
                "effective_start": term.get("effective_start"),
                "effective_end": term.get("effective_end"),
                "list_unit_price_e4": term.get("unit_price_e4"),
                "volume_threshold": term.get("volume_threshold"),
                "discounted_unit_price_e4": term.get("discounted_unit_price_e4"),
                "rate_if_this_term_alone_e4": term.get("rate_under_this_term_e4"),
            }
        )
    packet = {
        "task": (
            "These pricing terms cover the same billing period and do not agree. "
            "No contractual rate has been selected."
        ),
        "storage": {
            "currency": "CAD",
            "amounts": "integer cents",
            "unit_prices": "CAD multiplied by 10000",
        },
        "evidence": evidence,
    }
    _assert_safe_text(json.dumps(packet))
    return packet


def system_prompt() -> str:
    return (
        "You help a finance reviewer investigate conflicting contract amendments. "
        "Use only the evidence in the user message. Do not invent prices, dates, "
        "notes, documents, or quantities. Do not choose a contractual rate, a "
        "winning amendment, or a billing amount. "
        f"{RATE_STATEMENT} Human review is required. "
        "Respond with a JSON object and no other text, using these fields: "
        "explanation (string), uncertainty (string), recommended_steps "
        "(array of short strings), evidence_cited (array of evidence_id values "
        "copied from the evidence). "
        "explanation says why the amendment notes, prices, and effective dates conflict. "
        "uncertainty names missing or uncertain information and includes this sentence: "
        f"{RATE_STATEMENT} "
        "recommended_steps are questions or investigation steps for the reviewer. "
        "Do not add a selected rate or a recommended price."
    )


def parse_model_output(raw: str, allowed_ids: set[str]) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        raise InvestigationError(502, "The model returned an empty response")
    try:
        payload = json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        raise InvestigationError(502, "The model returned a response that is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise InvestigationError(502, "The model returned an unsupported response")
    _reject_rate_selection(payload)
    explanation = _text_field(payload.get("explanation"))
    uncertainty = _text_field(payload.get("uncertainty"))
    steps = _steps(payload.get("recommended_steps"))
    cited = _citations(payload.get("evidence_cited"), allowed_ids)
    if RATE_STATEMENT.lower() not in uncertainty.lower():
        uncertainty = f"{uncertainty.rstrip()} {RATE_STATEMENT}"
    return {
        "explanation": explanation,
        "uncertainty": uncertainty,
        "recommended_steps": steps,
        "evidence_cited": cited,
    }


class OpenAIChatClient:
    def __init__(self, api_key: str, model: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def __call__(self, system: str, user: str) -> str:
        try:
            response = httpx.post(
                OPENAI_CHAT_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise InvestigationError(502, "The language model request failed") from exc
        if response.status_code == 401:
            raise InvestigationError(502, "The language model rejected the API key")
        if response.status_code == 429:
            raise InvestigationError(502, "The language model request was rate limited")
        if response.status_code >= 400:
            raise InvestigationError(502, "The language model request failed")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise InvestigationError(
                502,
                "The language model returned an incomplete response",
            ) from exc
        if not isinstance(content, str):
            raise InvestigationError(502, "The language model returned an incomplete response")
        return content


def _api_key(explicit: str | None) -> str:
    if explicit is not None:
        return explicit.strip()
    return os.environ.get("OPENAI_API_KEY", "").strip()


def _model_name(explicit: str | None) -> str:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    configured = os.environ.get("OPENAI_MODEL", "").strip()
    return configured or DEFAULT_MODEL


def _assert_safe_text(text: str) -> None:
    lowered = text.lower()
    if any(label in lowered for label in _LEAKED_LABELS):
        raise InvestigationError(
            500,
            "The evidence packet was withheld because it contained testing-only labels",
        )


def _strip_fence(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _reject_rate_selection(payload: dict) -> None:
    for key in _RATE_KEYS:
        if payload.get(key) not in (None, "", [], {}):
            raise InvestigationError(
                502,
                "The model tried to select a contractual rate. That response was discarded",
            )
    if payload.get("rate_can_be_determined") is True or payload.get("authoritative_rate_selected") is True:
        raise InvestigationError(
            502,
            "The model tried to select a contractual rate. That response was discarded",
        )


def _text_field(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvestigationError(502, "The model returned an unsupported response")
    cleaned = value.strip()
    if len(cleaned) > 4_000:
        raise InvestigationError(502, "The model returned an unsupported response")
    return cleaned


def _steps(value: object) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 8:
        raise InvestigationError(502, "The model returned an unsupported response")
    steps = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 500:
            raise InvestigationError(502, "The model returned an unsupported response")
        steps.append(item.strip())
    return steps


def _citations(value: object, allowed_ids: set[str]) -> list[str]:
    if not isinstance(value, list) or not value:
        raise InvestigationError(502, "The model returned an unsupported response")
    cited: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in allowed_ids:
            raise InvestigationError(502, "The model cited evidence that was not supplied")
        if item not in cited:
            cited.append(item)
    return cited
