"""Persist V9/G2 strategy evidence without creating a second holdings ledger.

策略状态（信号、计划、持仓推进、冷却、NDX 辅助仓）是**衍生证据**：它们记录
"模型算出了什么、依据哪份行情、哪一版规则"，永远不替代 ``trade_executions`` /
``trade_lots`` / ``cash_movements`` 这套权威账本。真实成交只能经 TradeService
写入，因此本迁移只新增可追溯的证据表与行情缓存表，不改任何既有表结构。
"""
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 行情缓存：指数点位与 ETF 日线分开存，价格口径一起落库，禁止跨口径顶替。
    op.execute('''CREATE TABLE strategy_bars (
        series_key TEXT NOT NULL, trade_date TEXT NOT NULL,
        open REAL NOT NULL CHECK(open>0), high REAL NOT NULL CHECK(high>0),
        low REAL NOT NULL CHECK(low>0), close REAL NOT NULL CHECK(close>0),
        volume REAL NOT NULL DEFAULT 0 CHECK(volume>=0),
        price_basis TEXT NOT NULL, source TEXT NOT NULL,
        source_verified INTEGER NOT NULL CHECK(source_verified IN (0,1)),
        acquired_at TEXT NOT NULL,
        PRIMARY KEY(series_key, trade_date)
    ) STRICT''')

    # 正式信号：同一 (指数, 信号日, 输入版本, 规则版本) 只允许一条，刷新幂等。
    op.execute('''CREATE TABLE strategy_signals (
        signal_id TEXT PRIMARY KEY,
        account_id TEXT REFERENCES accounts(id),
        index_id TEXT NOT NULL, instrument_code TEXT,
        strategy_version TEXT NOT NULL, config_version TEXT NOT NULL,
        signal_date TEXT NOT NULL, input_version TEXT NOT NULL,
        price_basis TEXT NOT NULL, price_unit TEXT NOT NULL CHECK(price_unit IN ('元','点')),
        status TEXT NOT NULL, reason_codes TEXT NOT NULL DEFAULT '[]', detail TEXT NOT NULL DEFAULT '',
        breakout_window INTEGER, signal_close REAL, threshold_high REAL,
        atr14 REAL CHECK(atr14 IS NULL OR atr14>0), buy_max REAL CHECK(buy_max IS NULL OR buy_max>0),
        initial_stop_reference REAL, trail_trigger_reference REAL,
        factor_status TEXT, factor_detail TEXT, combo_status TEXT, combo_detail TEXT,
        regime_state TEXT, regime_er60 REAL,
        data_source TEXT, is_proxy INTEGER NOT NULL DEFAULT 0 CHECK(is_proxy IN (0,1)),
        source_verified INTEGER NOT NULL DEFAULT 1 CHECK(source_verified IN (0,1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(index_id, signal_date, input_version, strategy_version)
    ) STRICT''')

    # 待执行计划：价格条件通过后进入 AWAITING_CONFIRMATION，绝不自动建仓。
    op.execute('''CREATE TABLE strategy_plans (
        plan_id TEXT PRIMARY KEY,
        account_id TEXT REFERENCES accounts(id),
        index_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
        signal_id TEXT REFERENCES strategy_signals(signal_id),
        signal_date TEXT NOT NULL, status TEXT NOT NULL,
        signal_close REAL, threshold_high REAL, atr14 REAL, buy_max REAL,
        initial_stop_reference REAL, trail_trigger_reference REAL,
        detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(index_id, signal_date, strategy_version)
    ) STRICT''')

    # 策略持仓推进状态：state_json 保存 PositionState 完整快照，字段列用于查询/对账。
    op.execute('''CREATE TABLE strategy_positions (
        position_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        index_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
        signal_date TEXT NOT NULL, entry_date TEXT NOT NULL,
        entry_price REAL NOT NULL, signal_atr REAL NOT NULL CHECK(signal_atr>0),
        initial_stop REAL, stop_price REAL, trail_trigger REAL,
        stage INTEGER NOT NULL DEFAULT 1, highest_price REAL, current_atr REAL,
        holding_days INTEGER NOT NULL DEFAULT 0, exit_after_bars INTEGER NOT NULL DEFAULT 21,
        renewals INTEGER NOT NULL DEFAULT 0, e2a_extended_days INTEGER NOT NULL DEFAULT 0,
        below_ma20_streak INTEGER NOT NULL DEFAULT 0, extended INTEGER NOT NULL DEFAULT 0,
        add_executed INTEGER NOT NULL DEFAULT 0, add_filled_date TEXT,
        exit_filled INTEGER NOT NULL DEFAULT 0, closed_date TEXT,
        last_trade_date TEXT, last_exit_date TEXT, last_exit_action TEXT, last_exit_reason TEXT,
        pending_stop REAL, pending_stop_effective_date TEXT,
        pending_action TEXT, pending_action_date TEXT,
        main_lot_id TEXT REFERENCES trade_lots(id),
        shares INTEGER, entry_executed INTEGER NOT NULL DEFAULT 0,
        execution_source TEXT NOT NULL DEFAULT 'legacy_unverified',
        state_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(account_id, index_id, signal_date)
    ) STRICT''')

    op.execute('''CREATE TABLE strategy_cooldowns (
        account_id TEXT NOT NULL REFERENCES accounts(id), index_id TEXT NOT NULL,
        exit_date TEXT NOT NULL, exit_action TEXT NOT NULL,
        exit_reason TEXT NOT NULL DEFAULT '', required_days INTEGER NOT NULL,
        cooldown_until TEXT, regime TEXT NOT NULL DEFAULT '', extended INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL, PRIMARY KEY(account_id, index_id)
    ) STRICT''')

    op.execute('''CREATE TABLE strategy_ndx_aux (
        account_id TEXT PRIMARY KEY REFERENCES accounts(id),
        ratio REAL NOT NULL DEFAULT 0, last_add_date TEXT, updated_at TEXT NOT NULL
    ) STRICT''')

    op.execute('CREATE INDEX strategy_signals_index ON strategy_signals(index_id, signal_date)')
    op.execute('CREATE INDEX strategy_positions_index ON strategy_positions(account_id, index_id)')


def downgrade() -> None:
    raise RuntimeError('automatic downgrade is not supported')
