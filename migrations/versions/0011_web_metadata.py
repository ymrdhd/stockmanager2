"""Sourced security display metadata and explicit original-expense refund links."""
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    # Preserve instrument identity and existing six-column inserts in old revisions.
    op.execute('ALTER TABLE instruments ADD COLUMN name TEXT')
    op.execute('ALTER TABLE instruments ADD COLUMN name_source TEXT')
    op.execute('ALTER TABLE instruments ADD COLUMN name_observed_at TEXT')
    op.execute('''CREATE TABLE finance_transactions_next (
        id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
        kind TEXT NOT NULL CHECK(kind IN ('income','expense','fee','refund')),
        amount_fen INTEGER NOT NULL CHECK(amount_fen>0), category_id TEXT NOT NULL REFERENCES finance_categories(id),
        occurred_at TEXT NOT NULL, note TEXT NOT NULL,
        correction_of TEXT UNIQUE REFERENCES finance_transactions_next(id),
        status TEXT NOT NULL CHECK(status IN ('effective','reversed')),
        related_transaction_id TEXT REFERENCES finance_transactions_next(id),
        CHECK((kind='refund' AND related_transaction_id IS NOT NULL) OR
              (kind!='refund' AND related_transaction_id IS NULL))) STRICT''')
    op.execute('''INSERT INTO finance_transactions_next
        SELECT id,account_id,kind,amount_fen,category_id,occurred_at,note,correction_of,status,NULL
        FROM finance_transactions ORDER BY rowid''')
    op.execute('DROP TABLE finance_transactions')
    op.execute('ALTER TABLE finance_transactions_next RENAME TO finance_transactions')
    op.execute('CREATE INDEX finance_month ON finance_transactions(occurred_at,status)')
    op.execute('CREATE INDEX finance_refund_original ON finance_transactions(related_transaction_id,status)')


def downgrade():
    raise RuntimeError('automatic downgrade is not supported')
