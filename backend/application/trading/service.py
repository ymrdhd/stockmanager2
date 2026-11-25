"""Authoritative trading facts; each use case owns one database transaction."""
from datetime import UTC, date, datetime
from decimal import Decimal
import json
import re
import sqlite3
from typing import Literal
from collections.abc import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.infrastructure.database import Database
from backend.application.market_calendar import ordinary_execution_time

MAX_FEN = 10**15
MAX_QTY = 10**9
SCALE = 10_000


class TradeError(ValueError):
    pass


class Conflict(TradeError):
    pass


class Missing(TradeError):
    pass


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, revalidate_instances='always')


class AccountInput(Input):
    request_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    currency: Literal['CNY'] = 'CNY'
    kind: Literal['securities','bank','cash','payment','other_asset','credit_card','other_liability'] = 'securities'
    included: bool = Field(default=True, strict=True)
    effective_at: datetime | None = None

    @field_validator('effective_at', mode='before')
    @classmethod
    def effective_timestamp(cls, value):
        if value is not None and (not isinstance(value,(str,datetime)) or isinstance(value,str) and 'T' not in value):
            raise ValueError('account effective time must be ISO-8601 with timezone')
        return value

    @model_validator(mode='after')
    def validate_account(self):
        if self.kind != 'securities' and self.effective_at is None:
            raise ValueError('manual account requires effective timestamp')
        if self.effective_at is not None and (self.effective_at.utcoffset() is None or not 1900 <= self.effective_at.year <= 9998):
            raise ValueError('account timestamp must have timezone and supported year')
        return self


class InstrumentInput(Input):
    request_id: str = Field(min_length=1, max_length=128)
    code: str = Field(pattern=r'^\d{6}$')
    market: Literal['SH', 'SZ', 'BJ']
    kind: Literal['stock', 'etf']
    currency: Literal['CNY'] = 'CNY'


class TradeInput(Input):
    request_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    instrument_id: str = Field(min_length=1, max_length=128)
    side: Literal['buy', 'sell']
    quantity: int = Field(strict=True, gt=0, le=MAX_QTY)
    price: Decimal = Field(gt=0, le=1000000, decimal_places=4, max_digits=11)
    fees_fen: int | None = Field(ge=0, le=MAX_FEN, strict=True)
    executed_at: datetime
    trading_day: date
    external_id: str | None = Field(default=None, min_length=1, max_length=128)
    note: str = Field(default='', max_length=2000)
    strategy_id: str | None = Field(default=None, min_length=1, max_length=64)
    lot_role: Literal['ordinary', 'main', 'sub'] = 'ordinary'
    signal_date: date | None = None
    frozen_atr: Decimal | None = Field(default=None, gt=0, le=1000000, decimal_places=4, max_digits=11)
    signal_id: str | None = Field(default=None, min_length=1, max_length=128)
    strategy_exit_action: Literal['STOP','GAP_STOP','EXIT','CLOSED'] | None = None
    allocations: dict[str, int] | None = None
    strategy_deviation: bool = False

    @field_validator('executed_at', mode='before')
    @classmethod
    def timestamp_input(cls, value):
        if not isinstance(value, (str, datetime)) or isinstance(value, str) and 'T' not in value:
            raise ValueError('execution time must be ISO-8601 with timezone')
        return value

    @field_validator('trading_day', 'signal_date', mode='before')
    @classmethod
    def date_input(cls, value):
        if value is not None and not (type(value) is date or isinstance(value, str) and len(value) == 10 and date.fromisoformat(value).isoformat() == value):
            raise ValueError('date must be YYYY-MM-DD')
        return value

    @field_validator('price', 'frozen_atr', mode='before')
    @classmethod
    def exact_decimal(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError('decimal prices must be strings or integers')
        return value

    @field_validator('quantity', mode='before')
    @classmethod
    def whole_shares(cls, value):
        if isinstance(value, str) and len(value) <= 32 and re.fullmatch(r'[0-9]+(?:\.0+)?', value):
            return int(value.split('.')[0])
        return value

    @field_validator('allocations', mode='before')
    @classmethod
    def exact_allocations(cls, value):
        if isinstance(value, dict):
            value = {key: cls.whole_shares(q) for key, q in value.items()}
        if value is not None and (not isinstance(value, dict) or not value or any(
                type(q) is not int or not 0 < q <= MAX_QTY or not isinstance(key, str) or not key for key, q in value.items())):
            raise ValueError('allocations must map lot IDs to positive integer quantities')
        return value

    @model_validator(mode='after')
    def validate_trade(self):
        if not 1900 <= self.executed_at.year <= 9998:
            raise ValueError('execution year outside supported range')
        if self.executed_at.utcoffset() is None:
            raise ValueError('execution timestamp requires timezone')
        if self.executed_at.astimezone(ZoneInfo('Asia/Shanghai')).date() != self.trading_day:
            raise ValueError('trading day must match Asia/Shanghai execution date')
        if self.side == 'buy' and self.allocations is not None:
            raise ValueError('buy cannot allocate lots')
        if (self.lot_role != 'ordinary') != (self.strategy_id is not None):
            raise ValueError('strategy main/sub lots require strategy ID; ordinary lots do not')
        if self.lot_role == 'ordinary' and (self.signal_date is not None or self.frozen_atr is not None or self.signal_id is not None):
            raise ValueError('ordinary lots cannot carry strategy signal metadata')
        if self.signal_date is not None and self.signal_date > self.trading_day:
            raise ValueError('signal date cannot follow execution date')
        if self.side == 'sell' and (self.signal_date is not None or self.frozen_atr is not None or self.signal_id is not None):
            raise ValueError('sell uses original lot signal metadata, not new signal metadata')
        if self.strategy_exit_action is not None and self.side != 'sell':
            raise ValueError('only actual sells may record a confirmed strategy exit action')
        if money(self.quantity, int(self.price * SCALE)) > MAX_FEN:
            raise ValueError('trade amount exceeds supported range')
        return self


class CashInput(Input):
    request_id: str = Field(min_length=1, max_length=128)
    account_id: str
    kind: Literal['opening', 'deposit', 'withdrawal', 'adjustment']
    amount_fen: int = Field(strict=True, ge=-MAX_FEN, le=MAX_FEN)
    occurred_at: datetime
    note: str = Field(min_length=1, max_length=2000)
    transfer_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator('occurred_at', mode='before')
    @classmethod
    def timestamp_input(cls, value):
        return TradeInput.timestamp_input(value)

    @model_validator(mode='after')
    def validate_cash(self):
        if not 1900 <= self.occurred_at.year <= 9998:
            raise ValueError('cash year outside supported range')
        if self.occurred_at.utcoffset() is None:
            raise ValueError('cash timestamp requires timezone')
        if (self.kind == 'deposit' and self.amount_fen <= 0 or
                self.kind == 'withdrawal' and self.amount_fen >= 0 or
                self.kind == 'opening' and self.amount_fen < 0 or
                self.kind == 'adjustment' and self.amount_fen == 0):
            raise ValueError('invalid cash amount for movement kind')
        return self


def uid() -> str:
    return str(uuid4())


def utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec='microseconds')


