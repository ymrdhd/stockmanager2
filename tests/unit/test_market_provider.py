"""ISS-022 真实证券行情：腾讯未复权报价/日线的证据契约（固定夹具，不联网）。"""
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from backend.application.market import MarketService
from backend.application.market_calendar import SHANGHAI, session_bounds
from backend.infrastructure.market.eastmoney import MarketDataError
from backend.infrastructure.market.tencent import (
    RAW_KLINE_URL, TencentError, TencentQuoteProvider,
)
from test_trading import cfg, ledger  # noqa: F401  (fixtures)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=SHANGHAI)


def instrument(code: str, market: str, kind: str, name: str = 'x') -> dict:
    return {'id': f'{market}-{code}', 'code': code, 'market': market, 'kind': kind, 'name': name}


def quote_line(prefix: str, code: str, price: str, previous: str, stamp: str,
               name: str = '样本', high: str = '4.130', low: str = '4.080') -> str:
    """腾讯即时报价真实格式：0 市场标志, 1 名称, 2 代码, 3 现价, 4 昨收, 30 时间戳。"""
    parts = [prefix[0:1] == 's' and '1' or '51', name, code, price, previous, previous]
    parts += [''] * 23                       # 6..28 买卖档位与量价占位
    parts += ['', stamp, '0.000', '0.00', high, low]
    parts += ['CNY', '0', '']
    return f'v_{prefix}{code}="{"~".join(parts)}";'


def provider_for(calls: list[str], *, text: str = '', daily: dict | None = None,
                 status: int = 200) -> TencentQuoteProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if 'qt.gtimg.cn' in str(request.url):
            return httpx.Response(status, content=text.encode('gbk'))
        return httpx.Response(200, json=daily or {})

    return TencentQuoteProvider(httpx.Client(transport=httpx.MockTransport(handler)),
                                min_interval=0)


# -- 即时报价 -----------------------------------------------------------

def test_spot_after_close_is_confirmed_close_for_sh_etf():
    calls: list[str] = []
    text = quote_line('sh', '510300', '4.123', '4.100', '20261124161458', '沪深300ETF')
    provider = provider_for(calls, text=text)
    quote = provider.spot(instrument('510300', 'SH', 'etf'), local(24, 16, 20))
    assert calls == ['https://qt.gtimg.cn/q=sh510300']
    assert quote['instrument_id'] == 'SH-510300'
    assert quote['price'] == Decimal('4.123')
    assert quote['kind'] == 'close'
    assert quote['trading_day'] == date(2026, 11, 24)
    assert quote['observed_at'] == session_bounds(date(2026, 11, 24))[1]
    assert quote['quality'] == 'valid'
    assert quote['price_basis'] == 'unadjusted_security'
    assert quote['source'] == 'tencent:unadjusted_spot'


def test_spot_during_session_is_intraday_for_sz_etf():
    calls: list[str] = []
    text = quote_line('sz', '159915', '2.292', '2.295', '20261124103000', '创业板ETF')
    quote = provider_for(calls, text=text).spot(instrument('159915', 'SZ', 'etf'), local(24, 10, 30))
    assert 'q=sz159915' in calls[0]
    assert quote['price'] == Decimal('2.292')
    assert quote['kind'] == 'intraday'
    assert quote['trading_day'] == date(2026, 11, 24)
    assert quote['observed_at'] == local(24, 10, 30)


def test_spot_preopen_returns_previous_confirmed_close_for_stock():
    calls: list[str] = []
    text = quote_line('sh', '600519', '1460.00', '1439.00', '20261124150000', '贵州茅台',
                      high='1463.26', low='1420.00')
    quote = provider_for(calls, text=text).spot(instrument('600519', 'SH', 'stock'), local(25, 8, 30))
    assert 'q=sh600519' in calls[0]
    assert quote['price'] == Decimal('1460.00')
    assert quote['kind'] == 'close'
    assert quote['trading_day'] == date(2026, 11, 24)
    assert quote['quality'] == 'valid'


@pytest.mark.parametrize('text', [
    'v_pv_none_match="1";',                                  # 源端无此标的
    'v_sh510300="";',                                        # 空报价
    quote_line('sh', '510300', '', '4.100', '20261124140000'),  # 缺现价
    quote_line('sh', '510300', '0', '4.100', '20261124140000'),  # 零价不是价格
    quote_line('sh', '510300', '4.123', '4.100', ''),         # 缺时间戳
    quote_line('sh', '600519', '4.123', '4.100', '20261124140000'),  # 不同证券
])
def test_spot_rejects_unusable_quote_evidence(text):
    calls: list[str] = []
    provider = provider_for(calls, text=text)
    with pytest.raises(TencentError):
        provider.spot(instrument('510300', 'SH', 'etf'), local(24, 16, 20))


