# V9＋G2 原规则证据清单（04C 实际核验版）

## 来源状态（本轮实测，不是声称）

- 原始参考 ZIP：`artifacts/reference/stock-1_20261122_183448.zip`，实测大小 1749267 字节，实测 SHA256 `1edc58feabbe2e32cda9bfb2bd34f205f4cd3c4b0b3e008e5910002b6af6cc06`，与任务书一致。
- 只读解压到忽略目录 `artifacts/tmp/reference/`（未入库、未提交旧项目）。本轮读到的策略源码：`subroutine/index_strategy/{config,indicators,regime,entry,exit,v9_rules,factors,factor_combo,market_data,symbols,etf_link,position_lines,state,service,calendars,sizing}.py`、`subroutine/indicators.py`、`web_app/{server,trades,strategy_refresh}.py`。
- 旧纯计算模块已在本机真实可 import（`sys.path` 加 `artifacts/tmp/reference` 后 `subroutine.index_strategy.*` 全部成功），因此**本轮黄金对照是实际执行结果，不是 BLOCKED**。
- 迁移方式：`backend/domain/strategy/` 九个模块（`config`/`indicators`/`regime`/`entry`/`exit`/`v9_rules`/`factors`/`factor_combo`/`market_data`）逐字复制旧文件，只做两项必要改造——`market_data` 剥离网络/缓存只留数据结构与校验，`indicators.compute_atr14` 去掉对 `subroutine.indicators` 的依赖改为本地等价实现。其余零改动，`git diff --no-index` 对 `entry.py`/`exit.py` 无差异。

## 黄金对照结果（实跑）

| 对照脚本 | 覆盖 | 结果 |
|---|---|---|
| `artifacts/tmp/golden_probe.py` | config / indicators / v9_rules / regime / exit，490 个值 | mismatches 0 |
| `artifacts/tmp/golden_probe2.py` | entry / fill / pyramid / ndx_aux / advance_position，76 个值 | mismatches 0 |
| `artifacts/tmp/make_goldens.py` | 用**旧函数**产出 59 组期望值写入 `artifacts/tmp/goldens.json` | 新实现反向生成期望值的情况为零 |

期望值固化位置：`tests/unit/test_strategy_golden.py`（34 条），全部取自上述旧函数输出或独立人工计算。

## 规则→源码→新实现→黄金测试 对照