def money(quantity: int, price: int) -> int:
    # Price is integer 1/10000 CNY, convert once to cents, ROUND_HALF_UP.
    return (quantity * price + 50) // 100


def share(total: int, quantity: int, remaining: int) -> int:
    # Allocate proportionally from remaining cost; final sale receives all residue.
    return (total * quantity * 2 + remaining) // (2 * remaining)


def dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def canonical(data: TradeInput) -> dict:
    value = data.model_dump(mode='json', exclude={'request_id'})
    value['price'] = int(data.price * SCALE)
    value['frozen_atr'] = int(data.frozen_atr * SCALE) if data.frozen_atr is not None else None
    value['executed_at'] = utc(data.executed_at)
    # Old requests without a signal association keep exactly their original canonical form.
    if data.signal_id is None: value.pop('signal_id', None)
    if data.strategy_exit_action is None: value.pop('strategy_exit_action', None)
    return value


def rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    cursor = conn.execute(sql, args)
    names = [col[0] for col in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> dict:
    result = rows(conn, sql, args)
    if not result:
        raise Missing('record not found')
    return result[0]


def audit(conn: sqlite3.Connection, entity: str, id_: str, event: str, request: str, details: object) -> None:
    conn.execute('INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?)',
                 (uid(), entity, id_, event, request, utc(datetime.now(UTC)), 'local_user', dump(details)))


def bump(conn: sqlite3.Connection, day: str) -> None:
    conn.execute('UPDATE ledger_state SET ledger_revision=ledger_revision+1, earliest_affected_day=CASE WHEN earliest_affected_day IS NULL OR earliest_affected_day>? THEN ? ELSE earliest_affected_day END WHERE id=1', (day, day))


def prior(conn: sqlite3.Connection, request: str, payload: str) -> dict | None:
    row = conn.execute('SELECT payload,result_json FROM ledger_requests WHERE request_id=?', (request,)).fetchone()
    if row is not None and row[0] != payload:
        raise Conflict('request_id already used with different content')
    return json.loads(row[1]) if row else None


def remember(conn: sqlite3.Connection, request: str, payload: str, result: dict) -> None:
    conn.execute('INSERT INTO ledger_requests VALUES (?,?,?,?)', (request, payload, result['id'], dump(result)))


def require_account(conn: sqlite3.Connection, account: str, *, securities: bool | None = None,
                    active: bool = False, at: str | None = None) -> dict:
    result = one(conn, 'SELECT * FROM accounts WHERE id=?', (account,))
    if securities is not None and (result['kind'] == 'securities') != securities:
        raise TradeError('account type is not eligible for this operation')
    if active and result['status'] != 'active':
        raise Conflict('account is closed')
    if at and result['effective_at'] and at < result['effective_at']:
        raise Conflict('fact precedes account effective time')
    return result


def verified_trade_signal(conn: sqlite3.Connection, value: dict) -> dict | None:
    """Resolve an explicit signal against the actual account, instrument, time and scale.

    Bad/missing evidence never discards a broker execution. The recorded ID is an
    audit reference, not a permission to label a real lot as strategy-verified.
    """
    if not value.get('signal_id') or value.get('side') != 'buy':
        return None
    if value.get('strategy_id') == 'NDX_AUX_T5':
        return verified_ndx_aux_signal(conn, value)
    signal = conn.execute("""SELECT s.signal_id,s.strategy_version,s.config_version,s.input_version
        FROM strategy_signals s JOIN instruments i ON i.code=s.instrument_code
        WHERE s.signal_id=? AND s.account_id=? AND i.id=?
          AND i.kind='etf' AND s.status='SIGNAL' AND s.source_verified=1 AND s.is_proxy=0
          AND s.price_basis='etf_unadjusted' AND s.price_unit='元'
          AND s.signal_date=? AND s.created_at<=? AND s.config_version LIKE 'cfg:%'
          AND CAST(round(s.atr14*10000) AS INTEGER)=?""",
        (value['signal_id'],value['account_id'],value['instrument_id'],value.get('signal_date'),
         value['executed_at'],value.get('frozen_atr'))).fetchone()
    if signal is None: return None
    from backend.domain.strategy.calendars import market_clock
    expected = market_clock('CN').next_trading_day(value['signal_date'])
    if expected is None or value['trading_day'] != expected:
        return None
    # Ambiguous repeated consumption retains real executions but withdraws proof.
    count = conn.execute("""SELECT count(*) FROM trade_executions WHERE account_id=?
        AND side='buy' AND status='effective' AND json_extract(payload,'$.signal_id')=?""",
        (value['account_id'], value['signal_id'])).fetchone()[0]
    if count != 1:
        return None
    return dict(zip(('signal_id','strategy_version','config_version','input_version'),signal))

def verified_ndx_aux_signal(conn: sqlite3.Connection, value: dict) -> dict | None:
    """T5 evidence is distinct from ATR breakout/stop evidence. Broker facts survive
    missing proof. Distinct broker fill IDs can identify partial fills of one advice.
    """
    if (value.get('strategy_id') != 'NDX_AUX_T5' or value.get('side') != 'buy'
            or not value.get('signal_id') or value.get('strategy_deviation')
            or value.get('frozen_atr') is not None):
        return None
    from backend.domain.strategy.calendars import market_clock
    role = {'main': 'ndx_aux_initial', 'sub': 'ndx_aux_add'}.get(value.get('lot_role'))
    signal = conn.execute("""SELECT s.signal_id,s.strategy_version,s.config_version,s.input_version,s.reason_codes,s.created_at
        FROM strategy_signals s JOIN instruments i ON i.code=s.instrument_code
        WHERE s.signal_id=? AND s.account_id=? AND i.id=? AND i.code='159941' AND i.kind='etf'
        AND s.index_id='NDX' AND s.status='NDX_AUX_ADVICE' AND s.source_verified=1 AND s.is_proxy=0
        AND s.price_basis='etf_unadjusted' AND s.price_unit='元' AND s.signal_date=?
        AND s.created_at<=? AND s.config_version LIKE 'cfg:%'""",
        (value['signal_id'],value['account_id'],value['instrument_id'],value.get('signal_date'),value['executed_at'])).fetchone()
    if not role or not signal or json.loads(signal[4]) != [role]:
        return None
    if market_clock('CN').next_trading_day(value['signal_date']) != value['trading_day']:
        return None
    fills = rows(conn,"""SELECT payload,external_id FROM trade_executions WHERE account_id=?
        AND side='buy' AND status='effective' AND json_extract(payload,'$.signal_id')=?""",
        (value['account_id'],value['signal_id']))
    if not fills or (len(fills)>1 and (any(not f['external_id'] for f in fills)
                        or len({f['external_id'] for f in fills})!=len(fills))):
        return None
    for fill in fills:
        fact=json.loads(fill['payload'])
        if any(fact.get(k)!=value.get(k) for k in ('account_id','instrument_id','strategy_id','lot_role','signal_date','trading_day','frozen_atr')) or fact.get('strategy_deviation') or fact['executed_at']<signal[5]:
            return None
    return {**dict(zip(('signal_id','strategy_version','config_version','input_version'),signal[:4])), 'kind':'ndx_aux_advice_link'}



def post_cash(conn: sqlite3.Connection, *, account: str, kind: str, amount: int,
              at: str, note: str, request: str | None = None, transfer: str | None = None) -> dict:
    require_account(conn, account, securities=True, active=True, at=at)
    if kind == 'opening' and conn.execute("SELECT 1 FROM effective_cash_movements WHERE account_id=? AND (kind='opening' OR occurred_at<=?)", (account, at)).fetchone():
        raise Conflict('opening cash must be unique and precede all cash movements')
    if kind != 'opening' and conn.execute("SELECT 1 FROM effective_cash_movements WHERE account_id=? AND kind='opening' AND occurred_at>=?", (account, at)).fetchone():
        raise Conflict('cash fact conflicts with opening balance boundary')
    id_ = uid()
    conn.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?,?,?)',
                 (id_, account, None, transfer, amount, kind, at, None, request, note))
    return one(conn, 'SELECT * FROM cash_movements WHERE id=?', (id_,))