@pytest.mark.parametrize('bad', [
    {'id': 'x', 'code': '510300', 'market': 'HK', 'kind': 'etf'},
    {'id': 'x', 'code': '51030', 'market': 'SH', 'kind': 'etf'},
    {'id': 'x', 'code': '510300', 'market': 'SH', 'kind': 'fund'},
    {'id': 'x', 'code': '000001', 'market': 'SH', 'kind': 'stock'},
    {'id': 'x', 'code': '510300', 'market': 'SZ', 'kind': 'etf'},
    {'id': 'x', 'code': '51030O', 'market': 'SH', 'kind': 'etf'},
])
def test_spot_rejects_unsupported_instrument(bad):
    calls: list[str] = []
    provider = provider_for(calls, text=quote_line('sh', '510300', '4.123', '4.100', '20261124140000'))
    with pytest.raises(TencentError):
        provider.spot(bad, local(24, 16, 20))
    assert calls == []


def test_spot_weekend_returns_last_completed_close_not_a_live_price():
    calls: list[str] = []
    text = quote_line('sh', '510300', '4.033', '4.023', '20261127150000', '沪深300ETF')
    quote = provider_for(calls, text=text).spot(instrument('510300', 'SH', 'etf'), local(28, 11))
    assert quote['kind'] == 'close'
    assert quote['trading_day'] == date(2026, 11, 27)
    assert quote['quality'] == 'valid'


def test_spot_break_phase_stays_intraday():
    calls: list[str] = []
    text = quote_line('sh', '510300', '4.050', '4.023', '20261124113000', '沪深300ETF')
    quote = provider_for(calls, text=text).spot(instrument('510300', 'SH', 'etf'), local(24, 12, 10))
    assert quote['kind'] == 'intraday'
    assert quote['trading_day'] == date(2026, 11, 24)


# -- 不复权日线 ---------------------------------------------------------

def daily_payload(code: str, rows: list[list[str]]) -> dict:
    return {'code': 0, 'msg': '', 'data': {code: {'day': rows, 'version': '16'}}}


def test_closes_returns_only_requested_completed_sessions_on_raw_endpoint():
    calls: list[str] = []
    payload = daily_payload('sh510300', [
        ['2026-11-19', '4.000', '3.990', '4.020', '3.980', '100.000'],
        ['2026-11-20', '4.100', '4.120', '4.150', '4.050', '123456.000'],
        ['2026-11-24', '4.065', '4.033', '4.084', '4.031', '13717503.000'],
    ])
    provider = provider_for(calls, daily=payload)
    quotes = provider.closes(instrument('510300', 'SH', 'etf'), {date(2026, 11, 20)}, local(24, 21))
    assert len(quotes) == 1
    quote = quotes[0]
    assert quote['price'] == Decimal('4.12')
    assert quote['kind'] == 'close'
    assert quote['trading_day'] == date(2026, 11, 20)
    assert quote['observed_at'] == session_bounds(date(2026, 11, 20))[1]
    assert quote['quality'] == 'valid'
    assert quote['price_basis'] == 'unadjusted_security'
    assert quote['source'] == 'tencent:unadjusted_daily'
    # 不复权端点：RAW_KLINE_URL 且 param 不带 qfq 后缀。
    assert calls[0].startswith(RAW_KLINE_URL)
    assert 'qfq' not in calls[0]
    assert 'param=sh510300,day' in calls[0]


def test_closes_attaches_provider_name_so_an_after_close_refresh_has_names():
    """Daily bars carry no name; a close-only refresh must still record the real name."""
    calls: list[str] = []
    payload = daily_payload('sh510300', [
        ['2026-11-24', '4.065', '4.033', '4.084', '4.031', '13717503.000'],
    ])
    text = quote_line('sh', '510300', '4.033', '4.065', '20261124161458', '沪深300ETF')
    quotes = provider_for(calls, text=text, daily=payload).closes(
        instrument('510300', 'SH', 'etf', name=''), {date(2026, 11, 24)}, local(24, 21))
    assert [q['trading_day'] for q in quotes] == [date(2026, 11, 24)]
    assert quotes[-1]['instrument_name'] == '沪深300ETF'
    assert any('q=sh510300' in call for call in calls)


@pytest.mark.parametrize('code,market,kind,rows,day,price', [
    ('159915', 'SZ', 'etf',
     [['2026-11-23', '2.203', '2.295', '2.314', '2.202', '1.000']], date(2026, 11, 23), '2.295'),
    ('600519', 'SH', 'stock',
     [['2026-11-23', '1475.350', '1439.000', '1478.000', '1428.000', '42924.000']],
     date(2026, 11, 23), '1439.000'),
])
def test_closes_covers_sz_etf_and_sh_stock(code, market, kind, rows, day, price):
    calls: list[str] = []
    provider = provider_for(calls, daily=daily_payload(f'{market.lower()}{code}', rows))
    quotes = provider.closes(instrument(code, market, kind), {day}, local(24, 21))
    assert [q['trading_day'] for q in quotes] == [day]
    assert quotes[0]['price'] == Decimal(price)
    assert 'qfq' not in calls[0]


