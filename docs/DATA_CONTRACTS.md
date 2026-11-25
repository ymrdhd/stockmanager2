# 单库数据模型、交易、收益和财务口径契约

> **强约束文档**：任何涉及成交、批次、资金、收益、账户或迁移的 Agent 必须只读相关章节并遵守。SQL 字段是建议基线，实施时以受控 migration 和测试固化最终形式。

## 1. 来源优先级与整数/日期口径

1. **权威事实**：`trade_executions`、`cash_movements`、财务交易、账户校正、被核验的余额快照、审计更正。
2. **外部输入**：行情、ETF 日 K、利率、券商成交导入；必须有 `source`、`observed_at`、`effective_date`、`quality`。
3. **可重建衍生**：批次余额、持仓、市值、已/未实现收益、当日盈亏、个人净资产、统计图；不能反向修改事实。
4. 金额统一**整数分 `amount_fen`**（人民币为主，保留 `currency`），中间金额/证券价格用 Decimal，明确定点精度和四舍五入规则；不得 `float` 写资金。证券数量用定点十进制或受约束整数，价格精度依证券/行情源设定。
5. 时间戳存 ISO-8601 UTC（带时区）；A 股交易日使用 `Asia/Shanghai`；其他市场按所属交易所时区及日历，不一律用北京时间划分交易日；个人财务月固定 23 日至次月 22 日。
6. `NULL`/`unavailable` 与数字 0 不同。缺失昨收价、费用或关键策略因子不能自动推导为零。

## 2. 主数据与表（建议逻辑模型）

| 表 | 最重要字段 | 不变量 |
|---|---|---|
| `system_metadata` | `database_uuid`, `instance_role`, `generation`, `schema_version`, `created_at` | 正式启动身份/角色校验 |
| `accounts` | `id`, `kind`, `currency`, `balance_mode`, `status` | `balance_mode∈{manual_snapshot,ledger}`；历史沿用手工余额模式 |
| `instruments` | `id`, `code`, `market`, `currency`, `price_scale` | `(market, code)` 唯一；不能用指数点位冒充 ETF 价格 |
| `trade_executions` | `id`, `request_id`, `account_id`, `instrument_id`, `side`, `quantity`, `price`, `fees_fen`, `executed_at`, `external_id`, `status`, `correction_of` | 每笔真实成交独立行；请求去重；审计更正 |
| `trade_lots` | `id`, `open_execution_id`, `strategy_id`, `lot_role`, `signal_date`, `frozen_atr`, `opened_qty` | 主仓/子仓独立；信号日 ATR 不漂移 |
| `lot_allocations` | `id`, `sell_execution_id`, `lot_id`, `quantity`, `method` | 各批次剩余数量不为负；总扣减不超过成交数量 |
| `cash_movements` | `id`, `account_id`, `execution_id?`, `transfer_id?`, `amount_fen`, `kind`, `occurred_at` | 交易现金与成交同事务；内部转账配对 |
| `finance_transactions` | `id`, `account_id`, `kind`, `amount_fen`, `category_id?`, `occurred_at`, `reversal_of?` | 审计撤销，不物理删除真实记录 |
| `balance_adjustments` | `id`, `account_id`, `before_fen`, `after_fen`, `reason`, `occurred_at` | 手工余额调整有来源，不伪装普通收入 |
| `strategy_signals` | `id`, `strategy_version`, `instrument_id`, `signal_date`, `kind`, `quality`, `evidence_json` | 候选≠正式；来源时间可追溯 |
| `quotes` 或行情缓存 | `instrument_id`, `effective_at`, `value_decimal`, `source`, `quality`, `is_proxy` | 不写交易事实；过期禁止误报实时 |
| `valuation_snapshots` | `as_of`, `ledger_revision`, `quote_revision`, `equity_fen`, `quality` | 可重算，必须匹配源版本 |
| `audit_events` | `id`, `entity`, `entity_id`, `event`, `request_id`, `at`, `actor`, `details` | 更正可追溯，禁止悄悄删改 |
| `alembic_version` | `version_num` | 单向有序迁移，版本不允许分叉 |

**必要索引/约束**：`trade_executions.request_id UNIQUE`；如果存在稳定券商 ID，则 `(account_id, external_id) UNIQUE WHERE external_id IS NOT NULL`；`quantity > 0`, `price > 0`, `fees_fen >= 0`, 外键启用 `PRAGMA foreign_keys=ON`；账户/证券/批次状态有 `CHECK`；各视图使用有效记录而非全部历史更正行。

