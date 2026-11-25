"""Personal finance facts and snapshot-consistent net worth projections."""
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
import sqlite3
from decimal import Decimal
from typing import Literal
from pydantic import Field, field_validator, model_validator

from backend.application.trading.service import (
    Input, TradeInput, TradeService, TradeError, Conflict, MAX_FEN,
    audit, bump, dump, one, prior, remember, rows, uid, utc, require_account, post_cash,
)
from backend.application.portfolio import PortfolioService, SHANGHAI, day_of, require_time
from backend.application.market_calendar import CalendarUnavailable, market_window
from backend.infrastructure.database import Database


class RequestInput(Input):
    request_id: str = Field(min_length=1, max_length=128)


class FactInput(RequestInput):
    occurred_at: datetime
    note: str = Field(min_length=1, max_length=2000)

    @field_validator('occurred_at', mode='before')
    @classmethod
    def timestamp(cls, value):
        return TradeInput.timestamp_input(value)

    @model_validator(mode='after')
    def validate_time(self):
        require_time(self.occurred_at)
        return self


class BalanceInput(FactInput):
    account_id: str
    after_fen: int = Field(strict=True, ge=0, le=MAX_FEN)
    expected_balance_id: str | None  # explicitly null for first confirmed snapshot


class BillInput(FactInput):
    account_id: str
    kind: Literal['income', 'expense', 'fee', 'refund']
    related_transaction_id: str | None = None
    category_id: str
    amount_fen: int = Field(strict=True, gt=0, le=MAX_FEN)


class CategoryInput(RequestInput):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal['income', 'expense', 'fee']


class AccountUpdate(RequestInput):
    name: str = Field(min_length=1, max_length=120)
    status: Literal['active', 'closed']
    expected_ledger_revision: int = Field(strict=True, ge=0)


class LiquidityInput(FactInput):
    # Explicit three-state marker: never infer 'liquid' from the absence of evidence.
    status: Literal['liquid', 'restricted', 'unknown']
    expected_ledger_revision: int = Field(strict=True, ge=0)


class TransferInput(FactInput):
    from_account_id: str
    to_account_id: str
    amount_fen: int = Field(strict=True, gt=0, le=MAX_FEN)
    from_included: bool | None = Field(default=None, strict=True)
    to_included: bool | None = Field(default=None, strict=True)
    from_balance_id: str | None = None
    to_balance_id: str | None = None

    @model_validator(mode='after')
    def different_accounts(self):
        if self.from_account_id == self.to_account_id:
            raise ValueError('transfer endpoints must differ')
        return self


class ReconciledBalance(Input):
    account_id: str
    expected_balance_id: str
    confirmed_fen: int = Field(strict=True, ge=0, le=MAX_FEN)


class TransferReconciliation(FactInput):
    # occurred_at is the confirmed balance observation, not a replacement transfer date.
    corrected_amount_fen: int | None = Field(default=None, strict=True, gt=0, le=MAX_FEN)
    balances: list[ReconciledBalance] = Field(min_length=1, max_length=2)
    confirmed: Literal[True]


class AccountScope(FactInput):
    included: bool = Field(strict=True)
    kind: Literal['scope','mistaken_account'] = 'scope'
    expected_ledger_revision: int = Field(strict=True, ge=0)


class AccountEffective(FactInput):
    expected_ledger_revision: int = Field(strict=True, ge=0)


class CashCorrection(FactInput):
    amount_fen: int = Field(strict=True, ge=-MAX_FEN, le=MAX_FEN)


class VoidInput(RequestInput):
    reason: str = Field(min_length=1, max_length=2000)


def value_of(data: RequestInput) -> dict:
    value = data.model_dump(mode='json', exclude={'request_id'})
    if isinstance(data, BillInput) and data.related_transaction_id is None:
        value.pop('related_transaction_id',None) # preserve previous request fingerprints
    if isinstance(data, FactInput):
        value['occurred_at'] = utc(data.occurred_at)
    return value


def month_start(day: date) -> date:
    if day.day >= 23:
        return day.replace(day=23)
    return date(day.year-1, 12, 23) if day.month == 1 else date(day.year, day.month-1, 23)


def month_bounds(start: date) -> tuple[str, str]:
    if start.day != 23 or not 1900 <= start.year <= 9997:
        raise TradeError('finance month identifier must be a supported date on the 23rd')
    end = date(start.year+1, 1, 23) if start.month == 12 else date(start.year, start.month+1, 23)
    return tuple(utc(datetime.combine(d, time.min, SHANGHAI)) for d in (start, end))


def balance_at(conn: sqlite3.Connection, account: str, cutoff: str | None = None) -> dict | None:
    query = "SELECT * FROM balance_adjustments WHERE account_id=? AND status='effective'"
    args = (account,)
    if cutoff is not None:
        query += ' AND occurred_at<=?'
        args += (cutoff,)
    result = rows(conn, query+' ORDER BY occurred_at DESC,rowid DESC LIMIT 1', args)
    return result[0] if result else None


def first_account_fact(conn: sqlite3.Connection, account: str) -> str | None:
    # An observed fact establishes existence no later than its timestamp, not the creation date.
    candidates = []
    for table, column, condition in (
        ('trade_executions','executed_at',"status='effective'"),
        ('opening_positions','occurred_at','1=1'),
        ('effective_cash_movements','occurred_at','1=1'),
        ('company_events','occurred_at','1=1'),
        ('balance_adjustments','occurred_at',"status='effective'"),
        ('finance_transactions','occurred_at',"status='effective'"),
    ):
        value = conn.execute(f'SELECT min({column}) FROM {table} WHERE account_id=? AND {condition}',(account,)).fetchone()[0]
        if value is not None: candidates.append(value)
    value = conn.execute("SELECT min(occurred_at) FROM finance_transfers WHERE (from_account_id=? OR to_account_id=?) AND status='effective'",(account,account)).fetchone()[0]
    if value is not None: candidates.append(value)
    return min(candidates) if candidates else None


def scope_at(conn: sqlite3.Connection, account: dict, cutoff: str) -> bool:
    event = conn.execute('SELECT included FROM account_scope_events WHERE account_id=? AND occurred_at<=? ORDER BY occurred_at DESC,rowid DESC LIMIT 1',(account['id'],cutoff)).fetchone()
    return bool(event[0] if event else account['included'])


LIQUIDITY_DISPLAY = {
    'liquid': '有效流动性（可计入流动资产）',
    'restricted': '受限资产（计入总资产，不计入流动资产）',
    'unknown': '待确认（不计入流动资产）',
}


