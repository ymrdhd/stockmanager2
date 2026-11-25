# 09F V1.0 功能矩阵（开发自评，待独立审查）

继承 09C 对应表与 09D 独立审查。PASS指相应自动化实际通过，FIXED是待审查状态；PARTIAL区分源码/口径/验证。所有真实网页渲染未验收。

| V1.0 功能/问题 | 后端/CLI | 网页入口 | 证据/完成状态 |
|---|---|---|---|
| 工作台、资产、基础风险、当日收益 | FinanceService.overview、PortfolioService.valuation | Workspace总览、投资 | 基础金额/比率/流动性/风险入口；部分质量及风险边界 PARTIAL |
| 股票交易、持仓、批次、现金、更正、作废 | TradeService、PortfolioService | 交易及投资页面 | 09E跨运行时API回归PASS；真实浏览器NOT RUN |
| 001、002、003 | 09E WriteAttempt、TradeService、AccountScope | 表单和账户切换 | 保留FIXED；09E测试及本轮前端45项内回归PASS，未独立关闭 |
| 004 T5 独立证据 | PortfolioService.strategy_evidence/_evidence_items | 投资证据、T5比例及现金 | 真合成T5批次核实、偏离/手工拒绝，完整周期原测试PASS；FIXED待审查 |
| 005 会话重连 | auth、identity、WriteAttempt | Workspace offline/online/轮询、登录 | 断线禁写与清除旧投资测试PASS；全部异步入口、会话过期与真实重连待验证，PARTIAL |
| 006 收益版本 | PortfolioService.monthly_pnl/valuation、strategy_evidence | 摘要日期详情，同一账户/时点/版本检查 | 新行情及账本版本拒绝，页面版本冲突测试PASS；FIXED待审查 |
| 007 名称搜索 | 0011、MarketService._save、两行情提供方 | 投资代码/名称查询 | 合成名称、unknown、身份保留PASS；实网NOT RUN；历史来源名称迁移仍未覆盖，PARTIAL |
| 008 基础退款 | FinanceService.bill/change_bill/_month、0011 | 原支出行录入关联退款，更正/作废，月/年 | 跨月、上限、幂等、手工余额、升级更正链PASS；旧退款迁移阻断、旧口径与候选冲突，PARTIAL |
| 009 当日投资收益比例 | PortfolioService、FinanceService | 总览、投资和月度 | 金额与分母、空缺及版本专项PASS；候选口径待产品确认 |
| 010 旧批次核验/差额/权益校正 | 既有审计及 cash adjustment | 交易待核对/期初更正、投资差额与现金校正 | 最小入口已有，不自动改账；完整预览、专门旧证据工作流及UI专项未完，PARTIAL |
| 011 流动净资产 | FinanceService._overview/net_worth_trend | 总览、历史、趋势、CSV | 证券不重复、未知分类不计算PASS；受限/其他资产分类机制缺失，PARTIAL |
| 012 近止损 | StrategyService._near_stop/stop_distance、AppConfig、CLI | 总览、投资风险列 | 2%候选，未改变核心公式；距离边界PASS，实际保护价过期和主/子仓全链待验证，PARTIAL |
| 013 一键备份/端口 | POST /maintenance/backups、system/status | 系统页 | 登录/声明/旧generation/重复备份PASS，网页NOT RUN；端口只读/重启说明 |
| 014 期初数据分页 | /openings ledger_revision、确定排序 | PagedRecords | 202行分页及旧版本拒绝PASS，FIXED待审查 |
| 015 普通恢复 | restore/takeover-rollback、维护marker | CLI | 真实子进程中断/再次中断/启动拒绝/回滚PASS，FIXED（S1）待审查 |
| 016 数据质量/财务健康 | Finance/Portfolio现有质量字段、候选流动性与收益原因 | 总览/投资/财务/行情 | 基础事实有、逐项质量/来源解释仍PARTIAL |
| 017 审计 | trade get include_audit、已有审计 | 成交字段表与原始JSON | 部分中文字段，退款审计与全部策略细节尚不足，PARTIAL |
| 普通财务账户、余额、账单、转账、财务月/年 | FinanceService | 财务 | 既有规则与本轮财务专项PASS；真实页面NOT RUN |
| 三类迁移、独立对账、正式接管 | migrate_legacy、takeover | CLI+只读对账 | 合成迁移/接管回归PASS，真实旧库NOT RUN；退款映射PARTIAL |
| 行情/日历 | MarketService、verify_calendar | 投资/系统 | 历史可信2026-12-31；2027资料BLOCKED，实网NOT RUN |
| 认证/身份/单写锁、隔离、演示 | BrowserSecurity、Database、create_demo | 登录与系统 | 认证和身份专项PASS；演示/真实页面本轮NOT RUN |
| Windows/双机 | windows.ps1、start.py、Tailscale Serve | 本地脚本 | 保持单正式库，实机NOT RUN |

环境项09D-018保持BLOCKED/本轮NOT RUN；09D-019资料缺口不变。V1.5/V2.0延期保持ADR-013，不引入自动下单。09F未完成全部源码，不能无条件冻结。
