"""One serialized market refresh service; database transactions never span HTTP."""
import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
import logging
from threading import Event, Lock
from typing import Protocol

from backend.application.market_calendar import (
    CalendarUnavailable, SHANGHAI, calendar_coverage, market_window, session_bounds,
)
from backend.application.portfolio import PortfolioService, QuoteInput
from backend.application.trading.service import TradeError, dump, one, rows, utc
from backend.infrastructure.database import Database, DatabaseError
from backend.infrastructure.market.eastmoney import MarketDataError
from backend.infrastructure.market.tencent import CLOSE_DELAY, TencentQuoteProvider

logger = logging.getLogger(__name__)


class QuoteProvider(Protocol):
    def spot(self, instrument: dict, now: datetime) -> dict: ...
    def closes(self, instrument: dict, days: set[date], now: datetime) -> list[dict]: ...


class MarketService:
    def __init__(self, db: Database, provider: QuoteProvider | None = None,
                 clock: Callable[[], datetime] = lambda:datetime.now(UTC)) -> None:
        self.db = db
        self.provider = provider if provider is not None else TencentQuoteProvider()
        self.clock = clock
        self.portfolio = PortfolioService(db, clock)
        self._refresh_lock = Lock()
        self._state_lock = Lock()
        self._stop = Event()
        self._last_result: dict | None = None
        self._offset = 0
        self._last_closed_attempt: datetime | None = None

    def status(self, instrument_id: str | None = None) -> dict:
        now = self.clock()
        cutoff = utc(now)
        try:
            window = market_window(now)
        except CalendarUnavailable:
            window = None
        with self.db.read() as conn:
            revision = self.portfolio._revision(conn, None)
            instruments = self._instruments(conn,instrument_id)
            items = []
            for instrument in instruments:
                quote,quality = self.portfolio._current_quote(conn,instrument['id'],cutoff,window,revision)
                items.append({'instrument_id':instrument['id'], 'code':instrument['code'],
                    'market':instrument['market'], 'quote':quote, 'quality':quality,
                    # Evidence provenance is exposed explicitly so the UI and the
                    # reviewer can see which source produced an accounted quote.
                    'source':quote['source'] if quote else None,
                    'price_basis':quote['price_basis'] if quote else None,
                    'kind':quote['kind'] if quote else None,
                    'observed_at':quote['observed_at'] if quote else None,
                    'acquired_at':quote['acquired_at'] if quote else None})
        provider, provider_available = self._provider_state()
        with self._state_lock:
            last_result = self._last_result
        return {'as_of':cutoff, 'quote_revision':revision, 'market_phase':window.phase if window else 'unknown',
                'latest_completed_trading_day':window.latest_completed.isoformat() if window else None,
                'calendar_coverage':calendar_coverage(now),'last_refresh':last_result,
                'provider':provider,'provider_available':provider_available,'items':items}

    def _provider_state(self) -> tuple[str, bool]:
        """Name the configured source; availability is structural, not a probe."""
        provider = self.provider
        name = getattr(provider,'name',None) or type(provider).__name__
        ready = all(callable(getattr(provider,attr,None)) for attr in ('spot','closes'))
        return str(name), ready and bool(getattr(provider,'available',True))

    @staticmethod
    def _instruments(conn, instrument_id: str | None) -> list[dict]:
        if instrument_id is not None:
            return [one(conn,'SELECT * FROM instruments WHERE id=?',(instrument_id,))]
        return rows(conn,'SELECT * FROM instruments ORDER BY id')

    def _save(self, raw: dict) -> dict:
        # The provider observes a stable timestamp for each price version; use
        # a namespaced deterministic request key, not a new key on every poll.
        canonical = {**raw,'price':str(raw['price'].normalize()),
                     'observed_at':utc(raw['observed_at']),'trading_day':raw['trading_day'].isoformat()}
        key = 'market:' + sha256(dump(canonical).encode()).hexdigest()
        return self.portfolio.quote(QuoteInput(request_id=key,**raw))

    def refresh(self, instrument_id: str | None = None, *, automatic: bool = False) -> dict:
        if not self.db.settings.writable or (automatic and self.db.settings.mode != 'server'):
            raise DatabaseError('market refresh requires a verified writable database')
        if not self._refresh_lock.acquire(blocking=False):
            return {'status':'busy','items':[]}
        try:
            now = self.clock()
            try:
                window = market_window(now)
                # The recent completed day and its predecessor are needed by
                # both today's estimate and the last session's PnL.
                last = window.latest_completed
                prior = market_window(datetime.combine(last,time.max,SHANGHAI)).previous
            except CalendarUnavailable:
                result = {'status':'unavailable','as_of':utc(now),'reason':'calendar_unavailable','items':[]}
                with self._state_lock:
                    self._last_result = result
                return result
            target_days = {day for day in (last,prior) if now >= session_bounds(day)[1]+CLOSE_DELAY}
            with self.db.read() as conn:
                all_instruments = self._instruments(conn,instrument_id)
                # Bound one operation; rotate automatically if there are many
                # master records. Manual per-symbol refresh remains available.
                offset = self._offset % len(all_instruments) if all_instruments and automatic else 0
                instruments = (all_instruments[offset:]+all_instruments[:offset])[:20]
                if automatic:
                    self._offset = offset+len(instruments)
                rev = self.portfolio._revision(conn,None)
                targets = []
                for instrument in instruments:
                    days = target_days if not automatic else {day for day in target_days
                        if self.portfolio._quote(conn,instrument['id'],day.isoformat(),utc(now),
                            close=True,revision=rev)[1] in ('missing','stale','derived_unverified')}
                    targets.append((instrument,days))
            # The read transaction has closed. Each network request happens
            # before the small quote-only write transaction.
            items = []
            for instrument, days in targets:
                if self._stop.is_set():
                    break
                errors = []; stored = []
                if days:
                    try:
                        quotes = self.provider.closes(instrument,days,now)
                        returned_days = {q['trading_day'] for q in quotes}
                        if returned_days != days:
                            errors.append('required_close_missing')
                        for q in quotes:
                            self._validate_provider_quote(q,instrument,days,'close')
                        stored.extend(self._save(q)['quote_revision'] for q in quotes)
                    except (MarketDataError,TradeError,ValueError) as exc:
                        errors.append(str(exc) if isinstance(exc,MarketDataError) else 'daily_evidence_rejected')
                if window.phase == 'trading' and not self._stop.is_set():
                    try:
                        quote = self.provider.spot(instrument,now)
                        self._validate_provider_quote(quote,instrument,{window.day},'intraday')
                        stored.append(self._save(quote)['quote_revision'])
                    except (MarketDataError,TradeError,ValueError) as exc:
                        errors.append(str(exc) if isinstance(exc,MarketDataError) else 'intraday_evidence_rejected')
                items.append({'instrument_id':instrument['id'],'revisions':stored,'errors':errors})
            status = 'partial' if any(r['errors'] for r in items) and any(r['revisions'] for r in items) else 'unavailable' if any(r['errors'] for r in items) else 'ok' if any(r['revisions'] for r in items) else 'no_work'
            if len(instruments)<len(all_instruments) or self._stop.is_set():
                status = 'partial'
            result = {'status':status,'as_of':utc(now),'items':items,'instrument_count':len(all_instruments),
                      'processed_count':len(items)}
            with self._state_lock:
                self._last_result = result
            return result
        finally:
            self._refresh_lock.release()

    @staticmethod
    def _validate_provider_quote(raw: dict, instrument: dict, days: set[date], kind: str) -> None:
        if (raw.get('instrument_id') != instrument['id'] or raw.get('trading_day') not in days
                or raw.get('kind') != kind or raw.get('quality') != 'valid'
                or raw.get('price_basis') != 'unadjusted_security'):
            raise MarketDataError('provider evidence does not match requested instrument/session')

    def _due(self, now: datetime) -> bool:
        try:
            window = market_window(now)
        except CalendarUnavailable:
            return False
        if window.phase == 'trading':
            return True
        if window.phase == 'break':
            return False
        previous = self._last_closed_attempt
        if previous is None or previous.astimezone(SHANGHAI).date() != window.day:
            self._last_closed_attempt = now
            return True
        local = now.astimezone(SHANGHAI)
        if window.phase == 'after_close' and time(15,15)<=local.time()<=time(17) and now-previous>=timedelta(minutes=15):
            self._last_closed_attempt = now
            return True
        return False

    async def run(self, stop: asyncio.Event, interval: int) -> None:
        """One lifecycle-owned worker; wait for any bounded in-flight HTTP on shutdown."""
        while not stop.is_set():
            if self._due(self.clock()):
                try:
                    await asyncio.to_thread(self.refresh,automatic=True)
                except Exception:
                    # Explicit error status and log; no success or silent failure.
                    logger.exception('market_refresh_failed')
                    with self._state_lock:
                        self._last_result = {'status':'unavailable','reason':'refresh_failed',
                                             'as_of':utc(self.clock()),'items':[]}
            try:
                await asyncio.wait_for(stop.wait(),timeout=interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
