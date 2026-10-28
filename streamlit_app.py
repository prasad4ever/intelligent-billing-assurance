"""Review dashboard for Intelligent Billing Assurance.

Start the API first, then run:

    streamlit run streamlit_app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from billing_assurance.dashboard_client import (
    CATEGORY_LABELS,
    STATUS_LABELS,
    ApiError,
    BillingClient,
    format_cad_cents,
    format_unit_price_e4,
    label,
)

CATEGORY_ORDER = (
    "incorrect_usage_quantity",
    "incorrect_contractual_price",
    "missing_volume_discount",
    "duplicate_charge",
    "ambiguous_contract_amendment",
    "unresolved_discrepancy",
)
STATUS_ORDER = ("pending_approval", "open", "approved", "rejected", "auto_cleared")


def main() -> None:
    st.set_page_config(page_title="Billing Assurance", layout="wide")
    st.title("Intelligent Billing Assurance")
    st.caption("Draft invoice review. Amounts are CAD. Extended amounts are shown in dollars and cents.")

    base_url = st.sidebar.text_input(
        "API base URL",
        value=os.environ.get("BILLING_API_URL", "http://127.0.0.1:8000"),
    )
    client = BillingClient(base_url)

    try:
        totals = client.summary()
        listing = client.exceptions()
    except ApiError as exc:
        st.error(str(exc))
        st.stop()

    _metrics(totals)
    _count_tables(totals)
    exceptions = listing.get("exceptions", [])
    st.subheader("Exceptions")
    if not exceptions:
        st.info("No billing exceptions are open. Clean invoices were accepted without a review record.")
        st.stop()

    _exception_table(exceptions)
    selected_id = _selector(exceptions)
    if selected_id is None:
        return
    try:
        detail = client.exception(selected_id)
        history = client.audit(selected_id)
    except ApiError as exc:
        st.error(str(exc))
        return

    _detail(client, detail, history)


def _metrics(totals: dict) -> None:
    invoices, clean, exceptions, exposure = st.columns(4)
    invoices.metric("Invoices analyzed", f"{totals.get('invoices', 0):,}")
    clean.metric("Clean invoices", f"{totals.get('clean_invoices', 0):,}")
    exceptions.metric("Exceptions detected", f"{totals.get('exceptions', 0):,}")
    exposure.metric(
        "Potential financial exposure",
        format_cad_cents(totals.get("pending_variance_cents")),
    )
    st.caption(
        "Exposure is the sum of billed minus expected on corrections waiting for approval. "
        "Ambiguous amendments are excluded because no single amount was determined. "
        "Clean invoices have no exception record."
    )


def _count_tables(totals: dict) -> None:
    category, status = st.columns(2)
    with category:
        st.subheader("By category")
        st.dataframe(
            _ordered_counts(totals.get("by_type", {}), CATEGORY_ORDER, CATEGORY_LABELS),
            hide_index=True,
            width="stretch",
        )
    with status:
        st.subheader("By status")
        st.dataframe(
            _ordered_counts(totals.get("by_status", {}), STATUS_ORDER, STATUS_LABELS),
            hide_index=True,
            width="stretch",
        )


def _ordered_counts(counts: dict, order: tuple[str, ...], names: dict[str, str]) -> list[dict]:
    rows = []
    seen = set()
    for key in order:
        if key in counts:
            rows.append({"Name": label(names, key), "Count": counts[key]})
            seen.add(key)
    for key, count in counts.items():
        if key not in seen:
            rows.append({"Name": label(names, key), "Count": count})
    return rows


def _exception_table(exceptions: list[dict]) -> None:
    st.dataframe(
        [
            {
                "Exception ID": item["exception_id"],
                "Customer": item.get("customer_code") or "—",
                "Category": label(CATEGORY_LABELS, item.get("exception_type")),
                "Status": label(STATUS_LABELS, item.get("status")),
                "Billed amount": format_cad_cents(item.get("billed_amount_cents")),
                "Expected amount": format_cad_cents(item.get("expected_amount_cents")),
                "Financial difference": format_cad_cents(item.get("difference_cents")),
            }
            for item in exceptions
        ],
        hide_index=True,
        width="stretch",
    )


def _selector(exceptions: list[dict]) -> int | None:
    options = [item["exception_id"] for item in exceptions]
    labels = {item["exception_id"]: _option_label(item) for item in exceptions}
    if st.session_state.get("selected_exception_id") not in labels:
        st.session_state["selected_exception_id"] = options[0]

    def _format(exception_id: int) -> str:
        return labels.get(exception_id, str(exception_id))

    return st.selectbox(
        "Select an exception",
        options,
        format_func=_format,
        key="selected_exception_id",
    )


def _option_label(item: dict) -> str:
    return (
        f"{item['exception_id']} · {item.get('customer_code') or '—'} · "
        f"{label(CATEGORY_LABELS, item.get('exception_type'))} · "
        f"{label(STATUS_LABELS, item.get('status'))}"
    )


def _detail(client: BillingClient, detail: dict, history: dict) -> None:
    st.subheader(f"Exception {detail['exception_id']}")
    st.write(
        f"**{label(CATEGORY_LABELS, detail.get('exception_type'))}** · "
        f"{label(STATUS_LABELS, detail.get('status'))}"
    )

    invoice, money = st.columns(2)
    with invoice:
        st.markdown("**Invoice**")
        customer = detail.get("customer_name") or "—"
        code = detail.get("customer_code") or "—"
        st.write(f"{customer} ({code})")
        st.write(f"Invoice {detail.get('invoice_number') or '—'}")
        st.write(
            f"Period {detail.get('billing_period_start') or '—'} to "
            f"{detail.get('billing_period_end') or '—'}"
        )
        st.write(f"Charge {detail.get('charge_code') or '—'}")
        st.write(f"Line {detail.get('line_number') or '—'}: {detail.get('description') or '—'}")
        st.write(
            "Billed quantity: "
            f"{detail.get('billed_quantity') if detail.get('billed_quantity') is not None else '—'}"
        )
        st.write(f"Billed unit price: {format_unit_price_e4(detail.get('billed_unit_price_e4'))}")
    with money:
        st.markdown("**Amounts**")
        st.write(f"Billed amount: {format_cad_cents(detail.get('billed_amount_cents'))}")
        st.write(f"Expected amount: {format_cad_cents(detail.get('expected_amount_cents'))}")
        st.write(f"Financial difference: {format_cad_cents(detail.get('difference_cents'))}")

    st.markdown("**Discrepancy explanation**")
    st.write(detail.get("evidence_note") or "No explanation was returned.")

    investigation = detail.get("investigation")
    if investigation is not None:
        _investigation(client, detail)
    elif detail.get("financial_correction_allowed"):
        _decision_form(client, detail)
    elif detail.get("decision"):
        decision = detail["decision"]
        st.info(
            f"{label(STATUS_LABELS, decision.get('status'))} by {decision.get('reviewer') or '—'} "
            f"at {decision.get('decided_at') or '—'}. Reason: {decision.get('reason') or '—'}"
        )

    _audit(history)


def _investigation(client: BillingClient, detail: dict) -> None:
    investigation = detail["investigation"]
    st.warning(
        "Human investigation is required. These contract terms cover the same billing period "
        "and no authoritative price was selected. This adjustment cannot be approved."
    )
    st.write(f"Measured quantity used for comparison: {investigation.get('measured_quantity')}")
    terms = investigation.get("conflicting_terms") or []
    if not terms:
        st.write("No covering contract terms were returned.")
        return
    st.markdown("**Conflicting contract terms**")
    st.dataframe(
        [
            {
                "Term ID": term["pricing_term_id"],
                "Amendment": term["amendment_number"],
                "Effective start": term["effective_start"],
                "Effective end": term.get("effective_end") or "Open",
                "List unit price": format_unit_price_e4(term.get("unit_price_e4")),
                "Volume threshold": term.get("volume_threshold") if term.get("volume_threshold") is not None else "—",
                "Discounted unit price": format_unit_price_e4(term.get("discounted_unit_price_e4")),
                "Rate under this term": format_unit_price_e4(term.get("rate_under_this_term_e4")),
                "Amendment note": term.get("amendment_note") or "",
            }
            for term in terms
        ],
        hide_index=True,
        width="stretch",
    )
    _ai_analysis(client, detail)


def _ai_analysis(client: BillingClient, detail: dict) -> None:
    st.markdown("**AI investigation**")
    st.caption(
        "The explanation uses the evidence above. It does not select a rate or change this exception."
    )
    state_key = f"ai_analysis_{detail['exception_id']}"
    if st.button("Analyze with AI", key=f"analyze_{detail['exception_id']}"):
        try:
            st.session_state[state_key] = client.analyze(detail["exception_id"])
        except ApiError as exc:
            st.session_state.pop(state_key, None)
            st.error(str(exc))
            return
    result = st.session_state.get(state_key)
    if not result:
        return
    st.warning(
        f"{result.get('rate_statement') or 'The correct contractual rate cannot be determined.'} "
        "Human review is required."
    )
    st.markdown("**AI explanation**")
    st.write(result.get("explanation") or "No explanation was returned.")
    st.markdown("**Uncertainty**")
    st.write(result.get("uncertainty") or "No uncertainty statement was returned.")
    st.markdown("**Recommended investigation steps**")
    steps = result.get("recommended_steps") or []
    if steps:
        for step in steps:
            st.write(f"- {step}")
    else:
        st.write("No investigation steps were returned.")
    st.markdown("**Evidence considered**")
    for item in result.get("evidence") or []:
        marker = "Cited" if item.get("cited") else "Supplied"
        st.write(f"{marker}. {_evidence_line(item)}")


def _evidence_line(item: dict) -> str:
    kind = item.get("kind")
    if kind == "invoice":
        return (
            f"Invoice {item.get('invoice_number') or '—'}, "
            f"{item.get('customer_name') or '—'} ({item.get('customer_code') or '—'}), "
            f"period {item.get('billing_period_start') or '—'} to {item.get('billing_period_end') or '—'}, "
            f"charge {item.get('charge_code') or '—'}, "
            f"line {item.get('line_number') or '—'}: {item.get('description') or '—'}, "
            f"billed quantity {item.get('billed_quantity') if item.get('billed_quantity') is not None else '—'}, "
            f"billed unit price {format_unit_price_e4(item.get('billed_unit_price_e4'))}, "
            f"billed amount {format_cad_cents(item.get('billed_amount_cents'))}."
        )
    if kind == "usage":
        return (
            f"Measured usage {item.get('measured_quantity') if item.get('measured_quantity') is not None else '—'} "
            f"for {item.get('meter_code') or '—'} "
            f"from {item.get('billing_period_start') or '—'} to {item.get('billing_period_end') or '—'}."
        )
    return (
        f"Amendment {item.get('amendment_number') if item.get('amendment_number') is not None else '—'} "
        f"(term {item.get('pricing_term_id') or '—'}) "
        f"effective {item.get('effective_start') or '—'} to {item.get('effective_end') or 'open'}. "
        f"List unit price {format_unit_price_e4(item.get('list_unit_price_e4'))}. "
        f"Volume threshold {item.get('volume_threshold') if item.get('volume_threshold') is not None else '—'}. "
        f"Discounted unit price {format_unit_price_e4(item.get('discounted_unit_price_e4'))}. "
        f"Rate if this term stood alone {format_unit_price_e4(item.get('rate_if_this_term_alone_e4'))}. "
        f"Note: {item.get('amendment_note') or '—'}."
    )


def _decision_form(client: BillingClient, detail: dict) -> None:
    st.markdown("**Review decision**")
    st.caption("Approval records the decision in the audit log. It does not post a change to an external billing system.")
    with st.form("review_decision"):
        reviewer = st.text_input("Reviewer name")
        reason = st.text_area("Reason")
        approve = st.form_submit_button("Approve correction")
        reject = st.form_submit_button("Reject correction")
    if not approve and not reject:
        return
    if not reviewer.strip() or not reason.strip():
        st.error("Reviewer name and reason are required.")
        return
    action = client.approve if approve else client.reject
    try:
        action(detail["exception_id"], reviewer, reason)
    except ApiError as exc:
        st.error(str(exc))
        return
    st.success("Decision recorded.")
    st.rerun()


def _audit(history: dict) -> None:
    st.markdown("**Audit history**")
    entries = history.get("entries") or []
    if not entries:
        st.write("No review decisions have been recorded for this exception.")
        return
    st.dataframe(
        [
            {
                "When": entry.get("event_time"),
                "Reviewer": entry.get("actor"),
                "Action": entry.get("action"),
                "Reason": (entry.get("detail") or {}).get("reason")
                if isinstance(entry.get("detail"), dict)
                else "",
            }
            for entry in entries
        ],
        hide_index=True,
        width="stretch",
    )


main()
