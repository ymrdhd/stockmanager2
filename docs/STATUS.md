# 当前状态（每轮覆盖，不累计日志）

- 当前迭代：`04C-strategy-integration`（已完成，待独立审查）。基准提交 `8998f0f5ee35976e0063fb6ddc348b28a32ba933`（07-remote 审查通过后的工作区快照）。
- 完成：
  - **V9＋G2 正式策略迁移**：`backend/domain/strategy/`（config/indicators/regime/entry/exit/v9_rules/factors/factor_combo/market_data/calendars/symbols）逐字迁移旧源，仅 `market_data` 剥离网络层、`compute_atr14` 去外部依赖。14 指数个性化 N/C/A/B%、ETF 映射、NDX 独立轨、T3 窗口、BuyMax、G2 等权基准与 ER60、S1b+K6 加仓、止损/跳空/跟踪、第 21 日到期与 E2a 延持、3/10 日冷却、NDX 辅助仓 15→21.5→24.5% 全部按原函数口径。
  - **黄金对照实跑**：`golden_probe.py` 490 值、`golden_probe2.py` 76 值，mismatches 均为 0；期望值由旧函数产出后固化为 `tests/unit/test_strategy_golden.py`（34 条）。
  - **真实行情接入**：新增腾讯（12 只指数 + ETF 复权/未复权）与中证指数官网（4 只 CSI）适配器；东方财富指数路径本机不可达（ISS-022 根因），未伪装成功。实网已取到全部 14 只指数点位。
  - **策略服务与 API**：迁移 `0008_strategy.py` 六张证据表；`StrategyService`（观察池/单指数判定/持仓决策/冷却/NDX 辅助仓）与 `GET /strategy/{config,regime,pool,positions,signal,series}`、`POST /strategy/refresh`（只读账本、幂等、不下单）。
  - **策略前端**：投资页新增「V9/G2 策略观察池」与「持仓决策（策略建议 vs 实际持仓）」，含真实批次 custody 核对表、按信号预填真实成交表单（价格/数量/时间必须人工填）。
  - **ISS-025 闭环**：`WriteAttempt` 改用 `localStorage` 持久化（跨浏览器重启）、成功后置空但只在首次尝试且为**明确业务拒绝**时才清除、`check()` 强制校验 UUID+generation、跨标签页 `storage` 事件同步、存储不可用即拒绝发送、损坏记录阻止写入并可人工清除。
  - **ISS-029**：无正式绑定的独立沙盒可仅凭自身文件身份启动（`start.py local` 不再强制 `--uuid/--generation`），实跑验证通过。
  - **ISS-027**：断线/过期区分 `live/stale/unavailable`，旧值不再冒充当前值，恢复后重新校验数据库身份。
  - **ISS-030**：文档改用 Tailscale 实际私网 HTTPS 主机名（443 不带端口），明确不使用通配符、不采信可伪造代理身份头；双机现场验收仍 NOT RUN。
- 验收：`tests/unit/test_strategy_golden.py` 34 PASS；`tests/unit/test_strategy_service.py` 11 PASS；`test_strategy_golden + test_strategy_service + test_market + test_trading` 91 PASS；`test_database_upgrade + test_database` 22 PASS；`test_finance + test_pnl + test_market_resolution` 49 PASS；前端 `npm test` 29 PASS（15 既有 + 14 新增 ISS-025 专项）、`npm run typecheck` exit=0、`npm run build` exit=0（vite 7.3.8，dist 136.09 kB js / 2.31 kB css）。全量 pytest 与未受影响历史测试由独立审查者执行；本轮未跑。
- 阻塞：ISS-019（XSHG 日历止于 2026-12-31）仍为发布阻断；ISS-021 已在代码层落地待审查裁决；ISS-022 东方财富不可达，指数行情已改用腾讯/中证官网且真实取到数据，但证券现货接口连通性仍未解除；ISS-003 全仓 ruff/mypy 既有违规未清理（不在本轮范围）。
- 安全/交付：只用隔离合成数据库与一次性本地凭据（`artifacts/tmp/`，不入包）；未接触真实库；迁移仅在沙盒实跑升级，真实旧库迁移留到 08。增量包按本轮 BASE 生成、校验后停止。
