"""Valuation inputs and read-only projections over the trading ledger."""
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
import sqlite3
import json
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from backend.application.trading.service import (
    Input, TradeInput, TradeService, TradeError, Conflict, MAX_FEN, MAX_QTY, SCALE,
    audit, bump, cash_balance, dump, money, one, prior, remember, rows, uid, utc, require_account,
)
from backend.infrastructure.database import Database
from backend.application.market_calendar import (
    CalendarUnavailable, MarketWindow, freshness_reference, market_window, quote_time,
)

SHANGHAI = ZoneInfo('Asia/Shanghai')


def day_of(at: datetime) -> date:
    return at.astimezone(SHANGHAI).date()


def require_time(value: datetime) -> datetime:
    if value.utcoffset() is None or not 1900 <= value.year <= 9998:
        raise ValueError('timestamp must have timezone and supported year')
    return value


class OpeningInput(Input):
    request_id: str = Field(min_length=1,max_length=128)
    account_id: str
    instrument_id: str
    quantity: int = Field(strict=True,gt=0,le=MAX_QTY)
    cost_fen: int = Field(strict=True,ge=0,le=MAX_FEN)
    fees_fen: int = Field(strict=True,ge=0,le=MAX_FEN)
    occurred_at: datetime
    note: str = Field(min_length=1,max_length=2000)

    @field_validator('occurred_at',mode='before')
    @classmethod
    def timestamp(cls,value):
        return TradeInput.timestamp_input(value)

    @model_validator(mode='after')
    def validate_time(self):
        require_time(self.occurred_at)
        return self


class CompanyInput(Input):
    request_id: str = Field(min_length=1,max_length=128)
    account_id: str
    instrument_id: str
    kind: Literal['dividend','split','merge','rights','other']
    occurred_at: datetime
    amount_fen: int | None = Field(default=None,strict=True,gt=0,le=MAX_FEN)
    note: str = Field(min_length=1,max_length=2000)

    @field_validator('occurred_at',mode='before')
    @classmethod
    def timestamp(cls,value):
        return TradeInput.timestamp_input(value)

    @model_validator(mode='after')
    def validate_event(self):
        require_time(self.occurred_at)
        if (self.kind=='dividend') != (self.amount_fen is not None):
            raise ValueError('only confirmed cash dividends may carry amount')
        return self


class QuoteInput(Input):
    request_id: str = Field(min_length=1,max_length=128)
    instrument_id: str
    price: Decimal = Field(gt=0,le=1000000,decimal_places=4,max_digits=11)
    source: str = Field(min_length=1,max_length=128)
    observed_at: datetime
    trading_day: date
    kind: Literal['close','intraday']
    price_basis: Literal['unadjusted_security'] = 'unadjusted_security'
    quality: Literal['valid','stale','missing','conflict','derived_unverified']

    @field_validator('observed_at',mode='before')
    @classmethod
    def timestamp(cls,value):
        return TradeInput.timestamp_input(value)

    @field_validator('trading_day',mode='before')
    @classmethod
    def date_input(cls,value):
        return TradeInput.date_input(value)

    @field_validator('price',mode='before')
    @classmethod
    def exact(cls,value):
        return TradeInput.exact_decimal(value)

    @model_validator(mode='after')
    def validate_quote(self):
        require_time(self.observed_at)
        local=self.observed_at.astimezone(SHANGHAI)
        if self.trading_day>local.date() or (self.kind=='intraday' and self.trading_day!=local.date()):
            raise ValueError('quote day conflicts with observed time')
        if self.kind=='close' and self.trading_day==local.date() and local.time()<time(15):
            raise ValueError('confirmed close cannot precede market close')
        return self


class QuoteResolutionInput(Input):
    request_id: str = Field(min_length=1, max_length=128)
    selected_quote_id: str = Field(min_length=1, max_length=128)
    expected_quote_revision: int = Field(strict=True, ge=0)
    reason: str = Field(min_length=1, max_length=2000)


