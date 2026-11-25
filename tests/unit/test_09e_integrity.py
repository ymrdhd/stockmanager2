"""09E integration, synthetic databases only; Node transport uses real FastAPI TestClient."""
import json
from pathlib import Path
import shutil
import subprocess
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.application.portfolio import PortfolioService
from backend.application.strategy.store import StrategyStore
from backend.application.trading.service import AccountInput, InstrumentInput, TradeService, cash_balance
from backend.config import AppConfig
from backend.infrastructure.database import open_database
from backend.main import create_app
from test_strategy_service import fund, service, etf_fixture
from test_trading import cfg, trade  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]


def test_two_client_intents_real_api_and_ledger(cfg):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for cross-runtime client integration')
    with TestClient(create_app(AppConfig(market_auto_refresh=False), database=cfg)) as client:
        def post(path, body):
            response = client.post('/api/v1'+path, json=body)
            assert response.status_code == 200, response.text
            return response.json()
        a = post('/accounts', {'request_id':'acc-a','name':'A','effective_at':'2000-01-01T00:00:00+08:00'})['id']
        b = post('/accounts', {'request_id':'acc-b','name':'B','effective_at':'2000-01-01T00:00:00+08:00'})['id']
        i = post('/instruments', {'request_id':'ins','code':'510300','market':'SH','kind':'etf'})['id']
        for account in (a,b):
            post('/cash', {'request_id':'opening-'+account,'account_id':account,'kind':'opening','amount_fen':1000000,'occurred_at':'2025-12-01T09:30:00+08:00','note':'synthetic'})
        db = client.app.state.database
        seen, phases = [], []
        proc = subprocess.Popen([node,'--experimental-strip-types',str(ROOT/'frontend/tests/intent.integration.ts'),a,i,b,db.identity.database_uuid],
                                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        try:
            for line in proc.stdout:
                message = json.loads(line)
                if message['kind']=='http':
                    response = client.request(message['method'],message['path'],json=message['body'],headers=message.get('headers') or {})
                    if message['method']=='POST': seen.append(message['body']['request_id'])
                    answer={'status':response.status_code,'body':response.json()}
                else:
                    phase=message['phase'];phases.append(phase)
                    expected={'first':(1,0),'second':(2,0),'final':(2,1)}[phase]
                    with db.read() as conn:
                        for account,n in zip((a,b),expected):
                            assert conn.execute("SELECT count(*) FROM trade_executions WHERE account_id=? AND status='effective'",(account,)).fetchone()==(n,)
                            assert conn.execute("SELECT count(*) FROM cash_movements WHERE account_id=? AND kind='trade'",(account,)).fetchone()==(n,)
                            assert conn.execute("SELECT count(*),coalesce(sum(remaining_qty),0) FROM trade_lots l JOIN trade_executions t ON t.id=l.open_execution_id WHERE t.account_id=? AND l.status!='reversed'",(account,)).fetchone()==(n,n*100)
                            assert cash_balance(conn,account,'2026-11-24T00:00:00Z')[0]==1000000-n*34572
                    answer={'ok':True}
                proc.stdin.write(json.dumps(answer)+'\n');proc.stdin.flush()
            assert proc.wait(timeout=10)==0,proc.stderr.read()
        finally:
            if proc.poll() is None: proc.kill();proc.wait()
            proc.stdin.close();proc.stdout.close();proc.stderr.close()
        assert phases==['first','second','final']
        assert seen==['intent-first','intent-second','intent-other','intent-other']


def test_deviation_reopen_legacy_receipt_correction_and_void(cfg):
    with open_database(cfg) as db:
        svc=TradeService(db)
        a=svc.create_account(AccountInput(request_id='a',name='A',effective_at='2000-01-01T00:00:00+08:00'))['id']
        i=svc.create_instrument(InstrumentInput(request_id='i',code='510300',market='SH',kind='etf'))['id']
        fund(svc,a)
        signal=StrategyStore(db,clock=lambda:datetime.fromisoformat('2026-01-05T15:30:00+08:00')).save_signal(
            account_id=a,index_id='000300.SH',instrument_code='510300',signal_date='2026-01-05',input_version='fixture',
            strategy_version='index-strategy-atr-v9.2',config_version='cfg:fixture',status='SIGNAL',price_basis='etf_unadjusted',price_unit='元',atr14=.05,source_verified=True)
        data=trade(a,i,'normal',day=6,lot_role='main',strategy_id='INDEX_STRATEGY_ATR',frozen_atr='0.05',signal_date='2026-01-05',signal_id=signal['signal_id'])
        normal=svc.submit(data)
        assert normal['strategy_evidence']
        assert service(db,etf_fixture()).sync_from_custody(a)['created']
        deviating=data.model_copy(update={'request_id':'deviating','strategy_deviation':True,'note':'actual execution deviated from entry rule'})
        actual=svc.correct(normal['id'],deviating)
        assert actual['strategy_evidence'] is None and actual['status']=='effective'
        # Emulate a historical 09C cached receipt only, never fabricate a new trade.
        legacy={**actual,'strategy_evidence':normal['strategy_evidence'],'strategy_evidence_missing':False}
        with db.transaction() as conn:
            conn.execute('UPDATE ledger_requests SET result_json=? WHERE request_id=?',(json.dumps(legacy),'deviating'))
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=cfg)) as client:
        db=client.app.state.database;svc=TradeService(db)
        now=datetime.fromisoformat('2026-11-24T16:00:00+08:00')
        evidence=PortfolioService(db).strategy_evidence(a,now)['items'][0]
        assert evidence['quantity']==100 and evidence['signal_id']==signal['signal_id']
        assert evidence['strategy_deviation'] and 'strategy_deviation' in evidence['reason_codes']
        assert evidence['strategy'] is None and evidence['deviation_note']==deviating.note
        valuation=PortfolioService(db).valuation(a,now)
        assert valuation['positions'][0]['quantity']==100
        assert valuation['cash_fen']==100000000-34572
        rebuilt=service(db,etf_fixture()).position_rows(a)
        assert rebuilt['custody'][0]['strategy_deviation'] and rebuilt['custody'][0]['remaining']==100
        assert rebuilt['items'][0]['position']['stop_price'] is None
        assert rebuilt['items'][0]['position']['action']=='UNVERIFIED'
        assert not rebuilt['unrealized_model_actions']
        receipt=client.get('/api/v1/requests/deviating').json()['result']
        assert receipt['id']==actual['id'] and receipt['strategy_evidence'] is None and receipt['strategy_deviation']
        assert svc.correct(normal['id'],deviating)['strategy_evidence'] is None
        assert svc.get(actual['id'],include_audit=True)['audit_events']
        # Corrected compliant execution can regain proof; voiding removes real position, not history.
        compliant=deviating.model_copy(update={'request_id':'verified-correction','strategy_deviation':False,'note':'broker confirmed compliant entry'})
        corrected=svc.correct(actual['id'],compliant)
        assert corrected['strategy_evidence'] and svc.get(actual['id'])['status']=='reversed'
        svc.void(corrected['id'],'void-verified','synthetic execution correction')
        assert not PortfolioService(db).valuation(a,now)['positions']
        assert PortfolioService(db).valuation(a,now)['cash_fen']==100000000
        assert svc.get(corrected['id'])['status']=='reversed'
