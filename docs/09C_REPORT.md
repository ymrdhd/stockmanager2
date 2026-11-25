# 09C-final-web-completion 开发报告

结论：CHANGES_REQUIRED / 待独立审查，**不建议无条件源码冻结**。批准范围仅网页端开发，未启动本地 Agent、未部署、未接管任何用户库。BASE `9b88513c360a70473ef1b340415fb1a81b979ff3`：09B bundle 克隆后 HEAD 与指定值一致，130/130 tracked 文件与 09B source ZIP 字节一致，工作树干净。原始 ZIP SHA256 `1edc58feabbe2e32cda9bfb2bd34f205f4cd3c4b0b3e008e5910002b6af6cc06` 未变。最终 HEAD 由交付清单/Git bundle 确定，不推测 SHA。

## 核心变更与证据

- 原始 `subroutine/index_strategy/service.py` `_load_series` → `evaluate_index` 的 NDX 分支调用 `load_ndx_aux` 并传入所选未复权 ETF series；`state.py` `save_ndx_aux` 只有定义、未找到调用写回；原 `_ndx_days_since_add` 取未来日列表长度，不能代表真实已完成间隔。`v9_rules.py` `evaluate_ndx_aux` 与 `config.py` 参数未修改：SMA200/20样本趋势、初始15%、目标21.5%、10个完成美股交易日、5000元增仓门槛、>25%减至24.5%、<0.99*SMA200退出、250000元缓冲。原纯函数描述美股开盘，国内 ETF 实际执行窗口按已批准 ETF 口径采用下一合法 CN 会话。保留独立 ATR NDX 的20日退出与动态G2 3/10日冷却；T5 不添加臆造的20日强退。
- 新 `NDX_AUX_T5` 标识区分 T5 和 ATR，不自动重标历史/普通仓。无原写回时，以实际首笔建仓锚定间隔、最近有效买入重置间隔；同日 US 时点不猜，保守计实际完成日。以真实成交及有效 lot allocation 重建循环，含已卖掉子批次。实际现金扣已有 ATR 计划需求后核实 25 万缓冲；核心槽位不计 NDX。无证据、过期、重复歧义、异账户、混合普通持仓、行情/现金不足均禁止已核实建议。建议不是资金预留，实际成交数量可偏离目标且保留；T5 证据是关联建议，不是证明每笔成交金额遵循目标。不自动下单。观察池先记录 core 计划再复核 T5 建/增仓建议；实际券商 ID 区分部分成交，真实重复请求幂等。原纯计算文件不改。
- 合成完整周期：15,000 股分笔、未满间隔 HOLD、增 6,500 股、升值超过25%后目标24.5%并实际卖2,500股、SMA退出剩余19,000股、零持仓；公共 pool/真实 SQLite 关闭重开后重建。错账户、手工未核实、重复、现金及同标的竞争测试。纯计算和服务新路径需要独立审查，不能当实网买卖结论。
- 两条真实迁移程序合成正例：证券、混合。独立预期股票1000股，本金390000分、费用500分、证券现金4609500分，市值420000分，证券权益5029500分；混合普通余额1550000分，总/净资产6579500分；证券单库总/净资产5029500分。有效成交1，混合账单/转账各1；独立同截止2026-02-11T16:00:00+08:00。合成行情独立写入用于估值（非实网），999股声明拒绝，真实报告对账、审批、安全接管、FastAPI启动、投影再次比较与旧代拒绝。混合真实报告增加中断注入，拒绝启动、显式回滚和应用重开验证。09B 纯财务正例留存，不重复执行无关测试。Windows 断电与真实源数据未执行。
- XSHG 锁定4.13.1、1990-12-03 至2026-12-31及全日程指纹，统一应用和策略日历；CLI 独立清单、覆盖、历史指纹、官方来源格式、明确审批。合成延展仅测试，正式可信终点未改变。来源真实性必须人工审查；未取得/导入真实2027官方数据。
- PRD/UI覆盖补充：成交组合过滤/可选审计详情（旧 GET 默认不变）、按月日级投资收益/详情、年度财务月入口、工作台风险/成交导航。日级收益一次 DB read 使用同一账本/行情版本；未知不为0；当前账本重算，不承诺研究级回放。页面未生产 typecheck/build；不能算浏览器通过。

## 实际命令及结果

