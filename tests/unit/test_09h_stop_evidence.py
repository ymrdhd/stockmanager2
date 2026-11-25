"""Synthetic real-ledger protection evidence; no new strategy formula."""
from test_trading import cfg, ledger, trade, snapshot
from test_strategy_service import fund, FakeData, flat_bars
from test_pnl import at, quote
from backend.application.portfolio import PortfolioService
from backend.application.strategy.service import StrategyService
from backend.application.strategy.store import StrategyStore
from backend.domain.strategy.config import get_config


def test_verified_main_sub_and_expired_quote(ledger):
    svc,a,i=ledger
    fund(svc,a)
    for role,price in [('main','3.50'),('sub','3.60')]:
        signal=StrategyStore(svc.db,clock=lambda:at(5,15,30)).save_signal(
            account_id=a,index_id='000300.SH',instrument_code='510300',signal_date='2026-01-05',
            input_version='09h-'+role,strategy_version='index-strategy-atr-v9.2',config_version='cfg:fixture',
            status='SIGNAL',price_basis='etf_unadjusted',price_unit='元',atr14=.05,source_verified=True)
        svc.submit(trade(a,i,'09h-'+role,day=6,lot_role=role,strategy_id='INDEX_STRATEGY_ATR',
                         price=price,frozen_atr='0.05',signal_date='2026-01-05',signal_id=signal['signal_id']))
    now=[at(6,10)]
    strategy=StrategyService(svc.db,clock=lambda:now[0],data=FakeData(flat_bars(4000,50,until='2026-01-05'),
                              etf_bars=flat_bars(4,.05,until='2026-01-05')))
    quote(PortfolioService(svc.db),i,6,'3.50',kind='intraday',hour=9,minute=59)
    held=strategy._custody(a,'2026-01-05')
    assert len(held)==2 and all(l['signal_verified'] for l in held)
    position,reason=strategy._state_from_lots('000300.SH',get_config('000300.SH'),held)
    assert position is not None and not reason
    rev=svc.list_records('lots')['ledger_revision']
    before=snapshot(svc)
    warnings=strategy._near_stop(a,position,held,'2026-01-05',rev)
    assert {(w['role'],w['status']) for w in warnings}=={('main','near_stop'),('sub','protection_reached')}
    assert all(w['display_only'] and w['quote_source']=='synthetic_test' and w['threshold_bps']==200 for w in warnings)
    assert strategy._near_stop(a,position,held,'2026-01-05',rev-1)==[]
    assert strategy._near_stop(a,position,[{**held[0],'signal_verified':False},held[1]],'2026-01-05',rev)==[]
    now[0]=at(6,11,20)
    assert strategy._near_stop(a,position,held,'2026-01-05',rev)==[]
    assert snapshot(svc)==before
