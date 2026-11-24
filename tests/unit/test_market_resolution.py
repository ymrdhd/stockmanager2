"""04B safety cases, exclusively isolated databases and synthetic prices."""
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL

from backend.application.market import MarketService
from backend.application.market_calendar import CalendarUnavailable, SHANGHAI, calendar_coverage
from backend.application import market_calendar
from backend.application.portfolio import PortfolioService, QuoteInput, QuoteResolutionInput
from backend.application.trading.service import AccountInput, InstrumentInput, Conflict, TradeService, utc
from backend.config import AppConfig
from backend.infrastructure import database
from backend.infrastructure.database import Identity, adopt_database, migrate_schema, open_database, upgrade_database
from backend.main import create_app
from test_database_upgrade import cfg_for
from test_pnl import at, cash
from test_trading import cfg, data, ledger, snapshot, trade


def close(p, i, request, day, price, source='fixture'):
    return p.quote(QuoteInput(request_id=request, instrument_id=i, price=str(price),
        source=source, observed_at=at(day), trading_day=date(2026,1,day),
        kind='close', quality='valid'))


def test_iss018_resolution_preserves_evidence_and_temporal_versions(ledger):
    svc,a,i=ledger
    cash(svc,a,'base','opening',1000000,5)
    svc.submit(trade(a,i,'buy',day=5,quantity=100,price='10',fees_fen=0))
    p=PortfolioService(svc.db,clock=lambda:at(6,16))
    close(p,i,'prev',5,10)
    correct=close(p,i,'correct',6,12)
    wrong=close(p,i,'wrong',6,13,'other_fixture')
    old_revision=wrong['quote_revision']
    assert p.valuation(a,at(6))['daily_pnl_fen'] is None
    assert p.current_price(i,at(6))['quality']=='conflict'
    assert close(p,i,'repeat',6,12)==correct
    assert p.current_price(i,at(6))['quality']=='conflict'
    before=snapshot(svc)
    p=PortfolioService(svc.db,clock=lambda:at(8,16))
    command=QuoteResolutionInput(request_id='resolution',selected_quote_id=correct['id'],
        expected_quote_revision=old_revision,reason='verified exchange closing statement')
    with pytest.raises(Conflict,match='evidence changed'):
        p.resolve_close(command.model_copy(update={'expected_quote_revision':old_revision-1}))
    result=p.resolve_close(command)
    assert result==p.resolve_close(command)
    assert result['quote_revision']==old_revision+1
    assert set(result['reviewed_quote_ids'])=={correct['id'],wrong['id']}
    assert result['resolved_at']==utc(at(8,16))
    value=p.valuation(a,at(6))
    assert value['market_value_fen']==120000 and value['equity_fen']==1020000
    assert value['daily_pnl_fen']==value['equity_pnl_fen']==20000 and value['quality']=='valid'
    assert p.valuation(a,at(6),quote_revision=old_revision)['daily_pnl_fen'] is None
    assert p.current_price(i,at(6))['quote']['id']==correct['id']
    assert p.quotes(i,at(6))['resolutions']==[result]
    historical=p.quotes(i,at(7),available_at=at(7))
    assert historical['resolutions']==[] and {x['id'] for x in historical['items']} >= {correct['id'],wrong['id']}
    with svc.db.read() as conn:
        q,quality=p._quote(conn,i,'2026-01-06',utc(at(7)),close=True,revision=result['quote_revision'],available_at=utc(at(7)))
        assert q is not None and quality=='conflict'
        q,quality=p._quote(conn,i,'2026-01-06',utc(at(9)),close=True,revision=result['quote_revision'],available_at=utc(at(9)))
        assert quality=='valid' and q['id']==correct['id']
        assert conn.execute('SELECT count(*) FROM quotes').fetchone()==(3,)
    assert snapshot(svc)==before
    new=close(p,i,'third',6,14,'third_fixture')
    assert p.current_price(i,at(6))['quality']=='conflict'
    assert p.valuation(a,at(6),quote_revision=result['quote_revision'])==value
    result2=p.resolve_close(command.model_copy(update={'request_id':'resolution2',
        'expected_quote_revision':new['quote_revision'],'reason':'second review'}))
    assert result2['quote_revision']==new['quote_revision']+1
    assert p.valuation(a,at(6))['daily_pnl_fen']==20000
    assert snapshot(svc)==before


