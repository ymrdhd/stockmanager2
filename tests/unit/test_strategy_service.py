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
from backend.application.strategy.series import ETF_UNADJUSTED, SeriesError, StrategyData
from backend.application.strategy.service import StrategyService
from backend.application.strategy.store import StrategyStore
from backend.application.trading.service import AccountInput, CashInput, InstrumentInput
from backend.domain.strategy.calendars import CalendarCoverageError, market_clock
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
    """固定夹具行情：按 completed_on 截断，模拟真实装载器的可见窗口。

    同时提供 ``index_series``（指数点位）与 ``etf_series``（ETF 未复权人民币日线）
    两条通道：默认配置 ``uses_etf_underlying=True``，V9 必须走 ETF 通道。
    """

    def __init__(self, bars=None, *, failing=(), etf_bars=None):
        self.bars = bars if bars is not None else rising_bars()
        self.etf_bars = etf_bars if etf_bars is not None else self.bars
        self.failing = set(failing)
        self.calls: list[tuple[str, str, str]] = []

    def _kept(self, source, completed_on):
        cutoff = str(completed_on)[:10]
        return tuple(bar for bar in source if bar.date <= cutoff)

    def index_series(self, index_id, *, completed_on, allow_network=True):
        self.calls.append(('index_level', index_id, str(completed_on)[:10]))
        if index_id in self.failing:
            raise SeriesError('夹具行情不可用', detail='fixture_unavailable')
        return IndexSeries(index_id=index_id, name=index_id, market='CN',
                           bars=self._kept(self.bars, completed_on),
                           source='fixture', source_verified=True)

    def etf_series(self, index_id, *, completed_on, basis='etf_unadjusted',
                   allow_network=True):
        self.calls.append((basis, index_id, str(completed_on)[:10]))
        if index_id in self.failing:
            raise SeriesError('夹具行情不可用', detail='fixture_unavailable')
        return IndexSeries(index_id=index_id, name=index_id, market='CN',
                           bars=self._kept(self.etf_bars, completed_on),
                           source='fixture', source_verified=True)

    def input_version(self, index_id, *, completed_on, basis='index_level'):
        kept = self._kept(self.etf_bars if basis != 'index_level' else self.bars,
                          completed_on)
        return f'fixture-{basis}-{len(kept)}'

    def evaluation_version(self, index_id, *, completed_on, basis='index_level',
                           peer_versions=None):
        kept = self._kept(self.etf_bars if basis != 'index_level' else self.bars,
                          completed_on)
        return f'eval-{basis}-{len(kept)}'


def flat_bars(close: float, atr: float, count=80, until='2026-11-24'):
    """构造一组"ATR 恰好等于 atr、末日收盘恰好等于 close"的日线。

    前 count-1 根完全横盘（close = close-atr），每根 TR 都恰好等于 atr；
    最后一根把收盘推到 close 形成突破（阈值不含当日），同时保持 TR = atr。
    这样 ATR14 精确等于 atr，可以严格断言价格尺度，不用近似容差掩盖混算。
    ``until`` 用来构造"行情只到前一交易日"的过期夹具。
    """
    days = sessions(count, until=until)
    prev = close - atr
    bars = []
    for index, day in enumerate(days):
        if index == count - 1:
            bars.append(Bar(date=day, open=round(close - 0.3 * atr, 6), high=round(close, 6),
                            low=round(prev, 6), close=round(close, 6), volume=1000.0))
        else:
            bars.append(Bar(date=day, open=round(prev, 6), high=round(close - atr / 2, 6),
                            low=round(close - 1.5 * atr, 6), close=round(prev, 6),
                            volume=1000.0))
    return bars