```
PYTHONPATH=. python -m pytest -q tests/unit/test_09c_ndx.py tests/unit/test_09c_calendar.py tests/unit/test_09c_migration.py tests/unit/test_09c_history.py tests/unit/test_takeover.py tests/unit/test_strategy_service.py tests/unit/test_rc_decision_time.py tests/unit/test_09b_closure.py::test_ndx_cash_buffer_reference tests/unit/test_09b_closure.py::test_transaction_capacity_race tests/unit/test_09b_closure.py::test_real_trade_concurrent_request_id tests/unit/test_market_resolution.py::test_iss019_coverage_and_expiry tests/unit/test_trading.py tests/unit/test_pnl.py::test_golden_a_p02 tests/unit/test_pnl.py::test_p09_consistent_snapshot_and_master_revision
# 82 passed in 16.08s
python tools/verify_calendar.py # PASS; existing release
python -m compileall -q backend tools # PASS
git diff --check # PASS
```

中间测试保留日志：迁移初次夹具时区预期错误，修正独立 UTC 生效日后两例通过；T5初轮4 PASS（含09B现金缓冲），公共入口4 PASS；集中初轮75 PASS；真实报告中断+NDX+并发8 PASS；成交筛选首次脚本语法错误未写入实现，随后默认 GET 新增字段与原契约断言不符，改为显式 include_audit 后29 PASS；月度收益新增及受影响两例5 PASS。各组重叠，不相加。82 PASS 是本轮集中专项结果；之后补齐工作台风险/待办、持仓搜索、财务账单筛选、净资产趋势及摘要CSV、视图证据失效保护。最后受影响复测见下，不是全量或独立审查。实际日志独立交付。

最后补测实际命令：

```
PYTHONPATH=. python -m pytest -q tests/unit/test_09c_ndx.py::test_ndx_other_core_cash_buffer_after_initial tests/unit/test_09c_ndx.py::test_ndx_manual_sale_actual_allocation_rebuild tests/unit/test_09c_history.py::test_net_worth_trend_snapshot_and_unknown tests/unit/test_finance.py::test_golden_b_p04_p05_transfer_and_retry tests/unit/test_finance.py::test_iss012_and_networth_consistent_concurrent_snapshot tests/unit/test_finance.py::test_two_securities_consistent_quote_revision_and_missing_prices tests/unit/test_09c_migration.py
.........                                                                [100%]
9 passed in 3.98s
```

初次补测 1 FAIL / 8 PASS：手工卖出测试未指定真实策略批次，按现有契约进入待核对；改为明确分配真实 T5 批次、无关联建议的手工卖出，未放松交易校验，最终 9 PASS。含净资产一致性快照/未知值、现金缓冲、真实批次卖出及迁移复核。与集中组重叠，不相加。UI 源码已连接实际 API，未执行生产类型检查或浏览器。

## 尚未完成/风险/验收状态

- **剩余规格口径差距**：工作台接近止损的独立阈值、流动净资产独立定义尚未确定/实现；实际保护价和触发状态、总净资产均有入口，不擅自发明定义。UI风险与待办、搜索、净资产趋势及账单筛选入口已补齐。由独立审查及产品确认必要性和缺陷等级。未发现最后专项中可复现的未解决 S0/S1 数据破坏，但不能证明全范围无缺陷；上述口径未关闭，源码不无条件冻结。
- **环境 NOT RUN**：Windows 安装/启停/锁/断电，npm ci、vue-tsc、Vite、FastAPI dist、真实浏览器和双标签页、实网510300/159915/600519历史及实时、真实旧库、Tailscale 双机、完整独立回归。历史 09 npm ENOTCACHED 与实网六项 FAIL 保持，09C 未重试，不删除类型检查。真实2027官方日历待取得及审查。没有运行包/旧 dist。
- **已批准延期**：完整历史配置/研究级账本回放、Fed、分钟及高级图表、组合优化、高级加密异地备份、V2候选批量导入/复盘/预算等。动态冷却及正常策略规则不可用延期名义移除。
- 任何环境试验仅合成库。回滚源码：从本轮 bundle checkout BASE 到新副本（不对运行库执行 reset/迁移）；真实接管或恢复另需显式授权与写端实际隔离，遵循 09C_LOCAL_ACCEPTANCE。当前停止修改，提交独立审查，不开启新阶段。
