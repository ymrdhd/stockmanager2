"""G2 市场状态：14 指数等权基准的 ER60（Kaufman 效率比）。

口径（与 G2 回测一致，**只用已完成行情**）：

1. 基准 = 池内成分**等权**：逐日取各成分日收益的算术平均，链式累乘成净值曲线
   （等价于每日等权再平衡）；
2. ``ER60 = |净值_t − 净值_{t−60}| / Σ|净值_i − 净值_{i−1}|``，窗口 60 个交易日；
3. ``ER60 < 0.25`` → ``RANGE``（震荡市）；
   ``ER60 ≥ 0.25`` 且过去 60 日净值上涨 → ``UP``；下跌 → ``DOWN``；
4. 缺失处理：某成分当日无行情（停牌/未上市）按上一可用收盘前向填充，当日不参与均值；
   参与成分少于 ``G2_REGIME_MIN_COMPONENTS``（默认 10/14）或有效净值点不足 61 个
   → ``UNKNOWN``。**UNKNOWN 不套用任何 G2 限制**（退回 V9 原行为），绝不猜测。

本模块只做纯计算，不读数据库、不碰 UI；``as_of`` 之后的行情一律先截断再计算，
因此盘中使用也不会把未来数据算进来。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .config import (
    G2_ER60_THRESHOLD,
    G2_ER60_WINDOW,
    G2_REGIME_MIN_COMPONENTS,
    REGIME_DOWN,
    REGIME_RANGE,
    REGIME_UNKNOWN,
    REGIME_UP,
)

#: 状态 → 展示名（界面、提醒共用同一套文字）。
REGIME_LABELS: Dict[str, str] = {
    REGIME_RANGE: "RANGE震荡市",
    REGIME_UP: "UP上行市",
    REGIME_DOWN: "DOWN下行市",
    REGIME_UNKNOWN: "状态未知",
}

#: 状态 → 极短标签（表格列只放得下标签）。
REGIME_SHORT: Dict[str, str] = {
    REGIME_RANGE: "RANGE",
    REGIME_UP: "UP",
    REGIME_DOWN: "DOWN",
    REGIME_UNKNOWN: "--",
}


@dataclass(frozen=True)
class RegimeSnapshot:
    """一次市场状态判定的不可变快照（可直接进界面/提醒）。"""

    state: str = REGIME_UNKNOWN
    er60: Optional[float] = None
    change_pct: Optional[float] = None
    as_of: str = ""
    components: int = 0
    points: int = 0
    warning: str = ""

    @property
    def known(self) -> bool:
        return self.state != REGIME_UNKNOWN

    @property
    def is_range(self) -> bool:
        return self.state == REGIME_RANGE

    @property
    def label(self) -> str:
        return REGIME_LABELS.get(self.state, REGIME_LABELS[REGIME_UNKNOWN])

    @property
    def short(self) -> str:
        return REGIME_SHORT.get(self.state, "--")


def _close_map(series, as_of: Optional[str]) -> Dict[str, float]:
    """单只成分的 ``{date: close}``；代理行情与非法收盘价直接丢弃。"""
    if series is None:
        return {}
    if bool(getattr(series, "is_proxy", False)):
        return {}
    bars = getattr(series, "bars", ()) or ()
    if as_of:
        bars = [bar for bar in bars if str(bar.date) <= str(as_of)]
    out: Dict[str, float] = {}
    for bar in bars:
        try:
            close = float(bar.close)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        out[str(bar.date)[:10]] = close
    return out


def build_benchmark(
    series_map: Mapping[str, object],
    *,
    as_of: Optional[str] = None,
) -> Tuple[List[str], List[float], int]:
    """等权基准净值曲线：返回 ``(日期序列, 净值序列, 末日仍有行情的成分数)``。

    净值以第一日 = 100.0 起算；某日若所有成分都无行情则净值不前进（不虚构成交）。
    """
    maps = {}
    for key, series in dict(series_map or {}).items():
        closes = _close_map(series, as_of)
        if closes:
            maps[str(key)] = closes
    if not maps:
        return [], [], 0

    all_dates = sorted({d for closes in maps.values() for d in closes})
    levels: List[float] = []
    last: Dict[str, float] = {}
    level = 100.0
    for idx, day in enumerate(all_dates):
        returns: List[float] = []
        for key, closes in maps.items():
            close = closes.get(day)
            if close is None:
                continue
            prev = last.get(key)
            last[key] = close
            if prev and prev > 0:
                returns.append(close / prev - 1.0)
        if idx == 0:
            levels.append(level)
            continue
        if returns:
            level *= 1.0 + (sum(returns) / float(len(returns)))
        levels.append(level)

    if not all_dates:
        return [], [], 0
    last_day = all_dates[-1]
    active = sum(1 for closes in maps.values() if max(closes) >= last_day)
    return all_dates, levels, int(active)


def efficiency_ratio(levels: Sequence[float], window: int = G2_ER60_WINDOW) -> Optional[float]:
    """``|末值 − 首值| / Σ|逐日变动|``；样本不足返回 ``None``，零路径返回 ``0.0``。"""
    seq = [float(v) for v in (levels or ()) if v is not None]
    window = int(window)
    if window <= 0 or len(seq) < window + 1:
        return None
    segment = seq[-(window + 1):]
    net = abs(segment[-1] - segment[0])
    path = sum(abs(b - a) for a, b in zip(segment[:-1], segment[1:]))
    if path <= 0:
        # 60 日净值完全不动：效率比为 0（典型 RANGE），不是"数据不可用"。
        return 0.0
    return float(net / path)


def market_regime(
    series_map: Mapping[str, object],
    *,
    as_of: Optional[str] = None,
    window: int = G2_ER60_WINDOW,
    threshold: float = G2_ER60_THRESHOLD,
    min_components: int = G2_REGIME_MIN_COMPONENTS,
) -> RegimeSnapshot:
    """按 14 指数等权基准判定市场状态（只用 ``as_of`` 及之前的已完成行情）。"""
    dates, levels, active = build_benchmark(series_map, as_of=as_of)
    if not levels:
        return RegimeSnapshot(as_of=str(as_of or ""), warning="等权基准无可用行情")

    er60 = efficiency_ratio(levels, window)
    segment = levels[-(int(window) + 1):]
    if er60 is None:
        return RegimeSnapshot(
            er60=None,
            as_of=str(as_of or ""),
            components=active,
            points=len(levels),
            warning=f"等权基准净值样本仅 {len(levels)} 个，不足 {int(window) + 1} 个",
        )
    if active < int(min_components):
        return RegimeSnapshot(
            er60=er60,
            as_of=str(as_of or ""),
            components=active,
            points=len(levels),
            warning=f"参与等权基准的成分只有 {active} 只，不足 {int(min_components)} 只",
        )

    change_pct = (segment[-1] / segment[0] - 1.0) * 100.0 if segment and segment[0] > 0 else None
    if er60 < float(threshold):
        state = REGIME_RANGE
    elif change_pct is not None and change_pct < 0:
        state = REGIME_DOWN
    else:
        # 含「恰好 0%」这种理论边界：按上涨处理（不做三种状态之外的第四种）。
        state = REGIME_UP
    return RegimeSnapshot(
        state=state,
        er60=float(er60),
        change_pct=change_pct,
        as_of=str(as_of or (dates[-1] if dates else "")),
        components=active,
        points=len(levels),
    )


__all__ = [
    "REGIME_LABELS",
    "REGIME_SHORT",
    "RegimeSnapshot",
    "build_benchmark",
    "efficiency_ratio",
    "market_regime",
]