注意：仅使用 `(时间,数量,价格)` 作为唯一键会误杀真实的相同成交；不允许。

## 3. 交易写入协议与失败语义

- 各 UI、批量导入、API、后台任务 **只能调用 `TradeService`**，不允许各自写 `portfolio` 或 `trade_history` JSON。
- 入口接受不可复用的 `request_id`；如果重复 `request_id` 且请求主体相同，返回原结果；主体不同返回冲突，不执行写入。
- 入参校验：交易账户、标的、交易日、方向、数量、价格、费用、可卖批次；策略偏离标注而非拒绝已经发生的真实成交，无法分配批次进入待核对流程。正式买/卖必须对应用户**实际已成交**事件；计划与正式成交分表/分状态。
- **BEGIN IMMEDIATE → 校验当前事实 → 插成交 → 批次分配/建立 → 现金流水 → 审计 → 投影失效标记 → COMMIT**；任何失败 ROLLBACK，不提交半成品。
- 服务端由单一写实例负责，`busy_timeout`/数据库级约束作为并发兜底，不使用页面按钮禁用作为唯一保护。
- 修改历史成交采用“冲正原事件 + 插入更正成交”的可追溯链。受影响历史日期的 P&L 和持仓快照失效后重算。

## 4. 批次与现金守恒

- `position_qty(code, account) = Σ有效买入数量 − Σ有效卖出数量`，与所有未关闭批次的剩余数量相等。
- 每笔卖出分配的 `Σ allocated_qty` 必须等于出售数量（策略必选指定批次；普通 FIFO 按策略定义自动分配）。
- 卖出不能超卖；跨账户不得混用证券或证券现金。
- 同一笔买单：证券现金变化 = `-成交金额-费用`；卖单 = `+成交金额-费用`；现金金额换算在交易事务内以规定精度一次完成。
- 银行↔证券账户内部转账：一正一负、同币种等额、统一 `transfer_id`；**个人净资产和投资收益均不因转账本身变化**。
- 如果证券现金历史不足以可靠恢复，明确记“初始余额/基准调整”并人工核对，不凭总权益倒推现金。

## 5. 当日盈亏与历史收益

### 5.1 逐笔解释性计算（简化，无公司行为时）

`PnL(day, t) = Σ昨日收盘持仓数量 × (当前有效价-昨日有效收盘价) + Σ当日买入数量 × (当前有效价-买价) + Σ当日卖出数量 × (卖价-当前有效价) − 当日费用 + 当日有效分红`。

此公式使用**正确的前一交易日持仓和当日每笔成交**；分红、拆分、配股、红利税、跨币种估值必须有明确事实事件和校准方法；不能用简化公式覆盖异常场景。

### 5.2 账户权益核对（同估值边界）

`P&L = 期末证券账户权益 − 期初证券账户权益 − 当期净外部资金流`。

其中 `证券账户权益 = 证券现金 + 持仓市值 (+ 其他证券权益/负债如适用)`；内部转账对证券账户算资金流，对个人总资产不算收入。两种算法的对账差异必须标记（阈值可配置并有来源），**不得自动增加资本记录抹平差额**。

### 5.3 幂等与重算

- 收益算法是纯计算，不改变成交或入金；刷新页面/重启后重复计算结果相同。
- `valuation_snapshots` 只作带 `ledger_revision + quote_revision` 的加速缓存，过期必须重建。
- 历史补录：自最早受影响交易日起重算下游快照和财务投影；不反复叠加“前次累计收益增量”。
- 结果必须带 `as_of`, `trading_day`, `quality`, `source`, `ledger_revision`, `quote_revision`；数据缺口时返回 `unavailable` 而非 0。
- 全周期/阶段收益需区分已实现/未实现/时间加权/资金加权；首版不能把净资产增长直接称为投资收益。

## 6. 财务与证券资产联动