def main_lot_trade(account, instrument, req, *, signal_date, frozen_atr, price='3.4567'):
    """策略主仓买入（携带可核实的信号日与冻结 ATR）。"""
    return trade(account, instrument, req, strategy_id='INDEX_STRATEGY_ATR',
                 lot_role='main', signal_date=signal_date, frozen_atr=frozen_atr,
                 price=price)


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
    return StrategyService(db, data=data if data is not None else FakeData(), clock=lambda: datetime.fromisoformat('2026-11-24T16:00:00+08:00'))


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
    first = strategy.evaluate('000300.SH', account_id=account, as_of=as_of, persist=True)
    assert first['status'] == 'SIGNAL'
    assert first['signal_date'] == as_of
    # ISS-031：默认 ETF 模式下 V9 用 ETF **未复权人民币日线**，单位是元。
    assert first['price_basis'] == 'etf_unadjusted' and first['price_unit'] == '元'
    for _ in range(2):
        again = strategy.evaluate('000300.SH', account_id=account, as_of=as_of, persist=True)
        assert again['status'] == 'PLAN_PENDING'
    rows = signals(svc.db)
    assert len(rows) == 1
    assert rows[0][1] == '000300.SH' and rows[0][2] == as_of and rows[0][3] == 'SIGNAL'
    assert len(plans(svc.db)) == 1


def test_read_only_query_never_records_a_signal(ledger):
    """GET 语义（persist=False）只计算不落库：看一次页面不得产生正式证据。"""
    svc, account, instrument = ledger
    fund(svc, account)
    strategy = service(svc.db)
    as_of = sessions(1)[0]
    view = strategy.evaluate('000300.SH', account_id=account, as_of=as_of)
    assert view['status'] == 'SIGNAL' and view['read_only'] is True
    assert 'read_only_signal_not_recorded' in view['reason_codes']
    assert signals(svc.db) == [] and plans(svc.db) == []


def test_history_replay_never_sees_the_future_bar(ledger):
    """as_of 早于突破日时，回放不得提前产生信号；只读回放也不写任何正式证据。"""
    svc, account, instrument = ledger
    fund(svc, account)
    strategy = service(svc.db)
    days = sessions(80)
    early = strategy.pool(account, as_of=days[-2])
    row = next(item for item in early['items'] if item['index_id'] == '000300.SH')
    assert row['status'] == 'NO_SIGNAL'
    assert row['signal_date'] == days[-2]
    # ISS-034：历史只读回放不得写入任何当前正式证据。
    with svc.db.read() as conn:
        assert conn.execute('SELECT count(*) FROM strategy_signals').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM strategy_plans').fetchone() == (0,)
    # Isolate the one technical opportunity; the pool's simultaneous capacity
    # gate has its own regression and must not silently rank multiple signals.
    late = strategy.evaluate('000300.SH', account_id=account, as_of=days[-1], persist=True)
    assert late['status'] == 'SIGNAL'
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
    assert broken['reason_codes'] == ['series_unavailable']
    assert broken['price_basis'] == 'etf_unadjusted'
    assert broken['series'] == {'available': False}
    healthy = next(item for item in view['items'] if item['index_id'] == '000300.SH')
    assert healthy.get('technical_status', healthy['status']) == 'SIGNAL'
    assert healthy['series']['available'] and not healthy['tradable']


def test_signal_records_its_price_basis_and_input_version(ledger):
    """正式信号必须带价格口径、单位与输入数据版本。"""
    svc, account, instrument = ledger
    fund(svc, account)
    strategy = service(svc.db)
    as_of = sessions(1)[0]
    strategy.evaluate('000300.SH', account_id=account, as_of=as_of, persist=True)
    with svc.db.read() as conn:
        row = conn.execute('SELECT index_id,signal_date,input_version,price_basis,price_unit,'
                           'instrument_code,strategy_version,source_verified,is_proxy,'
                           'config_version FROM strategy_signals').fetchone()
    assert row[0] == '000300.SH' and row[1] == as_of
    assert row[2] == 'eval-etf_unadjusted-80'
    # ISS-031：ETF 模式的价格口径是未复权人民币价，不是指数点位。
    assert row[3] == 'etf_unadjusted' and row[4] == '元'
    assert row[5] == '510300'
    assert row[6] == 'index-strategy-atr-v9.2'
    assert row[7] == 1 and row[8] == 0
    # ISS-034：配置版本必须是真实规则参数的指纹，不是策略版本号的复制品。
    assert row[9].startswith('cfg:') and row[9] != 'index-strategy-atr-v9.2'


