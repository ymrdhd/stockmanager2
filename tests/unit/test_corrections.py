"""ISS-016 opening corrections and ISS-017 corporate actions; temporary databases only."""
from contextlib import closing
from datetime import date, datetime
import sqlite3

import pytest
from pydantic import ValidationError

from backend.application.portfolio import (CompanyInput, OpeningCorrectionInput, OpeningInput,
                                           PortfolioService)
from backend.application.trading.service import Conflict, TradeError
from test_trading import cfg, ledger, trade, data, snapshot  # noqa: F401
from test_pnl import at, cash, quote


def lots_of(svc):
    return {row['id']: row for row in data(svc, 'lots')}


def allocated_of(svc):
    totals = {}
    for row in data(svc, 'allocations'):
        totals[row['lot_id']] = totals.get(row['lot_id'], 0) + row['quantity']
    return totals


def conserved(svc):
    """Σ opened − Σ allocated == remaining, for every batch that is still counted."""
    allocated = allocated_of(svc)
    return all(row['opened_qty'] - allocated.get(row['id'], 0) == row['remaining_qty']
               for row in data(svc, 'lots') if row['status'] != 'reversed')


def all_rows(path):
    with closing(sqlite3.connect(path)) as c:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: c.execute('SELECT * FROM "%s" ORDER BY rowid' % n).fetchall() for n in names}


def test_golden_split_keeps_total_cost_and_unrealized(ledger):
    """1 拆 2：数量翻倍、总成本不变、单位成本 5 元、没有现金流水、没有虚假亏损。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 1000000, 5)
    opened = p.opening(OpeningInput(request_id='base', account_id=a, instrument_id=i, quantity=100,
                                    cost_fen=100000, fees_fen=0, occurred_at=at(5, 9),
                                    note='verified opening holdings'))
    quote(p, i, 5, 10)
    before = p.valuation(a, at(5))
    assert before['positions'][0]['quantity'] == 100
    assert before['positions'][0]['holding_cost_fen'] == 100000
    assert before['unrealized_fen'] == 0 and before['market_value_fen'] == 100000
    movements = len(data(svc, 'cash'))

    result = p.company(CompanyInput(request_id='split', account_id=a, instrument_id=i, kind='split',
                                    ratio_from=1, ratio_to=2, occurred_at=at(6, 12),
                                    note='1 for 2 split, broker statement 2026-01-06'))
    assert result['ratio_from'] == 1 and result['ratio_to'] == 2
    assert result['strategy_evidence_invalid'] is False

    lots = lots_of(svc)
    batch = lots[opened['lot_id']]
    assert (batch['opened_qty'], batch['remaining_qty']) == (200, 200)
    assert batch['remaining_cost_fen'] == 100000 and batch['remaining_fees_fen'] == 0
    assert len(data(svc, 'trades')) == 0
    assert len(data(svc, 'cash')) == movements
    assert [x['kind'] for x in data(svc, 'cash')] == ['opening']

    quote(p, i, 6, 5)
    after = p.valuation(a, at(6), date(2026, 1, 5))
    position = after['positions'][0]
    assert position['quantity'] == 200
    assert position['holding_cost_fen'] == 100000
    assert position['market_value_fen'] == 100000
    assert position['unrealized_fen'] == 0 and after['unrealized_fen'] == 0
    assert after['equity_fen'] == before['equity_fen'] == 1100000
    assert after['daily_pnl_fen']==after['equity_pnl_fen']==0
    historical=p.valuation(a,at(5))
    assert historical['positions'][0]['quantity']==100 and historical['market_value_fen']==100000
    sold=svc.submit(trade(a,i,'post-split-sale',day=7,side='sell',quantity=50,price='5',fees_fen=0))
    assert sold['status']=='effective'
    assert lots_of(svc)[opened['lot_id']]['remaining_qty']==150
    assert lots_of(svc)[opened['lot_id']]['remaining_cost_fen']==75000
    assert conserved(svc)
    # 单位成本由数量推导，不单独存：200 股 / 100000 分 = 500 分 = ¥5.00
    assert position['holding_cost_fen'] // position['quantity'] == 500
    print('split', position['quantity'], position['holding_cost_fen'],
          position['holding_cost_fen'] // position['quantity'], len(data(svc, 'cash')))


def test_merge_two_into_one_conserves_quantity_and_cost(ledger):
    """2 并 1：数量减半、总成本与市值口径都不因合并被改写。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 2000000, 5)
    opened = p.opening(OpeningInput(request_id='base', account_id=a, instrument_id=i, quantity=200,
                                    cost_fen=100000, fees_fen=10, occurred_at=at(5, 9),
                                    note='verified opening holdings'))
    quote(p, i, 5, 10)
    before = p.valuation(a, at(5))
    assert before['positions'][0]['quantity'] == 200 and before['positions'][0]['market_value_fen'] == 200000
    movements = len(data(svc, 'cash'))

    result = p.company(CompanyInput(request_id='merge', account_id=a, instrument_id=i, kind='merge',
                                    ratio_from=2, ratio_to=1, occurred_at=at(6, 12),
                                    note='2 for 1 consolidation'))
    assert result['cash_tail_fen'] is None
    batch = lots_of(svc)[opened['lot_id']]
    assert (batch['opened_qty'], batch['remaining_qty']) == (100, 100)
    assert batch['remaining_cost_fen'] == 100000 and batch['remaining_fees_fen'] == 10
    assert len(data(svc, 'cash')) == movements and len(data(svc, 'trades')) == 0

    quote(p, i, 6, 20)
    after = p.valuation(a, at(6), date(2026, 1, 5))
    position = after['positions'][0]
    assert position['quantity'] == 100 and position['holding_cost_fen'] == 100010
    assert position['market_value_fen'] == 200000
    assert position['unrealized_fen'] == 100000 - 10
    assert after['equity_fen'] == before['equity_fen']
    print('merge', position['quantity'], position['holding_cost_fen'], position['market_value_fen'])


