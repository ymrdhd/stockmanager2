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
| 09D-011 S2 | 流动净资产口径 | PARTIAL，口径已于 2026-11-25 产品批准（ADR-019），实现定向核对一致，见「10A」节 |
| 09D-012 S2 | 接近止损预警 | PARTIAL，口径已于 2026-11-25 产品批准（ADR-020），实现定向核对一致，见「10A」节 |
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
| 09D-009 | 候选实现，基础收益率专项PASS；口径已于 2026-11-25 批准（ADR-018），见「10A」节 |
| 09D-010 | PARTIAL：现金差额入口与原审计，完整预览及旧批次核验工作流未完 |
| 09D-011 | PARTIAL：已知分类与不重复计算PASS；口径已于 2026-11-25 批准（ADR-019），未知流动性分类仍阻止完整指标并提示待确认 |
| 09D-012 | PARTIAL：展示阈值候选与距离测试PASS；口径已于 2026-11-25 批准（ADR-020），09J 已补主/子仓及过期行情边界 |
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
| 09D-011/012 产品口径 | 流动净资产未知分类和接近止损阈值仍保留 09F 候选口径，不擅自升级为已批准规则。 | 历史状态；**2026-11-25 产品负责人已批准**（ADR-019/ADR-020），定向核对见「10A」节 |

本轮开发者不关闭 09D-010 历史问题，不裁决 S 级别或发布批准；详细证据见 `09G_REPORT.md`。


## 09H 开发处置（非独立关闭裁决）

| 编号 | 证据与剩余 |
|---|---|
| 09H-001 / 09D-005 | 3项原缺陷复现 FAIL 后修复；会话过期释放 loading、次查询401禁写、重连新 generation 显式确认；迟到401、候选再变化及退出也有自动化覆盖。FIXED（逻辑层），浏览器及独立审查待验。 |
| 09H-002 / 09D-017 | 09G 审计分支未并入交付 HEAD，恢复实际代码和测试（保留历史分支）。真实审计字段映射/原始JSON、中文原因；合成测试 PASS。退款及其他领域完整审计展示仍 PARTIAL。 |
| 09D-016 | 财务总览逐金额权威值、来源、截止、账本/行情版本，余额基线及报价证据，未知不填零；合成 PASS。非逐项历史来源全覆盖。PARTIAL。 |
| 09D-012 | 真实合成主/子仓、已触发/接近、旧版本、未核实及过期价验证 PASS；2% 已于 2026-11-25 批准为风险预警阈值（ADR-020，不改变策略规则），实网/浏览器表现仍待验。 |
| 09D-007/008/009/010/011/013 | 原功能与保护保留。真实样本、候选口径及实际界面/运维验证仍待完成；详见矩阵，未裁决关闭。 |
| 09D-018 | 本轮 npm ci NOT RUN（09G DNS FAIL）；实际 typecheck/build 前置 FAIL：vue-tsc Permission denied，127；Vite/真实浏览器 NOT RUN。 |
| 09D-019 | 未改变可信日历范围，真实官方资料仍 BLOCKED。 |

未更改原策略公式、财务算式或历史延期决策；新测试均用隔离合成库。

## 09I 开发处置（非独立关闭裁决）

| 编号 | 证据及剩余 |
|---|---|
| 09D-008/017 | 只读账单、退款、账户、余额及转账审计，原支出/有效退款/直接更正链接、账本版本及身份；合成与认证专项 PASS。旧库退款映射仍需真实库证据核对（口径本身已批准，见 ADR-017）。 |
| 09D-016 | 财务月逐笔退款冲减/原支出期、历史估值金额证据、年度截止/版本显示；当前事实重算非知识回放。PARTIAL：实际界面及真实历史数据验收待完成。 |
| 09I-001 | 新增财务历史查询代次及详情切换隔离，旧响应、断线、账本/身份变化保护自动化 PASS，待浏览器与审查。 |
| 09D-013 | 端口输入与受控启动命令预览及重启提醒，Python/PowerShell参数校验；网页不保存服务配置、不执行重启，不冒充实机成功。Windows实测与远程 Host/转发配置待验。 |
| 09D-009/011/012/008 | 09F 产品口径已于 2026-11-25 正式批准（ADR-017～ADR-020）；未知流动性分类仍阻止完整指标，原策略计算不变。 |
| 09D-018 | typecheck/build 前置 FAIL：vue-tsc not found（127）；npm ci 本轮 NOT RUN，历史环境限制；Vite/浏览器/独立全量回归 NOT RUN。 |
| 09D-019 | 已锁定日历指纹核验 PASS；可信覆盖未延长，真实2027官方资料仍待取得。 |

