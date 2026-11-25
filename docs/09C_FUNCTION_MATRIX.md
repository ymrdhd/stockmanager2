# Stock Manager V1.0 功能完成矩阵（09C 开发核对，非独立审查）

BASE `9b88513c360a70473ef1b340415fb1a81b979ff3`。范围来源 PRODUCT_SPEC §2、INTERFACE_CONTRACT UI01–09、用户 09B/09C 授权。**实现**指可定位到实际源码/业务入口；**PARTIAL** 指实现或证据有缺口；**NOT RUN** 指本轮未执行相应环境验收，不等于功能缺失。下列历史测试文件名是独立审查复测入口，并非 09C 新实测。09C 集中专项及最后补测见报告/日志（82 PASS、9 PASS，重叠不相加）。

| 规格/功能 | 后端真实入口（均 /api/v1 或 CLI） | 前端 / CLI 入口 | 测试证据及状态 |
|---|---|---|---|
| UI01 工作台、净资产、证券权益、当日收益、财务月 | FinanceService.overview；PortfolioService._valuation；/finance/overview | WorkspacePage 总览指标/质量/时间及投资、交易跳转 | 历史 test_finance/test_pnl；09C 两类迁移正式启动及投影 PASS；已实现基础数据、待核对计数和所选账户实际风险/策略信号；接近止损独立阈值未定义（PARTIAL），页面 NOT RUN |
| UI01 月度投资收益、日期详情 | PortfolioService.monthly_pnl；/portfolio/{account_id}/month；/portfolio/{account_id} | 投资 → 月度投资盈亏 → 查看该日持仓；表格而非高级图表 | 09C test_09c_history / test_pnl 选定 golden PASS；已实现，浏览器 NOT RUN |
| UI02 按账户持仓、批次、成本/费用、当前价、市值、当日/累计收益 | PortfolioService.valuation；/portfolio/{account_id}；/lots；/quotes/{id} | 投资 → 证券账户/历史截止、持仓与有效批次、冲突价格 | 09C 收益投影抽测 PASS；历史 test_pnl/test_market_resolution；按账户完整列表与代码/名称搜索已实现，页面 NOT RUN |
| UI03 实际买卖、待核对、幂等、批次、现金 | TradeService.submit/confirm_pending/cash/list_records；/trades、/pending、/cash、/allocations | 交易 → 录入实际成交/确认待核对/资金、指定批次 | 09C test_trading、并发同 ID PASS；双标签真实浏览器 NOT RUN；已实现 |
| UI03 成交更正/作废、审计、历史筛选 | TradeService.correct/void/get/list_records；/trades/{id}?include_audit=true；GET /trades 带账户/标的/UTC区间 | 交易 → 更正/作废/事实与审计/筛选 | 09C test_trading、test_09c_history PASS；已实现 |
| UI02/04 V9/G2、ATR、保护价、冷却、主/子仓、容量、时点 | StrategyService.pool/evaluate/position_rows；/strategy/pool、/positions、/refresh；TradeService | 投资 → 观察池/持仓决策/记录信号/实际成交 | 09C test_strategy_service、test_rc_decision_time、容量竞争 PASS；已实现；历史证据限制、实网及浏览器 NOT RUN |
| UI04 NDX T5 辅助仓 | StrategyService._ndx_advice；verified_ndx_aux_signal；真实批次与 allocations | 投资 → 观察池/持仓决策的 T5 目标及证据按钮；交易真实卖出指定批次 | 09C test_09c_ndx 集中4例及补测2例 PASS：全周期、真实部分成交/幂等、错账户/手工、同标的容量、公开 pool/重新打开 DB；已有现金缓冲专项 PASS；已实现；非资金预留，不证明券商成交符合目标，实际行情/前端 NOT RUN |
| 证券账户、标的、期初数据、现金调整/公司行为 | /accounts、/instruments、/opening-positions、/openings、/portfolio/openings/{id}/correct/void、/company-events、/cash | 交易创建账户/证券、期初现金/期初持仓、更正作废、分红/拆并 | 历史 test_corrections/test_pnl/test_finance；09C 未改旧流程；已实现已批准类型，复杂配股不支持 |
| UI06/07 普通账户/余额、纳入统计、分类、账单 | FinanceService.account/balance/bill/change_bill；/accounts、/finance/balances、/bills | 财务 → 账户/余额核实/分类/账单新增更正作废 | 09B 纯财务合成；09C 混合正式投影 PASS；已实现，实际浏览器 NOT RUN |
| UI07 转账、已包含声明及对账 | FinanceService.transfer/change_transfer/reconcile_transfer；/finance/transfers | 财务 → 内部转账、明确是否已包含、对账 | 09C 混合迁移的余额/转账计数 PASS；历史 test_finance；已实现，复杂未知拒绝 |
| UI06/08 财务月、截至时点历史及年度 | /finance/month、/finance/year/{year}、/finance/overview?as_of= | 财务 → 月、年、历史余额/净资产 | 09C 混合对账的财务月 PASS；历史 test_finance；已实现基础查询、多时点净资产趋势、摘要 CSV、账单组合筛选；净资产趋势后端补测 PASS，页面 NOT RUN；流动净资产独立口径未定义/未实现（PARTIAL） |
| 迁移与独立对账 | tools/migrate_legacy.py dry-run/migrate/reconcile；MigrationService/run_migration | CLI；系统/迁移报告只读查看 | 09B 纯财务 PASS；09C 证券、混合、999股差异拒绝、报告绑定、正常接管及中断 PASS；合成范围闭环，真实库 NOT RUN |
| 正式接管及回滚 | start.py takeover/takeover-rollback；infrastructure.takeover | CLI 审批摘要/明确确认/隔离写端 | 09C test_takeover、test_09c_migration 中断及回滚 PASS；旧 generation 拒绝；已实现，Windows/真实文件故障 NOT RUN |
| 备份/恢复/保留 | start.py backup/daily-backup/restore-check/restore；infrastructure backup/maintenance | 系统查看备份，CLI/Windows 脚本维护 | 09B 已有一致性/保留及备份专项 PASS；09C 接管的安全备份/回滚 PASS；一般恢复不改，Windows NOT RUN；高级异地加密已明确延期 |
| 行情、日历状态及可信更新 | /market/status、/market/refresh、/quotes；calendar_release；tools/verify_calendar.py | 投资/系统行情；CLI 依赖更新审查 | 09C test_09c_calendar、ISS019 到期 PASS；历史 09 实网六项 FAIL，09C NOT RUN；2027 正式数据未导入，可信范围闸门保留 |
| UI09 健康、认证、身份、写锁、请求恢复 | /system/status、/health、/auth、requests API；DatabaseSettings | 登录、顶部连接状态、系统、跨标签同源 pending 管理 | 历史 test_security/test_health、前端单元 34 PASS；09C 旧 generation/接管启动 PASS；真实浏览器 NOT RUN |
| 隔离/只读/演示 | start.py 模式、tools/windows.ps1 Demo | Windows Demo/DemoStart；本地只读启动 CLI | 09B 合成 demo 生成通过；09C test_trading 各模式 PASS；已实现；实机 NOT RUN |
| Windows 安装维护 | tools/windows.ps1；start.py CLI | Install/Build/Start/Stop/Backup/DailyBackup/ScheduleBackup/Demo/VerifyMarket/Maintenance | 源码及操作说明已有；09C NOT RUN，未生成 dist 或运行包 |
| 双机同一正式库 | FastAPI loopback、Host/Origin/session；Tailscale Serve 配置说明 | 公司唯一正式库，家里浏览器 | 源码/运维路径有；双机 NOT RUN，不同步活 SQLite |

## 源码完成判定

重点策略和合成迁移闭环已实现并通过针对性测试。最后补齐工作台所选账户风险/待办计数、持仓搜索、基础多时点净资产趋势/摘要 CSV、账单组合筛选；前端尚未构建/验收。仍有 **PARTIAL** 规格口径：接近止损独立预警阈值及流动净资产独立计算定义；现有实际保护价、触发状态及净资产仍可查，不以猜测阈值或总净资产冒充。由产品及独立审查确认口径及级别，不擅自豁免或关闭。没有依据仅以 82 PASS 断言全范围已完成或无 S0/S1；本轮不建议无条件源码冻结。不得以生产构建 NOT RUN 代替代码缺口，亦不得把缺少实网/Windows 等验收当作代码不存在。

明确延期（已批准）：完整历史配置版本与研究级重放、Fed 利率、日内分钟图及 14 ETF 等权参考、高级绩效/精细财务图表、复杂组合优化、高级加密异地备份。V2.0 候选：券商/账单批量导入、复盘、复杂预算及现金流预测；不承诺排期。自动下单、五资产、双正式库同步永久不在范围。
