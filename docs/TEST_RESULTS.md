# Test results

Verification date: 28 October 2026. Command:

```powershell
python -m unittest discover -s tests -v
```

Result: **29 passed, 0 failed, 1 skipped**, in 17.5 seconds.

The skipped test is `LiveInvestigationTests.test_one_live_analysis_does_not_change_records` because `OPENAI_API_KEY` is not set. No live language-model call was made. No Docker image and no local model were used.

The run prints a Starlette deprecation warning that `httpx` should be replaced by `httpx2` for `TestClient`. The tests still pass with the installed `httpx`. That warning was left as-is.

## Suites

| Suite | What it checked | Result |
| --- | --- | --- |
| `test_data_integrity` | Fresh database validation, reproducibility, population counts, cent rounding, volume price, and term coverage. | Passed |
| `test_reconciliation` | Engine does not import the generator or reference the label file. Fixture rules, rerun, and preserved decisions. Full-file detection against the 36 testing labels with no misses and no extra flags. Source totals unchanged by reconciliation. | Passed |
| `test_api` | Summary, filters, approve, reject, blank reason, missing id, ambiguous block, audit rows, and preservation of decisions across another reconcile. | Passed |
| `test_dashboard` | Cent display, approve and reject through a temporary API, ambiguous block, Streamlit process health, and the original database left unchanged. | Passed |
| `test_investigation` | Mocked explanation, invalid JSON, invented evidence, rate-selection rejection, missing key, wrong exception type, and parser fences. | Passed; live call skipped |

## Working database

`PRAGMA integrity_check` returned `ok`. `PRAGMA foreign_key_check` returned no rows.

Customers, contracts, pricing terms, usage records, invoices, and invoice lines in `data/billing_assurance.sqlite` match a freshly generated database row for row. Reconciliation has not changed those amounts.

| Item | Value |
| --- | ---: |
| Customers / contracts | 60 / 60 |
| Pricing terms | 287 |
| Usage rows / quantity sum | 10,800 / 10,161,909 |
| Invoices / lines | 180 / 606 |
| Invoice total | 103,211,687 cents |
| Clean invoices | 144 |
| Exceptions | 36 |
| Pending approval | 30 |
| Open ambiguous | 6 |
| Approved or rejected | 0 |
| Pending exposure | 3,238,704 cents ($32,387.04) |
| Variance rows that do not equal invoiced minus expected | 0 |
| Audit actions | 1 initialize, 2 reconcile |

Detection on a copy of this file, which is what `test_detection_matches_testing_labels_without_false_flags` runs:

- 180 invoices and 606 lines checked.
- 36 discrepancies: usage 8, contractual price 8, missing discount 8, duplicate 6, ambiguous amendment 6.
- Every testing label matched the detected type.
- No clean invoice received an exception.
- A second reconcile inserted 36 rows again and did not duplicate keys.
- Invoice, line, usage, and pricing totals were unchanged.

`validate.py` still expects a just-initialized database (`billing_exceptions` = 0, `audit_log` = 1). Running it against the reconciled file will fail those two counts. That is the dataset-QA contract, not a reconciliation defect. The integrity tests build a temporary database for that check.

## Demonstrated behavior

- Calculated corrections stay in `pending_approval` until a reviewer acts.
- Ambiguous amendments stay open and cannot be approved.
- Approve and reject each create one audit row and refuse a second decision.
- The dashboard can record those decisions through the API without editing the original file during the test.
- AI analysis, under a mock, returns an explanation and does not change financial rows. Without an API key the service reports that analysis is unavailable.
