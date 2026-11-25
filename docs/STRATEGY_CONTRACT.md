# V9/G2 与行情迁移契约（04-strategy）

## 1. 证据优先，不发明新策略

旧压缩包 `stock-1_20261122_183448.zip` 的 `subroutine/index_strategy/config.py` 声明版本 `index-strategy-atr-v9.2`、展示名 `V9＋G2`。这是源码版本线索，不是用户当前运行参数的证明。迁移前记录源 ZIP SHA256、实际代码版本、有效配置和数据源；旧项目源码放仓库外的只读参考位置，不整包复制到新仓库。没有真实配置/历史行情时使用合成夹具，标明未完成实盘对照。

### 按任务定位旧代码（只读相应符号）

| 任务 | 源文件/符号 |
|---|---|
| 正式策略、观察池 | `subroutine/index_strategy/service.py` 的 `IndexStrategyService.evaluate_index/evaluate_all`, `evaluation_to_row`, `build_rows`；追踪 `web_app/server.py` 中策略接口调用 |
| 参数与标的 | `config.py` 的 `INDEX_STRATEGY_CONFIGS`, `IndexStrategyConfig`, `factor_is_verified`；只迁移当前 Web 路径消费的字段 |
| 进出场与市场状态 | `entry.py`, `exit.py`, `regime.py`, `v9_rules.py`, `indicators.py`, `factors.py`, `factor_combo.py`；以实际调用链为准 |
| 交易批次与冻结 ATR | `web_app/trades.py` 的 `_frozen_atr`, `_allocate_sell`, `_apply_strategy_sell`, `record_manual_execution`, `link_legacy_strategy_lot`；`state.py` 的 `StrategyStore` |
| 行情与日期 | `market_data.py`, `etf_quote.py`, `etf_link.py`, `calendars.py`；已完成 K 线与盘中估计必须分离 |
| 利率观察 | `web_app/fed_observer.py` 与实际 Web 请求，不纳入交易准入规则 |

不因存在 NDX 辅助、通知、其他研究函数或常量便自动加入产品。只有当前已批准入口/调用链与用户确认的需求可迁移。五资产已下线。

## 2. 规则提取流程

先建立版本化规则清单（建议 `docs/reference/strategy_rule_map.md`，在 04 实现时生成）：规则 ID、源码符号/行号、输入条件、输出、配置来源、缺失行为、黄金样例 ID。参数保持原有效版本，不任意改阈值、冷却天数、止损、仓位数、费用、市场状态；用户批准的差异单独记录。不能把默认常量当成现场配置。旧实现疑似缺陷列差异，不默默修正。

黄金样例至少覆盖：正式准入通过/否决、缺因子、代理行情、无有效昨收、收盘与盘中、交易日窗口、UP/RANGE/DOWN/UNKNOWN、主仓/子仓、冻结 ATR、冷却、加仓、止损/跳空、到期与历史批次待关联。固定输入、时钟、配置、期望结果；保存完整精度及容差；运行旧有效函数与新函数对照。旧系统无法运行时明确 BLOCKED，不用新输出反向生成期望。

## 3. 新引擎输入输出与状态

输入：instrument/account、规则版本和配置哈希、交易所日历、as_of/信号日期、已完成行情版本、因子来源/验证状态、实际持仓批次、已核实证券现金、账户权益。输出：candidate/confirmed/rejected/unavailable、reason_codes、证据及实际执行窗口、关键价格/单位、输入版本。命名为新 API 契约，不假定旧枚举一一对应；映射必须经过规则清单确认。

- 信号不等于计划，更不等于成交。只有 TradeService 接受实际成交；策略读接口和行情刷新不能制造/关闭真实仓位。
- **价格口径（08 修正）**：默认 ETF 模式下 V9 的突破/ATR/BuyMax/退出/加仓/因子一律用**该指数对应 ETF 的未复权日线**（旧主链 `fqt=0` 的口径），G2 市场状态用 14 只指数的**真实点位**。两种尺度绝不混算，也不相互折算。`price_basis`（`etf_unadjusted` / `index_level`）、`price_unit`（元 / 点）随信号一起持久化；指数点位 ATR 不得预填人民币成交表单，前端以 `tradable` 与 `tradable_block_reason` 双重把关。旧版错误的「指数点位 + 元」正式信号保留审计但标为无效证据，不删除既有真实成交。
- **模型建议不等于执行（08 修正）**：模型止损/退出/加仓/NDX 比例变化只在 `PositionState` 的隔离副本上推进，产出 `model_exit` / `unrealized_model_actions`，不写 `exit_filled`、不释放组合槽位、不启动冷却；冷却按真实已清仓主批次的有效卖出及明确退出原因重建；退出原因不明则阻止新计划，允许通过真实成交更正补充原因。旧策略状态不充当真实持仓或退出事实。
- **策略状态由真实账本重建（08 修正）**：`strategy_positions` 只是可重建证据，不是第二套持仓。主仓/子仓买入须关联 `account_id`、`instrument_id`、`lot_id`、实际成交时间与价格、`signal_id`、策略与配置版本、信号日、冻结 ATR 与价格口径；缺可信证据的真实成交照常入账，策略状态进待核对。核心槽位按真实主仓计数（最多 8，子仓不占，NDX 独立轨不占）。
- **计划与信号按账户隔离（08 修正）**：市场信号可共享，执行计划必须带明确账户归属；A 账户的计划不得让 B 进入 `PLAN_PENDING`，A 取消不得影响 B；同账户等价信号重复刷新幂等。
- 实际成交即使偏离策略计划，也必须能记录事实并标注偏离；不可因信号否决或无行情而丢弃真实成交。批次/规则缺失时进入待核对状态，阻止不可信策略提示，不伪造冻结 ATR。
- 主仓和加仓子仓独立身份、原始信号日期及 ATR；保护价与退出以适用规则确定。卖出分配对应真实批次；策略版本升级不追溯改旧批次规则，除非明确获批且保留审计。
- 仅已完成且有效的输入可生成正式信号；未来数据、未核实因子、失效代理禁止冒充正式证据。现金不可靠时不给可执行数量；不得把账户总权益当现金。
- 同一输入版本重复评估结果一致；不同 as_of 不复用未来行情；失效输出不覆盖历史证据。

