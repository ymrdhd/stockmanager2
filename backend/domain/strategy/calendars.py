"""按市场各自的交易日历（A 股 XSHG / 港股 XHKG / 美股 XNYS）与收盘确认。

为什么不能统一用北京时间 15:00：A 股 15:00(Asia/Shanghai)、港股 16:00(Asia/Hong_Kong)、
美股 16:00(America/New_York，夏令时由时区自动处理)。

ISS-035：正式决策路径**没有** weekday_fallback。交易所日历不可用，或请求日期超出日历
已发布覆盖范围时，一律抛 ``CalendarCoverageError``，调用方必须降级为
``DATA_UNAVAILABLE`` 并给出真实原因：

- 不得按“周一至周五”近似交易日；
- 不得编造一个下一交易日；
- 不得伪造冷却完成日期；
- 2027 年的日历尚未发布时同样不猜。

ETF 模式下的**执行日历**取 ETF 所在市场（A 股 XSHG）；G2 指数日线的完成时点取该指数
所属市场日历。两者不能因为指数在海外就互相顶替。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date as _date, datetime, time as _time, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from .config import MARKET_CN, MARKET_HK, MARKET_US

_CALENDAR_NAMES = {MARKET_CN: "XSHG", MARKET_HK: "XHKG", MARKET_US: "XNYS"}
_LOCAL_TIMEZONES = {
    MARKET_CN: "Asia/Shanghai",
    MARKET_HK: "Asia/Hong_Kong",
    MARKET_US: "America/New_York",
}
_CLOSE_TIMES = {MARKET_CN: _time(15, 0), MARKET_HK: _time(16, 0), MARKET_US: _time(16, 0)}
_CALENDAR_CACHE: dict = {}


class CalendarCoverageError(RuntimeError):
    """交易所日历不可用，或不覆盖请求的日期；``reason`` 可直接展示给用户。"""

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _load_calendar(name: str):
    if name in _CALENDAR_CACHE:
        return _CALENDAR_CACHE[name]
    calendar = None
    try:
        import exchange_calendars as xcals

        if name == 'XSHG':
            from .calendar_release import reviewed_xshg
            calendar = reviewed_xshg()
        else:
            calendar = xcals.get_calendar(name)
    except Exception:
        calendar = None
    _CALENDAR_CACHE[name] = calendar
    return calendar


def _session_labels(calendar) -> List[str]:
    """已发布交易日标签（升序）；缓存起来避免每次触碰 pandas 索引。"""
    cached = getattr(calendar, "_dsh_labels", None)
    if cached is not None:
        return cached
    try:
        labels = sorted({str(value)[:10] for value in calendar.sessions})
    except Exception:
        labels = []
    try:
        calendar._dsh_labels = labels
    except Exception:
        pass
    return labels


def _sessions(calendar) -> dict:
    """把交易日缓存成 ``{iso: index}``，避免逐次 is_session 的 pandas 开销。"""
    cached = getattr(calendar, "_dsh_sessions", None)
    if cached is not None:
        return cached
    mapping = {label: index for index, label in enumerate(_session_labels(calendar))}
    try:
        calendar._dsh_sessions = mapping
    except Exception:
        pass
    return mapping


@dataclass(frozen=True)
class MarketClock:
    market: str
    calendar_name: str
    local_timezone: str = "Asia/Shanghai"
    close_time: _time = _time(15, 0)
    calendar_source: str = "exchange_calendars"

    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.local_timezone)
        except Exception as exc:
            raise CalendarCoverageError("市场时区不可用", detail=self.local_timezone) from exc

    def _calendar(self):
        calendar = _load_calendar(self.calendar_name)
        if calendar is None:
            raise CalendarCoverageError(
                "交易所日历不可用", detail=f"calendar_unavailable:{self.calendar_name}")
        return calendar

    def covered(self, value: str) -> bool:
        """该日期是否落在日历已发布的覆盖范围内（不判断是否为交易日）。"""
        text = str(value)[:10]
        calendar = _load_calendar(self.calendar_name)
        if calendar is None:
            return False
        labels = _session_labels(calendar)
        if not labels:
            return False
        return labels[0] <= text <= (str(calendar.bound_max().date()) if self.calendar_name == 'XSHG' else labels[-1])

    def is_trading_day(self, value: str) -> bool:
        text = str(value)[:10]
        calendar = self._calendar()
        labels = _session_labels(calendar)
        if not labels or not self.covered(text):
            raise CalendarCoverageError(
                "交易日历未覆盖该日期",
                detail=f"calendar_out_of_coverage:{self.calendar_name}:{text}")
        return text in _sessions(calendar)

    def source_label(self) -> str:
        return self.calendar_source if _load_calendar(self.calendar_name) is not None else "unavailable"

    def next_trading_day(self, value: str) -> Optional[str]:
        """``value`` 之后的第一个交易日；不可知时返回 ``None``（绝不猜）。"""
        text = str(value)[:10]
        calendar = _load_calendar(self.calendar_name)
        if calendar is None:
            return None
        labels = _session_labels(calendar)
        if not labels or text < labels[0] or text >= labels[-1]:
            return None
        index = _sessions(calendar).get(text)
        if index is None:
            position = bisect_right(labels, text)
            return labels[position] if position < len(labels) else None
        return labels[index + 1] if index + 1 < len(labels) else None

    def trading_days_after(self, value: str, count: int) -> List[str]:
        """返回 ``value`` 之后的连续交易日（升序），最多 ``count`` 个。

        日历覆盖不足时提前结束，返回的列表可能短于 ``count``；调用方必须按
        “日历不完整”处理，而不是把缺失的交易日当成已冷却。
        """
        out: List[str] = []
        cursor = str(value)[:10]
        for _ in range(max(0, int(count))):
            nxt = self.next_trading_day(cursor)
            if not nxt:
                break
            out.append(nxt)
            cursor = nxt
        return out

    def close_datetime(self, value: str) -> datetime:
        text = str(value)[:10]
        if not self.is_trading_day(text):
            raise CalendarCoverageError("非交易日无收盘时点", detail=text)
        return self._calendar().session_close(text).to_pydatetime()

    def is_close_confirmed(self, value: str, *, now: Optional[datetime] = None) -> bool:
        text = str(value)[:10]
        if not self.is_trading_day(text):
            return False
        current = now or datetime.now(timezone.utc)
        if current.utcoffset() is None:
            raise CalendarCoverageError("缺少可信时区")
        return current >= self.close_datetime(text)

    def last_completed_session(self, *, now: Optional[datetime] = None) -> Optional[str]:
        current = now or datetime.now(timezone.utc)
        if current.utcoffset() is None:
            raise CalendarCoverageError("缺少可信时区")
        local_day = current.astimezone(self.tz()).date()
        if not self.covered(local_day.isoformat()):
            raise CalendarCoverageError(
                "交易日历未覆盖今天",
                detail=f"calendar_out_of_coverage:{self.calendar_name}:{local_day.isoformat()}")
        for offset in range(0, 20):
            candidate = (local_day - timedelta(days=offset)).isoformat()
            try:
                confirmed = self.is_close_confirmed(candidate, now=current)
            except CalendarCoverageError:
                return None
            if confirmed:
                return candidate
        return None

    def next_completed_session(self, value: str) -> Optional[str]:
        return self.next_trading_day(value)


def market_clock(market: str) -> MarketClock:
    key = str(market)
    if key not in _CALENDAR_NAMES:
        raise CalendarCoverageError("未知市场", detail=key)
    return MarketClock(
        market=key,
        calendar_name=_CALENDAR_NAMES[key],
        local_timezone=_LOCAL_TIMEZONES[key],
        close_time=_CLOSE_TIMES[key],
    )


def all_market_clock_warnings() -> dict:
    """各市场日历来源；``unavailable`` 时界面必须提示“无法确认交易日”。"""
    return {market: market_clock(market).source_label()
            for market in (MARKET_CN, MARKET_HK, MARKET_US)}


__all__ = [
    "CalendarCoverageError",
    "MarketClock",
    "all_market_clock_warnings",
    "market_clock",
]