未新增已确认S0/S1裁决；历史FIXED不自行关闭。真实库退款/名称映射不可无证据迁移，仍以真实旧库独立对账为门槛。

## 09J 开发处置（非独立关闭裁决）

| 对应问题 | 本轮实际处理 | 当前状态 |
|---|---|---|
| 09D-012 接近止损证据 | 新增 12 项定向测试：真实临时 SQLite 与 `PortfolioService.quote` 报价写入/读取，验证主仓和子仓分别计算、真实过期盘中价、报价冲突/缺失、日历不可用、未核实信号、策略偏离、价格尺度失效、账本版本不一致、非真实/已关闭模型持仓、200 bps 阈值外及保护价触发边界。 | 自动化边界覆盖 PASS；2% 仍为未批准显示候选；实网/浏览器待验 |
| 09D-018 前端构建 | 本轮只改 Python 测试，无前端源码变更；09I 已记录 `vue-tsc` 缺失导致完整 typecheck/build 失败，Vite NOT RUN，浏览器 NOT RUN。 | BLOCKED / NOT RUN，未重试 npm 安装 |

本轮未更改策略公式、止损价格、保护动作、报价质量判定或真实成交。所有数据库均为临时合成库。测试证明的是显示层警示边界，不代表 200 bps 产品阈值获批，不自动生成卖出或下单。开发者不将 09D-012 标记 CLOSED。

## 09J 最终网页收尾补充（非独立关闭裁决）

| 编号 | 证据与剩余 |
|---|---|
| 09J-001 / 09D-005 | 建议S1安全边界：当前子查询网络超时仍允许写入，真实页面setup复现 FAIL；统一请求连接失败禁写、分页列表上报、旧代次拒绝，新增3项最终PASS。FIXED（逻辑层）；真实浏览器及独立审查待验，未自行CLOSED。 |
| 09D-001～017 | 09I功能矩阵与候选口径保留；全量338 PASS，前端修改后18项安全/审计专项及63项Node测试PASS，重叠不相加。不是逐项关闭裁决。 |
| 09D-018 | 本轮正式 typecheck/build 前置 FAIL（vue-tsc not found，127），Vite/真实浏览器 NOT RUN。限定普通TS及自动化PASS不替代生产编译。 |
| 09D-019 | 现有日历指纹PASS，2027官方资料未导入；可信截止不变。 |

## 10 本地验收处置（开发者实测，非独立关闭裁决）

