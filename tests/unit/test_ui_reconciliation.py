"""06-ui backend safety gates with isolated databases."""
from datetime import datetime, UTC
from dataclasses import replace
from pathlib import Path
from contextlib import closing
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from backend.application.finance import (FinanceService, TransferReconciliation, ReconciledBalance,
    AccountScope, AccountEffective)
from backend.application.portfolio import PortfolioService
from backend.application.trading.service import AccountInput, Conflict, TradeError, TradeService, utc
from backend.infrastructure import database
from backend.infrastructure.database import open_database, upgrade_database, migrate_schema, Identity
from backend.config import AppConfig
from backend.main import create_app
from test_trading import cfg, ledger
from test_pnl import at, cash
from test_finance import manual, balance, transfer, account, state, legacy05


def test_iss023_included_transfer_void_golden(ledger):
    svc,a,_=ledger; f=FinanceService(svc.db); p=PortfolioService(svc.db)
    bank=manual(svc,'bank'); cash(svc,a,'opening','opening',500000,5)
    b=balance(f,bank,800000,day=6,hour=10)
    t=f.transfer(transfer(bank,a,b['id'],included=True))
    data=TransferReconciliation(request_id='reconcile',occurred_at=at(6,11),note='bank statement and broker confirmed original transfer did not occur',confirmed=True,
        balances=[ReconciledBalance(account_id=bank,expected_balance_id=b['id'],confirmed_fen=1000000)])
    assert f.overview(at(6))['net_worth_fen']==1500000
    old=state(svc)
    with pytest.raises(Conflict): f.reconcile_transfer(t['id'],data.model_copy(update={'balances':[ReconciledBalance(account_id=bank,expected_balance_id='stale',confirmed_fen=1000000)]}))
    assert state(svc)==old
    result=f.reconcile_transfer(t['id'],data); assert result['status']=='reversed'
    now=f.overview(at(6)); v=p.valuation(a,at(6))
    assert (account(now,bank)['balance_fen'],account(now,a)['cash_fen'],now['net_worth_fen'])==(1000000,500000,1500000)
    assert v['external_flow_fen']==v['daily_pnl_fen']==v['cumulative_pnl_fen']==0
    assert now['finance_month']['income_fen']==now['finance_month']['expense_fen']==0
    assert f.overview(at(6,10,30))['net_worth_fen'] is None
    assert 'transfer_reconciliation_historical_gap' in account(f.overview(at(6,10,30)),bank)['reasons']
    saved=state(svc); assert f.reconcile_transfer(t['id'],data)==result and state(svc)==saved
    print('ISS-023:',1000000,500000,1500000,0)


def test_iss023_replacement_atomic_failure(ledger,monkeypatch):
    svc,a,_=ledger; f=FinanceService(svc.db); bank=manual(svc,'bank')
    cash(svc,a,'opening','opening',500000,5)
    b=balance(f,bank,800000,day=6,hour=10)
    t=f.transfer(transfer(bank,a,b['id'],included=True))
    d=TransferReconciliation(request_id='replace',occurred_at=at(7,10),note='verified amount',confirmed=True,corrected_amount_fen=150000,
        balances=[ReconciledBalance(account_id=bank,expected_balance_id=b['id'],confirmed_fen=850000)])
    import backend.application.finance as module
    original=module.post_cash
    def fail(*args,**kwargs): raise RuntimeError('injected replacement failure')
    before=state(svc); monkeypatch.setattr(module,'post_cash',fail)
    with pytest.raises(RuntimeError,match='injected'): f.reconcile_transfer(t['id'],d)
    assert state(svc)==before
    with svc.db.read() as c: assert c.execute('SELECT count(*) FROM transfer_reconciliation_gaps').fetchone()==(0,)
    monkeypatch.setattr(module,'post_cash',original)
    new=f.reconcile_transfer(t['id'],d); assert new['correction_of']==t['id']
    r=f.overview(at(7)); assert (account(r,bank)['balance_fen'],account(r,a)['cash_fen'],r['net_worth_fen'])==(850000,650000,1500000)
    assert PortfolioService(svc.db).valuation(a,at(6))['external_flow_fen']==150000


