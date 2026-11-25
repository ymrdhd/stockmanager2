"""09F candidate semantics and safety tests with synthetic data only."""
from datetime import date, datetime
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from backend.application.finance import FinanceService, BillInput, VoidInput
from backend.application.portfolio import PortfolioService, OpeningInput
from backend.application.trading.service import Conflict
from backend.application.strategy.service import stop_distance
from backend.application.market import MarketService
from backend.config import AppConfig
from backend.main import create_app
from test_trading import cfg, ledger, trade
from test_finance import manual, balance, bill
from test_pnl import at, cash, quote


def test_refund_original_month_and_receipt_cashflow(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db)
    bank=manual(svc,'refund-bank'); balance(f,bank,100000)
    original=bill(f,bank,amount=20000)
    refund=BillInput(request_id='refund',account_id=bank,kind='refund',category_id='food',related_transaction_id=original['id'],amount_fen=15000,occurred_at='2026-02-02T10:00:00+08:00',note='confirmed return')
    r=f.bill(refund); assert f.bill(refund)==r
    cutoff=datetime.fromisoformat('2026-02-04T16:00:00+08:00')
    jan=f.month(date(2025,12,23),cutoff); feb=f.month(date(2026,1,23),cutoff)
    assert jan['expense_fen']==5000 and jan['refund_against_original_fen']==15000
    assert feb['expense_fen']==0 and feb['income_fen']==0 and feb['bill_cash_flow_fen']==15000
    assert next(x for x in f.overview(cutoff)['accounts'] if x['id']==bank)['balance_fen']==100000
    with pytest.raises(Conflict): f.bill(refund.model_copy(update={'request_id':'too-much','amount_fen':6000}))
    with pytest.raises(Conflict): f.change_bill(original['id'],VoidInput(request_id='bad',reason='keep link'))
    changed=f.change_bill(r['id'],refund.model_copy(update={'request_id':'refund-correct','amount_fen':10000}))
    assert f.month(date(2025,12,23),cutoff)['expense_fen']==10000
    f.change_bill(changed['id'],VoidInput(request_id='refund-void',reason='cancelled'))
    assert f.month(date(2025,12,23),cutoff)['expense_fen']==20000


def test_ratio_and_version_guards(ledger):
    svc,a,i=ledger; p=PortfolioService(svc.db,clock=lambda:at(8,16))
    cash(svc,a,'opening','opening',3000000,5)
    svc.submit(trade(a,i,'purchase',day=5,quantity=1000,price='10',fees_fen=0))
    quote(p,i,5,10);quote(p,i,6,12)
    v=p.valuation(a,at(6)); assert v['daily_pnl_fen']==200000 and v['previous_close_equity_fen']==3000000
    assert v['daily_return_pct']=='6.666667'
    args={k:v[k] for k in ('ledger_revision','quote_revision','database_uuid','generation')}
    assert p.valuation(a,at(6),**args)==v
    quote(p,i,7,13)
    with pytest.raises(Conflict):p.valuation(a,at(6),**args)
    v=p.valuation(a,at(6));args={k:v[k] for k in args}
    cash(svc,a,'deposit','deposit',10000,7)
    with pytest.raises(Conflict):p.valuation(a,at(6),**args)
    assert p.valuation(a,at(5))['daily_return_pct'] is None


def test_liquidity_no_duplicate_and_unknown(ledger):
    svc,a,i=ledger; f=FinanceService(svc.db)
    bank=manual(svc,'liquid-bank'); credit=manual(svc,'liquid-credit','credit_card')
    balance(f,bank,100000,'bank-balance');balance(f,credit,20000,'credit-balance');cash(svc,a,'opening','opening',50000,5)
    v=f.overview(at(6));assert v['liquid_assets_fen']==150000 and v['liquid_net_worth_fen']==130000
    unknown=manual(svc,'uncertain','other_liability');balance(f,unknown,10000,'unknown-balance')
    v=f.overview(at(6));assert v['liquid_net_worth_fen'] is None and v['liquidity_reasons']