| 编号 | 现象与证据 | 影响 | 处置/状态 |
|---|---|---|---|
| 10-001 | Windows 上对只读句柄调用 `os.fsync` 抛 `OSError: [Errno 9] Bad file descriptor`（`backup.py`/`restore.py`/`takeover.py`），首次全量回归 42 FAIL（备份、恢复、接管、升级迁移、每日保留） | S1：Windows 目标平台的备份/恢复/接管全部不可用 | FIXED：新增 `backend/infrastructure/durability.py::fsync_file`（`r+b` 句柄，Windows `FlushFileBuffers` 需写权限），三处替换。定向下 38+96 项 PASS，最终全量 339 PASS。待独立审查 |
| 10-002 | 收盘后刷新只取日线，日线不含名称；`instrument_name` 仅盘中 `spot` 返回，导致名称长期为 null、名称搜索失效（实测合成库 3 个标的 name 全空） | S2：V1.0「真实证券名称」不可用 | FIXED：`TencentProvider` 增加进程内名称缓存并在 `closes` 最新 bar 附加 `instrument_name`；失败不影响价格证据。新增 `test_closes_attaches_provider_name_...`；实网写入「沪深300ETF华泰柏瑞」「创业板ETF易方达」「贵州茅台」。待独立审查 |
| 10-003 | `tools/verify_09f_browser.py` 用 `get_by_label(..., exact=True)`；Chromium 141 的可访问名包含 `<select>` 选项文本，定位超时 | 工具：真实浏览器防重用例无法执行 | FIXED：仅改为非精确标签定位，按钮仍 `exact=True`；真实双页面用例 PASS。不改业务断言 |
| 09D-018 | 生产构建与真实浏览器 | 发布阻断 | **本轮 PASS**：`npm ci` 52 包、`vue-tsc --noEmit` exit 0（修复 `WorkspacePage.vue` strategyRows 推断类型 TS2339）、`vite build` exit 0、`npm test` 63 PASS；Chromium 141 真实浏览器扩展 16 项 PASS（另 09F 双标签 1 项）。待独立审查 |
| ISS-022 | 真实行情 | 发布阻断 | **本轮实测通过**：腾讯源 510300/159915/600519 六项 spot/daily 均 valid、未复权、时点正确；名称已写入。历史 OPEN 由独立审查关闭 |
| ISS-019 / 09D-019 | 2027 可信日历 | 发布阻断 | **BLOCKED**：2026-11-25 抓取上交所「休市安排」官方页仅有 2026 年公告与安排，无 2027 资料；不猜测、不更新锁文件，到期安全闸门保留 |
| 新增 | Tailscale 私有 HTTPS 与双电脑 | 发布阻断 | **BLOCKED**：本机未安装 Tailscale，无第二台设备与公司许可；未做 Serve/Funnel 配置 |
| 新增 | 真实旧库只读试迁移与逐项对账 | 发布阻断 | **BLOCKED**：本轮无用户授权的真实库一致性副本；仅合成夹具与自动化覆盖，未宣称真实迁移通过 |
| 观察-1 | 超量卖出且费用留空时，待核对原因为 `unknown_fees`（先于 `insufficient_lots` 判定）；记录为 pending，不生效、不扣批次 | 提示原因不够直观，但拒绝生效路径安全 | 仅记录，待产品/审查决定是否调整原因优先级；不改校验 |
| 观察-2 | 沙盒模式点「一键备份」返回 403 后，页面进入「禁止写入」直至显式重新读取 | 安全侧 fail-closed；交互略重 | 仅记录；正式服务模式一键备份实测 PASS |
| 环境 | npm 10.9.7（脚本 `Assert-Node` 锁定 10.9.2） | `windows.ps1 -Action Install/Build` 会拒绝 | 仅记录；本轮按任务书手工 `npm ci`/`build`，未改环境守卫、未降级断言 |

## 10A 四项口径正式批准后的定向核对（开发者实测，非独立关闭裁决）

批准文本见 `DECISIONS.md` ADR-017～ADR-020（2026-11-25 产品负责人）。本节记录逐条核对结论，不关闭任何人历史编号，也不构成发布授权。

| 口径 | 核对位置 | 结论 | 备注 |
|---|---|---|---|
| 退款冲减原支出期、到账单独记录 | `backend/application/finance.py::_month`（`refund_against_original_fen`、`refund_received_fen`、`bill_cash_flow_fen`、`original_expense_period`）、`record_detail` | **一致，未改代码** | `test_refund_original_month_and_receipt_cashflow` 已覆盖跨期冲减、到账月现金流、累计上限、更正与作废 |
| 当日收益率两位小数 | `backend/application/portfolio.py:1313`、`backend/application/finance.py:701` | **差异已修复**：`.6f` → `.2f` | 只改显示精度，不改分子分母、不改变不可计算判定与原因码；同步更新 `tests/unit/test_09f_features.py:44-45` |
| 流动净资产口径 | `backend/application/finance.py::_overview`（`liquid_assets_fen`、`liquid_net_worth_fen`、`liquidity_reasons`、`liquidity_basis`、`amount_evidence`） | **一致，未改代码** | 已知限制见下条 10A-001 |
| 接近止损 2% 风险预警 | `backend/application/strategy/service.py::_near_stop`、`stop_distance`、`backend/config.py::near_stop_bps=200`、`start.py --near-stop-bps` | **一致，未改代码** | `held` 仅取 `INDEX_STRATEGY_ATR` 批次，T5 走 `_ndx_advice` 独立轨；仅展示，不写成交、不改 `action`/保护价 |

### 10A 新观察（仅记录，未改实现）

