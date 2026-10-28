"""SQLite schema for draft-invoice validation.

``billing_exceptions`` is created for later workflow use and is empty after
dataset initialization. Injected scenario labels are not stored here.
"""

SCHEMA_SQL = """
PRAGMA user_version = 1;

CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    customer_code TEXT NOT NULL UNIQUE,
    customer_name TEXT NOT NULL UNIQUE,
    institution_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    created_at TEXT NOT NULL
);

CREATE TABLE contracts (
    contract_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (customer_id),
    contract_number TEXT NOT NULL UNIQUE,
    currency_code TEXT NOT NULL CHECK (currency_code = 'CAD'),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'expired', 'pending')),
    created_at TEXT NOT NULL,
    CHECK (end_date >= start_date)
);

CREATE TABLE pricing_terms (
    pricing_term_id INTEGER PRIMARY KEY,
    contract_id INTEGER NOT NULL REFERENCES contracts (contract_id),
    charge_code TEXT NOT NULL CHECK (
        charge_code IN (
            'CORE_PLATFORM',
            'TXN_PROCESSING',
            'API_ACCESS',
            'PREMIUM_SUPPORT'
        )
    ),
    charge_name TEXT NOT NULL,
    charge_model TEXT NOT NULL CHECK (charge_model IN ('fixed_monthly', 'per_unit')),
    unit_price_e4 INTEGER NOT NULL CHECK (unit_price_e4 > 0),
    volume_threshold INTEGER CHECK (volume_threshold IS NULL OR volume_threshold > 0),
    discounted_unit_price_e4 INTEGER,
    effective_start TEXT NOT NULL,
    effective_end TEXT,
    amendment_number INTEGER NOT NULL CHECK (amendment_number >= 0),
    amendment_note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (
        (volume_threshold IS NULL AND discounted_unit_price_e4 IS NULL)
        OR (
            volume_threshold IS NOT NULL
            AND discounted_unit_price_e4 IS NOT NULL
            AND discounted_unit_price_e4 < unit_price_e4
        )
    ),
    CHECK (effective_end IS NULL OR effective_end >= effective_start),
    CHECK (
        charge_model != 'fixed_monthly'
        OR (volume_threshold IS NULL AND discounted_unit_price_e4 IS NULL)
    )
);

CREATE TABLE usage_records (
    usage_record_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (customer_id),
    contract_id INTEGER NOT NULL REFERENCES contracts (contract_id),
    usage_date TEXT NOT NULL,
    meter_code TEXT NOT NULL CHECK (meter_code IN ('TXN_PROCESSING', 'API_ACCESS')),
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    aggregation_level TEXT NOT NULL CHECK (aggregation_level = 'daily'),
    created_at TEXT NOT NULL,
    UNIQUE (contract_id, meter_code, usage_date)
);

CREATE TABLE invoices (
    invoice_id INTEGER PRIMARY KEY,
    invoice_number TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers (customer_id),
    contract_id INTEGER NOT NULL REFERENCES contracts (contract_id),
    billing_period_start TEXT NOT NULL,
    billing_period_end TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'draft'),
    currency_code TEXT NOT NULL CHECK (currency_code = 'CAD'),
    total_amount_cents INTEGER NOT NULL CHECK (total_amount_cents >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (customer_id, billing_period_start),
    CHECK (billing_period_end >= billing_period_start)
);

CREATE TABLE invoice_lines (
    invoice_line_id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices (invoice_id),
    line_number INTEGER NOT NULL CHECK (line_number > 0),
    charge_code TEXT NOT NULL CHECK (
        charge_code IN (
            'CORE_PLATFORM',
            'TXN_PROCESSING',
            'API_ACCESS',
            'PREMIUM_SUPPORT'
        )
    ),
    description TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_e4 INTEGER NOT NULL CHECK (unit_price_e4 >= 0),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (invoice_id, line_number)
);

CREATE TABLE billing_exceptions (
    exception_id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices (invoice_id),
    invoice_line_id INTEGER REFERENCES invoice_lines (invoice_line_id),
    exception_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'open',
            'auto_cleared',
            'pending_approval',
            'approved',
            'rejected'
        )
    ),
    expected_amount_cents INTEGER,
    invoiced_amount_cents INTEGER,
    variance_cents INTEGER,
    evidence_note TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_note TEXT,
    CHECK (
        variance_cents IS NULL
        OR (
            expected_amount_cents IS NOT NULL
            AND invoiced_amount_cents IS NOT NULL
            AND variance_cents = invoiced_amount_cents - expected_amount_cents
        )
    )
);

CREATE TABLE audit_log (
    audit_id INTEGER PRIMARY KEY,
    event_time TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    detail TEXT NOT NULL
);

CREATE INDEX idx_contracts_customer ON contracts (customer_id);
CREATE INDEX idx_terms_lookup
    ON pricing_terms (contract_id, charge_code, effective_start);
CREATE INDEX idx_usage_lookup
    ON usage_records (contract_id, meter_code, usage_date);
CREATE INDEX idx_invoices_customer_period
    ON invoices (customer_id, billing_period_start);
CREATE INDEX idx_invoice_lines_invoice ON invoice_lines (invoice_id);
CREATE INDEX idx_exceptions_status ON billing_exceptions (status, invoice_id);
CREATE INDEX idx_audit_entity ON audit_log (entity_type, entity_id);
"""
