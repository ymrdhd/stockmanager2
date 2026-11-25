# Web 功能与 API 验收契约

本文件定义新界面的交互与接口通用规范；功能范围以 PRODUCT_SPEC 的已批准版本为准（ADR-013）：UI05、UI01 的日内分钟图与 14 ETF 等权参考线、高级投资绩效图、UI06/UI08 精细财务图表和界面优化延至 V1.5；V1.0 仅验收其余适用基础能力，不创建延期功能空壳。页面只消费后端事实/投影，严禁浏览器维护第二套正式财务数据。新增路由必须有实际页面或批准的运维/测试用途；不用占位菜单假装功能完成。

## 1. 可达功能覆盖表

| ID | 新位置 | 必须保留的现行 Web 能力 |
|---|---|---|
| UI01 | 总览/投资概览 | 证券权益校正（审计）、持仓摘要、当日金额/比例、日内盈亏分钟图及放大、ETF 等权参考线开关、月度盈亏日历及日期详情、买点距离 ATR、数据源状态 |
| UI02 | 投资/持仓 | 搜索代码名称、数量成本市值盈亏、批次展开、止损/跟踪/加仓/到期提示、收盘行情更新 |
| UI03 | 交易/录入与历史 | 买卖、价格数量日期费用备注、普通 FIFO/指定策略批次、主仓加仓及退出原因、信号日期/ATR、历史过滤查询、旧批次核验关联；新增更正是规划功能 |
| UI04 | 投资/策略 | 观察池、候选/正式/否决、市场状态、买点距离、收盘日期/新鲜度、更新 ETF/指数日线 |
| UI05 | 投资/利率 | EFFR、目标区间、FOMC 概率与路径、日/周比较、CPI 更新、实际利率代理 |
| UI06 | 财务/总览 | 总资产/负债/净资产/流动净资产、现金流、近一年趋势、消费结余、账户余额与历史跳转 |
| UI07 | 财务/账本账户 | 手工余额校正、独立手工账单新增修改作废、查询、关联或导入账单查看（旧记录限制保留） |
| UI08 | 财务/详情历史 | 1/3/6/12月净资产、资产结构、收入支出结余、消费属性/分类、财务健康、截至日查询、摘要 CSV、数据质量、历史财务月按年筛选/月份明细 |
| UI09 | 设置 | 服务/数据库身份/写入状态、刷新/备份、端口设置与重启提示；保留已核实且用户使用的主题切换，其他装饰不阻断财务功能 |

UI01–UI09 是覆盖检查而非规定原布局。实际交付应能通过普通导航点击进入，执行成功/错误路径；功能需配置/数据时明确显示原因。自动下单、旧桌面研究和五资产不在范围内。导入等新增项只有批准排期后才进入实现，不能以旧程序已有为由扩张。

## 2. API 通用契约

- `/api/v1`、OpenAPI/Pydantic schema 为实现时接口事实；文档只维护行为约束和重大版本差异。前端生成或校验类型，禁用随意 `any`。
- 金额整数分及币种；超过 JS 安全整数范围时以字符串传输并使用精确解析；证券价格/数量以定点十进制字符串传输。UTC ISO-8601 时间，额外明确市场交易日、财务月；禁止把展示格式当入参。
- 查询包含 `as_of`, `ledger_revision`, `quote_revision`, `quality`, `source`（按字段适用）。多页汇总须共享一致的版本；不拼接两个版本的资产数据。`null` 不显示为 0。列表稳定排序，分页有上限，避免一次返回全库。
- `GET /api/v1/portfolio/{account_id}/reconciliation?as_of=<带时区ISO时间>` 提供内部只读对账预览；可同时传 `ledger_revision`、`quote_revision`、`database_uuid`、`generation` 四项快照约束（必须全传或全不传）。报告在单一 SQLite 只读快照内核对有效卖出与批次分配数量、剩余批次与持仓投影、现金质量、行情可用性、自然日收益与权益变动、待核对成交及未解决公司行为。只展示差异，不提供自动修复；不代表券商对账单或旧库外部对账。版本变化返回冲突，客户端必须重新读取估值后再查。
- 写入请求含幂等键；相同键相同语义返回原结果，键冲突 409；客户端断线/超时重试复用原键，不能自动生成新键。状态不明先查询请求结果。
- 错误体 `code/message/request_id/details`，400/422 输入、401 未认证、403 未授权/CSRF、409 冲突、503 暂不可用；不泄露堆栈和密钥。所有 GET 无账务副作用。后台刷新单独 POST 并查询任务状态。
- 现金/余额调整与普通账单分别显示，危险操作预览影响、确认与审计；不通过前端猜测可用现金或交易收益。

## 3. Vue 交互和验收

统一页面路由、表单、格式化、质量状态组件；Pinia 只存 UI 状态与可失效的服务端响应。按既有 ECharts 能力实现图表，避免为装饰替换框架。空/加载/过期/错误/无权限/离线/部分数据分别显示。数值来源与更新时间可查看；筛选保持一致，键盘操作、表头单位、中文文字清晰。WebSocket 只通知版本变化，断线重连后 HTTP 重取；不在前端增量累计盈亏。

投资页的「内部账本对账预览」必须绑定当前显示的账户、估值时点、账本/行情版本及数据库身份；账户或版本变化立即清空旧报告。它是只读内部检查，不替代券商或旧库对账。

