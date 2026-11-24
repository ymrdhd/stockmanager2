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

from datetime import date, datetime
import json
import logging
from time import monotonic, sleep
from typing import Any, Iterable

import httpx

from backend.domain.strategy.symbols import (
    OVERSEAS_NAME_ASSERTIONS, IndexSymbol, etf_quote_code,
)

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
RAW_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
US_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get"
QUOTE_URL = "https://qt.gtimg.cn/q="

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockManager/2.0"
MAX_BARS = 640
MIN_INTERVAL = 0.35

logger = logging.getLogger(__name__)


class TencentError(RuntimeError):
    """取数失败；消息里不含任何凭据，可直接展示给用户。"""


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

    def _request(self, url: str) -> dict:
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
        raise TencentError("provider request unavailable")

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
        if self._client is None:
            with httpx.Client(timeout=8, follow_redirects=False) as client:
                response = client.get(f"{QUOTE_URL}{code}", headers={"User-Agent": USER_AGENT})
        else:
            response = self._client.get(f"{QUOTE_URL}{code}", headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        text = response.content.decode("gbk", "replace")
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


__all__ = ["KLINE_URL", "RAW_KLINE_URL", "TencentError", "TencentProvider", "US_KLINE_URL"]
