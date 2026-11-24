"""策略服务的数据安全专项：只读账本、幂等信号、历史回放、偏离成交、证据可追溯。

这些用例全部用**固定夹具行情**（不联网），断言的是"策略刷新绝不改动真实账本"这条
边界，以及"刷新多少次都只产生一条等价正式信号"这条幂等性。
期望值来自 ``test_strategy_golden.py`` 已经验证过的旧函数口径，不在这里反推。
"""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
import exchange_calendars

from backend.application.portfolio import PortfolioService, QuoteInput
from backend.application.strategy.series import SeriesError
from backend.application.strategy.service import StrategyService
from backend.application.strategy.store import StrategyStore
from backend.application.trading.service import CashInput, InstrumentInput
from backend.domain.strategy.market_data import Bar, IndexSeries
from backend.domain.strategy.symbols import configured_index_ids, etf_code_of
from test_trading import cfg, ledger, snapshot, trade  # noqa: F401 - fixtures

SHANGHAI = ZoneInfo('Asia/Shanghai')


def sessions(count=80, until='2026-11-24'):
    """真实 XSHG 交易日（升序），保证与 market_clock 的 next_trading_day 一致。"""
    calendar = exchange_calendars.get_calendar('XSHG')
    days = [str(session.date()) for session in calendar.sessions
            if str(session.date()) <= until]
    return days[-count:]


def rising_bars(count=80, *, breakout=130.0):
    """前 count-1 根缓慢上行，最后一根向上突破（不含当日最高价的窗口判定）。"""
    days = sessions(count)
    bars = []
    for index, day in enumerate(days):
        last = index == count - 1
        close = breakout if last else 100.0 + index * 0.1
        opened = close - 0.2
        bars.append(Bar(date=day, open=round(opened, 4), high=round(close + 0.3, 4),
                        low=round(opened - 0.3, 4), close=round(close, 4), volume=1000.0))
    return bars


class FakeData:
    """固定夹具行情：按 completed_on 截断，模拟真实装载器的可见窗口。"""

    def __init__(self, bars=None, *, failing=()):
        self.bars = bars if bars is not None else rising_bars()
        self.failing = set(failing)
        self.calls: list[tuple[str, str]] = []

    def index_series(self, index_id, *, completed_on, allow_network=True):
        self.calls.append((index_id, str(completed_on)[:10]))
        if index_id in self.failing:
            raise SeriesError('夹具行情不可用', detail='fixture_unavailable')
        kept = tuple(bar for bar in self.bars if bar.date <= str(completed_on)[:10])
        return IndexSeries(index_id=index_id, name=index_id, market='CN', bars=kept,
                           source='fixture', source_verified=True)

    def input_version(self, index_id, *, completed_on):
        kept = [bar for bar in self.bars if bar.date <= str(completed_on)[:10]]
        return f'fixture-{len(kept)}'


def midday(day):
    return datetime.combine(date.fromisoformat(str(day)[:10]), time(15, 30), SHANGHAI)


def fund(svc, account, *, amount_fen=100_000_000):
    """建立可核实的现金基线：没有基线时策略只允许给出 CASH_BLOCKED。"""
    svc.cash(CashInput(request_id=f'fund-{account}', account_id=account, kind='opening',
                       amount_fen=amount_fen, occurred_at='2025-12-01T09:30:00+08:00',
                       note='verified statement'))


def quote_for(svc, instrument, *, day, price):
    """给真实批次一个可核实的收盘价，让权益校验不再因缺报价降级。"""
    PortfolioService(svc.db).quote(QuoteInput(
        request_id=f'quote-{instrument}-{day}', instrument_id=instrument, price=price,
        source='synthetic_test', observed_at=datetime.fromisoformat(f'{day}T15:00:00+08:00'),
        trading_day=date.fromisoformat(day), kind='close', quality='valid'))


def service(db, data=None):
    return StrategyService(db, data=data if data is not None else FakeData())


def signals(db):
    with db.read() as conn:
        return conn.execute('SELECT signal_id,index_id,signal_date,status,atr14,buy_max,'
                            'threshold_high,price_basis,price_unit FROM strategy_signals'
                            ' ORDER BY index_id').fetchall()


def plans(db):
    with db.read() as conn:
        return conn.execute('SELECT plan_id,index_id,signal_date,status,buy_max,atr14'
                            ' FROM strategy_plans ORDER BY index_id').fetchall()


