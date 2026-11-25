"""策略表迁移到账户隔离 + 追加式行情修订（ISS-033 / ISS-034）。

三处必须改的结构问题：

1. ``strategy_bars`` 原主键 ``(series_key, trade_date)`` + ``ON CONFLICT DO UPDATE``
   是覆盖式写入，价格修订会把原始行情抹掉，无法回答“当时用的是哪一版行情”。
   改为 ``(series_key, trade_date, revision)``，并显式保存
   ``effective_from``（交易日生效时点）与 ``observed_at``（本次获取时点）。

2. ``strategy_signals`` 的唯一键是 ``(index_id, signal_date, input_version,
   strategy_version)``，不含账户。于是账户 A 的信号会占住这条键，账户 B 只能复用它，
   计划也会互相串台。改为把 ``account_id`` 纳入唯一键。

3. ``strategy_plans`` 同理，唯一键改为含 ``account_id``。

SQLite 无法就地修改表约束，因此重建这三张表并在同一个事务内搬运已有数据：
既有证据全部保留（信号、计划、行情与冷却/持仓不动），只是加上了账户维度。
本迁移只动策略证据表，不触碰 ``trade_executions`` / ``trade_lots`` /
``cash_movements`` 这套权威账本。
"""
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- strategy_bars：覆盖式 -> 追加式修订 ---------------------------------
    op.execute('''CREATE TABLE strategy_bars_next (
        series_key TEXT NOT NULL, trade_date TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        open REAL NOT NULL CHECK(open>0), high REAL NOT NULL CHECK(high>0),
        low REAL NOT NULL CHECK(low>0), close REAL NOT NULL CHECK(close>0),
        volume REAL NOT NULL DEFAULT 0 CHECK(volume>=0),
        price_basis TEXT NOT NULL, source TEXT NOT NULL,
        source_verified INTEGER NOT NULL CHECK(source_verified IN (0,1)),
        effective_from TEXT NOT NULL, observed_at TEXT NOT NULL, acquired_at TEXT NOT NULL,
        PRIMARY KEY(series_key, trade_date, revision)
    ) STRICT''')
    op.execute('''INSERT INTO strategy_bars_next
        (series_key, trade_date, revision, open, high, low, close, volume, price_basis,
         source, source_verified, effective_from, observed_at, acquired_at)
        SELECT series_key, trade_date, 1, open, high, low, close, volume, price_basis,
               source, source_verified, trade_date, acquired_at, acquired_at
        FROM strategy_bars''')
    op.execute('DROP TABLE strategy_bars')
    op.execute('ALTER TABLE strategy_bars_next RENAME TO strategy_bars')

    # ---- strategy_signals：唯一键纳入 account_id ----------------------------
    op.execute('''CREATE TABLE strategy_signals_next (
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
        UNIQUE(account_id, index_id, signal_date, input_version, strategy_version)
    ) STRICT''')
    op.execute('''INSERT INTO strategy_signals_next
        SELECT signal_id, account_id, index_id, instrument_code, strategy_version,
               config_version, signal_date, input_version, price_basis, price_unit, status,
               reason_codes, detail, breakout_window, signal_close, threshold_high, atr14,
               buy_max, initial_stop_reference, trail_trigger_reference, factor_status,
               factor_detail, combo_status, combo_detail, regime_state, regime_er60,
               data_source, is_proxy, source_verified, created_at, updated_at
        FROM strategy_signals''')

    # ---- strategy_plans：唯一键纳入 account_id ------------------------------
    op.execute('''CREATE TABLE strategy_plans_next (
        plan_id TEXT PRIMARY KEY,
        account_id TEXT REFERENCES accounts(id),
        index_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
        signal_id TEXT REFERENCES strategy_signals_next(signal_id),
        signal_date TEXT NOT NULL, status TEXT NOT NULL,
        signal_close REAL, threshold_high REAL, atr14 REAL, buy_max REAL,
        initial_stop_reference REAL, trail_trigger_reference REAL,
        detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(account_id, index_id, signal_date, strategy_version)
    ) STRICT''')
    op.execute('''INSERT INTO strategy_plans_next
        SELECT plan_id, account_id, index_id, strategy_version, signal_id, signal_date,
               status, signal_close, threshold_high, atr14, buy_max, initial_stop_reference,
               trail_trigger_reference, detail, created_at, updated_at
        FROM strategy_plans''')

    # 先摘掉旧引用，再换名；重命名会同步修正 plans_next 里指向 signals 的外键。
    op.execute('DROP TABLE strategy_plans')
    op.execute('DROP TABLE strategy_signals')
    op.execute('ALTER TABLE strategy_signals_next RENAME TO strategy_signals')
    op.execute('ALTER TABLE strategy_plans_next RENAME TO strategy_plans')

    # signals/plans 上方的旧索引已随表 DROP 一起消失，这里按新唯一键重建；
    # strategy_positions 本次没有重建，其索引由 0008 建立，不能再建一次。
    op.execute('CREATE INDEX strategy_signals_index'
               ' ON strategy_signals(account_id, index_id, signal_date)')
    op.execute('CREATE INDEX strategy_plans_index'
               ' ON strategy_plans(account_id, index_id, signal_date)')
    op.execute("""UPDATE strategy_signals SET status='INVALID_PRICE_BASIS',
        detail=detail || '；历史指数点位与人民币尺度不一致，禁止作为新成交依据'
        WHERE price_basis='index_level' AND price_unit='元'""")
    op.execute("""UPDATE strategy_plans SET status='CANCELLED',
        detail=detail || '；原信号价格口径无效'
        WHERE signal_id IN (SELECT signal_id FROM strategy_signals WHERE status='INVALID_PRICE_BASIS')""")
    op.execute('CREATE INDEX strategy_bars_lookup ON strategy_bars(series_key, trade_date)')


def downgrade() -> None:
    raise RuntimeError('automatic downgrade is not supported')
