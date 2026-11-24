"""05-finance golden and safety cases using only isolated SQLite fixtures."""
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL

from backend.application.finance import (
    FinanceService, BalanceInput, BillInput, TransferInput, CashCorrection,
    VoidInput, AccountUpdate, CategoryInput, month_start,
)
from backend.application.portfolio import PortfolioService, CompanyInput, OpeningInput
from backend.application.trading.service import AccountInput, CashInput, Conflict, TradeError, utc
from backend.infrastructure import database
from backend.infrastructure.database import REVISION, SCHEMA_VERSION, Identity, migrate_schema, upgrade_database, open_database, adopt_database
from backend.main import create_app
from backend.config import AppConfig
from test_trading import cfg, ledger, trade, snapshot
from test_pnl import at, cash, quote, legacy_trading
from test_database_upgrade import cfg_for


def manual(svc,name,kind='bank',day=5,included=True):
    return svc.create_account(AccountInput(request_id='account-'+name,name=name,kind=kind,effective_at=at(day,7),included=included))['id']


def balance(f,a,amount,req='balance',day=5,hour=8,expected=None):
    return f.balance(BalanceInput(request_id=req,account_id=a,after_fen=amount,occurred_at=at(day,hour),note='confirmed statement',expected_balance_id=expected))


def bill(f,a,req='bill',amount=20000,occurred_at=None,kind='expense',category='food'):
    return f.bill(BillInput(request_id=req,account_id=a,kind=kind,category_id=category,amount_fen=amount,occurred_at=occurred_at or at(6),note='actual bill'))


def transfer(a,b,balance_id,*,req='transfer',amount=200000,included=False,day=6):
    return TransferInput(request_id=req,from_account_id=a,to_account_id=b,amount_fen=amount,occurred_at=at(day,9),note='confirmed transfer',from_included=included,from_balance_id=balance_id)


def account(result,a):
    return next(x for x in result['accounts'] if x['id']==a)


def state(svc):
    with svc.db.read() as c:
        return {t:c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall() for t in ('accounts','finance_transactions','finance_transfers','balance_adjustments','cash_movements','company_events','audit_events','ledger_state','ledger_requests')}


def test_golden_a_f02_and_d_bill_correction(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db)
    bank=manual(svc,'bank'); balance(f,bank,1000000)
    original=bill(f,bank)
    r=f.overview(at(6)); assert account(r,bank)['balance_fen']==1000000
    assert r['finance_month']['expense_fen']==20000
    correction=BillInput(request_id='correct',account_id=bank,kind='expense',category_id='food',amount_fen=15000,occurred_at=at(6),note='receipt corrected')
    corrected=f.change_bill(original['id'],correction); before=state(svc)
    assert f.change_bill(original['id'],correction)==corrected and state(svc)==before
    r=f.overview(at(6)); assert account(r,bank)['balance_fen']==1000000 and r['finance_month']['expense_fen']==15000
    rows=f.records('bills')['items']; assert len(rows)==2 and any(x['id']==original['id'] and x['amount_fen']==20000 and x['status']=='reversed' for x in rows)
    assert corrected['correction_of']==original['id']
    print('A/D',1000000,20000,r['finance_month']['expense_fen'])
    f.change_bill(corrected['id'],VoidInput(request_id='void',reason='duplicate receipt'))
    assert f.overview(at(6))['finance_month']['expense_fen']==0


