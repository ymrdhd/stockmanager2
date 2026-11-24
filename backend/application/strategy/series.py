"""策略行情装载：指数点位日线、ETF 日线，以及可追溯的缓存证据。

价格口径是本模块的核心约束：

- ``index_level``：指数**点位**（用于 V9 突破信号与 G2 等权基准）；
- ``etf_adjusted``：ETF **复权**日线（策略研究口径）；
- ``etf_unadjusted``：ETF **未复权**日线（真实市值/成交口径）。

三类价格单位与含义都不同，缓存表 ``strategy_bars`` 用 ``price_basis`` 分开存，
读取时必须指定口径，禁止"取到就用"。网络失败一律降级为
``DATA_UNAVAILABLE`` 并带原因，绝不返回编造的行情。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import logging
from hashlib import sha256
from typing import Optional

from backend.application.trading.service import rows, utc
from backend.domain.strategy.config import get_config
from backend.domain.strategy.market_data import Bar, IndexSeries, bars_from_columns
from backend.domain.strategy.symbols import (
    IndexSymbol, etf_code_of, etf_quote_code, normalize_index_id, symbol_of,
)
from backend.infrastructure.database import Database
from backend.infrastructure.market.csindex import CsindexError, CsindexProvider
from backend.infrastructure.market.tencent import TencentError, TencentProvider

logger = logging.getLogger(__name__)

INDEX_BASIS = 'index_level'
ETF_ADJUSTED = 'etf_adjusted'
ETF_UNADJUSTED = 'etf_unadjusted'

BASIS_LABELS = {
    INDEX_BASIS: '指数点位',
    ETF_ADJUSTED: 'ETF 复权价',
    ETF_UNADJUSTED: 'ETF 未复权价',
}


class SeriesError(RuntimeError):
    """行情不可用；``reason`` 可直接展示给用户。"""

    def __init__(self, reason: str, *, detail: str = '') -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _series_key(index_id: str, basis: str) -> str:
    code = etf_code_of(index_id) if basis != INDEX_BASIS else None
    return f'{basis}:{index_id}:{code or "-"}'


def _payload_hash(index_id: str, basis: str, bars: list[Bar]) -> str:
    last = bars[-1].date if bars else ''
    return sha256(f'{index_id}|{basis}|{last}|{len(bars)}'.encode()).hexdigest()[:16]


class StrategyData:
    """指数/ETF 日线装载与缓存（网络请求不占用写事务）。"""

    def __init__(self, db: Database, *, tencent: TencentProvider | None = None,
                 csindex: CsindexProvider | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.db = db
        self.tencent = tencent if tencent is not None else TencentProvider()
        self.csindex = csindex if csindex is not None else CsindexProvider()
        self.clock = clock

    # -- 读取缓存 --------------------------------------------------------
    def cached(self, index_id: str, basis: str) -> tuple[list[Bar], dict]:
        key = _series_key(index_id, basis)
        with self.db.read() as conn:
            records = rows(conn, 'SELECT * FROM strategy_bars WHERE series_key=? ORDER BY trade_date',
                           (key,))
        bars = [Bar(date=record['trade_date'], open=record['open'], high=record['high'],
                    low=record['low'], close=record['close'], volume=record['volume'] or 0.0)
                for record in records]
        meta = {'source': records[-1]['source'] if records else '',
                'source_verified': bool(records[-1]['source_verified']) if records else False,
                'acquired_at': records[-1]['acquired_at'] if records else '',
                'price_basis': records[-1]['price_basis'] if records else basis}
        return bars, meta

    def _store(self, index_id: str, basis: str, bars: list[Bar], *, source: str,
               source_verified: bool = True) -> None:
        if not bars:
            return
        key = _series_key(index_id, basis)
        acquired = utc(self.clock())
        with self.db.transaction() as conn:
            for bar in bars:
                conn.execute(
                    '''INSERT INTO strategy_bars
                       (series_key,trade_date,open,high,low,close,volume,price_basis,source,
                        source_verified,acquired_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(series_key,trade_date) DO UPDATE SET
                        open=excluded.open, high=excluded.high, low=excluded.low,
                        close=excluded.close, volume=excluded.volume,
                        source=excluded.source, source_verified=excluded.source_verified,
                        acquired_at=excluded.acquired_at''',
                    (key, bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume, basis,
                     source, int(source_verified), acquired))

    # -- 指数点位 --------------------------------------------------------
    def index_series(self, index_id: str, *, completed_on: str,
                     allow_network: bool = True) -> IndexSeries:
        canonical = normalize_index_id(index_id)
        cfg = get_config(canonical)
        symbol = symbol_of(canonical)
        target = str(completed_on)[:10]
        bars, meta = self.cached(canonical, INDEX_BASIS)
        kept = [bar for bar in bars if bar.date <= target]
        if kept and kept[-1].date >= target:
            return self._index_view(cfg, canonical, kept, meta)
        if not allow_network:
            if kept:
                return self._index_view(cfg, canonical, kept, meta, warning='本地缓存未覆盖最新已完成交易日')
            raise SeriesError('本地无缓存且已禁止联网', detail='index_cache_missing')
        try:
            fresh = self._fetch_index(symbol, target)
        except (TencentError, CsindexError) as exc:
            if kept:
                return self._index_view(cfg, canonical, kept, meta,
                                        warning=f'最新行情获取失败（{exc}），展示缓存数据')
            raise SeriesError('指数行情获取失败', detail=str(exc)) from exc
        self._store(canonical, INDEX_BASIS, fresh,
                    source=('csindex:' + (symbol.csindex_code or '')) if not symbol.vendor_covered
                    else ('tencent:' + symbol.quote_code))
        merged = {bar.date: bar for bar in kept}
        merged.update({bar.date: bar for bar in fresh})
        ordered = [merged[key] for key in sorted(merged)]
        return self._index_view(cfg, canonical, ordered,
                               {'source': ('csindex' if not symbol.vendor_covered else 'tencent'),
                                'source_verified': symbol.vendor_covered,
                                'acquired_at': utc(self.clock()), 'price_basis': INDEX_BASIS})

    @staticmethod
    def _index_view(cfg, canonical: str, bars: list[Bar], meta: dict,
                    *, warning: str = '') -> IndexSeries:
        return IndexSeries(
            index_id=canonical, name=cfg.name, market=cfg.market, bars=tuple(bars),
            source=meta.get('source') or 'cache', is_proxy=False, proxy_for=None,
            proxy_code=None, source_verified=bool(meta.get('source_verified', True)),
            warning=warning,
        )

    def _fetch_index(self, symbol: IndexSymbol, completed_on: str) -> list[Bar]:
        if symbol.vendor_covered:
            raw = self.tencent.completed_index_daily(symbol, completed_on=completed_on, count=320)
            if symbol.index_id in ('NDX', 'HSTECH'):
                self.tencent.assert_index_identity(symbol)
        else:
            raw = self.csindex.completed_index_daily(symbol.csindex_code or '', completed_on=completed_on,
                                                     days=400)
        if len(raw) < 40:
            raise CsindexError('index series is too short to be trustworthy')
        return bars_from_columns(
            dates=[item['date'] for item in raw], opens=[item['open'] for item in raw],
            highs=[item['high'] for item in raw], lows=[item['low'] for item in raw],
            closes=[item['close'] for item in raw], volumes=[item.get('volume') or 0.0 for item in raw],
        )

    # -- ETF 日线 --------------------------------------------------------
    def etf_series(self, index_id: str, *, completed_on: str, basis: str = ETF_ADJUSTED,
                   allow_network: bool = True) -> IndexSeries:
        canonical = normalize_index_id(index_id)
        cfg = get_config(canonical)
        code = etf_code_of(canonical)
        if not code:
            raise SeriesError('该指数没有内置交易标的', detail='etf_mapping_missing')
        target = str(completed_on)[:10]
        bars, meta = self.cached(canonical, basis)
        kept = [bar for bar in bars if bar.date <= target]
        if kept and kept[-1].date >= target:
            return self._etf_view(cfg, canonical, code, kept, meta, basis)
        if not allow_network:
            if kept:
                return self._etf_view(cfg, canonical, code, kept, meta, basis,
                                      warning='本地缓存未覆盖最新已完成交易日')
            raise SeriesError('本地无 ETF 缓存且已禁止联网', detail='etf_cache_missing')
        try:
            raw = self.tencent.etf_daily(code, count=320, adjusted=(basis == ETF_ADJUSTED))
            raw = [item for item in raw if item['date'] <= target]
            if not raw:
                raise TencentError('ETF series ends before the requested trading day')
        except TencentError as exc:
            if kept:
                return self._etf_view(cfg, canonical, code, kept, meta, basis,
                                      warning=f'ETF 行情获取失败（{exc}），展示缓存数据')
            raise SeriesError('ETF 行情获取失败', detail=str(exc)) from exc
        fresh = bars_from_columns(
            dates=[item['date'] for item in raw], opens=[item['open'] for item in raw],
            highs=[item['high'] for item in raw], lows=[item['low'] for item in raw],
            closes=[item['close'] for item in raw], volumes=[item.get('volume') or 0.0 for item in raw],
        )
        self._store(canonical, basis, fresh, source=f'tencent:{etf_quote_code(code)}')
        merged = {bar.date: bar for bar in kept}
        merged.update({bar.date: bar for bar in fresh})
        ordered = [merged[key] for key in sorted(merged)]
        return self._etf_view(cfg, canonical, code, ordered,
                              {'source': f'tencent:{etf_quote_code(code)}', 'source_verified': True,
                               'acquired_at': utc(self.clock()), 'price_basis': basis}, basis)

    @staticmethod
    def _etf_view(cfg, canonical: str, code: str, bars: list[Bar], meta: dict, basis: str,
                  *, warning: str = '') -> IndexSeries:
        return IndexSeries(
            index_id=canonical, name=cfg.etf_name or f'{cfg.name}({code})',
            market=cfg.clock_market, bars=tuple(bars),
            source=meta.get('source') or 'cache', is_proxy=False,
            proxy_for=canonical, proxy_code=code,
            source_verified=bool(meta.get('source_verified', True)), warning=warning,
        )

    # -- 版本标识 --------------------------------------------------------
    def input_version(self, index_id: str, *, completed_on: str) -> str:
        canonical = normalize_index_id(index_id)
        bars, meta = self.cached(canonical, INDEX_BASIS)
        kept = [bar for bar in bars if bar.date <= str(completed_on)[:10]]
        return _payload_hash(canonical, INDEX_BASIS, kept)


def series_summary(series: Optional[IndexSeries]) -> dict:
    if series is None:
        return {'available': False}
    return {'available': True, 'source': series.source, 'bars': len(series.bars),
            'last_date': series.last_date, 'source_verified': series.source_verified,
            'warning': series.warning}


__all__ = [
    'BASIS_LABELS', 'ETF_ADJUSTED', 'ETF_UNADJUSTED', 'INDEX_BASIS',
    'SeriesError', 'StrategyData', 'series_summary',
]
