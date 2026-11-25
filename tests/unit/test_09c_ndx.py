"""T5 uses actual executions, distinct advice evidence, and the unchanged rule function."""
from datetime import datetime
from dataclasses import replace

import pytest

from test_trading import cfg, ledger, trade, data
from test_strategy_service import FakeData, sessions, fund
from backend.application.strategy.service import StrategyService
from backend.application.trading.service import InstrumentInput, AccountInput, TradeInput
from backend.application.portfolio import PortfolioService, QuoteInput
from backend.domain.strategy.config import get_config, INDEX_STRATEGY_VERSION
from backend.domain.strategy.market_data import Bar
from backend.domain.strategy.calendars import market_clock


def ndx_series(day, close):
    days=sessions(220,until=day)
    return [Bar(date=d,open=p,high=p+.1,low=p-.1,close=p,volume=1000) for d,p in
            zip(days,[8+i*.005 for i in range(219)]+[close])]


class Scenario:
    def __init__(self, ledger):
        self.svc,self.account,_=ledger
        self.instrument=self.svc.create_instrument(InstrumentInput(request_id='ndx',code='159941',market='SZ',kind='etf'))['id']
        fund(self.svc,self.account)
        self.cfg=get_config('NDX')
        self.strategy=None
    def advise(self, day, close='10', *, persist=True, verified=True):
        # A fresh service instance each request; no in-memory strategy state survives.
        now=datetime.fromisoformat(day+'T16:00:00+08:00')
        bars=ndx_series(day,float(close))
        self.strategy=StrategyService(self.svc.db,data=FakeData(bars),clock=lambda:now)
        PortfolioService(self.svc.db,clock=lambda:now).quote(QuoteInput(request_id='quote-'+day,instrument_id=self.instrument,
            price=close,source='synthetic independent quote',observed_at=now,trading_day=day,kind='close',quality='valid'))
        series=self.strategy.data.etf_series('NDX',completed_on=day)
        return self.strategy._ndx_advice(self.account,day,self.cfg,replace(series,source_verified=verified),[],persist=persist)
    def buy(self, advice, request, quantity, *, external_id=None, day=None):
        day=day or advice['next_etf_session']
        return self.svc.submit(TradeInput(request_id=request,account_id=self.account,instrument_id=self.instrument,
            side='buy',quantity=quantity,price='10',fees_fen=0,executed_at=day+'T10:00:00+08:00',trading_day=day,
            strategy_id='NDX_AUX_T5',lot_role=advice['lot_role'],signal_id=advice['signal_id'],signal_date=advice['signal_date'],external_id=external_id))
    def sell(self, day, role, quantity, price, request):
        lots=[l for l in data(self.svc,'lots') if l['strategy_id']=='NDX_AUX_T5' and l['lot_role']==role and l['remaining_qty']>0]
        alloc={}; remain=quantity
        for lot in lots:
            take=min(remain,lot['remaining_qty'])
            if take: alloc[lot['id']]=take; remain-=take
        assert remain==0
        return self.svc.submit(TradeInput(request_id=request,account_id=self.account,instrument_id=self.instrument,
            side='sell',quantity=quantity,price=price,fees_fen=0,executed_at=day+'T10:00:00+08:00',trading_day=day,
            strategy_id='NDX_AUX_T5',lot_role=role,allocations=alloc,strategy_exit_action='EXIT'))


