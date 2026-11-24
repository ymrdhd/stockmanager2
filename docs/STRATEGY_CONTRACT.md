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
- 实际成交即使偏离策略计划，也必须能记录事实并标注偏离；不可因信号否决或无行情而丢弃真实成交。批次/规则缺失时进入待核对状态，阻止不可信策略提示，不伪造冻结 ATR。
- 主仓和加仓子仓独立身份、原始信号日期及 ATR；保护价与退出以适用规则确定。卖出分配对应真实批次；策略版本升级不追溯改旧批次规则，除非明确获批且保留审计。
- 仅已完成且有效的输入可生成正式信号；未来数据、未核实因子、失效代理禁止冒充正式证据。现金不可靠时不给可执行数量；不得把账户总权益当现金。
- 同一输入版本重复评估结果一致；不同 as_of 不复用未来行情；失效输出不覆盖历史证据。

## 4. 市场数据与 Fed

已有数据源适配优先，使用可验证的数据提供商；每个报价保留市场、标的、币种、价格单位、时间、来源、有效期、代理标记与复权口径。真实成交用未复权实际成交价；复权研究与账户估值区分。公司行为缺证据时标注不可计算。市场休市按交易日历，不用系统本地日期猜测；限流、超时、重试有上限，网络操作不占写事务。研究输入如无法重新获取，应保存可复现的最小快照。

Fed 页面（V1.5，不阻断 V1.0；ADR-013）保留 EFFR、目标区间、会议概率/路径、上一日/上一周比较、CPI Nowcast 和实际利率代理；明确来源与时点，不能将预测当已发生数据。数据不可用时展示状态，不回写 V9/G2 正式裁决。

## 5. 验收

执行 ACCEPTANCE_TESTS S01–S06 与新增 S07/S08。正式发布前应具备可复现的规则映射和对照报告；未经裁决的差异阻断受影响策略上线。禁止把旧模块的存在或旧 README 的表述当成迁移完成证据。