def test_etf_mode_never_mixes_index_points_into_the_v9_decision(ledger):
    """ISS-031 黄金样例：同日两套行情，V9 必须用 ETF 元价，G2 仍用指数点位。

    指数收盘 4000 点、ATR 50 点；ETF 收盘 4.00 元、ATR 0.05 元。
    V9 的突破/ATR/BuyMax 只能来自 ETF（BuyMax = 4.00 + 0.5×0.05 = 4.025 元），
    50 点绝不能作为人民币 ATR 落库或预填成交。
    """
    svc, account, instrument = ledger
    fund(svc, account)
    as_of = sessions(80)[-1]
    index_bars = flat_bars(4000.0, 50.0)
    etf_bars = flat_bars(4.0, 0.05)
    strategy = service(svc.db, FakeData(index_bars, etf_bars=etf_bars))
    view = strategy.evaluate('000300.SH', account_id=account, as_of=as_of, persist=True)
    assert view['price_basis'] == 'etf_unadjusted' and view['price_unit'] == '元'
    assert view['atr14'] == pytest.approx(0.05)
    assert view['buy_max'] == pytest.approx(4.025)
    assert view['tradable'] is True
    with svc.db.read() as conn:
        stored = conn.execute('SELECT atr14,buy_max,price_unit,price_basis,instrument_code'
                              ' FROM strategy_signals').fetchone()
    assert stored[0] == pytest.approx(0.05)
    assert stored[1] == pytest.approx(4.025)
    assert stored[2] == '元' and stored[3] == 'etf_unadjusted' and stored[4] == '510300'
    # G2：市场状态仍然来自指数点位（4000 点量级），不是 ETF 元价。
    assert view['regime']['price_basis'] == 'index_level'
    assert view['regime']['price_unit'] == '点'
    # 两条通道都被真实调用：V9 走 ETF，regime 走指数。
    assert any(call[0] == 'etf_unadjusted' for call in strategy.data.calls)
    assert any(call[0] == 'index_level' for call in strategy.data.calls)


def test_adjusted_etf_prices_are_never_used_for_the_signal(ledger):
    """ISS-031：V9 只能用 ETF 未复权人民币日线（旧版 fqt=0 口径）。

    复权价会让历史价格随分红送配整体平移，与真实成交价、批次保护价不同尺度，
    因此 etf_adjusted 绝不能出现在正式信号里。
    """
    svc, account, instrument = ledger
    strategy = service(svc.db)
    as_of = sessions(1)[0]
    view = strategy.evaluate('000300.SH', account_id=account, as_of=as_of)
    assert view['price_basis'] == 'etf_unadjusted'
    assert view['price_unit'] == '元'
    assert view['etf_code'] == etf_code_of('000300.SH') == '510300'
    assert view['series']['source'] == 'fixture'
    # 只有未复权的 ETF 通道被调用，复权通道一次都不能出现。
    assert [call[0] for call in strategy.data.calls].count('etf_unadjusted') == 1
    assert all(call[0] != 'etf_adjusted' for call in strategy.data.calls)


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


# ======================================================================
# 专项 A 组：ISS-032/033/034/035 的真实账本联动与证据边界
# ======================================================================
def etf_fixture():
    """指数点位与 ETF 元价两套同日行情：4000 点/ATR50 与 4.00 元/ATR0.05。"""
    return FakeData(flat_bars(4000.0, 50.0), etf_bars=flat_bars(4.0, 0.05))


def test_real_main_buy_establishes_the_strategy_state(ledger):
    """ISS-032：真实策略主仓成交后，持仓决策必须能立刻找到该批次。"""
    svc, account, instrument = ledger
    fund(svc, account)
    as_of = sessions(80)[-1]
    svc.submit(main_lot_trade(account, instrument, 'main-buy', signal_date='2026-01-05',
                              frozen_atr='0.05'))
    view = service(svc.db, etf_fixture()).position_rows(account, as_of=as_of)
    # 真实批次驱动：即使 strategy_positions 里一条记录都没有，也必须列出该指数。
    assert [item['index_id'] for item in view['items']] == ['000300.SH']
    assert view['custody'], '真实批次必须出现在 custody 里'
    row = view['items'][0]
    assert row['price_basis'] == 'etf_unadjusted' and row['price_unit'] == '元'
    position = row['position']
    assert position['main_lot_id'] == view['custody'][0]['lot_id']
    assert position['signal_atr'] == pytest.approx(0.05)
    assert position['shares'] == 100
    # 保护价与冻结 ATR 处于同一人民币尺度：3.4567 - 0.25×0.05 = 3.44495，按 tick 向上取整。
    assert position['unverified_reference']['initial_stop'] == pytest.approx(3.445, abs=1e-6)
    assert position['stop_price'] is None and position['action'] == 'UNVERIFIED'
    assert row['model_exit'] is None
    assert position['model_only'] is True
    # 证据只来自账本，没有凭空写入策略表。
    with svc.db.read() as conn:
        assert conn.execute('SELECT count(*) FROM strategy_positions').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM strategy_signals').fetchone() == (0,)


