"""03-pnl synthetic golden cases; never touch a real database."""
from datetime import date, datetime
from contextlib import closing
from dataclasses import replace
import sqlite3
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from fastapi.testclient import TestClient
from backend.application.trading.service import CashInput, Conflict, TradeError, TradeService, AccountInput, InstrumentInput
from backend.application.portfolio import PortfolioService, QuoteInput, OpeningInput, CompanyInput
from backend.infrastructure import database
from backend.infrastructure.database import REVISION, Identity, migrate_schema, upgrade_database, open_database, adopt_database, initialize_database
from backend.main import create_app
from backend.config import AppConfig
from test_trading import cfg, ledger, trade, data, snapshot
from test_database_upgrade import cfg_for


def at(day, hour=15, minute=0):
    return datetime.fromisoformat(f'2026-01-{day:02}T{hour:02}:{minute:02}:00+08:00')


def cash(svc,a,req,kind,amount,day,hour=8):
    return svc.cash(CashInput(request_id=req,account_id=a,kind=kind,amount_fen=amount,occurred_at=at(day,hour),note='verified statement'))


def quote(p,i,day,price,*,kind='close',quality='valid',hour=15,minute=0,req=None):
    return p.quote(QuoteInput(request_id=req or f'q-{day}-{price}',instrument_id=i,price=str(price),source='synthetic_test',observed_at=at(day,hour,minute),trading_day=date(2026,1,day),kind=kind,quality=quality))


def test_golden_a_p02(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db)
    cash(svc,a,'initial','opening',3000000,5)
    svc.submit(trade(a,i,'yesterday',day=5,quantity=1000,price='10',fees_fen=0))
    svc.submit(trade(a,i,'buy',day=6,quantity=100,price='11',fees_fen=500))
    svc.submit(trade(a,i,'sell',day=6,side='sell',quantity=200,price='11.50',fees_fen=500,executed_at=at(6,10)))
    quote(p,i,5,10); quote(p,i,6,12)
    r=p.valuation(a,at(6),date(2026,1,5))
    assert r['positions'][0]['quantity']==900
    assert r['market_value_fen']==1080000 and r['daily_pnl_fen']==199000
    assert r['equity_pnl_fen']==199000 and r['reconciliation_difference_fen']==0
    assert r['equity_fen']==3199000 and r['cumulative_pnl_fen']==199000
    assert r['realized_fen']==29500 and r['unrealized_fen']==169500
    assert r['quality']=='valid'
    before=snapshot(svc)
    assert all(p.valuation(a,at(6),date(2026,1,5))==r for _ in range(5))
    assert snapshot(svc)==before
    print('A',r['positions'][0]['quantity'],r['market_value_fen'],r['daily_pnl_fen'],r['equity_pnl_fen'])


def test_golden_b_p03(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db)
    cash(svc,a,'initial','opening',2000000,5)
    svc.submit(trade(a,i,'buy',day=6,price='10',fees_fen=500))
    svc.submit(trade(a,i,'sell',day=6,side='sell',price='11',fees_fen=500,executed_at=at(6,10)))
    r=p.valuation(a,at(6),date(2026,1,5))
    assert r['positions']==[] and r['realized_fen']==9000 and r['daily_pnl_fen']==9000 and r['equity_pnl_fen']==9000
    print('B',len(r['positions']),r['realized_fen'],r['daily_pnl_fen'])