def cash_balance(conn: sqlite3.Connection, account: str, cutoff: str) -> tuple[int | None, str, list[str]]:
    movements = rows(conn, 'SELECT m.* FROM effective_cash_movements m JOIN cash_movements raw ON raw.id=m.id WHERE m.account_id=? AND m.occurred_at<=? ORDER BY m.occurred_at,raw.rowid', (account,cutoff))
    openings = [r for r in movements if r['kind']=='opening']
    if len(openings)!=1:
        return None,'derived_unverified',['missing_or_ambiguous_cash_baseline']
    baseline = openings[0]
    reasons = []
    if any(r['kind']!='opening' and r['occurred_at']<=baseline['occurred_at'] for r in movements):
        reasons.append('cash_fact_conflicts_with_baseline')
    after = [r for r in movements if r['occurred_at']>baseline['occurred_at']]
    running = baseline['amount_fen']
    for r in after:
        running += r['amount_fen']
        if running<0 and 'historical_cash_shortfall' not in reasons:
            reasons.append('historical_cash_shortfall')
    if conn.execute("SELECT 1 FROM trade_executions WHERE account_id=? AND status='pending' AND executed_at<=?", (account,cutoff)).fetchone():
        reasons.append('pending_trade')
    return baseline['amount_fen']+sum(r['amount_fen'] for r in after),'derived_unverified' if reasons else 'valid',reasons



