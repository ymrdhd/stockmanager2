"""Trading facts, lot allocations and cash ledger."""
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = [
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind='securities'), currency TEXT NOT NULL CHECK(currency='CNY'), balance_mode TEXT NOT NULL CHECK(balance_mode='ledger'), status TEXT NOT NULL CHECK(status IN ('active','closed')))",
        "CREATE TABLE instruments (id TEXT PRIMARY KEY, code TEXT NOT NULL, market TEXT NOT NULL CHECK(market IN ('SH','SZ','BJ')), currency TEXT NOT NULL CHECK(currency='CNY'), price_scale INTEGER NOT NULL CHECK(price_scale=4), kind TEXT NOT NULL CHECK(kind IN ('stock','etf')), UNIQUE(market,code))",
        """CREATE TABLE trade_executions (id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, account_id TEXT NOT NULL REFERENCES accounts(id), instrument_id TEXT NOT NULL REFERENCES instruments(id), side TEXT NOT NULL CHECK(side IN ('buy','sell')), quantity INTEGER NOT NULL CHECK(quantity>0), price INTEGER NOT NULL CHECK(price>0), fees_fen INTEGER CHECK(fees_fen>=0), amount_fen INTEGER NOT NULL CHECK(amount_fen>=0), executed_at TEXT NOT NULL, trading_day TEXT NOT NULL, external_id TEXT, external_id_owner TEXT REFERENCES trade_executions(id), status TEXT NOT NULL CHECK(status IN ('effective','pending','reversed')), correction_of TEXT REFERENCES trade_executions(id), note TEXT NOT NULL, strategy_id TEXT, lot_role TEXT NOT NULL CHECK(lot_role IN ('ordinary','main','sub')), signal_date TEXT, frozen_atr INTEGER CHECK(frozen_atr>0), pending_reason TEXT, payload TEXT NOT NULL)""",
        "CREATE UNIQUE INDEX trade_external ON trade_executions(account_id,external_id) WHERE external_id IS NOT NULL",
        "CREATE TABLE trade_lots (id TEXT PRIMARY KEY, open_execution_id TEXT NOT NULL UNIQUE REFERENCES trade_executions(id), strategy_id TEXT, lot_role TEXT NOT NULL CHECK(lot_role IN ('ordinary','main','sub')), signal_date TEXT, frozen_atr INTEGER, opened_qty INTEGER NOT NULL CHECK(opened_qty>0), remaining_qty INTEGER NOT NULL CHECK(remaining_qty BETWEEN 0 AND opened_qty), remaining_cost_fen INTEGER NOT NULL CHECK(remaining_cost_fen>=0), remaining_fees_fen INTEGER NOT NULL CHECK(remaining_fees_fen>=0), status TEXT NOT NULL CHECK(status IN ('open','closed','reversed')))",
        "CREATE TABLE lot_allocations (id TEXT PRIMARY KEY, sell_execution_id TEXT NOT NULL REFERENCES trade_executions(id), lot_id TEXT NOT NULL REFERENCES trade_lots(id), quantity INTEGER NOT NULL CHECK(quantity>0), cost_fen INTEGER NOT NULL CHECK(cost_fen>=0), buy_fees_fen INTEGER NOT NULL CHECK(buy_fees_fen>=0), method TEXT NOT NULL CHECK(method IN ('fifo','specified')), UNIQUE(sell_execution_id,lot_id))",
        "CREATE TABLE cash_movements (id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), execution_id TEXT REFERENCES trade_executions(id), transfer_id TEXT, amount_fen INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('trade','reversal','opening','deposit','withdrawal','adjustment')), occurred_at TEXT NOT NULL, reversal_of TEXT UNIQUE REFERENCES cash_movements(id), request_id TEXT UNIQUE, note TEXT NOT NULL)",
        "CREATE UNIQUE INDEX cash_trade_once ON cash_movements(execution_id) WHERE kind='trade'",
        "CREATE TABLE audit_events (id TEXT PRIMARY KEY, entity TEXT NOT NULL, entity_id TEXT NOT NULL, event TEXT NOT NULL, request_id TEXT NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL, details TEXT NOT NULL)",
        "CREATE TABLE ledger_state (id INTEGER PRIMARY KEY CHECK(id=1), ledger_revision INTEGER NOT NULL CHECK(ledger_revision>=0), earliest_affected_day TEXT)",
        "INSERT INTO ledger_state VALUES (1,0,NULL)",
        "CREATE TABLE ledger_requests (request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, result_id TEXT NOT NULL, result_json TEXT NOT NULL)",
    ]
    for statement in statements:
        op.execute(statement + " STRICT" if statement.startswith("CREATE TABLE") else statement)


def downgrade() -> None:
    raise RuntimeError('automatic downgrade is not supported')
