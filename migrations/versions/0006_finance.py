"""Expand shared accounts and add auditable personal finance facts."""
import re
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # The effective-record projection assumes an original and its reversal
    # cancel at the original effective instant. Refuse inconsistent legacy
    # reversal evidence instead of silently changing the historical balance.
    malformed = conn.exec_driver_sql("""SELECT r.id FROM cash_movements r
        LEFT JOIN cash_movements m ON m.id=r.reversal_of
        WHERE (r.kind='reversal' AND (m.id IS NULL OR m.kind='reversal' OR
            r.account_id!=m.account_id OR r.amount_fen!=-m.amount_fen OR
            r.occurred_at!=m.occurred_at OR r.execution_id IS NOT m.execution_id OR
            r.transfer_id IS NOT m.transfer_id)) OR
            (r.kind!='reversal' AND r.reversal_of IS NOT NULL) LIMIT 1""").fetchone()
    if malformed:
        raise RuntimeError('legacy cash reversal evidence is inconsistent; reconcile before migration')
    # Rebuild the complete dependent graph, rather than dropping a referenced
    # parent or disabling foreign keys. Retain original row order and indexes.
    children = ('trade_executions', 'opening_positions', 'trade_lots',
                'lot_allocations', 'cash_movements', 'company_events')
    tables = ('accounts', *children)
    schemas = {name: conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).scalar_one() for name in children}
    indexes = [sql for (sql,) in conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
        "AND tbl_name IN ('accounts','trade_executions','opening_positions','trade_lots','lot_allocations','cash_movements','company_events') ORDER BY name"
    )]
    op.execute('''CREATE TABLE finance_new_accounts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('securities','bank','cash','payment','other_asset','credit_card','other_liability')),
        currency TEXT NOT NULL CHECK(currency='CNY'),
        balance_mode TEXT NOT NULL CHECK((kind='securities' AND balance_mode='ledger') OR (kind!='securities' AND balance_mode='manual_snapshot')),
        status TEXT NOT NULL CHECK(status IN ('active','closed')),
        asset_class TEXT NOT NULL CHECK(asset_class=CASE WHEN kind IN ('credit_card','other_liability') THEN 'liability' ELSE 'asset' END),
        included INTEGER NOT NULL CHECK(included IN (0,1)), effective_at TEXT
    ) STRICT''')
    op.execute("INSERT INTO finance_new_accounts SELECT *, 'asset', 1, NULL FROM accounts ORDER BY rowid")
    for name in children:
        sql = re.sub(r'^CREATE TABLE "?'+name+r'"?', 'CREATE TABLE finance_new_'+name, schemas[name])
        for target in tables:
            sql = re.sub(r'REFERENCES "?'+target+r'"?\s*\(', 'REFERENCES finance_new_'+target+'(', sql)
        op.execute(sql)
        op.execute(f'INSERT INTO finance_new_{name} SELECT * FROM {name} ORDER BY rowid')
    for name in reversed(tables):
        op.execute(f'DROP TABLE {name}')
    for name in tables:
        op.execute(f'ALTER TABLE finance_new_{name} RENAME TO {name}')
    for sql in indexes:
        op.execute(sql)
    op.execute('''CREATE TABLE finance_categories (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('income','expense','fee')),
        UNIQUE(kind,name)) STRICT''')
    for id_, name, kind in [('salary','工资','income'),('other_income','其他收入','income'),('food','餐饮','expense'),('living','生活','expense'),('other_expense','其他支出','expense'),('fees','手续费','fee')]:
        conn.exec_driver_sql('INSERT INTO finance_categories VALUES (?,?,?)', (id_,name,kind))
    op.execute('''CREATE TABLE finance_transactions (
        id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), kind TEXT NOT NULL CHECK(kind IN ('income','expense','fee')),
        amount_fen INTEGER NOT NULL CHECK(amount_fen>0), category_id TEXT NOT NULL REFERENCES finance_categories(id), occurred_at TEXT NOT NULL,
        note TEXT NOT NULL, correction_of TEXT UNIQUE REFERENCES finance_transactions(id), status TEXT NOT NULL CHECK(status IN ('effective','reversed'))) STRICT''')
    op.execute('''CREATE TABLE finance_transfers (
        id TEXT PRIMARY KEY, from_account_id TEXT NOT NULL REFERENCES accounts(id), to_account_id TEXT NOT NULL REFERENCES accounts(id),
        amount_fen INTEGER NOT NULL CHECK(amount_fen>0), occurred_at TEXT NOT NULL, note TEXT NOT NULL,
        from_included INTEGER CHECK(from_included IN (0,1)), to_included INTEGER CHECK(to_included IN (0,1)),
        from_balance_id TEXT REFERENCES balance_adjustments(id), to_balance_id TEXT REFERENCES balance_adjustments(id),
        correction_of TEXT UNIQUE REFERENCES finance_transfers(id),
        status TEXT NOT NULL CHECK(status IN ('effective','reversed')), CHECK(from_account_id!=to_account_id)) STRICT''')
    op.execute('''CREATE TABLE balance_adjustments (
        id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), before_fen INTEGER, after_fen INTEGER NOT NULL CHECK(after_fen>=0),
        occurred_at TEXT NOT NULL, reason TEXT NOT NULL, previous_id TEXT REFERENCES balance_adjustments(id),
        transfer_id TEXT REFERENCES finance_transfers(id), status TEXT NOT NULL CHECK(status IN ('effective','reversed'))) STRICT''')
    op.execute('CREATE INDEX balance_history ON balance_adjustments(account_id,occurred_at)')
    op.execute('CREATE INDEX finance_month ON finance_transactions(occurred_at,status)')
    # For restated histories remove both non-trade originals and reversal
    # entries. Trade reversals are kept in the underlying audit ledger too.
    op.execute('''CREATE VIEW effective_cash_movements AS SELECT m.* FROM cash_movements m
        WHERE m.kind!='reversal' AND NOT EXISTS (SELECT 1 FROM cash_movements r WHERE r.reversal_of=m.id)''')


def downgrade() -> None:
    raise RuntimeError('automatic downgrade is not supported')