## 4. 市场数据与 Fed

已有数据源适配优先，使用可验证的数据提供商；每个报价保留市场、标的、币种、价格单位、时间、来源、有效期、代理标记与复权口径。真实成交用未复权实际成交价；复权研究与账户估值区分。公司行为缺证据时标注不可计算。市场休市按交易日历，不用系统本地日期猜测；限流、超时、重试有上限，网络操作不占写事务。研究输入如无法重新获取，应保存可复现的最小快照。

**08 补充**：正式决策路径已删除 `weekday_fallback`——日历不在可信覆盖范围（如 2027 尚未发布）时，`MarketClock.is_trading_day` 抛 `CalendarCoverageError`，策略返回不可用原因，不生成正式信号、不猜下一交易日、不伪造冷却完成日。ETF 模式的执行与持仓天数用 ETF 所属市场日历，G2 指数日线完成时间用各指数所属市场日历（NDX/HSTECH 是海外标的，但国内 ETF 的执行日历仍是 A 股日历）。行情准入要求：最后一根必须是应有的已完成交易日、盘中未完成 bar 不作收盘、过期缓存与 `source_verified=false` 不产生貌似已核实的正式信号（以 `stale_series_not_recorded` / `unverified_source_not_recorded` 标注）；G2 关闭与 G2 数据缺失分别处理。

**证券行情（08 修正）**：`MarketService` 默认 provider 已换为 `TencentQuoteProvider`（沪深 ETF 与普通股票的实际价、盘中/收盘 `kind`、观测时间、未复权正式收盘价、实际前一交易日收盘、自动与手动刷新、标的/市场/单位校验、来源与质量展示）；`EastmoneyProvider` 已确认不可达，不再进入刷新路径。指数点位或复权 ETF 价永远不写入真实证券估值表（`quotes.price_basis` 固定 `unadjusted_security`）。

**历史证据（08 修正）**：`input_version` 覆盖规范化 OHLCV 与来源，价格修订会产生新版本；`strategy_bars` 改为追加式修订（`revision`/`observed_at`/`effective_from`），原始与修订都可追溯；历史日期按北京时间日末的实际可获得性截止，只读、不联网；日期以前取得的修订方可见。完整多日与历史配置快照重放仍待验证。`GET` 查询不再隐式写入，正式刷新走 `POST /strategy/refresh`。配置版本用 `config_fingerprint`（只哈希影响判定的规则参数），不用策略版本号冒充。

Fed 页面（V1.5，不阻断 V1.0；ADR-013）保留 EFFR、目标区间、会议概率/路径、上一日/上一周比较、CPI Nowcast 和实际利率代理；明确来源与时点，不能将预测当已发生数据。数据不可用时展示状态，不回写 V9/G2 正式裁决。

## 5. 验收

执行 ACCEPTANCE_TESTS S01–S06 与新增 S07/S08。正式发布前应具备可复现的规则映射和对照报告；未经裁决的差异阻断受影响策略上线。禁止把旧模块的存在或旧 README 的表述当成迁移完成证据。

**09C T5 执行边界**（依据用户批准的 NDX 原规则生命周期）：独立 `NDX_AUX_T5` 建/增仓建议证据区别于 ATR 信号，不填冻结 ATR，不预留现金；原始调用链没有实际 `save_ndx_aux` 写回，本实现从实际有效成交及批次分配重建仓位、以初次实际买入及最近增仓日期核对完成的美股会话间隔（同日保守不计）。ETF 未复权序列保持原默认口径，实际国内 ETF 用 CN 下一会话。现金扣已有 ATR 容量后保留原25万元；同标的普通或 ATR 持仓、同日多版本歧义、证据/行情不足保守降级。真实成交按券商事实保存，关联建议不等于金额符合目标，更不自动下单；普通 ATR NDX 20日规则、T5 SMA退出及 G2 动态3/10日规则不改。多笔真实成交用不同券商成交编号；更正、作废依账本重建。日历版本由 `tools/verify_calendar.py` 审查及锁定依赖控制，不使用未经核实的未来日历。
