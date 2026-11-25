"""Request-scoped strategy time semantics; never infer history from wall-clock date."""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, date, time
from functools import wraps
from typing import Literal
from zoneinfo import ZoneInfo

from backend.application.market_calendar import market_window, CalendarUnavailable

Mode = Literal['current', 'historical']
SHANGHAI = ZoneInfo('Asia/Shanghai')


@dataclass(frozen=True)
class Decision:
    mode: Mode
    now: datetime
    requested: str | None

    def cutoff(self, day: str) -> datetime:
        if self.mode == 'current':
            return self.now
        return min(self.now, datetime.combine(date.fromisoformat(day), time.max, SHANGHAI))


active_decision: ContextVar[Decision | None] = ContextVar('strategy_decision', default=None)


def scoped(method):
    """Share one clock snapshot/mode across nested service and data calls.

    Existing Python callers without a mode can still request an older session;
    HTTP callers select the mode explicitly. A same-session replay requires
    mode='historical', even when the session is also the current market date.
    """
    @wraps(method)
    def wrapper(self, *args, mode: Mode | None = None, **kwargs):
        if active_decision.get() is not None and mode is None:
            return method(self, *args, **kwargs)
        now = self.clock()
        requested = kwargs.get('as_of')
        if mode is None:
            try:
                latest = market_window(now).latest_completed.isoformat()
            except CalendarUnavailable:
                latest = None
            mode = 'historical' if requested is not None and requested != latest else 'current'
        if mode not in ('current', 'historical'):
            raise ValueError('unknown strategy mode')
        token = active_decision.set(Decision(mode, now, requested))
        try:
            return method(self, *args, **kwargs)
        finally:
            active_decision.reset(token)
    return wrapper