E2E 必测：当前版本适用的 UI01–09 路由及代表性操作；真实成交双击与网络超时重试、历史更正、资金转账、独立余额校正、财务月跨年、缺行情、认证失效、断网不回退、重新加载同版本一致。使用合成测试数据；不把生产页面截图或个人账单放进审查包。


## 09H 增量行为说明
- `/finance/overview` 增量字段 `amount_evidence` 解释既有权威金额：金额、质量、口径/来源、截止、账本和行情版本以及关联账户；`related_reasons` 是相关上下文，不表示每个原因均影响每个指标。账户详情增量携带余额来源、适用现金期初或手工余额快照、证券报价证据。它们来自同一读取快照；不生成新的账务事实。
- 会话失效、断线或身份变化时清理私有数据、失效表单；保留未核实写入记录。已接受连接的数据库身份变化需操作者确认重新读取；候选再变化需重新确认，不自动切换。历史异步响应不得恢复旧身份。

## 09I 财务只读审计与设置补充
- `GET /api/v1/finance/audit/{kind}/{id}?ledger_revision=<可选>`：kind 严格限 `bills/transfers/balances/accounts`，认证沿用统一中间件；返回 `kind,record,audit_events,related_records,original_expense,effective_refunds_fen,remaining_expense_fen,ledger_revision,database_uuid,generation,source`。原记录及直接关联、审计事件为当前事实，不伪造完整历史快照；无原支出/不可判断时退款汇总为 null。账本版本不匹配409、不存在404、不支持kind422。读取不修改账本。
- 财务月 `items` 新增非退款账单 `refund_applied_fen`、退款账单 `original_expense_period`（未知null）；`source=current_effective_finance_facts_not_knowledge_replay`。不改变原期冲减、到账现金流候选语义及手工余额。
- 页面财务详情按记录、账本版本及数据库身份核对，切换筛选/断线/账本变更时清除，身份不匹配拒绝并标记不可写；历史和趋势仅展示请求对应快照。
- UI09 端口入口仅生成下次启动的明确参数，建议1024–65535；CLI与PowerShell接受合法1–65535。正式启动命令只在正式服务模式展示，沙盒/只读保留原启动模式。不得通过网页直接改认证文件、自动重启服务或切换库。实际配置与端口验收在服务端进行。

## 四项已批准业务口径的接口约束（2026-11-25 产品负责人批准）

四项口径的批准文本见 `DECISIONS.md` ADR-017～ADR-020、`PRODUCT_SPEC.md` 2.1/2.2/2.4。本节只固化对外表现，不新增账务事实。

- **退款（ADR-017）**：`GET /api/v1/finance/month`、`/finance/year` 的 `expense_fen`/`fees_fen` 已按**原支出所属财务月**冲减；`refund_received_fen` 为该期内**实际到账**退款，`bill_cash_flow_fen = income − gross_expense + refund_received`，退款不进 `income_fen`。账单 `items` 携带 `refund_applied_fen`（本笔支出冲减额）与退款 `original_expense_period`（未知为 `null`）；`source=current_effective_finance_facts_not_knowledge_replay`。`GET /finance/audit/bills/{id}` 的 `original_expense`/`effective_refunds_fen`/`remaining_expense_fen` 为当前账本口径。退款不自动改手工余额。
- **当日投资收益率（ADR-018）**：`GET /finance/overview` 的 `investment_daily_return_pct` 与 `GET /portfolio/{account_id}/valuation` 的 `daily_return_pct` 为 **两位小数字符串**（如 `6.67`），单位百分数；分子为当日投资盈亏，分母为上一有效交易日收盘证券权益。分母缺失或 ≤0、非交易日、当日盈亏不可用时为 `null`，原因见 `investment_daily_return_reason` / `daily_return_reason`（`not_trading_session`、`prior_close_equity_unavailable`、`prior_close_equity_nonpositive`、`daily_pnl_unavailable`、`account_return_or_denominator_unavailable`）。客户端按字符串原样展示，不得再次四舍五入成别的位数，`null` 显示「不可计算」，不得显示 0。
- **流动净资产（ADR-019）**：`GET /finance/overview` 与 `/finance/net-worth-trend` 返回 `liquid_assets_fen`、`liquid_net_worth_fen`、`liquidity_reasons`、`liquidity_basis`。流动资产为 `bank`/`cash`/`payment`/`securities`（证券按权益计一次）；受限资产不计入。存在 `other_asset`/`other_liability` 或余额/估值不可用时，`liquid_net_worth_fen=null` 且 `liquidity_reasons` 逐账户给出 `liquidity_classification_unknown:<account_id>` 或 `balance_or_valuation_unavailable`；前端必须显示「待确认」，不得用已知部分顶替。`amount_evidence` 中两字段 `source=known_liquidity_classification`。
- **接近止损预警（ADR-020）**：`GET /api/v1/strategy/{account_id}/pool`（及工作台风险视图）中持仓条目新增 `near_stop` 数组，元素为 `{role,status,stop_price,quote_price,quote_at,quote_source,threshold_bps,display_only:true}`；`status` 取 `protection_reached`（报价已达保护价）或 `near_stop`（距保护价 ≤ 阈值）。默认阈值 200 bps，可由 `start.py server --near-stop-bps 0..1000` 调整。该字段是**只读风险展示**：不写入成交、不改变 `action`/`model_exit`/保护价/ATR/批次，NDX T5 走独立规则；报价未核实/过期、账本版本变化、未核实信号或策略偏离时不返回该项。