class PortfolioService:
    def __init__(self,db: Database,clock: Callable[[],datetime]=lambda:datetime.now(UTC)) -> None:
        self.db=db
        self.clock=clock

    def _actual(self,at: datetime) -> None:
        TradeService(self.db,self.clock).require_actual(require_time(at))

    def opening(self,data: OpeningInput) -> dict:
        data=OpeningInput.model_validate(data)
        self._actual(data.occurred_at)
        value=data.model_dump(mode='json',exclude={'request_id'})
        value['occurred_at']=utc(data.occurred_at)
        payload=dump({'opening_position':value})
        with self.db.transaction() as conn:
            old=prior(conn,data.request_id,payload)
            if old is not None: return old
            require_account(conn,data.account_id,securities=True,active=True,at=value['occurred_at'])
            one(conn,'SELECT id FROM instruments WHERE id=?',(data.instrument_id,))
            scope=(data.account_id,data.instrument_id)
            if conn.execute('SELECT 1 FROM opening_positions WHERE account_id=? AND instrument_id=?',scope).fetchone() or conn.execute("SELECT 1 FROM trade_executions WHERE account_id=? AND instrument_id=? AND status='effective'",scope).fetchone():
                raise Conflict('opening position must precede effective trades and be unique')
            if conn.execute('SELECT 1 FROM company_events WHERE account_id=? AND instrument_id=?',scope).fetchone():
                raise Conflict('opening position cannot follow company action facts')
            id_=uid(); lot=uid()
            conn.execute('INSERT INTO opening_positions VALUES (?,?,?,?,?,?,?,?)',(id_,*scope,data.quantity,data.cost_fen,data.fees_fen,value['occurred_at'],data.note))
            conn.execute("INSERT INTO trade_lots VALUES (?,NULL,NULL,'ordinary',NULL,NULL,?,?,?,?,'open',?)",(lot,data.quantity,data.quantity,data.cost_fen,data.fees_fen,id_))
            audit(conn,'opening_position',id_,'confirmed',data.request_id,value)
            bump(conn,day_of(data.occurred_at).isoformat())
            result={'id':id_,'lot_id':lot,**value}
            remember(conn,data.request_id,payload,result)
            return result

    def company(self,data: CompanyInput) -> dict:
        data=CompanyInput.model_validate(data)
        self._actual(data.occurred_at)
        value=data.model_dump(mode='json',exclude={'request_id'}); value['occurred_at']=utc(data.occurred_at)
        payload=dump({'company_event':value})
        with self.db.transaction() as conn:
            old=prior(conn,data.request_id,payload)
            if old is not None: return old
            require_account(conn,data.account_id,securities=True,active=True,at=value['occurred_at'])
            one(conn,'SELECT id FROM instruments WHERE id=?',(data.instrument_id,))
            if data.kind=='dividend' and conn.execute("SELECT 1 FROM cash_movements WHERE account_id=? AND kind='opening' AND occurred_at>=?",(data.account_id,value['occurred_at'])).fetchone():
                raise Conflict('dividend conflicts with opening cash boundary')
            id_=uid()
            movement_id=None
            if data.kind=='dividend':
                movement_id=uid()
                conn.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?,?,?)',(movement_id,data.account_id,None,None,data.amount_fen,'dividend',value['occurred_at'],None,data.request_id,data.note))
            conn.execute('INSERT INTO company_events VALUES (?,?,?,?,?,?,?)',(id_,data.account_id,data.instrument_id,data.kind,value['occurred_at'],movement_id,data.note))
            audit(conn,'company_event',id_,'confirmed' if data.kind=='dividend' else 'unresolved',data.request_id,value)
            bump(conn,day_of(data.occurred_at).isoformat())
            result={'id':id_,**value}
            remember(conn,data.request_id,payload,result)
            return result

    def quote(self, data: QuoteInput) -> dict:
        data = QuoteInput.model_validate(data)
        acquired = self.clock()
        require_time(acquired)
        if data.observed_at > acquired:
            raise TradeError('future quote observation is not supported')
        try:
            effective = utc(quote_time(data.trading_day, data.observed_at, data.kind))
        except CalendarUnavailable as exc:
            raise TradeError(str(exc)) from exc
        # Keep the original 0003 idempotency payload compatible; the only
        # permitted basis is already implicit in that contract.
        value = data.model_dump(mode='json', exclude={'request_id','price_basis'})
        value['observed_at'] = utc(data.observed_at)
        value['price'] = int(data.price * SCALE)
        payload = dump({'quote': value})
        with self.db.transaction() as conn:
            old = prior(conn, data.request_id, payload)
            if old is not None:
                return old
            one(conn, 'SELECT id FROM instruments WHERE id=?', (data.instrument_id,))
            # Effective price evidence is the identity, not the HTTP fetch time.
            # Legacy rows with unknown acquisition time cannot prove availability.
            existing = rows(conn, """SELECT * FROM quotes WHERE instrument_id=? AND
                trading_day=? AND kind=? AND effective_at=? AND price=? AND source=?
                AND quality=? AND acquired_at IS NOT NULL ORDER BY quote_revision LIMIT 1""",
                (data.instrument_id, value['trading_day'], data.kind, effective,
                 value['price'], data.source, data.quality))
            if existing:
                result = existing[0]
            else:
                conn.execute('UPDATE quote_state SET quote_revision=quote_revision+1 WHERE id=1')
                rev = one(conn, 'SELECT quote_revision FROM quote_state')['quote_revision']
                result = {'id': uid(), **value, 'quote_revision': rev,
                          'effective_at': effective, 'acquired_at': utc(acquired),
                          'price_basis':data.price_basis}
                conn.execute("""INSERT INTO quotes (id,instrument_id,price,source,observed_at,
                    trading_day,kind,quality,quote_revision,effective_at,acquired_at,price_basis)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(result[k] for k in ('id','instrument_id','price','source','observed_at',
                        'trading_day','kind','quality','quote_revision','effective_at','acquired_at','price_basis')))
            remember(conn, data.request_id, payload, result)
            return result

    def resolve_close(self, data: QuoteResolutionInput) -> dict:
        """Confirm an existing close against the exact evidence version reviewed."""
        data = QuoteResolutionInput.model_validate(data)
        resolved = utc(require_time(self.clock()))
        payload = dump({'quote_resolution': data.model_dump(exclude={'request_id'})})
        with self.db.transaction() as conn:
            old = prior(conn, data.request_id, payload)
            if old is not None:
                return old
            revision = self._revision(conn, None)
            if revision != data.expected_quote_revision:
                raise Conflict('quote evidence changed; review the current revision')
            selected = one(conn, 'SELECT * FROM quotes WHERE id=?', (data.selected_quote_id,))
            if (selected['kind'] != 'close' or selected['quality'] != 'valid'
                    or selected['effective_at'] is None or selected['acquired_at'] is None
                    or selected['effective_at'] > resolved or selected['acquired_at'] > resolved):
                raise TradeError('resolution requires acquired valid completed close evidence')
            instrument, day = selected['instrument_id'], selected['trading_day']
            _, quality = self._quote(conn, instrument, day, resolved, close=True, revision=revision)
            if quality != 'conflict':
                raise Conflict('no unresolved closing price conflict')
            evidence = rows(conn, """SELECT id,acquired_at FROM quotes WHERE instrument_id=?
                AND trading_day=? AND kind='close' AND effective_at<=? AND quote_revision<=?
                ORDER BY quote_revision""", (instrument, day, resolved, revision))
            if any(q['acquired_at'] is not None and q['acquired_at'] > resolved for q in evidence):
                raise Conflict('resolution clock precedes reviewed evidence')
            previous = conn.execute('SELECT resolved_at FROM quote_resolutions WHERE instrument_id=? AND trading_day=? ORDER BY quote_revision DESC LIMIT 1', (instrument, day)).fetchone()
            if previous and previous[0] > resolved:
                raise Conflict('resolution clock precedes previous confirmation')
            reviewed = [q['id'] for q in evidence]
            conn.execute('UPDATE quote_state SET quote_revision=quote_revision+1 WHERE id=1')
            result = {'id': uid(), 'instrument_id': instrument, 'trading_day': day,
                      'selected_quote_id': selected['id'], 'reviewed_quote_ids': reviewed,
                      'reason': data.reason, 'resolved_at': resolved, 'quote_revision': revision+1}
            conn.execute('INSERT INTO quote_resolutions VALUES (?,?,?,?,?,?,?,?)',
                         (result['id'], instrument, day, selected['id'], dump(reviewed),
                          data.reason, resolved, revision+1))
            remember(conn, data.request_id, payload, result)
            return result

    @staticmethod
    def _quote(conn: sqlite3.Connection, instrument: str, day: str, cutoff: str,
               *, close: bool = False, revision: int, available_at: str | None = None,
               freshness_at: datetime | None = None) -> tuple[dict | None, str]:
        sql = """SELECT * FROM quotes WHERE instrument_id=? AND trading_day=?
            AND effective_at<=? AND quote_revision<=? AND price_basis='unadjusted_security'"""
        args = (instrument, day, cutoff, revision)
        if available_at is not None:
            sql += ' AND acquired_at<=?'
            args += (available_at,)
        found = rows(conn, sql + ' ORDER BY effective_at DESC,quote_revision DESC', args)
        closes = [q for q in found if q['kind'] == 'close']
        # A confirmation resolves only evidence known at its own revision.
        # Later conflicting evidence reopens the conflict; old versions remain
        # reproducible, and strategy-style availability cutoffs cannot see it early.
        resolution_sql = """SELECT * FROM quote_resolutions WHERE instrument_id=?
            AND trading_day=? AND quote_revision<=?"""
        resolution_args = (instrument, day, revision)
        if available_at is not None:
            resolution_sql += ' AND resolved_at<=?'
            resolution_args += (available_at,)
        resolutions = rows(conn, resolution_sql + ' ORDER BY quote_revision DESC LIMIT 1', resolution_args)
        if resolutions:
            resolution = resolutions[0]
            selected = next((q for q in closes if q['id'] == resolution['selected_quote_id']), None)
            if selected is not None:
                reviewed = set(json.loads(resolution['reviewed_quote_ids']))
                closes = [q for q in closes if q['id'] not in reviewed or q['id'] == selected['id']]
        valid = [q for q in closes if q['quality'] == 'valid']
        if any(q['quality'] == 'conflict' for q in closes) or len({q['price'] for q in valid}) > 1:
            return (valid or closes)[0], 'conflict'
        if valid:
            return valid[0], 'valid'
        if close:
            return (closes[0], closes[0]['quality']) if closes else (None, 'missing')
        intraday = [q for q in found if q['kind'] == 'intraday']
        if not intraday:
            return (closes[0], closes[0]['quality']) if closes else (None, 'missing')
        q = intraday[0]
        if q['quality'] != 'valid':
            return q, q['quality']
        # Simultaneous valid observations with inconsistent prices are not silently chosen.
        if any(other['effective_at'] == q['effective_at'] and other['quality'] == 'valid'
               and other['price'] != q['price'] for other in intraday[1:]):
            return q, 'conflict'
        now = freshness_at or datetime.fromisoformat(cutoff)
        if now - datetime.fromisoformat(q['effective_at']) > timedelta(minutes=30):
            return q, 'stale'
        return q, 'valid'

    @classmethod
    def _current_quote(cls, conn: sqlite3.Connection, instrument: str, cutoff: str,
                       window: MarketWindow | None, revision: int) -> tuple[dict | None, str]:
        if window is None:
            return None, 'calendar_unavailable'
        day = window.valuation_day.isoformat()
        at = datetime.fromisoformat(cutoff)
        require_close = window.phase not in ('trading', 'break')
        q, quality = cls._quote(conn, instrument, day, cutoff, close=require_close,
                                revision=revision, freshness_at=freshness_reference(at, window))
        if quality == 'valid' or quality == 'conflict':
            return q, quality
        if window.phase == 'after_close' and q is None:
            q, intraday_quality = cls._quote(conn, instrument, day, cutoff, revision=revision)
            if intraday_quality == 'conflict':
                return q, 'conflict'
        # Show the last trustworthy observation as reference only. A missing
        # required close or stale live quote never yields trusted equity/PnL.
        fallback = rows(conn, """SELECT DISTINCT trading_day FROM quotes WHERE instrument_id=?
            AND kind='close' AND effective_at<=? AND quote_revision<=? AND trading_day<=?
            ORDER BY trading_day DESC LIMIT 30""", (instrument, cutoff, revision, day))
        for row in fallback:
            previous, state = cls._quote(conn, instrument, row['trading_day'], cutoff,
                                         close=True, revision=revision)
            if state == 'valid':
                if q is None or q['quality'] != 'valid' or q['effective_at'] < previous['effective_at']:
                    q = previous
                return q, quality if quality != 'missing' else 'stale'
        return (q if q is not None and q['quality'] == 'valid' else None), ('stale' if q is not None and quality == 'missing' else quality)

    @staticmethod
    def _lots(conn: sqlite3.Connection,account: str,cutoff: str) -> list[dict]:
        return rows(conn,"""SELECT l.id AS lot_id,l.open_execution_id,l.opening_id,l.lot_role,l.strategy_id,l.signal_date,l.frozen_atr,
            COALESCE(t.instrument_id,o.instrument_id) AS instrument_id,COALESCE(t.executed_at,o.occurred_at) AS opened_at,
            l.opened_qty-COALESCE(SUM(CASE WHEN s.id IS NOT NULL THEN a.quantity END),0) AS quantity,
            COALESCE(t.amount_fen,o.cost_fen)-COALESCE(SUM(CASE WHEN s.id IS NOT NULL THEN a.cost_fen END),0) AS cost_fen,
            COALESCE(t.fees_fen,o.fees_fen)-COALESCE(SUM(CASE WHEN s.id IS NOT NULL THEN a.buy_fees_fen END),0) AS fees_fen
            FROM trade_lots l LEFT JOIN trade_executions t ON t.id=l.open_execution_id LEFT JOIN opening_positions o ON o.id=l.opening_id
            LEFT JOIN lot_allocations a ON a.lot_id=l.id LEFT JOIN trade_executions s ON s.id=a.sell_execution_id AND s.status='effective' AND s.executed_at<=?
            WHERE COALESCE(t.account_id,o.account_id)=? AND COALESCE(t.executed_at,o.occurred_at)<=? AND (t.status='effective' OR o.id IS NOT NULL)
            GROUP BY l.id ORDER BY opened_at,l.rowid""",(cutoff,account,cutoff))

    def _cutoff(self,as_of: datetime) -> str:
        if as_of.utcoffset() is None or not 1900<=as_of.year<=9998: raise TradeError('timestamp must have timezone and supported year')
        if as_of>self.clock(): raise TradeError('future valuation cutoff is not supported')
        return utc(as_of)

    @staticmethod
    def _revision(conn: sqlite3.Connection, requested: int | None) -> int:
        latest = one(conn, 'SELECT quote_revision FROM quote_state')['quote_revision']
        if requested is not None and not 0 <= requested <= latest:
            raise TradeError('quote revision is outside available history')
        return latest if requested is None else requested

    def quotes(self, instrument: str, as_of: datetime, quote_revision: int | None = None,
               available_at: datetime | None = None) -> dict:
        cutoff = self._cutoff(as_of)
        available = self._cutoff(available_at) if available_at is not None else None
        if available is not None and available > cutoff:
            raise TradeError('availability cutoff must not follow evaluation time')
        with self.db.read() as conn:
            one(conn, 'SELECT id FROM instruments WHERE id=?', (instrument,))
            revision = self._revision(conn, quote_revision)
            sql = 'SELECT * FROM quotes WHERE instrument_id=? AND effective_at<=? AND quote_revision<=?'
            args = (instrument, cutoff, revision)
            if available is not None:
                sql += ' AND acquired_at<=?'
                args += (available,)
            resolution_sql = """SELECT r.* FROM quote_resolutions r JOIN quotes q
                ON q.id=r.selected_quote_id WHERE r.instrument_id=? AND q.effective_at<=?
                AND r.quote_revision<=?"""
            resolution_args = (instrument, cutoff, revision)
            if available is not None:
                resolution_sql += ' AND r.resolved_at<=?'
                resolution_args += (available,)
            resolutions = rows(conn, resolution_sql+' ORDER BY r.quote_revision DESC LIMIT 500', resolution_args)
            for resolution in resolutions:
                resolution['reviewed_quote_ids'] = json.loads(resolution['reviewed_quote_ids'])
            return {'quote_revision':revision, 'as_of':cutoff, 'available_at':available,
                    'items': rows(conn, sql+' ORDER BY effective_at DESC,quote_revision DESC LIMIT 500',args),
                    'resolutions': resolutions}

    def current_price(self, instrument: str, as_of: datetime) -> dict:
        cutoff = self._cutoff(as_of)
        try:
            window = market_window(as_of)
        except CalendarUnavailable:
            window = None
        with self.db.read() as conn:
            one(conn, 'SELECT id FROM instruments WHERE id=?', (instrument,))
            revision = self._revision(conn, None)
            q, quality = self._current_quote(conn, instrument, cutoff, window, revision)
            return {'instrument_id':instrument, 'as_of':cutoff, 'quote_revision':revision,
                    'quote':q, 'quality':quality, 'market_phase':window.phase if window else 'unknown'}

    def strategy_evidence(self, account: str, as_of: datetime) -> dict:
        """Expose recorded batch facts, not invented strategy decisions."""
        cutoff = self._cutoff(as_of)
        with self.db.read() as conn:
            require_account(conn,account,securities=True,at=cutoff)
            versions = {**one(conn,'SELECT ledger_revision FROM ledger_state'),
                        **one(conn,'SELECT quote_revision FROM quote_state')}
            items = [dict(lot, evidence_status='unverified',
                          reason_codes=['verified_legacy_rule_source_missing'] +
                          (['frozen_atr_or_signal_date_missing'] if lot['frozen_atr'] is None or lot['signal_date'] is None else []))
                     for lot in self._lots(conn, account, cutoff)
                     if lot['lot_role'] in ('main','sub')]
            return {**versions,'account_id':account,'as_of':cutoff,'status':'unavailable',
                    'reason_codes':['verified_legacy_rule_source_missing'],
                    'items':items}

    def valuation(self, account: str, as_of: datetime, previous_trading_day: date | None = None,
                  quote_revision: int | None = None) -> dict:
        self._cutoff(as_of)
        try:
            window = market_window(as_of)
        except CalendarUnavailable:
            window = None
        if window is not None and previous_trading_day is not None and previous_trading_day != window.previous:
            raise TradeError('previous trading day conflicts with exchange calendar')
        with self.db.read() as conn:
            require_account(conn,account,securities=True,at=utc(as_of))
            revision = self._revision(conn, quote_revision)
            result = self._valuation(conn, account, as_of, window, revision)
            if window is None:
                result.update(latest_completed_trading_day=None, latest_trading_day_pnl_fen=None,
                              latest_trading_day_quality='calendar_unavailable')
            elif window.latest_completed == window.day:
                result.update(latest_completed_trading_day=window.day.isoformat(),
                              latest_trading_day_pnl_fen=result['daily_pnl_fen'],
                              latest_trading_day_quality=result['daily_pnl_quality'])
            else:
                # Same DB read snapshot, but use the actual preceding session's day-end.
                end = datetime.combine(window.latest_completed,time.max,SHANGHAI)
                historical = self._valuation(conn, account, end, market_window(end), revision)
                result.update(latest_completed_trading_day=window.latest_completed.isoformat(),
                              latest_trading_day_pnl_fen=historical['daily_pnl_fen'],
                              latest_trading_day_quality=historical['daily_pnl_quality'])
            return result

    def _valuation(self, conn: sqlite3.Connection, account: str, as_of: datetime,
                   window: MarketWindow | None, revision: int) -> dict:
        cutoff = utc(as_of)
        day = day_of(as_of)
        start = utc(datetime.combine(day,time.min,SHANGHAI))
        before = utc(datetime.fromisoformat(start)-timedelta(microseconds=1))
        previous_trading_day = window.previous if window else None
        rev = {**one(conn,'SELECT ledger_revision,earliest_affected_day FROM ledger_state'),
               'quote_revision':revision}
        lots=self._lots(conn,account,cutoff)
        prev_lots=self._lots(conn,account,before)
        pending=rows(conn,"SELECT DISTINCT instrument_id FROM trade_executions WHERE account_id=? AND status='pending' AND executed_at<=?",(account,cutoff))
        actions=rows(conn,"SELECT e.*,m.amount_fen FROM company_events e LEFT JOIN effective_cash_movements m ON m.id=e.cash_movement_id WHERE e.account_id=? AND e.occurred_at<=? AND (e.kind!='dividend' OR m.id IS NOT NULL)",(account,cutoff))
        blocked={r['instrument_id'] for r in pending}|{r['instrument_id'] for r in actions if r['kind']!='dividend'}
        reasons=[] if window else ['calendar_unavailable']
        later_openings=rows(conn,'SELECT instrument_id FROM opening_positions WHERE account_id=? AND occurred_at>?',(account,cutoff))
        if later_openings:
            blocked.update(r['instrument_id'] for r in later_openings)
            reasons.append('before_confirmed_position_baseline')
        if pending: reasons.append('pending_trade')
        if any(r['kind']!='dividend' for r in actions): reasons.append('unresolved_company_action')
        trades=rows(conn,"SELECT * FROM trade_executions WHERE account_id=? AND status='effective' AND executed_at<=? ORDER BY executed_at,rowid",(account,cutoff))
        realized_rows=rows(conn,"""SELECT t.instrument_id,SUM(t.amount_fen-t.fees_fen-(SELECT COALESCE(SUM(a.cost_fen+a.buy_fees_fen),0) FROM lot_allocations a WHERE a.sell_execution_id=t.id)) AS realized_fen
            FROM trade_executions t WHERE t.account_id=? AND t.status='effective' AND t.side='sell' AND t.executed_at<=? GROUP BY t.instrument_id""",(account,cutoff))
        realized={r['instrument_id']:r['realized_fen'] for r in realized_rows}
        ids={r['instrument_id'] for r in lots+prev_lots+trades+actions+pending+later_openings}
        positions=[]; daily_known=0; daily_ok=not blocked and window is not None; market_known=0; market_ok=not blocked and window is not None; unrealized_known=0
        prev_market=0; prev_ok=window is not None
        opening_today_any=False
        for instrument in sorted(ids):
            master=one(conn,'SELECT code,market FROM instruments WHERE id=?',(instrument,))
            current=[r for r in lots if r['instrument_id']==instrument and r['quantity']>0]
            previous=[r for r in prev_lots if r['instrument_id']==instrument and r['quantity']>0]
            quantity=sum(r['quantity'] for r in current); prev_qty=sum(r['quantity'] for r in previous)
            cost=sum(r['cost_fen'] for r in current); fees=sum(r['fees_fen'] for r in current)
            q,quality=self._current_quote(conn,instrument,cutoff,window,revision) if quantity else (None,'valid')
            mv=money(quantity,q['price']) if quantity and quality=='valid' else 0 if not quantity else None
            if instrument in blocked:
                mv=None
            if mv is None:
                market_ok=False
                if instrument not in blocked: reasons.append(f'{instrument}:current_quote_{quality}')
            else: market_known+=mv; unrealized_known+=mv-cost-fees
            old_value=0
            if prev_qty:
                old,old_quality=self._quote(conn,instrument,previous_trading_day.isoformat(),before,close=True,revision=revision) if previous_trading_day else (None,'missing')
                old_value=money(prev_qty,old['price']) if old_quality=='valid' else None
                if previous_trading_day and any(previous_trading_day.isoformat()<r['trading_day']<day.isoformat() for r in trades if r['instrument_id']==instrument):
                    old_value=None
                if previous_trading_day and any(r['opening_id'] and day_of(datetime.fromisoformat(r['opened_at']))>previous_trading_day for r in previous):
                    old_value=None
                if old_value is None: reasons.append(f'{instrument}:previous_close_unavailable'); prev_ok=False
                else: prev_market+=old_value
            today=[r for r in trades if r['instrument_id']==instrument and r['executed_at']>=start]
            dividend=sum(r['amount_fen'] for r in actions if r['instrument_id']==instrument and r['kind']=='dividend' and r['occurred_at']>=start)
            cash_trades=sum((r['amount_fen'] if r['side']=='sell' else -r['amount_fen'])-r['fees_fen'] for r in today)
            opening_today=any(r['opening_id'] and r['opened_at']>=start for r in lots if r['instrument_id']==instrument)
            if opening_today:
                opening_today_any=True
                reasons.append(f'{instrument}:opening_position_during_day')
            invalid_session_trade = bool(today and window and not window.is_session)
            if invalid_session_trade:
                reasons.append(f'{instrument}:trade_on_non_session_day')
            dp=None if mv is None or old_value is None or instrument in blocked or opening_today or invalid_session_trade else mv-old_value+cash_trades+dividend
            if dp is None: daily_ok=False
            else: daily_known+=dp
            positions.append({'instrument_id':instrument,**master,'quantity':quantity,'lots':current,'remaining_cost_fen':cost,'remaining_fees_fen':fees,'holding_cost_fen':cost+fees,'price':q['price'] if q else None,'quote':q,'market_value_fen':mv,'realized_fen':None if instrument in blocked else realized.get(instrument,0),'unrealized_fen':None if mv is None or instrument in blocked else mv-cost-fees,'daily_pnl_fen':dp,'quality':'derived_unverified' if instrument in blocked else quality})
        balance,cash_quality,cash_reasons=cash_balance(conn,account,cutoff); reasons+=cash_reasons
        equity=balance+market_known if balance is not None and cash_quality=='valid' and market_ok else None
        previous_cash,previous_cash_quality,_=cash_balance(conn,account,before)
        flows=rows(conn,"SELECT kind,amount_fen FROM effective_cash_movements WHERE account_id=? AND occurred_at>=? AND occurred_at<=? AND kind IN ('deposit','withdrawal','adjustment','opening')",(account,start,cutoff))
        external=sum(r['amount_fen'] for r in flows if r['kind'] in ('deposit','withdrawal'))
        adjustments=sum(r['amount_fen'] for r in flows if r['kind']=='adjustment')
        equity_pnl=equity-previous_cash-prev_market-external-adjustments if equity is not None and previous_cash is not None and previous_cash_quality=='valid' and prev_ok and not opening_today_any and not any(r['kind']=='opening' for r in flows) else None
        daily=daily_known if daily_ok and balance is not None and 'cash_fact_conflicts_with_baseline' not in cash_reasons else None
        if equity_pnl is None:
            reasons.append('equity_reconciliation_unavailable')
        if daily is not None and equity_pnl is not None and daily!=equity_pnl:
            reasons.append('equity_reconciliation_difference'); daily=None
        openings=rows(conn,"SELECT occurred_at,amount_fen FROM effective_cash_movements WHERE account_id=? AND kind='opening' AND occurred_at<=?",(account,cutoff))
        cumulative=None; coverage=None
        if len(openings)==1:
            base=openings[0]; coverage=base['occurred_at']
            if equity is not None and not any(r['opening_id'] for r in lots) and not any(r['executed_at']<=coverage for r in trades):
                total_flows=conn.execute("SELECT COALESCE(SUM(amount_fen),0) FROM effective_cash_movements WHERE account_id=? AND occurred_at>? AND occurred_at<=? AND kind IN ('deposit','withdrawal','adjustment')",(account,coverage,cutoff)).fetchone()[0]
                cumulative=equity-base['amount_fen']-total_flows
            else: reasons.append('cumulative_baseline_incomplete')
        if not openings: reasons.append('cumulative_baseline_missing')
        return {**rev,'account_id':account,'as_of':cutoff,'trading_day':day.isoformat(),'market_phase':window.phase if window else 'unknown','valuation_trading_day':window.valuation_day.isoformat() if window else None,'is_trading_day':window.is_session if window else None,'previous_trading_day':previous_trading_day.isoformat() if previous_trading_day else None,'source':'sqlite_ledger_and_explicit_quotes','quality':'valid' if equity is not None and daily is not None and cumulative is not None and equity_pnl is not None else 'derived_unverified','reasons':sorted(set(reasons)),'cash_fen':balance,'cash_quality':cash_quality,'known_market_value_fen':market_known,'market_value_fen':market_known if market_ok else None,'equity_fen':equity,'daily_pnl_fen':daily,'daily_pnl_quality':'valid' if daily is not None else 'unavailable','equity_pnl_fen':equity_pnl,'reconciliation_difference_fen':daily_known-equity_pnl if daily_ok and equity_pnl is not None else None,'external_flow_fen':external,'adjustments_fen':adjustments,'dividends_fen':sum(r['amount_fen'] for r in actions if r['kind']=='dividend'),'daily_dividends_fen':sum(r['amount_fen'] for r in actions if r['kind']=='dividend' and r['occurred_at']>=start),'realized_fen':sum(realized.values()) if not blocked else None,'unrealized_fen':unrealized_known if market_ok else None,'cumulative_pnl_fen':cumulative,'cumulative_from':coverage,'cumulative_cash_baseline_fen':openings[0]['amount_fen'] if len(openings)==1 else None,'positions':[r for r in positions if r['quantity']>0],'instrument_results':positions}
