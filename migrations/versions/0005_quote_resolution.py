"""Append-only confirmation of conflicting closing price evidence."""
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('''CREATE TABLE quote_resolutions (
        id TEXT PRIMARY KEY,
        instrument_id TEXT NOT NULL REFERENCES instruments(id),
        trading_day TEXT NOT NULL,
        selected_quote_id TEXT NOT NULL REFERENCES quotes(id),
        reviewed_quote_ids TEXT NOT NULL,
        reason TEXT NOT NULL CHECK(length(reason)>0),
        resolved_at TEXT NOT NULL,
        quote_revision INTEGER NOT NULL UNIQUE CHECK(quote_revision>0)
    ) STRICT''')
    op.execute('CREATE INDEX quote_resolutions_lookup ON quote_resolutions(instrument_id,trading_day,quote_revision)')


def downgrade() -> None:
    raise RuntimeError('automatic downgrade is not supported')
