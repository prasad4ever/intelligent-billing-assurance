# Business case

## Problem

In order-to-cash, a draft invoice should be checked against the contract and the usage that was actually measured before it is approved. For a supplier of software subscriptions and transaction processing, the usual disagreements are a wrong quantity, a wrong unit price, a missed volume discount, a duplicated fee, or two amendments that both claim to set the price.

Those cases are expensive when they are found after the customer is billed, and they are unsafe to “correct” automatically when the contract itself is unclear.

## What this application solves

On a controlled synthetic book it:

- Accepts a draft invoice with no exception row when every line matches the contract and the usage.
- Calculates billed amount, expected amount, and the difference when one contractual rate is clear.
- Holds that difference for a named reviewer instead of posting it.
- Leaves an amendment conflict open, shows both terms, and refuses approval of an unknown adjustment.
- Keeps an audit trail of engine runs and reviewer decisions.

The demonstrated book is 60 fictional institutions, January–March 2026, Canadian dollars, no tax. It is not a production billing run and it is not tied to a named client.

## In scope

- Draft invoices only.
- Four charges: core platform fee, transaction processing, API access, and premium support.
- Monthly periods that a pricing term covers in full.
- All-units volume discount once a monthly quantity meets one threshold.
- Human approve and reject for a calculated correction.
- A read-only explanation of conflicting amendments.

## Out of scope

- Tax, credits, mid-month proration, and multi-currency.
- Posting an approved correction to an invoicing or ERP system.
- Customer collections, cash application, or dispute case management.
- Identity, authorization, and multi-tenant hosting.
- Training or hosting a custom model.
- Treating synthetic detection accuracy as a production performance claim.

## Outcome on this dataset

After reconciliation the working database contains:

- 180 draft invoices and 606 lines.
- 144 invoices with no exception.
- 36 exceptions, one for each planted disagreement.
- 30 exceptions in `pending_approval`, with a combined billed-minus-expected exposure of 3,238,704 cents ($32,387.04).
- 6 ambiguous amendments left `open`, with no expected amount and no exposure in that total.

Exposure by pending category:

| Category | Cases | Exposure |
| --- | ---: | ---: |
| Duplicate charge | 6 | $27,000.00 |
| Incorrect contractual price | 8 | $2,800.00 |
| Missing volume discount | 8 | $2,224.04 |
| Incorrect usage quantity | 8 | $363.00 |

The automated comparison with the testing labels, run on a copy of this database, missed none of the 36 faults and flagged no clean invoice. That result checks the implementation. It does not estimate how the same rules would perform on messy production extracts.

## Why a person still decides

A calculated difference is a proposal. Approve records that a reviewer accepted it. Reject records that they did not. Neither action changes the invoice total. When two amendments disagree, there is no honest expected amount to approve, so the case stays open.
