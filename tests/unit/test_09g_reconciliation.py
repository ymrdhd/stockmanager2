"""09G read-only ledger reconciliation preview; synthetic databases only."""
from fastapi.testclient import TestClient
import pytest

from backend.application.portfolio import PortfolioService
from backend.application.trading.service import Conflict, TradeError
from backend.config import AppConfig
from backend.main import create_app
from test_pnl import at, cash, quote
from test_trading import cfg, ledger, trade


def test_reconciliation_preview_reconciles_sales_lots_cash_and_pnl(ledger):
    svc, account, instrument = ledger
    portfolio = PortfolioService(svc.db)
    cash(svc, account, 'opening', 'opening', 3_000_000, 5)
    svc.submit(trade(account, instrument, 'buy', day=5, quantity=1_000, price='10', fees_fen=0))
    svc.submit(trade(account, instrument, 'sell', day=6, side='sell', quantity=200,
                     price='11.50', fees_fen=500, executed_at=at(6, 10)))
    quote(portfolio, instrument, 5, 10)
    quote(portfolio, instrument, 6, 12)

    before = {name: svc.list_records(name) for name in ('trades', 'lots', 'allocations', 'cash')}
    result = portfolio.reconciliation(account, at(6))

    assert result['source'] == 'sqlite_ledger_read_snapshot'
    assert result['summary']['effective_trade_count'] == 2
    assert result['summary']['buy_count'] == 1
    assert result['summary']['sell_count'] == 1
    assert result['summary']['allocation_count'] == 1
    assert len(result['allocation_details']) == 1
    assert result['allocation_details'][0]['quantity'] == 200
    assert result['summary']['open_quantity'] == 800
    assert result['summary']['open_lot_count'] == 1
    assert result['summary']['cash_quality'] == 'valid'
    assert result['summary']['daily_pnl_fen'] == result['summary']['equity_pnl_fen'] == 189_500
    assert result['summary']['reconciliation_difference_fen'] == 0
    checks = {item['code']: item['status'] for item in result['checks']}
    assert next(x for x in result['checks'] if x['code']=='market_valuation')['message'] == '持仓市值有可信行情依据'
    assert checks == {
        'sale_allocations': 'pass',
        'position_lots': 'pass',
        'cash_projection': 'pass',
        'market_valuation': 'pass',
        'daily_equity_pnl': 'pass',
        'pending_trades': 'pass',
        'unresolved_company_actions': 'pass',
    }
    assert result['exceptions'] == []
    assert {name: svc.list_records(name) for name in before} == before


def test_reconciliation_preview_flags_sale_allocation_mismatch_without_repair(ledger):
    svc, account, instrument = ledger
    portfolio = PortfolioService(svc.db)
    cash(svc, account, 'opening', 'opening', 3_000_000, 5)
    svc.submit(trade(account, instrument, 'buy', day=5, quantity=100, price='10', fees_fen=0))
    sale = svc.submit(trade(account, instrument, 'sell', day=6, side='sell', quantity=40,
                            price='11', fees_fen=0, executed_at=at(6, 10)))
    with svc.db.transaction() as conn:
        conn.execute('UPDATE lot_allocations SET quantity=39 WHERE sell_execution_id=?',
                     (sale['id'],))
    before_revision = svc.list_records('trades')['ledger_revision']

    result = portfolio.reconciliation(account, at(6))

    checks = {item['code']: item for item in result['checks']}
    assert checks['sale_allocations']['status'] == 'attention'
    assert checks['sale_allocations']['mismatch_count'] == 1
    assert result['exceptions'][0]['kind'] == 'sale_allocation_quantity'
    assert result['exceptions'][0]['expected'] == 40
    assert result['exceptions'][0]['actual'] == 39
    assert svc.list_records('trades')['ledger_revision'] == before_revision


def test_reconciliation_preview_rejects_stale_snapshot(ledger):
    svc, account, _ = ledger
    portfolio = PortfolioService(svc.db)
    initial = portfolio.reconciliation(account, at(6))
    with pytest.raises(TradeError, match='snapshot guard'):
        portfolio.reconciliation(account, at(6), ledger_revision=initial['ledger_revision'])
    cash(svc, account, 'deposit', 'deposit', 100, 6)
    with pytest.raises(Conflict, match='snapshot changed'):
        portfolio.reconciliation(
            account, at(6),
            ledger_revision=initial['ledger_revision'],
            quote_revision=initial['quote_revision'],
            database_uuid=initial['database_uuid'],
            generation=initial['generation'],
        )


def test_reconciliation_http_requires_timezone_and_returns_read_only_report(cfg):
    with TestClient(create_app(AppConfig(market_auto_refresh=False), database=cfg)) as client:
        account = client.post('/api/v1/accounts', json={
            'request_id': 'reconcile-account', 'name': '对账测试账户',
            'effective_at': '2000-01-01T00:00:00+08:00',
        }).json()['id']
        missing_time = client.get(f'/api/v1/portfolio/{account}/reconciliation')
        assert missing_time.status_code == 422
        response = client.get(
            f'/api/v1/portfolio/{account}/reconciliation',
            params={'as_of': at(6).isoformat()},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['account_id'] == account
        assert result['source'] == 'sqlite_ledger_read_snapshot'
        assert result['summary']['effective_trade_count'] == 0
        checks = {item['code']: item['status'] for item in result['checks']}
        assert checks['sale_allocations'] == 'pass'
        assert checks['cash_projection'] == 'unavailable'
        assert result['summary']['cash_fen'] is None
