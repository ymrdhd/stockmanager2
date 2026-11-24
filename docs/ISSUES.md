# 已确认问题库

严重程度、最终修复优先级及审查结论由产品负责人/独立审查者裁决；下表记录实际现象与已批准的计划边界，不替代验收结果。

| ID | 现象 | 影响 | 计划修复版本/阶段 | 状态 |
|---|---|---|---|---|
| ISS-001 | Python uv.lock 与 frontend/package-lock.json 尚未生成（00 依赖源访问失败） | 无法证明可复现安装 | V1.0 交付前 | OPEN；06 实测 uv 离线缓存缺少 exchange-calendars、PyPI 超时、npm registry DNS EAI_AGAIN；未生成锁文件 |
| ISS-002 | Vue 完整类型检查、生产构建、浏览器验收未完成 | 完整 Web 交付未验证 | V1.0 前端阶段 | OPEN；06 五页及表单源码已实现，独立 TS 与 npm test 11 PASS；vue-tsc/Vite 未安装，完整类型检查、构建及浏览器 E2E 未通过 |
| ISS-003 | Ruff/mypy 尚未完整执行，当前仍未安装 | 全仓静态检查未验证 | V1.0，环境具备后补齐 | OPEN |
| ISS-004 | 日志 basicConfig 在已有 handler 时可能不生效 | 配置的日志级别可能未应用 | V1.5（独立审查批准延期） | OPEN |
| ISS-005 | Windows 运行及 OS 文件锁未实际验证 | 目标部署平台兼容性待确认 | V1.0 正式部署阶段 | OPEN |
| ISS-006 | 普通启动参数原可重选另一份 formal 库 | 正式身份可能漂移 | 02-trading-core | CLOSED，独立审查确认；`test_iss006_fixed_binding_cli_and_service` 与 `test_explicit_cli_upgrade_and_maintenance_lock` |
| ISS-007 | 读上下文原为自动提交，跨查询快照不一致 | 持仓、现金及版本可能混读 | 02-trading-core | CLOSED，独立审查确认；`test_iss007_read_snapshot_across_concurrent_commit` 与 `test_actual_trade_reads_share_snapshot` |
| ISS-008 | 历史成交补录 pending 原无法确认，历史更正无法重放 | 历史持仓、成本及收益无法重算 | 03-pnl | CLOSED，独立审查确认；`test_golden_e_iss008_replay_and_void`、`test_historical_sell_replay_cash_once_and_cost_conservation`、`test_specified_reference_replay_failure_atomic` |
| ISS-009 | 期初现金以前的补录可能被重复加总 | 现金、权益及收益失真 | 03-pnl | CLOSED，独立审查确认；`test_iss009_cash_boundary_and_iss010_future`、`test_legacy_prebaseline_cash_is_not_double_counted` |
| ISS-010 | 原可录入未来成交或资金事实 | 有效账本可能包含未发生交易 | 03-pnl | CLOSED，独立审查确认；`test_iss009_cash_boundary_and_iss010_future`，可注入 UTC 时钟 |
| ISS-011 | 无正式绑定时不能初始化独立本地沙盒 | 独立本地模式首次启动受阻 | 第 07 轮（已指定） | OPEN |
| ISS-012 | 主数据新增原不递增账本版本 | 查询或缓存可能过期 | 03-pnl 及财务功能 | 证券主数据已修复；`test_p09_consistent_snapshot_and_master_revision`；财务新增/账户更新/分类及写入均递增 revision；`test_iss012_and_networth_consistent_concurrent_snapshot`、`test_golden_c_f04_f05_asset_once`；CLOSED，05 独立审查确认证券和财务版本保护通过 |
| ISS-013 | 调用者可传入更早的交易日绕过缺失昨收 | 当日收益错误 | 04-strategy | CLOSED，独立审查确认；`test_iss013_real_calendar_and_coverage`、`test_iss013_missing_actual_previous_close`、`test_iss013_calendar_unavailable_safe_degradation`；日历有效覆盖至 2026-12-31，超出范围安全降级 |
| ISS-014 | 周末及开盘前正常收盘价被视为不可用 | 休市权益及收益无法正确显示 | 04-strategy | CLOSED，独立审查确认；`test_iss014_weekend_preopen_and_dividend`、`test_iss014_lunch_and_after_close_quality` |
| ISS-015 | 观察时间混同行情生效时间与实际获取时间，迟到收盘价不能历史重算 | 历史收益、行情版本及策略可获得性不清 | 04-strategy | 证券部分 CLOSED，独立审查确认；`test_iss015_late_close_revision_and_availability`、`test_0003_to_0004_real_upgrade_and_legacy_availability`、`test_0004_actual_ddl_failure_rollback`；策略部分仍 OPEN；新增更正可获得性回归通过，不代表完整策略回放验收 |
| ISS-016 | 非成交资金、分红及期初记录缺少更正机制 | 误录后无法安全更正相关事实 | 第05/08轮（已指定） | 部分 FIXED，05 独立审查确认日常部分通过；入/出金、余额调整、现金分红、手工余额、账单、可安全重放的成对转账更正/作废通过（`test_golden_e_dividend_void_and_correction`、`test_golden_f_cash_correction_all_flow_types`、`test_golden_b_p04_p05_transfer_and_retry`、`test_transfer_correction_failure_retains_original_pair`）；证券期初现金/期初持仓及复杂历史调整仍 OPEN，按08处理 |
| ISS-017 | 未处理数量型公司行为缺少实际调整及解除异常入口 | 相关持仓及收益持续不可核验 | 第08轮（已指定） | OPEN |
| ISS-018 | 正式收盘价冲突后无法人工确认恢复 | 可信权益、日收益持续不可用 | 04B-strategy | CLOSED，独立审查确认；`test_iss018_resolution_preserves_evidence_and_temporal_versions`、`test_resolution_write_failure_keeps_revision_and_conflict`、`test_resolution_http_and_reopen` |
| ISS-019 | XSHG 日历有效期限至 2026-12-31 | 超期限无法核验交易日、行情及收益 | V1.0 正式发布前（已指定） | OPEN；已提供覆盖起止日期、剩余日数及60天临近提示，超范围安全降级；`test_iss019_coverage_and_expiry`。2026-11-24核验上交所休市公告页（https://www.sse.com.cn/disclosure/dealinstruc/closed/）未找到2027正式通知；PyPI 4.13.3虽已发布，其XSHG源码仍止于2026（https://raw.githubusercontent.com/gerrymanoim/exchange_calendars/4.13.3/exchange_calendars/exchange_calendar_xshg.py），未升级。发布前核对交易所公告、更新可信日历库及依赖锁，并测试新覆盖及节假日边界。 |
| ISS-020 | 普通沪深成交可在非交易日或异常时段有效入账 | 错误持仓、现金和收益 | 04B-strategy | CLOSED，独立审查确认；`test_iss020_invalid_facts_pending_and_explicit_date_correction`、`test_invalid_sell_and_calendar_unavailable`；异常事实 pending、确认受同一校验约束，已有历史有效成交不自动修改 |
| ISS-021 | 正式 V9/G2 引擎及原新规则对照缺失 | 观察池及正式决策不可用 | 04B-strategy | OPEN，V1.0 发布必需；产品负责人批准先完成05，不阻断其他开发；06 再核查指定旧源码 ZIP 未在本轮附件或可用参考目录中找到，不能核对实际 SHA256、有效调用链及参数；详见 strategy_rule_map |
| ISS-022 | 真实东方财富接口未取得有效响应 | 实际行情准确性与连通性尚未验收 | V1.0 真实行情验证 | OPEN；2026-11-24 对510300日线和盘中探测均返回 provider transport unavailable；固定夹具 PASS |
| ISS-023 | 已包含手工余额的转账更正原被拒绝 | 已核实错误转账无法整体撤销 | S1，第06轮（已指定） | FIXED，待独立审查；`test_iss023_included_transfer_void_golden`、`test_iss023_replacement_atomic_failure` 通过，保留两端证据、最新余额ID、防重、历史缺口及事务回滚 |
| ISS-024 | 新证券账户缺生效日期影响历史统计 | 历史净资产可能包含尚未存在的账户 | S2，第06轮（已指定） | FIXED，待独立审查；`test_iss024_effective_and_scope_history` 通过；旧未知日期不猜测，显式核实；统计范围事件及误创建保护，关闭不隐藏余额 |
