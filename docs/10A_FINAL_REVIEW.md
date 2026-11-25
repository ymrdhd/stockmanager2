# 10A 最终审查材料（四项口径批准后的定向核对与交付准备）

- 审查材料整理：开发 Agent（2026-11-25）。**本文不构成独立审查裁决，也不构成发布批准。**
- BASE：`26c981551cec7c3b8188b89664e5a5cbedde068f`（`10-local-acceptance`）。本轮分支：`10a-final-delivery`。
- 批准来源：产品负责人 2026-11-25 正式批准四项业务口径，权威文本见 `DECISIONS.md` ADR-017～ADR-020。
- 涉及文档：`PRODUCT_SPEC.md`、`DECISIONS.md`、`INTERFACE_CONTRACT.md`、`STATUS.md`、`ISSUES.md`、`CHANGELOG.md`、`09F_PRODUCT_CANDIDATES.md`、`INDEX.md`、`10A_HANDOFF.md`。

## 1 核对方法与边界

只读定位符号 + 按行读取目标实现与其专项测试，不做例行全仓扫描。逐条把批准文本拆成可判定的断言，落到具体代码行，再用既有专项测试验证。

判定用语：

- **一致**：实现行为与批准文本逐字对得上，未改代码。
- **最小修复**：存在可观察差异，只改差异点，不连带返回值结构、算法与判定分支。
- **文案差异**：语义正确但措辞未逐字对齐，未改代码，记入 `ISSUES.md`。

## 2 逐条核对

### 2.1 退款统计（ADR-017）

批准文本：冲减原支出所属财务月，实际到账时间单独记录。

实现证据：

- `backend/application/finance.py::_month`：按 `related_transaction_id` 把有效退款回填到原支出条目，`totals[kind]-=reduction`，同时 `recovered+=reduction`，条目内写 `refund_applied_fen` 与 `net_amount_fen=amount-reduction`。
- 退款条目自身写 `original_expense_period=month_start(day_of(original.occurred_at)).isoformat()`，即归属原支出财务月；`refund_received_fen` 按 `occurred_at` 落在本月统计，即到账时间单独记录。
- 返回 `refund_basis='original_expense_period; receipt_cash_flow_at_actual_date'`，`bill_cash_flow_fen=totals['income']-gross_expense+refund_received`，两条时间轴分列。
- 约束：`finance.py` 中 `refund requires explicit original expense and eligible manual asset account`、`refund must refer to an effective original expense/fee in its category`、`Conflict('cumulative refunds exceed original expense')`、`reverse or correct linked refunds before changing original expense`。
- `record_detail` 返回 `original_expense`、`effective_refunds_fen`、`remaining_expense_fen`，可审计。

结论：**一致，未改业务代码。** 测试 `tests/unit/test_09f_features.py::test_refund_original_month_and_receipt_cashflow` 覆盖跨期冲减、到账月现金流、累计上限与作废/更正路径。

### 2.2 当日投资收益率（ADR-018）

批准文本：当日投资盈亏 ÷ 上一有效交易日收盘证券权益，两位小数，分母缺失或非正数时不计算。

实现证据与差异：

- 分母口径正确：`backend/application/portfolio.py:1299-1313` 取 `prior_equity=self._valuation_projection(conn,account,prior_time,market_window(prior_time),revision,include_return=False)['equity_fen']`，`prior_time` 源自 `previous_trading_day`，即上一有效交易日收盘口径，不是自然日、不是今日盘中。
- 不计算分支齐备：`not_trading_session`、`prior_close_equity_unavailable`、`daily_pnl_unavailable`、`prior_close_equity_nonpositive`。
- **差异**：原实现固定 6 位小数（`.6f`）。已改为 `.2f`，共两处：
  - `backend/application/portfolio.py:1313`
  - `backend/application/finance.py:701`（财务总览的证券合计收益率，同口径复用）
- 未改动：分子分母来源、`previous_close_equity_fen` 取值、原因码集合、`null` 与 `quality` 判定、接口键名。

结论：**最小修复**（仅显示精度）。测试同步 `tests/unit/test_09f_features.py:44` 断言 `'6.67'`，并新增第 45 行以同一 `format(...,'.2f')` 表达式交叉断言，防止未来再被改回高精度；`test_09f_features.py:53` 的不可计算断言未变。

回退方式：把两处 `.2f` 改回 `.6f` 并还原测试断言即可，无数据迁移、无 schema 影响。

### 2.3 流动净资产（ADR-019）

批准文本：流动资产 − 流动负债；纳入现金、银行卡、支付账户和可变现证券；排除受限资产；未知分类明确提示待确认。

实现证据：

- 纳入集：`backend/application/finance.py::_overview` 中 `liquid_assets=[r for r in included if r['asset_class']=='asset' and r['kind'] in ('bank','cash','payment','securities')]`，与批准文本四项一一对应。
- 计算：`liquid_net=liquid_asset_total-liabilities`；证券按当前估值计入，`assets_ok`/`liabilities_ok` 不成立时整体不计算，缺失不按零处理。
- 未知分类：`liquidity_unknown=[r['id'] for r in included if r['kind'] in ('other_asset','other_liability')]`，`liquidity_reasons` 生成 `liquidity_classification_unknown:<account_id>`，并停止给出 `liquid_net_worth_fen`，即"明确提示待确认"而非静默归类。
- 不把长期/信用卡负债并入流动负债：`credit_card` 参与负债但按现有 legacy 口径处理，`other_liability` 一律视为分类未知。
- `amount_evidence` 同时给出 `liquid_assets_fen`（依据 `known_liquidity_classification`）与 `liquid_net_worth_fen`（依据 `流动资产减流动负债；未分类不计算`）。

