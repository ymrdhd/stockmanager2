"""Golden regressions for the migrated V9/G2 rules.

Every expectation in this file was produced by running the **original** functions in
``artifacts/tmp/reference/subroutine/index_strategy`` (see the round report), never by
running the new implementation and copying what it printed. The two pure-rule copies
are byte-identical, so these tests protect against accidental drift and against a later
"fix" that silently changes a rule.
"""
from __future__ import annotations

import pytest

from backend.domain.strategy import config as strategy_config
from backend.domain.strategy import entry, exit as strategy_exit, factor_combo, factors
from backend.domain.strategy import indicators, regime, v9_rules
from backend.domain.strategy.market_data import Bar, IndexSeries

# index_id, name, market, track, N, entry_window, C, A, B%, exit_after_bars,
# e2a_enabled, factor_key, etf_code, price_unit, price_digits, clock_market,
# counts_against_core_slots, pyramid_enabled, renewal_enabled
CONFIGS = [
    ('000016.SH', '上证50', 'CN', 'CN', 40, 30, 0.5, 0.5, 8.0, 21, True, None, '510050', '元', 3, 'CN', True, True, True),
    ('000300.SH', '沪深300', 'CN', 'CN', 5, 3, 0.25, 1.0, 5.0, 21, True, None, '510300', '元', 3, 'CN', True, True, True),
    ('000905.SH', '中证500', 'CN', 'CN', 20, 15, 0.5, 0.5, 12.0, 21, True, None, '510500', '元', 3, 'CN', True, True, True),
    ('000852.SH', '中证1000', 'CN', 'CN', 15, 11, 0.5, 0.5, 12.0, 21, True, None, '512100', '元', 3, 'CN', True, True, True),
    ('399001.SZ', '深证成指', 'CN', 'CN', 19, 14, 0.5, 7.0, 3.0, 21, True, None, '159943', '元', 3, 'CN', True, True, True),
    ('399006.SZ', '创业板指', 'CN', 'CN', 32, 24, 1.75, 4.0, 5.0, 21, True, None, '159915', '元', 3, 'CN', True, True, True),
    ('000688.SH', '科创50', 'CN', 'CN', 4, 3, 0.5, 6.0, 4.0, 21, True, None, '588000', '元', 3, 'CN', True, True, True),
    ('932000.CSI', '中证2000', 'CN', 'CN', 21, 15, 0.75, 0.5, 10.0, 21, True, None, '159531', '元', 3, 'CN', True, True, True),
    ('NDX', '纳斯达克100', 'US', 'US', 18, 18, 5.0, 4.0, 3.0, 21, False, None, '159941', '元', 3, 'CN', False, False, False),
    ('HSTECH', '恒生科技', 'HK', 'CN', 20, 15, 4.0, 0.5, 5.0, 21, True, None, '513180', '元', 3, 'CN', True, True, True),
    ('931152.CSI', '中证创新药', 'CN', 'CN', 8, 6, 2.0, 3.0, 3.0, 21, True, None, '159992', '元', 3, 'CN', True, True, True),
    ('H30269.CSI', '中证红利低波', 'CN', 'CN', 5, 3, 2.0, 0.5, 5.0, 21, True, None, '563020', '元', 3, 'CN', True, True, True),
    ('H30007.CSI', '中证芯片产业', 'CN', 'CN', 5, 3, 0.75, 5.0, 8.0, 21, True, None, '159599', '元', 3, 'CN', True, True, True),
    ('399997.SZ', '中证白酒', 'CN', 'CN', 10, 7, 1.0, 5.0, 3.0, 21, True, None, '161725', '元', 3, 'CN', True, True, True),
]


def _bars(closes):
    return tuple(Bar(date=f'2026-{1 + (i // 28):02d}-{1 + (i % 28):02d}', open=c, high=c * 1.01,
                     low=c * 0.99, close=c) for i, c in enumerate(closes))


def _series(index_id, closes, *, proxy=False, verified=True, source='golden'):
    return IndexSeries(index_id=index_id, name=index_id, market='CN', bars=_bars(closes),
                       source=source, is_proxy=proxy, source_verified=verified)


