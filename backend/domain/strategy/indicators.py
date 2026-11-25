"""指数策略使用的指标：ATR14、突破阈值、下行波动率、趋势效率、广度。

关键口径约束：
- ATR 是 14 个 TR 的**简单均值**（不用 Wilder 平滑），并对结果 ``round(x, 4)``；
  这正是原项目 ``subroutine.indicators.compute_atr14`` 的口径，本模块本地复刻
  同一算式并保留同样的取整，避免新旧系统出现两套 ATR。
- 突破阈值严格不含当日：``MAX(High[T-N : T-1])``。
- 下行波动率 / 趋势效率 / 广度属于"因子未验证"项（见 ``config.FACTOR_SPECS``）：
  这里给出的是标准公式实现，未与原始研究报告核对，调用方必须保留 verified 标记。
"""

from __future__ import annotations

import math
from statistics import fmean, pstdev
from typing import Iterable, Optional, Sequence

from .config import ATR_WINDOW


def _floats(values: Iterable[object]) -> list:
    out = []
    for raw in values:
        try:
            out.append(float(raw))
        except (TypeError, ValueError):
            return []
    return out


def compute_atr14(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> Optional[float]:
    """ATR14 = 最近 14 个真实波幅的简单均值，结果取 4 位小数。

    真实波幅 ``TR[i] = max(H[i]-L[i], |H[i]-C[i-1]|, |L[i]-C[i-1]|)``，其中
    ``i`` 覆盖最近 15 根日线里的后 14 根（首根只作为前收盘）。数据不足 15 根、
    长度不一致、或结果非有限/非正一律返回 ``None``——绝不拿不足的窗口凑一个值。

    这里手写循环而不是调 numpy/pandas：本函数在逐日回放里每根 bar 都要跑，
    纯 Python 版本少一次 Series 构造，且取整口径与原项目完全一致（``round(x, 4)``）。
    """
    highs_f, lows_f, closes_f = _floats(highs), _floats(lows), _floats(closes)
    if not (len(highs_f) == len(lows_f) == len(closes_f)) or not closes_f:
        return None
    if len(closes_f) < ATR_WINDOW + 1:
        return None
    tail_highs = highs_f[-ATR_WINDOW - 1:]
    tail_lows = lows_f[-ATR_WINDOW - 1:]
    tail_closes = closes_f[-ATR_WINDOW - 1:]
    ranges = []
    for index in range(ATR_WINDOW):
        high = tail_highs[index + 1]
        low = tail_lows[index + 1]
        prev_close = tail_closes[index]
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    value = round(sum(ranges) / float(ATR_WINDOW), 4)
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def breakout_threshold(highs: Sequence[float], window: int) -> Optional[float]:
    """``MAX(High[T-N : T-1])``：严格不含当日最高价。"""
    highs_f = _floats(highs)
    window = int(window)
    if window <= 0 or len(highs_f) < window + 1:
        return None
    window_highs = highs_f[-(window + 1):-1]
    if not window_highs:
        return None
    return max(window_highs)


def breakout_days(highs: Sequence[float], closes: Sequence[float], window: int) -> int:
    """连续突破天数（含当日）：从最新一根往前数，收盘仍高于"当日视角"的 N 日最高价。

    每一天用它自己那一天的阈值判定（``MAX(High[i-N : i-1])``），而不是拿今天的阈值
    去回测历史收盘——后者会在阈值逐日抬高时把真实突破天数算少。
    数据不足或从未突破返回 0。
    """
    highs_f, closes_f = _floats(highs), _floats(closes)
    window = int(window)
    if window <= 0 or len(highs_f) != len(closes_f) or len(closes_f) < window + 1:
        return 0
    days = 0
    for i in range(len(closes_f) - 1, window - 1, -1):
        if closes_f[i] > max(highs_f[i - window:i]):
            days += 1
        else:
            break
    return days


def daily_returns(closes: Sequence[float]) -> list:
    closes_f = _floats(closes)
    if len(closes_f) < 2:
        return []
    returns = []
    for prev, cur in zip(closes_f[:-1], closes_f[1:]):
        if prev <= 0:
            return []
        returns.append((cur - prev) / prev)
    return returns


def downside_volatility(closes: Sequence[float], window: int = 20) -> Optional[float]:
    """20 日下行波动率（只取负收益样本的标准差，未年化）。

    定义未与原始报告核对（见 ``config.FACTOR_SPECS``）：这里用总体标准差 ddof=0，
    负收益样本少于 2 个时返回 ``None``（宁可判为数据不足，也不虚构通过）。
    """
    returns = daily_returns(closes)
    if len(returns) < int(window):
        return None
    sample = [r for r in returns[-int(window):] if r < 0]
    if len(sample) < 2:
        return None
    return float(pstdev(sample))


def trend_efficiency(closes: Sequence[float], window: int = 20) -> Optional[float]:
    """趋势效率（Kaufman 效率比）：窗口净变动 / 逐日变动绝对值之和。

    定义未与原始报告核对（见 ``config.FACTOR_SPECS``）。
    """
    closes_f = _floats(closes)
    window = int(window)
    if window <= 0 or len(closes_f) < window + 1:
        return None
    segment = closes_f[-(window + 1):]
    net = abs(segment[-1] - segment[0])
    path = sum(abs(b - a) for a, b in zip(segment[:-1], segment[1:]))
    if path <= 0:
        return None
    return float(net / path)


def moving_average_last(closes: Sequence[float], window: int) -> Optional[float]:
    closes_f = _floats(closes)
    window = int(window)
    if window <= 0 or len(closes_f) < window:
        return None
    return float(fmean(closes_f[-window:]))


def ma_breadth(series_by_code: dict, window: int = 60) -> Optional[dict]:
    """``{code: {"closes": [...]}}`` → 收盘价站上自身 N 日均线的比例。"""
    hits = 0
    total = 0
    details = {}
    for code, payload in sorted(dict(series_by_code or {}).items()):
        closes = (payload or {}).get("closes") or []
        ma = moving_average_last(closes, window)
        closes_f = _floats(closes)
        if ma is None or not closes_f:
            details[str(code)] = None
            continue
        total += 1
        ok = closes_f[-1] > ma
        hits += 1 if ok else 0
        details[str(code)] = bool(ok)
    if total <= 0:
        return None
    return {"ratio": hits / total, "hits": hits, "total": total, "details": details}


def breakout_breadth(series_by_code: dict, window: int = 20) -> Optional[dict]:
    """同市场其他指数中，当日收盘创 N 日新高的比例（收盘 > 前 N 日最高收盘）。"""
    hits = 0
    total = 0
    details = {}
    window = int(window)
    for code, payload in sorted(dict(series_by_code or {}).items()):
        closes = _floats((payload or {}).get("closes") or [])
        if len(closes) < window + 1:
            details[str(code)] = None
            continue
        total += 1
        ok = closes[-1] > max(closes[-(window + 1):-1])
        hits += 1 if ok else 0
        details[str(code)] = bool(ok)
    if total <= 0:
        return None
    return {"ratio": hits / total, "hits": hits, "total": total, "details": details}


def ceil_to_tick(price: float, tick: float = 0.001) -> float:
    """按最小报价单位向上取整（止损/保本价宁可早触发，宁可卖低）。"""
    price = float(price)
    tick = float(tick)
    if not math.isfinite(price) or tick <= 0:
        return price
    steps = math.ceil(round(price / tick, 9))
    return round(steps * tick, 6)


def round_price(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), int(digits))
    except (TypeError, ValueError):
        return None


__all__ = [
    "breakout_breadth",
    "breakout_days",
    "breakout_threshold",
    "ceil_to_tick",
    "compute_atr14",
    "daily_returns",
    "downside_volatility",
    "ma_breadth",
    "moving_average_last",
    "round_price",
    "trend_efficiency",
]