- **一个业务数据库**：证券现金账户与普通财务账户使用统一 `accounts` 身份；证券市值按持仓估值生成**单次资产投影**，不是 Finance 再录一笔持仓资产。
- 普通收入、支出、转账与证券买卖不同；股票买入不算消费，卖出收入不算工资收入，收益也不是证券转入资金。
- `balance_mode=manual_snapshot`：用户维护余额，普通历史收支**不自动反推、加减旧账户余额**。新 `ledger` 模式必须先完成初始余额、交易完整性检查与显式启用。
- 手工余额调整对应 `balance_adjustments`，记录新旧金额和原因，不能伪装成薪资或投资收益。
- **财务月**：每月 23 日（含）至次月 22 日（含），应设计对跨年、闰年及 23 日边界的测试。
- 净资产 = 全部有效资产 − 全部有效负债。证券账户权益只计算一次，且需要相同的估值时点/换算汇率。

## 7. 数据迁移计划（从旧项目）

1. 必须使用经 SQLite Backup API 或停机获得的旧 `stock_manager.db` 与 `finance_manager.db` **副本**，先校验完整性、数据量、关键来源版本；不得直接对原库迁移。
2. `tools/migrate_legacy.py`（待开发）只做读旧→映射→写新**临时库**；原始来源 ID 和 `source_file_hash` 保留，不凭文件名臆断权威版本。
3. 逐笔恢复旧 `kv_store.trade_history`、`portfolio`、策略批次与 Finance 分录；发现相互矛盾时列出差额，人工裁决。不得用“取最新 JSON 覆盖”吞掉差异。
4. 无法恢复真实成交的手工持仓，先记录为“待确认初始持仓/期初基准”，**不伪造一笔真实历史买入**。
5. 迁移后检查：成交笔数与总量、按证券持仓、批次余额、费用、现金/净资产/财务月、历史日期和公司行为。对不上不准切换正式库。
6. 新库与旧系统并行只读核对，最终切换当天备份并锁定旧系统只读；任何失败可切回完整旧库但不能双边同时写。

## 8. 数据质量、异常与审计

- `valid/stale/missing/conflict/derived_unverified` 至少五类；策略正式准入只能基于允许的、可追溯的有效输入。
- 未核实的 V9/G2 研究因子不得自行设为通过；信号日期/执行窗口/真实成交时间严格分离。
- 关键错误必须可见：账户权益对不上、行情过期、策略证据缺口、数据库身份不符、备份失败、重复提交冲突。

## 9. 实施必须先裁决的账务细节

- 证券现金流水是唯一现金事实；finance_transactions 不得另复制同一现金影响。用共同 business_event_id/transfer_id 关联展示；证券交易费用已在净现金流水扣除，不能再次从现金扣费。收益公式扣费是解释，不是分录。
- manual_snapshot 账户的内部转账必须明确余额是否已包含该转账：对已有余额的历史转账不得再扣；新转账若要修改手工余额，应一次事务保存经用户确认的余额调整，不能隐式改旧余额。
- 期初余额/初始持仓是独立基准事件，持仓守恒从期初基准加有效成交及公司行为推导；没有原始成交不得制造假买单。历史缺口影响的区间收益标记不可用。
- 固定金额精度、币种范围、费用分配、余数、交易顺序和公司行为支持范围，在 02/03 任务以合成黄金样例固定。实际费用未知时不得填零；已有真实成交允许待核对，未确认部分不进入可靠收益。超卖/批次冲突必须阻断错误扣减、保留待处理记录与证据。
- 统一账本 revision 在事实事务内递增；投影只在输入版本仍匹配时提交，否则重算。历史更正先模拟按时间重放，检查后续批次/现金/公司行为，不允许只修当天数值。
- 正式物理表结构由批准的 Alembic migration、约束和 ER 图定义；本逻辑模型不是已经实现的 DDL。系统身份与 schema revision 校验由唯一迁移链提供，不维护竞争版本号。

### 02-trading-core 已固化的实现口径

