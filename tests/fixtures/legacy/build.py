"""Build representative Stock Manager 1.0 SQLite fixtures from the real legacy DDL.

The schemas and key names below are copied from the read-only legacy source tree
(``artifacts/tmp/reference/subroutine/db.py`` for the stock database and
``artifacts/tmp/reference/subroutine/finance/migrations.py`` for the finance
database).  The fixture is generated on demand so no binary ``.db`` is committed.

Usage::

    python tests/fixtures/legacy/build.py [OUTPUT_DIR]

Both databases are synthetic.  They intentionally contain the messy real cases a
migration must not paper over: a trade without fees, a trade without a price, an
"estimated" fee, a merged summary row, a manual balance that already includes the
bill spending, and a legacy ``STOCK_ASSET`` account that duplicates the securities
equity.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

STOCK_DDL = (
    "CREATE TABLE IF NOT EXISTS kv_store(key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS daily_snapshot(
        date TEXT PRIMARY KEY,
        total_holdings_value REAL,
        cash_balance REAL,
        total_buy_amount REAL,
        total_sell_amount REAL,
        net_buy_amount REAL,
        total_traded_amount REAL)""",
    """CREATE TABLE IF NOT EXISTS capital_history(
        date TEXT PRIMARY KEY,
        total_capital REAL,
        transfer_in REAL DEFAULT 0.0,
        transfer_out REAL DEFAULT 0.0,
        source TEXT DEFAULT 'manual')""",
    """CREATE TABLE IF NOT EXISTS daily_stock_snapshot(
        date TEXT NOT NULL,
        code TEXT NOT NULL,
        opening_quantity REAL NOT NULL DEFAULT 0,
        prev_close REAL NOT NULL DEFAULT 0,
        name TEXT DEFAULT '',
        PRIMARY KEY(date, code))""",
)

FINANCE_DDL = (
    "CREATE TABLE IF NOT EXISTS finance_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')",
    """CREATE TABLE IF NOT EXISTS finance_schema_history(
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS finance_account(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        account_type TEXT NOT NULL,
        institution TEXT NOT NULL DEFAULT '',
        currency TEXT NOT NULL DEFAULT 'CNY',
        opening_balance_fen INTEGER NOT NULL DEFAULT 0,
        current_balance_fen INTEGER NOT NULL DEFAULT 0,
        is_own_account INTEGER NOT NULL DEFAULT 1 CHECK(is_own_account IN (0,1)),
        include_in_balance_sheet INTEGER NOT NULL DEFAULT 1 CHECK(include_in_balance_sheet IN (0,1)),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(name, institution))""",
    """CREATE TABLE IF NOT EXISTS finance_category(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_id INTEGER REFERENCES finance_category(id) ON DELETE RESTRICT,
        name TEXT NOT NULL,
        transaction_nature TEXT NOT NULL,
        is_system INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0,1)),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(parent_id, name, transaction_nature))""",
    """CREATE TABLE IF NOT EXISTS finance_transaction(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at TEXT NOT NULL,
        nature TEXT NOT NULL,
        amount_fen INTEGER NOT NULL CHECK(amount_fen > 0),
        source_account_id INTEGER REFERENCES finance_account(id) ON DELETE RESTRICT,
        target_account_id INTEGER REFERENCES finance_account(id) ON DELETE RESTRICT,
        category_id INTEGER REFERENCES finance_category(id) ON DELETE RESTRICT,
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'POSTED',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(source_account_id IS NULL OR target_account_id IS NULL OR source_account_id <> target_account_id))""",
    """CREATE TABLE IF NOT EXISTS finance_balance_sheet_snapshot(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date TEXT NOT NULL UNIQUE,
        snapshot_kind TEXT NOT NULL DEFAULT 'AD_HOC',
        total_assets_fen INTEGER NOT NULL DEFAULT 0,
        total_liabilities_fen INTEGER NOT NULL DEFAULT 0,
        net_worth_fen INTEGER NOT NULL DEFAULT 0,
        liquid_assets_fen INTEGER NOT NULL DEFAULT 0,
        liquid_liabilities_fen INTEGER NOT NULL DEFAULT 0,
        liquid_net_worth_fen INTEGER NOT NULL DEFAULT 0,
        investable_assets_fen INTEGER NOT NULL DEFAULT 0,
        investable_liabilities_fen INTEGER NOT NULL DEFAULT 0,
        investable_net_worth_fen INTEGER NOT NULL DEFAULT 0,
        restricted_assets_fen INTEGER NOT NULL DEFAULT 0,
        restricted_liabilities_fen INTEGER NOT NULL DEFAULT 0,
        restricted_net_worth_fen INTEGER NOT NULL DEFAULT 0,
        risk_adjusted_assets_fen INTEGER NOT NULL DEFAULT 0,
        risk_adjusted_net_worth_fen INTEGER NOT NULL DEFAULT 0,
        data_quality_score INTEGER NOT NULL DEFAULT 100,
        data_quality_level TEXT NOT NULL DEFAULT 'GOOD',
        warning_count INTEGER NOT NULL DEFAULT 0,
        stock_valuation_date TEXT NOT NULL DEFAULT '',
        source_fingerprint TEXT NOT NULL,
        summary_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS finance_balance_sheet_snapshot_line(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id INTEGER NOT NULL REFERENCES finance_balance_sheet_snapshot(id) ON DELETE CASCADE,
        account_id INTEGER REFERENCES finance_account(id) ON DELETE RESTRICT,
        side TEXT NOT NULL,
        group_key TEXT NOT NULL,
        group_name TEXT NOT NULL,
        account_type TEXT NOT NULL,
        account_name TEXT NOT NULL,
        institution TEXT NOT NULL DEFAULT '',
        balance_fen INTEGER NOT NULL DEFAULT 0,
        risk_weight_bps INTEGER NOT NULL DEFAULT 10000,
        risk_adjusted_balance_fen INTEGER NOT NULL DEFAULT 0,
        is_liquid INTEGER NOT NULL DEFAULT 0 CHECK(is_liquid IN (0,1)),
        is_investable INTEGER NOT NULL DEFAULT 0 CHECK(is_investable IN (0,1)),
        is_restricted INTEGER NOT NULL DEFAULT 0 CHECK(is_restricted IN (0,1)),
        data_quality TEXT NOT NULL DEFAULT 'OK',
        created_at TEXT NOT NULL,
        UNIQUE(snapshot_id, account_id))""",
)