def test_sub_lot_keeps_its_own_protective_price(ledger):
    """ISS-032：子仓用**自己**的冻结 ATR 算保护价，绝不借用主仓尺度。"""
    svc, account, instrument = ledger
    fund(svc, account)
    as_of = sessions(80)[-1]
    svc.submit(main_lot_trade(account, instrument, 'main-buy', signal_date='2026-01-05',
                              frozen_atr='0.05', price='3.4567'))
    svc.submit(trade(account, instrument, 'sub-buy', day=6, quantity=50, price='3.6000',
                     strategy_id='INDEX_STRATEGY_ATR', lot_role='sub',
                     signal_date='2026-01-05', frozen_atr='0.0833'))
    row = service(svc.db, etf_fixture()).position_rows(account, as_of=as_of)['items'][0]
    assert row['position']['adds'][0]['stop_price'] is None
    adds = row['position']['unverified_reference']['adds']
    assert len(adds) == 1
    assert adds[0]['signal_atr'] == pytest.approx(0.0833)
    # 子仓独立止损以**自己的**成交价与 ATR 为准：3.6 - 0.25×0.0833 = 3.579175 → tick 上取整。
    assert adds[0]['initial_stop'] == pytest.approx(3.580, abs=1e-6)
    # 子仓保护价与主仓不同：绝不借用主仓冻结 ATR 的尺度。
    assert adds[0]['initial_stop'] != pytest.approx(row['position']['unverified_reference']['initial_stop'])


def test_model_exit_never_closes_or_cools_down_the_real_position(ledger):
    """ISS-032 §5.3：模型退出只是建议——仓位仍在、槽位不释放、冷却不启动。"""
    svc, account, instrument = ledger
    fund(svc, account)
    as_of = sessions(80)[-1]
    signal = StrategyStore(svc.db, clock=lambda: datetime.fromisoformat('2026-01-05T15:30:00+08:00')).save_signal(
        account_id=account,index_id='000300.SH',instrument_code='510300',signal_date='2026-01-05',
        input_version='fixture-exit',strategy_version='index-strategy-atr-v9.2',config_version='cfg:fixture',
        status='SIGNAL',price_basis='etf_unadjusted',price_unit='元',atr14=.05,source_verified=True)
    svc.submit(trade(account, instrument, 'main-buy', day=6, lot_role='main', strategy_id='INDEX_STRATEGY_ATR',
                     signal_date='2026-01-05', frozen_atr='0.05', signal_id=signal['signal_id']))
    strategy = service(svc.db, etf_fixture())
    before = snapshot(svc)
    view = strategy.position_rows(account, as_of=as_of, persist=True)
    row = view['items'][0]
    # 80 根日线 > exit_after_bars(21)：模型必然给出退出建议。
    assert row['model_exit'] is not None
    assert row['model_exit']['model_only'] is True
    assert row['model_exit']['action'] in ('EXIT', 'CLOSED', 'STOP', 'GAP_STOP')
    # 但真实执行状态没有被模型覆盖。
    assert row['position']['exit_filled'] is False
    assert row['position']['closed_date'] is None
    assert row['cooldown']['active'] is False
    # 未释放组合槽位：主仓仍占 1 个核心槽位。
    assert view['items'][0]['live_quantity'] == 100
    core_used, note = strategy.core_positions_used(account, as_of)
    assert core_used == 1 and '真实主仓 1 个' in note
    # 账本一个字节都没变：模型建议不是成交。
    assert snapshot(svc) == before
    with svc.db.read() as conn:
        assert conn.execute('SELECT count(*) FROM strategy_cooldowns').fetchone() == (0,)


