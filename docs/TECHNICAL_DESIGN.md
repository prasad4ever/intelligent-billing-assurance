# Technical design

## Money

Amounts are integer cents. Unit prices are CAD × 10,000 (`unit_price_e4`): $2,500.00 is 25,000,000 and $0.1000 is 1,000.

```text
amount_cents = round_half_up(quantity * unit_price_e4 / 100)
```

A fixed monthly fee uses quantity 1 and has no volume price. A per-unit charge uses the summed daily usage for that meter and month. If the monthly quantity meets the term’s threshold, the discounted unit price applies to the whole quantity. The schedule is not graduated. Thresholds in this dataset are 8,000 transactions and 40,000 API calls. The discount is 20 percent of the list unit price, rounded half up on the price scale.

Premium support is a $900 monthly fee for `customer_id` divisible by 3.

A term prices a month only when it covers the whole month. An empty `effective_end` means the term is open-ended. Partial months are not billed.

## Which term applies

Among terms that cover the month, the latest `effective_start` wins. If several terms share that start and produce the same rate at the measured quantity, any one of them is enough. If they produce different rates, no term is selected.

## Classification

Lines are grouped by invoice and charge code. The first matching rule wins.

1. **Duplicate charge.** The charge appears on more than one line. Expected amount is one contractual charge when a single rate exists. Variance is the extra billed amount. Status `pending_approval`.
2. **Ambiguous contract amendment.** No single rate covers the month. Expected amount and variance are null. Status `open`. The invoiced amount is still stored.
3. **Incorrect usage quantity.** Billed quantity differs from measured usage, and the billed unit price matches the contractual price at the true quantity.
4. **Missing volume discount.** Quantity matches usage, the threshold is met, and the invoice uses the list price instead of the discounted price.
5. **Incorrect contractual price.** Quantity matches, and the unit price or extended amount does not.
6. **Unresolved discrepancy.** Quantity and price both differ. Status `open`. This rule exists so a mixed disagreement is not forced into a single correction. The synthetic set does not contain one.

A line that matches produces no `billing_exceptions` row. The engine does not set `approved` or `rejected`.

## Reconciliation reruns

`reconcile()` fingerprints customers, contracts, invoices, lines, usage, and pricing terms and aborts if those totals change. It deletes exception rows whose `decided_at` and `decided_by` are both null, then inserts a finding only when that invoice, exception type, and line are not already stored. Approved and rejected rows therefore survive. Undecided rows are replaced, and their ids can change. Each run appends one audit row with actor `reconciliation-engine`.

The current file has been reconciled twice: one `initialize` audit row and two `reconcile` rows. No reviewer decision is stored.

## Review API

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/summary` | Invoice counts, exception counts, and pending exposure in cents. |
| GET | `/exceptions` | Optional `status` and `exception_type` filters. |
| GET | `/exceptions/{id}` | Invoice, line, amounts, evidence note, and, for an ambiguous case, every covering term. |
| GET | `/exceptions/{id}/audit` | Audit rows for that exception. |
| POST | `/exceptions/{id}/approve` | Body `{ "reviewer", "reason" }`. |
| POST | `/exceptions/{id}/reject` | Same body. |
| POST | `/exceptions/{id}/analyze` | Read-only AI explanation. |

Approve and reject require a non-blank reviewer and reason. They succeed only from `pending_approval`, with a non-null expected amount, and only when the type is not an ambiguous amendment. A second decision returns HTTP 409 and does not add another audit row. A successful decision writes one audit row and does not change source amounts.

Ambiguous detail sets `authoritative_rate_selected` to false and lists each term’s own rate. That per-term rate is not a selected price.

## Dashboard

Streamlit shows the summary, category and status counts, and an exception table. Selecting a row loads invoice evidence, the discrepancy note, conflicting terms when present, and audit history. Pending cases show Approve and Reject. Ambiguous cases show the conflict warning and **Analyze with AI**. Amounts are formatted from integer cents. Unit prices are formatted from the ×10,000 scale.

## AI investigation

`analyze_amendment()` runs only for an ambiguous exception that has at least two covering terms. The packet sent to the model contains the invoice line, the measured usage quantity, and each term’s note, dates, list price, threshold, discounted price, and rate if that term stood alone. It does not include the testing label file or the scenario code.

The model must return JSON with an explanation, an uncertainty statement, investigation steps, and evidence ids copied from that packet. The service rejects invalid JSON, a response that names a chosen rate or amount, and a citation that was not supplied. It always returns `human_review_required: true`, `authoritative_rate_selected: false`, and the sentence that the contractual rate cannot be determined. If `OPENAI_API_KEY` is unset, the response is HTTP 503 and no model is called.

The default model name is `gpt-4o-mini`, overridable with `OPENAI_MODEL`. No local model is installed. Mocked tests pass. The live test is skipped when the key is absent, which is the state of this workspace.

## Synthetic data limits

Faults were planted by the generator and listed outside SQLite so the engine cannot look up the answer. Near-perfect detection on this file shows that the rules match the generator. It does not show how the rules would behave with missing usage days, overlapping contracts, currency mixes, or amendment language from real negotiations.
