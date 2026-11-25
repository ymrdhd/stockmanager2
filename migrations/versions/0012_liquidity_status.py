"""受限资产明确标记与分类审计（10B-001）。

只增加最小必要字段，不建立第二套资产账本：

* ``accounts.liquidity_status``：``liquid``（已确认不受限）/ ``restricted``
  （已确认受限）/ ``unknown``（尚未确认）。迁移对既有账户一律保留
  ``unknown``——旧库没有分类证据，不能凭空认定为不受限。
* ``account_liquidity_events``：分类生效时点、变更前/后状态、原因。历史查询
  只使用该账户在该时点之前已经记录的分类证据；没有证据就是待确认，不用
  今天的分类倒推历史。

受限资产仍计入 ``total_assets_fen`` / ``net_worth_fen``，只是不进入流动
资产与流动净资产；负债流动性继续按已批准口径（全部纳入统计负债作为流动
负债），不使用本字段。
"""
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows deliberately become 'unknown': no historical classification
    # evidence exists, so none may be invented as 'liquid'.
    op.execute("ALTER TABLE accounts ADD COLUMN liquidity_status TEXT NOT NULL "
               "DEFAULT 'unknown' CHECK(liquidity_status IN ('liquid','restricted','unknown'))")
    op.execute('''CREATE TABLE account_liquidity_events (
        id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
        previous_status TEXT NOT NULL CHECK(previous_status IN ('liquid','restricted','unknown')),
        liquidity_status TEXT NOT NULL CHECK(liquidity_status IN ('liquid','restricted','unknown')),
        effective_at TEXT NOT NULL, reason TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('initial','changed'))
    ) STRICT''')
    op.execute('CREATE INDEX account_liquidity_history '
               'ON account_liquidity_events(account_id,effective_at)')


def downgrade() -> None:
    raise RuntimeError('automatic downgrade is not supported')