def test_closes_skips_unconfirmed_same_day_bar():
    calls: list[str] = []
    payload = daily_payload('sh510300', [
        ['2026-11-24', '4.065', '4.033', '4.084', '4.031', '13717503.000'],
    ])
    provider = provider_for(calls, daily=payload)
    # 15:05 仍在延迟窗口内：当日 K 线还不能当已完成收盘。
    assert provider.closes(instrument('510300', 'SH', 'etf'), {date(2026, 11, 24)},
                           local(24, 15, 5)) == []
    assert provider.closes(instrument('510300', 'SH', 'etf'), {date(2026, 11, 24)},
                           local(24, 15, 20))[0]['price'] == Decimal('4.033')


def test_closes_rejects_inconsistent_bar_and_empty_request():
    calls: list[str] = []
    payload = daily_payload('sh510300', [
        ['2026-11-20', '4.100', '4.120', '4.110', '4.050', '1.000'],   # 收 > 高
    ])
    provider = provider_for(calls, daily=payload)
    with pytest.raises(TencentError):
        provider.closes(instrument('510300', 'SH', 'etf'), {date(2026, 11, 20)}, local(24, 21))
    assert provider.closes(instrument('510300', 'SH', 'etf'), set(), local(24, 21)) == []


# -- MarketService 入账闸门 ---------------------------------------------

class FakeQuoteProvider:
    name = 'fake'
    available = True

    def __init__(self, evidence: list[dict]) -> None:
        self.evidence = evidence
        self.calls: list[tuple] = []

    def spot(self, instrument: dict, now: datetime) -> dict:
        self.calls.append(('spot', instrument['id']))
        raise TencentError('no intraday evidence in this fixture')

    def closes(self, instrument: dict, days: set[date], now: datetime) -> list[dict]:
        self.calls.append(('closes', instrument['id'], sorted(days)))
        return [dict(item) for item in self.evidence]


def evidence(instrument_id: str, day: date, price: str, **overrides) -> dict:
    raw = {'instrument_id': instrument_id, 'price': Decimal(price),
           'observed_at': session_bounds(day)[1], 'trading_day': day, 'kind': 'close',
           'quality': 'valid', 'source': 'fake:unadjusted_daily',
           'price_basis': 'unadjusted_security'}
    raw.update(overrides)
    return raw


def refresh_at(svc, provider, when: datetime, instrument_id: str) -> dict:
    market = MarketService(svc.db, provider, clock=lambda: when)
    return market.refresh(instrument_id)


def test_refresh_accounts_only_valid_unadjusted_close_evidence(ledger):
    svc, _account, master_id = ledger
    days = [date(2026, 11, 24), date(2026, 11, 23)]
    good = FakeQuoteProvider([evidence(master_id, day, '4.033') for day in days])
    market = MarketService(svc.db, good, clock=lambda: local(24, 16, 20))
    result = market.refresh(master_id)
    assert result['status'] == 'ok'
    assert result['items'][0]['errors'] == []
    assert len(result['items'][0]['revisions']) == 2
    status = market.status(master_id)
    item = status['items'][0]
    assert item['quality'] == 'valid'
    assert item['source'] == 'fake:unadjusted_daily'
    assert item['price_basis'] == 'unadjusted_security'
    assert item['kind'] == 'close'
    assert item['observed_at'] is not None and item['acquired_at'] is not None
    assert item['quote']['price'] == 40330
    revision = status['quote_revision']
    assert status['provider'] == 'fake'
    # 复权/指数口径与未完成收盘一律不入账，拒绝原因回落到状态里。
    for override in ({'price_basis': 'index_point'}, {'quality': 'stale'},
                     {'kind': 'intraday'}, {'trading_day': date(2026, 11, 20)}):
        bad = FakeQuoteProvider([evidence(master_id, day, '4.033', **override) for day in days])
        rejected = MarketService(svc.db, bad, clock=lambda: local(24, 16, 20))
        outcome = rejected.refresh(master_id)
        assert outcome['status'] == 'unavailable'
        assert outcome['items'][0]['revisions'] == []
        assert outcome['items'][0]['errors']
        after = rejected.status(master_id)
        assert after['quote_revision'] == revision
        assert after['last_refresh']['items'][0]['errors'] == outcome['items'][0]['errors']
        assert after['items'][0]['quote']['price'] == 40330


def test_default_provider_is_tencent_and_status_exposes_provenance(ledger):
    svc, _account, master_id = ledger
    market = MarketService(svc.db, clock=lambda: local(24, 16, 20))
    assert isinstance(market.provider, TencentQuoteProvider)
    status = market.status(master_id)
    assert status['provider'] == 'tencent'
    assert status['provider_available'] is True
    assert set(status['items'][0]) >= {'source', 'price_basis', 'kind', 'observed_at',
                                       'acquired_at', 'quality'}
    # 未取过证据时字段存在但为空，不是伪造价格。
    assert status['items'][0]['quote'] is None
    assert status['items'][0]['quality'] == 'missing'
    assert status['items'][0]['source'] is None


def test_tencent_error_stays_a_market_data_error():
    # refresh 只把 MarketDataError 转成拒绝原因，不吞成通用异常。
    assert issubclass(TencentError, MarketDataError)