# --- stock database content -------------------------------------------------
# Dates sit on real XSHG sessions so the 2.0 execution-calendar guard can accept
# them; the legacy trade rows carry a local time-of-day like the real database.
TRADE_HISTORY = [
    {  # 0: complete, single fee field
        "code": "510300", "name": "沪深300ETF", "action": "买入", "qty": 1000, "price": 3.9,
        "trade_date": "2025-12-01 10:12:00", "fee": 5.0, "fee_source": "RECORDED",
        "tags": [], "created_at": "2025-12-01 20:00:00",
    },
    {  # 1: complete, fees split into components
        "code": "510300", "name": "沪深300ETF", "action": "买入", "qty": 500, "price": 3.95,
        "trade_date": "2025-12-05 09:35:00", "commission": 5.0, "transfer_fee": 0.0,
        "stamp_tax": 0.0, "fee_source": "RECORDED", "created_at": "2025-12-05 21:00:00",
    },
    {  # 2: complete ETF buy
        "code": "159915", "name": "创业板ETF", "action": "买入", "qty": 2000, "price": 2.1,
        "trade_date": "2025-12-08 14:03:00", "fee": 6.0, "fee_source": "RECORDED",
        "created_at": "2025-12-08 22:00:00",
    },
    {  # 3: no fee at all -> the service must park it as pending/unknown_fees
        "code": "600519", "name": "贵州茅台", "action": "买入", "qty": 100, "price": 1500.0,
        "trade_date": "2025-12-10 10:20:00", "created_at": "2025-12-10 22:00:00",
    },
    {  # 4: sell of a position that only exists as a pending buy -> insufficient lots
        "code": "600519", "name": "贵州茅台", "action": "卖出", "qty": 20, "price": 1620.0,
        "trade_date": "2025-12-15 13:30:00", "fee": 9.0, "fee_source": "RECORDED",
        "created_at": "2025-12-15 22:00:00",
    },
    {  # 5: price missing (legacy stored 0) -> never zero-filled
        "code": "510500", "name": "中证500ETF", "action": "买入", "qty": 300, "price": 0,
        "trade_date": "2025-12-12 11:00:00", "fee": 5.0, "fee_source": "RECORDED",
        "created_at": "2025-12-12 22:00:00",
    },
    {  # 6: complete sell, FIFO against rows 0 and 1
        "code": "510300", "name": "沪深300ETF", "action": "卖出", "qty": 400, "price": 4.1,
        "trade_date": "2025-12-18 10:05:00", "fee": 4.0, "fee_source": "RECORDED",
        "created_at": "2025-12-18 22:00:00",
    },
    {  # 7: MERGED is a portfolio summary row, not a real execution
        "code": "600519", "name": "贵州茅台", "action": "合并", "qty": 0, "price": 0,
        "date": "2025-12-19", "created_at": "2025-12-19 22:00:00",
    },
    {  # 8: fee only estimated by the old app -> cannot be treated as recorded
        "code": "510300", "name": "沪深300ETF", "action": "买入", "qty": 200, "price": 4.05,
        "trade_date": "2025-12-22 14:40:00", "commission": 4.0, "fee_source": "ESTIMATE",
        "created_at": "2025-12-22 22:00:00",
    },
]