- 0002 在唯一 0001→0002 Alembic 链上增加交易表、`ledger_state`（revision 与最早失效日）及 `ledger_requests`（规范化请求与原始响应）。新库直达 0002；旧正式库需先绑定、停写、备份再显式事务升级。
- V1.0 此轮仅 CNY、SH/SZ/BJ 股票与 ETF，证券数量为正整数股；价格/冻结 ATR 存整数万分之一元，API 以四位十进制字符串传输（数量为整股字符串，兼容整数输入）。金额一次 ROUND_HALF_UP 至整数分；部分扣减按剩余本金/买入费用比例四舍五入，末次分配吸收余数。缺失费用保持 NULL。交易日必须与 UTC 时间换算后的 Asia/Shanghai 日期一致；本轮不接入交易所假日日历。
- 券商编号 `(account_id, external_id)` 永久唯一；更正复用同一编号时 `external_id_owner` 指向最初占有该编号的成交行，响应仍显示实际编号；禁止无关成交链重用。原始请求响应持久化，重试不随之后的更正改变；GET 返回当前状态。
- 未知费用、批次不足、指定批次无效或历史顺序不能安全重放时保留 pending 事实及证据，不产生现金/有效批次。查询在同一读取快照提供 revision 与受影响账户/证券；确认或安全更正后才进入有效账本。策略批次的未知信号/ATR 不补造，标注证据缺失。
- 更正保留原成交、历史分配和现金冲正；只处理经后续成交/现金依赖检查可安全重新分配的情形，复杂历史更正明确拒绝。最早受影响日与 revision 同事务更新，收益投影实际重算仍未实现。现金期初/入金/出金/校正明确区分，缺期初返回 null，历史现金不足或待核对标记 derived_unverified；不产生虚构资金事实。跨财务账户的配对转账与完整恢复对账不在本轮。

### 03-pnl 实现口径（本节覆盖 02 的历史重放与收益未实现说明）

- 0003 沿唯一 Alembic 链增加 `opening_positions`、`company_events`、`quotes`、`quote_state`，扩展现有批次和现金表。显式停写、备份、原子升级；已有成交、现金冲正、批次与分配 ID 保持不变，失败不自动降级。公司现金分红金额只存在 `cash_movements`，事件通过唯一外键引用。
- 历史补录先 pending，确认或更正时按同账户/证券 `executed_at, rowid` 顺序重放有效成交；同时间按成交行首次录入顺序，未更正买入的 lot ID 不变，原分配在审计中保留，现金只为新有效成交记一次。作废保留原成交及冲正；不能满足指定批次、资金/期初边界或存在未解决数量公司行为时安全失败。既有成交 ID 不重写。主数据新增同事务递增 ledger_revision；目前无主数据修改接口。
- 单一 opening 现金是指定时刻已核验余额；同一时刻及之前其他现金/成交不得重复计入，后续补录冲突拒绝或 pending；旧库冲突不擅自修改事实，当前已知余额仅从基准后流水推导并标记未核实。资金与成交新录入均拒绝未来事实，统计按 UTC 截止时间筛选；A 股日期使用 Asia/Shanghai。期初持仓独立记录数量、成本、费用，不造交易或现金流，历史缺口显式标记。已核验期初持仓仅支持普通批次。
- 价格为整数万分之一元，`observed_at`、生效交易日、来源、质量、close/intraday、quote_revision 均显式。估值按 as_of 过滤，不使用未来观察；确认昨收必须为指定前一交易日 close；无自动日历，调用者需提供实际前一交易日，已知中间交易/收盘证据冲突时不计算。盘中价格超过 30 分钟标 stale，同日确认收盘价可用于当日收盘估值；缺失或非 valid 价格不生成正式估值。人工输入来源须可追溯。
- 当日解释式使用当前持仓整数分市值减昨持仓整数分市值，加当日真实净成交现金及分红，等价于逐笔价格差公式；整数分以同口径 ROUND_HALF_UP 估值/结算，不重复扣费。权益核对减真实出入金及单独列示的余额校正，任何非零差异标记冲突（不自动抹平）。已实现为有效卖出净收入减其分配买入本金与费用，分红独立列出；未实现为当前市值减剩余成本费用。累计收益只在可信现金基准、没有期初持仓历史缺口、已知成交均在基准之后时由权益减期初现金及基准后出入金、调整计算；否则累计值 null，并列明基准与缺口。持仓、收益、权益只读同一数据库快照，不写回事实。未核对成交与未支持数量公司行为阻止相关可信估值；数量/成本公司行为的实际调整尚未实现。

### 04-strategy 证券行情时间口径（覆盖 03 行情时间及交易日说明）

