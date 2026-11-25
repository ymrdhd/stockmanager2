"""Bounded HTTPS adapter for Tencent index / ETF daily series.

实测（2026-11-24）：
- A 股指数、深证成指、创业板指、中证白酒、海外 NDX/HSTECH 都能通过
  ``web.ifzq.gtimg.cn`` 取到日线；
- 中证 93xxxx 与 H30xxx 系列（932000/931152/H30269/H30007）该源无数据，
  必须走中证指数官网；
- 同一端点用 ``qfq`` 拿复权序列、用 ``kline/kline`` 拿**未复权**序列，
  两类价格口径必须分开记录，不能互相顶替。

本适配器只做"取数 + 结构校验 + 字段单位断言"，不做策略判断；任何越界、
重复日期、非正价格、OHLC 不自洽都直接抛错，由上层降级为"数据不可用"。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import logging
from time import monotonic, sleep
from typing import Any, Iterable

import httpx

from backend.application.market_calendar import (
    CalendarUnavailable, SHANGHAI, market_window, quote_time, session_bounds,
)
from backend.domain.strategy.symbols import (
    OVERSEAS_NAME_ASSERTIONS, IndexSymbol, etf_quote_code,
)
from backend.infrastructure.market.eastmoney import MarketDataError

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
RAW_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
US_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get"
QUOTE_URL = "https://qt.gtimg.cn/q="

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockManager/2.0"
MAX_BARS = 640
MIN_INTERVAL = 0.35

# Same settlement delay as the retired default adapter: a same-day bar can be
# provisional at the closing bell, so no confirmed close is read before it.
CLOSE_DELAY = timedelta(minutes=15)

logger = logging.getLogger(__name__)


class TencentError(MarketDataError):
    """取数失败；消息里不含任何凭据，可直接展示给用户。

    继承 ``MarketDataError`` 使刷新服务的既有降级路径（按"数据证据不可用"
    记录拒绝原因）无需改动即可覆盖腾讯源。
    """


def _as_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TencentError("invalid numeric field in provider payload") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise TencentError("non-finite numeric field in provider payload")
    return number


def _rows_of(node: dict) -> list:
    """同一端点在不同标的上会用 ``qfqday`` / ``day`` / ``klines`` 三种键。"""
    for key in ("qfqday", "day", "klines"):
        rows = node.get(key)
        if isinstance(rows, list) and rows:
            return rows
    return []


def _bars_from_rows(rows: Iterable[Any]) -> list[tuple[str, float, float, float, float, float]]:
    """腾讯字段顺序是 **日期, 开, 收, 高, 低, 量**（不是 OHLC）。"""
    out: list[tuple[str, float, float, float, float, float]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        stamp = str(row[0])[:10]
        try:
            date.fromisoformat(stamp)
        except ValueError:
            continue
        opened, close, high, low, volume = (_as_float(value) for value in row[1:6])
        if min(opened, close, high, low) <= 0:
            raise TencentError("non-positive price in provider payload")
        if not low <= min(opened, close) <= max(opened, close) <= high:
            raise TencentError("inconsistent daily OHLC in provider payload")
        out.append((stamp, opened, high, low, close, volume))
    return out


class TencentProvider:
    """指数点位日线 / ETF 日线。指数与 ETF 走同一域名，但键与口径不同。"""

    def __init__(self, client: httpx.Client | None = None, *, min_interval: float = MIN_INTERVAL,
                 now=None) -> None:
        self._client = client
        self._min_interval = min_interval
        self._last_call: float | None = None
        self._cooldown_until = 0.0
        self._now = now

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _clock(self) -> datetime:
        return self._now() if self._now else datetime.now()

    def _fetch(self, url: str) -> httpx.Response:
        """One bounded HTTPS GET: cooldown, single rate limit, one retry on 5xx/transport."""
        if monotonic() < self._cooldown_until:
            raise TencentError("provider cooling down after network failure")
        for attempt in range(2):
            if self._last_call is not None:
                sleep(max(0.0, self._min_interval - (monotonic() - self._last_call)))
            self._last_call = monotonic()
            try:
                headers = {"User-Agent": USER_AGENT, "Referer": "https://gu.qq.com/"}
                if self._client is None:
                    with httpx.Client(timeout=8, follow_redirects=False) as client:
                        response = client.get(url, headers=headers)
                else:
                    response = self._client.get(url, headers=headers)
                if 500 <= response.status_code < 600 and attempt == 0:
                    sleep(0.5)
                    continue
                response.raise_for_status()
            except httpx.TransportError as exc:
                if attempt == 0:
                    sleep(0.5)
                    continue
                self._cooldown_until = monotonic() + 60
                raise TencentError("provider transport unavailable") from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (401, 403, 429) or status >= 500:
                    self._cooldown_until = monotonic() + (900 if status in (401, 403) else 60)
                raise TencentError(f"provider HTTP {status}") from exc
            return response
        raise TencentError("provider request unavailable")

    def _request(self, url: str) -> dict:
        response = self._fetch(url)
        try:
            payload = json.loads(response.content)
            if payload.get("code") != 0:
                raise ValueError
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise TencentError("invalid or unavailable provider payload") from exc
        return data

    def _request_text(self, url: str) -> str:
        """即时报价接口返回 GBK 文本，不是 JSON；共用同一套限速/冷却。"""
        return self._fetch(url).content.decode("gbk", "replace")


    # -- 指数点位日线 ----------------------------------------------------
    def index_daily(self, symbol: IndexSymbol, *, count: int = 320) -> list[dict]:
        """返回指数**点位**日线（未复权指数点位，不是 ETF 价格）。"""
        code = symbol.quote_code
        if not code:
            raise TencentError("该指数在该行情源无标识，需改用中证指数官网")
        size = max(1, min(MAX_BARS, int(count)))
        endpoint = US_KLINE_URL if code.startswith("us") else KLINE_URL
        data = self._request(f"{endpoint}?param={code},day,,,{size},qfq")
        node = data.get(code)
        if not isinstance(node, dict):
            raise TencentError("provider returned no series for requested index")
        bars = _bars_from_rows(_rows_of(node))
        if not bars:
            raise TencentError("provider returned an empty index series")
        return [{"date": day, "open": opened, "high": high, "low": low, "close": close,
                 "volume": volume}
                for day, opened, high, low, close, volume in bars]

    def index_name(self, symbol: IndexSymbol) -> str:
        """指数的中文名（用于海外指数的名称断言，防止拿综合指数冒充）。"""
        code = symbol.quote_code
        if not code:
            raise TencentError("该指数在该行情源无标识")
        text = self._request_text(f"{QUOTE_URL}{code}")
        if "=" not in text:
            raise TencentError("provider returned no quote name")
        payload = text.split("=", 1)[1].strip().rstrip(";").strip('"')
        parts = payload.split("~")
        return parts[1] if len(parts) > 1 else ""

    def assert_index_identity(self, symbol: IndexSymbol) -> None:
        assertions = OVERSEAS_NAME_ASSERTIONS.get(symbol.index_id)
        if not assertions:
            return
        name = self.index_name(symbol)
        if not any(token.lower() in name.lower() for token in assertions):
            raise TencentError(
                f"行情源返回的名称“{name}”与 {symbol.index_id}（{symbol.name}）不符，已拒绝使用")

    # -- ETF 日线 --------------------------------------------------------
    def etf_daily(self, code: str, *, count: int = 320, adjusted: bool = True) -> list[dict]:
        """ETF 日线。``adjusted=True`` 为研究口径复权价，``False`` 为真实成交口径。"""
        quote = etf_quote_code(code)
        if not quote:
            raise TencentError("unsupported ETF code")
        size = max(1, min(MAX_BARS, int(count)))
        if adjusted:
            data = self._request(f"{KLINE_URL}?param={quote},day,,,{size},qfq")
        else:
            data = self._request(f"{RAW_KLINE_URL}?param={quote},day,,,{size}")
        node = data.get(quote)
        if not isinstance(node, dict):
            raise TencentError("provider returned no series for requested ETF")
        bars = _bars_from_rows(_rows_of(node))
        if not bars:
            raise TencentError("provider returned an empty ETF series")
        return [{"date": day, "open": opened, "high": high, "low": low, "close": close,
                 "volume": volume}
                for day, opened, high, low, close, volume in bars]

    def completed_index_daily(self, symbol: IndexSymbol, *, completed_on: str,
                              count: int = 320) -> list[dict]:
        """只保留 ``completed_on`` 及之前的指数日线（不把未收盘当日当已完成）。"""
        bars = self.index_daily(symbol, count=count)
        kept = [bar for bar in bars if bar["date"] <= str(completed_on)[:10]]
        if not kept:
            raise TencentError("provider series ends before the requested trading day")
        return kept


# -- 真实证券行情（沪深 ETF + 普通股票）----------------------------------

# 与退役的东财适配器同一套代码空间白名单：行情记录只能存真实证券价格，
# 指数点位、代理指数、复权研究价一律不得进入 quotes。
SECURITY_PREFIXES: dict[tuple[str, str], tuple[str, ...]] = {
    ("SH", "stock"): ("600", "601", "603", "605", "688", "689"),
    ("SZ", "stock"): ("000", "001", "002", "003", "300", "301"),
    ("SH", "etf"): ("510", "511", "512", "513", "515", "516", "517", "518",
                    "520", "560", "561", "562", "563", "588", "589"),
    ("SZ", "etf"): ("159",),
    ("BJ", "stock"): ("43", "83", "87", "88", "92"),
}


def security_quote_code(instrument: dict) -> str:
    """把主数据里的标的换成腾讯报价代码（sh510300 / sz159915 / bj430047）。"""
    if not isinstance(instrument, dict):
        raise TencentError("instrument metadata is missing")
    code, market, kind = instrument.get("code"), instrument.get("market"), instrument.get("kind")
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        raise TencentError("unsupported security code")
    if market not in ("SH", "SZ", "BJ") or kind not in ("stock", "etf"):
        raise TencentError("unsupported security market or kind")
    if not code.startswith(SECURITY_PREFIXES.get((market, kind), ())):
        raise TencentError("unsupported or ambiguous security market/kind; no index proxy")
    return f"{market.lower()}{code}"


def _exact_price(value: Any) -> Decimal:
    """真实成交价：有限、正、不超过一百万、最多四位小数（万分之一元）。"""
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise ValueError
        price = Decimal(value)
        if not price.is_finite() or not 0 < price <= 1000000:
            raise ValueError
        if price != price.quantize(Decimal("0.0001")):
            raise ValueError
    except (ValueError, InvalidOperation, ArithmeticError) as exc:
        raise TencentError("invalid or unsupported price precision") from exc
    return price


def _quote_fields(text: str, code: str) -> list[str]:
    """腾讯即时报价是 GBK 文本：v_sh510300="1~名称~510300~现价~昨收~...";"""
    if '="' not in text:
        raise TencentError("provider returned no quote for requested security")
    body = text.split('="', 1)[1].rstrip().rstrip(";").rstrip('"')
    parts = body.split("~")
    if len(parts) < 35:
        raise TencentError("provider quote payload is incomplete")
    if parts[2].strip() != code:
        raise TencentError("provider returned a different security")
    return parts


def _quote_stamp(parts: list[str]) -> datetime:
    stamp = parts[30].strip()
    if len(stamp) != 14 or not stamp.isdigit():
        raise TencentError("provider quote has no usable observation time")
    try:
        return datetime(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]),
                        int(stamp[8:10]), int(stamp[10:12]), int(stamp[12:14]),
                        tzinfo=SHANGHAI)
    except ValueError as exc:
        raise TencentError("provider quote timestamp is invalid") from exc


class TencentQuoteProvider(TencentProvider):
    """``QuoteProvider`` 实现：未复权真实成交价，复用父类的限速与冷却。

    ``spot`` 只按交易所日历判定口径：当日快照只有在收盘之后才能成为
    ``close``，盘中只产出 ``intraday``；非交易时段返回上一交易日的正式收盘。
    """

    name = "tencent"
    available = True

    def spot(self, instrument: dict, now: datetime) -> dict:
        code = security_quote_code(instrument)
        parts = _quote_fields(self._request_text(f"{QUOTE_URL}{code}"), code[2:])
        price = _exact_price(parts[3])
        previous = parts[4].strip()
        if previous not in ("", "0", "0.00", "0.000"):
            _exact_price(previous)
        observed = _quote_stamp(parts)
        try:
            if observed > now:
                raise TencentError("provider quote is later than the requested clock")
            window = market_window(now)
            stamp_day = observed.astimezone(SHANGHAI).date()
            if window.phase in ("trading", "break"):
                # 盘中快照只能是当日 intraday；隔夜快照不是当前会话的证据。
                if stamp_day != window.day:
                    raise TencentError("provider quote outside requested session")
                kind, day, effective = "intraday", stamp_day, observed
            elif window.phase == "after_close":
                if stamp_day != window.day:
                    raise TencentError("provider quote outside requested session")
                kind, day, effective = "close", stamp_day, session_bounds(stamp_day)[1]
            else:
                # 盘前 / 休市：快照价格就是上一交易日的正式收盘价。
                if stamp_day == window.day or not self._is_completed(stamp_day, now):
                    raise TencentError("provider quote outside requested session")
                kind, day, effective = "close", stamp_day, session_bounds(stamp_day)[1]
            quote_time(day, effective, kind)
        except CalendarUnavailable as exc:
            raise TencentError("exchange calendar unavailable for quote session") from exc
        return {"instrument_id": instrument["id"], "price": price, "observed_at": effective,
                "trading_day": day, "kind": kind, "quality": "valid",
                "source": "tencent:unadjusted_spot", "price_basis": "unadjusted_security"}

    @staticmethod
    def _is_completed(day: date, now: datetime) -> bool:
        try:
            if now < session_bounds(day)[1]:
                return False
        except CalendarUnavailable:
            return False
        return True

    def closes(self, instrument: dict, days: set[date], now: datetime) -> list[dict]:
        if not days:
            return []
        if (max(days) - min(days)).days > 45 or len(days) > 10:
            raise TencentError("daily request exceeds bounded window")
        code = security_quote_code(instrument)
        span = max(1, (max(days) - min(days)).days + 1)
        size = max(20, min(MAX_BARS, span * 2 + 15))
        data = self._request(f"{RAW_KLINE_URL}?param={code},day,,,{size}")
        node = data.get(code)
        if not isinstance(node, dict):
            raise TencentError("provider returned no series for requested security")
        bars = _bars_from_rows(_rows_of(node))
        if not bars:
            raise TencentError("provider returned an empty security series")
        result: list[dict] = []
        seen: set[date] = set()
        for stamp, _opened, _high, _low, close, _volume in bars:
            session = date.fromisoformat(stamp)
            if session not in days:
                continue
            if session in seen:
                raise TencentError("duplicate daily session evidence")
            seen.add(session)
            try:
                closed = session_bounds(session)[1]
            except CalendarUnavailable as exc:
                raise TencentError("invalid daily session for requested security") from exc
            # 收盘后的当日 K 线仍可能被源端修订，延迟窗口内不当已完成收盘。
            if now < closed + CLOSE_DELAY:
                continue
            result.append({"instrument_id": instrument["id"], "price": _exact_price(str(close)),
                           "observed_at": closed, "trading_day": session, "kind": "close",
                           "quality": "valid", "source": "tencent:unadjusted_daily",
                           "price_basis": "unadjusted_security"})
        return sorted(result, key=lambda item: item["trading_day"])


__all__ = ["CLOSE_DELAY", "KLINE_URL", "RAW_KLINE_URL", "SECURITY_PREFIXES", "TencentError",
           "TencentProvider", "TencentQuoteProvider", "US_KLINE_URL", "security_quote_code"]
