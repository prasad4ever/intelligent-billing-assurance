# Intelligent Billing Assurance

Prototype for checking draft invoices against contract pricing and measured usage before a person approves a financial correction.

The population is synthetic: 60 financial institutions, one contract each, billed in CAD for January–March 2026. The application finds draft-invoice discrepancies, leaves a clean invoice alone, and records a human decision when a correction amount can be calculated. It does not post that decision to an external billing system.

## Data Confidentiality & Project Disclaimer

This repository presents an independent proof-of-concept implementation of enterprise Order-to-Cash (O2C) billing assurance and intelligent exception automation.

All customer information, contractual agreements, pricing structures, transaction volumes, and billing records used in this implementation are synthetic and intended exclusively for demonstration and technical evaluation.

The business scenarios are representative of financial technology industry workflows. No actual client data, proprietary business information, or confidential organizational materials are included.

The implementation demonstrates selected architectural patterns and business capabilities rather than a complete production enterprise system.

## What it does

- Stores customers, contracts, pricing terms, daily usage, and draft invoices in SQLite.
- Reconciles every draft line with deterministic pricing rules.
- Opens a review case only when a line disagrees with the contract or the usage.
- Lets a reviewer approve or reject a proposed correction, with an audit record.
- Explains conflicting contract amendments with an optional language-model call. That call is read-only.

## What it does not do

- It does not calculate tax or mid-month proration.
- It does not train a machine-learning model.
- It does not choose a price when two amendments conflict.
- It does not change invoice, contract, or usage amounts.
- It is not deployed, authenticated, or connected to a live billing platform.

## Stack

Python 3.13, SQLite, FastAPI, Streamlit, and an optional OpenAI-compatible chat call. See `docs/ARCHITECTURE.md` for why these pieces were chosen.

## Run on Windows

From the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\init_db.py
python scripts\reconcile.py
python scripts\serve.py
```

In a second terminal, with the same virtual environment:

```powershell
streamlit run streamlit_app.py
```

The API listens on http://127.0.0.1:8000. The dashboard uses that address unless `BILLING_API_URL` is set. Interactive API docs are at http://127.0.0.1:8000/docs.

A fresh clone does not include `data\billing_assurance.sqlite`. `python scripts\init_db.py` creates that database and the testing label file. Running it again replaces the database. Reconcile after each initialization. `python -m billing_assurance` also rebuilds the database.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The reconciliation test compares detections with `evaluation\injected_scenario_labels.json` after the engine runs. The engine does not read that file. Do not use it as an input to detection.

## Optional AI investigation

The dashboard button **Analyze with AI** calls `POST /exceptions/{id}/analyze` for an ambiguous amendment only. Set the key in the terminal that starts the API:

```powershell
$env:OPENAI_API_KEY = "your-key"
$env:OPENAI_MODEL = "gpt-4o-mini"
python scripts\serve.py
```

`OPENAI_MODEL` is optional. The default is `gpt-4o-mini`. If the key is missing, the API says analysis is unavailable. It does not invent a substitute explanation. Mocked tests cover the safeguards. A live model call has not been verified in this environment.

## Further reading

- `docs/BUSINESS_CASE.md` — problem, scope, and outcome on the synthetic set
- `docs/ARCHITECTURE.md` — components and data flow
- `docs/TECHNICAL_DESIGN.md` — pricing rules, classification, review, and AI limits
- `docs/TEST_RESULTS.md` — the verification run for this folder
- `docs/INTERVIEW_GUIDE.md` — plain-English walkthrough and architecture questions
