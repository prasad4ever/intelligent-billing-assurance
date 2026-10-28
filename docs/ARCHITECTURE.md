# Architecture

The prototype is one Python package, one SQLite file, and two local processes. Money is decided by rules. A language model, when configured, only explains conflicting amendments.

## Components

| Piece | Role |
| --- | --- |
| `billing_assurance/generate.py` | Builds the synthetic dataset and writes testing labels outside the database. |
| `billing_assurance/validate.py` | Checks a freshly initialized database. It may read the testing labels. Detection does not. |
| `billing_assurance/money.py` | Shared cent and unit-price arithmetic. |
| `billing_assurance/reconcile.py` | Classifies draft lines and writes `billing_exceptions`. |
| `billing_assurance/review.py` | Summary, evidence, approve, reject, and audit reads. |
| `billing_assurance/investigate.py` | Read-only explanation of an ambiguous amendment. |
| `billing_assurance/api.py` | FastAPI routes over review and investigation. |
| `streamlit_app.py` | Dashboard. It calls the API and does not reimplement pricing. |
| `evaluation/injected_scenario_labels.json` | Testing-only answer key. Access policy: `TESTING_ONLY_DO_NOT_USE_FOR_DETECTION`. |

`scripts/init_db.py`, `scripts/reconcile.py`, and `scripts/serve.py` are the Windows entry points.

## Data flow

1. Dataset generation writes operational rows to `data/billing_assurance.sqlite` and writes the known-fault list to `evaluation/injected_scenario_labels.json`.
2. Reconciliation reads customers, contracts, pricing terms, usage, and draft invoices. It inserts or replaces undecided exception rows and appends one `reconcile` audit event. It does not import the generator or open the label file.
3. The API reads those rows. Approve and reject update only the exception status, decision fields, and audit log, and only when a correction amount already exists.
4. Streamlit calls `/summary`, `/exceptions`, `/exceptions/{id}`, `/exceptions/{id}/audit`, and the approve, reject, and analyze routes.
5. Analysis builds a small evidence packet from the exception already stored, sends it to the chat API, and returns text. It does not write to SQLite.

## Database

Eight tables. Currency on contracts and invoices is constrained to CAD. Invoice status is constrained to `draft`.

- `customers` — 60 institutions. `customer_code` is unique.
- `contracts` — one row per customer. `customer_id` references `customers`.
- `pricing_terms` — charge, model, prices, effective dates, amendment number, and amendment note. `contract_id` references `contracts`.
- `usage_records` — one daily quantity per contract and meter (`TXN_PROCESSING`, `API_ACCESS`).
- `invoices` — one draft per customer per month. `customer_id` and `contract_id` must refer to the same customer.
- `invoice_lines` — quantity, unit price, and extended amount. `invoice_id` references `invoices`.
- `billing_exceptions` — one finding for a charge on an invoice. Optional `invoice_line_id`. Variance, when present, must equal invoiced minus expected.
- `audit_log` — initialize, reconcile, approve, and reject events.

Lookup indexes cover contract terms, usage dates, invoice periods, exception status, and audit entity.

Exception status values are `open`, `auto_cleared`, `pending_approval`, `approved`, and `rejected`. This dataset uses `open` for unresolved amendments and `pending_approval` for calculated corrections. Clean lines do not get a row. The engine never writes `approved` or `rejected`.

## Technology choices

SQLite keeps the prototype in one file with explicit integer money and foreign keys. FastAPI is a thin HTTP boundary so the dashboard cannot drift from the decision rules. Streamlit is the review screen for a demonstration. Shared functions in `money.py` keep generation and reconciliation on the same rounding rule.

A rules engine prices the invoice. The synthetic faults are known in advance, so a trained model would mostly learn the generator. The language-model call is limited to prose about amendments that the rules have already refused to price.

## Trade-offs

- No authentication and no separate environment configuration beyond `BILLING_API_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL`. That matches a local interview prototype and would be insufficient for a shared service.
- The working database is updated in place. Rerunning reconciliation deletes undecided exceptions and inserts them again, so undecided ids can change. Rows with both `decided_at` and `decided_by` are kept.
- `validate.py` expects zero exception rows and one audit row, which is the state immediately after initialization. After reconciliation those counts are different on purpose. Dataset QA and detection QA are separate tests.
- The chat integration uses the OpenAI HTTP API through `httpx`. No model is bundled, and no vector store is built.
