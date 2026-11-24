"""Explicit account scope history and manually reconciled transfer gaps."""
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('''CREATE TABLE account_scope_events (
        id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
        included INTEGER NOT NULL CHECK(included IN (0,1)), occurred_at TEXT NOT NULL,
        reason TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('scope','mistaken_account'))
    ) STRICT''')
    op.execute('''CREATE TABLE transfer_reconciliation_gaps (
        id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
        transfer_id TEXT NOT NULL REFERENCES finance_transfers(id),
        from_at TEXT NOT NULL, until_at TEXT NOT NULL,
        balance_id TEXT NOT NULL REFERENCES balance_adjustments(id), CHECK(from_at<=until_at)
    ) STRICT''')


def downgrade() -> None:
    raise RuntimeError('automatic downgrade is not supported')