def liquidity_status_at(conn: sqlite3.Connection, account: dict, cutoff: str) -> tuple[str, str | None]:
    """Classification evidence effective at the cutoff, never a retroactive guess.

    A migrated account without any recorded classification stays 'unknown' for every
    date; the absence of evidence must not be reported as an unrestricted asset.
    """
    event = conn.execute(
        'SELECT liquidity_status,effective_at FROM account_liquidity_events '
        'WHERE account_id=? AND effective_at<=? ORDER BY effective_at DESC,rowid DESC LIMIT 1',
        (account['id'], cutoff)).fetchone()
    return (event[0], event[1]) if event else ('unknown', None)


def set_balance(conn: sqlite3.Connection, account: str, amount: int, at: str, reason: str,
                expected: str | None, request: str, transfer: str | None = None) -> dict:
    require_account(conn, account, securities=False, active=True, at=at)
    latest = balance_at(conn, account)
    if (latest['id'] if latest else None) != expected:
        raise Conflict('balance changed; reload before adjusting')
    if latest and at < latest['occurred_at']:
        raise Conflict('historical balance adjustment has later dependent snapshots')
    if not 0 <= amount <= MAX_FEN:
        raise TradeError('balance outside supported nonnegative range')
    id_ = uid()
    conn.execute("INSERT INTO balance_adjustments VALUES (?,?,?,?,?,?,?,?,'effective')",
                 (id_,account,latest['after_fen'] if latest else None,amount,at,reason,expected,transfer))
    result = one(conn,'SELECT * FROM balance_adjustments WHERE id=?',(id_,))
    audit(conn,'balance',id_,'adjusted',request,result)
    return result


def reverse_cash(conn: sqlite3.Connection, original: dict, note: str, request: str) -> str:
    if original['kind'] not in ('opening','deposit','withdrawal','adjustment','dividend') or original['execution_id'] is not None:
        raise Conflict('only non-execution cash facts may be corrected here')
    if not conn.execute('SELECT 1 FROM effective_cash_movements WHERE id=?',(original['id'],)).fetchone():
        raise Conflict('cash fact already reversed')
    id_ = uid()
    conn.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?,?,?)',
                 (id_,original['account_id'],None,original['transfer_id'],-original['amount_fen'],
                  'reversal',original['occurred_at'],original['id'],None,note))
    audit(conn,'cash',original['id'],'reversed',request,{'reversal_id':id_,'reason':note})
    return id_