def test_two_accounts_plans_never_interfere(ledger):
    """ISS-033：A 账户的计划不得让 B 账户进入 PLAN_PENDING，也不得被 A 取消。"""
    svc, account, instrument = ledger
    fund(svc, account)
    other = svc.create_account(AccountInput(request_id='account-second', name='second',
                                            effective_at='2000-01-01T00:00:00+08:00'))['id']
    fund(svc, other)
    as_of = sessions(1)[0]
    strategy = service(svc.db)
    first = strategy.evaluate('000300.SH', account_id=account, as_of=as_of, persist=True)
    assert first['status'] == 'SIGNAL' and first['signal_id']
    # B 账户同一天同一指数：各自独立判定，不因为 A 有计划就 PLAN_PENDING。
    second = strategy.evaluate('000300.SH', account_id=other, as_of=as_of, persist=True)
    assert second['status'] == 'SIGNAL'
    assert second['signal_id'] != first['signal_id']
    with svc.db.read() as conn:
        owners = {row[0] for row in conn.execute('SELECT DISTINCT account_id FROM strategy_plans')}
    assert owners == {account, other}
    # A 取消自己的计划，B 的计划必须留着。
    strategy.store.clear_plan(account, '000300.SH', reason='fixture cancel')
    assert strategy.store.active_plan(account, '000300.SH') is None
    assert strategy.store.active_plan(other, '000300.SH') is not None
    # 账户 A 的同日重复刷新保持幂等，不会给 B 增加任何记录。
    strategy.evaluate('000300.SH', account_id=account, as_of=as_of, persist=True)
    with svc.db.read() as conn:
        assert conn.execute('SELECT count(*) FROM strategy_plans').fetchone() == (2,)


def test_correction_rebuilds_the_state_from_the_new_lot(ledger):
    """ISS-032：成交更正后，策略状态按新批次重建，原成交保留为 reversed。"""
    svc, account, instrument = ledger
    fund(svc, account)
    as_of = sessions(80)[-1]
    original = svc.submit(main_lot_trade(account, instrument, 'orig-main',
                                         signal_date='2026-01-05', frozen_atr='0.05'))
    corrected = trade(account, instrument, 'fixed-main', quantity=250, price='3.5000',
                      strategy_id='INDEX_STRATEGY_ATR', lot_role='main',
                      signal_date='2026-01-05', frozen_atr='0.05', note='broker correction')
    svc.correct(original['id'], corrected)
    assert svc.get(original['id'])['status'] == 'reversed'
    view = service(svc.db, etf_fixture()).position_rows(account, as_of=as_of)
    row = view['items'][0]
    assert row['position']['shares'] == 250
    assert row['position']['entry_price'] == pytest.approx(3.5)
    # 保护价按更正后的成交价与冻结 ATR 重算：3.5 - 0.25×0.05 = 3.4875 → tick 上取整 3.488。
    assert row['position']['unverified_reference']['initial_stop'] == pytest.approx(3.488, abs=1e-6)
    assert row['position']['initial_stop'] is None
    assert row['position']['action'] == 'UNVERIFIED'
    # 只有一条有效批次，作废的那条不得出现在 custody 里。
    assert len(view['custody']) == 1
    assert view['custody'][0]['lot_id'] == row['position']['main_lot_id']