def test_every_index_keeps_its_own_parameters():
    assert [section[0] for section in CONFIGS] == list(strategy_config._BY_ID)
    assert len(CONFIGS) == 14
    for (index_id, name, market, track, window, entry_window, stop, trail, drawdown,
         bars, e2a, factor, etf, unit, digits, clock, slots, pyramid, renewal) in CONFIGS:
        cfg = strategy_config.get_config(index_id)
        assert (cfg.index_id, cfg.name, cfg.market, cfg.track) == (index_id, name, market, track)
        assert (cfg.breakout_window, cfg.entry_window, cfg.exit_after_bars) == (window, entry_window, bars)
        assert (cfg.initial_stop_atr, cfg.trail_trigger_atr, cfg.trail_drawdown_pct) == (stop, trail, drawdown)
        assert (cfg.e2a_enabled, cfg.factor_key, cfg.etf_code) == (e2a, factor, etf)
        assert (cfg.price_unit, cfg.price_digits, cfg.clock_market) == (unit, digits, clock)
        assert (cfg.counts_against_core_slots, cfg.pyramid_enabled, cfg.renewal_enabled) == (slots, pyramid, renewal)
    # NDX is the only index on the independent US track and never renews.
    ndx = strategy_config.get_config('NDX')
    assert ndx.is_us_track and not ndx.renewal_enabled and not ndx.e2a_enabled
    assert not ndx.counts_against_core_slots


def test_t3_window_is_three_quarters_of_the_original_n():
    assert {n: v9_rules.t3_entry_window(n) for n in (4, 5, 8, 10, 15, 18, 19, 20, 21, 32, 40)} == {
        4: 3, 5: 3, 8: 6, 10: 7, 15: 11, 18: 13, 19: 14, 20: 15, 21: 15, 32: 24, 40: 30}
    # NDX keeps the original N: its entry window equals its breakout window.
    assert strategy_config.get_config('NDX').entry_window == 18


def test_atr14_is_the_simple_mean_of_fourteen_true_ranges():
    rows = [(10.0, 10.5, 9.5, 10.2), (10.2, 10.7, 10.0, 10.4), (10.4, 10.9, 10.2, 10.8),
            (10.8, 11.0, 10.5, 10.6), (10.6, 11.2, 10.4, 11.1), (11.1, 11.5, 10.9, 11.3),
            (11.3, 11.8, 11.1, 11.6), (11.6, 11.9, 11.2, 11.4), (11.4, 11.7, 11.0, 11.2),
            (11.2, 11.6, 10.8, 11.5), (11.5, 12.0, 11.3, 11.9), (11.9, 12.3, 11.7, 12.1),
            (12.1, 12.4, 11.9, 12.2), (12.2, 12.6, 12.0, 12.5), (12.5, 12.8, 12.3, 12.7)]
    highs = [row[1] for row in rows]
    lows = [row[2] for row in rows]
    closes = [row[3] for row in rows]
    assert indicators.compute_atr14(highs, lows, closes) == 0.65
    assert indicators.compute_atr14(highs[:14], lows[:14], closes[:14]) is None


def test_breakout_excludes_the_signal_bar_itself():
    highs = [100.0 + i for i in range(20)] + [125.0]
    closes = [100.0 + i for i in range(20)] + [125.0]
    assert indicators.breakout_threshold(highs, 5) == 119.0
    assert indicators.breakout_days(highs, closes, 5) == 16


def _regime(closes):
    many = {chr(ord('A') + i): _series(f'S{i}', closes) for i in range(12)}
    return regime.market_regime(many)


@pytest.mark.parametrize('label,closes,state,er60', [
    ('up', [100.0 * (1.01 ** i) for i in range(70)], 'UP', 1.0),
    ('down', [100.0 * (0.99 ** i) for i in range(70)], 'DOWN', 1.0),
    ('flat', [100.0 for _ in range(70)], 'RANGE', 0.0),
    ('zigzag', [100.0 + (2.0 if i % 2 else 0.0) for i in range(70)], 'RANGE', 0.0),
])
def test_er60_and_market_regime(label, closes, state, er60):
    snapshot = _regime(closes)
    assert (snapshot.state, snapshot.er60, snapshot.components) == (state, er60, 12)
    assert snapshot.warning == ''


def test_regime_needs_enough_components_and_enough_samples():
    few = {chr(ord('A') + i): _series(f'S{i}', [100.0 * (1.01 ** i) for i in range(70)])
           for i in range(6)}
    snapshot = regime.market_regime(few)
    assert snapshot.state == 'UNKNOWN' and snapshot.components == 6
    assert '不足 10 只' in snapshot.warning
    short = regime.market_regime({'A': _series('A', [100.0 + i for i in range(40)])})
    assert short.state == 'UNKNOWN' and short.er60 is None and '不足 61 个' in short.warning
    # A zero-length path is 0.0, not missing data.
    assert regime.efficiency_ratio([100.0 for _ in range(70)]) == 0.0