def test_golden_b_p04_p05_transfer_and_retry(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db); p=PortfolioService(svc.db)
    bank=manual(svc,'bank'); base=balance(f,bank,1000000)
    cash(svc,a,'opening','opening',500000,5)
    assert f.overview(at(5))['net_worth_fen']==1500000
    t=transfer(bank,a,base['id']); result=f.transfer(t); saved=state(svc)
    assert f.transfer(t)==result and state(svc)==saved
    with pytest.raises(Conflict): f.transfer(t.model_copy(update={'amount_fen':100}))
    r=f.overview(at(6)); securities=account(r,a)
    assert account(r,bank)['balance_fen']==800000 and securities['cash_fen']==700000
    assert r['net_worth_fen']==1500000 and r['finance_month']['income_fen']==r['finance_month']['expense_fen']==0
    v=p.valuation(a,at(6)); assert v['daily_pnl_fen']==v['cumulative_pnl_fen']==0 and v['external_flow_fen']==200000
    assert len([m for m in svc.list_records('cash')['items'] if m['transfer_id']==result['id']])==1
    print('B',800000,700000,1500000,v['daily_pnl_fen'])
    # Correction and void operate on both sides, not a cash-only correction.
    movement=next(m for m in svc.list_records('cash')['items'] if m['transfer_id']==result['id'])
    with pytest.raises(Conflict): f.change_cash(movement['id'],VoidInput(request_id='unsafe',reason='one side'))
    corrected=f.change_transfer(result['id'],transfer(bank,a,base['id'],req='correct-transfer',amount=150000))
    r=f.overview(at(6)); assert account(r,bank)['balance_fen']==850000 and account(r,a)['cash_fen']==650000 and r['net_worth_fen']==1500000
    f.change_transfer(corrected['id'],VoidInput(request_id='void-transfer',reason='not occurred'))
    r=f.overview(at(6)); assert account(r,bank)['balance_fen']==1000000 and account(r,a)['cash_fen']==500000 and r['net_worth_fen']==1500000
    assert p.valuation(a,at(6))['external_flow_fen']==0


def test_transfer_already_included_and_complex_void_rejected(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db)
    bank=manual(svc,'bank'); cash(svc,a,'opening','opening',500000,5)
    confirmed=balance(f,bank,800000,day=6,hour=10)
    t=f.transfer(transfer(bank,a,confirmed['id'],included=True))
    assert account(f.overview(at(6)),bank)['balance_fen']==800000
    assert f.overview(at(6))['net_worth_fen']==1500000
    before=state(svc)
    with pytest.raises(Conflict,match='manual reconciliation'): f.change_transfer(t['id'],VoidInput(request_id='void',reason='error'))
    assert state(svc)==before


def test_golden_c_f04_f05_asset_once(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db); p=PortfolioService(svc.db)
    bank=manual(svc,'bank'); credit=manual(svc,'credit','credit_card')
    balance(f,bank,800000,'b-bank'); balance(f,credit,300000,'b-credit')
    cash(svc,a,'opening','opening',1700000,5)
    svc.submit(trade(a,i,'buy',day=5,quantity=1000,price='10',fees_fen=0))
    quote(p,i,5,12); quote(p,i,6,12)
    r=f.overview(at(6)); s=account(r,a)
    assert s['cash_fen']==700000 and s['market_value_fen']==1200000 and s['equity_fen']==1900000
    assert (r['total_assets_fen'],r['total_liabilities_fen'],r['net_worth_fen'])==(2700000,300000,2400000)
    print('C',r['total_assets_fen'],r['total_liabilities_fen'],r['net_worth_fen'])
    before=state(svc)
    assert f.overview(at(6))==r and f.overview(at(6))==r
    assert f.overview(at(7,8))['net_worth_fen']==2400000 and state(svc)==before
    rev=r['ledger_revision']
    f.update_account(credit,AccountUpdate(request_id='close',name='closed credit',status='closed',expected_ledger_revision=rev))
    new=f.overview(at(6)); assert new['ledger_revision']==rev+1 and new['total_liabilities_fen']==300000 and new['net_worth_fen']==2400000