def test_golden_c_d_p04_p08(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db)
    cash(svc,a,'initial','opening',2000000,5)
    cash(svc,a,'deposit','deposit',1000000,6)
    r=p.valuation(a,at(6)); assert r['equity_fen']==3000000 and r['daily_pnl_fen']==r['equity_pnl_fen']==0
    print('C',r['equity_fen'],r['daily_pnl_fen'])
    event=CompanyInput(request_id='div',account_id=a,instrument_id=i,kind='dividend',amount_fen=10000,occurred_at=at(6,10),note='confirmed net cash')
    assert p.company(event)==p.company(event)
    r=p.valuation(a,at(6)); assert r['cash_fen']==3010000 and r['daily_pnl_fen']==r['equity_pnl_fen']==10000 and r['external_flow_fen']==1000000
    assert len([x for x in data(svc,'cash') if x['kind']=='dividend'])==1
    print('D',r['cash_fen'],r['daily_pnl_fen'],r['external_flow_fen'])
    cash(svc,a,'adjust','adjustment',12345,6,11)
    r=p.valuation(a,at(6)); assert r['daily_pnl_fen']==r['equity_pnl_fen']==r['cumulative_pnl_fen']==10000
    p.company(CompanyInput(request_id='split',account_id=a,instrument_id=i,kind='split',occurred_at=at(6,12),note='unresolved split'))
    r=p.valuation(a,at(6)); assert r['daily_pnl_fen'] is None and r['equity_fen'] is None and 'unresolved_company_action' in r['reasons']


def test_golden_e_iss008_replay_and_void(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db)
    cash(svc,a,'initial','opening',3000000,5)
    later=svc.submit(trade(a,i,'late',day=8,quantity=100,price='12',fees_fen=7))
    lot=data(svc,'lots')[0]['id']
    sell=svc.submit(trade(a,i,'sale',day=9,side='sell',quantity=80,price='15',fees_fen=3))
    early=svc.submit(trade(a,i,'early',day=6,quantity=60,price='10',fees_fen=5))
    assert early['status']=='pending'
    old_cash={r['id'] for r in data(svc,'cash')}
    confirmed=svc.confirm_pending(early['id'],'confirm')
    assert confirmed['id']==early['id'] and confirmed['status']=='effective'
    assert old_cash < {r['id'] for r in data(svc,'cash')}
    assert len(data(svc,'cash'))==4 and next(x for x in data(svc,'lots') if x['open_execution_id']==later['id'])['id']==lot
    alloc=[x for x in data(svc,'allocations') if x['sell_execution_id']==sell['id']]
    assert sorted(x['quantity'] for x in alloc)==[20,60] and sum(x['cost_fen'] for x in alloc)==84000
    quote(p,i,6,10); quote(p,i,8,12); quote(p,i,9,15)
    r=p.valuation(a,at(6)); assert r['positions'][0]['quantity']==60
    r=p.valuation(a,at(9),date(2026,1,8)); assert r['positions'][0]['quantity']==80 and r['realized_fen']==35991
    print('E',confirmed['status'],60,r['positions'][0]['quantity'],len(data(svc,'cash')))
    correction=svc.correct(early['id'],trade(a,i,'correct',day=6,quantity=70,price='10',fees_fen=5,note='verified correction'))
    assert svc.get(early['id'])['status']=='reversed' and correction['status']=='effective'
    r2=p.valuation(a,at(9),date(2026,1,8)); assert r2['positions'][0]['quantity']==90 and r2['ledger_revision']>r['ledger_revision']
    svc.void(correction['id'],'void','erroneous execution')
    assert p.valuation(a,at(9),date(2026,1,8))['positions'][0]['quantity']==20
    before=snapshot(svc)
    with pytest.raises(Conflict): svc.void(later['id'],'void-unsafe','would leave sale unfunded')
    assert snapshot(svc)==before


def test_specified_reference_replay_failure_atomic(ledger):
    svc,a,i=ledger
    buy=svc.submit(trade(a,i,'late',day=8,quantity=100)); lot=data(svc,'lots')[0]['id']
    svc.submit(trade(a,i,'specified',day=9,side='sell',quantity=20,allocations={lot:20}))
    early=svc.submit(trade(a,i,'early',day=6,quantity=100)); svc.confirm_pending(early['id'],'confirm')
    assert data(svc,'allocations')[0]['lot_id']==lot
    before=snapshot(svc)
    with pytest.raises(Conflict): svc.correct(buy['id'],trade(a,i,'correction',day=8,quantity=99,note='cannot change referenced lot'))
    assert snapshot(svc)==before