def test_entry_signal_freezes_breakout_atr_and_buy_max():
    closes = [100.0 for _ in range(30)] + [108.0]
    series = _series('000300.SH', closes)
    cfg = strategy_config.get_config('000300.SH')
    factor = factors.evaluate_factor(cfg, series, {})
    combo = factor_combo.evaluate_combo(cfg, series)
    plan = entry.evaluate_entry(cfg, series, as_of='2026-03-01', factor=factor, combo=combo,
                               peer_series={}, cooldown_active=False, cooldown_note='',
                               has_open_position=False, has_pending_plan=False,
                               open_position_count=0, max_positions=8, tick=0.001)
    assert plan.status == 'SIGNAL'
    assert (plan.signal_close, plan.threshold_high) == (108.0, 101.0)
    assert plan.atr14 == 2.5057
    assert plan.buy_max == 109.25285
    assert plan.initial_stop_reference == 107.374
    assert plan.trail_trigger_reference == 110.5057
    assert (plan.entry_window, plan.breakout_window) == (3, 5)


def test_next_open_above_buy_max_cancels_and_never_recovers():
    closes = [100.0 for _ in range(30)] + [108.0]
    series = _series('000300.SH', closes)
    cfg = strategy_config.get_config('000300.SH')
    plan = entry.evaluate_entry(cfg, series, as_of='2026-03-01',
                               factor=factors.evaluate_factor(cfg, series, {}),
                               combo=factor_combo.evaluate_combo(cfg, series), peer_series={},
                               cooldown_active=False, cooldown_note='', has_open_position=False,
                               has_pending_plan=False, open_position_count=0, max_positions=8,
                               tick=0.001)
    filled = entry.evaluate_fill(plan, Bar(date='2026-02-02', open=109.0, high=110.0, low=108.0, close=109.5))
    assert (filled.status, filled.entry_price) == ('FILLED', 109.0)
    cancelled = entry.evaluate_fill(plan, Bar(date='2026-02-02', open=130.0, high=131.0, low=129.0, close=130.0))
    assert cancelled.status == 'CANCELLED' and cancelled.entry_price is None
    pending = entry.evaluate_fill(plan, None)
    assert pending.status == 'PENDING'
    assert '不虚构成交' in pending.reason


def test_entry_refuses_proxy_data_and_respects_gates():
    closes = [100.0 for _ in range(30)] + [108.0]
    cfg = strategy_config.get_config('000300.SH')
    proxy = _series('000300.SH', closes, proxy=True)
    plan = entry.evaluate_entry(cfg, proxy, as_of='2026-03-01',
                               factor=factors.evaluate_factor(cfg, proxy, {}), combo=None,
                               peer_series={}, cooldown_active=False, cooldown_note='',
                               has_open_position=False, has_pending_plan=False,
                               open_position_count=0, max_positions=8, tick=0.001)
    assert plan.status == 'DATA_UNAVAILABLE'
    series = _series('000300.SH', closes)
    factor = factors.evaluate_factor(cfg, series, {})
    combo = factor_combo.evaluate_combo(cfg, series)
    common = dict(as_of='2026-03-01', factor=factor, combo=combo, peer_series={},
                  has_open_position=False, has_pending_plan=False, tick=0.001)
    assert entry.evaluate_entry(cfg, series, cooldown_active=True, cooldown_note='冷却中',
                               open_position_count=0, max_positions=8, **common).status == 'COOLDOWN'
    assert entry.evaluate_entry(cfg, series, cooldown_active=False, cooldown_note='',
                               open_position_count=8, max_positions=8, **common).status == 'PORTFOLIO_FULL'


def test_stop_and_trail_prices_match_the_original_rounding():
    cfg = strategy_config.get_config('000300.SH')
    assert strategy_exit.initial_stop_price(cfg, 4.0, 0.1, tick=0.001) == 3.975
    assert strategy_exit.trail_trigger_price(cfg, 4.0, 0.1) == 4.1
    assert strategy_exit.trail_drawdown_price(cfg, 4.5) == 4.275
    ndx = strategy_config.get_config('NDX')
    assert strategy_exit.initial_stop_price(ndx, 2.0, 0.05, tick=0.001) == 1.75
    assert strategy_exit.trail_trigger_price(ndx, 2.0, 0.05) == 2.2
    assert strategy_exit.trail_drawdown_price(ndx, 2.2) == 2.134


@pytest.mark.parametrize('state,action,days', [
    ('RANGE', 'STOP', 10), ('RANGE', 'GAP_STOP', 10), ('RANGE', 'EXIT', 3),
    ('RANGE', 'CLOSED', 3), ('UP', 'STOP', 3), ('UP', 'GAP_STOP', 3),
    ('DOWN', 'STOP', 3), ('UNKNOWN', 'STOP', 3),
])
def test_cooldown_days_depend_on_regime_and_exit_action(state, action, days):
    assert strategy_exit.cooldown_required_days(regime_state=state, exit_action=action) == days


