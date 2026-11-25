"""ISS-016 期初持仓更正与 ISS-017 基础公司行为的实际调整。

两条业务事实都必须在**不伪造买卖成交**的前提下可审计、可回放：

* ``opening_positions`` 从"每账户每证券只有一行"改为**版本化行**：原行永不
  被覆盖，``superseded_by`` 指向替代它的新行，``voided_at`` 标记作废；"当前
  行唯一"改由部分唯一索引 ``opening_current`` 保证。SQLite 既不能删除被外键
  引用的表，也不能就地改写表级 UNIQUE 约束，因此 ``opening_positions`` 及其
  依赖子图（``trade_lots`` / ``lot_allocations``）必须整表重建，行数据、列
  顺序、批次数与分配 ID 原样保留（``TradeService._post`` 以位置插入批次）。
* ``company_events`` 只**新增可空列**记录实际执行的比例、现金尾差、应用时间、
  作废时间、策略尺度失效标记与应用前快照；旧行保持 NULL，不被改写。

本迁移不产生任何 ``trade_executions``：拆分/合并只是把既有批次的数量按比例
换算，总成本与总费用守恒。
"""
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) 版本化的期初持仓：去掉表级 UNIQUE(account_id,instrument_id)，改用部分
    #    唯一索引，使"每条替换/作废行都留档"与"同时只有一条当前行"并存。
    op.execute('''CREATE TABLE m9_opening_positions (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        instrument_id TEXT NOT NULL REFERENCES instruments(id),
        quantity INTEGER NOT NULL CHECK(quantity>0),
        cost_fen INTEGER NOT NULL CHECK(cost_fen>=0),
        fees_fen INTEGER NOT NULL CHECK(fees_fen>=0),
        occurred_at TEXT NOT NULL,
        note TEXT NOT NULL,
        -- 链接到替代行时必须可延迟：更正先标记旧行、再插入新行，而部分唯一
        -- 索引要求两者在同一事务里互换"当前行"身份。
        superseded_by TEXT REFERENCES m9_opening_positions(id) DEFERRABLE INITIALLY DEFERRED,
        voided_at TEXT
    ) STRICT''')
    op.execute('''INSERT INTO m9_opening_positions
        (id,account_id,instrument_id,quantity,cost_fen,fees_fen,occurred_at,note,superseded_by,voided_at)
        SELECT id,account_id,instrument_id,quantity,cost_fen,fees_fen,occurred_at,note,NULL,NULL
        FROM opening_positions ORDER BY rowid''')

    # 2) 依赖重建：列顺序与数量必须与 0003/0006 完全一致，否则位置插入会错位。
    op.execute('''CREATE TABLE m9_trade_lots (
        id TEXT PRIMARY KEY,
        open_execution_id TEXT UNIQUE REFERENCES trade_executions(id),
        strategy_id TEXT,
        lot_role TEXT NOT NULL CHECK(lot_role IN ('ordinary','main','sub')),
        signal_date TEXT,
        frozen_atr INTEGER,
        opened_qty INTEGER NOT NULL CHECK(opened_qty>0),
        remaining_qty INTEGER NOT NULL CHECK(remaining_qty BETWEEN 0 AND opened_qty),
        remaining_cost_fen INTEGER NOT NULL CHECK(remaining_cost_fen>=0),
        remaining_fees_fen INTEGER NOT NULL CHECK(remaining_fees_fen>=0),
        status TEXT NOT NULL CHECK(status IN ('open','closed','reversed')),
        opening_id TEXT UNIQUE REFERENCES m9_opening_positions(id),
        CHECK((open_execution_id IS NULL)!=(opening_id IS NULL))
    ) STRICT''')
    op.execute('''INSERT INTO m9_trade_lots
        (id,open_execution_id,strategy_id,lot_role,signal_date,frozen_atr,opened_qty,
         remaining_qty,remaining_cost_fen,remaining_fees_fen,status,opening_id)
        SELECT id,open_execution_id,strategy_id,lot_role,signal_date,frozen_atr,opened_qty,
               remaining_qty,remaining_cost_fen,remaining_fees_fen,status,opening_id
        FROM trade_lots ORDER BY rowid''')
    op.execute('''CREATE TABLE m9_lot_allocations (
        id TEXT PRIMARY KEY,
        sell_execution_id TEXT NOT NULL REFERENCES trade_executions(id),
        lot_id TEXT NOT NULL REFERENCES m9_trade_lots(id),
        quantity INTEGER NOT NULL CHECK(quantity>0),
        cost_fen INTEGER NOT NULL CHECK(cost_fen>=0),
        buy_fees_fen INTEGER NOT NULL CHECK(buy_fees_fen>=0),
        method TEXT NOT NULL CHECK(method IN ('fifo','specified')),
        UNIQUE(sell_execution_id,lot_id)
    ) STRICT''')
    op.execute('''INSERT INTO m9_lot_allocations
        (id,sell_execution_id,lot_id,quantity,cost_fen,buy_fees_fen,method)
        SELECT id,sell_execution_id,lot_id,quantity,cost_fen,buy_fees_fen,method
        FROM lot_allocations ORDER BY rowid''')

    for name in ('lot_allocations', 'trade_lots', 'opening_positions'):
        op.execute(f'DROP TABLE {name}')
    for name in ('opening_positions', 'trade_lots', 'lot_allocations'):
        op.execute(f'ALTER TABLE m9_{name} RENAME TO {name}')

    # 当前行唯一：未替换且未作废的行在同一 (账户, 证券) 只能有一条。
    op.execute('''CREATE UNIQUE INDEX opening_current ON opening_positions(account_id,instrument_id)
        WHERE superseded_by IS NULL AND voided_at IS NULL''')
    op.execute('CREATE INDEX opening_history ON opening_positions(account_id,instrument_id,occurred_at)')

    # 3) 公司行为：只加可空列，旧行保持 NULL。
    for statement in (
        'ALTER TABLE company_events ADD COLUMN ratio_from INTEGER CHECK(ratio_from IS NULL OR ratio_from>0)',
        'ALTER TABLE company_events ADD COLUMN ratio_to INTEGER CHECK(ratio_to IS NULL OR ratio_to>0)',
        'ALTER TABLE company_events ADD COLUMN cash_tail_fen INTEGER CHECK(cash_tail_fen IS NULL OR cash_tail_fen>0)',
        'ALTER TABLE company_events ADD COLUMN applied_at TEXT',
        'ALTER TABLE company_events ADD COLUMN superseded_by TEXT',
        'ALTER TABLE company_events ADD COLUMN voided_at TEXT',
        "ALTER TABLE company_events ADD COLUMN strategy_evidence_invalid INTEGER NOT NULL DEFAULT 0 CHECK(strategy_evidence_invalid IN (0,1))",
        # 应用前每个批次的数量/成本快照，作废时据此精确还原（含零股尾差造成的舍入）。
        'ALTER TABLE company_events ADD COLUMN applied_snapshot TEXT',
    ):
        op.execute(statement)
    op.execute('CREATE INDEX company_events_scope ON company_events(account_id,instrument_id,occurred_at)')


def downgrade() -> None:
    raise RuntimeError('automatic downgrade is not supported')