def test_golden_e_dividend_void_and_correction(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db); f=FinanceService(svc.db)
    cash(svc,a,'opening','opening',500000,5)
    d=p.company(CompanyInput(request_id='div',account_id=a,instrument_id=i,kind='dividend',amount_fen=10000,occurred_at=at(6,10),note='confirmed'))
    before=p.valuation(a,at(6)); assert before['cash_fen']==510000 and before['daily_pnl_fen']==before['cumulative_pnl_fen']==10000 and before['external_flow_fen']==0
    corrected=f.change_dividend(d['id'],CashCorrection(request_id='correct',amount_fen=7000,occurred_at=at(6,10),note='correct amount'))
    assert p.valuation(a,at(6))['daily_pnl_fen']==7000
    request=VoidInput(request_id='void',reason='mistaken dividend'); result=f.change_dividend(corrected['id'],request)
    after=p.valuation(a,at(6)); assert after['cash_fen']==500000 and after['daily_pnl_fen']==after['cumulative_pnl_fen']==after['dividends_fen']==after['external_flow_fen']==0
    assert f.change_dividend(corrected['id'],request)==result
    with svc.db.read() as c:
        assert c.execute('SELECT count(*) FROM company_events').fetchone()==(2,)
        assert c.execute("SELECT count(*) FROM cash_movements WHERE kind='reversal'").fetchone()==(2,)
    print('E',after['cash_fen'],after['daily_pnl_fen'],after['cumulative_pnl_fen'],after['external_flow_fen'])


def test_golden_f_cash_correction_all_flow_types(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db); f=FinanceService(svc.db)
    cash(svc,a,'opening','opening',500000,5)
    deposit=cash(svc,a,'deposit','deposit',200000,6)
    r=f.change_cash(deposit['id'],CashCorrection(request_id='correct',amount_fen=150000,occurred_at=at(6,8),note='actual bank receipt'))
    v=p.valuation(a,at(6)); assert v['cash_fen']==650000 and v['external_flow_fen']==150000 and v['daily_pnl_fen']==v['cumulative_pnl_fen']==0
    print('F',v['cash_fen'],v['external_flow_fen'],v['daily_pnl_fen'])
    withdrawal=cash(svc,a,'withdrawal','withdrawal',-20000,6,9)
    corrected=f.change_cash(withdrawal['id'],CashCorrection(request_id='withdrawal-correct',amount_fen=-10000,occurred_at=at(6,9),note='verified'))
    v=p.valuation(a,at(6)); assert v['cash_fen']==640000 and v['external_flow_fen']==140000 and v['daily_pnl_fen']==0
    f.change_cash(corrected['id'],VoidInput(request_id='void-withdrawal',reason='error'))
    adjustment=cash(svc,a,'adjust','adjustment',10000,6,11)
    f.change_cash(adjustment['id'],CashCorrection(request_id='adjust-correct',amount_fen=5000,occurred_at=at(6,11),note='verified balance'))
    v=p.valuation(a,at(6)); assert v['cash_fen']==655000 and v['adjustments_fen']==5000 and v['external_flow_fen']==150000 and v['daily_pnl_fen']==v['cumulative_pnl_fen']==0
    f.change_cash(r['id'],VoidInput(request_id='void-deposit',reason='not occurred'))
    v=p.valuation(a,at(6)); assert v['cash_fen']==505000 and v['external_flow_fen']==v['daily_pnl_fen']==v['cumulative_pnl_fen']==0
    before=state(svc)
    with pytest.raises(Conflict): f.change_cash(adjustment['id'],VoidInput(request_id='again',reason='already reversed'))
    assert state(svc)==before


def test_f01_month_boundaries_year_leap_and_correction(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db)
    bank=svc.create_account(AccountInput(request_id='historical-bank',name='bank',kind='bank',effective_at='2023-01-01T00:00:00+08:00'))['id']
    for j,t in enumerate(('2023-12-22T23:59:59+08:00','2023-12-23T00:00:00+08:00','2024-01-22T23:59:59+08:00','2024-01-23T00:00:00+08:00','2024-02-29T12:00:00+08:00','2024-03-22T23:59:59+08:00','2024-03-23T00:00:00+08:00')):
        bill(f,bank,req=str(j),amount=100*(j+1),occurred_at=datetime.fromisoformat(t))
    periods={date(2023,11,23):100,date(2023,12,23):500,date(2024,1,23):400,date(2024,2,23):1100,date(2024,3,23):700}
    for start,amount in periods.items(): assert f.month(start,at(6))['expense_fen']==amount
    year=f.year(2024,at(6)); assert len(year['items'])==12 and year['items'][1]['expense_fen']==1100
    assert month_start(date(2024,1,22))==date(2023,12,23)
    assert month_start(date(2024,2,29))==date(2024,2,23)


