"""按市场各自的交易日历（A 股 XSHG / 港股 XHKG / 美股 XNYS）与收盘确认。

为什么不能统一用北京时间 15:00：A 股 15:00(Asia/Shanghai)、港股 16:00(Asia/Hong_Kong)、
美股 16:00(America/New_York，夏令时由时区自动处理)。这里保留原项目
``subroutine/index_strategy/calendars.py`` 的口径：优先用 exchange-calendars，
库不可用或日期超出已发布范围时**退化为周一至周五**并把来源标成
``weekday_fallback``，调用方必须把该警告展示出来——绝不把近似日历当成正式日历。
"""

from __future__ import annotations

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


def _load_calendar(name: str):
    if name in _CALENDAR_CACHE:
        return _CALENDAR_CACHE[name]
    calendar = None
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar(name)
    except Exception:
        calendar = None
    _CALENDAR_CACHE[name] = calendar
    return calendar


def _sessions(calendar) -> dict:
    """把交易日缓存成 ``{iso: index}``，避免逐次 is_session 的 pandas 开销。"""
    cached = getattr(calendar, "_dsh_sessions", None)
    if cached is not None:
        return cached
    mapping = {}
    try:
        labels = [str(value)[:10] for value in calendar.sessions]
    except Exception:
        labels = []
    for index, label in enumerate(labels):
        mapping[label] = index
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
        except Exception:
            return ZoneInfo("UTC")

    def _calendar(self):
        return _load_calendar(self.calendar_name)

    @staticmethod
    def _weekday_fallback_is_trading_day(value: str) -> bool:
        return _date.fromisoformat(str(value)[:10]).weekday() < 5

    def is_trading_day(self, value: str) -> bool:
        text = str(value)[:10]
        calendar = self._calendar()
        if calendar is None:
            return self._weekday_fallback_is_trading_day(text)
        try:
            return bool(calendar.is_session(text))
        except Exception:
            return self._weekday_fallback_is_trading_day(text)

    def source_label(self) -> str:
        return self.calendar_source if self._calendar() is not None else "weekday_fallback"

    def next_trading_day(self, value: str) -> Optional[str]:
        text = str(value)[:10]
        calendar = self._calendar()
        if calendar is not None:
            sessions = _sessions(calendar)
            index = sessions.get(text)
            if index is not None:
                for candidate in range(index + 1, len(sessions)):
                    label = str(calendar.sessions[candidate])[:10]
                    return label
            try:
                return str(calendar.next_session(text))[:10]
            except Exception:
                pass
        day = _date.fromisoformat(text)
        for _ in range(15):
            day = day + timedelta(days=1)
            if self.is_trading_day(day.isoformat()):
                return day.isoformat()
        return None

    def trading_days_after(self, value: str, count: int) -> List[str]:
        """返回 ``value`` 之后的连续交易日（升序），最多 ``count`` 个。"""
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
        day = _date.fromisoformat(str(value)[:10])
        return datetime.combine(day, self.close_time, tzinfo=self.tz())

    def is_close_confirmed(self, value: str, *, now: Optional[datetime] = None) -> bool:
        text = str(value)[:10]
        if not self.is_trading_day(text):
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current >= self.close_datetime(text)

    def last_completed_session(self, *, now: Optional[datetime] = None) -> Optional[str]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        local_day = current.astimezone(self.tz()).date()
        for offset in range(0, 20):
            candidate = (local_day - timedelta(days=offset)).isoformat()
            if self.is_close_confirmed(candidate, now=current):
                return candidate
        return None

    def next_completed_session(self, value: str) -> Optional[str]:
        return self.next_trading_day(value)


def market_clock(market: str) -> MarketClock:
    key = str(market)
    if key not in _CALENDAR_NAMES:
        key = MARKET_CN
    return MarketClock(
        market=key,
        calendar_name=_CALENDAR_NAMES[key],
        local_timezone=_LOCAL_TIMEZONES[key],
        close_time=_CLOSE_TIMES[key],
    )


def all_market_clock_warnings() -> dict:
    """各市场日历来源；fallback 时界面必须提示"按工作日近似，未核对交易所日历"。"""
    return {market: market_clock(market).source_label()
            for market in (MARKET_CN, MARKET_HK, MARKET_US)}


__all__ = ["MarketClock", "all_market_clock_warnings", "market_clock"]