def test_refresh_never_touches_the_real_ledger(ledger):
    """策略刷新只读账本：成交、批次、现金、账本版本一律不变。"""
    svc, account, instrument = ledger
    svc.submit(trade(account, instrument, 'ordinary-buy', quantity=100, price='3.1000'))
    before = snapshot(svc)
    versions = dict(svc.list_records('trades'))
    strategy = service(svc.db)
    as_of = sessions(1)[0]
    for _ in range(2):
        view = strategy.pool(account, as_of=as_of)
        assert view['as_of'] == as_of and len(view['items']) == 14
    assert snapshot(svc) == before
    assert svc.list_records('trades')['ledger_revision'] == versions['ledger_revision']
    with svc.db.read() as conn:
        assert conn.execute('SELECT count(*) FROM trade_executions').fetchone() == (1,)
        assert conn.execute('SELECT count(*) FROM lot_allocations').fetchone() == (0,)


def test_repeated_refresh_keeps_one_signal_per_day(ledger):
    """同一信号日重复刷新只留一条等价正式信号与一条计划。"""
    svc, account, instrument = ledger
    fund(svc, account)
    strategy = service(svc.db)
    as_of = sessions(1)[0]
    first = strategy.evaluate('000300.SH', account_id=account, as_of=as_of)
    assert first['status'] == 'SIGNAL'
    assert first['signal_date'] == as_of
    assert first['price_basis'] == 'index_level' and first['price_unit'] == '元'
    for _ in range(2):
        again = strategy.evaluate('000300.SH', account_id=account, as_of=as_of)
        assert again['status'] == 'PLAN_PENDING'
    rows = signals(svc.db)
    assert len(rows) == 1
    assert rows[0][1] == '000300.SH' and rows[0][2] == as_of and rows[0][3] == 'SIGNAL'
    assert len(plans(svc.db)) == 1


def test_history_replay_never_sees_the_future_bar(ledger):
    """as_of 早于突破日时，回放不得提前产生信号。"""
    svc, account, instrument = ledger
    fund(svc, account)
    strategy = service(svc.db)
    days = sessions(80)
    early = strategy.pool(account, as_of=days[-2])
    row = next(item for item in early['items'] if item['index_id'] == '000300.SH')
    assert row['status'] == 'NO_SIGNAL'
    assert row['signal_date'] == days[-2]
    late = strategy.pool(account, as_of=days[-1])
    assert next(item for item in late['items']
                if item['index_id'] == '000300.SH')['status'] == 'SIGNAL'
    with svc.db.read() as conn:
        dates = [r[0] for r in conn.execute(
            'SELECT DISTINCT signal_date FROM strategy_signals ORDER BY signal_date')]
    assert dates == [days[-1]]


def test_insufficient_cash_blocks_the_plan(ledger):
    """现金不足时只给 CASH_BLOCKED，不落任何买入计划。"""
    svc, account, instrument = ledger
    fund(svc, account, amount_fen=100_000_000)
    as_of = sessions(1)[0]
    # 现金几乎全部换成另一只 ETF：权益仍在，可用现金低于基础仓目标。
    other = svc.create_instrument(InstrumentInput(request_id='instrument-510500',
                                                  code='510500', market='SH', kind='etf'))['id']
    svc.submit(trade(account, other, 'drain', quantity=90_000, price='10.0000'))
    quote_for(svc, other, day=as_of, price='10.0000')
    strategy = service(svc.db)
    view = strategy.evaluate('000300.SH', account_id=account, as_of=as_of)
    assert view['status'] == 'CASH_BLOCKED'
    assert 'cash_shortfall' in view['reason_codes']
    assert plans(svc.db) == []


def test_deviation_trade_is_kept_and_flagged(ledger):
    """真实发生但偏离策略的成交仍留在账本里，只标注证据未核实。"""
    svc, account, instrument = ledger
    svc.submit(trade(account, instrument, 'deviation', quantity=10, price='3.1000',
                     strategy_id='INDEX_STRATEGY_ATR', lot_role='main',
                     signal_date='2025-12-31', frozen_atr='0.1234',
                     strategy_deviation=True))
    before = snapshot(svc)
    strategy = service(svc.db)
    strategy.position_rows(account, as_of=sessions(1)[0])
    assert snapshot(svc) == before
    evidence = PortfolioService(svc.db).strategy_evidence(account, midday(sessions(1)[0]))
    assert evidence['status'] == 'unavailable'
    item = evidence['items'][0]
    assert item['evidence_status'] == 'unverified'
    assert 'strategy_position_not_recorded' in item['reason_codes']
    assert item['frozen_atr'] == 1234 and item['index_id'] == '000300.SH'


