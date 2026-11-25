# 09F 产品口径决策记录（2026-11-25 已正式批准）

依据 PRODUCT_SPEC、INTERFACE_CONTRACT、09D 审查、原始参考 ZIP 的 Finance 实际源码及 09F 任务书。原始参考 ZIP 未修改。**2026-11-25 产品负责人已正式批准下列四项口径**，权威文本与回退方案见 `DECISIONS.md` ADR-017～ADR-020；本文件保留原始候选证据链，仅作历史溯源，不再代表「待确认」状态。不改变 V9/G2/ATR/动态3/10日冷却公式。

| 事项 | 证据与已批准口径 | 落地状态（定向核对结论） |
|---|---|---|
| 退款 | 原 Finance `subroutine/finance/transaction_service.py` 44–56、117–167、960–1004 及 `period_spending.py` 148–180：关联原支出/累计上限、冲减原支出期间。**批准口径**：冲减原支出所属财务月，实际到账时间单独记录。09F 保留原期支出冲减；实际收到退款另记到账月 `refund_received_fen`/`bill_cash_flow_fen`，不计收入。独立正数账单、关联原支出、现金收款账户、幂等、更正/作废，原支出有有效退款时拒绝直接冲正；手工余额不自动更新。 | **与实现一致**（`FinanceService._month`、`record_detail`、`BillInput kind='refund'` 校验）。旧库含 REFUND/REIMBURSEMENT 仍需迁移映射与独立对账，不默认迁入；普通财务账户现只允许 manual_snapshot。 |
| 基础当日投资收益率 | 统一 PortfolioService：当日正式盈亏 / 上一有效交易日收盘权益 ×100；同事务同版本；总体采用总体分子分母。**批准口径**：显示两位小数，分母缺失或非正数时不计算。 | **本次收敛一处精度差异**：原 `.6f` 六位小数 → `.2f`（`portfolio.py:1313`、`finance.py:701`），并更新 `tests/unit/test_09f_features.py:44` 断言为 `6.67`。非 TWR/MWR；非交易日、缺失或非正分母返回 `null` 及原因码不变。 |
| 流动净资产 | 原 Finance `subroutine/finance/balance_sheet_service.py` 153–160、289–317：流动资产减流动负债，不能把长期负债统一归流动。**批准口径**：流动资产−流动负债，纳入现金、银行卡、支付账户和可变现证券（权益计一次），排除受限资产，未知分类明确提示待确认。 | **与实现一致**（`_overview` 的 `liquid_assets_fen`/`liquid_net_worth_fen`/`liquidity_reasons`）。已知限制：schema 无「受限资产」分类字段，排除方式是只纳入 `bank`/`cash`/`payment`/`securities`，`other_asset`/`other_liability` 一律不进入分子并给出 `liquidity_classification_unknown:<id>`；因此任何真实受限资金目前只能落在 `other_asset` 里被排除并提示，见 ISSUES 10A-001。 |
| 接近止损 | 仅展示预警，默认 200 bps（2%），`start.py server --near-stop-bps 0..1000` 配置；使用有效证券未复权行情及账本核实的 ATR 仓证据，已达保护价优先，已有模型退出不覆盖；T5 排除。 | **与实现一致**（`StrategyService._near_stop`、`stop_distance`；`held` 仅含 `INDEX_STRATEGY_ATR` 批次，T5 独立行走 `_ndx_advice`）。09J 已补 12 项主/子仓与过期行情边界测试。仅风险展示，不写成交、不改策略。 |
| 财务健康 | 展示流动资金、负债、净资产和财务月收支事实；无综合评分。 | 流动口径已随 ADR-019 冻结；质量信息仍逐金额完善，避免零代未知。 |
