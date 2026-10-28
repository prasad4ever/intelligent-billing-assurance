# Interview guide

## Explain it in plain English

A supplier bills financial institutions for a platform fee, transaction processing, API access, and, for some customers, premium support. Before those draft invoices are treated as final, this prototype checks each line against the contract and the usage already recorded for that month.

If the line matches, nothing is queued for review. If the quantity, price, discount, or a repeated fee is wrong, and the contract supports one number, the app calculates the difference and waits for a person to approve or reject it. If two amendments start on the same day and name different prices, the app shows both and will not invent the “right” price.

Approving a case writes an audit record. It does not change the invoice and it does not send a correction to another billing system.

The customers, contracts, usage, and invoices are synthetic. The faults were planted on purpose so the rules could be tested. A separate file lists those faults. The detection program is not allowed to read that file.

## Walkthrough that matches the code

1. `python scripts\init_db.py` builds 60 customers, 180 draft invoices for January–March 2026, and writes the answer key under `evaluation\`.
2. `python scripts\reconcile.py` classifies the drafts. On the current file that produces 36 exceptions and leaves 144 invoices clean.
3. `python scripts\serve.py` and `streamlit run streamlit_app.py` open the review screen.
4. A pending row can be approved or rejected with a name and a reason. An ambiguous row cannot.
5. **Analyze with AI** asks a chat model to explain the conflicting notes. That step is tested with stand-in responses. A real model call has not been run here.

Exposure waiting for approval is $32,387.04. Most of that is six duplicated platform fees. The six amendment conflicts contribute nothing to the total because no expected amount exists.

## Questions a principal architect is likely to ask

**Why are the money decisions rules instead of a model?**  
The correct amount is defined by the contract, the measured quantity, and a rounding rule. Those inputs are already structured. A model would add a way to be wrong about cents. The synthetic faults were created by the same rules, so a trained model would look accurate without proving it understands a new book of business.

**Why is the answer key outside the database?**  
If the detector could read `evaluation/injected_scenario_labels.json`, the test would show that it can look up the label, not that it can price an invoice. The engine is checked so that it neither imports the generator nor mentions that file.

**Is 36 out of 36 a production accuracy claim?**  
No. It is an implementation check against faults this generator planted. Production usage has gaps, restatements, and amendment language this file does not contain. The honest next measurement is a time-based or customer-based holdout of real invoices, scored by reviewers, with the generator labels kept out of training.

**Why not auto-approve a clean invoice or a small variance?**  
A clean line is simply not given an exception. That is an acceptance by absence of a finding, not a posted approval. A calculated difference stays in `pending_approval` because someone accountable has to accept the financial effect. The audit row stores who did that and why.

**What happens when the contract is ambiguous?**  
Two March transaction amendments share an effective date and different prices. The engine stores both, leaves expected amount null, and the API returns 409 for approve and reject. The UI says a person has to investigate. The optional model may describe the conflict. It is not allowed to return a chosen rate, and a response that tries is discarded.

**How do you stop a rerun from wiping a decision?**  
Undecided exception rows are deleted and recalculated. A row is kept when both `decided_at` and `decided_by` are set. Inserts are skipped when that invoice, type, and line already exist. Tests approve a case, reconcile again, and require the same decision and the same source totals.

**What does the AI feature actually guarantee?**  
It can only be requested for an ambiguous amendment. The packet is the invoice line, the measured quantity, and the stored terms. The service refuses to save anything. It refuses citations that were not in the packet and refuses a selected price. Missing credentials produce an explicit unavailable message. Those guarantees are covered by mocked tests. Live inference is unverified.

**Why SQLite, FastAPI, and Streamlit?**  
One file is enough to demonstrate relational integrity and integer money. The API is the only place that applies a decision, so the screen cannot drift from the rules. Streamlit is sufficient for a local review demonstration. None of these choices is a production operating model: there is no auth, no job runner, and no separation of the demo database from a system of record.

**What would you add after this prototype, and what would you still refuse to automate?**  
Next would be real usage extracts, a reviewer feedback set, and monitoring of exception rates by charge and customer. I would still refuse to let a model pick a contractual rate or post a credit. I would also keep amendment conflicts in a human queue until the company has an explicit rule for which document wins.

**What are the main limits you would volunteer?**  
Synthetic near-perfect detection, no tax or proration, no external posting, no authentication, undecided exception ids can change on rerun, and the language-model path has not been exercised against a live endpoint.