class TradeService:
    def __init__(self, db: Database, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.db = db
        self.clock = clock

    def require_actual(self, at: datetime) -> None:
        if at > self.clock():
            raise TradeError("future actual facts are not allowed")

    def create_account(self, data: AccountInput) -> dict:
        data = AccountInput.model_validate(data)
        if data.effective_at is not None:
            self.require_actual(data.effective_at)
        value = data.model_dump(mode='json', exclude={'request_id'})
        value['effective_at'] = utc(data.effective_at) if data.effective_at else None
        # Preserve the legacy request fingerprint for ordinary securities accounts.
        if data.kind == 'securities' and data.included and data.effective_at is None:
            value = {'name': data.name, 'currency': data.currency}
        payload = dump({'account': value})
        with self.db.transaction() as conn:
            existing = prior(conn, data.request_id, payload)
            if existing is not None:
                return existing
            if data.effective_at is None:
                raise TradeError('new account requires confirmed effective time')
            id_ = uid()
            conn.execute("INSERT INTO accounts VALUES (?,?,?,'CNY',?,'active',?,?,?)", (id_, data.name, data.kind,
                         'ledger' if data.kind=='securities' else 'manual_snapshot',
                         'liability' if data.kind in ('credit_card','other_liability') else 'asset',
                         int(data.included), utc(data.effective_at) if data.effective_at else None))
            audit(conn,'account',id_,'created',data.request_id,value)
            result = one(conn, 'SELECT * FROM accounts WHERE id=?', (id_,))
            conn.execute('UPDATE ledger_state SET ledger_revision=ledger_revision+1 WHERE id=1')
            remember(conn, data.request_id, payload, result)
            return result

    def create_instrument(self, data: InstrumentInput) -> dict:
        data = InstrumentInput.model_validate(data)
        payload = dump({'instrument': data.model_dump(exclude={'request_id'})})
        with self.db.transaction() as conn:
            existing = prior(conn, data.request_id, payload)
            if existing is not None:
                return existing
            id_ = uid()
            conn.execute("INSERT INTO instruments VALUES (?,?,?,'CNY',4,?)", (id_, data.code, data.market, data.kind))
            result = one(conn, 'SELECT * FROM instruments WHERE id=?', (id_,))
            conn.execute('UPDATE ledger_state SET ledger_revision=ledger_revision+1 WHERE id=1')
            remember(conn, data.request_id, payload, result)
            return result

    def submit(self, data: TradeInput) -> dict:
        data = TradeInput.model_validate(data)
        self.require_actual(data.executed_at)
        value = canonical(data)
        payload = dump({'trade': value})
        with self.db.transaction() as conn:
            existing = prior(conn, data.request_id, payload)
            if existing is not None:
                return existing
            if data.external_id:
                found = rows(conn, "SELECT * FROM trade_executions WHERE account_id=? AND (external_id=? OR external_id_owner=(SELECT id FROM trade_executions WHERE account_id=? AND external_id=?)) ORDER BY CASE status WHEN 'reversed' THEN 1 ELSE 0 END", (data.account_id, data.external_id, data.account_id, data.external_id))
                if found:
                    # Original broker IDs remain reserved even after correction.
                    match = next((r for r in found if r['payload'] == dump(value)), None)
                    if match is None:
                        raise Conflict('broker execution ID already used')
                    result = self._trade(conn, match['id'])
                    remember(conn, data.request_id, payload, result)
                    return result
            id_ = self._insert(conn, value, data.request_id)
            bump(conn, value['trading_day'])
            result = self._trade(conn, id_)
            remember(conn, data.request_id, payload, result)
            return result

    @staticmethod
    def _trade(conn: sqlite3.Connection, id_: str) -> dict:
        trade = one(conn, 'SELECT * FROM trade_executions WHERE id=?', (id_,))
        v = json.loads(trade.pop('payload'))
        trade['external_id'] = v['external_id']
        trade['requested_allocations'] = v['allocations']
        trade['strategy_deviation'] = bool(v['strategy_deviation'])
        trade['signal_id'] = v.get('signal_id')
        trade['strategy_exit_action'] = v.get('strategy_exit_action')
        trade['strategy_evidence'] = verified_trade_signal(conn, v) if trade['status'] == 'effective' else None
        trade['strategy_evidence_missing'] = bool(trade['side'] == 'buy' and trade['strategy_id'] and trade['strategy_evidence'] is None)
        return trade

    def _plan(self, conn: sqlite3.Connection, v: dict, *, replay: bool = False) -> tuple[str | None, list[tuple[dict, int]]]:
        if v['fees_fen'] is None:
            return 'unknown_fees', []
        reason = self._boundary(conn, v)
        if reason:
            return reason, []
        market = one(conn, 'SELECT market FROM instruments WHERE id=?', (v['instrument_id'],))['market']
        reason = ordinary_execution_time(datetime.fromisoformat(v['executed_at']), market)
        if reason:
            return reason, []
        if not replay and conn.execute("SELECT 1 FROM trade_executions WHERE account_id=? AND instrument_id=? AND status='effective' AND executed_at>? LIMIT 1", (v['account_id'], v['instrument_id'], v['executed_at'])).fetchone():
            return 'historical_replay_required', []
        if v['side'] == 'buy':
            return None, []
        lots = rows(conn, """SELECT l.*,COALESCE(t.executed_at,o.occurred_at) AS executed_at FROM trade_lots l
            LEFT JOIN trade_executions t ON t.id=l.open_execution_id LEFT JOIN opening_positions o ON o.id=l.opening_id
            WHERE COALESCE(t.account_id,o.account_id)=? AND COALESCE(t.instrument_id,o.instrument_id)=?
            AND (t.status='effective' OR (o.id IS NOT NULL AND o.superseded_by IS NULL AND o.voided_at IS NULL)) AND l.status='open'
            AND COALESCE(t.executed_at,o.occurred_at)<=? ORDER BY COALESCE(t.executed_at,o.occurred_at),COALESCE(t.rowid,0)""", (v['account_id'], v['instrument_id'], v['executed_at']))
        eligible = {lot['id']: lot for lot in lots if lot['lot_role'] == v['lot_role'] and lot['strategy_id'] == v['strategy_id']}
        specified = v['allocations']
        if specified is not None:
            if sum(specified.values()) != v['quantity'] or any(k not in eligible or q > eligible[k]['remaining_qty'] for k, q in specified.items()):
                return 'invalid_or_insufficient_specified_lots', []
            return None, [(eligible[k], q) for k, q in specified.items()]
        if v['lot_role'] != 'ordinary':
            return 'strategy_requires_specified_lots', []
        need = v['quantity']
        plan = []
        for lot in eligible.values():
            take = min(need, lot['remaining_qty'])
            if take:
                plan.append((lot, take))
                need -= take
        return ('insufficient_lots', []) if need else (None, plan)

    def _insert(self, conn: sqlite3.Connection, v: dict, request: str, correction_of: str | None = None, *, defer: bool = False) -> str:
        require_account(conn, v['account_id'], securities=True, active=True, at=v['executed_at'])
        one(conn, 'SELECT id FROM instruments WHERE id=?', (v['instrument_id'],))
        reason, plan = ('replay_requested', []) if defer else self._plan(conn, v)
        id_ = uid()
        amount = money(v['quantity'], v['price'])
        owner = conn.execute('SELECT id FROM trade_executions WHERE account_id=? AND external_id=?', (v['account_id'], v['external_id'])).fetchone() if v['external_id'] is not None else None
        external_id = None if owner else v['external_id']
        conn.execute('''INSERT INTO trade_executions(id,request_id,account_id,instrument_id,side,quantity,price,fees_fen,amount_fen,executed_at,trading_day,external_id,external_id_owner,status,correction_of,note,strategy_id,lot_role,signal_date,frozen_atr,pending_reason,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (id_, request, v['account_id'], v['instrument_id'], v['side'], v['quantity'], v['price'], v['fees_fen'], amount, v['executed_at'], v['trading_day'], external_id, owner[0] if owner else None, 'pending' if reason else 'effective', correction_of, v['note'], v['strategy_id'], v['lot_role'], v['signal_date'], v['frozen_atr'], reason, dump(v)))
        if not reason:
            self._post(conn, id_, v, plan)
        audit(conn, 'trade', id_, 'pending' if reason else 'posted', request,
              {'reason': reason, 'strategy_deviation': v['strategy_deviation'], 'correction_of': correction_of})
        return id_

    @staticmethod
    def _post(conn: sqlite3.Connection, id_: str, v: dict, plan: list[tuple[dict, int]], *, cash: bool = True) -> None:
        amount = money(v['quantity'], v['price'])
        fees = v['fees_fen']
        if v['side'] == 'buy':
            conn.execute('''INSERT INTO trade_lots VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)
                         ON CONFLICT(open_execution_id) DO UPDATE SET remaining_qty=excluded.remaining_qty,
                         remaining_cost_fen=excluded.remaining_cost_fen,remaining_fees_fen=excluded.remaining_fees_fen,status='open' ''',
                         (uid(), id_, v['strategy_id'], v['lot_role'], v['signal_date'], v['frozen_atr'], v['quantity'], v['quantity'], amount, fees, 'open'))
        else:
            for lot, q in plan:
                remaining = lot['remaining_qty'] - q
                cost = share(lot['remaining_cost_fen'], q, lot['remaining_qty'])
                fee = share(lot['remaining_fees_fen'], q, lot['remaining_qty'])
                conn.execute('INSERT INTO lot_allocations VALUES (?,?,?,?,?,?,?)', (uid(), id_, lot['id'], q, cost, fee, 'specified' if v['allocations'] else 'fifo'))
                conn.execute('UPDATE trade_lots SET remaining_qty=?,remaining_cost_fen=remaining_cost_fen-?,remaining_fees_fen=remaining_fees_fen-?,status=? WHERE id=?', (remaining, cost, fee, 'open' if remaining else 'closed', lot['id']))
        if cash:
            TradeService._post_cash(conn, id_, v)

    @staticmethod
    def _post_cash(conn: sqlite3.Connection, id_: str, v: dict) -> None:
        amount = money(v['quantity'], v['price'])
        cash = (-amount if v['side'] == 'buy' else amount) - v['fees_fen']
        conn.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?,?,?)', (uid(), v['account_id'], id_, None, cash, 'trade', v['executed_at'], None, None, v['note']))

    @staticmethod
    def _boundary(conn: sqlite3.Connection, v: dict) -> str | None:
        cash = conn.execute("SELECT occurred_at FROM effective_cash_movements WHERE account_id=? AND kind='opening'", (v['account_id'],)).fetchone()
        if cash and v['executed_at'] <= cash[0]:
            return 'before_or_at_cash_baseline'
        opening = conn.execute('SELECT occurred_at FROM opening_positions WHERE account_id=? AND instrument_id=? AND superseded_by IS NULL AND voided_at IS NULL', (v['account_id'], v['instrument_id'])).fetchone()
        if opening and v['executed_at'] <= opening[0]:
            return 'before_or_at_position_baseline'
        if conn.execute("SELECT 1 FROM company_events WHERE account_id=? AND instrument_id=? AND kind!='dividend' AND voided_at IS NULL AND (applied_at IS NULL OR occurred_at>=?)", (v['account_id'],v['instrument_id'],v['executed_at'])).fetchone():
            return 'unresolved_company_action'
        return None

    def _replay(self, conn: sqlite3.Connection, account: str, instrument: str, request: str) -> None:
        if conn.execute("SELECT 1 FROM company_events WHERE account_id=? AND instrument_id=? AND kind!='dividend' AND voided_at IS NULL",(account,instrument)).fetchone():
            raise Conflict('unresolved company action requires manual reconciliation before replay')
        facts = rows(conn, "SELECT * FROM trade_executions WHERE account_id=? AND instrument_id=? AND status='effective' ORDER BY executed_at,rowid", (account,instrument))
        allocations = rows(conn, "SELECT a.* FROM lot_allocations a JOIN trade_executions t ON t.id=a.sell_execution_id WHERE t.account_id=? AND t.instrument_id=? AND t.status='effective' ORDER BY a.rowid", (account,instrument))
        if allocations:
            audit(conn,'trade_replay',instrument,'allocations_rebuilt',request,{'account_id':account,'previous_allocations':allocations})
        conn.execute("DELETE FROM lot_allocations WHERE sell_execution_id IN (SELECT id FROM trade_executions WHERE account_id=? AND instrument_id=? AND status='effective')", (account,instrument))
        conn.execute("UPDATE trade_lots SET remaining_qty=0,remaining_cost_fen=0,remaining_fees_fen=0,status='reversed' WHERE open_execution_id IN (SELECT id FROM trade_executions WHERE account_id=? AND instrument_id=?)", (account,instrument))
        for opening in rows(conn,'SELECT * FROM opening_positions WHERE account_id=? AND instrument_id=? AND superseded_by IS NULL AND voided_at IS NULL',(account,instrument)):
            conn.execute("UPDATE trade_lots SET remaining_qty=?,remaining_cost_fen=?,remaining_fees_fen=?,status='open' WHERE opening_id=?",(opening['quantity'],opening['cost_fen'],opening['fees_fen'],opening['id']))
        for fact in facts:
            v = json.loads(fact['payload'])
            self.require_actual(datetime.fromisoformat(v['executed_at']))
            reason, plan = self._plan(conn, v, replay=True)
            if reason:
                raise Conflict(f'historical replay conflict: {fact["id"]}: {reason}')
            self._post(conn, fact['id'], v, plan, cash=False)

    def _activate(self, conn: sqlite3.Connection, id_: str, v: dict, request: str) -> None:
        self.require_actual(datetime.fromisoformat(v['executed_at']))
        require_account(conn,v['account_id'],securities=True,active=True,at=v['executed_at'])
        conn.execute("UPDATE trade_executions SET status='effective',pending_reason=NULL WHERE id=?", (id_,))
        self._replay(conn,v['account_id'],v['instrument_id'],request)
        self._post_cash(conn,id_,v)


    def confirm_pending(self, id_: str, request: str) -> dict:
        request = request.strip()
        if not 1 <= len(request) <= 128:
            raise TradeError('invalid request ID')
        payload = dump({'confirm': id_})
        with self.db.transaction() as conn:
            existing = prior(conn, request, payload)
            if existing is not None:
                return existing
            old = one(conn, 'SELECT * FROM trade_executions WHERE id=?', (id_,))
            if old['status'] != 'pending':
                raise Conflict('only pending facts can be confirmed')
            v = json.loads(old['payload'])
            self._activate(conn,id_,v,request)
            audit(conn, 'trade', id_, 'confirmed', request, {})
            bump(conn, v['trading_day'])
            result = self._trade(conn, id_)
            remember(conn, request, payload, result)
            return result

    def correct(self, id_: str, data: TradeInput) -> dict:
        data = TradeInput.model_validate(data)
        self.require_actual(data.executed_at)
        v = canonical(data)
        payload = dump({'correct': id_, 'trade': v})
        with self.db.transaction() as conn:
            existing = prior(conn, data.request_id, payload)
            if existing is not None:
                return existing
            old = one(conn, 'SELECT * FROM trade_executions WHERE id=?', (id_,))
            if old['status'] == 'reversed' or (old['account_id'], old['instrument_id']) != (v['account_id'], v['instrument_id']):
                raise Conflict('correction requires same account and instrument and unreversed fact')
            if not v['note']:
                raise TradeError('correction requires reason in note')
            if v['external_id']:
                ancestors = {id_}
                parent = old['correction_of']
                while parent is not None:
                    ancestors.add(parent)
                    parent = one(conn, 'SELECT correction_of FROM trade_executions WHERE id=?', (parent,))['correction_of']
                matches = rows(conn, 'SELECT id FROM trade_executions WHERE account_id=? AND external_id=?', (v['account_id'], v['external_id']))
                if any(r['id'] not in ancestors for r in matches):
                    raise Conflict('broker execution ID already used by another fact')
            self._reverse(conn,old,v['note'])
            new = self._insert(conn, v, data.request_id, correction_of=id_, defer=True)
            self._activate(conn,new,v,data.request_id)
            audit(conn, 'trade', id_, 'reversed', data.request_id, {'replacement': new, 'reason': v['note'], 'earliest_affected_day': min(old['trading_day'], v['trading_day'])})
            bump(conn, min(old['trading_day'], v['trading_day']))
            result = self._trade(conn, new)
            remember(conn, data.request_id, payload, result)
            return result

    @staticmethod
    def _reverse(conn: sqlite3.Connection, old: dict, reason: str) -> None:
        if old['status'] == 'effective':
            movement = one(conn, "SELECT * FROM cash_movements WHERE execution_id=? AND kind='trade'", (old['id'],))
            conn.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?,?,?)', (uid(), old['account_id'], old['id'], None, -movement['amount_fen'], 'reversal', movement['occurred_at'], movement['id'], None, reason))
        conn.execute("UPDATE trade_executions SET status='reversed' WHERE id=?", (old['id'],))

    def void(self, id_: str, request: str, reason: str) -> dict:
        if not 1 <= len(request.strip()) <= 128 or not 1 <= len(reason.strip()) <= 2000:
            raise TradeError('void requires request ID and reason')
        payload = dump({'void':id_,'reason':reason})
        with self.db.transaction() as conn:
            existing = prior(conn,request,payload)
            if existing is not None:
                return existing
            old = one(conn,'SELECT * FROM trade_executions WHERE id=?',(id_,))
            if old['status']=='reversed':
                raise Conflict('fact already reversed')
            self._reverse(conn,old,reason)
            if old['status']=='effective':
                self._replay(conn,old['account_id'],old['instrument_id'],request)
            audit(conn,'trade',id_,'reversed',request,{'reason':reason,'void':True})
            bump(conn,old['trading_day'])
            result=self._trade(conn,id_)
            remember(conn,request,payload,result)
            return result

    def cash(self, data: CashInput) -> dict:
        data = CashInput.model_validate(data)
        self.require_actual(data.occurred_at)
        value = data.model_dump(mode='json', exclude={'request_id'})
        value['occurred_at'] = utc(data.occurred_at)
        payload = dump({'cash': value})
        with self.db.transaction() as conn:
            existing = prior(conn, data.request_id, payload)
            if existing is not None:
                return existing
            if data.transfer_id is not None:
                raise TradeError('internal transfers must use the paired finance transfer operation')
            result = post_cash(conn, account=data.account_id, kind=data.kind, amount=data.amount_fen,
                               at=value['occurred_at'], note=data.note, request=data.request_id)
            audit(conn, 'cash', result['id'], data.kind, data.request_id, value)
            bump(conn, data.occurred_at.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat())
            remember(conn, data.request_id, payload, result)
            return result

    def list_records(self, name: str, limit: int | None = None, offset: int = 0, expected_revision: int | None = None, account_id: str | None = None, instrument_id: str | None = None, from_at: datetime | None = None, to_at: datetime | None = None) -> dict:
        queries = {'accounts': 'SELECT * FROM accounts ORDER BY id', 'instruments': 'SELECT * FROM instruments ORDER BY market,code', 'trades': 'SELECT * FROM trade_executions ORDER BY executed_at,rowid', 'pending': "SELECT * FROM trade_executions WHERE status='pending' ORDER BY executed_at,rowid", 'lots': ('SELECT l.*, COALESCE(t.account_id,o.account_id) AS account_id, COALESCE(t.instrument_id,o.instrument_id) AS instrument_id FROM trade_lots l LEFT JOIN trade_executions t ON t.id=l.open_execution_id LEFT JOIN opening_positions o ON o.id=l.opening_id ORDER BY l.rowid'), 'allocations': 'SELECT a.*,t.status AS execution_status FROM lot_allocations a JOIN trade_executions t ON t.id=a.sell_execution_id ORDER BY a.rowid', 'cash': "SELECT * FROM cash_movements ORDER BY occurred_at,CASE kind WHEN 'opening' THEN 0 ELSE 1 END,rowid"}
        with self.db.read() as conn:
            revision = one(conn, 'SELECT ledger_revision,earliest_affected_day FROM ledger_state')
            if expected_revision is not None and revision['ledger_revision'] != expected_revision:
                raise Conflict('ledger changed; restart paginated query')
            query = queries[name]
            args = []
            if any(v is not None for v in (account_id,instrument_id,from_at,to_at)):
                if name!='trades': raise TradeError('filters supported only for trades')
                if any(v is not None and (v.tzinfo is None or v.utcoffset() is None) for v in (from_at,to_at)):
                    raise TradeError('history cutoff requires timezone')
                if from_at and to_at and from_at>=to_at: raise TradeError('invalid history interval')
                conditions=[]
                for column,op,value in [('account_id','=',account_id),('instrument_id','=',instrument_id),('executed_at','>=',utc(from_at) if from_at else None),('executed_at','<',utc(to_at) if to_at else None)]:
                    if value is not None: conditions.append(column+op+'?'); args.append(value)
                query='SELECT * FROM trade_executions WHERE '+' AND '.join(conditions)+' ORDER BY executed_at,rowid'
            if limit is None:
                records = rows(conn, query, args)
            else:
                records = rows(conn, query + ' LIMIT ? OFFSET ?', (*args,limit,offset))
            for record in records:
                if 'payload' in record:
                    v = json.loads(record.pop('payload'))
                    record['external_id'] = v['external_id']
                    record['requested_allocations'] = v['allocations']
                    record['strategy_deviation'] = bool(v['strategy_deviation'])
                    record['signal_id'] = v.get('signal_id')
                    record['strategy_exit_action'] = v.get('strategy_exit_action')
                    record['strategy_evidence'] = verified_trade_signal(conn, v) if record['status'] == 'effective' else None
                    record['strategy_evidence_missing'] = bool(record['side'] == 'buy' and record['strategy_id'] and record['strategy_evidence'] is None)
            incomplete = rows(conn, "SELECT DISTINCT account_id,instrument_id FROM trade_executions WHERE status='pending' ORDER BY account_id,instrument_id")
            result = {'items': records, **revision, 'data_incomplete': bool(incomplete), 'incomplete_scopes': incomplete}
            if limit is not None:
                result['total'] = conn.execute('SELECT count(*) FROM (' + query + ')',args).fetchone()[0]
                result['limit'] = limit
                result['offset'] = offset
            if name == 'cash':
                balances = []
                cutoff=utc(self.clock())
                for account in rows(conn, "SELECT id FROM accounts WHERE kind='securities'"):
                    balance, quality, _ = cash_balance(conn,account['id'],cutoff)
                    balances.append({'account_id':account['id'],'balance_fen':balance,'quality':quality})
                result['balances'] = balances
            return result

    def get(self, id_: str, include_audit: bool=False) -> dict:
        with self.db.read() as conn:
            result=self._trade(conn, id_)
            if include_audit: result['audit_events']=rows(conn,"SELECT id,event,request_id,at,actor,details FROM audit_events WHERE entity='trade' AND entity_id=? ORDER BY at,rowid",(id_,))
            return result
