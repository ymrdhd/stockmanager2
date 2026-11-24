"""Preserve existing quote evidence and separate effective/acquisition times."""
from datetime import date, datetime

from alembic import op

from backend.application.market_calendar import CalendarUnavailable, quote_time
from backend.application.trading.service import utc

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('ALTER TABLE quotes ADD COLUMN effective_at TEXT')
    # Legacy observations do not establish when the service obtained data.
    # Never invent an acquisition timestamp for strategy replay.
    op.execute('ALTER TABLE quotes ADD COLUMN acquired_at TEXT')
    # The 0003 quotes table already represents actual security prices, not
    # research indices or adjusted series. Keep that contract explicit.
    op.execute("ALTER TABLE quotes ADD COLUMN price_basis TEXT NOT NULL DEFAULT 'unadjusted_security' CHECK(price_basis='unadjusted_security')")
    conn = op.get_bind()
    for row in conn.exec_driver_sql('SELECT id,trading_day,observed_at,kind FROM quotes').fetchall():
        id_, day, observed, kind = row
        try:
            observed_at = datetime.fromisoformat(observed)
            if observed_at.utcoffset() is None:
                continue
            effective = utc(quote_time(date.fromisoformat(day), observed_at, kind))
        except (ValueError, CalendarUnavailable):
            continue
        conn.exec_driver_sql('UPDATE quotes SET effective_at=? WHERE id=?', (effective, id_))
    op.execute('CREATE INDEX quotes_effective ON quotes(instrument_id,trading_day,effective_at,quote_revision)')


def downgrade() -> None:
    raise RuntimeError('automatic downgrade is not supported')