| 规则 | 旧符号（实际读取） | 实际参数 | 新实现位置 | 黄金测试 |
|---|---|---|---|---|
| 版本与展示名 | `config.INDEX_STRATEGY_VERSION` / `STRATEGY_DISPLAY_NAME` | `index-strategy-atr-v9.2` / `V9＋G2` | `backend/domain/strategy/config.py`；API `GET /strategy/config` | `test_every_index_keeps_its_own_parameters`、`test_signal_records_its_price_basis_and_input_version`（断言 `strategy_version`） |
| 14 指数映射与个性化参数 | `config.INDEX_STRATEGY_CONFIGS`、`IndexStrategyConfig` | 见下表，全部 exit_after_bars=21 | `config.py`；`backend/domain/strategy/symbols.py` 的 `ETF_INDEX_MAP`/`INDEX_SYMBOLS` | `test_every_index_keeps_its_own_parameters`、`test_etf_link_table_covers_all_fourteen_indices` |
| ATR14 | `indicators.compute_atr14` → `subroutine.indicators.compute_atr14`（`atr14_abs`） | 14 个真实波幅**简单均值**（非 Wilder），`round(x,4)`，需 ≥15 根 | `backend/domain/strategy/indicators.py`（本地实现，无 numpy/pandas 依赖） | `test_atr14_is_the_simple_mean_of_fourteen_true_ranges`（0.65；14 根返回 None） |
| T3 首次突破窗口 | `v9_rules.t3_entry_window`、`config.IndexStrategyConfig.entry_window` | `max(1, floor(0.75×N))`；US 轨=N | `v9_rules.py` / `config.py` | `test_t3_window_is_three_quarters_of_the_original_n`（4→3, 5→3, 15→11, 20→15, 32→24, 40→30） |
| 突破位与突破天数 | `indicators.breakout_threshold` / `breakout_days` | 窗口严格不含信号当日 | `indicators.py` | `test_breakout_excludes_the_signal_bar_itself`（119.0 / 16） |
| 入场判定与闸门顺序 | `entry.evaluate_entry` | 顺序：空/截断→proxy→完整性→`buy_max`→未突破→POSITION_OPEN→PLAN_PENDING→COOLDOWN→PORTFOLIO_FULL→FACTOR_UNVERIFIED→FACTOR_BLOCKED→SIGNAL | `entry.py`；服务层 `backend/application/strategy/service.py:evaluate` | `test_entry_signal_freezes_breakout_atr_and_buy_max`、`test_entry_refuses_proxy_data_and_respects_gates` |
| BuyMax 与次日高开取消 | `entry.evaluate_fill` | `buy_max = close + 0.5×ATR14`；`open > buy_max+1e-9` 即 CANCELLED 且不因盘中回落恢复；缺开盘价 PENDING | `entry.py`；服务 `_evaluate_pending_plan` | `test_next_open_above_buy_max_cancels_and_never_recovers` |
| G2 等权基准与 ER60 | `regime.build_benchmark` / `efficiency_ratio` / `market_regime` | 等权日收益均值累乘、首个 level=100、`active`=最新日仍有行情的成分数；ER60 需 ≥61 点；路径为 0 返回 **0.0**（不是缺失） | `regime.py` | `test_er60_and_market_regime`（up/down/flat/zigzag）、`test_regime_needs_enough_components_and_enough_samples` |
| G2 状态判定 | `regime.market_regime` | `ER60<0.25→RANGE`；否则 `change_pct<0→DOWN`，其余（含 0%）UP；成分<10 或样本不足→UNKNOWN + warning | `regime.py` | 同上 |
| 基础仓与 8 仓容量 | `v9_rules.base_position_amount` / `portfolio_sizing`、`config.BASE_POSITION_DIVISOR`/`PORTFOLIO_MAX_POSITIONS` | 权益÷8；最多 8 个核心槽位；US 轨不计槽位 | `v9_rules.py`；服务 `pool` 的 `core_used` | `test_portfolio_capacity_and_base_amount` |
| 现金不足 | 旧 `service.evaluate_index` 的 `STATUS_CASH_BLOCKED` | 可用现金 < 基础仓目标即不得生成买入计划 | `service.py` 现金闸门（`base_amount_ok`） | `test_insufficient_cash_blocks_the_plan`（`cash_shortfall`） |
| 盈利加仓 S1b+K6 | `v9_rules.evaluate_pyramid` / `g2_pyramid_block` / `apply_pyramid_fill` | 每主仓成功一次；目标 62500；含建仓日 ≥4 日；浮盈 ≥1×冻结ATR；收盘 > 前 10 日最高收盘；`Close>SMA20`；`SMA20/SMA60≥1.02`；RANGE 禁止 | `v9_rules.py`；服务 `evaluate_pyramid` | `test_range_regime_blocks_the_profit_add`（checks 仅 `G2震荡市禁加仓`；UP 下六项顺序固定） |
| 初始止损 / 跟踪触发 / 回撤保护 | `exit.initial_stop_price` / `trail_trigger_price` / `trail_drawdown_price` | `entry − C×ATR`（tick=0.001 向上取整）；`entry + A×ATR`；`peak×(1−B%)` | `exit.py` | `test_stop_and_trail_prices_match_the_original_rounding`（000300: 3.975/4.1/4.275；NDX: 1.75/2.2/2.134） |
| 盘中/跳空止损与移动止盈时点 | `exit.advance_position` | `open<=stop→GAP_STOP@open`；`low<=stop→STOP@stop`；与时间退出同日取 `min(open,close)`；新保护档 `pending_stop` 次日生效，禁止用当日最高价抬升后再用当日最低价触发 | `exit.py`；服务 `_advance` | `test_intraday_stop_and_gap_stop_use_different_prices`、`test_trailing_stop_raises_only_from_the_previous_bar`（pending_stop 4.0945 生效日 = 次一交易日） |
| 子仓独立止损 | `exit._advance_add_stops` | 子仓用自己的成交价与冻结 ATR，只卖子仓 | `exit.py` | 同 `test_intraday_stop_and_gap_stop_use_different_prices`（子仓分支） |
| 时间退出与续期 | `exit.advance_position`、`exit.renewal_allowed`、`config.TIME_EXIT_BARS_AFTER_ENTRY` | `exit_after_bars=21`（建仓日为第 1 日）；续期阈值用**原始 N**（非 0.75N） | `exit.py`；服务 `_advance`（`threshold=max(highs[-N-1:-1])`） | `test_time_exit_on_the_a_share_track` |
| E2a 延持 | `exit.e2a_extend_allowed`、`config.E2A_*` | 最多 10 日；`close>entry` 且强趋势；连续 2 个收盘 < SMA20 → 次日开盘退出 | `exit.py` / `v9_rules.py` | `test_time_exit_on_the_a_share_track`（延持与 streak 分支） |
| 冷却天数 | `exit.cooldown_required_days` / `cooldown_until_date` | RANGE 且 `STOP`/`GAP_STOP` → 10 日；其余 3 日；起算为退出后第 N+1 个交易日 | `exit.py`；服务 `_finish_cooldown`/`_cooldown_state`（按**买入信号日** regime 重算，不冻结卖出日状态） | `test_cooldown_days_depend_on_regime_and_exit_action`、`test_cooldown_until_counts_complete_trading_days` |
| NDX 独立轨 | `config`（`track=TRACK_US`）、`v9_rules.evaluate_ndx_aux` | 原始 N=18 不乘 0.75；不占 A 股槽位；不参与 62500 加仓；第 21 日收盘退出（无续期、无 E2a）；辅助仓 15%→21.5%，>25% 修剪至 24.5%，加仓间隔 10 日、现金缓冲 250000、最小 5000 | `v9_rules.py` / `config.py`；服务 `evaluate_ndx_aux` | `test_ndx_exits_on_its_own_track_without_renewal`、`test_ndx_auxiliary_position_ratios` |
| 因子与因子组合 | `factors.evaluate_factor`、`factor_combo.evaluate_combo` | 五个因子 `verified=False`（严格模式下统一降为 UNVERIFIED，不默认通过）；组合闸门默认 `warn`，`len(bars)<240` 或因子样本不足 → DATA_UNAVAILABLE | `factors.py` / `factor_combo.py`；服务 `_peer_series` | `test_entry_refuses_proxy_data_and_respects_gates`；服务层 `test_unavailable_index_degrades_instead_of_inventing` |
| 指数点位口径 | `market_data.load_series` 与 `load_index_level_series` 的分工；`ETF_UNDERLYING` | 信号一律用**指数点位**；ETF 复权价仅研究口径；ETF 未复权价才是真实市值 | `backend/application/strategy/series.py`（`INDEX_BASIS`/`ETF_ADJUSTED`/`ETF_UNADJUSTED`） | `test_etf_basis_is_never_used_for_the_signal`（服务层）；金样例 `test_entry_refuses_proxy_data_and_respects_gates` |

