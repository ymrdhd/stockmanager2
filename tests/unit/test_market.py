"""04 market/calendar regression cases: fixed clocks, fixtures, isolated databases."""
import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
import sqlite3
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL

from backend.application.market import MarketService
from backend.application.market_calendar import CalendarUnavailable, SHANGHAI, market_window, session_bounds
from backend.application.portfolio import CompanyInput, PortfolioService, QuoteInput
from backend.application.trading.service import InstrumentInput, TradeError, TradeService, utc
from backend.config import AppConfig
from backend.infrastructure import database
from backend.infrastructure.database import (
    DatabaseError, Identity, REVISION, adopt_database, initialize_database, migrate_schema, open_database, upgrade_database,
)
from backend.infrastructure.market.eastmoney import EastmoneyProvider, MarketDataError
from backend.main import create_app
from test_database_upgrade import cfg_for
from test_pnl import at, cash, quote
from test_trading import cfg, data, ledger, snapshot, trade


def test_iss013_real_calendar_and_coverage():
    # SSE Spring Festival closure: Feb 15–23, 2026; trading resumes Feb 24.
    before = market_window(datetime(2026,2,23,12,tzinfo=SHANGHAI))
    after = market_window(datetime(2026,2,24,10,tzinfo=SHANGHAI))
    assert before.phase == 'closed' and before.latest_completed == date(2026,2,13)
    assert after.phase == 'trading' and after.previous == date(2026,2,13)
    assert market_window(at(5,9)).previous == date(2025,12,31)
    assert market_window(at(6,12)).phase == 'break'
    assert market_window(at(6,15)).latest_completed == date(2026,1,6)
    with pytest.raises(CalendarUnavailable):
        market_window(datetime(2027,1,4,10,tzinfo=SHANGHAI))


def test_iss013_missing_actual_previous_close(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db)
    cash(svc,a,'base','opening',1000000,5)
    svc.submit(trade(a,i,'buy',day=5,quantity=100,price='10',fees_fen=0))
    quote(p,i,5,10); quote(p,i,7,12)
    r=p.valuation(a,at(7))
    assert r['previous_trading_day']=='2026-01-06'
    assert r['market_value_fen']==120000 and r['daily_pnl_fen'] is None
    assert r['quality']!='valid' and any('previous_close_unavailable' in x for x in r['reasons'])
    with pytest.raises(TradeError,match='exchange calendar'):
        p.valuation(a,at(7),date(2026,1,5))
    assert p.valuation(a,at(7),date(2026,1,6))==r


def test_iss014_weekend_preopen_and_dividend(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db)
    cash(svc,a,'base','opening',1000000,5)
    svc.submit(trade(a,i,'buy',day=7,quantity=100,price='10',fees_fen=0))
    quote(p,i,8,10); quote(p,i,9,12)
    r=p.valuation(a,at(10,12))
    assert r['market_phase']=='closed' and r['valuation_trading_day']=='2026-01-09'
    assert r['market_value_fen']==120000 and r['equity_fen']==1020000
    assert r['daily_pnl_fen']==0 and r['latest_trading_day_pnl_fen']==20000
    assert r['latest_completed_trading_day']=='2026-01-09' and r['quality']=='valid'
    p.company(CompanyInput(request_id='div',account_id=a,instrument_id=i,kind='dividend',
        occurred_at=at(10,13),amount_fen=10000,note='confirmed weekend dividend'))
    r=p.valuation(a,at(10,14))
    assert r['daily_pnl_fen']==r['equity_pnl_fen']==10000 and r['latest_trading_day_pnl_fen']==20000
    assert r['external_flow_fen']==0 and r['equity_fen']==1030000
    r=p.valuation(a,at(12,9))
    assert r['market_phase']=='preopen' and r['equity_fen']==1030000
    assert r['daily_pnl_fen']==0 and r['latest_trading_day_pnl_fen']==20000
    assert p.current_price(i,at(12,9))['quote']['trading_day']=='2026-01-09'


