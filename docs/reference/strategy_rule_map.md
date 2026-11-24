# 04/04B-strategy 原规则证据清单（未完成迁移）

## 来源状态

- 目标 ZIP：`stock-1_20261122_183448.zip`。04B 再次在提供的工作区及会话附件中核验，文件仍未找到；独立审查给出的预期大小为 1749267 字节、预期 SHA256 为 `1edc58feabbe2e32cda9bfb2bd34f205f4cd3c4b0b3e008e5910002b6af6cc06`，**未实测验证**。未读取、解压或复制旧源码。
- `index-strategy-atr-v9.2` / `V9＋G2` 是任务和 STRATEGY_CONTRACT 给出的源码版本线索，**未实际核验**。现场配置、指数成分、ETF 映射、额外因子、实际数据源均未知，不将默认值冒充用户运行配置。
- 缺失证据时禁止生成正式规则、止损、仓位、G2 裁决或可执行数量。现有真实批次独立保留；证据查询明确 `unavailable/unverified`，不能作为策略建议。

| 规则/证据 | 待定位旧符号（来自契约，非已读证据） | 输入/输出待核对 | 本轮实际对照 |
|---|---|---|---|
| G2 ER60、成分、状态 | `regime.py`, `config.py`, `service.py` | 14 指数、窗口60、阈值0.25、最低10等任务线索须实际核实；UP/RANGE/DOWN/UNKNOWN | NOT RUN，未取得源码 |
| V9 准入、因子 | `entry.py`, `v9_rules.py`, `factors.py`, `factor_combo.py`, `indicators.py` | 已完成日线、因子验证、入场/否决 | NOT RUN |
| 加仓与冷却 | `regime.py`, `entry.py`, `state.py` | 主/子仓、RANGE冷却10天及默认禁止盈利加仓为待核线索 | NOT RUN |
| 止损与退出 | `exit.py`, `v9_rules.py` | 普通/跳空止损、保护价、时间退出、数量 | NOT RUN |
| 观察池与持仓决策 | `service.py: evaluate_index/evaluate_all/evaluation_to_row/build_rows`, `web_app/server.py` | 规则版本、配置哈希、输入/输出、市场日历 | NOT RUN |
| 批次证据 | `web_app/trades.py: _frozen_atr/_allocate_sell/_apply_strategy_sell/record_manual_execution/link_legacy_strategy_lot`, `state.py: StrategyStore` | 真实成交与信号关联、主/子仓、冻结ATR、偏离 | 既有 TradeService 批次保护及新行情刷新隔离测试 PASS；旧新规则/信号版本对照 NOT RUN |
| 行情边界 | `market_data.py`, `etf_quote.py`, `etf_link.py`, `calendars.py` | 原数据源、指数/ETF 映射、真实完成日线与盘中候选 | 未取得旧源；新证券适配器单独测试，非迁移一致性证据 |

## 本轮独立实现的证券行情来源证据

东方财富 HTTPS 接口的参数格式参考 AKShare 公开适配器（不是旧策略实际数据源证明）：
- <https://github.com/akfamily/akshare/blob/main/akshare/fund/fund_etf_em.py>，`fund_etf_hist_em` 日线接口，使用未复权口径 `fqt=0`。
- <https://github.com/akfamily/akshare/blob/main/akshare/stock/stock_info_em.py>，`stock_individual_info_em` 证券行情接口。
- <https://github.com/WangYang-Rex/eastmoney-data-sdk/blob/main/docs/API_FIELDS.md>，行情时间字段参考。接口非交易所 SLA，当前真实响应未取得，字段及覆盖仍需实网核验。

固定夹具：`tests/unit/test_market.py`，覆盖节假日、实际昨收、闭市价格、历史补录/可获得性、实际/代理价格隔离、冻结 ATR、刷新/重启/迁移等证券基础安全。无旧代码可执行对照，S01 和策略黄金样例不得宣称通过。