def test_f03_balance_history_stale_correction_and_unknown(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db)
    bank=manual(svc,'bank'); credit=manual(svc,'credit','credit_card'); excluded=manual(svc,'excluded','other_asset',included=False)
    cash(svc,a,'opening','opening',500000,5)
    assert f.overview(at(5))['net_worth_fen'] is None
    first=balance(f,bank,1000000); second=balance(f,bank,900000,'second',day=6,expected=first['id'])
    assert second['before_fen']==1000000 and second['after_fen']==900000
    assert account(f.overview(at(5)),bank)['balance_fen']==1000000
    assert account(f.overview(at(6)),bank)['balance_fen']==900000
    assert f.overview(at(6))['finance_month']['income_fen']==0 and f.overview(at(6))['finance_month']['expense_fen']==0
    before=state(svc)
    with pytest.raises(Conflict): balance(f,bank,2,'stale',day=6,expected=first['id'])
    assert state(svc)==before
    balance(f,credit,100000,'credit')
    assert f.overview(at(6))['net_worth_fen']==1300000  # excluded unknown doesn't poison total
    assert account(f.overview(at(6)),excluded)['balance_fen'] is None


def test_repayment_and_bank_transfer_no_expense(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db)
    cash(svc,a,'opening','opening',100000,5)
    bank=manual(svc,'bank'); credit=manual(svc,'credit','credit_card'); payment=manual(svc,'payment','payment')
    b=balance(f,bank,1000000,'bank'); c=balance(f,credit,300000,'credit'); w=balance(f,payment,0,'wallet')
    bill(f,credit,amount=5000)
    before=f.overview(at(6))
    f.transfer(TransferInput(request_id='repay',from_account_id=bank,to_account_id=credit,amount_fen=100000,occurred_at=at(6,9),note='repayment',from_included=False,to_included=False,from_balance_id=b['id'],to_balance_id=c['id']))
    r=f.overview(at(6)); assert account(r,credit)['balance_fen']==200000 and account(r,bank)['balance_fen']==900000
    assert r['net_worth_fen']==before['net_worth_fen'] and r['finance_month']['expense_fen']==5000
    f.transfer(TransferInput(request_id='wallet-transfer',from_account_id=bank,to_account_id=payment,amount_fen=50000,occurred_at=at(6,10),note='wallet topup',from_included=False,to_included=False,from_balance_id=account(r,bank)['balance_id'],to_balance_id=w['id']))
    r=f.overview(at(6)); assert account(r,payment)['balance_fen']==50000 and account(r,bank)['balance_fen']==850000 and r['net_worth_fen']==before['net_worth_fen']


