"""策略行情数据结构与完整性校验（纯计算，无网络、无数据库）。

原项目 ``subroutine/index_strategy/market_data.py`` 把这个模块同时用于
「数据结构 + 本地/网络加载」；迁移到 2.0 时只保留**数据结构和校验**，
网络加载与缓存写入全部移到 ``backend/application/strategy.py`` 与
``backend/infrastructure/market/index_series.py``，避免领域层依赖 I/O。

``Bar`` / ``IndexSeries`` 字段、``validate_series`` 的校验顺序与容差、
``min_bars_required`` 的算法与原实现逐字一致：这些是黄金对照的输入契约，
任何改动都会让策略判定与旧系统产生分歧。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import List, Optional, Sequence, Tuple

from .config import ATR_WINDOW


@dataclass(frozen=True)
class Bar:
    """一根已完成日线。价格单位由 ``IndexSeries`` 的口径决定（元或点）。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class IndexSeries:
    """一条标的的日线序列；``is_proxy`` 标记的是「不是该标的本身」的价格。"""

    index_id: str
    name: str
    market: str
    bars: Tuple[Bar, ...]
    source: str
    is_proxy: bool = False
    proxy_for: Optional[str] = None
    proxy_code: Optional[str] = None
    source_verified: bool = True
    warning: str = ""

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def dates(self) -> Tuple[str, ...]:
        return tuple(bar.date for bar in self.bars)

    @property
    def closes(self) -> Tuple[float, ...]:
        return tuple(bar.close for bar in self.bars)

    @property
    def highs(self) -> Tuple[float, ...]:
        return tuple(bar.high for bar in self.bars)

    @property
    def lows(self) -> Tuple[float, ...]:
        return tuple(bar.low for bar in self.bars)

    @property
    def opens(self) -> Tuple[float, ...]:
        return tuple(bar.open for bar in self.bars)

    @property
    def volumes(self) -> Tuple[float, ...]:
        return tuple(bar.volume for bar in self.bars)

    @property
    def last_date(self) -> Optional[str]:
        return self.bars[-1].date if self.bars else None

    def last_bar(self) -> Optional[Bar]:
        return self.bars[-1] if self.bars else None

    def bar_on(self, date: str) -> Optional[Bar]:
        target = str(date)
        for bar in reversed(self.bars):
            if bar.date == target:
                return bar
        return None

    def bars_until(self, date: str) -> Tuple[Bar, ...]:
        """截断到指定日期（含），用于避免使用未来行情。"""
        target = str(date)
        return tuple(bar for bar in self.bars if bar.date <= target)

    def truncated(self, date: str) -> "IndexSeries":
        return replace(self, bars=self.bars_until(date))

    def to_payload(self) -> dict:
        return {
            "index_id": self.index_id,
            "name": self.name,
            "market": self.market,
            "source": self.source,
            "source_verified": self.source_verified,
            "is_proxy": self.is_proxy,
            "proxy_for": self.proxy_for,
            "proxy_code": self.proxy_code,
            "dates": list(self.dates),
            "opens": list(self.opens),
            "highs": list(self.highs),
            "lows": list(self.lows),
            "closes": list(self.closes),
            "volumes": list(self.volumes),
        }

    @classmethod
    def from_payload(cls, payload: dict, *, market: str = "") -> "IndexSeries":
        payload = dict(payload or {})
        dates = list(payload.get("dates") or [])
        opens = list(payload.get("opens") or [])
        highs = list(payload.get("highs") or [])
        lows = list(payload.get("lows") or [])
        closes = list(payload.get("closes") or [])
        volumes = list(payload.get("volumes") or [])
        count = min(len(dates), len(opens), len(highs), len(lows), len(closes))
        bars: List[Bar] = []
        for index in range(count):
            try:
                bars.append(Bar(
                    date=str(dates[index])[:10],
                    open=float(opens[index]),
                    high=float(highs[index]),
                    low=float(lows[index]),
                    close=float(closes[index]),
                    volume=(float(volumes[index])
                            if index < len(volumes) and volumes[index] not in (None, "") else 0.0),
                ))
            except (TypeError, ValueError):
                continue
        return cls(
            index_id=str(payload.get("index_id") or ""),
            name=str(payload.get("name") or ""),
            market=str(payload.get("market") or market),
            bars=tuple(bars),
            source=str(payload.get("source") or "unknown"),
            is_proxy=bool(payload.get("is_proxy")),
            proxy_for=payload.get("proxy_for"),
            proxy_code=payload.get("proxy_code"),
            source_verified=bool(payload.get("source_verified", True)),
            warning=str(payload.get("warning") or ""),
        )


def bars_from_columns(*, dates: Sequence[object], opens: Sequence[object],
                      highs: Sequence[object], lows: Sequence[object],
                      closes: Sequence[object], volumes: Sequence[object] = ()) -> Tuple[Bar, ...]:
    """按列装配日线；非正价格的行直接丢弃（不修数、不补零）。"""
    bars: List[Bar] = []
    count = min(len(dates), len(opens), len(highs), len(lows), len(closes))
    for index in range(count):
        try:
            bar = Bar(
                date=str(dates[index])[:10],
                open=float(opens[index]),
                high=float(highs[index]),
                low=float(lows[index]),
                close=float(closes[index]),
                volume=(float(volumes[index])
                        if index < len(volumes) and volumes[index] not in (None, "") else 0.0),
            )
        except (TypeError, ValueError):
            continue
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            continue
        bars.append(bar)
    return tuple(bars)


def min_bars_required(breakout_window: int) -> int:
    """ATR14 需要 15 根，突破需要 N+1 根。"""
    return max(int(breakout_window) + 1, ATR_WINDOW + 1)


def validate_series(series: Optional[IndexSeries], *, breakout_window: int,
                    as_of: Optional[str] = None) -> dict:
    """完整性校验：根数、重复日期、升序、非正价格、OHLC 自洽、未来数据。"""
    reasons: List[str] = []
    if series is None:
        return {"ok": False, "reasons": ["行情数据不可用"], "bar_count": 0, "last_date": None}
    bars = series.bars
    need = min_bars_required(breakout_window)
    if len(bars) < need:
        reasons.append(f"交易日根数不足：需要 {need} 根，实际 {len(bars)} 根")
    dates = [bar.date for bar in bars]
    if len(set(dates)) != len(dates):
        reasons.append("交易日存在重复")
    if dates != sorted(dates):
        reasons.append("交易日不是升序")
    for bar in bars:
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            reasons.append(f"{bar.date} 存在非正价格")
            break
        # 行情源（例如新浪港股指数的收盘价保留更多小数位）与最高/最低之间存在
        # 1e-4 量级四舍五入差，不是真的 OHLC 矛盾；按相对 1e-6 放宽容差。
        tolerance = max(1e-6, 1e-6 * abs(bar.close))
        if bar.high + tolerance < max(bar.open, bar.close) or \
                bar.low - tolerance > min(bar.open, bar.close):
            reasons.append(f"{bar.date} 的最高/最低价与开收盘不自洽")
            break
    if as_of and bars and str(as_of)[:10] < bars[-1].date:
        reasons.append("行情包含了目标交易日之后的未来数据")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "bar_count": len(bars),
        "last_date": bars[-1].date if bars else None,
        "is_proxy": series.is_proxy,
        "source": series.source,
        "source_verified": series.source_verified,
    }


__all__ = [
    "Bar",
    "IndexSeries",
    "bars_from_columns",
    "min_bars_required",
    "validate_series",
]