def test_opening_pages_and_stale_version(ledger):
    svc,a,i=ledger;p=PortfolioService(svc.db)
    # Synthetic historical rows with no bypass of pagination logic: insertion
    # is fixture setup, not a claim that 201 independent opening writes are legal.
    with svc.db.transaction() as c:
        cols=[r[1] for r in c.execute('PRAGMA table_info(opening_positions)')]
    # Generate legitimate current and reversed rows through the supported API where possible.
    p.opening(OpeningInput(request_id='o',account_id=a,instrument_id=i,quantity=100,cost_fen=100,fees_fen=0,occurred_at=at(5,8),note='fixture'))
    with svc.db.transaction() as c:
        row=c.execute('SELECT * FROM opening_positions').fetchone(); values=dict(zip(cols,row))
        for n in range(201):
            item={**values,'id':f'historical-{n}'}
            if 'voided_at' in item:item['voided_at']='2026-01-06T00:00:00+00:00'
            c.execute('INSERT INTO opening_positions ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',tuple(item[k] for k in cols))
    first=p.openings(limit=50); assert first['total']==202
    found=[]
    for offset in range(0,202,50):found.extend(x['id'] for x in p.openings(limit=50,offset=offset,ledger_revision=first['ledger_revision'])['items'])
    assert len(set(found))==202
    cash(svc,a,'deposit','deposit',10000,7)
    with pytest.raises(Conflict):p.openings(limit=50,offset=50,ledger_revision=first['ledger_revision'])


def test_display_stop_distance():
    assert stop_distance(Decimal('10.2'),10,200)=='near_stop'
    assert stop_distance('10.21',10,200) is None
    assert stop_distance('10',10,200)=='protection_reached'
    assert stop_distance('9.99',10,200)=='protection_reached'
    assert stop_distance('10',None,200) is None


def test_sourced_security_name_and_unknown(ledger):
    svc,a,i=ledger;p=PortfolioService(svc.db);m=MarketService(svc.db)
    assert svc.list_records('instruments')['items'][0]['name_quality']=='missing'
    m._save(dict(instrument_id=i,price=Decimal('10'),source='synthetic provider',observed_at=at(5,15),trading_day=date(2026,1,5),kind='close',quality='valid',instrument_name='合成证券名称'))
    item=svc.list_records('instruments')['items'][0]
    assert item['id']==i and item['name']=='合成证券名称' and item['name_quality']=='valid'


def test_manual_backup_http(cfg):
    from test_security import app_for, BASE, sign_in, identity_headers
    with TestClient(app_for(cfg),base_url=BASE) as client:
        assert client.post('/api/v1/maintenance/backups').status_code==401
        token=sign_in(client);headers=identity_headers(cfg,token)
        assert client.post('/api/v1/maintenance/backups',headers=token).status_code==428
        assert client.post('/api/v1/maintenance/backups',headers={**headers,'x-database-generation':'2'}).status_code==409
        response=client.post('/api/v1/maintenance/backups',headers=headers)
        assert response.status_code==200,response.text
        result=response.json();assert result['verified'] and result['backup_at']
        assert client.post('/api/v1/maintenance/backups',headers=headers).json()['name']==result['name']
        assert len(client.get('/api/v1/maintenance/backups').json()['items'])==1


def test_upgrade_v10_preserves_refund_related_schema_history(tmp_path):
    from dataclasses import replace
    import sqlite3
    from contextlib import closing
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    from backend.infrastructure.database import Identity,migrate_schema,adopt_database,upgrade_database,open_database
    from test_database_upgrade import cfg_for
    path=tmp_path/'old.sqlite';identity=replace(Identity.new('formal'),schema_version=10)
    engine=create_engine(URL.create('sqlite',database=str(path)))
    try:migrate_schema(engine,identity,target='0010')
    finally:engine.dispose()
    with closing(sqlite3.connect(path)) as c,c:
        c.execute("INSERT INTO accounts (id,name,kind,currency,balance_mode,status,asset_class,included,effective_at) VALUES ('bank','bank','bank','CNY','manual_snapshot','active','asset',1,'2025-01-01T00:00:00Z')")
        c.execute("INSERT INTO finance_transactions VALUES ('orig','bank','expense',200,'food','2026-01-05T02:00:00Z','original',NULL,'reversed')")
        c.execute("INSERT INTO finance_transactions VALUES ('corr','bank','expense',100,'food','2026-01-05T02:00:00Z','corrected','orig','effective')")
    cfg=cfg_for(path,identity,tmp_path/'official.json');adopt_database(cfg,confirm=True)
    upgrade_database(cfg,tmp_path/'backup-v10',confirm=True)
    with open_database(cfg) as db,db.read() as c:
        assert c.execute('SELECT id,correction_of,status,related_transaction_id FROM finance_transactions ORDER BY rowid').fetchall()==[('orig',None,'reversed',None),('corr','orig','effective',None)]
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