def test_transfer_failure_and_cash_baseline_atomic(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db)
    bank=manual(svc,'bank'); base=balance(f,bank,1000000)
    cash(svc,a,'opening','opening',500000,6,10)
    before=state(svc)
    with pytest.raises(Conflict,match='opening balance'): f.transfer(transfer(bank,a,base['id']))
    assert state(svc)==before
    with svc.db.transaction() as c:
        c.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON cash_movements BEGIN SELECT RAISE(ABORT,'failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='failure'): f.transfer(transfer(bank,a,base['id'],day=7))
    assert state(svc)==before
    with svc.db.transaction() as c: c.execute('DROP TRIGGER injected_failure')
    t=f.transfer(transfer(bank,a,base['id'],day=7))
    r=f.overview(at(7)); current=account(r,bank)['balance_id']
    balance(f,bank,790000,'later',day=8,expected=current)
    before=state(svc)
    with pytest.raises(Conflict,match='later balance'): f.change_transfer(t['id'],VoidInput(request_id='void',reason='unsafe'))
    assert state(svc)==before


def test_finance_type_guards_and_http(cfg):
    with TestClient(create_app()) as client:
        assert client.post('/api/v1/finance/balances',json={'request_id':'x','account_id':'x','after_fen':100,'expected_balance_id':None,'occurred_at':at(5).isoformat(),'note':'test'}).status_code==503
    with open_database(cfg) as db:
        from backend.application.trading.service import TradeService, InstrumentInput
        svc=TradeService(db); f=FinanceService(db); p=PortfolioService(db)
        a=manual(svc,'bank'); i=svc.create_instrument(InstrumentInput(request_id='inst',code='510300',market='SH',kind='etf'))['id']
        before=state(svc)
        for op in (lambda:svc.submit(trade(a,i,'bad')),lambda:cash(svc,a,'bad','opening',10000,5),lambda:p.opening(OpeningInput(request_id='bad',account_id=a,instrument_id=i,quantity=100,cost_fen=100,fees_fen=0,occurred_at=at(5),note='bad')),lambda:p.company(CompanyInput(request_id='bad',account_id=a,instrument_id=i,kind='dividend',amount_fen=100,occurred_at=at(5),note='bad')),lambda:p.valuation(a,at(5))):
            with pytest.raises(TradeError,match='account type'): op()
        assert state(svc)==before
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=cfg)) as client:
        request={'request_id':'balance','account_id':a,'after_fen':10000,'expected_balance_id':None,'occurred_at':at(5).isoformat(),'note':'test'}
        r=client.post('/api/v1/finance/balances',json=request); assert r.status_code==200,r.text
        assert client.post('/api/v1/finance/balances',json=request).json()==r.json()
        assert client.get('/api/v1/finance/overview',params={'as_of':at(6).isoformat()}).json()['net_worth_fen']==10000
        assert client.get('/api/v1/finance/categories').status_code==200
    from backend.infrastructure.database import initialize_database
    sandbox=cfg.path.parent/'readonly.sqlite'
    identity=initialize_database(sandbox,role='sandbox',confirm=True,binding_path=cfg.binding_path)
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=replace(cfg,path=sandbox,expected_uuid=identity.database_uuid,mode='local'))) as client:
        assert client.post('/api/v1/finance/balances',json={**request,'request_id':'readonly'}).status_code==403


def test_iss012_and_networth_consistent_concurrent_snapshot(ledger,monkeypatch):
    svc,a,i=ledger; f=FinanceService(svc.db)
    bank=manual(svc,'bank'); old=balance(f,bank,1000000)
    cash(svc,a,'opening','opening',500000,5)
    initial=f.overview(at(6)); original=PortfolioService._valuation
    wrote=False
    def concurrent(self,conn,*args):
        nonlocal wrote
        if not wrote:
            wrote=True
            f.transfer(transfer(bank,a,old['id']))
        return original(self,conn,*args)
    monkeypatch.setattr(PortfolioService,'_valuation',concurrent)
    r=f.overview(at(6)); assert r['ledger_revision']==initial['ledger_revision'] and account(r,bank)['balance_fen']==1000000 and account(r,a)['cash_fen']==500000
    next_=f.overview(at(6)); assert next_['ledger_revision']==r['ledger_revision']+1 and account(next_,bank)['balance_fen']==800000 and account(next_,a)['cash_fen']==700000
    category=f.category(CategoryInput(request_id='new-category',name='books',kind='expense'))
    assert f.overview(at(6))['ledger_revision']==next_['ledger_revision']+1
    assert f.category(CategoryInput(request_id='new-category',name='books',kind='expense'))==category