class FinanceService:
    def __init__(self, db: Database, clock: Callable[[],datetime] = lambda: datetime.now(UTC)) -> None:
        self.db = db
        self.clock = clock

    def _write(self, operation: str, data: RequestInput, fn, *, target: str | None = None) -> dict:
        value = value_of(data)
        if isinstance(data, FactInput):
            TradeService(self.db,self.clock).require_actual(data.occurred_at)
        payload = dump({'finance_operation':operation,'target':target,'value':value})
        with self.db.transaction() as conn:
            existing = prior(conn,data.request_id,payload)
            if existing is not None:
                return existing
            result, affected = fn(conn,value)
            bump(conn,day_of(datetime.fromisoformat(affected)).isoformat())
            remember(conn,data.request_id,payload,result)
            return result

    def update_account(self, account: str, data: AccountUpdate) -> dict:
        data = AccountUpdate.model_validate(data)
        def work(conn,v):
            old = require_account(conn,account)
            if one(conn,'SELECT ledger_revision FROM ledger_state')['ledger_revision'] != data.expected_ledger_revision:
                raise Conflict('ledger changed; reload account')
            conn.execute('UPDATE accounts SET name=?,status=? WHERE id=?',(data.name,data.status,account))
            result=one(conn,'SELECT * FROM accounts WHERE id=?',(account,))
            audit(conn,'account',account,'updated',data.request_id,{'before':old,'after':result})
            return result,old['effective_at'] or utc(self.clock())
        return self._write('account_update',data,work,target=account)

    def set_account_scope(self, account: str, data: AccountScope) -> dict:
        data = AccountScope.model_validate(data)
        def work(conn,v):
            old = require_account(conn,account,at=v['occurred_at'])
            if one(conn,'SELECT ledger_revision FROM ledger_state')['ledger_revision'] != data.expected_ledger_revision:
                raise Conflict('ledger changed; reload account')
            if data.kind == 'mistaken_account':
                # Even reversed or pending facts are evidence this account has been used.
                used = any(conn.execute(f'SELECT 1 FROM {table} WHERE account_id=? LIMIT 1',(account,)).fetchone()
                           for table in ('trade_executions','opening_positions','cash_movements','company_events','balance_adjustments','finance_transactions'))
                if data.included or used or conn.execute('SELECT 1 FROM finance_transfers WHERE from_account_id=? OR to_account_id=?',(account,account)).fetchone():
                    raise Conflict('mistaken account exclusion requires an unused account')
                conn.execute("UPDATE accounts SET status='closed' WHERE id=?",(account,))
            id_=uid()
            conn.execute('INSERT INTO account_scope_events VALUES (?,?,?,?,?,?)',(id_,account,int(data.included),v['occurred_at'],data.note,data.kind))
            result=one(conn,'SELECT * FROM account_scope_events WHERE id=?',(id_,))
            audit(conn,'account',account,'scope_changed',data.request_id,result)
            return result,v['occurred_at']
        return self._write('account_scope',data,work,target=account)

    def confirm_account_effective(self, account: str, data: AccountEffective) -> dict:
        data = AccountEffective.model_validate(data)
        def work(conn,v):
            old=require_account(conn,account)
            if one(conn,'SELECT ledger_revision FROM ledger_state')['ledger_revision'] != data.expected_ledger_revision:
                raise Conflict('ledger changed; reload account')
            if old['effective_at'] is not None:
                raise Conflict('account already has a confirmed effective date')
            first=first_account_fact(conn,account)
            if first is not None and v['occurred_at']>first:
                raise Conflict('effective date follows known account facts')
            conn.execute('UPDATE accounts SET effective_at=? WHERE id=?',(v['occurred_at'],account))
            result=one(conn,'SELECT * FROM accounts WHERE id=?',(account,))
            audit(conn,'account',account,'effective_confirmed',data.request_id,{'before':old,'after':result,'reason':data.note})
            return result,v['occurred_at']
        return self._write('account_effective',data,work,target=account)

    def set_account_liquidity(self, account: str, data: LiquidityInput) -> dict:
        """Confirm, restrict or reset an asset account's liquidity classification.

        Restricted assets remain in total assets and net worth. Liabilities keep the
        approved current-liability basis and cannot carry this marker. The change is
        append-only evidence: a confirmation can never be dated before an already
        recorded classification, and history before the first record stays unknown.
        """
        data = LiquidityInput.model_validate(data)
        def work(conn,v):
            old=require_account(conn,account,at=v['occurred_at'])
            if old['asset_class']!='asset':
                raise TradeError('liquidity classification applies to asset accounts only; '
                                 'liabilities follow the approved current-liability basis')
            if one(conn,'SELECT ledger_revision FROM ledger_state')['ledger_revision'] != data.expected_ledger_revision:
                raise Conflict('ledger changed; reload account')
            if old['effective_at'] is not None and v['occurred_at'] < old['effective_at']:
                raise Conflict('liquidity classification cannot precede the account effective date')
            latest=conn.execute('SELECT max(effective_at) FROM account_liquidity_events WHERE account_id=?',
                                (account,)).fetchone()[0]
            if latest is not None and v['occurred_at'] < latest:
                raise Conflict('liquidity classification history is append-only; '
                               'record later changes on or after the latest effective time')
            previous,_=liquidity_status_at(conn,old,v['occurred_at'])
            event_id=uid()
            conn.execute('INSERT INTO account_liquidity_events VALUES (?,?,?,?,?,?,?)',
                         (event_id,account,previous,data.status,v['occurred_at'],data.note,'changed'))
            conn.execute('UPDATE accounts SET liquidity_status=? WHERE id=?',(data.status,account))
            result=one(conn,'SELECT * FROM accounts WHERE id=?',(account,))
            audit(conn,'account',account,'liquidity_changed',data.request_id,
                  {'before':old,'after':result,'event_id':event_id,'effective_at':v['occurred_at'],'reason':data.note})
            return {**result,'event_id':event_id,'effective_at':v['occurred_at'],
                    'previous_status':previous},v['occurred_at']
        return self._write('account_liquidity',data,work,target=account)

    def balance(self, data: BalanceInput) -> dict:
        data = BalanceInput.model_validate(data)
        return self._write('balance',data,lambda c,v: (set_balance(c,data.account_id,data.after_fen,v['occurred_at'],data.note,data.expected_balance_id,data.request_id),v['occurred_at']))

    def category(self, data: CategoryInput) -> dict:
        data = CategoryInput.model_validate(data)
        def work(conn,v):
            id_=uid()
            conn.execute('INSERT INTO finance_categories VALUES (?,?,?)',(id_,data.name,data.kind))
            result=one(conn,'SELECT * FROM finance_categories WHERE id=?',(id_,))
            audit(conn,'category',id_,'created',data.request_id,result)
            return result,utc(self.clock())
        return self._write('category',data,work)

    @staticmethod
    def _bill(conn, data: BillInput, v: dict, correction: str | None = None) -> dict:
        account=require_account(conn,data.account_id,securities=False,active=True,at=v['occurred_at'])
        category=one(conn,'SELECT * FROM finance_categories WHERE id=?',(data.category_id,))
        if data.kind=='refund':
            if not data.related_transaction_id or account['asset_class']!='asset' or account['balance_mode']!='manual_snapshot':
                raise TradeError('refund requires explicit original expense and eligible manual asset account')
            original=one(conn,'SELECT * FROM finance_transactions WHERE id=?',(data.related_transaction_id,))
            if original['kind'] not in ('expense','fee') or original['status']!='effective' or original['occurred_at']>v['occurred_at'] or original['category_id']!=data.category_id:
                raise TradeError('refund must refer to an effective original expense/fee in its category')
            recovered=conn.execute("SELECT COALESCE(SUM(amount_fen),0) FROM finance_transactions WHERE related_transaction_id=? AND status='effective'",(original['id'],)).fetchone()[0]
            if recovered+data.amount_fen>original['amount_fen']:
                raise Conflict('cumulative refunds exceed original expense')
        elif data.related_transaction_id is not None or category['kind']!=data.kind:
            raise TradeError('category or refund reference does not match bill kind')
        id_=uid()
        conn.execute("INSERT INTO finance_transactions (id,account_id,kind,amount_fen,category_id,occurred_at,note,correction_of,status,related_transaction_id) VALUES (?,?,?,?,?,?,?,?,'effective',?)",
                     (id_,data.account_id,data.kind,data.amount_fen,data.category_id,v['occurred_at'],data.note,correction,data.related_transaction_id))
        result=one(conn,'SELECT * FROM finance_transactions WHERE id=?',(id_,))
        audit(conn,'bill',id_,'created' if correction is None else 'corrected',data.request_id,result)
        return result

    def bill(self,data: BillInput) -> dict:
        data = BillInput.model_validate(data)
        def work(conn,v):
            result=self._bill(conn,data,v)
            affected=v['occurred_at']
            if data.related_transaction_id:
                affected=min(affected,one(conn,'SELECT occurred_at FROM finance_transactions WHERE id=?',(data.related_transaction_id,))['occurred_at'])
            return result,affected
        return self._write('bill',data,work)

    def change_bill(self,id_: str,data: BillInput | VoidInput) -> dict:
        data = type(data).model_validate(data)
        def work(conn,v):
            old=one(conn,'SELECT * FROM finance_transactions WHERE id=?',(id_,))
            if old['status']!='effective': raise Conflict('bill already reversed')
            if conn.execute("SELECT 1 FROM finance_transactions WHERE related_transaction_id=? AND status='effective'",(id_,)).fetchone():
                raise Conflict('reverse or correct linked refunds before changing original expense')
            conn.execute("UPDATE finance_transactions SET status='reversed' WHERE id=?",(id_,))
            audit(conn,'bill',id_,'reversed',data.request_id,{'reason':data.note if isinstance(data,BillInput) else data.reason})
            affected=old['occurred_at']
            if old['related_transaction_id']:
                affected=min(affected,one(conn,'SELECT occurred_at FROM finance_transactions WHERE id=?',(old['related_transaction_id'],))['occurred_at'])
            if isinstance(data,BillInput):
                result=self._bill(conn,data,v,id_)
                if data.related_transaction_id:
                    affected=min(affected,one(conn,'SELECT occurred_at FROM finance_transactions WHERE id=?',(data.related_transaction_id,))['occurred_at'])
                return result,min(affected,v['occurred_at'])
            return one(conn,'SELECT * FROM finance_transactions WHERE id=?',(id_,)),affected
        return self._write('bill_correction' if isinstance(data,BillInput) else 'bill_void',data,work,target=id_)

    @staticmethod
    def _transfer(conn, data: TransferInput, v: dict, correction: str | None = None) -> dict:
        at=v['occurred_at']; id_=uid()
        endpoints=[(require_account(conn,data.from_account_id,active=True,at=at),-data.amount_fen,data.from_included,data.from_balance_id),
                   (require_account(conn,data.to_account_id,active=True,at=at),data.amount_fen,data.to_included,data.to_balance_id)]
        conn.execute("INSERT INTO finance_transfers VALUES (?,?,?,?,?,?,?,?,?,?,?, 'effective')",
                     (id_,data.from_account_id,data.to_account_id,data.amount_fen,at,data.note,data.from_included,data.to_included,data.from_balance_id,data.to_balance_id,correction))
        for account,delta,included,expected in endpoints:
            aid=account['id']
            if account['kind']=='securities':
                if included is not None or expected is not None:
                    raise TradeError('securities use ledger, not manual balance confirmation')
                movement=post_cash(conn,account=aid,kind='deposit' if delta>0 else 'withdrawal',amount=delta,at=at,note=data.note,transfer=id_)
                audit(conn,'cash',movement['id'],'transfer',data.request_id,movement)
            else:
                if included is None or expected is None:
                    raise TradeError('manual transfer endpoint requires explicit inclusion confirmation and balance ID')
                latest=balance_at(conn,aid)
                if latest is None or latest['id']!=expected:
                    raise Conflict('manual transfer balance missing or changed')
                if included:
                    if latest['occurred_at']<at:
                        raise Conflict('balance cannot already include a later transfer')
                else:
                    if latest['occurred_at']>at:
                        raise Conflict('historical transfer requires already-included balance confirmation')
                    # A positive payment into a liability reduces the outstanding debt.
                    amount=latest['after_fen']+(-delta if account['asset_class']=='liability' else delta)
                    set_balance(conn,aid,amount,at,data.note,expected,data.request_id,id_)
        result=one(conn,'SELECT * FROM finance_transfers WHERE id=?',(id_,))
        audit(conn,'transfer',id_,'created' if correction is None else 'corrected',data.request_id,result)
        return result

    def transfer(self,data: TransferInput) -> dict:
        data = TransferInput.model_validate(data)
        return self._write('transfer',data,lambda c,v:(self._transfer(c,data,v),v['occurred_at']))

    def change_transfer(self,id_: str,data: TransferInput | VoidInput) -> dict:
        data = type(data).model_validate(data)
        def work(conn,v):
            old=one(conn,'SELECT * FROM finance_transfers WHERE id=?',(id_,))
            if old['status']!='effective': raise Conflict('transfer already reversed')
            if old['from_included'] or old['to_included']:
                raise Conflict('already-included manual balance requires manual reconciliation before transfer correction')
            adjustments=rows(conn,"SELECT * FROM balance_adjustments WHERE transfer_id=? AND status='effective'",(id_,))
            for item in adjustments:
                latest=balance_at(conn,item['account_id'])
                if latest is None or latest['id']!=item['id'] or conn.execute(
                        "SELECT 1 FROM finance_transfers WHERE id!=? AND status='effective' AND (from_balance_id=? OR to_balance_id=?)",
                        (id_,item['id'],item['id'])).fetchone():
                    raise Conflict('later balance or transfer facts depend on transfer; reconcile before correction')
            reason=data.note if isinstance(data,TransferInput) else data.reason
            for item in adjustments:
                conn.execute("UPDATE balance_adjustments SET status='reversed' WHERE id=?",(item['id'],))
                audit(conn,'balance',item['id'],'reversed',data.request_id,{'reason':reason,'transfer_id':id_})
            for movement in rows(conn,'SELECT * FROM effective_cash_movements WHERE transfer_id=?',(id_,)):
                reverse_cash(conn,movement,reason,data.request_id)
            conn.execute("UPDATE finance_transfers SET status='reversed' WHERE id=?",(id_,))
            audit(conn,'transfer',id_,'reversed',data.request_id,{'reason':reason})
            if isinstance(data,TransferInput):
                if data.from_included or data.to_included:
                    raise Conflict('correction of adjusted transfer must explicitly apply both new balance effects')
                return self._transfer(conn,data,v,id_),min(old['occurred_at'],v['occurred_at'])
            return one(conn,'SELECT * FROM finance_transfers WHERE id=?',(id_,)),old['occurred_at']
        return self._write('transfer_correction' if isinstance(data,TransferInput) else 'transfer_void',data,work,target=id_)

    def reconcile_transfer(self,id_: str,data: TransferReconciliation) -> dict:
        """Explicit current balance evidence; never infer an earlier manual balance."""
        data=TransferReconciliation.model_validate(data)
        def work(conn,v):
            old=one(conn,'SELECT * FROM finance_transfers WHERE id=?',(id_,))
            if old['status']!='effective': raise Conflict('transfer already reversed')
            at=v['occurred_at']
            if at<old['occurred_at']: raise Conflict('reconciliation precedes transfer')
            accounts=[require_account(conn,old[key]) for key in ('from_account_id','to_account_id')]
            manual={a['id'] for a in accounts if a['kind']!='securities'}
            evidence={b.account_id:b for b in data.balances}
            if set(evidence)!=manual or len(evidence)!=len(data.balances):
                raise TradeError('confirm each manual endpoint exactly once')
            # The cash leg is reversed at its original time. Preserve all later
            # observed balances; only append explicit new manual evidence.
            for a in accounts:
                if a['id'] in manual:
                    latest=balance_at(conn,a['id']); b=evidence[a['id']]
                    if latest is None or latest['id']!=b.expected_balance_id or latest['occurred_at']>at:
                        raise Conflict('balance changed or reconciliation precedes latest snapshot')
            confirmed={}
            for aid,b in evidence.items():
                result=set_balance(conn,aid,b.confirmed_fen,at,data.note,b.expected_balance_id,data.request_id)
                confirmed[aid]=result['id']
                conn.execute('INSERT INTO transfer_reconciliation_gaps VALUES (?,?,?,?,?,?)',
                             (uid(),aid,id_,old['occurred_at'],at,result['id']))
            movements=rows(conn,'SELECT * FROM effective_cash_movements WHERE transfer_id=?',(id_,))
            expected_cash={a['id']: (-old['amount_fen'] if a['id']==old['from_account_id'] else old['amount_fen'])
                           for a in accounts if a['kind']=='securities'}
            if len(movements)!=len(expected_cash) or any(expected_cash.get(m['account_id'])!=m['amount_fen'] or
                    m['occurred_at']!=old['occurred_at'] or m['kind']!=('deposit' if m['amount_fen']>0 else 'withdrawal') for m in movements):
                raise Conflict('transfer cash evidence inconsistent; reconciliation refused')
            for movement in movements:
                reverse_cash(conn,movement,data.note,data.request_id)
            conn.execute("UPDATE finance_transfers SET status='reversed' WHERE id=?",(id_,))
            audit(conn,'transfer',id_,'reconciled',data.request_id,{'reason':data.note,'confirmed_balances':confirmed,'replacement_amount_fen':data.corrected_amount_fen})
            if data.corrected_amount_fen is not None:
                # Same endpoints and original occurrence time. Both manual sides
                # already reflect the verified actual outcome; never adjust twice.
                replacement=TransferInput(request_id=data.request_id,from_account_id=old['from_account_id'],to_account_id=old['to_account_id'],
                    amount_fen=data.corrected_amount_fen,occurred_at=old['occurred_at'],note=data.note,
                    from_included=True if old['from_account_id'] in manual else None,
                    to_included=True if old['to_account_id'] in manual else None,
                    from_balance_id=confirmed.get(old['from_account_id']),to_balance_id=confirmed.get(old['to_account_id']))
                return self._transfer(conn,replacement,value_of(replacement),id_),old['occurred_at']
            return one(conn,'SELECT * FROM finance_transfers WHERE id=?',(id_,)),old['occurred_at']
        return self._write('transfer_reconciliation',data,work,target=id_)

    def change_cash(self,id_: str,data: CashCorrection | VoidInput) -> dict:
        data = type(data).model_validate(data)
        def work(conn,v):
            old=one(conn,'SELECT * FROM cash_movements WHERE id=?',(id_,))
            if old['transfer_id'] is not None:
                raise Conflict('internal transfer must be corrected as a whole')
            if old['kind']=='dividend':
                raise Conflict('use company event correction for dividend')
            reason=data.note if isinstance(data,CashCorrection) else data.reason
            reversal=reverse_cash(conn,old,reason,data.request_id)
            if isinstance(data,CashCorrection):
                if (old['kind']=='opening' and data.amount_fen<0 or old['kind']=='deposit' and data.amount_fen<=0 or old['kind']=='withdrawal' and data.amount_fen>=0 or old['kind']=='adjustment' and data.amount_fen==0):
                    raise TradeError('correction amount conflicts with cash kind')
                result=post_cash(conn,account=old['account_id'],kind=old['kind'],amount=data.amount_fen,at=v['occurred_at'],note=data.note,request=data.request_id)
                audit(conn,'cash',result['id'],'corrected',data.request_id,{'correction_of':id_,'reversal_id':reversal})
                return result,min(old['occurred_at'],v['occurred_at'])
            return {'id':id_,'reversal_id':reversal,'status':'reversed'},old['occurred_at']
        return self._write('cash_correction' if isinstance(data,CashCorrection) else 'cash_void',data,work,target=id_)

    def change_dividend(self,id_: str,data: CashCorrection | VoidInput) -> dict:
        data = type(data).model_validate(data)
        def work(conn,v):
            old=one(conn,'SELECT * FROM company_events WHERE id=?',(id_,))
            if old['kind']!='dividend': raise Conflict('only confirmed dividends supported')
            reason=data.note if isinstance(data,CashCorrection) else data.reason
            movement=one(conn,'SELECT * FROM cash_movements WHERE id=?',(old['cash_movement_id'],))
            reversal=reverse_cash(conn,movement,reason,data.request_id)
            audit(conn,'company_event',id_,'reversed',data.request_id,{'reason':reason,'reversal_id':reversal})
            if isinstance(data,CashCorrection):
                if data.amount_fen<=0: raise TradeError('dividend must be positive')
                cash=post_cash(conn,account=old['account_id'],kind='dividend',amount=data.amount_fen,at=v['occurred_at'],note=data.note,request=data.request_id)
                new=uid()
                conn.execute('INSERT INTO company_events (id,account_id,instrument_id,kind,occurred_at,cash_movement_id,note) VALUES (?,?,?,?,?,?,?)',(new,old['account_id'],old['instrument_id'],'dividend',v['occurred_at'],cash['id'],data.note))
                audit(conn,'company_event',new,'corrected',data.request_id,{'correction_of':id_,'reversal_id':reversal})
                return one(conn,'SELECT * FROM company_events WHERE id=?',(new,)),min(old['occurred_at'],v['occurred_at'])
            return {'id':id_,'reversal_id':reversal,'status':'reversed'},old['occurred_at']
        return self._write('dividend_correction' if isinstance(data,CashCorrection) else 'dividend_void',data,work,target=id_)

    @staticmethod
    def _month(conn: sqlite3.Connection,start: date,cutoff: str) -> dict:
        begin,end=month_bounds(start)
        items=rows(conn,"SELECT * FROM finance_transactions WHERE status='effective' AND occurred_at>=? AND occurred_at<? AND occurred_at<=? ORDER BY occurred_at,rowid",(begin,end,cutoff))
        totals={kind:sum(r['amount_fen'] for r in items if r['kind']==kind) for kind in ('income','expense','fee')}
        categories={}; refund_received=sum(r['amount_fen'] for r in items if r['kind']=='refund'); recovered=0
        for item in items:
            if item['kind']=='refund':
                original=rows(conn,'SELECT occurred_at FROM finance_transactions WHERE id=?',(item['related_transaction_id'],))
                item['original_expense_period']=month_start(day_of(datetime.fromisoformat(original[0]['occurred_at']))).isoformat() if original else None
                continue
            reduction=0
            if item['kind'] in ('expense','fee'):
                reduction=conn.execute("SELECT COALESCE(SUM(amount_fen),0) FROM finance_transactions WHERE related_transaction_id=? AND status='effective' AND occurred_at<=?",(item['id'],cutoff)).fetchone()[0]
                totals[item['kind']]-=reduction; recovered+=reduction
            item['refund_applied_fen']=reduction
            item['net_amount_fen']=item['amount_fen']-reduction
            key=item['category_id'];categories[key]=categories.get(key,0)+item['net_amount_fen']
        gross_expense=sum(r['amount_fen'] for r in items if r['kind'] in ('expense','fee'))
        return {'period':start.isoformat(),'start_at':begin,'end_exclusive':end,
                'income_fen':totals['income'],'expense_fen':totals['expense'],'fees_fen':totals['fee'],
                'net_fen':totals['income']-totals['expense']-totals['fee'],'categories':categories,'items':items,
                'refund_received_fen':refund_received,'refund_against_original_fen':recovered,
                'bill_cash_flow_fen':totals['income']-gross_expense+refund_received,
                'source':'current_effective_finance_facts_not_knowledge_replay',
                'refund_basis':'original_expense_period; receipt_cash_flow_at_actual_date'}

    def month(self,start: date,as_of: datetime) -> dict:
        cutoff=PortfolioService(self.db,self.clock)._cutoff(as_of)
        with self.db.read() as conn:
            return {**one(conn,'SELECT ledger_revision FROM ledger_state'),'as_of':cutoff,**self._month(conn,start,cutoff)}

    def year(self,year: int,as_of: datetime) -> dict:
        if not 1900<=year<=9997: raise TradeError('unsupported finance year')
        cutoff=PortfolioService(self.db,self.clock)._cutoff(as_of)
        with self.db.read() as conn:
            return {**one(conn,'SELECT ledger_revision FROM ledger_state'),'as_of':cutoff,'items':[self._month(conn,date(year,m,23),cutoff) for m in range(1,13)]}

    def record_detail(self, kind: str, id_: str, expected_revision: int | None=None) -> dict:
        """Read immutable audit facts and explicit direct links in one ledger snapshot."""
        tables={'bills':('finance_transactions','bill'), 'transfers':('finance_transfers','transfer'),
                'balances':('balance_adjustments','balance'), 'accounts':('accounts','account')}
        if kind not in tables: raise TradeError('unsupported finance audit record kind')
        table,entity=tables[kind]
        with self.db.read() as conn:
            revision=one(conn,'SELECT ledger_revision FROM ledger_state')['ledger_revision']
            if expected_revision is not None and expected_revision!=revision:
                raise Conflict('ledger changed; reload audit query')
            record=one(conn,'SELECT * FROM '+table+' WHERE id=?',(id_,))
            events=rows(conn,'SELECT id,event,request_id,at,actor,details FROM audit_events WHERE entity=? AND entity_id=? ORDER BY at,rowid',(entity,id_))
            related=[]; original=None; refunded=None; remaining=None
            if kind=='bills':
                if record['kind']=='refund' and record['related_transaction_id']:
                    candidates=rows(conn,'SELECT * FROM finance_transactions WHERE id=?',(record['related_transaction_id'],))
                    original=candidates[0] if candidates else None
                elif record['kind'] in ('expense','fee'):
                    original=record
                related=rows(conn,"SELECT * FROM finance_transactions WHERE id<>? AND (correction_of=? OR id=? OR related_transaction_id=?) ORDER BY rowid",(id_,id_,record['correction_of'],id_))
                if original is not None and original['status']=='effective' and original['kind'] in ('expense','fee'):
                    refunded=conn.execute("SELECT COALESCE(SUM(amount_fen),0) FROM finance_transactions WHERE related_transaction_id=? AND kind='refund' AND status='effective'",(original['id'],)).fetchone()[0]
                    remaining=original['amount_fen']-refunded
            elif kind=='transfers':
                related=rows(conn,'SELECT * FROM finance_transfers WHERE id<>? AND (correction_of=? OR id=?) ORDER BY rowid',(id_,id_,record['correction_of']))
            elif kind=='balances':
                related=rows(conn,'SELECT * FROM balance_adjustments WHERE id<>? AND (previous_id=? OR id=?) ORDER BY rowid',(id_,id_,record['previous_id']))
            return {'kind':kind,'record':record,'audit_events':events,'related_records':related,
                    'database_uuid':self.db.identity.database_uuid,'generation':self.db.identity.generation,
                    'original_expense':original,'effective_refunds_fen':refunded,'remaining_expense_fen':remaining,
                    'ledger_revision':revision,'source':'current_ledger_audit_facts'}

    def records(self, kind: str, limit: int=100, offset: int=0,
                account_id: str | None=None, from_at: datetime | None=None,
                to_at: datetime | None=None, expected_revision: int | None=None) -> dict:
        tables={'categories':'finance_categories','bills':'finance_transactions','balances':'balance_adjustments',
                'transfers':'finance_transfers','company-events':'company_events'}
        if kind not in tables or not 1<=limit<=500 or offset<0:
            raise TradeError('invalid finance list query')
        if kind=='categories' and any(x is not None for x in (account_id,from_at,to_at)):
            raise TradeError('categories do not have account or occurrence times')
        try:
            begin=utc(require_time(from_at)) if from_at is not None else None
            end=utc(require_time(to_at)) if to_at is not None else None
        except ValueError as exc:
            raise TradeError(str(exc)) from exc
        if begin and end and begin>=end: raise TradeError('invalid history interval')
        conditions=[]; args=[]
        if account_id is not None:
            if kind=='transfers':
                conditions.append('(from_account_id=? OR to_account_id=?)'); args.extend((account_id,account_id))
            else:
                conditions.append('account_id=?'); args.append(account_id)
        if begin: conditions.append('occurred_at>=?'); args.append(begin)
        if end: conditions.append('occurred_at<?'); args.append(end)
        query='SELECT * FROM '+tables[kind]+(' WHERE '+' AND '.join(conditions) if conditions else '')
        with self.db.read() as conn:
            rev=one(conn,'SELECT ledger_revision FROM ledger_state')
            if expected_revision is not None and expected_revision!=rev['ledger_revision']:
                raise Conflict('ledger changed; restart paginated query')
            items=rows(conn,query+' ORDER BY rowid DESC LIMIT ? OFFSET ?',(*args,limit,offset))
            if kind=='company-events':
                for item in items:
                    item['status']='unresolved' if item['kind']!='dividend' else ('effective' if conn.execute('SELECT 1 FROM effective_cash_movements WHERE id=?',(item['cash_movement_id'],)).fetchone() else 'reversed')
                    if item['kind']=='dividend':
                        item['amount_fen']=one(conn,'SELECT amount_fen FROM cash_movements WHERE id=?',(item['cash_movement_id'],))['amount_fen']
            return {**rev,'items':items,'limit':limit,'offset':offset,
                    'total':conn.execute('SELECT count(*) FROM ('+query+')',tuple(args)).fetchone()[0]}

    def overview(self,as_of: datetime,quote_revision: int | None=None) -> dict:
        portfolio=PortfolioService(self.db,self.clock)
        cutoff=portfolio._cutoff(as_of)
        try: window=market_window(as_of)
        except CalendarUnavailable: window=None
        with self.db.read() as conn:
            ledger=one(conn,'SELECT ledger_revision FROM ledger_state')['ledger_revision']
            revision=portfolio._revision(conn,quote_revision)
            return self._overview(conn,portfolio,as_of,cutoff,window,ledger,revision)

    def net_worth_trend(self, months: int, as_of: datetime) -> dict:
        if months not in (1,3,6,12): raise TradeError('supported trend windows: 1,3,6,12 months')
        portfolio=PortfolioService(self.db,self.clock)
        cutoff=portfolio._cutoff(as_of)
        today=day_of(as_of)
        with self.db.read() as conn:
            ledger=one(conn,'SELECT ledger_revision FROM ledger_state')['ledger_revision']
            revision=portfolio._revision(conn,None)
            items=[]
            for offset in range(months-1,-1,-1):
                year,month0=divmod(today.year*12+today.month-1-offset,12)
                first=date(year,month0+1,1)
                if year<1900: raise TradeError('trend outside supported timestamp range')
                last=(first.replace(day=28)+timedelta(days=4)).replace(day=1)-timedelta(days=1)
                point=min(as_of,datetime.combine(last,time.max,SHANGHAI))
                try: window=market_window(point)
                except CalendarUnavailable: window=None
                value=self._overview(conn,portfolio,point,utc(point),window,ledger,revision)
                row={k:value[k] for k in ('as_of','total_assets_fen','total_liabilities_fen','net_worth_fen','liquid_assets_fen','liquid_net_worth_fen','restricted_assets_fen','liquidity_reasons','liquidity_basis','securities_equity_fen','quality','reasons')}
                if not any(a['included'] for a in value['accounts']):
                    row.update(total_assets_fen=None,total_liabilities_fen=None,net_worth_fen=None,liquid_assets_fen=None,liquid_net_worth_fen=None,restricted_assets_fen=None,securities_equity_fen=None,quality='unavailable',reasons=['no_included_account_evidence'])
                items.append(row)
            return {'as_of':cutoff,'ledger_revision':ledger,'quote_revision':revision,'source':'current_ledger_projection_not_knowledge_replay','items':items}

    def _overview(self,conn,portfolio,as_of,cutoff,window,ledger,revision):
        items=[]; assets=liabilities=0; assets_ok=liabilities_ok=True; reasons=[]
        for account in rows(conn,'SELECT * FROM accounts WHERE effective_at IS NULL OR effective_at<=? ORDER BY id',(cutoff,)):
            if account['kind']=='securities':
                valuation=portfolio._valuation(conn,account['id'],as_of,window,revision)
                amount=valuation['equity_fen']
                details={k:valuation[k] for k in ('cash_fen','cash_quality','market_value_fen','known_market_value_fen','equity_fen','daily_pnl_fen','daily_return_pct','daily_return_reason','previous_close_equity_fen','previous_close_as_of','cumulative_pnl_fen','reasons')}
                # Explain the actual valuation inputs, never infer a quote from a market value.
                details['balance_source']='sqlite_ledger_and_explicit_quotes'
                details['balance_at']=cutoff
                details['quote_evidence']=[
                    {'instrument_id':p['instrument_id'],'code':p['code'],'market':p['market'],'quantity':p['quantity'],
                     'market_value_fen':p['market_value_fen'],'quality':p['quality'],
                     'quote_id':p['quote']['id'] if p['quote'] else None,
                     'quote_source':p['quote']['source'] if p['quote'] else None,
                     'quote_at':p['quote']['effective_at'] if p['quote'] else None,
                     'quote_revision':p['quote']['quote_revision'] if p['quote'] else None,
                     'quote_price_basis':p['quote']['price_basis'] if p['quote'] else None}
                    for p in valuation['positions']]
                baseline=rows(conn,"SELECT id,occurred_at,amount_fen FROM effective_cash_movements WHERE account_id=? AND kind='opening' AND occurred_at<=?",(account['id'],cutoff))
                details['cash_baseline']=baseline[0] if len(baseline)==1 else None
            else:
                balance=balance_at(conn,account['id'],cutoff)
                amount=balance['after_fen'] if balance else None
                historical_gap=conn.execute("""SELECT 1 FROM finance_transfers t JOIN balance_adjustments b ON
                    (t.from_account_id=? AND t.from_included=1 AND t.from_balance_id=b.id) OR
                    (t.to_account_id=? AND t.to_included=1 AND t.to_balance_id=b.id)
                    WHERE t.status='effective' AND t.occurred_at<=? AND b.occurred_at>? LIMIT 1""",
                    (account['id'],account['id'],cutoff,cutoff)).fetchone()
                if historical_gap: amount=None
                details={'balance_source':'manual_balance_snapshot','balance_id':balance['id'] if balance else None,'balance_at':balance['occurred_at'] if balance else None,
                         'last_snapshot_fen':balance['after_fen'] if balance else None,
                         'reasons':(['transfer_inclusion_snapshot_not_yet_effective'] if historical_gap else [] if balance else ['missing_balance'])}
            details['effective_date_quality']='valid' if account['effective_at'] is not None else 'unknown'
            if account['effective_at'] is None:
                first=first_account_fact(conn,account['id'])
                details['effective_date_quality']='unknown'
                if first is None or first>cutoff:
                    amount=None
                    details['reasons']=[*details['reasons'],'account_existence_unverified']
            if conn.execute('SELECT 1 FROM transfer_reconciliation_gaps WHERE account_id=? AND from_at<=? AND until_at>? LIMIT 1',(account['id'],cutoff,cutoff)).fetchone():
                amount=None
                details['reasons']=[*details['reasons'],'transfer_reconciliation_historical_gap']
            included_now=scope_at(conn,account,cutoff)
            status,status_at=liquidity_status_at(conn,account,cutoff)
            display=(LIQUIDITY_DISPLAY[status] if account['asset_class']=='asset'
                     else '负债：按现行流动负债口径（不使用资产受限标记）')
            items.append({**account,**details,'included':included_now,'balance_fen':amount,
                          'liquidity_status':status,'liquidity_status_effective_at':status_at,
                          'liquidity_status_display':display,
                          'quality':'valid' if amount is not None else 'unavailable'})
            if not included_now: continue
            if account['asset_class']=='asset':
                if amount is None: assets_ok=False
                else: assets+=amount
            else:
                if amount is None: liabilities_ok=False
                else: liabilities+=amount
            if amount is None:
                reasons.extend(account['id']+':'+reason for reason in details['reasons'])
                reasons.append(account['id']+':unverified_balance_or_equity')
        # Imported pre-0006 single-sided transfer references remain evidence,
        # but cannot silently be presented as a reconciled personal total.
        orphan=rows(conn,"""SELECT m.id,m.account_id FROM effective_cash_movements m JOIN accounts a ON a.id=m.account_id
            WHERE m.transfer_id IS NOT NULL AND m.occurred_at<=?
            AND NOT EXISTS (SELECT 1 FROM finance_transfers t WHERE t.id=m.transfer_id AND t.status='effective')""",(cutoff,))
        included_ids={r['id'] for r in items if r['included']}
        orphan=[r for r in orphan if r['account_id'] in included_ids]
        if orphan:
            assets_ok=False
            reasons.extend(r['account_id']+':legacy_unpaired_transfer' for r in orphan)
        included=[r for r in items if r['included']]
        categories={}
        for item in included:
            group=categories.setdefault(item['kind'],{'asset_class':item['asset_class'],'known_fen':0,'quality':'valid'})
            if item['balance_fen'] is None: group['quality']='unavailable'
            else: group['known_fen']+=item['balance_fen']
        for group in categories.values():
            group['amount_fen']=group['known_fen'] if group['quality']=='valid' else None
        securities=[r for r in included if r['kind']=='securities']
        def total(field):
            return sum(r[field] for r in securities) if all(r[field] is not None for r in securities) else None
        denominator=total('previous_close_equity_fen'); numerator=total('daily_pnl_fen')
        return_pct=None
        if securities and all(r['daily_return_pct'] is not None for r in securities) and denominator and denominator>0 and numerator is not None:
            return_pct=format(Decimal(numerator)*100/Decimal(denominator),'.2f')
        # Account-level liquidity classification is explicit evidence; absence of
        # evidence is 'unknown', never silently unrestricted. Restricted assets stay
        # in total assets and net worth but never enter liquid assets. Liabilities
        # keep the approved current-liability basis and are not mixed with this mark.
        asset_accounts=[r for r in included if r['asset_class']=='asset']
        liquid_accounts=[r for r in asset_accounts if r['liquidity_status']=='liquid']
        restricted_accounts=[r for r in asset_accounts if r['liquidity_status']=='restricted']
        liquidity_unknown=[r['id'] for r in asset_accounts if r['liquidity_status']=='unknown']
        restricted_total=(sum(r['balance_fen'] for r in restricted_accounts)
                          if all(r['balance_fen'] is not None for r in restricted_accounts) else None)
        liquid_asset_total=(sum(r['balance_fen'] for r in liquid_accounts)
                            if all(r['balance_fen'] is not None for r in liquid_accounts)
                            and not liquidity_unknown and assets_ok else None)
        liquid_net=(liquid_asset_total-liabilities
                    if liquid_asset_total is not None and liabilities_ok else None)
        liquidity_reasons=(['liquidity_classification_unknown:'+i for i in liquidity_unknown]
                           + ([] if assets_ok and liabilities_ok else ['balance_or_valuation_unavailable']))
        result={'ledger_revision':ledger,'quote_revision':revision,'as_of':cutoff,'quality':'valid' if assets_ok and liabilities_ok else 'derived_unverified','asset_categories':categories,
                'securities_cash_fen':total('cash_fen') if all(r['cash_quality']=='valid' for r in securities) else None,
                'securities_market_value_fen':total('market_value_fen'),'securities_equity_fen':total('equity_fen'),
                'investment_daily_pnl_fen':numerator,'investment_daily_return_pct':return_pct,'investment_daily_return_reason':None if return_pct is not None else 'account_return_or_denominator_unavailable','investment_previous_close_equity_fen':denominator,
                'liquid_assets_fen':liquid_asset_total,'liquid_net_worth_fen':liquid_net,'restricted_assets_fen':restricted_total,
                'liquidity_reasons':liquidity_reasons,
                'liquidity_basis':('explicit_account_liquidity_status; restricted assets stay in total assets and net worth '
                                   'but are excluded from liquid assets; unconfirmed classification withheld; '
                                   'liabilities use the approved current-liability basis'),
                'reasons':reasons,'accounts':items,'known_assets_fen':assets,'known_liabilities_fen':liabilities,
                'total_assets_fen':assets if assets_ok else None,'total_liabilities_fen':liabilities if liabilities_ok else None,
                'net_worth_fen':assets-liabilities if assets_ok and liabilities_ok else None,
                'finance_month':self._month(conn,month_start(day_of(as_of)),cutoff)}
        # Per-amount explanations reuse the authoritative calculated values, in the same read snapshot.
        # Related reasons are context, not an assertion that each code directly caused the field to be null.
        security_reasons=sorted(set(r['id']+':'+reason for r in securities for reason in r['reasons']))
        definitions=[
            ('net_worth_fen','净资产','included_account_projection','纳入统计的总资产 − 总负债',included,reasons),
            ('total_assets_fen','总资产','included_account_projection','纳入统计的资产账户余额或证券权益之和',[r for r in included if r['asset_class']=='asset'],reasons),
            ('total_liabilities_fen','总负债','included_account_projection','纳入统计的负债账户余额之和',[r for r in included if r['asset_class']=='liability'],reasons),
            ('liquid_assets_fen','流动资产','confirmed_liquidity_classification','已确认不受限的资产账户余额或证券权益之和；受限资产与待确认分类不计入',liquid_accounts,liquidity_reasons),
            ('liquid_net_worth_fen','流动净资产','confirmed_liquidity_classification','流动资产减纳入统计的流动负债；存在待确认分类时不计算',liquid_accounts,liquidity_reasons),
            ('restricted_assets_fen','受限资产','account_liquidity_status','已确认受限的资产账户余额或证券权益；仍计入总资产及净资产，不计入流动资产',restricted_accounts,[]),
            ('securities_cash_fen','证券现金','effective_cash_ledger','有效证券现金期初及流水；不使用普通账单改余额',securities,security_reasons),
            ('securities_market_value_fen','证券市值','sqlite_ledger_and_explicit_quotes','剩余批次数量乘有效证券报价；缺失报价不填零',securities,security_reasons),
            ('securities_equity_fen','证券权益','sqlite_ledger_and_explicit_quotes','证券现金加证券市值，仅计入个人资产一次',securities,security_reasons),
            ('investment_daily_pnl_fen','投资当日盈亏','sqlite_ledger_and_explicit_quotes','自然日持仓价值变化、实际交易及公司行为；资金划转不是收益',securities,security_reasons),
        ]
        result['amount_evidence']=[
            {'field':field,'label':label,'amount_fen':result[field],
             'quality':'valid' if result[field] is not None else 'unavailable',
             'source':source,'basis':basis,'as_of':cutoff,'ledger_revision':ledger,'quote_revision':revision,
             'account_ids':[r['id'] for r in scope],'related_reasons':sorted(set(context))}
            for field,label,source,basis,scope,context in definitions]
        result['amount_evidence'].append({
            'field':'finance_month.bill_cash_flow_fen','label':'本财务月账单现金流',
            'amount_fen':result['finance_month']['bill_cash_flow_fen'],'quality':'valid',
            'source':'effective_finance_bill_ledger','basis':'本期收入减原始支出及费用加实际到账退款；与手工余额独立',
            'as_of':cutoff,'ledger_revision':ledger,'quote_revision':revision,
            'account_ids':sorted({r['account_id'] for r in result['finance_month']['items']}),'related_reasons':[],
        })
        return result