def test_revised_quotes_produce_a_new_input_version(ledger):
    """ISS-034：价格修订必须产生新的输入版本，原始行情仍可追溯。"""
    svc, account, instrument = ledger
    data = StrategyData(svc.db)
    as_of = sessions(2)[-1]
    bars = flat_bars(4.0, 0.05, count=40)
    first = data._store('000300.SH', ETF_UNADJUSTED, bars, source='fixture:a')
    assert data._store('000300.SH', ETF_UNADJUSTED, bars, source='fixture:a') == first
    revised = list(bars)
    last = revised[-1]
    revised[-1] = Bar(date=last.date, open=last.open, high=last.high, low=last.low,
                      close=round(last.close + 0.01, 6), volume=last.volume)
    second = data._store('000300.SH', ETF_UNADJUSTED, revised, source='fixture:a')
    assert second != first
    with svc.db.read() as conn:
        revisions = [row[0] for row in conn.execute(
            'SELECT revision FROM strategy_bars WHERE series_key=? AND trade_date=?'
            ' ORDER BY revision', (f'{ETF_UNADJUSTED}:000300.SH:510300', last.date))]
    # 同一交易日保留两条修订：原始与修订都在，不覆盖历史。
    assert revisions == [1, 2]
    kept, meta = data.cached('000300.SH', ETF_UNADJUSTED, as_of=as_of)
    assert meta['revision'] == 2 and kept[-1].close == pytest.approx(last.close + 0.01)


def test_calendar_out_of_coverage_never_invents_a_session():
    """ISS-035：日历未覆盖的日期不猜交易日，也不编造下一交易日。"""
    clock = market_clock('CN')
    with pytest.raises(CalendarCoverageError):
        clock.is_trading_day('2099-01-05')
    assert clock.next_trading_day('2099-01-05') is None
    assert clock.trading_days_after('2099-01-05', 3) == []
    assert clock.source_label() == 'exchange_calendars'


def test_stale_or_unverified_series_never_records_a_formal_signal(ledger):
    """ISS-035/036：过期缓存与未核实来源都不得写成"看似已核实的正式信号"。"""
    svc, account, instrument = ledger
    fund(svc, account)
    days = sessions(80)
    as_of = days[-1]
    # 行情只到前一交易日 → 过期，不得落正式信号。
    previous = days[-2]
    stale = FakeData(flat_bars(4000.0, 50.0, count=79, until=previous),
                     etf_bars=flat_bars(4.0, 0.05, count=79, until=previous))
    view = service(svc.db, stale).evaluate('000300.SH', account_id=account, as_of=as_of,
                                           persist=True)
    assert view['status'] == 'DATA_UNAVAILABLE' and not view['tradable']
    assert 'stale_series_not_recorded' in view['reason_codes']
    with svc.db.read() as conn:
        assert conn.execute('SELECT count(*) FROM strategy_signals').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM strategy_plans').fetchone() == (0,)
    # 来源未核实 → 同样不落正式信号，但依然展示建议与原因。
    class Unverified(FakeData):
        def etf_series(self, index_id, *, completed_on, basis='etf_unadjusted',
                       allow_network=True):
            series = super().etf_series(index_id, completed_on=completed_on, basis=basis,
                                        allow_network=allow_network)
            return IndexSeries(index_id=series.index_id, name=series.name,
                               market=series.market, bars=series.bars,
                               source=series.source, source_verified=False)
    pending = service(svc.db, Unverified(flat_bars(4000.0, 50.0),
                                        etf_bars=flat_bars(4.0, 0.05))).evaluate(
        '000300.SH', account_id=account, as_of=as_of, persist=True)
    assert pending['status'] == 'DATA_UNAVAILABLE' and not pending['tradable']
    assert 'unverified_source_not_recorded' in pending['reason_codes']
    assert pending['tradable'] is False
    with svc.db.read() as conn:
        assert conn.execute('SELECT count(*) FROM strategy_signals').fetchone() == (0,)


def test_historical_replay_excludes_later_acquisition_and_is_readonly(ledger):
    """Real cache (not FakeData) must enforce acquired_at; POST cannot mutate history."""
    svc, account, _ = ledger
    day = '2026-11-20'
    before = datetime.fromisoformat(day+'T16:00:00+08:00')
    later = datetime.fromisoformat('2026-11-23T16:00:00+08:00')
    now = datetime.fromisoformat('2026-11-24T16:00:00+08:00')
    bars = flat_bars(4, .05, count=60, until=day)
    original = StrategyData(svc.db, clock=lambda: before)
    original._store('000300.SH', ETF_UNADJUSTED, bars, source='fixture:original')
    revised = flat_bars(5, .05, count=60, until=day)
    data = StrategyData(svc.db, clock=lambda: later)
    data._store('000300.SH', ETF_UNADJUSTED, revised, source='fixture:later')
    data._store('000905.SH', ETF_UNADJUSTED, revised, source='fixture:later')
    data.clock = lambda: now
    assert data.etf_series('000300.SH', completed_on=day, allow_network=True).closes[-1] == 4
    with pytest.raises(SeriesError):
        data.etf_series('000905.SH', completed_on=day, allow_network=True)
    before_state = snapshot(svc)
    result = StrategyService(svc.db, data=data, clock=lambda: now).pool(account, as_of=day, persist=True)
    assert result['read_only'] and not result['data']['allow_network']
    assert signals(svc.db) == [] and plans(svc.db) == [] and snapshot(svc) == before_state