def test_split_fractional_tail_is_recorded_as_cash_not_cost(ledger):
    """除不尽的碎股只按调用方申报的真实现金尾差入账，绝不并进成本。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 3000000, 5)
    clean = p.opening(OpeningInput(request_id='base', account_id=a, instrument_id=i, quantity=103,
                                   cost_fen=100000, fees_fen=0, occurred_at=at(5, 9),
                                   note='verified opening holdings'))
    split = p.company(CompanyInput(request_id='clean', account_id=a, instrument_id=i, kind='split',
                                   ratio_from=1, ratio_to=2, occurred_at=at(6, 12),
                                   note='1 for 2 split of 103 shares'))
    assert lots_of(svc)[clean['lot_id']]['opened_qty'] == 206
    assert split['cash_tail_fen'] is None
    assert len(data(svc, 'cash')) == 1

    # 3 并 2 且股数不是 2 的倍数：206 × 2 = 412，412 // 3 = 137 余 1，必须有真实现金尾差。
    with pytest.raises(Conflict, match='cash tail'):
        p.company(CompanyInput(request_id='merge-missing', account_id=a, instrument_id=i, kind='merge',
                               ratio_from=3, ratio_to=2, occurred_at=at(7, 12),
                               note='merge without the actual cash tail'))
    merge = p.company(CompanyInput(request_id='merge', account_id=a, instrument_id=i, kind='merge',
                                   ratio_from=3, ratio_to=2, cash_tail_fen=1234, occurred_at=at(7, 12),
                                   note='3 for 2 reverse split, cash in lieu 12.34'))
    batch = lots_of(svc)[clean['lot_id']]
    assert (batch['opened_qty'], batch['remaining_qty']) == (137, 137)
    assert batch['remaining_cost_fen'] == 100000 and batch['remaining_fees_fen'] == 0
    assert merge['cash_tail_fen'] == 1234
    tails = [x for x in data(svc, 'cash') if x['kind'] == 'adjustment']
    assert [(x['amount_fen'], x['note']) for x in tails] == [(1234, '公司行为现金尾差')]
    assert len(data(svc, 'trades')) == 0
    print('tail', batch['opened_qty'], batch['remaining_cost_fen'], tails[0]['amount_fen'])


def test_opening_correction_replays_sales_without_duplicating_cash(ledger):
    """期初数量更正后重算 FIFO 与成本：现金不重复、数量守恒、版本与最早受影响日正确。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 5000000, 5)
    opened = p.opening(OpeningInput(request_id='base', account_id=a, instrument_id=i, quantity=100,
                                    cost_fen=100000, fees_fen=0, occurred_at=at(5, 9),
                                    note='verified opening holdings'))
    svc.submit(trade(a, i, 'buy', day=6, quantity=100, price='10', fees_fen=0))
    svc.submit(trade(a, i, 'sell', day=7, side='sell', quantity=150, price='11', fees_fen=0))
    before = p.valuation(a, at(7), date(2026, 1, 6))
    cash_before = len(data(svc, 'cash'))
    revision = before['ledger_revision']
    assert before['positions'][0]['quantity'] == 50

    result = p.correct_opening(OpeningCorrectionInput(
        request_id='fix-base', opening_id=opened['id'], quantity=120, cost_fen=100000, fees_fen=0,
        occurred_at=at(4, 9), note='broker statement shows 120 shares at the baseline'))
    assert result['supersedes'] == opened['id'] and result['strategy_evidence_recheck'] is True
    assert set(result['replayed_executions']) == {x['id'] for x in data(svc, 'trades')}

    after = p.valuation(a, at(7), date(2026, 1, 6))
    assert after['ledger_revision'] == revision + 1
    assert after['earliest_affected_day'] == '2026-01-04'
    assert after['positions'][0]['quantity'] == 70
    assert len(data(svc, 'cash')) == cash_before
    assert conserved(svc)
    batch = lots_of(svc)[opened['lot_id']]
    assert batch['status'] == 'reversed'
    current = [x for x in data(svc, 'lots') if x['opening_id'] == result['id']][0]
    assert (current['opened_qty'], current['remaining_qty']) == (120, 0)
    replayed = {x['lot_id']: x['quantity'] for x in data(svc, 'allocations')}
    assert replayed[current['id']] == 120
    print('correction', after['positions'][0]['quantity'], after['earliest_affected_day'], len(data(svc, 'cash')))