def test_resolution_http_and_reopen(cfg):
    with open_database(cfg) as db:
        svc=TradeService(db)
        i=svc.create_instrument(InstrumentInput(request_id='i',code='510300',market='SH',kind='etf'))['id']
        p=PortfolioService(db)
        a=close(p,i,'a',6,12); b=close(p,i,'b',6,13)
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=cfg)) as client:
        payload={'request_id':'r','selected_quote_id':a['id'],'expected_quote_revision':b['quote_revision'],'reason':'confirmed close'}
        response=client.post('/api/v1/quotes/resolutions',json=payload)
        assert response.status_code==200,response.text
        assert client.post('/api/v1/quotes/resolutions',json=payload).json()==response.json()
        assert client.post('/api/v1/quotes/resolutions',json={**payload,'reason':'different'}).status_code==409
    with open_database(cfg) as db:
        p=PortfolioService(db)
        assert p.current_price(i,at(6))['quote']['id']==a['id']
        assert p.quotes(i,at(6))['resolutions'][0]['reason']=='confirmed close'


@pytest.mark.parametrize('day,hour,reason',[(10,10,'execution_non_trading_day'),
    (2,10,'execution_non_trading_day'),(6,12,'execution_outside_session'),
    (6,8,'execution_outside_session'),(6,16,'execution_outside_session')])
def test_iss020_invalid_facts_pending_and_explicit_date_correction(ledger,day,hour,reason):
    svc,a,i=ledger
    cash(svc,a,'opening','opening',1000000,5)
    if day==2:
        a=svc.create_account(AccountInput(request_id='other',name='other',effective_at='2000-01-01T00:00:00+08:00'))['id']
    before={n:data(svc,n) for n in ('cash','lots','allocations')}
    bad=svc.submit(trade(a,i,'bad',day=day,executed_at=at(day,hour)))
    assert bad['status']=='pending' and bad['pending_reason']==reason
    assert {n:data(svc,n) for n in before}==before
    pending=snapshot(svc)
    with pytest.raises(Conflict,match=reason): svc.confirm_pending(bad['id'],'bad-confirm')
    assert snapshot(svc)==pending
    with pytest.raises(Conflict,match=reason):
        svc.correct(bad['id'],trade(a,i,'still-bad',day=day,executed_at=at(day,hour),note='not yet corrected'))
    assert snapshot(svc)==pending
    new=svc.correct(bad['id'],trade(a,i,'corrected',day=6,note='verified broker date and time'))
    assert new['status']=='effective' and new['correction_of']==bad['id']
    assert svc.get(bad['id'])['status']=='reversed'
    assert data(svc,'lots')[0]['remaining_qty']==100
    assert len([r for r in data(svc,'cash') if r['kind']=='trade'])==1
    with svc.db.read() as conn:
        assert conn.execute('SELECT event FROM audit_events WHERE entity_id=? ORDER BY rowid',(bad['id'],)).fetchall()==[('pending',),('reversed',)]


def test_invalid_sell_and_calendar_unavailable(ledger,monkeypatch):
    svc,a,i=ledger
    svc.submit(trade(a,i,'buy',day=5,executed_at=at(5,9,25)))
    before={n:data(svc,n) for n in ('lots','cash','allocations')}
    bad=svc.submit(trade(a,i,'weekend-sell',day=10,side='sell'))
    assert bad['pending_reason']=='execution_non_trading_day'
    assert {n:data(svc,n) for n in before}==before
    def no_calendar(): raise CalendarUnavailable('test missing calendar')
    monkeypatch.setattr(market_calendar,'calendar',no_calendar)
    other=svc.submit(trade(a,i,'no-calendar',day=6))
    assert other['pending_reason']=='execution_calendar_unavailable'
    assert {n:data(svc,n) for n in before}==before
    assert calendar_coverage(at(6))['status']=='unavailable'


def test_iss019_coverage_and_expiry(ledger):
    svc,a,i=ledger
    assert calendar_coverage(at(6))['start']=='1990-12-03'
    end=datetime(2026,12,1,10,tzinfo=SHANGHAI)
    status=MarketService(svc.db,clock=lambda:end).status(i)
    assert status['calendar_coverage']=={'start':'1990-12-03','end':'2026-12-31',
        'days_remaining':30,'status':'expiring','calendar':'XSHG'}
    out=datetime(2027,1,4,10,tzinfo=SHANGHAI)
    status=MarketService(svc.db,clock=lambda:out).status(i)
    assert status['calendar_coverage']['status']=='outside_coverage'
    assert status['items'][0]['quality']=='calendar_unavailable'
    svc=TradeService(svc.db,clock=lambda:out)
    bad=svc.submit(trade(a,i,'outside',executed_at=out,trading_day='2027-01-04'))
    assert bad['pending_reason']=='execution_calendar_unavailable' and not data(svc,'lots')


def legacy04(tmp_path):
    path=tmp_path/'old.sqlite'; identity=replace(Identity.new('formal'),schema_version=4)
    engine=create_engine(URL.create('sqlite',database=str(path)))
    try: migrate_schema(engine,identity,target='0004')
    finally: engine.dispose()
    with closing(sqlite3.connect(path)) as conn,conn:
        conn.execute("INSERT INTO instruments VALUES ('i','510300','SH','CNY',4,'etf')")
        for id_,price,revision in [('q1',120000,1),('q2',130000,2)]:
            conn.execute("INSERT INTO quotes VALUES (?,?,?,'fixture',?,'2026-01-06','close','valid',?,?,?,'unadjusted_security')",
                (id_,'i',price,utc(at(6)),revision,utc(at(6)),utc(at(6,16))))
        conn.execute('UPDATE quote_state SET quote_revision=2')
    settings=cfg_for(path,identity,tmp_path/'official.json'); adopt_database(settings,confirm=True)
    return settings