| ID | 现象 | 影响 | 建议 |
|---|---|---|---|
| 10A-001 | accounts schema（`migrations/versions/0006_finance.py`）无「受限资产」标记字段，排除受限资产的唯一路径是账户 `kind` 落在 `other_asset`/`other_liability`，触发 `liquidity_classification_unknown:<id>` 并停止计算 `liquid_net_worth_fen` | 真实受限资金（如冻结、保证金）当前无法既「明确排除」又「保持其他流动资产可计算」，只能整体降级为待确认 | **已由 10C 修复**（ADR-021，Alembic `0012`）：新增账户级三态分类与分时点证据，见下节 |
| 10A-002 | 前端 `WorkspacePage.vue:638`、`:665` 直接拼接 `investment_daily_return_pct`/`daily_return_pct` 字符串加 `%`；后端改为两位小数后展示为 `6.67%` | 展示层无需改动即符合口径 | 若后续需要千分位或正负号样式，另立任务 |
| 10A-003 | `liquidity_basis` 文案仍为 `legacy_current_liabilities; unclassified other assets/liabilities withheld`（`finance.py:714`），未显式写「排除受限资产」 | 接口语义完整，措辞与批准文本不完全对齐 | **已由 10C 修复**：文案改为显式说明受限资产排除、待确认分类不计算、负债按现行流动负债口径；前端同步显示口径与拆分记录操作说明 |

## 10C 最终最小补丁处置（开发者实测，非独立关闭裁决）

依据 10B 独立审查（10B-001 S2、10B-002 S2、10B-003 S3），本轮采用受限资产明确标记方案 B，不扩大 V1.0 功能范围。

| 编号 | 本轮实际处理 | 状态 |
|---|---|---|
| 10B-001 / 10A-001 | **受限资产识别**：Alembic `0012` 新增 `accounts.liquidity_status`（`liquid`/`restricted`/`unknown`，默认 `unknown`）与 `account_liquidity_events`（`previous_status`、`liquidity_status`、`effective_at`、`reason`、`kind`）；`POST /api/v1/accounts/{id}/liquidity` 记录分类并以 `expected_ledger_revision` 做 CAS；每次变更写 `audit_events(event='liquidity_changed')`。`_overview` 只把 `liquid` 资产账户计入流动资产，`restricted` 计入 `restricted_assets_fen` 且仍在总资产/净资产，`unknown` 使 `liquid_assets_fen`/`liquid_net_worth_fen` 为 null 并逐账户提示；负债沿用现行流动负债口径且拒绝设置该标记。历史查询按 `effective_at` 取当时证据，无证据即待确认。旧库升级不写任何分类事件，全部保持 `unknown`。账户页新增「流动性分类」表单、列与说明，含部分受限资金的拆分账户操作说明。 | FIXED，待定向复审；证据 `tests/unit/test_10c_restricted_assets.py`（7 项）与真实 Chromium 9/9 |
| 10B-002 | **安装脚本版本守卫**：`tools/windows.ps1` 把 npm 精确 `10.9.2` 改为 `10.9.x`（主版本 10 且次版本 ≥9），Node 保持 `>=22.13.0 <23`；新增 `.venv` Python ≥3.11 检查（uv 按 `requires-python >=3.11` 与 `uv.lock` 选择）。未删除检查、未改依赖锁。实测 Node 22.22.2 / npm 10.9.7 下 `-Action Install` 完整通过；npm 11.17.0、Node 24 仍被明确拒绝。 | FIXED，待定向复审；证据 `10C_TEST_REPORT.md` |
| 10B-003 | **冷却 regime 文档措辞**：`docs/reference/strategy_rule_map.md` 改为按当次评估日 G2 regime 计算；未改 `cooldown_required_days`、3/10 日阈值、`cooldown_until_date` 或任何策略公式。`test_cooldown_days_depend_on_regime_and_exit_action` 保持通过。 | FIXED（文档），待定向复审 |
| 10B-004 | 近止损只读单一报价价：**按审查建议不改**。ADR-020 措辞为「持仓报价距有效保护价」，真实退出仍用 `bar.low`/`bar.open`，模型 `STOP` 会在 `action` 中显示。 | 保持现状，记录不升级 |
| 观察（成本） | `other_asset` 不再被无条件视为「未知分类」——其流动性现在取决于账户 `liquidity_status`。旧库中的 `other_asset` 账户升级后为 `unknown`，仍提示待确认；不会因此高估流动净资产。 | 行为变更已记录于 ADR-021 |