def test_opening_correction_conflict_leaves_no_partial_write(cfg, ledger):
    """后续卖出点名了将被替换的批次时整体拒绝，数据库不留任何部分修改。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 5000000, 5)
    opened = p.opening(OpeningInput(request_id='base', account_id=a, instrument_id=i, quantity=100,
                                    cost_fen=100000, fees_fen=0, occurred_at=at(5, 9),
                                    note='verified opening holdings'))
    svc.submit(trade(a, i, 'buy', day=6, quantity=100, price='10', fees_fen=0))
    svc.submit(trade(a, i, 'specified', day=7, side='sell', quantity=50, price='11', fees_fen=0,
                     allocations={opened['lot_id']: 50}))
    before = all_rows(cfg.path)

    with pytest.raises(Conflict):
        p.correct_opening(OpeningCorrectionInput(
            request_id='unsafe', opening_id=opened['id'], quantity=40, cost_fen=100000, fees_fen=0,
            occurred_at=at(5, 9), note='would strand a specified sale'))
    assert all_rows(cfg.path) == before
    assert snapshot(svc)['allocations']['items']
    print('conflict rows', sum(len(v) for v in before.values()))


def test_opening_void_keeps_history_and_never_drops_the_row(ledger):
    """作废期初：原行保留、批次反向、持仓清零，期初持仓本身不带现金。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 2000000, 5)
    opened = p.opening(OpeningInput(request_id='base', account_id=a, instrument_id=i, quantity=100,
                                    cost_fen=100000, fees_fen=5, occurred_at=at(5, 9),
                                    note='verified opening holdings'))
    quote(p, i, 5, 10)
    rows_before = len(p.openings()['items'])
    cash_before = len(data(svc, 'cash'))

    result = p.void_opening(opened['id'], 'void-base', 'baseline entered on the wrong account')
    assert result['voided_at'] is not None and result['cash_reversed_fen'] == 0
    listing = p.openings()
    assert listing['total'] == rows_before == len(listing['items'])
    row = [x for x in listing['items'] if x['id'] == opened['id']][0]
    assert row['voided_at'] is not None and row['is_current'] == 0
    assert row['quantity'] == 100 and row['cost_fen'] == 100000
    assert lots_of(svc)[opened['lot_id']]['status'] == 'reversed'
    assert p.valuation(a, at(5))['positions'] == []
    assert len(data(svc, 'cash')) == cash_before
    assert len(data(svc, 'trades')) == 0
    print('void', row['is_current'], row['quantity'], len(listing['items']))