def test_correction_keeps_evidence_traceable(ledger):
    """交易更正后，策略证据仍能按新批次追溯，原成交保留为 reversed。"""
    svc, account, instrument = ledger
    original = svc.submit(trade(account, instrument, 'orig-buy', quantity=10, price='3.1000',
                                strategy_id='INDEX_STRATEGY_ATR', lot_role='main',
                                signal_date='2025-12-31', frozen_atr='0.1234'))
    replacement = trade(account, instrument, 'fixed-buy', quantity=20, price='3.2000',
                        strategy_id='INDEX_STRATEGY_ATR', lot_role='main',
                        signal_date='2025-12-31', frozen_atr='0.1234', note='broker correction')
    svc.correct(original['id'], replacement)
    assert svc.get(original['id'])['status'] == 'reversed'
    evidence = PortfolioService(svc.db).strategy_evidence(account, midday(sessions(1)[0]))
    assert [(item['quantity'], item['frozen_atr']) for item in evidence['items']] == [(20, 1234)]
    assert evidence['items'][0]['reason_codes'] == ['strategy_position_not_recorded']


def test_unavailable_index_degrades_instead_of_inventing(ledger):
    """单只指数行情缺失时只降级该只，其余照常，且写清原因。"""
    svc, account, instrument = ledger
    fund(svc, account)
    strategy = service(svc.db, FakeData(failing={'000905.SH'}))
    view = strategy.pool(account, as_of=sessions(1)[0])
    broken = next(item for item in view['items'] if item['index_id'] == '000905.SH')
    assert broken['status'] == 'DATA_UNAVAILABLE'
    assert broken['reason_codes'] == ['index_series_unavailable']
    assert broken['series'] == {'available': False}
    healthy = next(item for item in view['items'] if item['index_id'] == '000300.SH')
    assert healthy['status'] == 'SIGNAL'


def test_signal_records_its_price_basis_and_input_version(ledger):
    """正式信号必须带价格口径、单位与输入数据版本。"""
    svc, account, instrument = ledger
    fund(svc, account)
    strategy = service(svc.db)
    as_of = sessions(1)[0]
    strategy.evaluate('000300.SH', account_id=account, as_of=as_of)
    with svc.db.read() as conn:
        row = conn.execute('SELECT index_id,signal_date,input_version,price_basis,price_unit,'
                           'instrument_code,strategy_version,source_verified,is_proxy'
                           ' FROM strategy_signals').fetchone()
    assert row[0] == '000300.SH' and row[1] == as_of
    assert row[2] == 'fixture-80'
    assert row[3] == 'index_level' and row[4] == '元'
    assert row[5] == '510300'
    assert row[6] == 'index-strategy-atr-v9.2'
    assert row[7] == 1 and row[8] == 0


def test_etf_basis_is_never_used_for_the_signal(ledger):
    """ETF 代码只是交易标的，信号一律来自指数点位。"""
    svc, account, instrument = ledger
    strategy = service(svc.db)
    as_of = sessions(1)[0]
    view = strategy.evaluate('000300.SH', account_id=account, as_of=as_of)
    assert view['price_basis'] == 'index_level'
    assert view['etf_code'] == etf_code_of('000300.SH') == '510300'
    assert view['series']['source'] == 'fixture'


def test_all_fourteen_indices_are_evaluated(ledger):
    """观察池必须覆盖全部 14 只配置指数，一只都不能漏。"""
    svc, account, instrument = ledger
    view = service(svc.db).pool(account, as_of=sessions(1)[0])
    assert [item['index_id'] for item in view['items']] == list(configured_index_ids())
    assert view['max_positions'] == 8
    assert view['regime']['state'] in ('UP', 'DOWN', 'RANGE', 'UNKNOWN')


def test_store_never_writes_trading_tables(ledger):
    """策略存储只在策略表内落地，绝不写成交/批次/现金。"""
    svc, account, instrument = ledger
    store = StrategyStore(svc.db)
    before = snapshot(svc)
    store.save_signal(index_id='000300.SH', signal_date='2026-11-20',
                      input_version='fixture-1', strategy_version='index-strategy-atr-v9.2',
                      config_version='index-strategy-atr-v9.2', status='SIGNAL',
                      account_id=account, instrument_code='510300', atr14=1.5, buy_max=4.5)
    store.save_plan(index_id='000300.SH', signal_date='2026-11-20',
                    strategy_version='index-strategy-atr-v9.2', status='SIGNAL',
                    account_id=account, atr14=1.5, buy_max=4.5)
    store.save_cooldown(account, '000300.SH', exit_date='2026-11-18', exit_action='STOP',
                        exit_reason='fixture', required_days=3, cooldown_until='2026-11-23')
    store.save_ndx_aux(account, ratio=0.15, last_add_date='2026-11-01')
    assert snapshot(svc) == before
    assert store.load_ndx_aux(account)['ratio'] == pytest.approx(0.15)
    assert store.load_cooldown(account, '000300.SH')['cooldown_until'] == '2026-11-23'