- Alembic 0004 保留原 quotes 行和版本，增加 `effective_at`、`acquired_at`、`price_basis`。有效交易日由 exchange-calendars 4.13.1 XSHG 预计算日历确定；该日历目前覆盖至 2026-12-31，沪深使用内地交易日；不可用或超范围时不推测。兼容传入的 previous_trading_day 必须与日历一致，否则拒绝。指数市场日历尚未接入。
- 正式收盘的生效时间为对应实际交易日收盘时间，盘中为观测时间；服务端记录实际获取时间。旧报价根据原观察时点和已验证日历补生效时间；获取时间未知保持 NULL，不作为历史策略可获得性证据。无法验证的旧报价不参与估值，原数据保留。账务重算按 effective_at <= as_of；严格历史数据查询还限制 acquired_at <= available_at <= as_of。可固定 quote_revision，账本使用当前有效事实。证券 quotes 只接收未复权实际证券价格（CNY 定点万分之一元），不可存入指数、代理或复权研究价格；手工输入的来源真实性仍依赖录入人确认。
- 内容相同的报价（证券、日期、生效时间、来源、价格、类型、质量）不递增 quote_revision；旧获取时点未知的记录重新取得属于新增可追溯证据。不同可信收盘价格产生 conflict，不静默采用最新值；非 valid 的盘中记录不覆盖有效收盘。同一交易日有效盘中价格只用于盘中/午休（午休新鲜度以休市开始计），收盘后必须有该日正式收盘才能用于可信估值；开盘前及非交易日取最近完成交易日的正式收盘，不按自然时间判 stale。缺少当前需要的报价可展示最后可信参考价及时点，但可信市值/权益/PnL 为 null。自然日收益与最近完成交易日收益分开，休市分红按实际日期计入。
- 自动刷新仅在正式服务生命周期运行一份；手动及后台共享刷新互斥，网络操作在 SQLite 写事务外，限时/限次/限流。当前东方财富适配器限定可识别沪深证券代码、校验市场/代码/定点价格/观测日期，日线 fqt=0，当前日收盘后至少等待 15 分钟再采集日线；真实接口尚未验证。观察池、G2、V9 的规则、因子和指数映射等待旧源证据，不产生未经核实的正式信号或数量。

### 04B-strategy 行情人工核实及成交时间保护

- Alembic 0005 仅新增 `quote_resolutions` 及索引，保留报价、账本及历史版本；升级沿原备份/事务路线。`POST /api/v1/quotes/resolutions` 要求已有 valid、已获取、已完成收盘报价 ID、当前 `expected_quote_revision`、唯一 request_id 与非空核实原因。仅处理尚未解决的收盘冲突；原报价行不修改，核实记录保存所选及已审阅报价 ID、原因、UTC核实时间和新的 quote_revision。新冲突证据仍会重新阻断可信估值，普通重复报价不能解除冲突。账务按所选版本重算；严格可获得性额外要求核实时间 <= available_at。`GET /quotes/{instrument_id}` 同时返回原始报价和符合截止版本/可获得性条件的 `resolutions`；核实不改变 ledger_revision 或真实交易事实。
- 普通沪深成交入账、确认及历史重放共用真实交易日历；接受连续交易区间（含结束边界）及 09:25 开盘集合竞价撮合分钟，午休、明显异常时段、非交易日、超覆盖和无已验证市场日历的事实进入 pending，不影响有效现金、持仓和分配。原日期不自动改写；通过既有成交更正接口核实日期后原子冲正、重放。其他特殊交易类型需独立证据，本轮不开放特殊入账；已有有效事实不批量修改，需显式核对。
- `/market/status` 返回 `calendar_coverage` 的实际 start/end、days_remaining、status（valid/expiring/outside_coverage/unavailable），期限剩余60个自然日内提示 expiring。无可信后续日历时继续安全降级，不猜测未来休市安排。

### 05-finance 实现口径