def test_cooldown_until_counts_complete_trading_days():
    days = [f'2026-01-{d:02d}' for d in range(5, 31)]
    assert strategy_exit.cooldown_until_date('2026-01-05', days, required_days=3) == '2026-01-08'
    assert strategy_exit.cooldown_until_date('2026-01-05', days, required_days=10) == '2026-01-15'


def _position(**overrides):
    payload = dict(index_id='000300.SH', strategy_version='v', signal_date='2026-01-05',
                   entry_date='2026-01-06', entry_price=4.0, signal_atr=0.1, initial_stop=3.975,
                   stop_price=3.975, trail_trigger=4.1, stage=1, highest_price=4.0,
                   current_atr=0.1, holding_days=1, exit_after_bars=21, entry_executed=True)
    payload.update(overrides)
    return strategy_exit.PositionState(**payload)


def _advance(state, rows, cfg=None, **kwargs):
    cfg = cfg or strategy_config.get_config('000300.SH')
    decision = None
    for row in rows:
        decision = strategy_exit.advance_position(state, row, cfg, trade_date=row.date,
                                                  current_atr=0.1, **kwargs)
        if state.exit_filled:
            break
    return decision


def test_intraday_stop_and_gap_stop_use_different_prices():
    state = _position()
    rows = [Bar(date=f'2026-01-{d:02d}', open=4.0, high=4.02, low=3.95, close=3.98)
            for d in range(7, 10)]
    decision = _advance(state, rows, tick=0.001)
    assert (decision.action, decision.price, decision.stop_after) == ('STOP', 3.975, 3.975)
    gap_state = _position()
    gap = _advance(gap_state, [Bar(date='2026-01-07', open=3.9, high=3.95, low=3.8, close=3.85)],
                   tick=0.001)
    assert (gap.action, gap.price) == ('GAP_STOP', 3.9)


def test_trailing_stop_raises_only_from_the_previous_bar():
    state = _position()
    rows = [Bar(date=f'2026-01-{d:02d}', open=4.0 + d * 0.01, high=4.2 + d * 0.01,
                low=4.0 + d * 0.01, close=4.1 + d * 0.01) for d in range(7, 12)]
    decision = _advance(state, rows, tick=0.001)
    assert decision.action == 'HOLD'
    assert (decision.stage_after, decision.stop_after) == (3, 4.085)
    assert state.highest_price == pytest.approx(4.31)
    # The new protection level only takes effect the next trading day.
    assert state.pending_stop == pytest.approx(4.0945)
    assert state.pending_stop_effective_date == '2026-01-11'


def test_time_exit_on_the_a_share_track():
    state = _position()
    rows = [Bar(date=f'2026-{2 + (i // 28):02d}-{1 + (i % 28):02d}', open=4.0, high=4.05,
                low=3.98, close=4.02) for i in range(25)]
    decision = _advance(state, rows, tick=0.001)
    assert (decision.action, decision.price, state.holding_days) == ('EXIT', 4.02, 21)
    assert '时间退出' in decision.reason


def test_ndx_exits_on_its_own_track_without_renewal():
    cfg = strategy_config.get_config('NDX')
    state = strategy_exit.PositionState(index_id='NDX', strategy_version='v',
                                        signal_date='2026-01-05', entry_date='2026-01-06',
                                        entry_price=2.0, signal_atr=0.05, initial_stop=1.75,
                                        stop_price=1.75, trail_trigger=2.2, stage=1,
                                        highest_price=2.0, current_atr=0.05, holding_days=1,
                                        exit_after_bars=21, entry_executed=True)
    rows = [Bar(date=f'2026-{2 + (i // 28):02d}-{1 + (i % 28):02d}', open=4.0, high=4.05,
                low=3.98, close=4.02) for i in range(25)]
    decision = _advance(state, rows, cfg=cfg, tick=0.001)
    assert decision.action == 'EXIT' and state.holding_days == 21
    assert cfg.renewal_enabled is False and cfg.e2a_enabled is False


