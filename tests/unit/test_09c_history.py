from datetime import datetime
import pytest
from test_trading import cfg,ledger,trade
from backend.application.trading.service import TradeError,Conflict,InstrumentInput
from backend.config import AppConfig
from backend.main import create_app
from fastapi.testclient import TestClient


def test_trade_history_filters_and_audit(ledger):
    svc,account,instrument=ledger
    other=svc.create_instrument(InstrumentInput(request_id='other',code='159915',market='SZ',kind='etf'))['id']
    a=svc.submit(trade(account,instrument,'first',day=5)); svc.submit(trade(account,instrument,'later',day=6)); svc.submit(trade(account,other,'other-trade',day=5))
    begin=datetime.fromisoformat('2026-01-05T00:00:00+08:00'); end=datetime.fromisoformat('2026-01-06T00:00:00+08:00')
    result=svc.list_records('trades',1,0,None,account,instrument,begin,end)
    assert result['total']==1 and result['items'][0]['id']==a['id']
    assert svc.get(a['id'],True)['audit_events'][0]['event']=='posted'
    with pytest.raises(TradeError): svc.list_records('trades',1,0,None,account,instrument,end,begin)
    with pytest.raises(TradeError): svc.list_records('trades',1,0,None,account,instrument,datetime(2026,1,5),end)
    with pytest.raises(Conflict): svc.list_records('trades',1,0,0,account,instrument,begin,end)


def test_trade_history_http(cfg):
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=cfg)) as client:
        a=client.post('/api/v1/accounts',json={'request_id':'account','name':'Account','effective_at':'2025-01-01T00:00:00+08:00'}).json()['id']
        i=client.post('/api/v1/instruments',json={'request_id':'instrument','code':'510300','market':'SH','kind':'etf'}).json()['id']
        first=client.post('/api/v1/trades',json=trade(a,i,'first',day=5).model_dump(mode='json')).json()
        client.post('/api/v1/trades',json=trade(a,i,'second',day=6).model_dump(mode='json'))
        params={'account_id':a,'instrument_id':i,'from_at':'2026-01-05T00:00:00+08:00','to_at':'2026-01-06T00:00:00+08:00','limit':1}
        result=client.get('/api/v1/trades',params=params)
        assert result.status_code==200 and result.json()['total']==1 and result.json()['items'][0]['id']==first['id']
        assert client.get('/api/v1/trades/'+first['id']).json()==first
        assert client.get('/api/v1/trades/'+first['id'],params={'include_audit':True}).json()['audit_events'][0]['event']=='posted'


def test_month_pnl_keeps_unknown_and_matches_day_projection(ledger):
    from datetime import date
    from test_pnl import cash,quote,at
    from backend.application.portfolio import PortfolioService
    svc,a,i=ledger
    p=PortfolioService(svc.db,clock=lambda:at(8,16))
    cash(svc,a,'opening','opening',3000000,5)
    svc.submit(trade(a,i,'purchase',day=5,quantity=1000,price='10',fees_fen=0))
    quote(p,i,5,10);quote(p,i,6,12)
    rows=p.monthly_pnl(a,date(2026,1,1))
    assert len(rows['items'])==8
    by_day={r['day']:r for r in rows['items']}
    assert by_day['2026-01-06']['daily_pnl_fen']==200000
    assert by_day['2026-01-07']['daily_pnl_fen'] is None
    assert by_day['2026-01-06']['daily_pnl_fen']==p.valuation(a,at(6))['daily_pnl_fen']
    with pytest.raises(TradeError): p.monthly_pnl(a,date(2026,1,2))
    with pytest.raises(TradeError): p.monthly_pnl(a,date(2026,2,1))


def test_net_worth_trend_snapshot_and_unknown(ledger):
    from test_finance import manual,balance
    from test_pnl import cash,at
    from backend.application.finance import FinanceService
    svc,a,i=ledger; f=FinanceService(svc.db,clock=lambda:at(8,16))
    bank=manual(svc,'trend-bank'); balance(f,bank,1000000)
    cash(svc,a,'opening','opening',500000,5)
    trend=f.net_worth_trend(3,at(8,16))
    assert len(trend['items'])==3
    assert trend['items'][0]['net_worth_fen'] is None
    assert trend['items'][-1]['net_worth_fen']==1500000
    assert trend['items'][-1]['net_worth_fen']==f.overview(at(8,16))['net_worth_fen']
    with pytest.raises(TradeError): f.net_worth_trend(2,at(8,16))