- Alembic `0006` 扩展唯一 `accounts`（证券 ledger；银行、现金、支付、手工资产、信用卡、其他负债为 manual_snapshot）；保留所有原账户 ID、关联事实及外键。资产/负债金额均用非负整数分，负债在净资产中扣减。普通账户要求显式生效时间；兼容旧证券账户未核验生效日为 NULL，不将未知历史账户自动排除。纳入统计标记在创建时确定，名称/状态更新用账本版本 CAS、保存审计；关闭不移除资产负债。历史数值按事实时点，账户展示名称/状态为当前主数据。
- `balance_adjustments` 是手工余额权威历史；以同一账户 `(occurred_at,rowid)` 选截至时点最近有效记录。保存 before/after、previous_id、原因；使用 expected_balance_id（首次为显式 NULL），较晚快照存在时拒绝复杂历史修改；同时间的显式后续快照可更正当前记录。账单不修改快照。普通 `finance_transactions` 只支持收入/支出/手续费，正整数分，类别与方向一致；更正原记录标为 reversed、新记录关联 correction_of，作废保留原记录及审计，按有效事实重算财务月。周期 ID 为北京时间每月23日，区间左闭右开。
- `finance_transfers` 是成对事实的唯一标识；证券端只有 `cash_movements`，手工端需明确已包含与否及对应余额 ID。未包含时在同一事务新增余额快照，负债偿还减少正值负债；已包含时不调整，若查询处于转账与包含该转账的快照之间则历史余额未知。单端现金入口拒绝新 transfer_id。更正仅对可安全撤销的完整转账开放，先同时撤销两端，替换时传入撤销后的前一有效余额 ID；有后续快照/转账引用或已包含历史余额时拒绝，保留原有效事实。旧库遗留无配对 transfer_id 不自动补造，个人合计标为不可信。
- 非成交现金/现金分红更正与作废用原发生时点的反向现金记录及审计；`effective_cash_movements` 同时排除已冲正原记录与冲正行，现金余额、外部流、余额调整及分红收益共用有效口径；历史为按当前已核实事实重述，不是当时录入可获得性回放。证券期初现金/持仓和复杂公司行为的更正仍待后续处理。升级遇不一致旧冲正证据拒绝，不能默默改变余额。
- `/finance/overview` 在同一 read transaction 中计算普通资产、负债、所有证券账户权益（复用 PortfolioService）、财务月和版本；有未核验纳入项时合计为 NULL，并返回已知部分、原因、每账户及类别。证券现金、市值及权益是分解展示，只有权益进入总资产一次；投资收益与普通收支分列，未知不填零。

### 06-ui 实现口径（覆盖 05 中账户生效/统计标记与已包含转账更正限制）

- Alembic `0007` 新增 `account_scope_events`、`transfer_reconciliation_gaps`，原事实与外键保持。新账户必须提交经确认的生效时间；旧证券账户 NULL 保留，历史时点早于已知事实或无存在证据时总额不可核实。经资料确认的有效日期通过账本版本 CAS 写入，不能晚于已知事实。
- 账户统计范围显式事件按发生时点应用，保留被排除账户的数值与原因；关闭状态不改变统计范围；误创建只允许无任何账务事实的账户。旧账户名称、关闭状态仍按当前主数据展示。
- 已包含余额或存在后续手工快照的错误转账通过人工核对：明确核实实际两端资金，为每个手工端提交最新余额 ID 及核实后的余额，同一事务保存新快照、冲正证券现金腿、审计及转账状态；可作废或以相同两端、原日期的正确金额替换。历史无法确认区间从原转账时点至核实快照之前标记不可核实；不机械增减手工余额，不改变普通收入或投资收益。普通更正入口仍拒绝不具备安全条件的复杂更正。
- UI 只消费 API，未知值显示不可用，不自行投影账务。`/system/status` 返回当前已验证的模式、身份与写权限；请求状态未知时在当前页面保留原请求ID及载荷、核对数据库身份后重试。未完成 Vue 构建与浏览器验证，不视为可发布界面。

### 08-integration-and-migration 实现口径