def test_iss009_cash_boundary_and_iss010_future(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db)
    cash(svc,a,'initial','opening',1000000,5)
    with pytest.raises(Conflict): cash(svc,a,'old-deposit','deposit',200000,4)
    old=svc.submit(trade(a,i,'old-buy',executed_at='2025-12-31T09:30:00+08:00',trading_day='2025-12-31')); assert old['status']=='pending' and old['pending_reason']=='before_or_at_cash_baseline'
    with pytest.raises(Conflict): svc.confirm_pending(old['id'],'confirm')
    assert p.valuation(a,at(6))['cash_fen']==1000000 and p.valuation(a,at(6))['equity_fen'] is None
    before=snapshot(svc)
    fixed=TradeService(svc.db,clock=lambda:at(6,10))
    with pytest.raises(TradeError,match='future'): fixed.submit(trade(a,i,'future',day=7))
    with pytest.raises(TradeError,match='future'): cash(fixed,a,'future-cash','deposit',100,7)
    assert snapshot(svc)==before


def test_p06_p10_missing_stale_future_quotes_and_opening(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db)
    cash(svc,a,'initial','opening',2000000,5)
    opening=p.opening(OpeningInput(request_id='position',account_id=a,instrument_id=i,quantity=100,cost_fen=100000,fees_fen=5,occurred_at=at(5,9),note='verified opening holdings'))
    assert data(svc,'trades')==[] and len(data(svc,'cash'))==1
    quote(p,i,5,10,kind='intraday',hour=14)
    quote(p,i,6,12,kind='intraday',hour=10)
    assert p.valuation(a,at(6,10),date(2026,1,5))['daily_pnl_fen'] is None
    r=p.valuation(a,at(6,11),date(2026,1,5)); assert r['market_value_fen'] is None and r['equity_fen'] is None
    quote(p,i,5,10,req='confirmed-close'); quote(p,i,6,12,req='future-close')
    assert p.valuation(a,at(6,11),date(2026,1,5))['market_value_fen'] is None
    r=p.valuation(a,at(6),date(2026,1,5)); assert r['positions'][0]['lots'][0]['lot_id']==opening['lot_id'] and r['daily_pnl_fen']==20000 and r['cumulative_pnl_fen'] is None
    svc.submit(trade(a,i,'sell',day=7,side='sell',quantity=20,price='12',fees_fen=1))
    assert p.valuation(a,at(6),date(2026,1,5))['positions'][0]['quantity']==100
    assert p.valuation(a,at(7),date(2026,1,6))['positions'][0]['quantity']==80
    assert data(svc,'lots')[0]['remaining_qty']==80


def test_p09_consistent_snapshot_and_master_revision(ledger,monkeypatch):
    svc,a,i=ledger; p=PortfolioService(svc.db)
    cash(svc,a,'initial','opening',2000000,5); svc.submit(trade(a,i,'buy',day=6,quantity=100,price='10',fees_fen=0)); quote(p,i,6,10)
    before=p.valuation(a,at(6)); original=p._lots; changed=False
    def concurrent(conn,account,cutoff):
        nonlocal changed
        if not changed:
            changed=True
            svc.submit(trade(a,i,'concurrent',day=6,quantity=100,price='10',fees_fen=0))
            quote(p,i,6,11)
            svc.create_instrument(InstrumentInput(request_id='newmaster',code='600001',market='SH',kind='stock'))
        return original(conn,account,cutoff)
    monkeypatch.setattr(p,'_lots',concurrent)
    assert p.valuation(a,at(6))==before
    after=p.valuation(a,at(6)); assert after['ledger_revision']==before['ledger_revision']+2 and after['quote_revision']==before['quote_revision']+1 and after['positions'][0]['quantity']==200


