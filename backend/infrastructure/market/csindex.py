"""Bounded HTTPS adapter for CSI (中证指数) official index levels.

只有中证 93xxxx 与 H30xxx 系列（932000/931152/H30269/H30007）需要这条路径：
腾讯与新浪都不提供这些指数的日线，而中证指数官网提供官方点位。

实测要点（2026-11-24）：
- **必须用 GET**；同一 URL 用 POST 返回 ``{"code":"500"}``。
- 日期参数不带横线（``20260101``）最稳；带横线时返回行数不同。
- 返回 ``data[]``，字段为 tradeDate/indexCode/indexNameCn/open/high/low/close/...
- 一次最多约一年；这里按窗口切片，逐段请求，避免被截断。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import logging
from time import monotonic, sleep

import httpx

PERF_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockManager/2.0"
MIN_INTERVAL = 0.4
MAX_SPAN_DAYS = 360

logger = logging.getLogger(__name__)


class CsindexError(RuntimeError):
    """取数失败；消息可直接展示，不含凭据。"""


def _number(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CsindexError("invalid numeric field in csindex payload") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise CsindexError("non-finite numeric field in csindex payload")
    return number


class CsindexProvider:
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

    def _request(self, params: dict) -> list:
        if monotonic() < self._cooldown_until:
            raise CsindexError("provider cooling down after network failure")
        for attempt in range(2):
            if self._last_call is not None:
                sleep(max(0.0, self._min_interval - (monotonic() - self._last_call)))
            self._last_call = monotonic()
            headers = {"User-Agent": USER_AGENT, "Referer": "https://www.csindex.com.cn/"}
            try:
                if self._client is None:
                    with httpx.Client(timeout=12, follow_redirects=True) as client:
                        response = client.get(PERF_URL, params=params, headers=headers)
                else:
                    response = self._client.get(PERF_URL, params=params, headers=headers)
                if 500 <= response.status_code < 600 and attempt == 0:
                    sleep(0.5)
                    continue
                response.raise_for_status()
            except httpx.TransportError as exc:
                if attempt == 0:
                    sleep(0.5)
                    continue
                self._cooldown_until = monotonic() + 60
                raise CsindexError("provider transport unavailable") from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                self._cooldown_until = monotonic() + (900 if status in (401, 403) else 60)
                raise CsindexError(f"provider HTTP {status}") from exc
            try:
                payload = json.loads(response.content)
            except ValueError as exc:
                raise CsindexError("invalid csindex payload") from exc
            if str(payload.get("code")) != "200":
                raise CsindexError("csindex refused the request")
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise CsindexError("csindex returned no series")
            return rows
        raise CsindexError("provider request unavailable")

    def index_daily(self, csindex_code: str, *, days: int = 400,
                    end: date | None = None) -> list[dict]:
        """返回指数点位日线（升序、去重、校验 OHLC 与收盘）。"""
        code = str(csindex_code or "").strip()
        if not code:
            raise CsindexError("csindex code is required")
        span = max(1, min(MAX_SPAN_DAYS, int(days) + 30))
        last = end or self._clock().astimezone().date()
        start = last - timedelta(days=span)
        collected: dict[str, dict] = {}
        for chunk_start in _chunks(start, last, MAX_SPAN_DAYS):
            for row in self._request({
                "indexCode": code,
                "startDate": chunk_start.strftime("%Y%m%d"),
                "endDate": last.strftime("%Y%m%d"),
            }):
                bar = _parse_row(row, code)
                if bar:
                    collected[bar["date"]] = bar
        bars = [collected[key] for key in sorted(collected)]
        return bars[-max(1, int(days)):]

    def completed_index_daily(self, csindex_code: str, *, completed_on: str,
                              days: int = 400) -> list[dict]:
        bars = self.index_daily(csindex_code, days=days)
        kept = [bar for bar in bars if bar["date"] <= str(completed_on)[:10]]
        if not kept:
            raise CsindexError("csindex series ends before the requested trading day")
        return kept


def _chunks(start: date, end: date, size: int):
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=size), end)
        yield cursor
        cursor = stop + timedelta(days=1)


def _parse_row(row, code: str) -> dict | None:
    if not isinstance(row, dict):
        return None
    if str(row.get("indexCode", "")) != code:
        return None
    stamp = str(row.get("tradeDate", ""))[:10]
    if len(stamp) == 8 and stamp.isdigit():
        stamp = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
    try:
        date.fromisoformat(stamp)
    except ValueError:
        return None
    opened, high, low, close = (_number(row.get(key)) for key in ("open", "high", "low", "close"))
    if min(opened, high, low, close) <= 0:
        raise CsindexError("non-positive price in csindex payload")
    if not low <= min(opened, close) <= max(opened, close) <= high:
        raise CsindexError("inconsistent daily OHLC in csindex payload")
    volume = row.get("tradingVol")
    try:
        volume = float(volume) if volume not in (None, "") else 0.0
    except (TypeError, ValueError):
        volume = 0.0
    return {"date": stamp, "open": opened, "high": high, "low": low, "close": close,
            "volume": volume}


__all__ = ["CsindexError", "CsindexProvider", "PERF_URL"]