def test_iss014_lunch_and_after_close_quality(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db)
    quote(p,i,5,10)
    quote(p,i,6,11,kind='intraday',hour=11,minute=25)
    assert p.current_price(i,at(6,12))['quality']=='valid'
    assert p.current_price(i,at(6,14))['quality']=='stale'
    # A same-day intraday reference, however fresh, cannot become a confirmed close.
    quote(p,i,6,12,kind='intraday',hour=14,minute=59)
    r=p.current_price(i,at(6,15,1))
    assert r['quality']=='stale' and r['quote']['kind']=='intraday'
    with pytest.raises(TradeError,match='recess'):
        quote(p,i,6,12,kind='intraday',hour=12)
    with pytest.raises(TradeError,match='not a mainland trading session'):
        quote(p,i,10,12)


def test_iss015_late_close_revision_and_availability(ledger):
    svc,a,i=ledger
    cash(svc,a,'base','opening',1000000,5)
    svc.submit(trade(a,i,'buy',day=5,quantity=100,price='10',fees_fen=0))
    p=PortfolioService(svc.db,clock=lambda:at(8,16))
    def historical(req,day,price,observed=None,source='confirmed_fixture'):
        return p.quote(QuoteInput(request_id=req,instrument_id=i,price=str(price),source=source,
            observed_at=observed or at(8),trading_day=date(2026,1,day),kind='close',quality='valid'))
    first=historical('prev',5,10)
    assert first['effective_at']==utc(at(5)) and first['acquired_at']==utc(at(8,16))
    old=p.valuation(a,at(6),quote_revision=1)
    assert old['daily_pnl_fen'] is None
    today=historical('close',6,12)
    assert today['effective_at']==utc(at(6)) and today['acquired_at']==utc(at(8,16))
    r=p.valuation(a,at(6))
    assert r['daily_pnl_fen']==20000 and r['market_value_fen']==120000
    assert p.valuation(a,at(6,14))['market_value_fen'] is None
    assert p.valuation(a,at(6),quote_revision=1)==old
    assert p.quotes(i,at(6),available_at=at(6))['items']==[]
    assert len(p.quotes(i,at(6))['items'])==2
    with pytest.raises(TradeError,match='availability cutoff'):
        p.quotes(i,at(6),available_at=at(8))
    duplicate=historical('same-content-new-request',6,12,at(8,15,30))
    assert duplicate==today and p.quotes(i,at(6))['quote_revision']==2
    quote(p,i,6,11,kind='intraday',quality='stale',hour=14,minute=59)
    assert p.current_price(i,at(6))['quote']['id']==today['id']
    historical('conflict',6,13,source='second_confirmed_fixture')
    r=p.valuation(a,at(6))
    assert r['positions'][0]['quality']=='conflict' and r['equity_fen'] is None and r['daily_pnl_fen'] is None
    assert p.valuation(a,at(6),quote_revision=2)['daily_pnl_fen']==20000


def test_iss013_calendar_unavailable_safe_degradation(ledger):
    svc,a,i=ledger
    cash(svc,a,'base','opening',1000000,5)
    svc.submit(trade(a,i,'buy',day=5,quantity=100,price='10',fees_fen=0))
    p=PortfolioService(svc.db,clock=lambda:datetime(2027,1,5,tzinfo=SHANGHAI))
    quote(p,i,5,10)
    r=p.valuation(a,datetime(2027,1,4,15,tzinfo=SHANGHAI))
    assert r['market_value_fen'] is None and r['equity_fen'] is None and r['daily_pnl_fen'] is None
    assert 'calendar_unavailable' in r['reasons'] and r['cash_fen']==900000


