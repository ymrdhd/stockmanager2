"""Mainland exchange session boundaries; never infer a session from weekdays."""
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo('Asia/Shanghai')


class CalendarUnavailable(ValueError):
    """No verified exchange session for the requested date."""


@lru_cache(maxsize=1)
def calendar():
    # XSHG is a precomputed mainland holiday calendar. Use its actual published
    # bounds, not exchange_calendars' rolling default start/end dates.
    try:
        import exchange_calendars as xc
        from exchange_calendars.exchange_calendar_xshg import XSHGExchangeCalendar
        return xc.get_calendar('XSHG', start=XSHGExchangeCalendar.bound_min(),
                               end=XSHGExchangeCalendar.bound_max())
    except (ImportError, ValueError) as exc:
        raise CalendarUnavailable('verified exchange calendar unavailable') from exc


def _covered(day: date):
    cal = calendar()
    if not cal.first_session.date() <= day <= cal.bound_max().date():
        raise CalendarUnavailable('mainland calendar does not cover date')
    return cal


def session_bounds(day: date) -> tuple[datetime, datetime]:
    cal = _covered(day)
    if not cal.is_session(day.isoformat()):
        raise CalendarUnavailable('not a mainland trading session')
    return (cal.session_open(day.isoformat()).to_pydatetime(),
            cal.session_close(day.isoformat()).to_pydatetime())


def quote_time(day: date, observed: datetime, kind: str) -> datetime:
    opened, closed = session_bounds(day)
    if kind == 'close':
        if observed < closed:
            raise CalendarUnavailable('close evidence predates exchange close')
        return closed
    if not opened <= observed <= closed or observed.astimezone(SHANGHAI).date() != day:
        raise CalendarUnavailable('intraday observation outside trading session')
    cal = calendar()
    if cal.session_break_start(day.isoformat()).to_pydatetime() < observed < cal.session_break_end(day.isoformat()).to_pydatetime():
        raise CalendarUnavailable('intraday observation during exchange recess')
    return observed


@dataclass(frozen=True)
class MarketWindow:
    day: date
    is_session: bool
    previous: date
    latest_completed: date
    phase: str  # preopen, trading, break, after_close, closed

    @property
    def valuation_day(self) -> date:
        return self.day if self.phase in ('trading', 'break', 'after_close') else self.latest_completed


def market_window(at: datetime) -> MarketWindow:
    if at.utcoffset() is None:
        raise CalendarUnavailable('market timestamp requires timezone')
    day = at.astimezone(SHANGHAI).date()
    cal = _covered(day)
    label = day.isoformat()
    if not cal.is_session(label):
        previous = cal.date_to_session(label, direction='previous').date()
        return MarketWindow(day, False, previous, previous, 'closed')
    if day == cal.first_session.date():
        raise CalendarUnavailable('previous trading session not covered')
    previous = cal.previous_session(label).date()
    opened, closed = session_bounds(day)
    if at < opened:
        return MarketWindow(day, True, previous, previous, 'preopen')
    if at >= closed:
        return MarketWindow(day, True, previous, day, 'after_close')
    phase = 'break' if cal.session_break_start(label).to_pydatetime() <= at < cal.session_break_end(label).to_pydatetime() else 'trading'
    return MarketWindow(day, True, previous, previous, phase)


def freshness_reference(at: datetime, window: MarketWindow) -> datetime:
    # Lunch recess does not age a valid pre-break quote artificially.
    if window.phase == 'break':
        return calendar().session_break_start(window.day.isoformat()).to_pydatetime()
    return at


def calendar_coverage(at: datetime) -> dict:
    """Expose installed verified bounds even when the requested day is outside."""
    try:
        cal = calendar()
    except CalendarUnavailable:
        return {'start': None, 'end': None, 'status': 'unavailable'}
    start, end = cal.first_session.date(), cal.bound_max().date()
    day = at.astimezone(SHANGHAI).date()
    days = (end-day).days
    status = ('outside_coverage' if not start <= day <= end else
              'expiring' if days <= 60 else 'valid')
    return {'start': start.isoformat(), 'end': end.isoformat(), 'days_remaining': days,
            'status': status, 'calendar': 'XSHG'}


def ordinary_execution_time(at: datetime, market: str) -> str | None:
    """Ordinary mainland exchange fills; special after-hours facts need review."""
    if market not in ('SH', 'SZ'):
        return 'execution_calendar_unsupported_market'
    day = at.astimezone(SHANGHAI).date()
    try:
        cal = _covered(day)
        if not cal.is_session(day.isoformat()):
            return 'execution_non_trading_day'
        opened, closed = session_bounds(day)
        # Opening auction matches at 09:25. Order submission windows are not fills.
        local = at.astimezone(SHANGHAI).time()
        if time(9,25) <= local < time(9,26):
            return None
        if not opened <= at <= closed:
            return 'execution_outside_session'
        if (cal.session_break_start(day.isoformat()).to_pydatetime() < at <
                cal.session_break_end(day.isoformat()).to_pydatetime()):
            return 'execution_outside_session'
    except CalendarUnavailable:
        return 'execution_calendar_unavailable'
    return None
