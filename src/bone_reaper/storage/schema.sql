-- Bone Reaper transactional store. Migration 1. Money and shares are NUMERIC, asset IDs are TEXT.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    version TEXT NOT NULL,
    yes_asset_id TEXT NOT NULL,
    no_asset_id TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    oracle_source TEXT NOT NULL,
    fee_version TEXT NOT NULL,
    start_utc_ms BIGINT NOT NULL,
    end_utc_ms BIGINT NOT NULL,
    status TEXT NOT NULL,
    CHECK (start_utc_ms < end_utc_ms),
    CHECK (yes_asset_id <> no_asset_id)
);

CREATE TABLE IF NOT EXISTS order_intents (
    intent_id TEXT PRIMARY KEY,
    wallet_id TEXT NOT NULL,
    created_utc_ms BIGINT NOT NULL,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    quantity NUMERIC NOT NULL CHECK (quantity > 0),
    price NUMERIC NOT NULL CHECK (price > 0 AND price < 1),
    strategy TEXT NOT NULL,
    purpose TEXT NOT NULL,
    risk_approval_id TEXT,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    exchange_order_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE REFERENCES order_intents (intent_id),
    remote_state TEXT NOT NULL,
    requested_shares NUMERIC NOT NULL,
    matched_shares NUMERIC NOT NULL,
    pending_cancel BOOLEAN NOT NULL,
    last_update_utc_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    trade_id TEXT NOT NULL,
    order_leg_key TEXT NOT NULL,
    intent_id TEXT,
    asset_id TEXT NOT NULL,
    executed_shares NUMERIC NOT NULL,
    net_shares NUMERIC NOT NULL,
    cash_delta NUMERIC NOT NULL,
    fee_cash NUMERIC NOT NULL,
    fee_shares NUMERIC NOT NULL,
    fee_asset TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    simulated BOOLEAN NOT NULL,
    ts_utc_ms BIGINT NOT NULL,
    PRIMARY KEY (trade_id, order_leg_key)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id TEXT PRIMARY KEY,
    cause_id TEXT NOT NULL,
    wallet_id TEXT NOT NULL,
    account TEXT NOT NULL,
    asset TEXT NOT NULL,
    debit NUMERIC NOT NULL CHECK (debit >= 0),
    credit NUMERIC NOT NULL CHECK (credit >= 0),
    ts_utc_ms BIGINT NOT NULL,
    settlement_status TEXT NOT NULL,
    reversal_of TEXT
);
CREATE INDEX IF NOT EXISTS ledger_entries_cause ON ledger_entries (cause_id);

CREATE TABLE IF NOT EXISTS cash_reservations (
    reservation_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES order_intents (intent_id),
    asset TEXT NOT NULL,
    amount NUMERIC NOT NULL CHECK (amount >= 0),
    state TEXT NOT NULL CHECK (state IN ('reserved', 'released'))
);

CREATE TABLE IF NOT EXISTS inventory_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    paired NUMERIC NOT NULL,
    unpaired_yes NUMERIC NOT NULL,
    unpaired_no NUMERIC NOT NULL,
    reserved_cash NUMERIC NOT NULL,
    valuation NUMERIC,
    ts_utc_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    risk_decision_id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    allowed BOOLEAN NOT NULL,
    reasons TEXT NOT NULL,
    limits_snapshot TEXT NOT NULL,
    ts_utc_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    run_id TEXT PRIMARY KEY,
    diffs TEXT NOT NULL,
    state TEXT NOT NULL,
    ts_utc_ms BIGINT NOT NULL,
    evidence_path TEXT
);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    winning_outcome TEXT NOT NULL,
    status TEXT NOT NULL,
    cash_released NUMERIC NOT NULL,
    confirmed_utc_ms BIGINT
);

CREATE TABLE IF NOT EXISTS run_manifests (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    sdk_version TEXT NOT NULL,
    dataset_hashes TEXT NOT NULL,
    started_utc_ms BIGINT NOT NULL,
    ended_utc_ms BIGINT
);

CREATE TABLE IF NOT EXISTS journal (
    seq BIGSERIAL PRIMARY KEY,
    wallet_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts_utc_ms BIGINT NOT NULL,
    body TEXT NOT NULL
);
