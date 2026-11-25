#!/usr/bin/env python3
"""Create a NEW isolated synthetic demo; never read or bind the formal database."""
import argparse
import json
from datetime import datetime, UTC
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.application.finance import FinanceService, BalanceInput, BillInput, TransferInput
from backend.application.market_calendar import market_window, session_bounds, CalendarUnavailable
from backend.application.portfolio import PortfolioService, QuoteInput
from backend.application.strategy.series import StrategyData, ETF_UNADJUSTED, INDEX_BASIS
from backend.application.trading.service import TradeService, AccountInput, CashInput, InstrumentInput, TradeInput
from backend.domain.strategy.config import get_config
from backend.domain.strategy.calendars import market_clock
from backend.domain.strategy.market_data import Bar
from backend.infrastructure.database import initialize_database, DatabaseSettings, open_database


def create(root: Path):
    root=root.resolve()
    # New directory, not an overwrite operation. All facts are fictional.
    root.mkdir(parents=True,exist_ok=False)
    path=root/'demo.sqlite'
    identity=initialize_database(path,role='sandbox',confirm=True,binding_path=root/'never-formal.json')
    settings=DatabaseSettings(path,identity.database_uuid,identity.generation,mode='local',writable_sandbox=True,binding_path=root/'never-formal.json')
    now=datetime.now(UTC)
    with open_database(settings) as db:
        t=TradeService(db); f=FinanceService(db); p=PortfolioService(db)
        accounts={kind:t.create_account(AccountInput(request_id='demo-account-'+kind,name='演示-'+label,kind=kind,effective_at='2025-12-01T08:00:00+08:00'))['id'] for kind,label in [('securities','证券'),('bank','储蓄'),('cash','钱包')]}
        a=accounts['securities']
        t.cash(CashInput(request_id='demo-capital',account_id=a,kind='opening',amount_fen=100_000_000,occurred_at='2025-12-01T09:00:00+08:00',note='合成示例，不是真实资金'))
        instruments={}
        for code,market,kind,price in [('510300','SH','etf','4.2000'),('159915','SZ','etf','2.1000'),('600519','SH','stock','1450.0000')]:
            i=t.create_instrument(InstrumentInput(request_id='demo-instrument-'+code,code=code,market=market,kind=kind))['id']; instruments[code]=i
            t.submit(TradeInput(request_id='demo-buy-'+code,account_id=a,instrument_id=i,side='buy',quantity=100,price=price,fees_fen=500,executed_at='2026-01-05T10:00:00+08:00',trading_day='2026-01-05',note='合成成交，不含真实策略执行证据'))
        t.submit(TradeInput(request_id='demo-sale',account_id=a,instrument_id=instruments['510300'],side='sell',quantity=20,price='4.3000',fees_fen=100,executed_at='2026-01-06T10:00:00+08:00',trading_day='2026-01-06',note='合成卖出'))
        balances={}
        for kind,amount in [('bank',2_000_000),('cash',50_000)]:
            balances[kind]=f.balance(BalanceInput(request_id='demo-balance-'+kind,account_id=accounts[kind],after_fen=amount,expected_balance_id=None,occurred_at='2026-01-05T08:00:00+08:00',note='合成余额'))['id']
        f.bill(BillInput(request_id='demo-expense',account_id=accounts['bank'],kind='expense',category_id='other_expense',amount_fen=12500,occurred_at='2026-01-06T12:00:00+08:00',note='合成账单'))
        f.transfer(TransferInput(request_id='demo-transfer',from_account_id=accounts['bank'],to_account_id=accounts['cash'],amount_fen=10000,occurred_at='2026-01-07T10:00:00+08:00',from_included=False,to_included=False,from_balance_id=balances['bank'],to_balance_id=balances['cash'],note='合成转账'))
        try:
            window=market_window(now)
            days={window.previous,window.latest_completed}
            for code,i in instruments.items():
                price={'510300':'4.5000','159915':'2.3500','600519':'1460.0000'}[code]
                for day in days:
                    close=session_bounds(day)[1]
                    if close<=now:
                        p.quote(QuoteInput(request_id=f'demo-quote-{code}-{day}',instrument_id=i,price=price,source='synthetic-demo-only',observed_at=close,trading_day=day,kind='close',quality='valid'))
        except CalendarUnavailable:
            pass  # no fabricated sessions
        # Provide illustrative series as explicitly UNVERIFIED; no executable signal.
        d=StrategyData(db)
        for idx in ('000300.SH','NDX'):
            config=get_config(idx)
            for basis,market in [(ETF_UNADJUSTED,config.clock_market),(INDEX_BASIS,config.market)]:
                clock=market_clock(market)
                completed=clock.last_completed_session(now=now)
                if not completed: continue
                days=[day for day in clock.trading_days_after('2025-01-02',600) if day<=completed][-220:]
                bars=[]
                for n,day in enumerate(days):
                    price=3+n*.001 if basis==ETF_UNADJUSTED else 3000+n*2
                    bars.append(Bar(date=day,open=price,high=price*1.01,low=price*.99,close=price,volume=1000))
                d._store(idx,basis,bars,source='synthetic-demo-only',source_verified=False)
    result={'database':str(path),'uuid':identity.database_uuid,'generation':identity.generation,'role':'sandbox','notice':'全部合成；不连接正式数据库，不运行实网刷新'}
    (root/'DEMO.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--target',type=Path,required=True)
    print(json.dumps(create(parser.parse_args().target),ensure_ascii=False,indent=2))