- **Alembic 0009**（更正）：`opening_positions` 改为版本化行（新增 `superseded_by`、`is_current`，部分唯一索引 `opening_current` 保证每 `(account_id,instrument_id)` 只有一条当前行；`superseded_by` 必须 `DEFERRABLE INITIALLY DEFERRED`，否则「先插新行再把旧行指过去」会被外键拒绝）；`company_events` 新增 `ratio_from`/`ratio_to`/`cash_tail_fen`/`applied_at`/`superseded_by`/`voided_at`/`strategy_evidence_invalid`/`applied_snapshot`。既有事实行、批次与分配 ID、列顺序原样保留，新列一律可空或默认 0（`test_0005_to_0006_preserves_all_facts_with_fk` 按旧 schema 列逐字节比对，并断言新列未被迁移填值）。
- **Alembic 0010**（策略隔离与行情修订）：`strategy_bars` 主键改为 `(series_key, trade_date, revision)` 并新增 `effective_from`/`observed_at`，旧行 `revision=1` 搬运——同一交易日的价格修订是新增行而非覆盖；`strategy_signals` 唯一键改为 `(account_id, index_id, signal_date, input_version, strategy_version)`，`strategy_plans` 改为 `(account_id, index_id, signal_date, strategy_version)`。`SCHEMA_VERSION=10`、`REVISION='0010'`。
- **期初与公司行为更正**：`correct_opening` 先插新的版本化期初行、再指向旧行、重放该账户/证券在其之后的有效成交与资金并按需重分配批次；现金不重复、批次与分配数量守恒、成本/费用/收益重算、`ledger_revision` 递增且回填最早受影响日。存在后续指定批次冲突或无法安全重放时整体拒绝，不留部分修改。`void_opening` 保留原记录并标记，历史查询可追溯。拆分/合并按 `new_qty = old_qty * ratio_to // ratio_from` 向下取整，**除不尽必须由调用方给出真实 `cash_tail_fen`，否则拒绝**——系统不自行定价零股，也不把尾差塞进成本；尾差作为独立 `cash_movements(kind='adjustment')` 记录。`rights`/`other` 仍不建模数量，只落事件并标 `company_action_not_modeled`，相关标的估值被 `unresolved_company_action` 阻断。已应用的拆分按旧份额与旧收盘价、调整后的份额与新报价计算当日价值变化；缺少相应证据时降级，不把除权价格变动伪造成盈亏；影响策略冻结 ATR 尺度时该批次策略证据置 `strategy_evidence_invalid`，不静默沿用。`void_company_event` 冲销已产生的现金尾差与批次调整并把实际金额（`tail_reversed_fen`、`reversed_batches`）回给调用方。
- **策略价格口径与证据**：`strategy_signals.price_basis` 取值 `etf_unadjusted`（默认 ETF 模式下 V9 用未复权 ETF 日线）或 `index_level`（G2 与指数模式），`price_unit` 随之为「元」或「点」。`input_version` 由规范化 OHLCV 内容与来源算出（价格修订必产生新版本），写库时的 `input_version` 用 `evaluation_version`（自身输入版本 + 同组 peer 版本 + `config_fingerprint`），`config_version` 存 `cfg:` 开头的规则指纹而不是策略版本号。过期（`series.last_date != as_of`）或来源未核实的序列只展示、不落正式信号（`stale_series_not_recorded` / `unverified_source_not_recorded`）。
- **行情准入与日历**：正式决策路径不再有 `weekday_fallback`；日历未覆盖（如 2027 未发布）时 `MarketClock` 抛 `CalendarCoverageError`，策略返回不可用原因而非猜测交易日、下一交易日或冷却完成日。ETF 执行日与持仓天数用 ETF 所属市场日历，G2 指数日线完成时间用各指数所属市场日历。
- **证券行情**：`quotes.price_basis` 固定 `unadjusted_security`，默认 provider 为腾讯；指数点位与复权 ETF 价不得写入该表。`/market/status` 每条报价返回 `source`/`price_basis`/`kind`/`observed_at`/`acquired_at`，顶层返回 provider 与可用性。
- **只读与写入分离**：`GET /strategy/pool/{account_id}`、`GET /strategy/positions/{account_id}` 为只读（`read_only=true`，不给正式信号落库）；正式刷新只走 `POST /strategy/refresh`、`POST /strategy/positions/{account_id}/refresh`、`POST /strategy/sync/{account_id}`，遵守既有认证、CSRF 与数据库身份约束。
- **备份/恢复**：manifest 见 OPERATIONS_SECURITY 3.1；恢复要求备份 `schema_version == SCHEMA_VERSION`，成功后 generation=max(当前,备份)+1 并同步 `official.json`，旧 generation 的写请求被 409 拒绝。
- **旧库迁移**：`tools/migrate_legacy.py` 只读打开源库，写入隔离目标库，记录源文件哈希与源记录 ID 映射实现幂等；成交历史完整时经 `TradeService` 重建事实与批次，不完整时用经核实的期初基准，绝不伪造历史买卖；旧版累计资本增量不重复写现金流水，未知费用不填零；旧策略状态只作证据迁移（`strategy_evidence_legacy`），不生成可信正式持仓，冲突以真实账本优先。跳过与异常必须带原因和数量。

08 补充：`TradeInput.signal_id`、`strategy_exit_action`（仅真实卖出可选 STOP/GAP_STOP/EXIT/CLOSED）保留在成交 payload，缺值保持旧请求幂等哈希。仅有效已清仓主批次的实际卖出可生成冷却；原因未知阻止新计划，须明确更正。期初现金更正使用有效资金记录时间边界；历史复杂依赖不得强行重放。