def legacy05(tmp_path):
    # Migrate representative 0002 trade graph to real 0005 before exercising 0006.
    settings=legacy_trading(tmp_path)
    engine=create_engine(URL.create('sqlite',database=str(settings.path)))
    try:
        from alembic.config import Config
        from alembic import command
        with engine.begin() as conn:
            conn.exec_driver_sql('PRAGMA foreign_keys=ON')
            conn.exec_driver_sql('BEGIN IMMEDIATE')
            cfg=Config(); cfg.set_main_option('script_location',str(database.ROOT/'migrations')); cfg.attributes['connection']=conn
            command.upgrade(cfg,'0005')
            conn.exec_driver_sql('UPDATE system_metadata SET schema_version=5')
            conn.exec_driver_sql("INSERT INTO opening_positions VALUES ('open','a','i',100,20000,2,'2025-12-01T00:00:00.000000+00:00','legacy')")
            conn.exec_driver_sql("INSERT INTO trade_lots VALUES ('openlot',NULL,NULL,'ordinary',NULL,NULL,100,100,20000,2,'open','open')")
            conn.exec_driver_sql("INSERT INTO cash_movements VALUES ('divcash','a',NULL,NULL,100,'dividend','2026-01-06T04:00:00.000000+00:00',NULL,NULL,'legacy')")
            conn.exec_driver_sql("INSERT INTO company_events VALUES ('div','a','i','dividend','2026-01-06T04:00:00.000000+00:00','divcash','legacy')")
            conn.exec_driver_sql("UPDATE trade_executions SET correction_of='old' WHERE id='sell'")
            assert conn.exec_driver_sql('PRAGMA foreign_key_check').fetchall()==[]
    finally: engine.dispose()
    return settings


def test_0005_to_0006_preserves_all_facts_with_fk(tmp_path):
    settings=legacy05(tmp_path)
    tables=('trade_executions','trade_lots','lot_allocations','cash_movements','opening_positions','company_events','ledger_state')
    with closing(sqlite3.connect(settings.path)) as c:
        before={t:c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall() for t in tables}
        accounts=c.execute('SELECT * FROM accounts').fetchall()
    dest=tmp_path/'pre-finance'; upgrade_database(settings,dest,confirm=True)
    with open_database(settings) as db,db.read() as c:
        assert c.execute('SELECT version_num FROM alembic_version').fetchone()==(REVISION,)
        assert c.execute('SELECT schema_version FROM system_metadata').fetchone()==(SCHEMA_VERSION,)
        assert c.execute('SELECT id,name,kind,currency,balance_mode,status FROM accounts').fetchall()==accounts
        for t in tables: assert c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall()==before[t]
        assert c.execute('PRAGMA integrity_check').fetchone()==('ok',) and c.execute('PRAGMA foreign_key_check').fetchall()==[] and c.execute('PRAGMA foreign_keys').fetchone()==(1,)
    assert json.loads((dest/'manifest.json').read_text())['schema_version']==5
    with closing(sqlite3.connect(dest/'database.sqlite')) as c:
        for t in tables: assert c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall()==before[t]


def test_0006_actual_ddl_failure_rolls_back(tmp_path,monkeypatch):
    settings=legacy05(tmp_path)
    with closing(sqlite3.connect(settings.path)) as c: before=list(c.iterdump())
    original=database.create_engine
    def factory(*args,**kwargs):
        engine=original(*args,**kwargs)
        @event.listens_for(engine,'after_cursor_execute')
        def fail(conn,cursor,statement,parameters,context,executemany):
            if statement=='DROP TABLE accounts':
                assert cursor.connection.execute('PRAGMA foreign_keys').fetchone()==(1,)
                raise RuntimeError('injected 0006 after account drop')
        return engine
    monkeypatch.setattr(database,'create_engine',factory)
    dest=tmp_path/'pre-failed';
    with pytest.raises(RuntimeError,match='injected 0006'): upgrade_database(settings,dest,confirm=True)
    assert (dest/'manifest.json').is_file()
    with closing(sqlite3.connect(settings.path)) as c:
        assert list(c.iterdump())==before and c.execute('PRAGMA integrity_check').fetchone()==('ok',) and c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_historical_already_included_gap_and_securities_withdrawal(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db); p=PortfolioService(svc.db)
    bank=manual(svc,'bank'); base=balance(f,bank,1000000)
    cash(svc,a,'opening','opening',500000,5)
    later=balance(f,bank,800000,'confirmed-later',day=6,hour=10,expected=base['id'])
    f.transfer(transfer(bank,a,later['id'],included=True))
    gap=f.overview(at(6,9,30)); assert gap['net_worth_fen'] is None
    assert account(gap,bank)['balance_fen'] is None and 'transfer_inclusion_snapshot_not_yet_effective' in account(gap,bank)['reasons']
    assert f.overview(at(6))['net_worth_fen']==1500000
    f.transfer(TransferInput(request_id='out',from_account_id=a,to_account_id=bank,amount_fen=100000,occurred_at=at(7,9),note='verified withdrawal',to_included=False,to_balance_id=later['id']))
    r=f.overview(at(7)); assert account(r,bank)['balance_fen']==900000 and account(r,a)['cash_fen']==600000 and r['net_worth_fen']==1500000
    assert p.valuation(a,at(7))['external_flow_fen']==-100000 and p.valuation(a,at(7))['daily_pnl_fen']==0


