"""Additive financial ledger migration; monetary values are integer kopecks."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS finance_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS finance_transactions(
 payment_id TEXT PRIMARY KEY, status TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>=0),
 currency TEXT NOT NULL, fee INTEGER, refunded INTEGER NOT NULL DEFAULT 0,
 donor TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', method TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, captured_at TEXT, updated_at TEXT NOT NULL, source TEXT NOT NULL,
 is_test INTEGER NOT NULL DEFAULT 0 CHECK(is_test IN (0,1)));
CREATE INDEX IF NOT EXISTS finance_tx_date ON finance_transactions(captured_at, status);
CREATE INDEX IF NOT EXISTS finance_tx_created ON finance_transactions(created_at);
CREATE INDEX IF NOT EXISTS finance_tx_effective_date ON finance_transactions(COALESCE(captured_at,created_at),status);
CREATE TABLE IF NOT EXISTS finance_refunds(
 refund_id TEXT PRIMARY KEY, payment_id TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>=0),
 currency TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS finance_refund_payment ON finance_refunds(payment_id);
CREATE TABLE IF NOT EXISTS finance_bank_imports(
 id INTEGER PRIMARY KEY, digest TEXT NOT NULL UNIQUE, filename TEXT NOT NULL, source TEXT NOT NULL,
 account TEXT NOT NULL, rows_added INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS finance_bank_operations(
 id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, external_id TEXT NOT NULL DEFAULT '',
 booked_at TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0), currency TEXT NOT NULL,
 source TEXT NOT NULL, account TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 kind TEXT NOT NULL DEFAULT 'unclassified' CHECK(kind IN ('unclassified','settlement','income','excluded')),
 import_id INTEGER REFERENCES finance_bank_imports(id), created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS finance_bank_date ON finance_bank_operations(booked_at,kind);
CREATE TABLE IF NOT EXISTS finance_payouts(
 id INTEGER PRIMARY KEY, external_id TEXT NOT NULL UNIQUE, expected_at TEXT NOT NULL,
 amount INTEGER NOT NULL CHECK(amount>0), fee INTEGER CHECK(fee>=0), currency TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'expected' CHECK(status IN ('expected','confirmed','canceled')),
 bank_id INTEGER UNIQUE REFERENCES finance_bank_operations(id), credited_at TEXT,
 source TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS finance_payout_date ON finance_payouts(expected_at,status);
CREATE TABLE IF NOT EXISTS finance_reconciliation(
 payment_id TEXT PRIMARY KEY REFERENCES finance_transactions(payment_id),
 payout_id INTEGER NOT NULL REFERENCES finance_payouts(id), amount INTEGER NOT NULL CHECK(amount>=0),
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS finance_reconciliation_payout ON finance_reconciliation(payout_id);
CREATE TABLE IF NOT EXISTS finance_receipts(
 income_key TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','issued','verified','refund','excluded')),
 customer_category TEXT NOT NULL DEFAULT 'individual' CHECK(customer_category IN ('individual','business','unknown')),
 receipt_date TEXT NOT NULL DEFAULT '', receipt_number TEXT NOT NULL DEFAULT '',
 receipt_url TEXT NOT NULL DEFAULT '', comment TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS finance_audit_log(
 id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL,
 before_json TEXT NOT NULL, after_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS finance_audit_entity ON finance_audit_log(entity,id);
CREATE TABLE IF NOT EXISTS finance_cache(key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS finance_sync(
 id INTEGER PRIMARY KEY CHECK(id=1), requested INTEGER NOT NULL DEFAULT 0,
 lease_until INTEGER NOT NULL DEFAULT 0, token TEXT NOT NULL DEFAULT '',
 stage TEXT NOT NULL DEFAULT 'payments', cursor TEXT NOT NULL DEFAULT '',
 since_at TEXT NOT NULL DEFAULT '', last_success TEXT NOT NULL DEFAULT '',
 last_attempt TEXT NOT NULL DEFAULT '', retry_at INTEGER NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT '');
INSERT OR IGNORE INTO finance_sync(id) VALUES(1);
"""


def migrate(db):
    db.executescript(SCHEMA)
    with db:
        db.execute("BEGIN IMMEDIATE")
        if "is_test" not in {
            r["name"] for r in db.execute("PRAGMA table_info(finance_transactions)")
        }:
            db.execute(
                "ALTER TABLE finance_transactions ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0 CHECK(is_test IN (0,1))"
            )
        if not db.execute("SELECT 1 FROM finance_meta WHERE key='schema_1'").fetchone():
            from .ledger import mirror_payment

            for row in db.execute("SELECT * FROM payments"):
                mirror_payment(db, dict(row))
            db.execute("INSERT INTO finance_meta VALUES('schema_1','1')")


TABLES = (
    "finance_meta",
    "finance_transactions",
    "finance_refunds",
    "finance_bank_imports",
    "finance_bank_operations",
    "finance_payouts",
    "finance_reconciliation",
    "finance_receipts",
    "finance_audit_log",
)
