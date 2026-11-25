"""V9/G2 策略应用层：行情装载、状态持久化、信号与持仓决策。

本包只做**衍生计算与证据记录**：权威交易账本（trade_executions / trade_lots /
cash_movements）仍由 TradeService 独占写入。策略层可以读账本、可以写自己的
信号/计划/持仓推进证据，绝不直接改真实成交或现金。

价格口径（ISS-031）：V9 用 ETF 未复权人民币日线，G2 用指数点位；两者单位不同，
不得混算，也不得把指数点位 ATR 预填成人民币成交。
"""

from __future__ import annotations

__all__ = ["SeriesError", "StrategyCalendarUnavailable", "StrategyData", "StrategyService",
           "StrategyStore"]

from .series import SeriesError, StrategyData
from .service import StrategyCalendarUnavailable, StrategyService
from .store import StrategyStore
