"""V9＋G2 指数策略领域层：纯计算规则，无数据库、无网络、无界面依赖。

模块来源与原项目 ``subroutine/index_strategy/`` 一一对应，逐字迁移以避免
同一规则在新旧系统出现第二套口径：

- ``config``        冻结参数、轨道、因子登记（14 只指数 N/C/A/B）
- ``indicators``    ATR14（14 个 TR 简单均值）、突破阈值/天数、因子原材料
- ``market_data``   ``Bar`` / ``IndexSeries`` 与行情完整性校验
- ``entry``         T3 首次突破准入与次日开盘成交判定
- ``exit``          初始止损、跟踪止盈、V9 到期续期/E2a、冷却
- ``v9_rules``      T3 窗口、S1b+K6 盈利加仓、T5 NDX 辅助仓、基础仓金额
- ``factors``       单因子（全部注明未核实）
- ``factor_combo``  两因子组合闸门
- ``regime``        G2 等权基准 ER60 市场状态
"""
