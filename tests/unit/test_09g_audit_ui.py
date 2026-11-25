"""The web audit table must consume the real, opt-in audit response contract."""
import json

import pytest

from backend.application.trading.service import AccountInput, InstrumentInput, TradeService
from backend.infrastructure.database import DatabaseSettings, initialize_database, open_database
from test_trading import trade


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / 'official.sqlite'
    binding = tmp_path / 'official.json'
    identity = initialize_database(path, role='formal', confirm=True, binding_path=binding)
    settings = DatabaseSettings(path, identity.database_uuid, 1, path, binding_path=binding)
    with open_database(settings) as db:
        service = TradeService(db)
        account = service.create_account(AccountInput(request_id='audit-account', name='test account', effective_at='2000-01-01T00:00:00+08:00'))['id']
        instrument = service.create_instrument(InstrumentInput(request_id='audit-instrument', code='510300', market='SH', kind='etf'))['id']
        yield service, account, instrument


def test_trade_audit_detail_exposes_the_fields_rendered_by_workspace(ledger):
    service, account, instrument = ledger
    result = service.submit(trade(account, instrument, '09g-audit-ui'))

    ordinary = service.get(result['id'])
    assert 'audit_events' not in ordinary

    detail = service.get(result['id'], include_audit=True)
    assert detail['id'] == result['id']
    assert detail['audit_events']
    event = detail['audit_events'][-1]
    assert {'id', 'event', 'request_id', 'at', 'actor', 'details'} <= set(event)
    assert event['request_id'] == '09g-audit-ui'
    assert event['at']
    assert event['actor']
    payload = json.loads(event['details'])
    assert payload['strategy_deviation'] is False
    assert payload['reason'] is None