PORTFOLIO = [
    {  # opening baseline holding: 500 shares confirmed by daily_stock_snapshot
        "code": "588000", "name": "科创50ETF",
        "lots": [{"qty": 500, "cost": 1.0, "buy_date": "2025-11-20", "strategy_version": 1,
                  "exit_model_profile": "LEGACY_NO_TRUE_R", "stop_loss": None}],
        "partial_sales": [],
    },
    {  # stale cache: the rebuilt ledger says 1100, this cache still says 1200
        "code": "510300", "name": "沪深300ETF",
        "lots": [{"qty": 1200, "cost": 3.92, "buy_date": "2025-12-01", "strategy_version": 1,
                  "exit_model_profile": "LEGACY_NO_TRUE_R", "stop_loss": None}],
        "partial_sales": [],
    },
]

TRANSFER_DATA = {
    "in": 30000.0,
    "out": 5000.0,
    "history": [
        {"date": "2025-12-02", "net_cf": 30000.0},
        {"date": "2025-12-16", "net_cf": -5000.0},
    ],
}

CAPITAL_HISTORY = [
    ("2025-11-28", 50000.0, 0.0, 0.0, "manual"),
    ("2025-12-22", 90000.0, 30000.0, 5000.0, "manual"),
]

DAILY_SNAPSHOT = [
    # earliest cash observation: this is the only defensible cash baseline
    ("2025-11-28", 0.0, 50000.0, 0.0, 0.0, 0.0, 0.0),
    ("2025-12-22", 9015.0, 62000.0, 2483200.0, 164000.0, 2319200.0, 2647200.0),
]

DAILY_STOCK_SNAPSHOT = [
    ("2025-11-28", "588000", 500.0, 1.0, "科创50ETF"),
    ("2025-11-28", "510300", 0.0, 3.9, "沪深300ETF"),
]

STRATEGY_MAIN = {
    "index_id": "CSI300",
    "etf_code": "510300",
    "strategy_version": "index-strategy-atr-v9.2",
    "signal_date": "2025-12-01",
    "entry_date": "2025-12-02",
    "entry_price": 3.9,
    "signal_atr": 0.05,
    "shares": 1000,
    "main_lot_id": "lot-main-1",
    "entry_executed": True,
    "exit_filled": False,
    "adds": [
        {"date": "2025-12-05", "price": 3.95, "shares": 500, "signal_atr": 0.048,
         "amount": 1975.0},
    ],
    "cooldown_until": None,
    "execution_source": "legacy_unverified",
}

STRATEGY_UNVERIFIABLE = {
    "index_id": "CSI500",
    "strategy_version": "index-strategy-atr-v9.2",
    "signal_date": "",
    "entry_price": 0,
    "shares": 0,
    "entry_executed": False,
    "notes": "stale cache without verified signal",
}

WATCHLIST = [
    {"code": "510300", "name": "沪深300ETF", "source": "watchlist_forward_adjusted"},
    {"code": "588000", "name": "科创50ETF", "source": "watchlist_forward_adjusted"},
]

KV_ROWS = {
    "trade_history": TRADE_HISTORY,
    "portfolio": PORTFOLIO,
    "transfer_data": TRANSFER_DATA,
    "cleared_stocks": [],
    "stock_watchlist": WATCHLIST,
    "total_capital": 90000.0,
    "index_strategy:v1:position:CSI300": STRATEGY_MAIN,
    "index_strategy:v1:position:CSI500": STRATEGY_UNVERIFIABLE,
}

# --- finance database content ----------------------------------------------
ACCOUNTS = [
    # id, name, account_type, institution, opening, current
    (1, "华泰证券", "SECURITIES_CASH", "华泰证券", 0, 6654500),
    (2, "中信证券", "SECURITIES_CASH", "中信证券", 0, 0),
    (3, "招行储蓄卡", "BANK", "招商银行", 1200000, 1200000),
    (4, "支付宝", "ALIPAY", "支付宝", 350000, 350000),
    (5, "招行信用卡", "CREDIT_CARD", "招商银行", 0, 286000),
    (6, "花呗", "CONSUMER_CREDIT", "蚂蚁集团", 0, 45000),
    (7, "股票资产(旧)", "STOCK_ASSET", "同花顺", 0, 5000000),
    (8, "现金钱包", "CASH", "", 0, 80000),
]