def test_cross_period_bill_correction_income_fees_and_history_filter(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db); bank=manual(svc,'bank')
    before=datetime.fromisoformat('2026-01-22T23:59:00+08:00')
    after=datetime.fromisoformat('2026-01-23T00:00:00+08:00')
    cutoff=datetime.fromisoformat('2026-01-24T12:00:00+08:00')
    old=bill(f,bank,occurred_at=before)
    bill(f,bank,req='salary',amount=100000,occurred_at=after,kind='income',category='salary')
    bill(f,bank,req='fee',amount=500,occurred_at=after,kind='fee',category='fees')
    correction=BillInput(request_id='move',account_id=bank,kind='expense',category_id='food',amount_fen=15000,occurred_at=after,note='date corrected')
    f.change_bill(old['id'],correction)
    assert f.month(date(2025,12,23),cutoff)['expense_fen']==0
    r=f.month(date(2026,1,23),cutoff)
    assert (r['income_fen'],r['expense_fen'],r['fees_fen'],r['net_fen'])==(100000,15000,500,84500)
    assert r['categories']=={'salary':100000,'fees':500,'food':15000}
    listed=f.records('bills',account_id=bank,from_at=after,to_at=cutoff)
    assert listed['total']==3 and all(x['occurred_at']==utc(after) for x in listed['items'])
    with pytest.raises(Conflict): f.records('bills',expected_revision=listed['ledger_revision']-1)


def test_two_securities_consistent_quote_revision_and_missing_prices(ledger,monkeypatch):
    from backend.application.trading.service import InstrumentInput
    svc,a,i=ledger; f=FinanceService(svc.db); p=PortfolioService(svc.db)
    second=svc.create_account(AccountInput(request_id='second-account',name='second',effective_at='2000-01-01T00:00:00+08:00'))['id']
    j=svc.create_instrument(InstrumentInput(request_id='second-instrument',code='510500',market='SH',kind='etf'))['id']
    cash(svc,a,'opening1','opening',500000,5); cash(svc,second,'opening2','opening',500000,5)
    svc.submit(trade(a,i,'buy1',quantity=100,price='10',fees_fen=0)); svc.submit(trade(second,j,'buy2',quantity=100,price='10',fees_fen=0))
    quote(p,i,5,10,req='i-prev'); quote(p,i,6,12,req='i-now'); quote(p,j,5,10,req='j-prev')
    before=f.overview(at(6)); assert before['net_worth_fen'] is None and account(before,second)['cash_fen']==400000 and account(before,second)['market_value_fen'] is None
    original=PortfolioService._valuation; wrote=False
    def write_quote(self,conn,*args):
        nonlocal wrote
        if not wrote:
            wrote=True; quote(p,j,6,11,req='j-now')
        return original(self,conn,*args)
    monkeypatch.setattr(PortfolioService,'_valuation',write_quote)
    r=f.overview(at(6)); assert r['quote_revision']==before['quote_revision'] and r['net_worth_fen'] is None
    fresh=f.overview(at(6)); assert fresh['quote_revision']==r['quote_revision']+1 and fresh['net_worth_fen']==1030000
    assert fresh['securities_equity_fen']==1030000 and fresh['securities_market_value_fen']==230000
    assert f.overview(at(6),quote_revision=r['quote_revision'])['net_worth_fen'] is None


