"""Trading golden cases and HTTP boundary checks; exclusively temporary databases."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.application.trading.service import (AccountInput, CashInput, Conflict, InstrumentInput,
                                                  TradeInput, TradeService, TradeError)
from backend.infrastructure.database import DatabaseSettings, initialize_database, open_database
from backend.main import create_app
from backend.config import AppConfig


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / 'official.sqlite'
    binding = tmp_path / 'official.json'
    identity = initialize_database(path, role='formal', confirm=True, binding_path=binding)
    return DatabaseSettings(path, identity.database_uuid, 1, path, binding_path=binding)


@pytest.fixture
def ledger(cfg):
    with open_database(cfg) as db:
        svc = TradeService(db)
        account = svc.create_account(AccountInput(request_id='account-test-account',name='test account',effective_at='2000-01-01T00:00:00+08:00'))['id']
        instrument = svc.create_instrument(InstrumentInput(request_id='instrument-510300',code='510300', market='SH', kind='etf'))['id']
        yield svc, account, instrument


def trade(account, instrument, req, *, day=5, **kwargs):
    values = dict(request_id=req, account_id=account, instrument_id=instrument,
                  side='buy', quantity=100, price='3.4567', fees_fen=5,
                  executed_at=f'2026-01-{day:02d}T09:30:00+08:00', trading_day=f'2026-01-{day:02d}')
    values.update(kwargs)
    return TradeInput(**values)


def data(svc, name):
    return svc.list_records(name)['items']


def snapshot(svc):
    return {k: svc.list_records(k) for k in ('trades', 'lots', 'cash', 'allocations', 'pending')}


def test_t01_t02_atomic_buy_precision_and_duplicate(ledger):
    svc, a, i = ledger
    order = trade(a, i, 'buy', quantity=3, price='1.0050', fees_fen=7, external_id='broker1')
    result = svc.submit(order)
    assert result['amount_fen'] == 302 and result['price'] == 10050
    before = snapshot(svc)
    repeat = trade(a, i, 'buy', quantity=3, price='1.005', fees_fen=7, external_id='broker1', executed_at='2026-01-05T01:30:00Z')
    assert svc.submit(repeat) == result
    assert svc.submit(repeat.model_copy(update={'request_id': 'new-id'})) == result
    with pytest.raises(Conflict):
        svc.submit(order.model_copy(update={'quantity': 4}))
    with pytest.raises(Conflict):
        svc.submit(order.model_copy(update={'request_id': 'another', 'quantity': 4}))
    assert snapshot(svc) == before
    assert data(svc, 'lots')[0]['remaining_cost_fen'] == 302
    assert data(svc, 'lots')[0]['remaining_fees_fen'] == 7
    assert data(svc, 'cash')[0]['amount_fen'] == -309
    assert svc.list_records('trades')['ledger_revision'] == 3
    with svc.db.read() as conn:
        assert conn.execute("SELECT entity,event FROM audit_events ORDER BY rowid").fetchall() == [('account','created'),('trade','posted')]


def test_t07_identical_distinct_broker_ids(ledger):
    svc, a, i = ledger
    first = svc.submit(trade(a, i, 'one', external_id='A'))
    second = svc.submit(trade(a, i, 'two', external_id='B'))
    third = svc.submit(trade(a, i, 'three'))
    fourth = svc.submit(trade(a, i, 'four'))
    assert len({first['id'], second['id'], third['id'], fourth['id']}) == 4
    assert len(data(svc, 'cash')) == 4


def test_fifo_specified_partial_full_clear_and_fee_conservation(ledger):
    svc, a, i = ledger
    svc.submit(trade(a, i, 'a', quantity=3, price='1.0050', fees_fen=7))
    svc.submit(trade(a, i, 'b', day=6, quantity=4, price='2.001', fees_fen=3))
    lots = data(svc, 'lots')
    sale = svc.submit(trade(a, i, 's1', day=7, side='sell', quantity=2, price='4.0025', fees_fen=1, allocations={lots[1]['id']: 2}))
    assert sale['status'] == 'effective' and sale['amount_fen'] == 801
    assert [l['remaining_qty'] for l in data(svc, 'lots')] == [3, 2]
    sale2 = svc.submit(trade(a, i, 's2', day=8, side='sell', quantity=4))
    alloc = [x for x in data(svc, 'allocations') if x['sell_execution_id'] == sale2['id']]
    assert [(x['lot_id'], x['quantity']) for x in alloc] == [(lots[0]['id'], 3), (lots[1]['id'], 1)]
    svc.submit(trade(a, i, 's3', day=9, side='sell', quantity=1))
    assert [(l['remaining_qty'], l['status']) for l in data(svc, 'lots')] == [(0,'closed'), (0,'closed')]
    assert sum(x['buy_fees_fen'] for x in data(svc, 'allocations')) == 10
    assert sum(x['cost_fen'] for x in data(svc, 'allocations')) == 1102
    assert [x['amount_fen'] for x in data(svc, 'cash')][:3] == [-309, -803, 800]
    assert all(l['remaining_cost_fen'] == l['remaining_fees_fen'] == 0 for l in data(svc, 'lots'))


def test_t05_pending_oversell_wrong_lot_and_confirmation(ledger):
    svc, a, i = ledger
    svc.submit(trade(a, i, 'buy', quantity=3))
    other = svc.create_account(AccountInput(request_id='account-other',name='other',effective_at='2000-01-01T00:00:00+08:00'))['id']
    svc.submit(trade(other, i, 'other', quantity=10))
    lots = data(svc, 'lots')
    for req, qty, specified in [('over', 5, None), ('wrong', 1, {lots[1]['id']: 1}), ('sum', 2, {lots[0]['id']: 1})]:
        before = {name: data(svc, name) for name in ('lots', 'cash', 'allocations')}
        result = svc.submit(trade(a, i, req, day=8, side='sell', quantity=qty, allocations=specified))
        assert result['status'] == 'pending' and result['pending_reason']
        assert {name: data(svc, name) for name in before} == before
    assert svc.list_records('cash')['data_incomplete']
    with pytest.raises(Conflict):
        svc.confirm_pending(data(svc,'pending')[0]['id'], 'confirm-too-soon')
    svc.submit(trade(a, i, 'fill', day=6, quantity=2))
    pending = data(svc,'pending')[0]
    confirmed = svc.confirm_pending(pending['id'], 'confirm')
    assert confirmed['status'] == 'effective'
    assert svc.confirm_pending(pending['id'], 'confirm') == confirmed
    assert [l['remaining_qty'] for l in data(svc, 'lots') if l['open_execution_id'] != lots[1]['open_execution_id']] == [0,0]
    assert len(data(svc,'pending')) == 2


@pytest.mark.parametrize('strategy',['V9','G2'])
def test_t06_main_sub_frozen_atr_and_strategy_deviation(ledger,strategy):
    svc, a, i = ledger
    for role, atr in [('main','0.1234'), ('sub','0.4567')]:
        svc.submit(trade(a, i, role, quantity=10, strategy_id=strategy, lot_role=role, signal_date='2025-12-31', frozen_atr=atr, strategy_deviation=True))
    main, sub = data(svc,'lots')
    pending = svc.submit(trade(a, i, 'no-fifo', day=6, side='sell', quantity=1, strategy_id=strategy, lot_role='main'))
    assert pending['status'] == 'pending'
    for role, lot, q in [('main',main,3), ('sub',sub,4)]:
        result = svc.submit(trade(a, i, 'sell-'+role, day=7, side='sell', quantity=q, strategy_id=strategy, lot_role=role, allocations={lot['id']:q}))
        assert result['status'] == 'effective'
    assert [(x['lot_role'], x['remaining_qty'], x['frozen_atr'], x['signal_date']) for x in data(svc,'lots')] == [('main',7,1234,'2025-12-31'),('sub',6,4567,'2025-12-31')]
    assert svc.get(main['open_execution_id'])['strategy_deviation'] is True


def test_t04_mid_transaction_constraint_rolls_back(ledger):
    svc, a, i = ledger
    before = snapshot(svc)
    with svc.db.transaction() as conn:
        conn.execute("CREATE TRIGGER test_failure BEFORE INSERT ON cash_movements BEGIN SELECT RAISE(ABORT,'injected cash failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected cash failure'):
        svc.submit(trade(a, i, 'failed'))
    assert snapshot(svc) == before
    with svc.db.read() as conn:
        assert conn.execute("SELECT entity,event FROM audit_events ORDER BY rowid").fetchall() == [('account','created')]
        assert conn.execute("SELECT count(*) FROM ledger_requests WHERE request_id='failed'").fetchone() == (0,)
    with svc.db.transaction() as conn:
        conn.execute('DROP TRIGGER test_failure')
    assert svc.submit(trade(a, i, 'failed'))['status'] == 'effective'


def test_t03_process_crash_and_restart(cfg):
    with open_database(cfg) as db:
        svc = TradeService(db)
        a = svc.create_account(AccountInput(request_id='account-restart',name='restart',effective_at='2000-01-01T00:00:00+08:00'))['id']
        i = svc.create_instrument(InstrumentInput(request_id='instrument-600000',code='600000', market='SH', kind='stock'))['id']
    code = '''import os,sys
from pathlib import Path
from backend.infrastructure.database import DatabaseSettings,open_database
from backend.application.trading.service import TradeService,TradeInput
p,u,b,payload=sys.argv[1:]
with open_database(DatabaseSettings(Path(p),u,1,Path(p),binding_path=Path(b))) as db:
    TradeService(db).submit(TradeInput.model_validate_json(payload))
    os._exit(73)
'''
    order = trade(a,i,'crash')
    result = subprocess.run([sys.executable,'-c',code,str(cfg.path),cfg.expected_uuid,str(cfg.binding_path),order.model_dump_json()],capture_output=True,timeout=10)
    assert result.returncode == 73, result.stderr
    with open_database(cfg) as db:
        svc = TradeService(db)
        assert len(data(svc,'trades')) == len(data(svc,'lots')) == len(data(svc,'cash')) == 1
        assert data(svc,'lots')[0]['remaining_qty'] == 100
        assert data(svc,'cash')[0]['amount_fen'] == -34572
        assert svc.submit(order)['id'] == data(svc,'trades')[0]['id']
        assert svc.list_records('trades')['ledger_revision'] == 3


def test_t08_correction_preserves_original_and_rejects_unsafe_replay(ledger):
    svc, a, i = ledger
    buy = svc.submit(trade(a,i,'buy',quantity=10,fees_fen=7,external_id='B'))
    sell = svc.submit(trade(a,i,'sell',day=6,side='sell',quantity=4,fees_fen=2,external_id='S'))
    before = snapshot(svc)
    with pytest.raises(Conflict):
        svc.correct(buy['id'],trade(a,i,'unsafe',quantity=2,external_id='B',note='requires replay'))
    assert snapshot(svc) == before
    with pytest.raises(Conflict):
        svc.correct(sell['id'],trade(a,i,'too-much',day=6,side='sell',quantity=99,note='incorrect'))
    assert snapshot(svc) == before
    replacement_input = trade(a,i,'correct',day=6,side='sell',quantity=3,fees_fen=3,external_id='S',note='broker correction')
    new = svc.correct(sell['id'],replacement_input)
    assert svc.correct(sell['id'],replacement_input) == new
    assert new['correction_of'] == sell['id']
    assert svc.get(sell['id'])['status'] == 'reversed' and svc.get(sell['id'])['quantity'] == 4
    assert data(svc,'lots')[0]['remaining_qty'] == 7
    assert [r['execution_status'] for r in data(svc,'allocations')] == ['reversed','effective']
    cash = data(svc,'cash')
    assert len(cash) == 4 and sum(r['amount_fen'] for r in cash) == -3464 + 1034
    assert svc.list_records('trades')['ledger_revision'] == 5
    assert svc.list_records('trades')['earliest_affected_day'] == '2026-01-05'
    with svc.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM audit_events WHERE entity_id=? AND event='reversed'", (sell['id'],)).fetchone() == (1,)


def test_unknown_fees_cash_quality_and_explicit_correction(ledger):
    svc,a,i=ledger
    unknown = svc.submit(trade(a,i,'unknown',fees_fen=None))
    assert unknown['status']=='pending' and unknown['fees_fen'] is None
    assert not data(svc,'cash') and not data(svc,'lots')
    assert svc.list_records('cash')['balances'][0]['balance_fen'] is None
    posted=svc.correct(unknown['id'],trade(a,i,'fee-confirmed',fees_fen=5,note='verified statement'))
    assert posted['status']=='effective'
    opening=CashInput(request_id='opening',account_id=a,kind='opening',amount_fen=100,occurred_at='2025-12-31T00:00:00Z',note='verified opening')
    assert svc.cash(opening)==svc.cash(opening)
    with pytest.raises(Conflict):
        svc.cash(opening.model_copy(update={'request_id':'duplicate-opening'}))
    deposit=CashInput(request_id='deposit',account_id=a,kind='deposit',amount_fen=50000,occurred_at='2026-01-06T00:00:00Z',note='actual transfer',transfer_id='transfer1')
    with pytest.raises(TradeError,match='paired finance transfer'):
        svc.cash(deposit)
    svc.cash(deposit.model_copy(update={'transfer_id':None}))
    assert svc.list_records('cash')['balances'][0] == {'account_id':a,'balance_fen':15528,'quality':'derived_unverified'}
    svc.cash(CashInput(request_id='adjust',account_id=a,kind='adjustment',amount_fen=100,occurred_at='2026-01-07T00:00:00Z',note='confirmed reconciliation'))
    assert [r['kind'] for r in data(svc,'cash')]==['opening','trade','deposit','adjustment']


@pytest.mark.parametrize('changes',[{'price':1.23},{'price':'NaN'},{'price':'1.00001'},{'quantity':True},{'quantity':0},{'fees_fen':-1},{'executed_at':'2026-01-05T09:30:00'},{'trading_day':'2026-02-30'},{'trading_day':'2026-01-06'},{'price':'1000001'},{'allocations':{'bad':True}}])
def test_invalid_input(changes):
    with pytest.raises(ValidationError):
        trade('a','i','invalid',**changes)


def test_api_idempotency_readonly_sandbox_and_no_database(cfg):
    with TestClient(create_app()) as client:
        assert client.post('/api/v1/accounts',json={'request_id':'account-refuse','name':'refuse','effective_at':'2000-01-01T00:00:00+08:00'}).status_code==503
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=cfg)) as client:
        a=client.post('/api/v1/accounts',json={'request_id':'account-API','name':'API','effective_at':'2000-01-01T00:00:00+08:00'}).json()['id']
        i=client.post('/api/v1/instruments',json={'request_id':'instrument-510300','code':'510300','market':'SH','kind':'etf'}).json()['id']
        order=trade(a,i,'api').model_dump(mode='json')
        r=client.post('/api/v1/trades',json=order)
        assert r.status_code==200, r.text
        assert client.post('/api/v1/trades',json=order).json()==r.json()
        assert client.post('/api/v1/trades',json={**order,'quantity':101}).status_code==409
        assert client.get('/api/v1/trades/'+r.json()['id']).json()==r.json()
        before=client.get('/api/v1/trades').json()
        assert before['ledger_revision']==3 and client.get('/api/v1/lots').json()['items'][0]['remaining_qty']=='100'
        assert {k:v for k,v in client.get('/api/v1/trades').json().items() if k!='as_of'}=={k:v for k,v in before.items() if k!='as_of'}
    sandbox=cfg.path.parent/'sandbox.sqlite'
    sid=initialize_database(sandbox,role='sandbox',confirm=True,binding_path=cfg.binding_path)
    for writable in (False,True):
        local=DatabaseSettings(sandbox,sid.database_uuid,1,cfg.path,mode='local',writable_sandbox=writable,binding_path=cfg.binding_path)
        with TestClient(create_app(AppConfig(market_auto_refresh=False),database=local)) as client:
            assert client.post('/api/v1/accounts',json={'request_id':'account-sandbox','name':'sandbox','effective_at':'2000-01-01T00:00:00+08:00'}).status_code==(200 if writable else 403)
            assert client.get('/api/v1/trades').json()['items']==[]
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=cfg)) as client:
        assert {k:v for k,v in client.get('/api/v1/trades').json().items() if k!='as_of'}=={k:v for k,v in before.items() if k!='as_of'}


def test_correction_broker_uniqueness_and_original_retry(ledger):
    svc,a,i=ledger
    original=trade(a,i,'original',external_id='stable')
    first=svc.submit(original)
    replacement=trade(a,i,'replacement',external_id='stable',quantity=99,note='verified quantity')
    second=svc.correct(first['id'],replacement)
    assert second['external_id']=='stable' and second['external_id_owner']==first['id']
    assert svc.submit(original)==first  # exact original request response, even after reversal
    assert svc.get(first['id'])['status']=='reversed'
    assert svc.submit(replacement.model_copy(update={'request_id':'broker-retry'}))==second
    with pytest.raises(Conflict):
        svc.submit(trade(a,i,'different',external_id='stable',quantity=88))
    newer=svc.submit(trade(a,i,'newer',day=6,external_id='different'))
    before=snapshot(svc)
    with pytest.raises(Conflict):
        svc.correct(newer['id'],trade(a,i,'steal-id',day=6,external_id='stable',note='wrong identity'))
    assert snapshot(svc)==before
    with svc.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM trade_executions WHERE account_id=? AND external_id='stable'",(a,)).fetchone()==(1,)
        assert conn.execute("SELECT count(*) FROM trade_executions WHERE external_id_owner=?",(first['id'],)).fetchone()==(1,)


def test_cross_instrument_and_pending_original_response(ledger):
    svc,a,i=ledger
    j=svc.create_instrument(InstrumentInput(request_id='instrument-600001',code='600001',market='SH',kind='stock'))['id']
    svc.submit(trade(a,j,'other-instrument',quantity=10))
    lot=data(svc,'lots')[0]
    order=trade(a,i,'wrong-instrument',day=6,side='sell',quantity=1,allocations={lot['id']:1})
    result=svc.submit(order)
    assert result['status']=='pending' and result['requested_allocations']=={lot['id']:1}
    assert svc.list_records('lots')['incomplete_scopes']==[{'account_id':a,'instrument_id':i}]
    assert data(svc,'lots')[0]['remaining_qty']==10
    svc.submit(trade(a,i,'correct-instrument',quantity=1))
    newlot=data(svc,'lots')[1]
    corrected=svc.correct(result['id'],trade(a,i,'fix-allocation',day=6,side='sell',quantity=1,allocations={newlot['id']:1},note='identified correct lot'))
    assert corrected['status']=='effective' and svc.submit(order)==result
    assert data(svc,'lots')[0]['remaining_qty']==10 and data(svc,'lots')[1]['remaining_qty']==0
    assert not svc.list_records('lots')['data_incomplete']


def test_actual_trade_reads_share_snapshot(ledger):
    svc,a,i=ledger
    with svc.db.read() as read:
        assert read.execute('SELECT ledger_revision FROM ledger_state').fetchone()==(2,)
        assert read.execute('SELECT count(*) FROM trade_executions').fetchone()==(0,)
        svc.submit(trade(a,i,'while-read'))
        assert read.execute('SELECT count(*) FROM cash_movements').fetchone()==(0,)
        assert read.execute('SELECT count(*) FROM trade_lots').fetchone()==(0,)
        assert read.execute('SELECT ledger_revision FROM ledger_state').fetchone()==(2,)
    assert svc.list_records('cash')['ledger_revision']==3
    assert len(data(svc,'trades'))==len(data(svc,'cash'))==len(data(svc,'lots'))==1


def test_trade_api_post_refuses_no_db_and_readonly_and_allows_sandbox(cfg):
    sandbox=cfg.path.parent/'isolated.sqlite'
    sid=initialize_database(sandbox,role='sandbox',confirm=True,binding_path=cfg.binding_path)
    writable=DatabaseSettings(sandbox,sid.database_uuid,1,cfg.path,mode='local',writable_sandbox=True,binding_path=cfg.binding_path)
    readonly=DatabaseSettings(sandbox,sid.database_uuid,1,cfg.path,mode='local',binding_path=cfg.binding_path)
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=writable)) as client:
        a=client.post('/api/v1/accounts',json={'request_id':'account-sandbox','name':'sandbox','currency':'CNY','effective_at':'2000-01-01T00:00:00+08:00'}).json()['id']
        i=client.post('/api/v1/instruments',json={'request_id':'instrument-510300','code':'510300','market':'SH','kind':'etf','currency':'CNY'}).json()['id']
        order=trade(a,i,'sandbox').model_dump(mode='json')
        assert client.post('/api/v1/trades',json=order).status_code==200
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=readonly)) as client:
        assert client.post('/api/v1/trades',json={**order,'request_id':'readonly'}).status_code==403
        assert client.get('/api/v1/trades').json()['ledger_revision']==3
    with TestClient(create_app()) as client:
        assert client.post('/api/v1/trades',json=order).status_code==503
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=cfg)) as client:
        assert client.get('/api/v1/trades').json()['items']==[]


def test_api_exact_wire_paging_master_request_retries_and_conflict(cfg):
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=cfg)) as client:
        account={'request_id':'account-api-paging','name':'paging','currency':'CNY','effective_at':'2000-01-01T00:00:00+08:00'}
        first=client.post('/api/v1/accounts',json=account)
        assert first.status_code==200
        assert client.post('/api/v1/accounts',json=account).json()==first.json()
        assert client.post('/api/v1/accounts',json={**account,'name':'changed'}).status_code==409
        a=first.json()['id']
        i=client.post('/api/v1/instruments',json={'request_id':'instrument-api-paging','code':'510300','market':'SH','kind':'etf'}).json()['id']
        order=trade(a,i,'paging').model_dump(mode='json')
        r=client.post('/api/v1/trades',json={**order,'quantity':'100.0'})
        assert r.status_code==200 and r.json()['price']=='3.4567' and r.json()['quantity']=='100'
        assert client.post('/api/v1/trades',json=order).json()==r.json()
        assert client.get('/api/v1/trades?limit=501').status_code==422
        page=client.get('/api/v1/trades?limit=1').json()
        assert page['total']==1 and page['ledger_revision']==3 and page['quality']=='valid'
        assert client.post('/api/v1/trades',json={**order,'request_id':'new-paging'}).status_code==200
        assert client.get('/api/v1/trades?limit=1&offset=1&ledger_revision=3').status_code==409
        page=client.get('/api/v1/trades?limit=1&offset=1&ledger_revision=4').json()
        assert len(page['items'])==1 and page['total']==2