def fixture_provider(svc, calls, *, bad=None):
    def respond(request):
        calls.append(request)
        # Acquiring a write transaction here proves the HTTP callback is not
        # enclosed in the refresh service's SQLite writer transaction.
        with svc.db.transaction() as conn:
            assert conn.execute('SELECT count(*) FROM instruments').fetchone()[0]>=1
        assert request.url.scheme=='https'
        if request.url.path.endswith('/kline/get'):
            assert request.url.params['secid']=='1.510300' and request.url.params['fqt']=='0'
            data={'code':'510300','market':1,'klines':[
                '2025-12-31,10.0,10.1,10.2,9.9,1,2,3,4,5,6',
                '2026-01-05,10.1,10.2,10.3,10.0,1,2,3,4,5,6',
            ]}
        else:
            assert request.url.params['secid']=='1.510300'
            data={'f57':'510300','f107':1,'f59':3,'f43':12345,'f86':int(at(6,10,5).timestamp())}
            if bad: data.update(bad)
        return httpx.Response(200,json={'rc':0,'data':data})
    return httpx.Client(transport=httpx.MockTransport(respond))


def test_provider_fixture_refresh_dedup_cash_isolation_and_atr(ledger):
    svc,a,i=ledger
    for role,atr in [('main','0.1234'),('sub','0.5678')]:
        svc.submit(trade(a,i,role,day=5,quantity=10,price='10',strategy_id='V9',lot_role=role,
                         signal_date='2025-12-31',frozen_atr=atr,strategy_deviation=True))
    svc.submit(trade(a,i,'missing-atr',day=5,quantity=10,strategy_id='G2',lot_role='main'))
    before=snapshot(svc); calls=[]
    with fixture_provider(svc,calls) as client:
        market=MarketService(svc.db,EastmoneyProvider(client,min_interval=0),clock=lambda:at(6,10,6))
        result=market.refresh(i)
        assert result['status']=='ok' and len(result['items'][0]['revisions'])==3
        price=market.status(i)
        assert price['quote_revision']==3 and price['items'][0]['quote']['price']==123450
        assert price['items'][0]['quote']['kind']=='intraday'
        assert market.refresh(i)['status']=='ok' and market.status(i)['quote_revision']==3
        evidence=market.portfolio.strategy_evidence(a,at(6,10,6))
        assert evidence['status']=='unavailable'
        assert sorted((r['lot_role'],r['frozen_atr']) for r in evidence['items'] if r['frozen_atr'] is not None)==[('main',1234),('sub',5678)]
        assert any('frozen_atr_or_signal_date_missing' in r['reason_codes'] for r in evidence['items'])
    assert len(calls)==4 and snapshot(svc)==before


@pytest.mark.parametrize('bad',[{'f57':'000001'},{'f107':0},{'f59':None},{'f43':0},
                               {'f86':int(at(5,10).timestamp())},{'f86':int(at(6,11).timestamp())}])
def test_provider_rejects_wrong_symbol_unit_or_time(ledger,bad):
    svc,a,i=ledger; calls=[]
    with fixture_provider(svc,calls,bad=bad) as client:
        provider=EastmoneyProvider(client,min_interval=0)
        master={'id':i,'code':'510300','market':'SH','kind':'etf'}
        with pytest.raises(MarketDataError): provider.spot(master,at(6,10,6))
        with pytest.raises(MarketDataError,match='no index proxy'):
            provider.spot({**master,'code':'000001'},at(6,10,6))
    assert len(calls)==1 and PortfolioService(svc.db).quotes(i,at(6))['items']==[]


def test_provider_rate_limit_and_unfinished_daily(ledger):
    svc,a,i=ledger; master={'id':i,'code':'510300','market':'SH','kind':'etf'}; requests=[]
    def limited(req):
        requests.append(req)
        return httpx.Response(429,headers={'Retry-After':'120'})
    with httpx.Client(transport=httpx.MockTransport(limited)) as client:
        provider=EastmoneyProvider(client,min_interval=0)
        with pytest.raises(MarketDataError,match='429'): provider.spot(master,at(6,10))
        with pytest.raises(MarketDataError,match='cooling down'): provider.spot(master,at(6,10))
    assert len(requests)==1
    def day(req):
        return httpx.Response(200,json={'rc':0,'data':{'code':'510300','market':1,'klines':[
            '2026-01-06,10,12,12,10,1,2,3,4,5,6']}})
    with httpx.Client(transport=httpx.MockTransport(day)) as client:
        provider=EastmoneyProvider(client,min_interval=0)
        assert provider.closes(master,{date(2026,1,6)},at(6,15,1))==[]
        assert provider.closes(master,{date(2026,1,6)},at(6,15,15))[0]['price']==Decimal('12')


