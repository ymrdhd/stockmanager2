"""Bounded HTTPS adapter for Eastmoney unadjusted mainland security prices.

The endpoint format is based on AKShare's public Eastmoney adapters; it is not
an exchange SLA or proof of the legacy strategy's actual provider configuration.
"""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from time import monotonic, sleep
from typing import Any

import httpx

from backend.application.market_calendar import (
    CalendarUnavailable, SHANGHAI, market_window, quote_time, session_bounds,
)

SPOT_URL = 'https://push2.eastmoney.com/api/qt/stock/get'
DAILY_URL = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
CLOSE_DELAY = timedelta(minutes=15)


class MarketDataError(RuntimeError):
    """A network or validation failure, safe to report without URL/credentials."""


def security_market(instrument: dict) -> int:
    """Accept known mainland stock/ETF code spaces, never index substitutes."""
    code, market, kind = instrument['code'], instrument['market'], instrument['kind']
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        raise MarketDataError('unsupported security code')
    prefixes = {
        ('SH', 'stock'): ('600', '601', '603', '605', '688'),
        ('SZ', 'stock'): ('000', '001', '002', '003', '300', '301'),
        ('SH', 'etf'): ('510', '511', '512', '513', '515', '516', '517', '518',
                        '520', '560', '561', '562', '563', '588', '589'),
        ('SZ', 'etf'): ('159',),
    }
    if not code.startswith(prefixes.get((market, kind), ())):
        raise MarketDataError('unsupported or ambiguous security market/kind; no index proxy')
    return 1 if market == 'SH' else 0


def exact_price(value: Any) -> Decimal:
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise ValueError
        price = Decimal(value)
        if not price.is_finite() or not 0 < price <= 1000000 or price != price.quantize(Decimal('0.0001')):
            raise ValueError
    except (ValueError, InvalidOperation) as exc:
        raise MarketDataError('invalid or unsupported price precision') from exc
    return price


class EastmoneyProvider:
    def __init__(self, client: httpx.Client | None = None, *, min_interval: float = 0.35) -> None:
        self._client = client
        self._last_call: float | None = None
        self._min_interval = min_interval
        self._cooldown_until = 0.0

    def _request(self, url: str, params: dict) -> dict:
        # All callers use one serialized refresh service. No retries of access
        # denials or rate-limit responses, no proxy bypass or TLS overrides.
        if monotonic() < self._cooldown_until:
            raise MarketDataError('provider cooling down after network/access failure')
        for attempt in range(2):
            if self._last_call is not None:
                sleep(max(0, self._min_interval - (monotonic() - self._last_call)))
            self._last_call = monotonic()
            try:
                if self._client is None:
                    with httpx.Client(timeout=6, follow_redirects=False) as client:
                        response = client.get(url, params=params)
                else:
                    response = self._client.get(url, params=params)
                if 500 <= response.status_code < 600 and attempt == 0:
                    sleep(0.5)
                    continue
                response.raise_for_status()
            except httpx.TransportError as exc:
                if attempt == 0:
                    sleep(0.5)
                    continue
                self._cooldown_until = monotonic()+60
                raise MarketDataError('provider transport unavailable') from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (401,403,429) or status >= 500:
                    retry = exc.response.headers.get('Retry-After','')
                    delay = int(retry) if retry.isascii() and retry.isdecimal() and len(retry)<=9 else 0
                    self._cooldown_until = monotonic()+max(900 if status in (401,403) else 60,delay)
                raise MarketDataError(f'provider HTTP {status}') from exc
            try:
                payload = json.loads(response.content, parse_float=Decimal)
                data = payload['data']
                if payload.get('rc') != 0 or not isinstance(data, dict):
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise MarketDataError('invalid or unavailable provider payload') from exc
            return data
        raise MarketDataError('provider request unavailable')

    def spot(self, instrument: dict, now: datetime) -> dict:
        market = security_market(instrument)
        code = instrument['code']
        data = self._request(SPOT_URL, {'secid': f'{market}.{code}',
                                       'fields': 'f43,f57,f59,f86,f107'})
        try:
            if data['f57'] != code or str(data['f107']) != str(market):
                raise MarketDataError('provider returned a different security')
            digits, raw, stamp = data['f59'], data['f43'], data['f86']
            if type(digits) is not int or not 0 <= digits <= 4 or type(raw) is not int or type(stamp) is not int:
                raise MarketDataError('provider price unit or time is missing')
            observed = datetime.fromtimestamp(stamp, UTC)
            day = observed.astimezone(SHANGHAI).date()
            window = market_window(now)
            if window.phase not in ('trading', 'break') or day != window.day or observed > now:
                raise MarketDataError('provider quote outside requested session')
            quote_time(day, observed, 'intraday')
            price = exact_price(Decimal(raw).scaleb(-digits))
        except (KeyError, ValueError, OverflowError, OSError, CalendarUnavailable) as exc:
            raise MarketDataError('invalid security quote timestamp or fields') from exc
        return {'instrument_id':instrument['id'], 'price':price, 'observed_at':observed,
                'trading_day':day, 'kind':'intraday', 'quality':'valid',
                'source':'eastmoney:unadjusted_spot','price_basis':'unadjusted_security'}

    def closes(self, instrument: dict, days: set[date], now: datetime) -> list[dict]:
        if not days:
            return []
        if (max(days)-min(days)).days > 45 or len(days) > 10:
            raise MarketDataError('daily request exceeds bounded window')
        market = security_market(instrument)
        code = instrument['code']
        data = self._request(DAILY_URL, {'secid':f'{market}.{code}', 'klt':'101', 'fqt':'0',
            'beg':min(days).strftime('%Y%m%d'), 'end':max(days).strftime('%Y%m%d'),
            'fields1':'f1,f2,f3,f4,f5,f6', 'fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'})
        if data.get('code') != code or str(data.get('market')) != str(market):
            raise MarketDataError('provider returned a different security')
        records = data.get('klines')
        if not isinstance(records,list):
            raise MarketDataError('provider daily series unavailable')
        result = []
        seen = set()
        try:
            for record in records:
                if not isinstance(record,str):
                    raise ValueError
                parts = record.split(',')
                if len(parts) != 11:
                    raise ValueError
                day = date.fromisoformat(parts[0])
                if day not in days:
                    continue
                if day in seen:
                    raise MarketDataError('duplicate daily session evidence')
                seen.add(day)
                _, closed = session_bounds(day)
                # Same-day K-lines can still be provisional at the closing bell.
                if now < closed+CLOSE_DELAY:
                    continue
                opened, close, high, low = map(exact_price,parts[1:5])
                if not low <= min(opened,close) <= max(opened,close) <= high:
                    raise MarketDataError('inconsistent daily OHLC')
                result.append({'instrument_id':instrument['id'], 'price':close,
                    'observed_at':closed, 'trading_day':day, 'kind':'close', 'quality':'valid',
                    'source':'eastmoney:unadjusted_daily','price_basis':'unadjusted_security'})
        except (ValueError, CalendarUnavailable) as exc:
            raise MarketDataError('invalid daily session or fields') from exc
        return result