CATEGORIES = [
    (1, None, "工资", "INCOME"),
    (2, None, "餐饮", "EXPENSE"),
    (3, None, "交通", "EXPENSE"),
    (4, None, "手续费", "FEE"),
    (5, None, "其他收入", "INCOME"),
]

TRANSACTIONS = [
    # id, occurred_at, nature, amount_fen, source, target, category, description, status
    (1, "2025-12-05T12:30:00", "EXPENSE", 8800, 3, None, 2, "午餐", "POSTED"),
    (2, "2025-12-20T08:00:00", "INCOME", 1500000, None, 3, 1, "12月工资", "POSTED"),
    (3, "2026-01-08T19:00:00", "EXPENSE", 25900, 3, None, 3, "打车", "POSTED"),
    (4, "2026-01-19T09:00:00", "FEE", 500, 3, None, 4, "基金手续费", "POSTED"),
    (5, "2026-02-03T13:00:00", "EXPENSE", 13500, 3, None, 2, "聚餐", "POSTED"),
    (6, "2026-02-10T10:00:00", "INTERNAL_TRANSFER", 200000, 3, 4, None, "还信用卡", "POSTED"),
    (7, "2025-12-10T10:00:00", "EXPENSE", 19900, 3, None, 3, "已作废的记账", "VOIDED"),
]

BALANCE_SHEET_SNAPSHOT = (
    # snapshot_date, total_assets, total_liabilities, net_worth
    ("2026-02-10", 8000000, 331000, 7669000),
)


def build_stock(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        for ddl in STOCK_DDL:
            conn.execute(ddl)
        for key, value in KV_ROWS.items():
            conn.execute("INSERT INTO kv_store(key,value) VALUES(?,?)",
                         (key, json.dumps(value, ensure_ascii=False)))
        conn.executemany("INSERT INTO daily_snapshot VALUES(?,?,?,?,?,?,?)", DAILY_SNAPSHOT)
        conn.executemany("INSERT INTO capital_history VALUES(?,?,?,?,?)", CAPITAL_HISTORY)
        conn.executemany("INSERT INTO daily_stock_snapshot VALUES(?,?,?,?,?)",
                         DAILY_STOCK_SNAPSHOT)
        conn.commit()
    finally:
        conn.close()
    return path


def build_finance(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        for ddl in FINANCE_DDL:
            conn.execute(ddl)
        conn.executemany("INSERT INTO finance_meta(key,value) VALUES(?,?)",
                         [("schema_version", "21"),
                          ("module_version", "finance-stage10-v10.25-spending-profile-v1320")])
        conn.execute("INSERT INTO finance_schema_history VALUES(?,?,?)",
                     (21, "2025-11-20T10:00:00", "spending profile v1320"))
        stamp = "2025-11-20T09:00:00"
        for account_id, name, kind, institution, opening, current in ACCOUNTS:
            conn.execute(
                """INSERT INTO finance_account(id,name,account_type,institution,currency,
                   opening_balance_fen,current_balance_fen,is_own_account,include_in_balance_sheet,
                   is_active,created_at,updated_at) VALUES(?,?,?,?,'CNY',?,?,1,1,1,?,?)""",
                (account_id, name, kind, institution, opening, current, stamp,
                 "2025-12-22T20:00:00"))
        for category_id, parent, name, nature in CATEGORIES:
            conn.execute(
                """INSERT INTO finance_category(id,parent_id,name,transaction_nature,is_system,
                   is_active,sort_order,created_at,updated_at) VALUES(?,?,?,?,1,1,0,?,?)""",
                (category_id, parent, name, nature, stamp, stamp))
        for row in TRANSACTIONS:
            conn.execute(
                """INSERT INTO finance_transaction(id,occurred_at,nature,amount_fen,
                   source_account_id,target_account_id,category_id,description,status,
                   created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (*row, stamp, stamp))
        for day, assets, liabilities, net in BALANCE_SHEET_SNAPSHOT:
            conn.execute(
                """INSERT INTO finance_balance_sheet_snapshot(snapshot_date,snapshot_kind,
                   total_assets_fen,total_liabilities_fen,net_worth_fen,source_fingerprint,
                   created_at,updated_at) VALUES(?,'DAILY_CLOSE',?,?,?,'legacy-fixture',?,?)""",
                (day, assets, liabilities, net, f"{day}T22:00:00", f"{day}T22:00:00"))
        conn.commit()
    finally:
        conn.close()
    return path


def build(directory: Path) -> tuple[Path, Path]:
    directory = Path(directory)
    return build_stock(directory / "stock_manager.db"), build_finance(directory / "finance_manager.db")


if __name__ == "__main__":
    import sys

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts/tmp/legacy-fixtures")
    stock, finance = build(out)
    print(f"stock={stock}")
    print(f"finance={finance}")