def test_verified_signal_link_and_correction_rebuild(ledger):
    svc, account, instrument = ledger
    fund(svc, account)
    signal = StrategyStore(svc.db, clock=lambda: datetime.fromisoformat('2026-01-05T15:30:00+08:00')).save_signal(
        account_id=account,index_id='000300.SH',instrument_code='510300',signal_date='2026-01-05',
        input_version='fixture-raw-ohlcv',strategy_version='index-strategy-atr-v9.2',config_version='cfg:fixture',
        status='SIGNAL',price_basis='etf_unadjusted',price_unit='元',atr14=.05,source_verified=True)
    actual = svc.submit(trade(account,instrument,'linked-buy',day=6,lot_role='main',strategy_id='INDEX_STRATEGY_ATR',
                              frozen_atr='0.05',signal_date='2026-01-05',signal_id=signal['signal_id']))
    assert actual['strategy_evidence']['signal_id'] == signal['signal_id']
    strategy = service(svc.db, etf_fixture())
    assert strategy.sync_from_custody(account)['created'][0]['execution_source'] == 'ledger_verified'
    corrected = svc.correct(actual['id'],trade(account,instrument,'linked-correction',day=6,quantity=120,price='4.0000',
                            lot_role='main',strategy_id='INDEX_STRATEGY_ATR',frozen_atr='0.05',signal_date='2026-01-05',signal_id=signal['signal_id'],note='broker verified correction'))
    assert corrected['strategy_evidence']['signal_id'] == signal['signal_id']
    result = strategy.position_rows(account)['items'][0]
    assert result['position']['shares'] == 120 and result['position']['entry_price'] == 4
    assert result['evidence']['status'] == 'ledger_verified'
    # Different account cannot inherit the proof even when all other metadata matches.
    other = svc.create_account(AccountInput(request_id='other-link',name='other',effective_at='2000-01-01T00:00:00+08:00'))['id']
    deviation = svc.submit(trade(other,instrument,'other-link-buy',day=6,lot_role='main',strategy_id='INDEX_STRATEGY_ATR',
                                frozen_atr='0.05',signal_date='2026-01-05',signal_id=signal['signal_id']))
    assert deviation['status'] == 'effective' and deviation['strategy_evidence'] is None


def test_invalid_explicit_calendar_day_is_not_formal(ledger):
    svc, account, _ = ledger
    strategy = service(svc.db)
    for day in ('2026-11-22','2027-01-04','2026-11-24T06:00:00+08:00'):
        r = strategy.pool(account, as_of=day,persist=True)
        assert r['status'] == 'DATA_UNAVAILABLE' and r['read_only']
    assert signals(svc.db) == []


def test_actual_exit_cooldown_is_derived_not_model_or_cache(ledger):
    svc, account, instrument = ledger
    fund(svc, account)
    buy = svc.submit(main_lot_trade(account,instrument,'real-main',signal_date='2026-01-05',frozen_atr='0.05'))
    lot = next(l for l in svc.list_records('lots')['items'] if l['open_execution_id']==buy['id'])
    sale = trade(account,instrument,'real-exit',day=6,side='sell',lot_role='main',strategy_id='INDEX_STRATEGY_ATR',allocations={lot['id']:100},price='3.5000')
    sold = svc.submit(sale)
    strategy = service(svc.db,etf_fixture())
    clock = market_clock('CN')
    assert sold['status']=='effective'
    unknown = strategy._cooldown_state(account,'000300.SH','2026-01-07',clock,'RANGE')
    assert unknown['active'] and unknown['until'] is None
    corrected = svc.correct(sold['id'],sale.model_copy(update={'request_id':'exit-confirmed','strategy_exit_action':'STOP','note':'confirmed actual stop'}))
    assert corrected['status']=='effective'
    confirmed = strategy._cooldown_state(account,'000300.SH','2026-01-07',clock,'RANGE')
    assert confirmed['required_days']==10 and confirmed['active'] and confirmed['until']
    assert strategy._cooldown_state(account,'000300.SH','2026-01-07',clock,'UP')['required_days']==3
    svc.void(corrected['id'],'undo-exit','actual sell not executed')
    assert strategy._cooldown_state(account,'000300.SH','2026-01-07',clock,'RANGE')['active'] is False