def test_pnl_api_modes(cfg):
    with TestClient(create_app()) as client:
        assert client.get('/api/v1/portfolio/a',params={'as_of':at(6).isoformat()}).status_code==503
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=cfg)) as client:
        a=client.post('/api/v1/accounts',json={'request_id':'acc','name':'test','effective_at':'2000-01-01T00:00:00+08:00'}).json()['id']
        assert client.get('/api/v1/portfolio/'+a,params={'as_of':at(6).isoformat()}).status_code==200
        assert client.get('/api/v1/portfolio/'+a,params={'as_of':'2026-01-06T15:00:00'}).status_code==422
    sandbox=cfg.path.parent/'sandbox.sqlite'
    identity=initialize_database(sandbox,role='sandbox',confirm=True,binding_path=cfg.binding_path)
    local=replace(cfg,path=sandbox,expected_uuid=identity.database_uuid,mode='local')
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=local)) as client:
        assert client.post('/api/v1/quotes',json={'request_id':'q','instrument_id':'missing','price':'1','source':'test','observed_at':at(6).isoformat(),'trading_day':'2026-01-06','kind':'close','quality':'valid'}).status_code==403


def legacy_trading(tmp_path):
    path=tmp_path/'legacy.sqlite'
    identity=replace(Identity.new('formal'),schema_version=2)
    engine=create_engine(URL.create('sqlite',database=str(path)))
    try: migrate_schema(engine,identity,target='0002')
    finally: engine.dispose()
    # Representative old data, including a reversal self-FK and sell allocation.
    with closing(sqlite3.connect(path)) as conn,conn:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute("INSERT INTO accounts VALUES ('a','account','securities','CNY','ledger','active')")
        conn.execute("INSERT INTO instruments VALUES ('i','510300','SH','CNY',4,'etf')")
        from backend.application.trading.service import canonical,dump,utc
        for id_,side,qty,status,day in [('buy','buy',100,'effective',5),('sell','sell',10,'effective',6),('old','sell',5,'reversed',6)]:
            v=canonical(trade('a','i',id_,side=side,quantity=qty,day=day,price='10',fees_fen=0))
            conn.execute('INSERT INTO trade_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(id_,id_,'a','i',side,qty,100000,0,qty*1000,v['executed_at'],v['trading_day'],None,None,status,None,'',None,'ordinary',None,None,None,dump(v)))
        conn.execute("INSERT INTO trade_lots VALUES ('lot','buy',NULL,'ordinary',NULL,NULL,100,90,90000,0,'open')")
        conn.execute("INSERT INTO lot_allocations VALUES ('alloc','sell','lot',10,10000,0,'fifo')")
        conn.execute("INSERT INTO lot_allocations VALUES ('old-alloc','old','lot',5,5000,0,'fifo')")
        for id_,execution,amount,kind,day,reversal in [('initial',None,1000000,'opening',4,None),('buy-cash','buy',-100000,'trade',5,None),('sell-cash','sell',10000,'trade',6,None),('old-cash','old',5000,'trade',6,None),('reversal','old',-5000,'reversal',6,'old-cash')]:
            conn.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?,?,?)',(id_,'a',execution,None,amount,kind,utc(at(day)),reversal,None,''))
        conn.execute('UPDATE ledger_state SET ledger_revision=7')
    cfg=cfg_for(path,identity,tmp_path/'official.json'); adopt_database(cfg,confirm=True)
    return cfg


def test_0002_to_0003_real_upgrade_with_facts(tmp_path):
    cfg=legacy_trading(tmp_path)
    with closing(sqlite3.connect(cfg.path)) as conn:
        before={table:conn.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall() for table in ('trade_executions','trade_lots','lot_allocations','cash_movements','ledger_state')}
    backup=tmp_path/'backup'; upgrade_database(cfg,backup,confirm=True)
    with open_database(cfg) as db,db.read() as conn:
        for table,expected in before.items():
            actual=conn.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()
            assert actual==([r+(None,) for r in expected] if table=='trade_lots' else expected)
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[] and conn.execute('PRAGMA integrity_check').fetchone()==('ok',)
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone()==(REVISION,)
    with closing(sqlite3.connect(backup/'database.sqlite')) as conn:
        assert conn.execute('SELECT schema_version FROM system_metadata').fetchone()==(2,)
        assert conn.execute('SELECT * FROM cash_movements ORDER BY rowid').fetchall()==before['cash_movements']