def test_split_invalidates_strategy_scale_without_rewriting_frozen_atr(ledger):
    """策略批次被拆分改变价格尺度后，证据必须显式失效且原冻结 ATR 原样保留。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 3000000, 5)
    svc.submit(trade(a, i, 'main-buy', day=6, quantity=10, price='3.1000', fees_fen=5,
                     strategy_id='INDEX_STRATEGY_ATR', lot_role='main',
                     signal_date='2025-12-31', frozen_atr='0.1234'))
    result = p.company(CompanyInput(request_id='split', account_id=a, instrument_id=i, kind='split',
                                    ratio_from=1, ratio_to=2, occurred_at=at(7, 12),
                                    note='1 for 2 split after the strategy entry'))
    assert result['strategy_evidence_invalid'] is True
    assert lots_of(svc)[data(svc, 'lots')[0]['id']]['opened_qty'] == 20
    assert data(svc, 'lots')[0]['frozen_atr'] == 1234

    evidence = p.strategy_evidence(a, at(8))
    assert evidence['status'] == 'unavailable'
    item = evidence['items'][0]
    assert item['evidence_status'] == 'unverified'
    assert 'company_action_invalidated_strategy_scale' in item['reason_codes']
    assert item['frozen_atr'] == 1234 and item['signal_date'] == '2025-12-31'
    assert 'company_action_invalidated_strategy_scale' in evidence['reason_codes']
    valuation = p.valuation(a, at(8), date(2026, 1, 7))
    assert 'company_action_invalidated_strategy_scale' in valuation['reasons']
    print('scale', item['frozen_atr'], item['reason_codes'])


def test_company_event_void_restores_quantity_and_reverses_the_cash_tail(ledger):
    """作废已应用的公司行为：数量与成本完全还原，现金尾差冲正而不是删除。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 3000000, 5)
    opened = p.opening(OpeningInput(request_id='base', account_id=a, instrument_id=i, quantity=206,
                                    cost_fen=100000, fees_fen=0, occurred_at=at(5, 9),
                                    note='verified opening holdings'))
    applied = p.company(CompanyInput(request_id='merge', account_id=a, instrument_id=i, kind='merge',
                                     ratio_from=3, ratio_to=2, cash_tail_fen=1234, occurred_at=at(6, 12),
                                     note='3 for 2 reverse split, cash in lieu 12.34'))
    batch = lots_of(svc)[opened['lot_id']]
    assert (batch['opened_qty'], batch['remaining_qty']) == (137, 137)
    tail = [x for x in data(svc, 'cash') if x['kind'] == 'adjustment'][0]

    voided = p.void_company_event(applied['id'], 'void-merge', 'ratio was recorded backwards')
    assert voided['voided_at'] is not None and voided['tail_reversed_fen'] == 1234
    batch = lots_of(svc)[opened['lot_id']]
    assert (batch['opened_qty'], batch['remaining_qty']) == (206, 206)
    assert batch['remaining_cost_fen'] == 100000 and batch['status'] == 'open'
    reversal = [x for x in data(svc, 'cash') if x['kind'] == 'reversal' and x['reversal_of'] == tail['id']]
    assert [(x['amount_fen'], x['note']) for x in reversal] == [(-1234, 'ratio was recorded backwards')]
    assert p.openings()['items'][0]['remaining_qty'] == 206
    print('company-void', batch['opened_qty'], reversal[0]['amount_fen'])