def test_repeated_signal_keeps_real_trades_but_withdraws_ambiguous_evidence(ledger):
    svc, account, instrument = ledger
    fund(svc, account)
    signal = StrategyStore(svc.db, clock=lambda: datetime.fromisoformat('2026-01-05T15:30:00+08:00')).save_signal(
        account_id=account,index_id='000300.SH',instrument_code='510300',signal_date='2026-01-05',
        input_version='fixture-raw-ohlcv',strategy_version='index-strategy-atr-v9.2',config_version='cfg:fixture',
        status='SIGNAL',price_basis='etf_unadjusted',price_unit='元',atr14=.05,source_verified=True)
    def buy(request):
        return svc.submit(trade(account,instrument,request,day=6,lot_role='main',strategy_id='INDEX_STRATEGY_ATR',
                                frozen_atr='0.05',signal_date='2026-01-05',signal_id=signal['signal_id']))
    first, second = buy('dup-one'), buy('dup-two')
    assert first['status'] == second['status'] == 'effective'
    assert svc.get(first['id'])['strategy_evidence'] is None
    assert svc.get(second['id'])['strategy_evidence'] is None


def test_expired_signal_is_not_proof_but_late_execution_is_kept(ledger):
    svc, account, instrument = ledger
    fund(svc, account)
    signal = StrategyStore(svc.db, clock=lambda: datetime.fromisoformat('2026-01-05T15:30:00+08:00')).save_signal(
        account_id=account,index_id='000300.SH',instrument_code='510300',signal_date='2026-01-05',
        input_version='fixture-raw-ohlcv',strategy_version='index-strategy-atr-v9.2',config_version='cfg:fixture',
        status='SIGNAL',price_basis='etf_unadjusted',price_unit='元',atr14=.05,source_verified=True)
    actual = svc.submit(trade(account,instrument,'late-real',day=7,lot_role='main',strategy_id='INDEX_STRATEGY_ATR',
                              frozen_atr='0.05',signal_date='2026-01-05',signal_id=signal['signal_id']))
    assert actual['status'] == 'effective' and actual['strategy_evidence'] is None


def test_deviation_link_preserves_fact_but_not_compliance(ledger):
    svc, account, instrument = ledger
    fund(svc, account)
    signal = StrategyStore(svc.db, clock=lambda: datetime.fromisoformat('2026-01-05T15:30:00+08:00')).save_signal(
        account_id=account,index_id='000300.SH',instrument_code='510300',signal_date='2026-01-05',
        input_version='fixture-raw-ohlcv',strategy_version='index-strategy-atr-v9.2',config_version='cfg:fixture',
        status='SIGNAL',price_basis='etf_unadjusted',price_unit='元',atr14=.05,source_verified=True)
    data = trade(account,instrument,'deviated-linked',day=6,lot_role='main',strategy_id='INDEX_STRATEGY_ATR',
                 frozen_atr='0.05',signal_date='2026-01-05',signal_id=signal['signal_id'],strategy_deviation=True)
    actual = svc.submit(data)
    assert actual['status']=='effective' and actual['strategy_deviation']
    assert actual['signal_id']==signal['signal_id']
    assert actual['strategy_evidence'] is None
    strategy = service(svc.db, etf_fixture())
    assert not strategy.sync_from_custody(account)['created']
    result = strategy.position_rows(account)['items'][0]
    assert result['position']['shares']==100
    assert result['position']['stop_price'] is None
    assert result['position']['action']=='UNVERIFIED'