def test_market_api_no_database_readonly_and_lifecycle(cfg,monkeypatch):
    with TestClient(create_app()) as client:
        assert client.get('/api/v1/market/status').status_code==503
        assert client.post('/api/v1/market/refresh').status_code==503
    started=Event(); ended=Event(); starts=[]
    original=MarketService.run
    async def tracked(self,stop,interval):
        starts.append(self)
        started.set()
        try: await original(self,stop,interval)
        finally: ended.set()
    monkeypatch.setattr(MarketService,'run',tracked)
    with TestClient(create_app(database=cfg)) as client:
        assert started.wait(2)
        assert client.get('/api/v1/market/status').status_code==200
    assert ended.is_set() and len(starts)==1
    sandbox=cfg.path.parent/'sandbox.sqlite'
    ident=initialize_database(sandbox,role='sandbox',confirm=True,binding_path=cfg.binding_path)
    local=replace(cfg,path=sandbox,expected_uuid=ident.database_uuid,mode='local')
    with TestClient(create_app(database=local)) as client:
        assert client.get('/api/v1/market/status').status_code==200
        assert client.post('/api/v1/market/refresh').status_code==403
    assert len(starts)==1


def test_refresh_worker_serial_and_restart(ledger):
    svc,a,i=ledger; calls=[]
    with fixture_provider(svc,calls) as client:
        market=MarketService(svc.db,EastmoneyProvider(client,min_interval=0),clock=lambda:at(6,10,6))
        # API and background both take the same refresh guard.
        market._refresh_lock.acquire()
        try: assert market.refresh(i)['status']=='busy'
        finally: market._refresh_lock.release()
        async def run_worker():
            stop=asyncio.Event(); worker=asyncio.create_task(market.run(stop,20))
            for _ in range(200):
                if market.status(i)['last_refresh'] is not None: break
                await asyncio.sleep(.01)
            else: pytest.fail('background refresh never completed')
            stop.set(); market.stop(); await asyncio.wait_for(worker,2)
        asyncio.run(run_worker())
        assert market.status(i)['quote_revision']==3
        # New service reads persisted evidence, not the former worker's memory.
        fresh=MarketService(svc.db,EastmoneyProvider(client,min_interval=0),clock=lambda:at(6,10,6))
        assert fresh.status(i)['last_refresh'] is None
        assert fresh.status(i)['items']==market.status(i)['items']
        assert len(calls)==2


def legacy_quotes(tmp_path):
    path=tmp_path/'old.sqlite'; identity=replace(Identity.new('formal'),schema_version=3)
    engine=create_engine(URL.create('sqlite',database=str(path)))
    try: migrate_schema(engine,identity,target='0003')
    finally: engine.dispose()
    with closing(sqlite3.connect(path)) as conn,conn:
        conn.execute("INSERT INTO instruments (id,code,market,currency,price_scale,kind) VALUES ('i','510300','SH','CNY',4,'etf')")
        conn.execute("INSERT INTO quotes VALUES ('old','i',100000,'old_fixture',?,'2026-01-05','close','valid',1)",(utc(at(8)),))
        conn.execute("INSERT INTO quotes VALUES ('invalid','i',100000,'old_fixture',?,'2026-01-04','close','valid',2)",(utc(at(8)),))
        conn.execute('UPDATE quote_state SET quote_revision=2')
    cfg=cfg_for(path,identity,tmp_path/'official.json'); adopt_database(cfg,confirm=True)
    return cfg


