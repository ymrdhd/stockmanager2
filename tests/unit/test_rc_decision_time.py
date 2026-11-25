from datetime import datetime

from backend.application.strategy.decision import Decision, active_decision
from backend.application.strategy.series import StrategyData, ETF_UNADJUSTED
from backend.application.strategy.service import StrategyService
from backend.application.trading.service import CashInput
from test_strategy_service import FakeData, flat_bars, fund, signals, plans
from test_trading import cfg, ledger, snapshot, trade  # noqa: F401


def at(value):
    return datetime.fromisoformat(value + '+08:00')


def test_current_preopen_uses_current_cash_and_completed_session(ledger):
    svc, account, _ = ledger
    fund(svc, account)
    svc.cash(CashInput(request_id='today-deposit', account_id=account, kind='deposit', amount_fen=10000,
                       occurred_at=at('2026-11-24T08:00:00'), note='actual deposit'))
    now = at('2026-11-24T09:00:00')
    data = FakeData(flat_bars(4000, 50, until='2026-11-23'), etf_bars=flat_bars(4, .05, until='2026-11-23'))
    result = StrategyService(svc.db, data=data, clock=lambda: now).pool(account, mode='current', persist=True)
    assert result['market_session'] == '2026-11-23'
    assert result['ledger_cutoff'] == now.isoformat()
    assert result['cash_fen'] == 100010000
    assert not result['read_only'] and result['data']['allow_network']


def test_weekend_current_and_explicit_history_same_session(ledger):
    svc, account, _ = ledger
    fund(svc, account)
    now = at('2026-11-22T12:00:00')
    data = FakeData(flat_bars(4000, 50, until='2026-11-20'), etf_bars=flat_bars(4, .05, until='2026-11-20'))
    strategy = StrategyService(svc.db, data=data, clock=lambda: now)
    before = snapshot(svc)
    current = strategy.pool(account, mode='current')
    historical = strategy.pool(account, mode='historical', as_of='2026-11-20', persist=True)
    assert current['market_session'] == historical['market_session'] == '2026-11-20'
    assert current['ledger_cutoff'] == now.isoformat()
    assert historical['ledger_cutoff'].startswith('2026-11-20T23:59:59')
    assert historical['historical_replay_complete'] is False and historical['read_only']
    assert not historical['data']['allow_network'] and not any(r['tradable'] for r in historical['items'])
    assert signals(svc.db) == plans(svc.db) == [] and snapshot(svc) == before


def test_cache_acquired_after_session_visible_current_not_history(ledger):
    svc, _, _ = ledger
    now = at('2026-11-22T12:00:00')
    data = StrategyData(svc.db, clock=lambda: now)
    data._store('000300.SH', ETF_UNADJUSTED, flat_bars(4, .05, until='2026-11-20'), source='fixture')
    for mode, expected in [('current', True), ('historical', False)]:
        token = active_decision.set(Decision(mode, now, '2026-11-20'))
        try:
            target, cutoff, historical = data.decision_window('2026-11-20', 'CN')
            bars, _ = data.cached('000300.SH', ETF_UNADJUSTED, as_of=target, acquired_before=cutoff)
            assert bool(bars) is expected and historical is (mode == 'historical')
        finally:
            active_decision.reset(token)


def test_simultaneous_candidates_are_not_all_committed_over_capacity(ledger):
    svc, account, _ = ledger
    fund(svc, account)
    now = at('2026-11-24T16:00:00')
    data = FakeData(flat_bars(4000, 50), etf_bars=flat_bars(4, .05))
    strategy = StrategyService(svc.db, data=data, clock=lambda: now)
    result = strategy.pool(account, mode='current', persist=True)
    assert result['capacity']['competition']
    assert result['capacity']['candidate_core_slots'] > 8 or result['capacity']['candidate_cash_fen'] > result['cash_fen']
    assert any(row['status'] == 'PORTFOLIO_CAPACITY_BLOCKED' for row in result['items'])
    assert not any(row['tradable'] for row in result['items'])
    assert signals(svc.db) == plans(svc.db) == []