def test_range_regime_blocks_the_profit_add():
    cfg = strategy_config.get_config('000300.SH')
    closes = [100.0 for _ in range(30)] + [108.0]
    position = _position(initial_stop=3.8, stop_price=3.8, trail_trigger=4.5, highest_price=4.2,
                         holding_days=5)
    snap = v9_rules.ma_snapshot(closes, close=4.3)
    prior = [3.9, 4.0, 4.05, 4.1, 4.15, 4.2, 4.25, 4.3, 4.35, 3.95, 4.0]
    blocked = v9_rules.evaluate_pyramid(cfg, position, close=4.3, snap=snap, prior_closes=prior,
                                        holding_days=5, regime_state='RANGE', regime_label='RANGE')
    assert blocked.status == 'REJECT' and blocked.allowed is False
    assert blocked.reason.startswith('G2-2')
    assert [check[0] for check in blocked.checks] == ['G2震荡市禁加仓']
    allowed = v9_rules.evaluate_pyramid(cfg, position, close=4.3, snap=snap, prior_closes=prior,
                                        holding_days=5, regime_state='UP', regime_label='UP')
    assert [check[0] for check in allowed.checks] == [
        'G2市场状态', '持仓天数', '浮盈≥1ATR', '强趋势', '10日收盘新高', 'K6门禁']
    assert allowed.status == 'REJECT' and allowed.allowed is False
    short = v9_rules.evaluate_pyramid(cfg, position, close=4.3, snap=snap, prior_closes=prior,
                                      holding_days=3, regime_state='UP', regime_label='UP')
    assert short.status == 'REJECT'
    done_position = _position(initial_stop=3.8, stop_price=3.8, trail_trigger=4.5,
                              highest_price=4.2, holding_days=9, add_executed=True)
    done = v9_rules.evaluate_pyramid(cfg, done_position, close=4.3, snap=snap, prior_closes=prior,
                                     holding_days=9, regime_state='UP', regime_label='UP')
    assert done.status == 'DONE'


@pytest.mark.parametrize('ratio,days,status,target', [
    (0.0, None, 'ALLOW', 0.15),
    (0.15, 5, 'HOLD', None),
    (0.16, 12, 'ALLOW', 0.215),
    (0.22, 20, 'HOLD', 0.215),
    (0.26, 30, 'TRIM', 0.245),
])
def test_ndx_auxiliary_position_ratios(ratio, days, status, target):
    closes = [100.0 + i * 0.1 for i in range(210)]
    plan = v9_rules.evaluate_ndx_aux(closes, equity=1000000.0, current_ratio=ratio,
                                     days_since_add=days, last_date='2026-11-24')
    assert plan.status == status
    assert plan.target_ratio == target
    assert v9_rules.evaluate_ndx_aux(closes[:150]).status == 'DATA_UNAVAILABLE'


def test_portfolio_capacity_and_base_amount():
    assert v9_rules.base_position_amount(0) is None
    assert v9_rules.base_position_amount(-1) is None
    assert v9_rules.base_position_amount(100.0) == 12.5
    assert v9_rules.base_position_amount(500000.0) == 62500.0
    sizing = v9_rules.portfolio_sizing(equity=500000.0, core_count=8)
    assert sizing.full and sizing.free_slots == 0
    assert strategy_config.PORTFOLIO_MAX_POSITIONS == 8
    assert strategy_config.BASE_POSITION_DIVISOR == 8
    assert strategy_config.PYRAMID_TARGET_AMOUNT == 62500.0
    assert strategy_config.TIME_EXIT_BARS_AFTER_ENTRY == 20
    assert strategy_config.ATR_WINDOW == 14
    assert strategy_config.BUY_MAX_ATR_MULTIPLIER == 0.5
    assert strategy_config.T3_WINDOW_FACTOR == 0.75
    assert strategy_config.COOLDOWN_TRADING_DAYS == 3
    assert strategy_config.G2_RANGE_COOLDOWN_TRADING_DAYS == 10
    assert strategy_config.E2A_MAX_EXTEND_DAYS == 10
    assert strategy_config.K6_GATE_MIN_MA_RATIO == 1.02
    assert strategy_config.G2_REGIME_MIN_COMPONENTS == 10
    assert strategy_config.G2_ER60_THRESHOLD == 0.25


def test_etf_link_table_covers_all_fourteen_indices():
    from backend.domain.strategy.symbols import (ETF_INDEX_MAP, etf_code_of, index_for_etf,
                                                 normalize_index_id)
    expected = {config[0]: config[12] for config in CONFIGS}
    assert {index_id: etf_code_of(index_id) for index_id in expected} == expected
    for index_id, etf in expected.items():
        assert ETF_INDEX_MAP[etf] == index_id
        assert index_for_etf(etf) == index_id
    assert normalize_index_id('沪深300') == '000300.SH'
    with pytest.raises(Exception):
        normalize_index_id('000001.SZ')