def test_iss024_effective_and_scope_history(ledger):
    svc,a,_=ledger; f=FinanceService(svc.db)
    with pytest.raises(TradeError,match='effective time'): svc.create_account(AccountInput(request_id='no-date',name='no date'))
    cash(svc,a,'opening','opening',500000,5)
    b=svc.create_account(AccountInput(request_id='new-sec',name='new sec',effective_at=at(7,8)))['id']
    assert [x['id'] for x in f.overview(at(6))['accounts']]==[a]
    assert f.overview(at(6))['net_worth_fen']==500000
    cash(svc,b,'opening2','opening',200000,7)
    old=f.overview(at(7)); assert old['net_worth_fen']==700000
    req=AccountScope(request_id='scope',occurred_at=at(8),note='explicit statistics scope',included=False,expected_ledger_revision=old['ledger_revision'])
    f.set_account_scope(b,req)
    assert f.overview(at(7))['net_worth_fen']==700000
    later=f.overview(at(8)); assert later['net_worth_fen']==500000 and account(later,b)['balance_fen']==200000 and not account(later,b)['included']
    with pytest.raises(Conflict): f.set_account_scope(b,req.model_copy(update={'request_id':'bad','kind':'mistaken_account','expected_ledger_revision':later['ledger_revision']}))
    with svc.db.transaction() as c: c.execute('UPDATE accounts SET effective_at=NULL WHERE id=?',(b,))
    early=f.overview(at(6)); assert early['net_worth_fen'] is None and account(early,b)['effective_date_quality']=='unknown'
    d=AccountEffective(request_id='date-confirm',occurred_at=at(7,8),note='confirmed account opening statement',expected_ledger_revision=early['ledger_revision'])
    with pytest.raises(Conflict): f.confirm_account_effective(b,d.model_copy(update={'occurred_at':at(8)}))
    f.confirm_account_effective(b,d)
    assert f.overview(at(6))['net_worth_fen']==500000


def test_system_status_and_write_guard(cfg):
    with TestClient(create_app()) as c:
        assert c.get('/api/v1/system/status').json()['writable'] is False
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=cfg)) as c:
        r=c.get('/api/v1/system/status').json(); assert r['mode']=='server' and r['identity']['schema_version']==7 and r['writable']
        assert c.post('/api/v1/accounts',json={'request_id':'no-date','name':'missing'}).status_code==422


def legacy06(tmp_path):
    settings=legacy05(tmp_path)
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    from alembic.config import Config
    from alembic import command
    engine=create_engine(URL.create('sqlite',database=str(settings.path)))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql('PRAGMA foreign_keys=ON'); conn.exec_driver_sql('BEGIN IMMEDIATE')
            config=Config();config.set_main_option('script_location',str(database.ROOT/'migrations'));config.attributes['connection']=conn
            command.upgrade(config,'0006');conn.exec_driver_sql('UPDATE system_metadata SET schema_version=6')
    finally: engine.dispose()
    return settings


def test_0006_upgrade_and_failure(tmp_path,monkeypatch):
    settings=legacy06(tmp_path)
    with closing(sqlite3.connect(settings.path)) as c:
        facts=c.execute('SELECT * FROM trade_executions').fetchall(); before=list(c.iterdump())
    original=database.create_engine
    def factory(*args,**kwargs):
        engine=original(*args,**kwargs)
        @event.listens_for(engine,'after_cursor_execute')
        def fail(conn,cursor,statement,parameters,context,executemany):
            if statement.startswith('CREATE TABLE transfer_reconciliation_gaps'): raise RuntimeError('injected 0007 failure')
        return engine
    monkeypatch.setattr(database,'create_engine',factory)
    with pytest.raises(RuntimeError,match='injected 0007'): upgrade_database(settings,tmp_path/'failed',confirm=True)
    with closing(sqlite3.connect(settings.path)) as c: assert list(c.iterdump())==before
    monkeypatch.setattr(database,'create_engine',original)
    upgrade_database(settings,tmp_path/'backup',confirm=True)
    with open_database(settings) as db,db.read() as c:
        assert c.execute('SELECT * FROM trade_executions').fetchall()==facts
        assert c.execute('SELECT effective_at FROM accounts').fetchall()==[(None,)]
        assert c.execute('PRAGMA integrity_check').fetchone()==('ok',) and c.execute('PRAGMA foreign_key_check').fetchall()==[]