def test_0003_migration_failure_restores_0002(tmp_path,monkeypatch):
    cfg=legacy_trading(tmp_path)
    with closing(sqlite3.connect(cfg.path)) as conn:
        before=list(conn.iterdump())
    original=database.create_engine
    def fail_engine(*args,**kwargs):
        engine=original(*args,**kwargs)
        @event.listens_for(engine,'after_cursor_execute')
        def fail(conn,cursor,statement,parameters,context,executemany):
            if 'ALTER TABLE new_cash RENAME TO cash_movements' in statement:
                raise RuntimeError('injected DDL failure after cash table replacement')
        return engine
    monkeypatch.setattr(database,'create_engine',fail_engine)
    with pytest.raises(RuntimeError,match='injected DDL failure'):
        upgrade_database(cfg,tmp_path/'backup',confirm=True)
    with closing(sqlite3.connect(cfg.path)) as conn:
        assert list(conn.iterdump())==before
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert conn.execute('PRAGMA integrity_check').fetchone()==('ok',)


def test_p01_reopen_and_recalculate_does_not_write(cfg):
    with open_database(cfg) as db:
        svc=TradeService(db)
        a=svc.create_account(AccountInput(request_id='account',name='account',effective_at='2000-01-01T00:00:00+08:00'))['id']
        i=svc.create_instrument(InstrumentInput(request_id='instrument',code='510300',market='SH',kind='etf'))['id']
        cash(svc,a,'opening','opening',1000000,5)
        svc.submit(trade(a,i,'buy',day=6,quantity=10,price='10',fees_fen=3))
        p=PortfolioService(db); quote(p,i,6,12)
        expected=p.valuation(a,at(6)); assert expected['daily_pnl_fen']==1997
        with db.read() as conn: before=list(conn.iterdump())
    with open_database(cfg) as db:
        for _ in range(3): assert PortfolioService(db).valuation(a,at(6))==expected
        with db.read() as conn: assert list(conn.iterdump())==before


def test_historical_sell_replay_cash_once_and_cost_conservation(ledger):
    svc,a,i=ledger
    cash(svc,a,'opening','opening',1000000,5)
    buy=svc.submit(trade(a,i,'buy',day=6,quantity=100,price='10',fees_fen=7))
    lot=data(svc,'lots')[0]['id']
    later=svc.submit(trade(a,i,'later-sell',day=9,side='sell',quantity=50,price='12',fees_fen=3))
    earlier=svc.submit(trade(a,i,'earlier-sell',day=7,side='sell',quantity=30,price='11',fees_fen=2))
    assert earlier['status']=='pending'
    svc.confirm_pending(earlier['id'],'confirm')
    allocations=data(svc,'allocations'); remaining=data(svc,'lots')[0]
    assert remaining['id']==lot and remaining['open_execution_id']==buy['id'] and remaining['remaining_qty']==20
    assert sum(x['quantity'] for x in allocations)==80
    assert sum(x['cost_fen'] for x in allocations)+remaining['remaining_cost_fen']==100000
    assert sum(x['buy_fees_fen'] for x in allocations)+remaining['remaining_fees_fen']==7
    assert len([r for r in data(svc,'cash') if r['execution_id']==later['id']])==1
    bad=svc.submit(trade(a,i,'too-early-sale',day=7,side='sell',quantity=30,price='11'))
    before=snapshot(svc)
    with pytest.raises(Conflict): svc.confirm_pending(bad['id'],'bad-confirm')
    assert snapshot(svc)==before


def test_legacy_prebaseline_cash_is_not_double_counted(tmp_path):
    cfg=legacy_trading(tmp_path)
    from backend.application.trading.service import utc
    with closing(sqlite3.connect(cfg.path)) as conn,conn:
        conn.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?,?,?)',('prebaseline','a',None,None,200000,'deposit',utc(at(3)),None,'old-deposit','legacy ambiguous'))
    upgrade_database(cfg,tmp_path/'backup',confirm=True)
    with open_database(cfg) as db:
        result=PortfolioService(db).valuation('a',at(6))
        assert result['cash_fen']==910000 and result['cash_quality']=='derived_unverified'
        assert result['equity_fen'] is None and result['daily_pnl_fen'] is None
        assert 'cash_fact_conflicts_with_baseline' in result['reasons']