def test_correction_is_idempotent_per_request_id(ledger):
    """同一 request_id 重复提交返回同一结果，数量不会被再次调整。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 2000000, 5)
    opened = p.opening(OpeningInput(request_id='base', account_id=a, instrument_id=i, quantity=100,
                                    cost_fen=100000, fees_fen=0, occurred_at=at(5, 9),
                                    note='verified opening holdings'))
    body = OpeningCorrectionInput(request_id='once', opening_id=opened['id'], quantity=60,
                                  cost_fen=100000, fees_fen=0, occurred_at=at(5, 9),
                                  note='counted twice on the statement')
    first = p.correct_opening(body)
    revision = p.valuation(a, at(6), date(2026, 1, 5))['ledger_revision']
    assert p.correct_opening(body) == first
    assert p.correct_opening(body.model_copy(update={'opening_id': ''}), opened['id']) == first
    assert p.valuation(a, at(6), date(2026, 1, 5))['ledger_revision'] == revision
    current = [x for x in data(svc, 'lots') if x['opening_id'] == first['id']]
    assert [(x['opened_qty'], x['remaining_qty']) for x in current] == [(60, 60)]
    assert len(p.openings()['items']) == 2
    with pytest.raises(TradeError):
        p.correct_opening(body.model_copy(update={'request_id': 'other', 'quantity': 60,
                                                  'note': 'different content, same target'}))
    print('idempotent', first['id'], len(p.openings()['items']))


def test_company_input_rejects_ambiguous_payloads_before_any_write(ledger):
    """比例与金额各有归属：不合法组合在写库前就被拒，账本一行不动。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 2000000, 5)
    opened = p.opening(OpeningInput(request_id='base', account_id=a, instrument_id=i, quantity=100,
                                    cost_fen=100000, fees_fen=0, occurred_at=at(5, 9),
                                    note='verified opening holdings'))
    before = snapshot(svc)
    cases = [
        {'kind': 'split', 'amount_fen': 100, 'ratio_from': 1, 'ratio_to': 2},       # 拆分不得带金额
        {'kind': 'dividend', 'ratio_from': 1, 'ratio_to': 2},                        # 分红不得带比例
        {'kind': 'dividend'},                                                        # 分红必须有金额
        {'kind': 'split', 'ratio_from': 1},                                          # 比例必须成对
        {'kind': 'split', 'ratio_from': 2, 'ratio_to': 2},                            # 比例必须改变股数
        {'kind': 'rights', 'ratio_from': 1, 'ratio_to': 2},                          # 只有拆并带比例
        {'kind': 'split', 'ratio_from': 1, 'ratio_to': 2, 'cash_tail_fen': 0},        # 尾差必须为正
    ]
    for index, change in enumerate(cases):
        body = dict(request_id=f'bad-{index}', account_id=a, instrument_id=i,
                    occurred_at=at(6, 12).isoformat(), note='invalid combination')
        body.update(change)
        with pytest.raises(ValidationError):
            CompanyInput(**body)
    assert snapshot(svc) == before
    with pytest.raises(ValidationError):
        OpeningCorrectionInput(request_id='no-note', opening_id=opened['id'], quantity=10,
                               cost_fen=1000, fees_fen=0, occurred_at=at(5, 9), note='')
    assert snapshot(svc) == before