def test_0004_to_0005_preserves_conflict_and_backup(tmp_path):
    settings=legacy04(tmp_path)
    with closing(sqlite3.connect(settings.path)) as conn:
        before=conn.execute('SELECT * FROM quotes ORDER BY quote_revision').fetchall()
    upgrade_database(settings,tmp_path/'backup',confirm=True)
    with open_database(settings) as db,db.read() as conn:
        assert conn.execute('SELECT * FROM quotes ORDER BY quote_revision').fetchall()==before
        assert conn.execute('SELECT * FROM quote_resolutions').fetchall()==[]
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone()==('0007',)
        assert conn.execute('PRAGMA integrity_check').fetchone()==('ok',)
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert PortfolioService(db).current_price('i',at(6))['quality']=='conflict'
    with closing(sqlite3.connect(tmp_path/'backup'/'database.sqlite')) as conn:
        assert conn.execute('SELECT * FROM quotes ORDER BY quote_revision').fetchall()==before
        assert conn.execute('SELECT schema_version FROM system_metadata').fetchone()==(4,)


def test_0005_ddl_failure_rolls_back(tmp_path,monkeypatch):
    settings=legacy04(tmp_path)
    with closing(sqlite3.connect(settings.path)) as conn: before=list(conn.iterdump())
    original=database.create_engine
    def engine_factory(*args,**kwargs):
        engine=original(*args,**kwargs)
        @event.listens_for(engine,'after_cursor_execute')
        def fail(conn,cursor,statement,parameters,context,executemany):
            if 'CREATE TABLE quote_resolutions' in statement:
                raise RuntimeError('injected 0005 DDL failure')
        return engine
    monkeypatch.setattr(database,'create_engine',engine_factory)
    with pytest.raises(RuntimeError,match='injected 0005'):
        upgrade_database(settings,tmp_path/'backup',confirm=True)
    with closing(sqlite3.connect(settings.path)) as conn:
        assert list(conn.iterdump())==before
        assert conn.execute('PRAGMA integrity_check').fetchone()==('ok',)
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_resolution_write_failure_keeps_revision_and_conflict(ledger):
    svc,a,i=ledger
    p=PortfolioService(svc.db)
    first=close(p,i,'first',6,12)
    second=close(p,i,'second',6,13)
    before=p.quotes(i,at(6))
    with svc.db.transaction() as conn:
        conn.execute("CREATE TRIGGER inject_resolution_failure BEFORE INSERT ON quote_resolutions BEGIN SELECT RAISE(ABORT,'test write failure'); END")
    command=QuoteResolutionInput(request_id='r',selected_quote_id=first['id'],
        expected_quote_revision=second['quote_revision'],reason='verified close')
    with pytest.raises(sqlite3.IntegrityError,match='test write failure'):
        p.resolve_close(command)
    assert p.quotes(i,at(6))==before
    assert p.current_price(i,at(6))['quality']=='conflict'
    with svc.db.transaction() as conn:
        conn.execute('DROP TRIGGER inject_resolution_failure')
    assert p.resolve_close(command)['quote_revision']==second['quote_revision']+1


def test_resolution_refuses_readonly_and_health_mode(cfg):
    from backend.infrastructure.database import DatabaseSettings, initialize_database
    path=cfg.path.parent/'sandbox.sqlite'
    identity=initialize_database(path,role='sandbox',confirm=True,binding_path=cfg.binding_path)
    writable=DatabaseSettings(path,identity.database_uuid,1,cfg.path,mode='local',
        writable_sandbox=True,binding_path=cfg.binding_path)
    readonly=replace(writable,writable_sandbox=False)
    with open_database(writable) as db:
        i=TradeService(db).create_instrument(InstrumentInput(request_id='i',code='510300',market='SH',kind='etf'))['id']
        p=PortfolioService(db)
        first=close(p,i,'a',6,12); second=close(p,i,'b',6,13)
    payload={'request_id':'resolution','selected_quote_id':first['id'],
        'expected_quote_revision':second['quote_revision'],'reason':'verified close'}
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=readonly)) as client:
        assert client.post('/api/v1/quotes/resolutions',json=payload).status_code==403
        assert client.get('/api/v1/market/prices/'+i,params={'as_of':at(6).isoformat()}).json()['quality']=='conflict'
    with TestClient(create_app()) as client:
        assert client.post('/api/v1/quotes/resolutions',json=payload).status_code==503