## 14 指数参数（逐字来自 `config.INDEX_STRATEGY_CONFIGS`，非猜测）

| index_id | 名称 | market/track | N | 入场窗口 | C | A | B% | ETF | 核心槽位 |
|---|---|---|---|---|---|---|---|---|---|
| 000016.SH | 上证50 | CN/CN | 40 | 30 | 0.5 | 0.5 | 8.0 | 510050 | 是 |
| 000300.SH | 沪深300 | CN/CN | 5 | 3 | 0.25 | 1.0 | 5.0 | 510300 | 是 |
| 000905.SH | 中证500 | CN/CN | 20 | 15 | 0.5 | 0.5 | 12.0 | 510500 | 是 |
| 000852.SH | 中证1000 | CN/CN | 15 | 11 | 0.5 | 0.5 | 12.0 | 512100 | 是 |
| 399001.SZ | 深证成指 | CN/CN | 19 | 14 | 0.5 | 7.0 | 3.0 | 159943 | 是 |
| 399006.SZ | 创业板指 | CN/CN | 32 | 24 | 1.75 | 4.0 | 5.0 | 159915 | 是 |
| 000688.SH | 科创50 | CN/CN | 4 | 3 | 0.5 | 6.0 | 4.0 | 588000 | 是 |
| 932000.CSI | 中证2000 | CN/CN | 21 | 15 | 0.75 | 0.5 | 10.0 | 159531 | 是 |
| NDX | 纳斯达克100 | US/US | 18 | 18 | 5.0 | 4.0 | 3.0 | 159941 | **否** |
| HSTECH | 恒生科技 | HK/**CN 轨** | 20 | 15 | 4.0 | 0.5 | 5.0 | 513180 | 是 |
| 931152.CSI | 中证创新药 | CN/CN | 8 | 6 | 2.0 | 3.0 | 3.0 | 159992 | 是 |
| H30269.CSI | 中证红利低波 | CN/CN | 5 | 3 | 2.0 | 0.5 | 5.0 | 563020 | 是 |
| H30007.CSI | 中证芯片产业 | CN/CN | 5 | 3 | 0.75 | 5.0 | 8.0 | 159599 | 是 |
| 399997.SZ | 中证白酒 | CN/CN | 10 | 7 | 1.0 | 5.0 | 3.0 | 161725 | 是 |

注：`HSTECH` 的实际 `track` 是 `CN`（港股标的走 A 股核心仓规则、`clock_market=CN`），不是 `TRACK_US`；只有 `NDX` 是独立轨。旧代码在构造配置后统一 `replace(e2a_enabled=bool(E2A_ENABLED and renewal_enabled))`，因此 NDX 的 `e2a_enabled` 恒为 `False`。

## 安全差异（新实现相对旧实现，均为收紧方向）

1. **旧 kv JSON 账本不再迁移**：`state.py` 的 `StrategyStore`（`kv_store` 前缀键）替换为正式 SQLite 表（迁移 `0008_strategy.py`：`strategy_bars`/`strategy_signals`/`strategy_plans`/`strategy_positions`/`strategy_cooldowns`/`strategy_ndx_aux`）。
2. **策略不写真实账本**：旧 `web_app/trades.py` 的 `link_legacy_strategy_lot`、`_apply_strategy_sell` 等自动落账路径不迁移；真实主/子仓与退出只能经 `TradeService`。`test_refresh_never_touches_the_real_ledger`、`test_store_never_writes_trading_tables` 实测锁定。
3. **信号幂等**：`strategy_signals` 用 `(index_id, signal_date, input_version, strategy_version)` 唯一 + `signal_id` 主键 `ON CONFLICT DO UPDATE`，重复刷新不产生等价新信号（`test_repeated_refresh_keeps_one_signal_per_day`）。
4. **历史回放不用未来数据**：`_live_quantity` 必须传入本次判定的 `as_of`（原实现按"当前时刻"取持仓，回放会读到未来批次）；`series` 一律 `truncated(as_of)`，`validate_series` 拒绝含目标日之后数据的序列（`test_history_replay_never_sees_the_future_bar`）。
5. **权益/现金不可核实时不生成计划**：`equity_fen is None` 或现金基线缺失 → `CASH_BLOCKED` + `cash_unverified`，不把不可核实金额当可用现金。
6. **成交偏离仍入账但标注**：真实发生且偏离策略的成交由 `TradeService` 正常入账，`strategy_deviation`/`strategy_evidence_missing` 标记，策略证据按 `(index_id, signal_date)` 关联，未记录则标 `strategy_position_not_recorded`（`test_deviation_trade_is_kept_and_flagged`、`test_correction_keeps_evidence_traceable`）。
7. **不自动建仓**：次日开盘价通过 `buy_max` 只把计划置为 `AWAITING_CONFIRMATION` 并提示人工核对，不自动生成成交。

## 真实行情来源（本轮实测结论，非推测）

- **东方财富 `push2his.eastmoney.com` 从本机完全不可达**（所有 secid 均 `RemoteProtocolError: Server disconnected without sending a response`；换域名、加 UA、`trust_env=False` 直连同样失败）→ ISS-022 的根因。指数点位因此**不复用**东财适配器（其 `security_market` 白名单只含股票/ETF）。
- 指数点位日线主源：**腾讯** `web.ifzq.gtimg.cn`（12 只：8 只 A 股指数 + `usNDX` + `hkHSTECH`，各 320 根）+ **中证指数官网** `GET csindex-home/perf/index-perf`（4 只 CSI：932000/931152/H30269/H30007；必须 GET、日期 `%Y%m%d` 无横线，360 天切片）。
- 海外指数名称断言：`qt.gtimg.cn/q=usNDX` → 「纳斯达克100」、`q=hkHSTECH` → 「恒生科技指数」，防用综合指数冒充。
- ETF 日线：腾讯 `fqkline`（复权 `qfqday`，研究口径）与 `kline/kline`（未复权 `day`，真实市值口径）分别可取。
- 退役为备用/不可用：新浪 A 股指数 K 线可用但 CSI 系列返回 `null`；新浪港股指数服务全 `Service not valid`；`akshare`/`yfinance` 未安装，本轮不引入。
- 适配器位置：`backend/infrastructure/market/tencent.py`、`backend/infrastructure/market/csindex.py`；装载与缓存：`backend/application/strategy/series.py`（`strategy_bars` 按 `(series_key, trade_date)` 落库，带 `price_basis`/`source`/`source_verified`/`acquired_at`）。
- 实网实测：14 只指数全部取到（2026-11-24；NDX 最后一根为 2026-11-23 因美股未收盘），`GET /strategy/pool/<account>` 返回 `as_of=2026-11-24`、`regime=DOWN`、`er60=0.3054`、`components=13`，14 只均为 `NO_SIGNAL`（阈值未突破），详见本轮报告。

## 尚未完成（不得宣称通过）

- S01 全量验收、`ISS-015` 策略历史回放的完整验收仍由独立审查者裁定；本轮只提供单点历史回放用例。
- 真实券商成交联动、真实旧库迁移留到 08。
