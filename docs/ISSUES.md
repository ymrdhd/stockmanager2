# 已确认问题库

严重程度、最终修复优先级及审查结论由产品负责人/独立审查者裁决；下表记录实际现象与已批准的计划边界，不替代验收结果。

| ID | 现象 | 影响 | 计划修复版本/阶段 | 状态 |
|---|---|---|---|---|
| ISS-001 | Python uv.lock 与 frontend/package-lock.json 尚未生成（00 依赖源访问失败） | 无法证明可复现安装 | V1.0 交付前 | CLOSED（04C 按产品负责人指示关闭）；07 实测网络恢复后 `uv lock` 生成 uv.lock（254,407 字节，requires-python >=3.11，42 包，exchange-calendars 4.13.1 锁定）、`uv sync --locked --python 3.11` exit=0 且 fastapi/exchange_calendars/sqlalchemy/alembic 可导入；`npm install --package-lock-only` 生成 frontend/package-lock.json（51,829 字节）、`npm ci` exit=0（52 packages）。04C 未新增第三方依赖（腾讯/中证适配器只用既有 httpx），两个锁文件无需重生成。注意当前 Node v24.19.0 与 package.json engines `>=22.13.0 <23` 不符，npm 仅 EBADENGINE 警告 |
| ISS-002 | Vue 完整类型检查、生产构建、浏览器验收未完成 | 完整 Web 交付未验证 | V1.0 前端阶段 | FIXED，待独立审查；07 修复 client.ts `WriteAttempt` 构造器 `newId` 的 UUID 模板字面量推断（改为显式 `() => string`）后 `npm run typecheck` exit=0、`npm run build` exit=0（vite 7.3.8，dist/index.html + assets/index-*.css 2.31 kB + index-*.js 123.39 kB，22 modules）；真实浏览器验收 21/21 PASS（Edge 158.0.4370.33 headless + CDP，服务由 FastAPI 直接托管 dist） |
| ISS-003 | Ruff/mypy 尚未完整执行，当前仍未安装 | 全仓静态检查未验证 | V1.0，环境具备后补齐 | 部分完成；07 依赖装齐后实际执行：ruff 与 mypy 均已安装（ruff 0.17.6、mypy 1.20.2）。本轮新增/改动文件（start.py、backend/config.py、backend/main.py、backend/security/*、backend/api/auth.py、backend/api/requests.py、backend/infrastructure/{authfile,database}.py、backend/api/trading.py、tests/unit/test_security.py）`ruff check` 全部通过、`mypy` 无告警。全仓仍有既有违规未清理（ruff 433 项，多为 tools/ 与旧测试的 UP017/I001；mypy 28 项，26 项在既有 application 层 portfolio/finance/market），不在本轮范围，仍 OPEN |
| ISS-004 | 日志 basicConfig 在已有 handler 时可能不生效 | 配置的日志级别可能未应用 | V1.5（独立审查批准延期） | OPEN |
| ISS-005 | Windows 运行及 OS 文件锁未实际验证 | 目标部署平台兼容性待确认 | V1.0 正式部署阶段 | FIXED，待独立审查；07 在 Windows 11 实机 12/12 PASS（`artifacts/tmp/windows_check.py`）：中文含空格安装路径（`中文 目录 with space`）、从无关工作目录用绝对数据库路径启动、未认证 status 401、health 200、同库第二写实例被拒（`writer lock unavailable`）且首实例不受影响、正常停止与 kill -9 后均能重启（未删任何 sidecar）、重启后 database_uuid 一致、`check-database` 报告身份、中文路径下独立沙盒创建成功、`ZoneInfo('Asia/Shanghai')` 可用。附带修真实缺陷：`check-database` 原抢占 writer lock 导致正式服务运行时无法自检，已改为 `inspect_database` 只读诊断 |
| ISS-006 | 普通启动参数原可重选另一份 formal 库 | 正式身份可能漂移 | 02-trading-core | CLOSED，独立审查确认；`test_iss006_fixed_binding_cli_and_service` 与 `test_explicit_cli_upgrade_and_maintenance_lock` |
| ISS-007 | 读上下文原为自动提交，跨查询快照不一致 | 持仓、现金及版本可能混读 | 02-trading-core | CLOSED，独立审查确认；`test_iss007_read_snapshot_across_concurrent_commit` 与 `test_actual_trade_reads_share_snapshot` |
| ISS-008 | 历史成交补录 pending 原无法确认，历史更正无法重放 | 历史持仓、成本及收益无法重算 | 03-pnl | CLOSED，独立审查确认；`test_golden_e_iss008_replay_and_void`、`test_historical_sell_replay_cash_once_and_cost_conservation`、`test_specified_reference_replay_failure_atomic` |
| ISS-009 | 期初现金以前的补录可能被重复加总 | 现金、权益及收益失真 | 03-pnl | CLOSED，独立审查确认；`test_iss009_cash_boundary_and_iss010_future`、`test_legacy_prebaseline_cash_is_not_double_counted` |
| ISS-010 | 原可录入未来成交或资金事实 | 有效账本可能包含未发生交易 | 03-pnl | CLOSED，独立审查确认；`test_iss009_cash_boundary_and_iss010_future`，可注入 UTC 时钟 |
| ISS-011 | 无正式绑定时不能初始化独立本地沙盒 | 独立本地模式首次启动受阻 | 第 07 轮（已指定） | FIXED，待独立审查；07 解除两处强制依赖：`initialize_database(role='sandbox')` 在无 official.json 时不再报错（`bound=None` 时退回显式 `--official-database` 或默认路径做冲突比对），`validate_binding` 的 local 分支不再要求正式绑定存在，只拒绝「打开正式库本体或其同 inode 别名」。`start.py` 无绑定时要求 `--official-database/--uuid/--generation` 三者齐全才能进入 local 模式。`test_sec08_standalone_local_sandbox_without_formal_binding` 实测：全新目录无 official.json 可创建 sandbox 库、可写出自己库、`instance_role='sandbox'` 且正式库路径始终不存在；`test_sec09_local_mode_never_opens_the_formal_database` 实测同路径/硬链接/正式 UUID 带 writable_sandbox 均被拒且正式库未受损 |
| ISS-012 | 主数据新增原不递增账本版本 | 查询或缓存可能过期 | 03-pnl 及财务功能 | 证券主数据已修复；`test_p09_consistent_snapshot_and_master_revision`；财务新增/账户更新/分类及写入均递增 revision；`test_iss012_and_networth_consistent_concurrent_snapshot`、`test_golden_c_f04_f05_asset_once`；CLOSED，05 独立审查确认证券和财务版本保护通过 |
| ISS-013 | 调用者可传入更早的交易日绕过缺失昨收 | 当日收益错误 | 04-strategy | CLOSED，独立审查确认；`test_iss013_real_calendar_and_coverage`、`test_iss013_missing_actual_previous_close`、`test_iss013_calendar_unavailable_safe_degradation`；日历有效覆盖至 2026-12-31，超出范围安全降级 |
| ISS-014 | 周末及开盘前正常收盘价被视为不可用 | 休市权益及收益无法正确显示 | 04-strategy | CLOSED，独立审查确认；`test_iss014_weekend_preopen_and_dividend`、`test_iss014_lunch_and_after_close_quality` |
| ISS-015 | 观察时间混同行情生效时间与实际获取时间，迟到收盘价不能历史重算 | 历史收益、行情版本及策略可获得性不清 | 04-strategy | 证券部分 CLOSED（历史独立审查）；策略部分待审查：追加式行情修订、历史获取时间截止、OHLCV/来源/同组输入及配置指纹；历史日期只读不联网。完整历史配置快照、逐 bar 全覆盖仍未验收。 |
| ISS-016 | 非成交资金、分红及期初记录缺少更正机制 | 误录后无法安全更正相关事实 | 第05/08轮（已指定） | 日常部分 CLOSED（05）；本轮期初现金与期初持仓更正/作废、后续成交重放、幂等及冲突回滚专项通过，复杂依赖拒绝并保留人工核对。待审查。 |
| ISS-017 | 未处理数量型公司行为缺少实际调整及解除异常入口 | 相关持仓及收益持续不可核验 | 第08轮（已指定） | 拆分/合并保留批次与总成本，1拆2 100→200 股、成本100000分、无虚假收益；尾差独立现金事实，冲销保审计；策略尺度失效保护。配股等未建模仍保留缺口，复杂调整拒绝。待审查。 |
| ISS-018 | 正式收盘价冲突后无法人工确认恢复 | 可信权益、日收益持续不可用 | 04B-strategy | CLOSED，独立审查确认；`test_iss018_resolution_preserves_evidence_and_temporal_versions`、`test_resolution_write_failure_keeps_revision_and_conflict`、`test_resolution_http_and_reopen` |
| ISS-019 | XSHG 日历有效期限至 2026-12-31 | 超期限无法核验交易日、行情及收益 | V1.0 正式发布前（已指定） | OPEN（**仍为发布阻断**）。08 已删除正式决策路径的 `weekday_fallback`：`MarketClock.is_trading_day` 在日历不可用或超出覆盖范围时抛 `CalendarCoverageError`，策略返回真实不可用原因，不生成正式信号、不猜下一交易日、不伪造冷却完成日（`test_calendar_out_of_coverage_never_invents_a_session`）。已提供覆盖起止日期、剩余日数及60天临近提示；`test_iss019_coverage_and_expiry`。2026-11-24 核验上交所休市公告页未找到 2027 正式通知；PyPI 4.13.3 的 XSHG 源码仍止于 2026，未升级。发布前须核对交易所公告、更新可信日历库与依赖锁，并测试新覆盖及节假日边界 |
| ISS-020 | 普通沪深成交可在非交易日或异常时段有效入账 | 错误持仓、现金和收益 | 04B-strategy | CLOSED，独立审查确认；`test_iss020_invalid_facts_pending_and_explicit_date_correction`、`test_invalid_sell_and_calendar_unavailable`；异常事实 pending、确认受同一校验约束，已有历史有效成交不自动修改 |
| ISS-021 | 正式 V9/G2 引擎及原新规则对照缺失 | 观察池及正式决策不可用 | 04C-strategy | 旧纯计算模块保留，04C 历史黄金证据不重跑；本轮修复 ETF/G2 口径、真实持仓及历史证据，仍待独立审查。不可据此批准发布。 |
| ISS-022 | 真实东方财富接口未取得有效响应 | 实际行情准确性与连通性尚未验收 | V1.0 真实行情验证 | 腾讯证券 QuoteProvider 接入及固定夹具通过。交接报告中的实网结果为历史记录；本轮 2026-11-24 22:43 北京时间 510300/159915/600519 spot/daily 六项均失败（首次传输失败，其余冷却），未核实真实行情，OPEN。 |
| ISS-023 | 已包含手工余额的转账更正原被拒绝 | 已核实错误转账无法整体撤销 | S1，第06轮（已指定） | CLOSED，06 独立审查确认；`test_iss023_included_transfer_void_golden`、`test_iss023_replacement_atomic_failure` 通过，保留两端证据、最新余额ID、防重、历史缺口及事务回滚 |
| ISS-024 | 新证券账户缺生效日期影响历史统计 | 历史净资产可能包含尚未存在的账户 | S2，第06轮（已指定） | CLOSED，06 独立审查确认；`test_iss024_effective_and_scope_history` 通过；旧未知日期不猜测，显式核实；统计范围事件及误创建保护，关闭不隐藏余额 |
| ISS-025 | 写入请求仅在内存保存 pending，刷新页面后生成新 request_id 可能重复入账 | 网络中断或刷新页面可能导致重复成交 | S1，第04C轮（已指定） | 待独立审查：同源单 Web Lock、锁内重读、原请求 ID 比较清除、跨库跨代拒绝、持久化失败阻止写入。前端测试首轮两项异步夹具失败，定向复测通过；真实双标签页浏览器仍未验收。 |
| ISS-026 | 期初持仓、现金分红及冲突收盘价核实缺少前端入口 | 重要业务只能走 API，日常无法使用 | S1，第07轮（已指定） | FIXED，待独立审查；新增 `openingForm`/`dividendForm`/`dividendCorrectionForm`/`quoteResolutionForm`，交易页加「期初持仓录入」「录入现金分红」及分红记录表（更正金额/作废），投资页加「冲突收盘价核实」块（查询报价证据、仅对 quality='conflict' 提供核实采用）。复用 OperationForm/PagedRecords，未复制交易或收益计算。真实浏览器实测 5 项 PASS（期初 200 股/700.00 元、分红记录含更正作废按钮、核实价格 3.5000、报价证据可见） |
| ISS-027 | 前端当前行情不自动刷新，只探活健康接口 | 长期开着的页面显示过期行情 | S2，第04C轮（已指定） | FIXED，待独立审查。20 秒 HTTP 轮询保留（不引入 WebSocket），04C 补齐断线/过期语义：状态分为 `live/stale/unavailable` 并在页面顶部以 `role="alert"` 提示；轮询失败区分 401（会话过期）与其他网络失败，提示文案明确「下列数字是 X 时刻读取的旧值，不是当前值」；成功刷新后回到 `live`；`status.mode==='development'` 直接标 `unavailable`；轮询在 `blocked`（有待核实写入）时跳过；会话恢复后先 `/system/status` 比对数据库身份，身份变化不自动确认新库；正在填写的表单不被刷新覆盖（重组不重置 form）。`test_recovery_*` 与 pending 专项测试覆盖 |
| ISS-028 | 前端 system/status 预检查不能代替服务端写入保护，写请求未绑定数据库身份 | 旧页面或错误库连接可能向非预期库写入 | S1，第07轮（已指定） | FIXED，待独立审查；写入请求携带 `x-database-uuid` 与 `x-database-generation`，中间件在业务写入路径解析后通过 `ContextVar` 声明身份，`Database.transaction()` 在 `validate_identity` 之后、yield 之前调用 `require_declared_identity()`，不匹配抛 `IdentityMismatch`（`invoke()` 映射为 409 `identity_mismatch`，不写任何业务事实）；缺身份头 428 `identity_required`、格式非法 400 `identity_malformed`。`test_sec06_write_identity_must_match_the_open_database` 覆盖无头/非法/他库 UUID/stale generation，事后 accounts 与 ledger_requests 均为 0；`test_sec07` 覆盖未确认请求重试路径；`test_sec10` 覆盖正式绑定损坏时写入被拒 |
| ISS-029 | `start.py local` 在没有正式绑定的全新安装上强制要求 `--official-database/--uuid/--generation`，独立沙盒无法启动 | 新用户无法使用可写沙盒验证功能 | S2，第04C轮（已指定） | FIXED，待独立审查。`start.py` 新增 `_sandbox_identity(path)`：只读打开沙盒库、`read_identity()` 取自身 UUID/generation，并要求 `instance_role=='sandbox'`（拒绝把正式库当可写沙盒）；`_local_settings` 在 `load_binding()` 失败时不再强制三参数——`official` 缺省取 `DEFAULT_DATABASE` 仅用于同文件冲突比对，`--uuid/--generation` 显式给出则优先使用，否则用沙盒自身身份。已有正式绑定时仍走原隔离与路径别名保护；损坏正式绑定仍拒绝（不自动建库、不绕过身份检查）。实跑验证：`init-database --database artifacts/tmp/iss029.sqlite --role sandbox --confirm` → `{"instance_role":"sandbox","generation":1,"schema_version":8}`；`start.py local --database <sandbox> --writable-sandbox --no-market-refresh --port 9099 --auth-file … --static-dir … --allow-missing-web-build` 无正式绑定启动成功，`GET /api/v1/health` → `{"status":"ok"}`，登录后 `/api/v1/system/status` → `{"mode":"sandbox","writable":true}` |
| ISS-030 | 部署文档中的 Tailscale Host 配置与 HTTPS 实际访问端口不一致 | 按文档配置会导致 Host 校验失败或被迫放宽白名单 | S3，第04C轮（已指定） | FIXED（文档层），待独立审查。`docs/OPERATIONS_SECURITY.md` 改为：`set-password --host 127.0.0.1:9091 --host <机器名>.<tailnet>.ts.net`（经 Tailscale Serve 的 HTTPS 走 443，浏览器 Host 头不带端口，故登记裸主机名，不写 `:9091`/`:443`），并说明只有确实以非默认端口访问时才登记 `主机名:端口`；家用机访问明确在公司电脑执行 `tailscale serve --bg --https=443 http://127.0.0.1:9091`、浏览器打开 `https://<机器名>.<tailnet>.ts.net/`；不使用通配符放行；明确服务端只比对 Host/Origin 头本身，**不采信 `X-Forwarded-Host`/`X-Forwarded-Proto`/`X-Real-IP` 等可伪造代理身份头**（与 `backend/security/policy.py` 的 `host_allowed`/`origin_allowed` 精确比对实现一致，代码里无任何 Forwarded 处理）。**双机现场验收仍 NOT RUN**，留到正式部署 |
| ISS-031 | 策略把指数点位日线当成 ETF 价格口径：`StrategyService.evaluate` 无条件调 `index_series`，产出 `price_basis=index_level` 而 `price_unit=元`，ATR 被前端直接预填进人民币真实成交表单 | 指数点位 ATR（如 2.35 点）与 ETF 成交价混算，BuyMax/止损/冻结 ATR 全部错尺度，可导致错误真实成交 | 08-integration-and-migration（已指定） | 默认 V9 使用旧 fqt=0 ETF 未复权日线，G2 用真实指数点位；夹具 4000 点/ATR50 与 4元/ATR0.05 隔离，BuyMax 4.025元；0010 保留并标记旧指数点位+元信号无效，取消对应计划。待审查。 |
| ISS-032 | 真实策略买入后 `custody` 有批次但 `position_rows.items` 为 0、`core_positions` 为 0；`_advance` 把模型退出写成 `exit_filled` 并启动冷却 | 策略页看不到真实持仓、组合槽位与冷却被模型建议错误占用，可能多开仓位 | 08-integration-and-migration（已指定） | 真实批次驱动，显式 signal_id 核验账户/标的/时点/尺度，缺证据仍保留成交。模型退出不改真实仓位；已确认实际卖出才按事实重建冷却，退出原因不明阻止新计划，可更正卖出，作废后消除冷却。复杂多主仓保守待核对，完整旧策略执行状态待审查。 |
| ISS-033 | `StrategyStore` 的 `_signal_id`/`_plan_id`/`active_plan`/`clear_plan` 不含 `account_id`，`strategy_signals`/`strategy_plans` 唯一键也不含账户 | A 账户的计划会让 B 账户错误进入 `PLAN_PENDING`，A 取消会连带取消 B | 08-integration-and-migration（已指定） | 账户进入信号/计划唯一键，按账户查询取消；已用信号不得复用旧计划，隔离夹具通过。待审查。 |
| ISS-034 | `_payload_hash` 只含 `index_id|basis|last_date|len(bars)`（不同价格同版本）；`strategy_bars` 覆盖式更新无法留历史；`config_version` 用策略版本号冒充 | 价格修订后仍复用旧信号版本，历史策略可获得性不可追溯 | 08-integration-and-migration（已指定） | OHLCV/来源/实际版本/peer 与 G2 输入和配置指纹；追加式 revision、获取截止、GET 无联网写入；未知获取时间不猜测。历史日期按日末截止，尚无完整历史配置重放验收。待审查。 |
| ISS-035 | 正式决策路径存在 `weekday_fallback`（`calendars.py` 用 `weekday()<5` 兜底），日历不可用/超范围时仍产出正式信号与下一交易日 | 用系统本地日期冒充交易日，产生虚假信号与冷却完成日 | 08-integration-and-migration（已指定） | 去除正式决策 weekday fallback，实际市场完成时间和可信日历闸门；超范围拒绝，ISS-019 仍 OPEN。待审查。 |
| ISS-036 | `_peer_series` 返回 `{etf_code: {'closes': [...]}}`，而 `evaluate_factor` 要求 `Dict[str, Optional[IndexSeries]]`；NDX 退出期限按注释 20 日 | 因子计算类型不匹配，要么报错要么被静默降级为「因子缺失」 | 08-integration-and-migration（已指定） | peer 类型 IndexSeries、保留原因子和 NDX 规则，未核实/过期/G2 未知阻止正式信号。固定夹具通过，未验证全部实网输入。待审查。 |
| ISS-037 | 备份 manifest 缺 `ledger_revision`/`quote_revision`/文件大小；没有恢复演练命令；恢复无法拒绝活写者与旧 generation | 无法证明备份对应哪个账本版本，恢复操作可能覆盖运行中的正式库 | 08-integration-and-migration（已指定） | manifest、restore-check/restore；维护锁和绑定锁覆盖发布，校验哈希、角色、UUID、schema、版本、完整性/外键；失败回退，旧 generation 拒绝。同估值时点净资产1305428分一致。跨文件切换非原子，故障需维护核对，待审查。 |
| ISS-038 | 跨标签页写请求无互斥：两标签页在 `storage` 事件到达前都能通过 pending 检查并各写一条；持久化存储不可用时退到内存 | 同一笔真实成交可能被提交两次 | 08-integration-and-migration（已指定） | 同源全身份共用互斥锁；锁内重读并比较 request_id，存储不可用安全拒绝，跨库跨代不得清除旧记录；竞争与丢响应夹具通过，实际浏览器未验证。ISS-025 不关闭。 |

## 09 任务编号映射（不覆盖上表历史 ISS-037/038）

| 09 任务 | 当前处置 | 状态 |
|---|---|---|
| 09 ISS-037/038 当前/历史决策 | 显式模式、完成日/当前账本截止分离、历史只读和获取截止；完整历史配置与更正双时态未建立 | PARTIAL，发布阻断 |
| 09 ISS-039 迁移账户日期 | 删除任意默认日期；证据/推断/未知分开，推断必须确认 | PARTIAL，待真实三类样本 |
| 09 ISS-040 对账 | 实际持仓数量差异、运行协议/来源校验、正式投影及版本、历史阻断保留；源同截止估值、完整费用/资金/资产逐项勾稽仍缺 | PARTIAL，禁止接管 |
| 09 ISS-041 接管 | 未实现新迁移接管命令；现有备份恢复专项通过，不能替代接管验收 | OPEN，禁止接管 |
| 09 ISS-042 冷却/组合 | 原始调用链为信号日动态 RANGE，保留规则；组合候选汇总阻断；NDX 辅助仓真实仓位及现金缓冲、跨请求组合容量验证未完成 | PARTIAL |
| 09 ISS-019/022/025 与历史 ISS-037/038 | 日历期限和实网未解；前端单元、备份/身份专项通过，但无生产构建、真实浏览器、Windows/双机、本轮备份保留与异地加密全流程验收 | OPEN / 待审查 |

以上为开发者实测记录，不作独立审查关闭裁决。

## 09B 任务对应关系（历史记录不改，非关闭裁决）

| 任务/历史对应 | 09B 实际结果与待验收 |
|---|---|
| 09 ISS-041 接管 / 历史 ISS-037 备份身份 | CLI 实现，合成审批/报告变化/活写/中断/回滚 PASS；Windows 断电、真实正式库未验收；保留 OPEN 待独立审查 |
| 09 ISS-039/040 迁移日期及对账 | 独立声明绑定、有效事实和金额对账、关键差异阻断；纯财务合成正例 PASS，证券/混合阻断夹具 PASS，干净证券/混合与真实库待验收，PARTIAL |
| 09 ISS-042 策略容量/冷却/NDX | 动态冷却不变；跨请求事务槽位竞争与真实成交并发幂等 PASS，信号/计划同事务；NDX 辅助建议不可执行，完整原规则增减仓周期待验收，PARTIAL |
| 09 ISS-037/038 时点 / 历史 ISS-038 和 ISS-025 防重 | 历史配置与研究回放延期，证据不足不可执行；后端同 ID 并发专项 PASS，真实浏览器双标签 NOT RUN |
| ISS-019 日历 | 可信覆盖展示/到期专项 PASS，未增加2027覆盖，OPEN |
| ISS-022 真实行情 | 09 六项 FAIL；09B 不重试，独立脚本本地执行，OPEN |
| 生产构建/Windows/远程/演示 | 脚本和合成 demo 已实现，安装、类型检查、build、真实浏览器与双机 NOT RUN；不交付运行包，OPEN |

完整历史配置、研究级回放、高级加密异地备份、复杂优化及美化按 09B 用户授权延期，前提是不误导当前或历史输出；不可将原策略必需功能以延期删除。

## 09C 任务对应关系（开发证据，不覆盖旧编号、不作关闭裁决）

| 原任务 | 09C 开发结果及剩余 |
|---|---|
| 09 ISS-039/040/041 | 证券及混合实际迁移程序、独立逐项对账、审批、接管、应用启动、999股差异拒绝、真实报告中断回滚 PASS；09B纯财务正例保留，真实库/Windows未验收 |
| 09 ISS-042 | NDX T5独立证据及实际批次完整周期、公共pool、DB重开、现金/容量安全；G2动态冷却/ATR原算法保留；不预留资金，实网和前端未验收 |
| ISS-019 | 锁定XSHG日程指纹、独立官方清单更新审查入口、合成冲突拒绝；可信上限仍2026-12-31，真实2027资料待取得 |
| ISS-022、ISS-025、历史 ISS-037/038 | 09实网/依赖历史FAIL；09C实网、前端构建、双标签页、Windows与双机NOT RUN；接管绑定/中断回滚合成PASS |
| 09C-coverage（新发现，独立审查编号/等级待定） | 工作台所选账户风险待办、持仓搜索、净资产多点趋势/摘要CSV、账单筛选已补齐；接近止损独立阈值及流动净资产定义未明确/未实现；见功能矩阵。禁止宣告全部V1.0完成/无条件冻结 |

## 09D 独立审查编号及 09E 开发处置（不覆盖历史记录）

| 编号 | 缺陷/关卡 | 09E 状态 |
|---|---|---|
| 09D-001 S1 | 跨标签成功响应竞争重复成交 | FIXED，跨运行时合成账本/前端测试 PASS；真实浏览器 BLOCKED，待独立关闭；ISS-025/038 不关闭 |
| 09D-002 S1 | 偏离成交错误合规证明 | FIXED，真实合成账本重建及更正作废 PASS，待独立关闭 |
| 09D-003 S1 | 账户切换旧数据污染 | FIXED，实际 SFC setup 逻辑测试 PASS；真实浏览器 BLOCKED，待独立关闭 |
| 09D-004 S2 | NDX T5 证据误分类 | OPEN，未修复 |
| 09D-005 S2 | 会话恢复/重连/清理 | PARTIAL，未专项处理 |
| 09D-006 S2 | 收益摘要与详情版本 | PARTIAL |
| 09D-007 S2 | 证券名称搜索 | PARTIAL |
| 09D-008 S2 | 基础退款 | PARTIAL |
| 09D-009 S2 | 当日投资收益比例 | PARTIAL |
| 09D-010 S2 | 批次核验/账户差额/权益校正入口 | PARTIAL |
| 09D-011 S2 | 流动净资产口径 | PARTIAL，待产品确认 |
| 09D-012 S2 | 接近止损预警 | PARTIAL，待产品确认 |
| 09D-013 S2 | 一键备份/端口/重启提示 | PARTIAL |
| 09D-014 S2 | 期初数据分页 | PARTIAL |
| 09D-015 S2 | 普通恢复跨文件中断故障注入 | NOT RUN |
| 09D-016 S2 | 数据质量与解释 | PARTIAL |
| 09D-017 S3 | 审计/证据 JSON 可读性 | PARTIAL |
| 09D-018 环境 | 锁定前端构建与真实浏览器 | npm ci/typecheck/build FAIL，Vite NOT RUN，真实浏览器 BLOCKED |
| 09D-019 外部数据 | 2027可信交易日历 | BLOCKED，未改变覆盖 |

详见 `09E_REPORT.md`。历史独立审查裁决与其他问题不变；本轮无 CLOSED 裁决。


## 09F 开发处置（保留历史，不裁决 CLOSED）

| 编号 | 状态与证据 |
|---|---|
| 09D-001/002/003 | FIXED 保留，合成 API/账本及前端回归 PASS，浏览器NOT RUN，待审查 |
| 09D-004 | FIXED：T5独立证据分类合成PASS，普通ATR规则保留 |
| 09D-005 | PARTIAL：断线禁写/旧请求废弃、重新读取；全部会话/重连路径尚未验收 |
| 09D-006 | FIXED：版本绑定/冲突拒绝专项PASS，真实页面待验证 |
| 09D-007 | PARTIAL：真实供应方名称入口、合成元数据PASS；实网、历史名称迁移未验收 |
| 09D-008 | PARTIAL：关联原支出退款、跨期、幂等更正作废PASS；旧退款迁移及口径冲突待审查 |
| 09D-009 | 候选实现，基础收益率专项PASS，待产品确认 |
| 09D-010 | PARTIAL：现金差额入口与原审计，完整预览及旧批次核验工作流未完 |
| 09D-011 | PARTIAL：已知分类与不重复计算PASS，未知流动性分类缺配置，待产品确认 |
| 09D-012 | PARTIAL：展示阈值候选与距离测试PASS，实际主/子仓及过期行情完整测试待补 |
| 09D-013 | 备份API认证身份及去重PASS；页面和端口配置/重启说明实机NOT RUN |
| 09D-014 | FIXED：202记录分页、账本变化拒绝专项PASS |
| 09D-015 | 建议升级S1，FIXED：真实进程退出故障注入及回滚PASS，Windows待验收 |
| 09D-016/017 | PARTIAL：基础事实质量及成交中文审计表新增，逐金额来源/退款及策略详细审计不足 |
| 09D-018 | 环境BLOCKED，09F NOT RUN，不伪造生产构建 |
| 09D-019 | 官方资料BLOCKED，可信范围不变 |

见09F矩阵及报告；不新增正式发布授权。

## 09G-A 开发处置（不覆盖历史审查裁决）

| 范围 | 本轮实现与证据 | 当前状态 |
|---|---|---|
| 09D-010 内部批次/权益核验入口 | 新增 `GET /api/v1/portfolio/{account_id}/reconciliation`，同一只读快照检查有效卖出分配数量、剩余批次/持仓投影、证券现金、行情、自然日收益/权益勾稽、待核对成交及未解决公司行为；投资页按当前估值版本显示检查结果和异常，不自动改账。新增4项 Python 测试及2项实际 Workspace setup 测试。 | 内部账本预览已实现，外部券商/旧库核对及完整旧批次证据仍 PARTIAL；待独立审查 |
| 09D-018 前端构建 | 本轮 `npm ci --fetch-retries=0 --fetch-timeout=15000` 因 `registry.npmjs.org` DNS `EAI_AGAIN` 失败；正式 `vue-tsc`/Vite 未获依赖，不能以 Node 单元测试代替。 | BLOCKED，未生成 dist |
| 09D-011/012 产品口径 | 流动净资产未知分类和接近止损阈值仍保留 09F 候选口径，不擅自升级为已批准规则。 | 待产品确认/专项验证 |

本轮开发者不关闭 09D-010 历史问题，不裁决 S 级别或发布批准；详细证据见 `09G_REPORT.md`。


## 09H 开发处置（非独立关闭裁决）

| 编号 | 证据与剩余 |
|---|---|
| 09H-001 / 09D-005 | 3项原缺陷复现 FAIL 后修复；会话过期释放 loading、次查询401禁写、重连新 generation 显式确认；迟到401、候选再变化及退出也有自动化覆盖。FIXED（逻辑层），浏览器及独立审查待验。 |
| 09H-002 / 09D-017 | 09G 审计分支未并入交付 HEAD，恢复实际代码和测试（保留历史分支）。真实审计字段映射/原始JSON、中文原因；合成测试 PASS。退款及其他领域完整审计展示仍 PARTIAL。 |
| 09D-016 | 财务总览逐金额权威值、来源、截止、账本/行情版本，余额基线及报价证据，未知不填零；合成 PASS。非逐项历史来源全覆盖。PARTIAL。 |
| 09D-012 | 真实合成主/子仓、已触发/接近、旧版本、未核实及过期价验证 PASS；2%仅候选展示规则，产品确认和实际报价/页面待验。 |
| 09D-007/008/009/010/011/013 | 原功能与保护保留。真实样本、候选口径及实际界面/运维验证仍待完成；详见矩阵，未裁决关闭。 |
| 09D-018 | 本轮 npm ci NOT RUN（09G DNS FAIL）；实际 typecheck/build 前置 FAIL：vue-tsc Permission denied，127；Vite/真实浏览器 NOT RUN。 |
| 09D-019 | 未改变可信日历范围，真实官方资料仍 BLOCKED。 |

未更改原策略公式、财务算式或历史延期决策；新测试均用隔离合成库。