def test_ndx_full_actual_lifecycle(ledger):
    s=Scenario(ledger)
    start=s.advise('2026-06-01')
    assert start['status']=='ALLOW' and start['target_ratio']==.15 and start['target_amount']==150000
    assert start['actionable'] and not start['tradable'] and start['signal_id']
    repeat=s.advise('2026-06-01')
    assert repeat['signal_id']==start['signal_id']
    a=s.buy(start,'initial-part1',10000,external_id='fill-1')
    b=s.buy(start,'initial-part2',5000,external_id='fill-2')
    assert s.buy(start,'initial-part2',5000,external_id='fill-2')==b
    assert a['strategy_evidence'] and b['strategy_evidence']
    early=s.advise('2026-06-05')
    assert early['status']=='HOLD' and early['actual_quantity']==15000 and early['days_since_add']<10
    add=s.advise('2026-07-01')
    assert add['status']=='ALLOW' and add['target_ratio']==.215 and add['target_amount']==215000
    assert s.buy(add,'add',6500,external_id='fill-3')['strategy_evidence']
    trim=s.advise('2026-07-06','14')
    assert trim['status']=='TRIM' and trim['target_ratio']==.245 and trim['actual_quantity']==21500
    assert trim['actionable'] and not trim['tradable']
    assert s.sell('2026-07-07','sub',2500,'14','trim')['status']=='effective'
    after=s.advise('2026-07-07','14')
    assert after['actual_quantity']==19000 and after['last_actual_buy_day']=='2026-07-02'
    exit=s.advise('2026-07-08','7')
    assert exit['status']=='EXIT' and exit['target_amount']==0 and exit['actionable']
    assert s.sell('2026-07-09','main',15000,'7','exit-main')['status']=='effective'
    assert s.sell('2026-07-09','sub',4000,'7','exit-sub')['status']=='effective'
    final=s.advise('2026-07-09','7')
    assert final['status']=='NO_POSITION' and final['actual_quantity']==0 and not final['actionable']
    assert s.strategy.core_positions_used(s.account,'2026-07-09')[0]==0


def test_ndx_manual_missing_and_cross_account_evidence(ledger):
    s=Scenario(ledger)
    start=s.advise('2026-06-01')
    other=s.svc.create_account(AccountInput(request_id='other',name='Other',effective_at='2025-01-01T00:00:00+08:00'))['id']
    wrong=s.svc.submit(TradeInput(request_id='other-buy',account_id=other,instrument_id=s.instrument,side='buy',quantity=100,
        price='10',fees_fen=0,executed_at='2026-06-02T10:00:00+08:00',trading_day='2026-06-02',strategy_id='NDX_AUX_T5',
        lot_role='main',signal_id=start['signal_id'],signal_date=start['signal_date']))
    assert wrong['status']=='effective' and wrong['strategy_evidence'] is None
    assert s.advise('2026-06-01',verified=False)['status']=='DATA_UNAVAILABLE'
    # A manual ordinary holding cannot silently be adopted into the T5 track.
    s.svc.submit(trade(s.account,s.instrument,'manual-ordinary',quantity=100,price='10',executed_at='2026-06-02T10:00:00+08:00',trading_day='2026-06-02'))
    assert s.advise('2026-06-05')['status']=='DATA_UNAVAILABLE'


def test_ndx_competing_core_claims_and_unlinked_t5(ledger):
    s=Scenario(ledger)
    # A separate core claim for this same instrument cannot be double counted.
    s.advise('2026-06-01',persist=False)
    s.strategy.store.save_plan(account_id=s.account,index_id='NDX',signal_date='2026-06-01',strategy_version=INDEX_STRATEGY_VERSION,status='SIGNAL')
    assert s.advise('2026-06-01')['status']=='CAPACITY_BLOCKED'
    s.strategy.store.clear_plan(s.account,'NDX',reason='test cancellation')
    result=s.svc.submit(TradeInput(request_id='manual-t5',account_id=s.account,instrument_id=s.instrument,side='buy',quantity=100,
        price='10',fees_fen=0,executed_at='2026-06-02T10:00:00+08:00',trading_day='2026-06-02',strategy_id='NDX_AUX_T5',lot_role='main'))
    assert result['status']=='effective' and not result['strategy_evidence']
    assert s.advise('2026-07-01')['status']=='DATA_UNAVAILABLE'


