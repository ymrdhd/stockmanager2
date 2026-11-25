"""Read-only finance evidence and safe restart parameters (synthetic only)."""
from datetime import date, datetime
import pytest
from backend.application.finance import FinanceService, BillInput, VoidInput
from backend.application.trading.service import Conflict, TradeError
from test_trading import cfg, ledger
from test_finance import manual, bill, state
from test_pnl import at


def test_refund_audit_preserves_original_corrected_and_voided(ledger):
    svc, _, _ = ledger
    f = FinanceService(svc.db)
    bank = manual(svc, 'audit-bank')
    original = bill(f, bank, amount=20000)
    data = BillInput(request_id='refund-one', account_id=bank, kind='refund', category_id='food', related_transaction_id=original['id'], amount_fen=15000, occurred_at='2026-02-02T10:00:00+08:00', note='returned')
    refund = f.bill(data)
    corrected = f.change_bill(refund['id'], data.model_copy(update={'request_id':'refund-two','amount_fen':10000}))
    f.change_bill(corrected['id'], VoidInput(request_id='refund-void',reason='cancelled'))
    before = state(svc)
    result = f.record_detail('bills', original['id'])
    assert result['record']['id'] == original['id']
    assert {r['id'] for r in result['related_records']} == {refund['id'], corrected['id']}
    assert result['effective_refunds_fen'] == 0 and result['remaining_expense_fen'] == 20000
    detail = f.record_detail('bills', corrected['id'], result['ledger_revision'])
    assert detail['original_expense']['id'] == original['id']
    assert {x['event'] for x in detail['audit_events']} == {'corrected','reversed'}
    assert any(r['id']==refund['id'] for r in detail['related_records'])
    assert state(svc) == before
    bill(f, bank, req='new-bill')
    with pytest.raises(Conflict): f.record_detail('bills', original['id'], result['ledger_revision'])
    with pytest.raises(TradeError): f.record_detail('unknown', original['id'])


def test_finance_month_explains_actual_refund_period(ledger):
    svc, _, _ = ledger
    f=FinanceService(svc.db); bank=manual(svc,'month-audit'); original=bill(f,bank,amount=20000)
    r=f.bill(BillInput(request_id='refund',account_id=bank,kind='refund',category_id='food',related_transaction_id=original['id'],amount_fen=5000,occurred_at='2026-02-02T10:00:00+08:00',note='returned'))
    cutoff=datetime.fromisoformat('2026-02-04T16:00:00+08:00')
    old=f.month(date(2025,12,23),cutoff); new=f.month(date(2026,1,23),cutoff)
    assert old['items'][0]['refund_applied_fen']==5000 and old['items'][0]['net_amount_fen']==15000
    assert new['items'][0]['original_expense_period']=='2025-12-23' and new['items'][0]['id']==r['id']
    assert new['source']=='current_effective_finance_facts_not_knowledge_replay'


def test_finance_audit_http_auth_and_version(cfg):
    from fastapi.testclient import TestClient
    from test_security import app_for, BASE, sign_in, identity_headers
    from backend.infrastructure.database import open_database
    from backend.application.trading.service import TradeService
    with open_database(cfg) as db:
        svc=TradeService(db); f=FinanceService(db); bank=manual(svc,'http-audit'); original=bill(f,bank)
        rev=f.record_detail('bills', original['id'])['ledger_revision']
    with TestClient(app_for(cfg),base_url=BASE) as client:
        path='/api/v1/finance/audit/bills/'+original['id']
        assert client.get(path).status_code==401
        headers=identity_headers(cfg,sign_in(client))
        response=client.get(path,params={'ledger_revision':rev},headers=headers)
        assert response.status_code==200,response.text
        assert response.json()['record']['id']==original['id']
        assert client.get(path,params={'ledger_revision':rev+1}).status_code==409
        assert client.get('/api/v1/finance/audit/bills/missing').status_code==404
        assert client.get('/api/v1/finance/audit/audit_events/'+original['id']).status_code==422
        assert client.get('/api/v1/finance/audit/accounts/'+bank).json()['record']['id']==bank


def test_port_number_guard():
    import argparse
    from start import _port_number
    assert [_port_number(str(v)) for v in (1,9091,65535)]==[1,9091,65535]
    for val in ('0','-1','65536','9000;whoami','1e4','9091.5'):
        with pytest.raises(argparse.ArgumentTypeError): _port_number(val)


def test_balance_transfer_audit_links_and_identity(ledger):
    from test_finance import balance, transfer
    from test_pnl import cash
    svc, sec, _ = ledger; f=FinanceService(svc.db)
    bank=manual(svc,'transfer-audit-bank'); first=balance(f,bank,1000000)
    cash(svc,sec,'opening','opening',500000,5)
    tr=f.transfer(transfer(bank,sec,first['id']))
    corrected=f.change_transfer(tr['id'],transfer(bank,sec,first['id'],req='correction',amount=100000))
    result=f.record_detail('transfers',corrected['id'])
    assert result['database_uuid']==svc.db.identity.database_uuid and result['generation']==svc.db.identity.generation
    assert result['related_records'][0]['id']==tr['id']
    assert result['audit_events'][0]['event']=='corrected'
    latest=next(x for x in f.records('balances')['items'] if x['status']=='effective' and x['id']!=first['id'])
    evidence=f.record_detail('balances',latest['id'])
    assert evidence['record']['after_fen']==900000
    assert first['id'] in {x['id'] for x in evidence['related_records']}
    assert evidence['audit_events'][0]['event']=='adjusted'
