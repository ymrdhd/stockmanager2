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
    audit, bump, cash_balance, dump, money, one, prior, remember, rows, uid, utc, require_account, verified_trade_signal,
)
from backend.infrastructure.database import Database
from backend.application.market_calendar import (
    CalendarUnavailable, MarketWindow, freshness_reference, market_window, quote_time, session_bounds,
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


class OpeningCorrectionInput(Input):
    """更正要说明依据；原记录不覆盖，只被标记为已被替换。

    ``opening_id`` 由路径给出时允许在请求体里省略；两者同时给出时必须一致。
    """
    request_id: str = Field(min_length=1,max_length=128)
    opening_id: str = Field(default='',max_length=128)
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
    # 拆分/合并的比例：新数量 = 旧数量 × ratio_to // ratio_from。
    ratio_from: int | None = Field(default=None,strict=True,ge=1,le=10**9)
    ratio_to: int | None = Field(default=None,strict=True,ge=1,le=10**9)
    # 除不尽的碎股按真实收到/付出的现金尾差单独记录，绝不并入成本。
    cash_tail_fen: int | None = Field(default=None,strict=True,gt=0,le=MAX_FEN)

    @field_validator('occurred_at',mode='before')
    @classmethod
    def timestamp(cls,value):
        return TradeInput.timestamp_input(value)

    @model_validator(mode='after')
    def validate_event(self):
        require_time(self.occurred_at)
        if (self.kind=='dividend') != (self.amount_fen is not None):
            raise ValueError('only confirmed cash dividends may carry amount')
        scaling = self.kind in ('split','merge')
        if not scaling and (self.ratio_from is not None or self.ratio_to is not None
                            or self.cash_tail_fen is not None):
            raise ValueError('only split or merge may carry ratio or cash tail')
        if (self.ratio_from is None) != (self.ratio_to is None):
            raise ValueError('split or merge requires both ratio_from and ratio_to')
        if scaling and self.ratio_from is not None and self.ratio_to == self.ratio_from:
            raise ValueError('split or merge ratio must change the share count')
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
            # Only a current row blocks a new baseline; replaced or voided rows are
            # history and must never make the scope permanently unwritable.
            if conn.execute("""SELECT 1 FROM opening_positions WHERE account_id=? AND instrument_id=?
                    AND superseded_by IS NULL AND voided_at IS NULL""",scope).fetchone() or conn.execute("SELECT 1 FROM trade_executions WHERE account_id=? AND instrument_id=? AND status='effective'",scope).fetchone():
                raise Conflict('opening position must precede effective trades and be unique')
            if conn.execute("""SELECT 1 FROM company_events WHERE account_id=? AND instrument_id=?
                    AND voided_at IS NULL AND occurred_at<=?""",(*scope,value['occurred_at'])).fetchone():
                raise Conflict('opening position cannot follow company action facts')
            id_=uid(); lot=uid()
            conn.execute("""INSERT INTO opening_positions
                (id,account_id,instrument_id,quantity,cost_fen,fees_fen,occurred_at,note,superseded_by,voided_at)
                VALUES (?,?,?,?,?,?,?,?,NULL,NULL)""",
                (id_,*scope,data.quantity,data.cost_fen,data.fees_fen,value['occurred_at'],data.note))
            conn.execute("INSERT INTO trade_lots VALUES (?,NULL,NULL,'ordinary',NULL,NULL,?,?,?,?,'open',?)",(lot,data.quantity,data.quantity,data.cost_fen,data.fees_fen,id_))
            audit(conn,'opening_position',id_,'confirmed',data.request_id,value)
            bump(conn,day_of(data.occurred_at).isoformat())
            result={'id':id_,'lot_id':lot,**value}
            remember(conn,data.request_id,payload,result)
            return result

    @staticmethod
    def _allocate(conn: sqlite3.Connection,v: dict) -> list[tuple[dict,int]]:
        """FIFO (or explicitly specified) allocation over the batches visible now.

        ``TradeService._plan`` cannot be reused here: it refuses every scope that
        carries a non-dividend company event, which is exactly the scope a
        correction has to replay. The allocation maths is identical.
        """
        lots=rows(conn,"""SELECT l.*,COALESCE(t.executed_at,o.occurred_at) AS executed_at
            FROM trade_lots l LEFT JOIN trade_executions t ON t.id=l.open_execution_id
            LEFT JOIN opening_positions o ON o.id=l.opening_id
            WHERE COALESCE(t.account_id,o.account_id)=? AND COALESCE(t.instrument_id,o.instrument_id)=?
            AND (t.status='effective' OR (o.id IS NOT NULL AND o.superseded_by IS NULL AND o.voided_at IS NULL))
            AND l.status='open' AND COALESCE(t.executed_at,o.occurred_at)<=?
            ORDER BY COALESCE(t.executed_at,o.occurred_at),COALESCE(t.rowid,0)""",
            (v['account_id'],v['instrument_id'],v['executed_at']))
        eligible={lot['id']:lot for lot in lots
                  if lot['lot_role']==v['lot_role'] and lot['strategy_id']==v['strategy_id']}
        specified=v['allocations']
        if specified is not None:
            if (sum(specified.values())!=v['quantity']
                    or any(k not in eligible or q>eligible[k]['remaining_qty'] for k,q in specified.items())):
                raise Conflict('historical replay conflict: specified lots no longer fund this sale')
            return [(eligible[k],q) for k,q in specified.items()]
        if v['lot_role']!='ordinary':
            raise Conflict('historical replay conflict: strategy sale lost its specified lots')
        need=plan_need=v['quantity']
        plan=[]
        for lot in eligible.values():
            take=min(need,lot['remaining_qty'])
            if take:
                plan.append((lot,take)); need-=take
        if need: raise Conflict('historical replay conflict: insufficient lots after correction')
        return plan if plan_need else []

    def _replay_scope(self,conn: sqlite3.Connection,account: str,instrument: str,request: str,
                      opening: tuple[str,int,int,int] | None) -> list[dict]:
        """Rebuild every allocation of one scope against the current baseline batch.

        ``opening`` is the batch that must survive as the position baseline (its id,
        quantity, cost and fees); ``None`` means the scope has no baseline any more,
        so any remaining sale must fail instead of silently running on empty lots.
        Batches opened by real buys are replayed from their own recorded payload, so
        nothing here invents a trade, a price or a cash movement.
        """
        facts=rows(conn,"""SELECT * FROM trade_executions WHERE account_id=? AND instrument_id=?
            AND status='effective' ORDER BY executed_at,rowid""",(account,instrument))
        previous=rows(conn,"""SELECT a.* FROM lot_allocations a JOIN trade_executions t
            ON t.id=a.sell_execution_id WHERE t.account_id=? AND t.instrument_id=?
            AND t.status='effective' ORDER BY a.rowid""",(account,instrument))
        conn.execute("""DELETE FROM lot_allocations WHERE sell_execution_id IN
            (SELECT id FROM trade_executions WHERE account_id=? AND instrument_id=? AND status='effective')""",
            (account,instrument))
        conn.execute("""UPDATE trade_lots SET remaining_qty=0,remaining_cost_fen=0,remaining_fees_fen=0,
            status='reversed' WHERE open_execution_id IN
            (SELECT id FROM trade_executions WHERE account_id=? AND instrument_id=?)""",(account,instrument))
        conn.execute("""UPDATE trade_lots SET remaining_qty=0,remaining_cost_fen=0,remaining_fees_fen=0,
            status='reversed' WHERE opening_id IN
            (SELECT id FROM opening_positions WHERE account_id=? AND instrument_id=? AND id IS NOT ?)""",
            (account,instrument,opening[0] if opening else None))
        if opening is not None:
            conn.execute("""UPDATE trade_lots SET remaining_qty=?,remaining_cost_fen=?,remaining_fees_fen=?,
                status='open' WHERE opening_id=?""",(opening[1],opening[2],opening[3],opening[0]))
        for fact in facts:
            v=json.loads(fact['payload'])
            if v['side']=='buy':
                TradeService._post(conn,fact['id'],v,[],cash=False)
            else:
                TradeService._post(conn,fact['id'],v,self._allocate(conn,v),cash=False)
        if previous:
            audit(conn,'opening_position',opening[0] if opening else instrument,'allocations_rebuilt',
                  request,{'account_id':account,'instrument_id':instrument,'previous_allocations':previous})
        return [{'id':fact['id'],'executed_at':fact['executed_at']} for fact in facts]

    def correct_opening(self,data: OpeningCorrectionInput,opening_id: str|None=None) -> dict:
        """Revise a confirmed opening baseline without overwriting the original row."""
        data=OpeningCorrectionInput.model_validate(data)
        target=data.opening_id or (opening_id or '')
        if not target:
            raise TradeError('correction requires the opening position it replaces')
        if data.opening_id and opening_id and data.opening_id!=opening_id:
            raise Conflict('opening position in body conflicts with the addressed record')
        if data.opening_id!=target:
            data=data.model_copy(update={'opening_id':target})
        self._actual(data.occurred_at)
        value=data.model_dump(mode='json',exclude={'request_id'})
        value['occurred_at']=utc(data.occurred_at)
        payload=dump({'opening_correction':value})
        with self.db.transaction() as conn:
            old=prior(conn,data.request_id,payload)
            if old is not None: return old
            original=one(conn,'SELECT * FROM opening_positions WHERE id=?',(data.opening_id,))
            if original['superseded_by'] is not None:
                raise Conflict('opening position already replaced')
            if original['voided_at'] is not None:
                raise Conflict('opening position already voided')
            account,instrument=original['account_id'],original['instrument_id']
            require_account(conn,account,securities=True,active=True,at=value['occurred_at'])
            id_=uid(); lot=uid()
            # The original row stays exactly as recorded; only its successor link changes.
            conn.execute('UPDATE opening_positions SET superseded_by=? WHERE id=?',(id_,original['id']))
            conn.execute("""INSERT INTO opening_positions
                (id,account_id,instrument_id,quantity,cost_fen,fees_fen,occurred_at,note,superseded_by,voided_at)
                VALUES (?,?,?,?,?,?,?,?,NULL,NULL)""",
                (id_,account,instrument,data.quantity,data.cost_fen,data.fees_fen,value['occurred_at'],data.note))
            conn.execute("INSERT INTO trade_lots VALUES (?,NULL,NULL,'ordinary',NULL,NULL,?,?,?,?,'open',?)",
                         (lot,data.quantity,data.quantity,data.cost_fen,data.fees_fen,id_))
            replayed=self._replay_scope(conn,account,instrument,data.request_id,
                                        (id_,data.quantity,data.cost_fen,data.fees_fen))
            earliest=min([day_of(datetime.fromisoformat(original['occurred_at'])),
                          day_of(data.occurred_at)]+[day_of(datetime.fromisoformat(f['executed_at'])) for f in replayed]).isoformat()
            details={**value,'supersedes':original['id'],'lot_id':lot,
                     'previous':{'quantity':original['quantity'],'cost_fen':original['cost_fen'],
                                 'fees_fen':original['fees_fen'],'occurred_at':original['occurred_at']},
                     'replayed_executions':[f['id'] for f in replayed],'earliest_affected_day':earliest,
                     'strategy_evidence_recheck':True}
            audit(conn,'opening_position',id_,'corrected',data.request_id,details)
            bump(conn,earliest)
            result={'id':id_,'lot_id':lot,'supersedes':original['id'],**value,
                    'replayed_executions':[f['id'] for f in replayed],'earliest_affected_day':earliest,
                    'strategy_evidence_recheck':True}
            remember(conn,data.request_id,payload,result)
            return result

    def void_opening(self,opening_id: str,request_id: str,reason: str) -> dict:
        """Void a baseline while keeping the row as history; unsafe scopes roll back."""
        request=request_id.strip() if isinstance(request_id,str) else ''
        if not 1<=len(request)<=128 or not isinstance(reason,str) or not 1<=len(reason.strip())<=2000:
            raise TradeError('void requires request ID and reason')
        payload=dump({'void_opening':opening_id,'reason':reason})
        with self.db.transaction() as conn:
            old=prior(conn,request,payload)
            if old is not None: return old
            original=one(conn,'SELECT * FROM opening_positions WHERE id=?',(opening_id,))
            if original['voided_at'] is not None:
                raise Conflict('opening position already voided')
            if original['superseded_by'] is not None:
                raise Conflict('opening position already replaced')
            account,instrument=original['account_id'],original['instrument_id']
            voided=utc(require_time(self.clock()))
            conn.execute('UPDATE opening_positions SET voided_at=? WHERE id=?',(voided,opening_id))
            conn.execute("""UPDATE trade_lots SET remaining_qty=0,remaining_cost_fen=0,remaining_fees_fen=0,
                status='reversed' WHERE opening_id=?""",(opening_id,))
            # A baseline that funded sales cannot be removed without breaking them.
            replayed=self._replay_scope(conn,account,instrument,request,None)
            earliest=min([day_of(datetime.fromisoformat(original['occurred_at']))]+
                         [day_of(datetime.fromisoformat(f['executed_at'])) for f in replayed]).isoformat()
            details={'reason':reason,'voided_at':voided,
                     'previous':{'quantity':original['quantity'],'cost_fen':original['cost_fen'],
                                 'fees_fen':original['fees_fen'],'occurred_at':original['occurred_at']},
                     'replayed_executions':[f['id'] for f in replayed],'earliest_affected_day':earliest,
                     'strategy_evidence_recheck':True,
                     # Opening positions carry no cash by themselves; the account's cash
                     # baseline is a separate movement and is never rewritten here.
                     'cash_reversed_fen':0}
            audit(conn,'opening_position',opening_id,'voided',request,details)
            bump(conn,earliest)
            result={'id':opening_id,'account_id':account,'instrument_id':instrument,
                    'voided_at':voided,'reason':reason,'replayed_executions':[f['id'] for f in replayed],
                    'earliest_affected_day':earliest,'strategy_evidence_recheck':True,
                    'cash_reversed_fen':0}
            remember(conn,request,payload,result)
            return result

    def openings(self,account_id: str|None=None,limit: int=100,offset: int=0,ledger_revision: int|None=None) -> dict:
        """Every recorded baseline row, including replaced and voided history."""
        sql="""SELECT o.*,CASE WHEN o.superseded_by IS NULL AND o.voided_at IS NULL THEN 1 ELSE 0 END AS is_current,
            l.id AS lot_id,l.remaining_qty AS remaining_qty,l.opened_qty AS opened_qty,
            l.remaining_cost_fen AS remaining_cost_fen,l.remaining_fees_fen AS remaining_fees_fen,
            l.status AS lot_status
            FROM opening_positions o LEFT JOIN trade_lots l ON l.opening_id=o.id"""
        args: tuple=()
        if account_id is not None:
            sql+=' WHERE o.account_id=?'
            args=(account_id,)
        with self.db.read() as conn:
            revision=one(conn,'SELECT ledger_revision,earliest_affected_day FROM ledger_state')
            if ledger_revision is not None and revision['ledger_revision'] != ledger_revision:
                raise Conflict('opening records changed; reload from first page')
            items=rows(conn,sql+' ORDER BY o.occurred_at,o.rowid LIMIT ? OFFSET ?',(*args,limit,offset))
            for item in items:
                item['is_current']=bool(item['is_current'])
            total=conn.execute('SELECT count(*) FROM ('+sql+')',args).fetchone()[0]
            incomplete=rows(conn,"""SELECT DISTINCT account_id,instrument_id FROM trade_executions
                WHERE status='pending' ORDER BY account_id,instrument_id""")
            return {'items':items,**revision,'data_incomplete':bool(incomplete),
                    'incomplete_scopes':incomplete,'total':total,'limit':limit,'offset':offset}

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
            if data.kind=='dividend' and conn.execute("SELECT 1 FROM effective_cash_movements WHERE account_id=? AND kind='opening' AND occurred_at>=?",(data.account_id,value['occurred_at'])).fetchone():
                raise Conflict('dividend conflicts with opening cash boundary')
            id_=uid()
            movement_id=None
            if data.kind=='dividend':
                movement_id=uid()
                conn.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?,?,?)',(movement_id,data.account_id,None,None,data.amount_fen,'dividend',value['occurred_at'],None,data.request_id,data.note))
            conn.execute("""INSERT INTO company_events
                (id,account_id,instrument_id,kind,occurred_at,cash_movement_id,note,ratio_from,ratio_to,cash_tail_fen)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (id_,data.account_id,data.instrument_id,data.kind,value['occurred_at'],movement_id,data.note,
                 data.ratio_from,data.ratio_to,data.cash_tail_fen))
            details=dict(value)
            applied=None
            gap=None
            if data.kind in ('split','merge'):
                if data.ratio_from is None:
                    # No verified ratio: this stays a recorded data gap. Nothing is
                    # rescaled and nothing is silently assumed.
                    gap=['company_action_ratio_missing']
                else:
                    applied=self._scale_position(conn,data,id_,value['occurred_at'])
                    details.update(applied['details'])
            elif data.kind in ('rights','other'):
                # Rights issues and one-off events have no verified quantity formula,
                # so they stay recorded gaps rather than guessed adjustments.
                gap=['company_action_not_modeled']
            if gap: details['reason_codes']=gap
            event='applied' if applied else ('confirmed' if data.kind=='dividend' else 'unresolved')
            audit(conn,'company_event',id_,event,data.request_id,details)
            bump(conn,day_of(data.occurred_at).isoformat())
            result={'id':id_,**value}
            if applied: result.update(applied['result'])
            elif gap: result['reason_codes']=gap
            remember(conn,data.request_id,payload,result)
            return result

    @staticmethod
    def _scale_position(conn: sqlite3.Connection, data: CompanyInput, event: str, occurred_at: str) -> dict:
        """把既有批次的数量按 ``ratio_to/ratio_from`` 换算，总成本与总费用守恒。

        只调整**尚未被卖出**的批次：一个已经用旧尺度分摊过成本的批次无法在不
        改写已执行成交的前提下换算，因此这里整体拒绝，而不是留下一半旧尺度
        一半新尺度的账本。碎股按调用方申报的真实现金尾差单独记账，绝不并进
        成本，也不伪造任何买卖成交通知。
        """
        frm,to=data.ratio_from,data.ratio_to
        scope=(data.account_id,data.instrument_id)
        if conn.execute('SELECT 1 FROM company_events WHERE account_id=? AND instrument_id=? AND occurred_at>=? AND applied_at IS NOT NULL AND voided_at IS NULL',(*scope,occurred_at)).fetchone():
            raise Conflict('company action must follow previous applied events')
        if conn.execute("""SELECT 1 FROM trade_executions WHERE account_id=? AND instrument_id=?
                AND status='effective' AND executed_at>?""",(*scope,occurred_at)).fetchone():
            raise Conflict('company action must follow the last effective trade in this scope')
        lots=rows(conn,"""SELECT l.id,l.opened_qty,l.remaining_qty,l.remaining_cost_fen,l.remaining_fees_fen,
            l.lot_role,l.frozen_atr FROM trade_lots l
            LEFT JOIN trade_executions t ON t.id=l.open_execution_id
            LEFT JOIN opening_positions o ON o.id=l.opening_id
            WHERE COALESCE(t.account_id,o.account_id)=? AND COALESCE(t.instrument_id,o.instrument_id)=?
            AND l.status='open' AND l.remaining_qty>0
            AND (t.status='effective' OR (o.id IS NOT NULL AND o.superseded_by IS NULL AND o.voided_at IS NULL))
            ORDER BY COALESCE(t.executed_at,o.occurred_at),l.rowid""",scope)
        if not lots:
            raise Conflict('company action requires an open position in this scope')
        if conn.execute("SELECT 1 FROM opening_positions WHERE account_id=? AND instrument_id=? AND superseded_by IS NULL AND voided_at IS NULL AND occurred_at>=?", (*scope, occurred_at)).fetchone():
            raise Conflict('company action must follow confirmed opening position')
        scaled,fractional,invalid=[],False,False
        for lot in lots:
            if lot['opened_qty']!=lot['remaining_qty']:
                raise Conflict('company action cannot rescale a batch that already funded a sale')
            units=lot['opened_qty']*to
            new=units//frm
            if not 0<new<=MAX_QTY:
                raise Conflict('company action quantity outside supported range')
            fractional=fractional or units%frm!=0
            invalid=invalid or (lot['lot_role'] in ('main','sub') and lot['frozen_atr'] is not None)
            scaled.append((lot,new))
        if fractional and data.cash_tail_fen is None:
            raise Conflict('fractional shares require the actual cash tail amount')
        if not fractional and data.cash_tail_fen is not None:
            raise Conflict('cash tail requires a fractional share result')
        tail=None
        if data.cash_tail_fen is not None:
            from backend.application.trading.service import post_cash
            tail=post_cash(conn, account=data.account_id,kind='adjustment',amount=data.cash_tail_fen,
                           at=occurred_at,note='公司行为现金尾差',request=f'{event}:tail')['id']
        snapshot={'lots':[],'ratio_from':frm,'ratio_to':to}
        for lot,new in scaled:
            conn.execute('UPDATE trade_lots SET opened_qty=?,remaining_qty=? WHERE id=?',(new,new,lot['id']))
            snapshot['lots'].append({'id':lot['id'],'opened_before':lot['opened_qty'],
                                     'opened_after':new,'cost_before':lot['remaining_cost_fen'],
                                     'fees_before':lot['remaining_fees_fen']})
        if tail: snapshot['tail_cash_movement_id']=tail
        applied=utc(datetime.now(UTC))
        conn.execute("""UPDATE company_events SET applied_at=?,strategy_evidence_invalid=?,applied_snapshot=?
            WHERE id=?""",(applied,1 if invalid else 0,dump(snapshot),event))
        details={'applied_at':applied,'ratio_from':frm,'ratio_to':to,
                 'cash_tail_fen':data.cash_tail_fen,'tail_cash_movement_id':tail,
                 'batches':snapshot['lots'],'strategy_evidence_invalid':invalid}
        if invalid:
            details['strategy_evidence_recheck_reason']='公司行为改变了价格尺度，冻结 ATR 与保护价需重新核实'
        result={'applied_at':applied,'ratio_from':frm,'ratio_to':to,
                'cash_tail_fen':data.cash_tail_fen,'strategy_evidence_invalid':invalid,
                'batches':snapshot['lots']}
        return {'details':details,'result':result}

    def void_company_event(self,event_id: str,request_id: str,reason: str) -> dict:
        request=request_id.strip() if isinstance(request_id,str) else ''
        if not 1<=len(request)<=128 or not isinstance(reason,str) or not 1<=len(reason.strip())<=2000:
            raise TradeError('void requires request ID and reason')
        payload=dump({'void_company_event':event_id,'reason':reason})
        with self.db.transaction() as conn:
            old=prior(conn,request,payload)
            if old is not None: return old
            event=one(conn,'SELECT * FROM company_events WHERE id=?',(event_id,))
            if event['voided_at'] is not None:
                raise Conflict('company event already voided')
            if event['superseded_by'] is not None:
                raise Conflict('company event already replaced')
            voided=utc(require_time(self.clock()))
            details={'reason':reason,'kind':event['kind'],'applied_at':event['applied_at'],
                     'strategy_evidence_invalid_before':bool(event['strategy_evidence_invalid'])}
            if event['applied_at'] is not None:
                if conn.execute('SELECT 1 FROM company_events WHERE account_id=? AND instrument_id=? AND occurred_at>? AND applied_at IS NOT NULL AND voided_at IS NULL',(event['account_id'],event['instrument_id'],event['occurred_at'])).fetchone():
                    raise Conflict('later corporate action depends on this event')
                snapshot=json.loads(event['applied_snapshot'])
                # Only an untouched application can be undone: if later facts moved
                # these batches, reversing the ratio would invent a wrong quantity.
                for item in snapshot['lots']:
                    lot=one(conn,'SELECT * FROM trade_lots WHERE id=?',(item['id'],))
                    if lot['opened_qty']!=item['opened_after'] or lot['remaining_qty']!=item['opened_after'] or lot['status']!='open':
                        raise Conflict('later facts changed these batches; void is not safe')
                for item in snapshot['lots']:
                    conn.execute("""UPDATE trade_lots SET opened_qty=?,remaining_qty=?
                        WHERE id=?""",(item['opened_before'],item['opened_before'],item['id']))
                details['batches']=snapshot['lots']
                tail=snapshot.get('tail_cash_movement_id')
                if tail:
                    movement=one(conn,'SELECT * FROM cash_movements WHERE id=?',(tail,))
                    if conn.execute('SELECT 1 FROM cash_movements WHERE reversal_of=?',(tail,)).fetchone():
                        raise Conflict('cash tail already reversed')
                    conn.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?,?,?)',
                                 (uid(),movement['account_id'],None,None,-movement['amount_fen'],'reversal',
                                  movement['occurred_at'],tail,None,reason))
                    details['tail_reversed_fen']=movement['amount_fen']
                conn.execute('UPDATE company_events SET voided_at=?,strategy_evidence_invalid=0 WHERE id=?',(voided,event_id))
            else:
                conn.execute('UPDATE company_events SET voided_at=? WHERE id=?',(voided,event_id))
            details['voided_at']=voided
            audit(conn,'company_event',event_id,'voided',request,details)
            bump(conn,day_of(datetime.fromisoformat(event['occurred_at'])).isoformat())
            result={**one(conn,'SELECT * FROM company_events WHERE id=?',(event_id,)),'reason':reason,
                    # 冲销的实际金额必须回给调用方：现金尾差是按真实金额独立记录的事实。
                    'tail_reversed_fen':details.get('tail_reversed_fen',0),
                    'reversed_batches':details.get('batches',[])}
            remember(conn,request,payload,result)
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
        # A replaced or voided opening row is history: its batch must never add a
        # second position. Reversed batches are likewise excluded, so a replay that
        # is still in progress can never be observed as real holdings.
        result=rows(conn,"""SELECT l.id AS lot_id,l.open_execution_id,l.opening_id,l.lot_role,l.strategy_id,l.signal_date,l.frozen_atr,
            COALESCE(t.instrument_id,o.instrument_id) AS instrument_id,COALESCE(t.executed_at,o.occurred_at) AS opened_at,
            l.opened_qty-COALESCE(SUM(CASE WHEN s.id IS NOT NULL THEN a.quantity END),0) AS quantity,
            COALESCE(t.amount_fen,o.cost_fen)-COALESCE(SUM(CASE WHEN s.id IS NOT NULL THEN a.cost_fen END),0) AS cost_fen,
            COALESCE(t.fees_fen,o.fees_fen)-COALESCE(SUM(CASE WHEN s.id IS NOT NULL THEN a.buy_fees_fen END),0) AS fees_fen
            FROM trade_lots l LEFT JOIN trade_executions t ON t.id=l.open_execution_id LEFT JOIN opening_positions o ON o.id=l.opening_id
            LEFT JOIN lot_allocations a ON a.lot_id=l.id LEFT JOIN trade_executions s ON s.id=a.sell_execution_id AND s.status='effective' AND s.executed_at<=?
            WHERE COALESCE(t.account_id,o.account_id)=? AND COALESCE(t.executed_at,o.occurred_at)<=? AND l.status!='reversed'
            AND (t.status='effective' OR (o.id IS NOT NULL AND o.superseded_by IS NULL AND o.voided_at IS NULL))
            GROUP BY l.id ORDER BY opened_at,l.rowid""",(cutoff,account,cutoff))
        # Each applied action retains its before/after quantities. Reverse future
        # changes in reverse effective order; cost/fee facts remain unchanged.
        by_id={lot['lot_id']:lot for lot in result}
        for event in rows(conn,"""SELECT applied_snapshot FROM company_events
            WHERE account_id=? AND applied_at IS NOT NULL AND voided_at IS NULL
              AND occurred_at>? ORDER BY occurred_at DESC,rowid DESC""",(account,cutoff)):
            for item in json.loads(event['applied_snapshot'])['lots']:
                if item['id'] in by_id:
                    by_id[item['id']]['quantity'] += item['opened_before']-item['opened_after']
        return result

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
        """Attach each real strategy batch to the recorded V9/G2 decision it came from.

        The ledger stays authoritative: a batch is only 'verified' when the frozen ATR,
        the signal date and a matching recorded strategy position exist. Nothing here
        invents a decision, a stop level or a signal date for a legacy batch.
        """
        cutoff = self._cutoff(as_of)
        with self.db.read() as conn:
            require_account(conn,account,securities=True,at=cutoff)
            versions = {**one(conn,'SELECT ledger_revision FROM ledger_state'),
                        **one(conn,'SELECT quote_revision FROM quote_state')}
            lots = [lot for lot in self._lots(conn, account, cutoff)
                    if lot['lot_role'] in ('main','sub')]
            positions = self._strategy_positions(conn, account, cutoff)
            for lot in lots:
                payload = conn.execute('SELECT payload FROM trade_executions WHERE id=?',(lot['open_execution_id'],)).fetchone()
                execution = json.loads(payload[0]) if payload else {}
                proof = verified_trade_signal(conn,execution) if payload else None
                lot['signal_evidence'] = proof
                lot['strategy_deviation'] = bool(execution.get('strategy_deviation'))
                lot['deviation_note'] = execution.get('note') if lot['strategy_deviation'] else None
                lot['signal_id'] = execution.get('signal_id')
            codes = {row['id']: row['code'] for row in
                     rows(conn,'SELECT id,code FROM instruments')}
            invalidated = self._scale_invalidated(conn, account, cutoff)
            items, reasons = self._evidence_items(lots, positions, codes, cutoff, invalidated)
            if invalidated:
                reasons = sorted(set(reasons) | {'company_action_invalidated_strategy_scale'})
            return {**versions,'database_uuid':self.db.identity.database_uuid,'generation':self.db.identity.generation,'account_id':account,'as_of':cutoff,
                    'status':self._evidence_status(items),'reason_codes':reasons,
                    'items':items,
                    'note':'策略证据只说明该批次来自哪一条已记录的 V9/G2 决策；成交、数量与现金仍以账本为准。'}

    @staticmethod
    def _scale_invalidated(conn: sqlite3.Connection, account: str, cutoff: str) -> set[str]:
        """Instruments whose applied company action broke the frozen price scale."""
        return {row['instrument_id'] for row in rows(conn,
            """SELECT DISTINCT instrument_id FROM company_events WHERE account_id=?
            AND occurred_at<=? AND strategy_evidence_invalid=1""",(account,cutoff))}

    @staticmethod
    def _strategy_positions(conn: sqlite3.Connection, account: str, cutoff: str) -> dict:
        """Index recorded strategy positions by (index_id, signal_date)."""
        try:
            found = rows(conn,
                'SELECT index_id,signal_date,entry_date,entry_price,stop_price,trail_trigger,'
                'stage,highest_price,holding_days,exit_after_bars,renewals,e2a_extended_days,'
                'extended,add_executed,exit_filled,closed_date,last_exit_action,last_exit_reason,'
                'pending_stop,pending_stop_effective_date,execution_source,main_lot_id '
                'FROM strategy_positions WHERE account_id=? AND updated_at<=?',(account,cutoff))
        except sqlite3.OperationalError:
            # A database below schema 8 has no strategy evidence table at all.
            return {}
        return {(row['index_id'],str(row['signal_date'])[:10]): row for row in found}

    @staticmethod
    def _evidence_items(lots: list[dict], positions: dict, codes: dict, cutoff: str,
                        invalidated: set[str] | None = None):
        from backend.domain.strategy.symbols import IndexCodeError, index_for_etf

        invalidated = invalidated or set()
        items, reasons = [], set()
        for lot in lots:
            code = codes.get(lot['instrument_id'])
            try:
                index_id = index_for_etf(code) if code else None
            except IndexCodeError:
                index_id = None
            signal_date = str(lot['signal_date'])[:10] if lot['signal_date'] else None
            record = positions.get((index_id,signal_date)) if index_id and signal_date else None
            codes_out = list(lot.get('reason_codes', []))
            if lot['instrument_id'] in invalidated:
                # The batch itself is intact, but its frozen ATR and protective stop
                # belong to the pre-action price scale and cannot be reused silently.
                status, codes_out = 'unverified', codes_out + ['company_action_invalidated_strategy_scale']
            elif lot.get('strategy_id') == 'NDX_AUX_T5':
                proof = lot.get('signal_evidence') or {}
                status = 'verified' if index_id == 'NDX' and proof.get('kind') == 'ndx_aux_advice_link' and not lot.get('strategy_deviation') else 'unverified'
                if status != 'verified': codes_out.append('ndx_aux_evidence_unverified')
            elif lot['frozen_atr'] is None or signal_date is None:
                status, codes_out = 'unverified', codes_out + ['frozen_atr_or_signal_date_missing']
            elif index_id is None:
                status, codes_out = 'unverified', codes_out + ['security_is_not_a_configured_etf']
            elif lot.get('signal_evidence'):
                status = 'verified'
            elif record is None:
                status, codes_out = 'unverified', codes_out + ['strategy_position_not_recorded']
            else:
                status, codes_out = 'unverified', codes_out + ['strategy_signal_link_not_verified']
            if lot.get('strategy_deviation'):
                status = 'unverified'
                codes_out.append('strategy_deviation')
            reasons.update(codes_out)
            items.append({**lot,'instrument_code':code,'index_id':index_id,
                          'evidence_status':status,'reason_codes':codes_out,
                          'strategy':None if lot.get('strategy_id') == 'NDX_AUX_T5' or record is None or status!='verified' or record['main_lot_id']!=lot['lot_id'] else {
                              'entry_date':record['entry_date'],'entry_price':record['entry_price'],
                              'stop_price':record['stop_price'],'trail_trigger':record['trail_trigger'],
                              'stage':record['stage'],'highest_price':record['highest_price'],
                              'holding_days':record['holding_days'],
                              'exit_after_bars':record['exit_after_bars'],
                              'renewals':record['renewals'],
                              'e2a_extended_days':record['e2a_extended_days'],
                              'extended':bool(record['extended']),
                              'add_executed':bool(record['add_executed']),
                              'exit_filled':bool(record['exit_filled']),
                              'closed_date':record['closed_date'],
                              'last_exit_action':record['last_exit_action'],
                              'last_exit_reason':record['last_exit_reason'],
                              'pending_stop':record['pending_stop'],
                              'pending_stop_effective_date':record['pending_stop_effective_date'],
                              'execution_source':record['execution_source']}})
        return items, sorted(reasons)

    @staticmethod
    def _evidence_status(items: list[dict]) -> str:
        if not items:
            return 'valid'
        marks = {item['evidence_status'] for item in items}
        if marks == {'verified'}:
            return 'verified'
        return 'partial' if 'verified' in marks or 'partial' in marks else 'unavailable'

    def valuation(self, account: str, as_of: datetime, previous_trading_day: date | None = None,
                  quote_revision: int | None = None, ledger_revision: int | None = None,
                  database_uuid: str | None = None, generation: int | None = None) -> dict:
        self._cutoff(as_of)
        try:
            window = market_window(as_of)
        except CalendarUnavailable:
            window = None
        if window is not None and previous_trading_day is not None and previous_trading_day != window.previous:
            raise TradeError('previous trading day conflicts with exchange calendar')
        with self.db.read() as conn:
            require_account(conn,account,securities=True,at=utc(as_of))
            current = one(conn,'SELECT ledger_revision FROM ledger_state')['ledger_revision']
            current_quote = self._revision(conn,None)
            identity = self.db.identity
            if ledger_revision is not None and (current != ledger_revision or quote_revision != current_quote or database_uuid != identity.database_uuid or generation != identity.generation):
                raise Conflict('valuation snapshot changed; reload monthly summary')
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
            return {**result,'database_uuid':identity.database_uuid,'generation':identity.generation}

    def monthly_pnl(self, account: str, month: date) -> dict:
        """One current-ledger/quote revision, daily rows only; not a historical knowledge replay."""
        if month.day!=1 or not 1900<=month.year<=9997:
            raise TradeError('month must be its first date')
        now=self.clock(); self._cutoff(now)
        end=(month.replace(day=28)+timedelta(days=4)).replace(day=1)
        if month>day_of(now): raise TradeError('future month is not supported')
        with self.db.read() as conn:
            master=require_account(conn,account,securities=True)
            revision=self._revision(conn,None)
            ledger=one(conn,'SELECT ledger_revision FROM ledger_state')['ledger_revision']
            items=[]; day=month
            while day<end and day<=day_of(now):
                cutoff=min(now,datetime.combine(day,time.max,SHANGHAI))
                row={'day':day.isoformat(),'as_of':utc(cutoff),'daily_pnl_fen':None,'equity_fen':None,'quality':'unavailable'}
                try: window=market_window(cutoff)
                except CalendarUnavailable:
                    row['reason']='calendar_unavailable'
                else:
                    row['is_session']=window.is_session
                    if master['effective_at'] is None or utc(cutoff)<master['effective_at']:
                        row['reason']='account_effective_date_unavailable'
                    else:
                        value=self._valuation(conn,account,cutoff,window,revision)
                        row.update(daily_pnl_fen=value['daily_pnl_fen'],equity_fen=value['equity_fen'],quality=value['daily_pnl_quality'],
                                   reasons=value['reasons'],market_phase=window.phase,daily_return_pct=value['daily_return_pct'],daily_return_reason=value['daily_return_reason'])
                items.append(row); day+=timedelta(days=1)
            return {'account_id':account,'month':month.isoformat(),'as_of':utc(now),'ledger_revision':ledger,'quote_revision':revision,
                    'database_uuid':self.db.identity.database_uuid,'generation':self.db.identity.generation,
                    'source':'current_ledger_projection_not_knowledge_replay','items':items}

    def _valuation(self, conn: sqlite3.Connection, account: str, as_of: datetime,
                   window: MarketWindow | None, revision: int) -> dict:
        return self._valuation_projection(conn,account,as_of,window,revision,include_return=True)

    def _valuation_projection(self, conn: sqlite3.Connection, account: str, as_of: datetime,
                              window: MarketWindow | None, revision: int, *, include_return: bool) -> dict:
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
        # An applied split/merge is a settled fact, not a gap. Only events that are
        # still unresolved and not voided may block the instrument, and an applied
        # event that invalidated a strategy scale is reported as its own reason code.
        unresolved=[r for r in actions if r['kind']!='dividend'
                    and r['applied_at'] is None and r['voided_at'] is None]
        scale_invalid={r['instrument_id'] for r in actions if r['strategy_evidence_invalid'] and r['voided_at'] is None}
        tails={json.loads(r['applied_snapshot']).get('tail_cash_movement_id'): r for r in actions if r['applied_at'] and r['voided_at'] is None and r['applied_snapshot'] and json.loads(r['applied_snapshot']).get('tail_cash_movement_id')}
        blocked={r['instrument_id'] for r in pending}|{r['instrument_id'] for r in unresolved}
        reasons=[] if window else ['calendar_unavailable']
        later_openings=rows(conn,'SELECT instrument_id FROM opening_positions WHERE account_id=? AND occurred_at>? AND voided_at IS NULL AND superseded_by IS NULL',(account,cutoff))
        if later_openings:
            blocked.update(r['instrument_id'] for r in later_openings)
            reasons.append('before_confirmed_position_baseline')
        if pending: reasons.append('pending_trade')
        if unresolved: reasons.append('unresolved_company_action')
        if scale_invalid: reasons.append('company_action_invalidated_strategy_scale')
        trades=rows(conn,"SELECT * FROM trade_executions WHERE account_id=? AND status='effective' AND executed_at<=? ORDER BY executed_at,rowid",(account,cutoff))
        realized_rows=rows(conn,"""SELECT t.instrument_id,SUM(t.amount_fen-t.fees_fen-(SELECT COALESCE(SUM(a.cost_fen+a.buy_fees_fen),0) FROM lot_allocations a WHERE a.sell_execution_id=t.id)) AS realized_fen
            FROM trade_executions t WHERE t.account_id=? AND t.status='effective' AND t.side='sell' AND t.executed_at<=? GROUP BY t.instrument_id""",(account,cutoff))
        realized={r['instrument_id']:r['realized_fen'] for r in realized_rows}
        ids={r['instrument_id'] for r in lots+prev_lots+trades+actions+pending+later_openings}
        positions=[]; daily_known=0; daily_ok=not blocked and window is not None; market_known=0; market_ok=not blocked and window is not None; unrealized_known=0
        prev_market=0; prev_ok=window is not None
        opening_today_any=False
        for instrument in sorted(ids):
            master=one(conn,"SELECT code,market,name,name_source,name_observed_at,CASE WHEN name IS NULL THEN 'missing' ELSE 'valid' END AS name_quality FROM instruments WHERE id=?",(instrument,))
            current=[r for r in lots if r['instrument_id']==instrument and r['quantity']>0]
            previous=[r for r in prev_lots if r['instrument_id']==instrument and r['quantity']>0]
            quantity=sum(r['quantity'] for r in current); prev_qty=sum(r['quantity'] for r in previous)
            cost=sum(r['cost_fen'] for r in current); fees=sum(r['fees_fen'] for r in current)
            q,quality=self._current_quote(conn,instrument,cutoff,window,revision) if quantity else (None,'valid')
            if q and any(r['instrument_id']==instrument and r['applied_at'] is not None and r['voided_at'] is None and r['occurred_at']>q['effective_at'] for r in actions):
                quality='stale'
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
            tail=sum(r['cash_tail_fen'] or 0 for r in tails.values() if r['instrument_id']==instrument and r['occurred_at']>=start)
            cash_trades=sum((r['amount_fen'] if r['side']=='sell' else -r['amount_fen'])-r['fees_fen'] for r in today)
            opening_today=any(r['opening_id'] and r['opened_at']>=start for r in lots if r['instrument_id']==instrument)
            if opening_today:
                opening_today_any=True
                reasons.append(f'{instrument}:opening_position_during_day')
            invalid_session_trade = bool(today and window and not window.is_session)
            if invalid_session_trade:
                reasons.append(f'{instrument}:trade_on_non_session_day')
            dp=None if mv is None or old_value is None or instrument in blocked or opening_today or invalid_session_trade else mv-old_value+cash_trades+dividend+tail
            if dp is None: daily_ok=False
            else: daily_known+=dp
            positions.append({'instrument_id':instrument,**master,'quantity':quantity,'lots':current,'remaining_cost_fen':cost,'remaining_fees_fen':fees,'holding_cost_fen':cost+fees,'price':q['price'] if q else None,'quote':q,'market_value_fen':mv,'realized_fen':None if instrument in blocked else realized.get(instrument,0),'unrealized_fen':None if mv is None or instrument in blocked else mv-cost-fees,'daily_pnl_fen':dp,'quality':'derived_unverified' if instrument in blocked else quality})
        balance,cash_quality,cash_reasons=cash_balance(conn,account,cutoff); reasons+=cash_reasons
        equity=balance+market_known if balance is not None and cash_quality=='valid' and market_ok else None
        previous_cash,previous_cash_quality,_=cash_balance(conn,account,before)
        flows=rows(conn,"SELECT id,kind,amount_fen FROM effective_cash_movements WHERE account_id=? AND occurred_at>=? AND occurred_at<=? AND kind IN ('deposit','withdrawal','adjustment','opening')",(account,start,cutoff))
        external=sum(r['amount_fen'] for r in flows if r['kind'] in ('deposit','withdrawal'))
        adjustments=sum(r['amount_fen'] for r in flows if r['kind']=='adjustment' and r['id'] not in tails)
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
                total_flows-=sum(r['cash_tail_fen'] or 0 for r in tails.values() if r['occurred_at']>coverage)
                cumulative=equity-base['amount_fen']-total_flows
            else: reasons.append('cumulative_baseline_incomplete')
        if not openings: reasons.append('cumulative_baseline_missing')
        prior_equity=None; return_reason='not_trading_session'; return_pct=None; prior_as_of=None
        if include_return and window and window.is_session:
            return_reason='prior_close_equity_unavailable'
            try:
                prior_time=session_bounds(window.previous)[1]
                prior_as_of=utc(prior_time)
                master=one(conn,'SELECT effective_at FROM accounts WHERE id=?',(account,))
                if master['effective_at'] and master['effective_at']<=prior_as_of:
                    prior_equity=self._valuation_projection(conn,account,prior_time,market_window(prior_time),revision,include_return=False)['equity_fen']
            except CalendarUnavailable:
                pass
            if daily is None: return_reason='daily_pnl_unavailable'
            elif prior_equity is not None and prior_equity<=0: return_reason='prior_close_equity_nonpositive'
            elif prior_equity is not None:
                return_pct=format(Decimal(daily)*100/Decimal(prior_equity),'.6f'); return_reason=None
        return {**rev,'account_id':account,'as_of':cutoff,'trading_day':day.isoformat(),'market_phase':window.phase if window else 'unknown','valuation_trading_day':window.valuation_day.isoformat() if window else None,'is_trading_day':window.is_session if window else None,'previous_trading_day':previous_trading_day.isoformat() if previous_trading_day else None,'source':'sqlite_ledger_and_explicit_quotes','quality':'valid' if equity is not None and daily is not None and cumulative is not None and equity_pnl is not None else 'derived_unverified','reasons':sorted(set(reasons)),'cash_fen':balance,'cash_quality':cash_quality,'known_market_value_fen':market_known,'market_value_fen':market_known if market_ok else None,'equity_fen':equity,'daily_pnl_fen':daily,'daily_return_pct':return_pct,'daily_return_reason':return_reason,'previous_close_equity_fen':prior_equity,'previous_close_as_of':prior_as_of,'daily_pnl_quality':'valid' if daily is not None else 'unavailable','equity_pnl_fen':equity_pnl,'reconciliation_difference_fen':daily_known-equity_pnl if daily_ok and equity_pnl is not None else None,'external_flow_fen':external,'adjustments_fen':adjustments,'dividends_fen':sum(r['amount_fen'] for r in actions if r['kind']=='dividend'),'daily_dividends_fen':sum(r['amount_fen'] for r in actions if r['kind']=='dividend' and r['occurred_at']>=start),'realized_fen':sum(realized.values()) if not blocked else None,'unrealized_fen':unrealized_known if market_ok else None,'cumulative_pnl_fen':cumulative,'cumulative_from':coverage,'cumulative_cash_baseline_fen':openings[0]['amount_fen'] if len(openings)==1 else None,'positions':[r for r in positions if r['quantity']>0],'instrument_results':positions}
