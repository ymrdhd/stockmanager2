"""Valuation inputs and independent opening lots; preserve existing facts."""
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = [
        "CREATE TABLE opening_positions (id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), instrument_id TEXT NOT NULL REFERENCES instruments(id), quantity INTEGER NOT NULL CHECK(quantity>0), cost_fen INTEGER NOT NULL CHECK(cost_fen>=0), fees_fen INTEGER NOT NULL CHECK(fees_fen>=0), occurred_at TEXT NOT NULL, note TEXT NOT NULL, UNIQUE(account_id,instrument_id)) STRICT",
        "CREATE TABLE new_lots (id TEXT PRIMARY KEY, open_execution_id TEXT UNIQUE REFERENCES trade_executions(id), strategy_id TEXT, lot_role TEXT NOT NULL CHECK(lot_role IN ('ordinary','main','sub')), signal_date TEXT, frozen_atr INTEGER, opened_qty INTEGER NOT NULL CHECK(opened_qty>0), remaining_qty INTEGER NOT NULL CHECK(remaining_qty BETWEEN 0 AND opened_qty), remaining_cost_fen INTEGER NOT NULL CHECK(remaining_cost_fen>=0), remaining_fees_fen INTEGER NOT NULL CHECK(remaining_fees_fen>=0), status TEXT NOT NULL CHECK(status IN ('open','closed','reversed')), opening_id TEXT UNIQUE REFERENCES opening_positions(id), CHECK((open_execution_id IS NULL)!=(opening_id IS NULL))) STRICT",
        "INSERT INTO new_lots SELECT *,NULL FROM trade_lots ORDER BY rowid",
        "CREATE TABLE new_allocations (id TEXT PRIMARY KEY, sell_execution_id TEXT NOT NULL REFERENCES trade_executions(id), lot_id TEXT NOT NULL REFERENCES new_lots(id), quantity INTEGER NOT NULL CHECK(quantity>0), cost_fen INTEGER NOT NULL CHECK(cost_fen>=0), buy_fees_fen INTEGER NOT NULL CHECK(buy_fees_fen>=0), method TEXT NOT NULL CHECK(method IN ('fifo','specified')), UNIQUE(sell_execution_id,lot_id)) STRICT",
        "INSERT INTO new_allocations SELECT * FROM lot_allocations ORDER BY rowid",
        "DROP TABLE lot_allocations", "DROP TABLE trade_lots",
        "ALTER TABLE new_lots RENAME TO trade_lots", "ALTER TABLE new_allocations RENAME TO lot_allocations",
        "CREATE TABLE new_cash (id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), execution_id TEXT REFERENCES trade_executions(id), transfer_id TEXT, amount_fen INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('trade','reversal','opening','deposit','withdrawal','adjustment','dividend')), occurred_at TEXT NOT NULL, reversal_of TEXT UNIQUE REFERENCES new_cash(id), request_id TEXT UNIQUE, note TEXT NOT NULL) STRICT",
        "INSERT INTO new_cash SELECT * FROM cash_movements ORDER BY rowid", "DROP TABLE cash_movements", "ALTER TABLE new_cash RENAME TO cash_movements",
        "CREATE UNIQUE INDEX cash_trade_once ON cash_movements(execution_id) WHERE kind='trade'",
        "CREATE TABLE company_events (id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), instrument_id TEXT NOT NULL REFERENCES instruments(id), kind TEXT NOT NULL CHECK(kind IN ('dividend','split','merge','rights','other')), occurred_at TEXT NOT NULL, cash_movement_id TEXT UNIQUE REFERENCES cash_movements(id), note TEXT NOT NULL, CHECK((kind='dividend')=(cash_movement_id IS NOT NULL))) STRICT",
        "CREATE TABLE quotes (id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL REFERENCES instruments(id), price INTEGER NOT NULL CHECK(price>0), source TEXT NOT NULL, observed_at TEXT NOT NULL, trading_day TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('close','intraday')), quality TEXT NOT NULL CHECK(quality IN ('valid','stale','missing','conflict','derived_unverified')), quote_revision INTEGER NOT NULL UNIQUE) STRICT",
        "CREATE TABLE quote_state (id INTEGER PRIMARY KEY CHECK(id=1), quote_revision INTEGER NOT NULL CHECK(quote_revision>=0)) STRICT",
        "INSERT INTO quote_state VALUES (1,0)",
    ]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError('automatic downgrade is not supported')