结论：**一致，未改业务代码。** 测试 `tests/unit/test_09f_features.py::test_liquidity_no_duplicate_and_unknown` 断言 `liquid_assets_fen==150000`、`liquid_net_worth_fen==130000`；加入 `other_liability` 后 `liquid_net_worth_fen is None` 且 `liquidity_reasons` 非空。

已知限制（见 §3、`ISSUES.md` 10A-001）：当前 schema 没有独立的"受限资产"标记字段，"排除受限资产"只能通过把该账户归入 `other_asset`/`other_liability` 实现，而那会整体触发待确认。也就是说，口径本身已实现，但真实受限资金的表达通道偏窄。此项属产品/数据模型决策，本轮不动 schema。

### 2.4 接近止损预警（ADR-020）

批准文本：距有效保护价 2% 以内提示；仅用于风险预警；不改变 V9/G2 原始策略规则；NDX T5 继续使用独立规则。

实现证据：

- 阈值判定：`backend/application/strategy/service.py::stop_distance(price, stop, threshold_bps)`，`if p<=s: return 'protection_reached'`；`if (p-s)*10000<=s*threshold_bps: return 'near_stop'`；否则 `None`。即严格"以内"含等号，价格已跌破保护价单独标记，不与预警混淆。
- 有效保护价来源：`_near_stop` 中候选为 `('main', position.stop_price)` 加各加仓批次 `('sub', a.get('stop_price'))`，逐角色判定，返回 `role`、`status`、`stop_price`、`quote_price`、`quote_at`、`quote_source`、`threshold_bps`、`display_only: True`。
- 只在证据充分时提示：`valid=[l for l in held if l['signal_verified'] and not l['scale_invalid'] and not l['strategy_deviation']]`，`len(valid)!=len(held)` 或多标的混仓时返回空；行情 `quality!='valid'` 或缺失时返回空。
- 只读不改策略：`display_only=True`，不写成交、不改 `action`、不改保护价、不改批次状态；调用点 `service.py` 中 `stop_warning = self._near_stop(...) if safe and not historical and basis == ETF_UNADJUSTED and not model_exit else []`。
- NDX T5 独立：`held=[lot for lot in all_held if lot['strategy_id']=='INDEX_STRATEGY_ATR']`，`_near_stop` 只接收 ATR 批次；`cfg.is_us_track` 分支走 `_ndx_advice`，T5 不受 2% 规则影响。
- 阈值可配且受约束：`backend/config.py::near_stop_bps` 默认 200、范围 `0..1000`；`start.py server --near-stop-bps 0-1000`；`backend/main.py` 注入 `StrategyService(db, near_stop_bps=settings.near_stop_bps)`。

结论：**一致，未改业务代码。** 测试 `tests/unit/test_09j_near_stop_edges.py` 与 `tests/unit/test_09h_stop_evidence.py` 覆盖边界、证据不足与 T5 分隔。

## 3 新观察（仅记录，未改实现）

| ID | 现象 | 影响 | 建议 |
|---|---|---|---|
| 10A-001 | `migrations/versions/0006_finance.py` 的 accounts 无"受限资产"标记字段；`kind` 仅 securities/bank/cash/payment/other_asset/credit_card/other_liability，`asset_class` 由 kind 推导 | 真实受限资金（冻结、保证金）无法既明确排除又保持其他流动资产可计算 | 仅在产品明确要求时新增受限资产标记迁移，本轮不动 schema |
| 10A-002 | `frontend/src/pages/WorkspacePage.vue:638`、`:665` 直接拼接收益率字符串加 `%`；月度收益表列名为"基础收益率（%）" | 后端改两位小数后前端原样展示 `6.67%`，无需改动 | 无需动作 |
| 10A-003 | `liquidity_basis` 文案仍为 `legacy_current_liabilities; unclassified other assets/liabilities withheld`，未逐字写"排除受限资产" | 语义完整，措辞未对齐批准文本 | 文案级；如需逐字一致可单独修 |

## 4 本轮实际执行的验证

| 内容 | 命令 | 结果 |
|---|---|---|
| 受影响 Python 专项 | `pytest tests/unit/test_09f_features.py tests/unit/test_finance.py tests/unit/test_pnl.py -q` | **44 passed, 5 warnings**（17.31s） |
| 前端 typecheck / build / 单测 | 未执行 | 前端源码本轮零改动，不重复构建（历史基线已 PASS） |
| 全量 Python 回归 | 未执行 | 按开发/审查分工由独立审查者执行 |

未运行项如实记录，不以推断代替实测。

## 5 仍需独立审查裁决的事项

1. 10-001（Windows 只读句柄 fsync）、10-002（收盘后证券名称缺失）、10-003（浏览器标签定位）三项 FIXED 的关闭裁决。
2. ISS-019 / 09D-019（2027 官方日历）仍 BLOCKED：上交所当前仅发布 2026 年休市安排，未猜测、未改锁文件。
3. 真实旧库只读试迁移与逐项对账仍 BLOCKED：无授权一致性副本。
4. Tailscale 私有 HTTPS 与双电脑联调仍 BLOCKED：本机未安装、无第二设备。
5. 完整 Python 回归、缺陷严重程度裁决与 ISSUES 关闭结论。
6. ADR-018 的精度修复是否被接受为最小必要变更（本文给出回退方式）。

## 6 未授权事项（明确未做）

- 未操作、未覆盖、未迁移任何真实业务数据库。
- 未执行正式接管、未切换写端、未部署、未发布。
- 未改数据库 schema、核心 V9/G2 公式、ATR、动态 3/10 日冷却、保护价与批次逻辑、依赖锁。
- 未删除测试、未降低断言、未隐瞒失败以缩短流程。
- 产品口径批准不等于部署授权。