def test_0003_to_0004_real_upgrade_and_legacy_availability(tmp_path):
    cfg=legacy_quotes(tmp_path)
    with closing(sqlite3.connect(cfg.path)) as conn: before=conn.execute('SELECT * FROM quotes ORDER BY quote_revision').fetchall()
    upgrade_database(cfg,tmp_path/'backup',confirm=True)
    with open_database(cfg) as db,db.read() as conn:
        assert conn.execute('SELECT * FROM quotes ORDER BY quote_revision').fetchall()==[
            before[0]+(utc(at(5)),None,'unadjusted_security'),before[1]+(None,None,'unadjusted_security')]
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone()==(REVISION,)
        assert conn.execute('PRAGMA integrity_check').fetchone()==('ok',)
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
        p=PortfolioService(db)
        assert p.current_price('i',at(5))['quote']['id']=='old'
        assert p.quotes('i',at(8),available_at=at(8))['items']==[]
    with closing(sqlite3.connect(tmp_path/'backup'/'database.sqlite')) as conn:
        assert conn.execute('SELECT * FROM quotes ORDER BY quote_revision').fetchall()==before
        assert conn.execute('SELECT schema_version FROM system_metadata').fetchone()==(3,)


def test_0004_actual_ddl_failure_rollback(tmp_path,monkeypatch):
    cfg=legacy_quotes(tmp_path)
    with closing(sqlite3.connect(cfg.path)) as conn: before=list(conn.iterdump())
    original=database.create_engine
    def fail_engine(*args,**kwargs):
        engine=original(*args,**kwargs)
        @event.listens_for(engine,'after_cursor_execute')
        def fail(conn,cursor,statement,parameters,context,executemany):
            if 'ALTER TABLE quotes ADD COLUMN acquired_at' in statement:
                raise RuntimeError('injected migration failure')
        return engine
    monkeypatch.setattr(database,'create_engine',fail_engine)
    with pytest.raises(RuntimeError,match='injected migration failure'):
        upgrade_database(cfg,tmp_path/'backup',confirm=True)
    with closing(sqlite3.connect(cfg.path)) as conn:
        assert list(conn.iterdump())==before
        assert conn.execute('PRAGMA integrity_check').fetchone()==('ok',)
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_market_manual_refresh_http_and_price_basis(cfg):
    # Close the setup writer before the real ASGI lifespan opens its own writer.
    with open_database(cfg) as db:
        svc=TradeService(db)
        i=svc.create_instrument(InstrumentInput(request_id='master',code='510300',market='SH',kind='etf'))['id']
    calls=[]
    def respond(req):
        calls.append(req)
        assert req.url.params['secid']=='1.510300'
        if req.url.path.endswith('/kline/get'):
            d={'code':'510300','market':1,'klines':['2025-12-31,10,10,10,10,1,2,3,4,5,6',
                '2026-01-05,10,11,11,10,1,2,3,4,5,6']}
        else:
            d={'f57':'510300','f107':1,'f59':3,'f43':12345,'f86':int(at(6,10,5).timestamp())}
        return httpx.Response(200,json={'rc':0,'data':d})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        app=create_app(AppConfig(market_auto_refresh=False),database=cfg,
                       market_provider=EastmoneyProvider(http,min_interval=0))
        with TestClient(app) as client:
            app.state.market.clock=lambda:at(6,10,6)
            app.state.market.portfolio.clock=lambda:at(6,10,6)
            assert client.get('/api/v1/market/status').json()['items'][0]['quality']=='missing'
            result=client.post('/api/v1/market/refresh',params={'instrument_id':i})
            assert result.status_code==200 and result.json()['status']=='ok'
            price=client.get('/api/v1/market/prices/'+i).json()
            assert price['quote']['price']=='12.3450' and price['quote']['price_basis']=='unadjusted_security'
            request={'request_id':'bad','instrument_id':i,'price':'4000','source':'index_proxy_fixture',
                'observed_at':at(5).isoformat(),'trading_day':'2026-01-05','kind':'close','quality':'valid'}
            for basis in ('index','proxy','adjusted_security'):
                assert client.post('/api/v1/quotes',json={**request,'price_basis':basis}).status_code==422
            assert client.get('/api/v1/market/status').json()['quote_revision']==3
    assert len(calls)==2
