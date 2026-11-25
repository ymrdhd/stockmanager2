"""Near-stop warnings are display-only and require verified current evidence."""
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.application.market_calendar import CalendarUnavailable
from backend.application.portfolio import QuoteInput
from backend.application.strategy.service import SOURCE_LEDGER, SOURCE_UNVERIFIED, StrategyService
from backend.application.trading.service import AccountInput, InstrumentInput, TradeService
from backend.infrastructure.database import DatabaseSettings, initialize_database, open_database


@pytest.fixture
def strategy_context(tmp_path):
    path = tmp_path / 'official.sqlite'
    binding = tmp_path / 'official.json'
    identity = initialize_database(path, role='formal', confirm=True, binding_path=binding)
    settings = DatabaseSettings(path, identity.database_uuid, 1, path, binding_path=binding)
    clock = datetime(2026, 11, 24, 8, 30, tzinfo=UTC)  # 16:30 Beijing, after close
    with open_database(settings) as db:
        trading = TradeService(db)
        account = trading.create_account(AccountInput(
            request_id='near-stop-account', name='synthetic securities',
            effective_at='2000-01-01T00:00:00+08:00'))['id']
        instrument = trading.create_instrument(InstrumentInput(
            request_id='near-stop-instrument', code='510300', market='SH', kind='etf'))['id']
        strategy = StrategyService(db, clock=lambda: clock, near_stop_bps=200)
        with db.read() as conn:
            revision = conn.execute('SELECT ledger_revision FROM ledger_state').fetchone()[0]
        yield strategy, account, instrument, revision, clock


def position_state(source=SOURCE_LEDGER, *, is_open=True):
    return SimpleNamespace(
        execution_source=source,
        is_open=is_open,
        stop_price=Decimal('10.00'),
        adds=[{'stop_price': Decimal('9.90')}],
    )


def held_lots(instrument, **overrides):
    base = {
        'signal_verified': True,
        'scale_invalid': False,
        'strategy_deviation': False,
        'instrument_id': instrument,
    }
    return [
        {**base, 'lot_role': 'main', **overrides.get('main', {})},
        {**base, 'lot_role': 'sub', **overrides.get('sub', {})},
    ]


def add_quote(strategy, instrument, observed_at, *, price='10.05', kind='close', quality='valid', request_id='near-stop-quote'):
    return strategy.portfolio.quote(QuoteInput(
        request_id=request_id,
        instrument_id=instrument,
        price=Decimal(price),
        source='synthetic-near-stop-test',
        observed_at=observed_at,
        trading_day='2026-11-24',
        kind=kind,
        quality=quality,
    ))


def test_verified_main_and_sub_lots_show_only_display_warnings(strategy_context):
    strategy, account, instrument, revision, clock = strategy_context
    add_quote(strategy, instrument, datetime(2026, 11, 24, 8, 0, tzinfo=UTC))

    warnings = strategy._near_stop(
        account, position_state(), held_lots(instrument), '2026-11-24', revision)

    assert [item['role'] for item in warnings] == ['main', 'sub']
    assert all(item['status'] == 'near_stop' for item in warnings)
    assert all(item['threshold_bps'] == 200 for item in warnings)
    assert all(item['display_only'] is True for item in warnings)
    assert all(item['quote_source'] == 'synthetic-near-stop-test' for item in warnings)


@pytest.mark.parametrize('quality', ['stale', 'conflict', 'missing'])
def test_untrusted_quote_never_generates_near_stop_warning(strategy_context, quality):
    strategy, account, instrument, revision, clock = strategy_context
    if quality == 'stale':
        # A valid intraday quote older than 30 minutes becomes stale at the actual cutoff.
        add_quote(strategy, instrument, datetime(2026, 11, 24, 6, 0, tzinfo=UTC), kind='intraday')
    elif quality == 'conflict':
        observed = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)
        add_quote(strategy, instrument, observed, price='10.05', request_id='near-stop-valid-a')
        add_quote(strategy, instrument, observed, price='10.10', request_id='near-stop-valid-b')
    elif quality == 'missing':
        pass
    assert strategy._near_stop(
        account, position_state(), held_lots(instrument), '2026-11-24', revision) == []


@pytest.mark.parametrize('overrides', [
    {'main': {'signal_verified': False}},
    {'sub': {'strategy_deviation': True}},
    {'main': {'scale_invalid': True}},
])
def test_unverified_or_deviated_lot_suppresses_all_warnings(strategy_context, overrides):
    strategy, account, instrument, revision, clock = strategy_context
    add_quote(strategy, instrument, datetime(2026, 11, 24, 8, 0, tzinfo=UTC))

    assert strategy._near_stop(
        account, position_state(), held_lots(instrument, **overrides), '2026-11-24', revision) == []


def test_stale_position_evidence_revision_suppresses_warning(strategy_context):
    strategy, account, instrument, revision, clock = strategy_context
    add_quote(strategy, instrument, datetime(2026, 11, 24, 8, 0, tzinfo=UTC))

    assert strategy._near_stop(
        account, position_state(), held_lots(instrument), '2026-11-24', revision + 1) == []


def test_unverified_or_closed_model_position_suppresses_warning(strategy_context):
    strategy, account, instrument, revision, clock = strategy_context
    add_quote(strategy, instrument, datetime(2026, 11, 24, 8, 0, tzinfo=UTC))

    assert strategy._near_stop(
        account, position_state(SOURCE_UNVERIFIED), held_lots(instrument), '2026-11-24', revision) == []
    assert strategy._near_stop(
        account, position_state(is_open=False), held_lots(instrument), '2026-11-24', revision) == []


def test_calendar_unavailable_suppresses_near_stop_warning(strategy_context, monkeypatch):
    strategy, account, instrument, revision, clock = strategy_context
    add_quote(strategy, instrument, datetime(2026, 11, 24, 8, 0, tzinfo=UTC))

    def unavailable(_):
        raise CalendarUnavailable('calendar coverage unavailable')

    monkeypatch.setattr('backend.application.strategy.service.market_window', unavailable)
    assert strategy._near_stop(
        account, position_state(), held_lots(instrument), '2026-11-24', revision) == []


def test_price_outside_configured_warning_band_has_no_warning(strategy_context):
    strategy, account, instrument, revision, clock = strategy_context
    add_quote(strategy, instrument, datetime(2026, 11, 24, 8, 0, tzinfo=UTC), price='10.21')

    assert strategy._near_stop(
        account, position_state(), held_lots(instrument), '2026-11-24', revision) == []


def test_price_at_protection_is_reported_as_reached_not_near(strategy_context):
    strategy, account, instrument, revision, clock = strategy_context
    add_quote(strategy, instrument, datetime(2026, 11, 24, 8, 0, tzinfo=UTC), price='10.00')

    warnings = strategy._near_stop(
        account, position_state(), held_lots(instrument), '2026-11-24', revision)
    assert [item['status'] for item in warnings] == ['protection_reached', 'near_stop']