def test_rights_and_other_stay_recorded_data_gaps(ledger):
    """权利与其他公司行为不自动调整数量：只记录缺口并阻断该标的的估值。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 2000000, 5)
    opened = p.opening(OpeningInput(request_id='base', account_id=a, instrument_id=i, quantity=100,
                                    cost_fen=100000, fees_fen=0, occurred_at=at(5, 9),
                                    note='verified opening holdings'))
    quote(p, i, 5, 10)
    quote(p, i, 6, 10)
    gap = p.company(CompanyInput(request_id='rights', account_id=a, instrument_id=i, kind='rights',
                                 occurred_at=at(6, 12), note='rights issue terms not yet verified'))
    assert gap['reason_codes'] == ['company_action_not_modeled']
    batch = lots_of(svc)[opened['lot_id']]
    assert (batch['opened_qty'], batch['remaining_qty']) == (100, 100)
    assert len(data(svc, 'cash')) == 1 and len(data(svc, 'trades')) == 0
    result = p.valuation(a, at(6))
    assert result['equity_fen'] is None and 'unresolved_company_action' in result['reasons']
    assert result['positions'][0]['quality'] == 'derived_unverified'
    # 缺口可以按审计口径直接作废，不需要任何数量还原。
    voided = p.void_company_event(gap['id'], 'void-rights', 'terms will be entered separately')
    assert voided['voided_at'] is not None
    assert p.valuation(a, at(6))['equity_fen'] == 2100000
    print('gap', gap['reason_codes'], voided['kind'])


def test_openings_list_pages_and_exposes_history(ledger):
    """列表接口翻页、给出当前行标记与对应批次的剩余量。"""
    svc, a, i = ledger
    p = PortfolioService(svc.db)
    cash(svc, a, 'initial', 'opening', 2000000, 5)
    opened = p.opening(OpeningInput(request_id='base', account_id=a, instrument_id=i, quantity=100,
                                    cost_fen=100000, fees_fen=5, occurred_at=at(5, 9),
                                    note='verified opening holdings'))
    fixed = p.correct_opening(OpeningCorrectionInput(
        request_id='fix', opening_id=opened['id'], quantity=120, cost_fen=150000, fees_fen=7,
        occurred_at=at(5, 9), note='statement recount'))
    first = p.openings(limit=1)
    assert first['total'] == 2 and len(first['items']) == 1 and first['offset'] == 0
    assert first['items'][0]['is_current'] == 0 and first['items'][0]['superseded_by'] == fixed['id']
    second = p.openings(limit=1, offset=1)
    assert second['items'][0]['id'] == fixed['id'] and second['items'][0]['is_current'] == 1
    assert (second['items'][0]['opened_qty'], second['items'][0]['remaining_qty']) == (120, 120)
    assert second['items'][0]['remaining_cost_fen'] == 150000
    assert p.openings(account_id='missing')['items'] == []
    assert p.openings()['ledger_revision'] >= 1 and 'earliest_affected_day' in p.openings()
    print('list', first['total'], first['items'][0]['is_current'], second['items'][0]['is_current'])


def test_correction_api_endpoints_use_the_same_error_mapping(cfg):
    """HTTP 层：分页列表、更正、作废、公司行为作废都能直达同一条服务路径。"""
    from fastapi.testclient import TestClient

    from backend.config import AppConfig
    from backend.main import create_app

    with TestClient(create_app(AppConfig(market_auto_refresh=False), database=cfg)) as client:
        a = client.post('/api/v1/accounts', json={'request_id': 'acc-api', 'name': 'api',
                                                  'effective_at': '2000-01-01T00:00:00+08:00'}).json()['id']
        i = client.post('/api/v1/instruments', json={'request_id': 'ins-api', 'code': '510300',
                                                     'market': 'SH', 'kind': 'etf'}).json()['id']
        assert client.post('/api/v1/cash', json={
            'request_id': 'cash-api', 'account_id': a, 'kind': 'opening', 'amount_fen': 2000000,
            'occurred_at': at(5, 8).isoformat(), 'note': 'verified statement'}).status_code == 200
        opened = client.post('/api/v1/opening-positions', json={
            'request_id': 'base-api', 'account_id': a, 'instrument_id': i, 'quantity': 100,
            'cost_fen': 100000, 'fees_fen': 0, 'occurred_at': at(5, 9).isoformat(),
            'note': 'verified opening holdings'}).json()
        applied = client.post('/api/v1/company-events', json={
            'request_id': 'merge-api', 'account_id': a, 'instrument_id': i, 'kind': 'merge',
            'ratio_from': 2, 'ratio_to': 1, 'occurred_at': at(6, 12).isoformat(),
            'note': '2 for 1 consolidation'}).json()

        listing = client.get('/api/v1/openings', params={'account_id': a, 'limit': 10})
        assert listing.status_code == 200, listing.text
        payload = listing.json()
        assert payload['total'] == 1 and payload['limit'] == 10 and payload['offset'] == 0
        assert set(payload) >= {'items', 'ledger_revision', 'total', 'source', 'quality', 'as_of'}
        assert payload['items'][0]['is_current'] == 1

        # 先作废公司行为：批次一旦被后续更正替换，作废就必须安全拒绝而不是猜。
        event = client.post('/api/v1/portfolio/company-events/%s/void' % applied['id'],
                            json={'request_id': 'api-company-void', 'reason': 'ratio reversed'})
        assert event.status_code == 200, event.text
        assert event.json()['voided_at'] is not None
        assert client.post('/api/v1/portfolio/company-events/%s/void' % applied['id'],
                           json={'request_id': 'api-company-void-2', 'reason': 'twice'}).status_code == 409
        assert client.post('/api/v1/portfolio/company-events/missing/void',
                           json={'request_id': 'api-company-void-3', 'reason': 'absent'}).status_code == 404
        adjusted = client.get('/api/v1/openings', params={'account_id': a}).json()['items'][0]
        # ``wire`` 把数量按既有约定序列化成字符串，这里只关心数值是否还原。
        assert int(adjusted['opened_qty']) == int(adjusted['remaining_qty']) == 100

        corrected = client.post('/api/v1/portfolio/openings/%s/correct' % opened['id'],
                                json={'request_id': 'api-fix', 'quantity': 120, 'cost_fen': 0,
                                      'fees_fen': 0, 'occurred_at': at(5, 9).isoformat(),
                                      'note': 'API recount'})
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()['supersedes'] == opened['id']
        assert client.post('/api/v1/portfolio/openings/%s/correct' % opened['id'],
                           json={'request_id': 'api-fix-again', 'quantity': 120, 'cost_fen': 0,
                                 'fees_fen': 0, 'occurred_at': at(5, 9).isoformat(),
                                 'note': 'already replaced'}).status_code == 409

        voided = client.post('/api/v1/portfolio/openings/%s/void' % corrected.json()['id'],
                             json={'request_id': 'api-void', 'reason': 'wrong account'})
        assert voided.status_code == 200, voided.text
        assert voided.json()['voided_at'] is not None
        assert client.post('/api/v1/portfolio/openings/%s/void' % corrected.json()['id'],
                           json={'request_id': 'api-void-2', 'reason': 'again'}).status_code == 409
        assert client.post('/api/v1/portfolio/openings/%s/void' % corrected.json()['id'],
                           json={'request_id': 'api-void-3', 'reason': ''}).status_code == 422
        assert client.get('/api/v1/openings', params={'account_id': a}).json()['total'] == 2
    print('api', listing.status_code, corrected.status_code, voided.status_code, event.status_code)



def test_opening_cash_correction_preserves_cash_facts_and_boundary(ledger):
    from backend.application.finance import FinanceService,CashCorrection,VoidInput
    svc,a,i=ledger; p=PortfolioService(svc.db); f=FinanceService(svc.db)
    opened=cash(svc,a,'initial','opening',500000,5)
    buy=svc.submit(trade(a,i,'buy-after-base',day=6,price='10',fees_fen=0))
    quote(p,i,6,10)
    replacement=f.change_cash(opened['id'],CashCorrection(request_id='fix-base',amount_fen=600000,occurred_at=at(5,8),note='broker confirmed'))
    assert p.valuation(a,at(6))['cash_fen']==500000
    assert p.valuation(a,at(6))['equity_fen']==600000
    assert svc.get(buy['id'])['status']=='effective'
    old=snapshot(svc)
    with pytest.raises(Conflict):
        f.change_cash(replacement['id'],CashCorrection(request_id='invalid-base',amount_fen=600000,occurred_at=at(7,8),note='bad boundary'))
    assert snapshot(svc)==old
    f.change_cash(replacement['id'],VoidInput(request_id='void-base',reason='incorrect opening balance'))
    assert p.valuation(a,at(6))['cash_fen'] is None
    assert svc.get(buy['id'])['status']=='effective'