def test_future_facts_and_same_time_manual_balance_restatement(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db,clock=lambda:at(6,12)); bank=manual(svc,'bank')
    first=balance(f,bank,1000000); revised=balance(f,bank,900000,'restate',day=5,expected=first['id'])
    assert revised['before_fen']==1000000 and account(f.overview(at(5)),bank)['balance_fen']==900000
    assert len(f.records('balances')['items'])==2
    before=state(svc)
    for op in (lambda:balance(f,bank,10,'future',day=7,expected=revised['id']),
               lambda:bill(f,bank,req='futurebill',occurred_at=at(7)),
               lambda:f.transfer(transfer(bank,a,revised['id'],day=7))):
        with pytest.raises(TradeError,match='future'): op()
    assert state(svc)==before


def test_legacy_unpaired_transfer_not_reported_as_verified_networth(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db); bank=manual(svc,'bank'); balance(f,bank,1000000)
    cash(svc,a,'opening','opening',500000,5)
    with svc.db.transaction() as c:
        c.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?,?,?)',('legacy',a,None,'legacy-transfer',100000,'deposit',utc(at(6,9)),None,None,'old unpaired'))
    r=f.overview(at(6)); assert r['net_worth_fen'] is None and a+':legacy_unpaired_transfer' in r['reasons']
    assert account(r,a)['cash_fen']==600000
    with pytest.raises(Conflict,match='whole'): f.change_cash('legacy',VoidInput(request_id='orphan-void',reason='cannot reconcile'))


def test_transfer_correction_failure_retains_original_pair(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db); bank=manual(svc,'bank'); base=balance(f,bank,1000000)
    cash(svc,a,'opening','opening',500000,5)
    original=f.transfer(transfer(bank,a,base['id']))
    before=state(svc)
    with svc.db.transaction() as c:
        c.execute("CREATE TRIGGER reject_replacement BEFORE INSERT ON cash_movements WHEN NEW.kind='deposit' BEGIN SELECT RAISE(ABORT,'injected correction failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='injected correction failure'):
        f.change_transfer(original['id'],transfer(bank,a,base['id'],req='correction',amount=150000))
    assert state(svc)==before
    assert f.overview(at(6))['net_worth_fen']==1500000


def test_transfer_void_rejects_dependent_inclusion_evidence(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db); bank=manual(svc,'bank')
    base=balance(f,bank,1000000); cash(svc,a,'opening','opening',500000,5)
    original=f.transfer(transfer(bank,a,base['id']))
    # Same confirmed snapshot is cited by another earlier transfer, which does
    # not create a new adjustment. Removing that snapshot would break evidence.
    latest=account(f.overview(at(6)),bank)['balance_id']
    f.transfer(TransferInput(request_id='earlier',from_account_id=bank,to_account_id=a,amount_fen=10000,occurred_at=at(6,8),note='earlier statement item',from_included=True,from_balance_id=latest))
    before=state(svc)
    with pytest.raises(Conflict,match='transfer facts depend'):
        f.change_transfer(original['id'],VoidInput(request_id='unsafe',reason='cannot invalidate evidence'))
    assert state(svc)==before


def test_malformed_legacy_cash_reversal_refuses_migration(tmp_path):
    settings=legacy05(tmp_path)
    with closing(sqlite3.connect(settings.path)) as c,c:
        c.execute("UPDATE cash_movements SET amount_fen=-4999 WHERE id='reversal'")
        before=list(c.iterdump())
    dest=tmp_path/'malformed-backup'
    with pytest.raises(RuntimeError,match='reversal evidence is inconsistent'):
        upgrade_database(settings,dest,confirm=True)
    assert (dest/'manifest.json').is_file()
    with closing(sqlite3.connect(settings.path)) as c:
        assert list(c.iterdump())==before and c.execute('SELECT version_num FROM alembic_version').fetchone()==('0005',)