def test_ndx_public_pool_and_database_reopen(cfg):
    from backend.infrastructure.database import open_database
    from backend.application.trading.service import TradeService
    from backend.domain.strategy.regime import RegimeSnapshot
    with open_database(cfg) as db:
        svc=TradeService(db)
        account=svc.create_account(AccountInput(request_id='account',name='NDX restart',effective_at='2025-01-01T00:00:00+08:00'))['id']
        s=Scenario((svc,account,None))
        day='2026-06-01'; now=datetime.fromisoformat(day+'T16:00:00+08:00')
        strategy=StrategyService(db,data=FakeData(ndx_series(day,9.1)),clock=lambda:now)
        strategy.regime=lambda **kwargs:RegimeSnapshot(state='UP',as_of=day)
        def aux(persist):
            return next(r['ndx_aux'] for r in strategy.pool(account,persist=persist)['items'] if r['index_id']=='NDX')
        read=aux(False)
        assert read['actionable'] and read['signal_id'] is None
        start=aux(True)
        assert start['actionable'] and start['signal_id']
        assert s.buy(start,'initial',15000,external_id='first')['strategy_evidence']
        instrument=s.instrument
    with open_database(cfg) as db:
        day='2026-07-01'; now=datetime.fromisoformat(day+'T16:00:00+08:00')
        PortfolioService(db,clock=lambda:now).quote(QuoteInput(request_id='reopened-quote',instrument_id=instrument,price='9.1',
            source='synthetic independent quote',observed_at=now,trading_day=day,kind='close',quality='valid'))
        strategy=StrategyService(db,data=FakeData(ndx_series(day,9.1)),clock=lambda:now)
        strategy.regime=lambda **kwargs:RegimeSnapshot(state='UP',as_of=day)
        pos=strategy.position_rows(account)['items'][0]
        assert pos['index_id']=='NDX' and pos['live_quantity']==15000 and pos['position'] is None
        assert pos['ndx_aux']['status']=='ALLOW' and pos['ndx_aux']['last_actual_buy_day']=='2026-06-02'
        assert strategy.core_positions_used(account,day)[0]==0


def test_ndx_other_core_cash_buffer_after_initial(ledger):
    s=Scenario(ledger)
    initial=s.advise('2026-06-01')
    s.buy(initial,'initial',15000,external_id='initial')
    core=ledger[2]
    assert s.svc.submit(trade(s.account,core,'core',quantity=70000,price='10',fees_fen=0,
        executed_at='2026-06-03T10:00:00+08:00',trading_day='2026-06-03',strategy_id='INDEX_STRATEGY_ATR',lot_role='main'))['status']=='effective'
    day='2026-07-01';now=datetime.fromisoformat(day+'T16:00:00+08:00')
    PortfolioService(s.svc.db,clock=lambda:now).quote(QuoteInput(request_id='core-quote',instrument_id=core,price='10',source='synthetic',observed_at=now,trading_day=day,kind='close',quality='valid'))
    add=s.advise(day)
    assert s.strategy.core_positions_used(s.account,day)[0]==1
    assert add['status']=='CASH_BLOCKED' and add['actual_quantity']==15000 and not add['actionable']


def test_ndx_manual_sale_actual_allocation_rebuild(ledger):
    s=Scenario(ledger); initial=s.advise('2026-06-01'); s.buy(initial,'initial',15000,external_id='initial')
    lot=next(l for l in data(s.svc,'lots') if l['strategy_id']=='NDX_AUX_T5' and l['remaining_qty']>0)
    manual=s.svc.submit(trade(s.account,s.instrument,'manual-sell',side='sell',quantity=5000,price='10',fees_fen=0,
        executed_at='2026-06-03T10:00:00+08:00',trading_day='2026-06-03',strategy_id='NDX_AUX_T5',lot_role='main',allocations={lot['id']:5000}))
    assert manual['status']=='effective'
    plan=s.advise('2026-07-01')
    assert plan['actual_quantity']==10000 and plan['last_actual_buy_day']=='2026-06-02' and plan['status']=='ALLOW'
