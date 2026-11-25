"""策略行情装载：指数点位日线、ETF 日线，以及可追溯的缓存证据。

价格口径是本模块的核心约束（ISS-031）：

- ``index_level``：指数**点位**，用于 G2 等权基准与 ER60；
- ``etf_unadjusted``：ETF **未复权**人民币日线，用于 V9 突破/ATR/BuyMax/退出/加仓/因子，
  与旧项目 ``_fetch_eastmoney_kline(..., fqt="0")`` 的口径一致；
- ``etf_adjusted``：ETF **复权**日线，仅用于研究口径。

三类价格单位与含义都不同，绝不能混算：指数点位的 ATR 不得写入人民币成交，ETF 价格
也不得冒充指数点位。缓存表 ``strategy_bars`` 用 ``price_basis`` 分开存，读取时必须
显式指定口径。

ISS-034（历史证据）：
- 输入版本 ``input_version`` 必须覆盖**规范化 OHLCV 内容与来源**，价格修订必然产生
  新的输入版本（旧实现只哈希 ``last_date`` 与根数，不同价格会撞同一个版本）；
- ``strategy_bars`` 不是覆盖式 upsert，而是**追加式修订**：同一 (series_key, trade_date)
  可以有多条 revision，保存 ``effective_from``（交易日生效时点）、``observed_at``
  （本次获取时点）、``acquired_at``。原始行情与修订行情都可追溯，且历史回放可以按
  “当时可获得的信息”选择版本。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from dataclasses import asdict, is_dataclass
from zoneinfo import ZoneInfo
from backend.domain.strategy import config as rules
from backend.domain.strategy.calendars import CalendarCoverageError, market_clock
import json
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

#: 各口径的价格单位。任何把指数点位当人民币、或把 ETF 价当点位的地方都必须自行拒绝。
BASIS_UNITS = {
    INDEX_BASIS: '点',
    ETF_ADJUSTED: '元',
    ETF_UNADJUSTED: '元',
}

#: 可信策略判定的最小根数（与 ``min_bars_required`` 一致的下限保护）。
MIN_TRUSTWORTHY_BARS = 40


class SeriesError(RuntimeError):
    """行情不可用；``reason`` 可直接展示给用户。"""

    def __init__(self, reason: str, *, detail: str = '') -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def basis_unit(basis: str) -> str:
    return BASIS_UNITS.get(basis, '')


def _series_key(index_id: str, basis: str) -> str:
    code = etf_code_of(index_id) if basis != INDEX_BASIS else None
    return f'{basis}:{index_id}:{code or "-"}'


def canonical_payload(*, source: str, source_verified: bool, bars: list[Bar]) -> str:
    """规范化行情载荷：价格、量、来源全部进入哈希输入。

    任何一根 OHLCV 或来源发生变化，都会得到不同的输入版本——这正是 ISS-034 要求的
    “价格修订产生新的输入版本”。
    """
    body = {
        'source': source,
        'source_verified': bool(source_verified),
        'bars': [[bar.date, repr(float(bar.open)), repr(float(bar.high)),
                  repr(float(bar.low)), repr(float(bar.close)), repr(float(bar.volume))]
                 for bar in bars],
    }
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def payload_hash(*, index_id: str, basis: str, source: str,
                 source_verified: bool, bars: list[Bar]) -> str:
    canonical = canonical_payload(source=source, source_verified=source_verified, bars=bars)
    return sha256(f'{index_id}|{basis}|{canonical}'.encode()).hexdigest()[:32]


def _payload_hash(index_id: str, basis: str, bars: list[Bar], *,
                  source: str = '', source_verified: bool = True) -> str:
    """兼容旧调用点的薄封装（保留旧签名，但语义已覆盖完整 OHLCV）。"""
    return payload_hash(index_id=index_id, basis=basis, source=source,
                        source_verified=source_verified, bars=bars)


def config_fingerprint(index_id: str) -> str:
    """可核验的**配置快照哈希**，不得用策略版本号冒充。

    只把影响判定的规则参数纳入哈希：版本号变化但参数不变时哈希不变（说明规则未改），
    参数变化时哈希必然变化（说明必须视为新配置版本）。
    """
    cfg = get_config(normalize_index_id(index_id))
    fields = {'instrument': asdict(cfg), 'rules': {
        key: value for key, value in vars(rules).items()
        if key.isupper() and isinstance(value, (str, int, float, bool, tuple, list, dict))
        and key != 'INDEX_STRATEGY_VERSION'}}
    body = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      default=lambda value: asdict(value) if is_dataclass(value) else str(value))
    return 'cfg:' + sha256(body.encode()).hexdigest()[:24]


class StrategyData:
    """指数/ETF 日线装载与追加式缓存（网络请求不占用写事务）。"""

    def __init__(self, db: Database, *, tencent: TencentProvider | None = None,
                 csindex: CsindexProvider | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.db = db
        self.tencent = tencent if tencent is not None else TencentProvider()
        self.csindex = csindex if csindex is not None else CsindexProvider()
        self.clock = clock

    # -- 读取缓存 --------------------------------------------------------
    def cached(self, index_id: str, basis: str, *, as_of: str | None = None,
               acquired_before: str | None = None) -> tuple[list[Bar], dict]:
        """读取缓存。

        ``as_of``：只取该交易日（含）之前的 bar（不得使用未来行情）。
        ``acquired_before``：只取该时刻**之前已获取**的修订（历史回放只能用当时已知信息）。
        每个交易日取满足条件的**最新 revision**。
        """
        key = _series_key(index_id, basis)
        sql = ('SELECT trade_date,open,high,low,close,volume,source,source_verified,'
               'acquired_at,observed_at,revision,price_basis FROM strategy_bars '
               'WHERE series_key=?')
        args: list = [key]
        if as_of:
            sql += ' AND trade_date<=?'
            args.append(str(as_of)[:10])
        if acquired_before:
            sql += ' AND acquired_at<=?'
            args.append(str(acquired_before))
            sql += " AND acquired_at IS NOT NULL AND acquired_at!=''"
        sql += ' ORDER BY trade_date, revision'
        with self.db.read() as conn:
            records = rows(conn, sql, tuple(args))
        latest: dict[str, dict] = {}
        for record in records:
            latest[str(record['trade_date'])] = record
        ordered = [latest[day] for day in sorted(latest)]
        bars = [Bar(date=record['trade_date'], open=record['open'], high=record['high'],
                    low=record['low'], close=record['close'], volume=record['volume'] or 0.0)
                for record in ordered]
        last = ordered[-1] if ordered else None
        meta = {'source': last['source'] if last else '',
                'source_verified': bool(ordered) and all(r['source_verified'] for r in ordered),
                'provenance': [(r['trade_date'],r['source'],r['source_verified'],r['revision'],r['acquired_at']) for r in ordered],
                'acquired_at': last['acquired_at'] if last else '',
                'observed_at': last['observed_at'] if last else '',
                'revision': last['revision'] if last else 0,
                'price_basis': last['price_basis'] if last else basis}
        return bars, meta

    def _store(self, index_id: str, basis: str, bars: list[Bar], *, source: str,
               source_verified: bool = True) -> str:
        """追加式写入：内容未变则复用现有 revision，内容变了就写新 revision。

        绝不覆盖历史行，因此“原始行情”与“修订行情”都可追溯。
        返回本次写入后的 ``input_version``。
        """
        if not bars:
            return payload_hash(index_id=index_id, basis=basis, source=source,
                                source_verified=source_verified, bars=bars)
        key = _series_key(index_id, basis)
        now = self.clock()
        observed = utc(now)
        cfg = get_config(index_id)
        clock = market_clock(cfg.market if basis == INDEX_BASIS else cfg.clock_market)
        # Reject incomplete or impossible sessions before they enter the evidence store.
        try:
            effective = {bar.date: utc(clock.close_datetime(bar.date)) for bar in bars}
        except CalendarCoverageError as exc:
            raise SeriesError('日线交易日无法核实', detail=str(exc)) from exc
        if any(value > observed for value in effective.values()):
            raise SeriesError('日线尚未正式收盘')
        with self.db.transaction() as conn:
            existing = rows(
                conn,
                'SELECT trade_date,open,high,low,close,volume,revision,source,source_verified '
                'FROM strategy_bars WHERE series_key=? ORDER BY trade_date,revision',
                (key,))
            current: dict[str, dict] = {}
            for record in existing:
                current[str(record['trade_date'])] = record
            for bar in bars:
                prior = current.get(bar.date)
                if (prior is not None
                        and float(prior['open']) == float(bar.open)
                        and float(prior['high']) == float(bar.high)
                        and float(prior['low']) == float(bar.low)
                        and float(prior['close']) == float(bar.close)
                        and float(prior['volume'] or 0.0) == float(bar.volume)
                        and str(prior['source']) == str(source)
                        and bool(prior['source_verified']) == bool(source_verified)):
                    continue  # 同一份行情重复刷新：不产生新修订
                revision = int(prior['revision']) + 1 if prior is not None else 1
                conn.execute(
                    '''INSERT INTO strategy_bars
                       (series_key,trade_date,open,high,low,close,volume,price_basis,source,
                        source_verified,acquired_at,observed_at,revision,effective_from)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (key, bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume, basis,
                     source, int(source_verified), observed, observed, revision, effective[bar.date]))
        return payload_hash(index_id=index_id, basis=basis, source=source,
                            source_verified=source_verified, bars=bars)

    def decision_window(self, completed_on: str, market: str) -> tuple[str, str, bool]:
        """Day-only replay uses the end of that Shanghai day, never today's later inputs."""
        now = self.clock()
        try:
            latest = market_clock('CN').last_completed_session(now=now)
            requested = date.fromisoformat(completed_on[:10])
            if latest is None or requested.isoformat() > latest or not market_clock('CN').is_trading_day(requested.isoformat()):
                raise CalendarCoverageError('请求日期不是已完成的国内交易日')
            historic = requested < now.astimezone(ZoneInfo('Asia/Shanghai')).date()
            cutoff = min(now, datetime.combine(requested, time.max, ZoneInfo('Asia/Shanghai')))
            target = market_clock(market).last_completed_session(now=cutoff)
            if target is None:
                raise CalendarCoverageError('无法确定市场收盘交易日')
        except (CalendarCoverageError, ValueError) as exc:
            raise SeriesError('交易日历不可用', detail=str(exc)) from exc
        return target, utc(cutoff), historic

    # -- 指数点位 --------------------------------------------------------
    def index_series(self, index_id: str, *, completed_on: str,
                     allow_network: bool = True) -> IndexSeries:
        canonical = normalize_index_id(index_id)
        cfg = get_config(canonical)
        symbol = symbol_of(canonical)
        target, cutoff, historic = self.decision_window(completed_on, cfg.market)
        allow_network = allow_network and not historic
        bars, meta = self.cached(canonical, INDEX_BASIS, as_of=target, acquired_before=cutoff)
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
        source = 'csindex:' + (symbol.csindex_code or '') if not symbol.vendor_covered \
            else 'tencent:' + symbol.quote_code
        self._store(canonical, INDEX_BASIS, fresh, source=source,
                    source_verified=symbol.vendor_covered)
        ordered, meta = self.cached(canonical, INDEX_BASIS, as_of=target, acquired_before=utc(self.clock()))
        return self._index_view(cfg, canonical, ordered, meta)

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
            raw = self.csindex.completed_index_daily(symbol.csindex_code or '',
                                                     completed_on=completed_on, days=400)
        if len(raw) < MIN_TRUSTWORTHY_BARS:
            raise CsindexError('index series is too short to be trustworthy')
        return bars_from_columns(
            dates=[item['date'] for item in raw], opens=[item['open'] for item in raw],
            highs=[item['high'] for item in raw], lows=[item['low'] for item in raw],
            closes=[item['close'] for item in raw], volumes=[item.get('volume') or 0.0 for item in raw],
        )

    # -- ETF 日线 --------------------------------------------------------
    def etf_series(self, index_id: str, *, completed_on: str, basis: str = ETF_UNADJUSTED,
                   allow_network: bool = True) -> IndexSeries:
        """ETF 日线。**默认未复权**（与旧项目 ``fqt=0`` 一致），复权价必须显式请求。"""
        if basis not in (ETF_ADJUSTED, ETF_UNADJUSTED):
            raise SeriesError('ETF 口径必须是复权或未复权', detail=f'unknown_basis:{basis}')
        canonical = normalize_index_id(index_id)
        cfg = get_config(canonical)
        code = etf_code_of(canonical)
        if not code:
            raise SeriesError('该指数没有内置交易标的', detail='etf_mapping_missing')
        target, cutoff, historic = self.decision_window(completed_on, cfg.clock_market)
        allow_network = allow_network and not historic
        bars, meta = self.cached(canonical, basis, as_of=target, acquired_before=cutoff)
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
            if len(raw) < MIN_TRUSTWORTHY_BARS:
                raise TencentError('ETF series is too short to be trustworthy')
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
        source = f'tencent:{etf_quote_code(code)}:{basis}'
        self._store(canonical, basis, fresh, source=source, source_verified=True)
        ordered, meta = self.cached(canonical, basis, as_of=target, acquired_before=utc(self.clock()))
        return self._etf_view(cfg, canonical, code, ordered, meta, basis)

    @staticmethod
    def _etf_view(cfg, canonical: str, code: str, bars: list[Bar], meta: dict, basis: str,
                  *, warning: str = '') -> IndexSeries:
        # ETF 交易在场内 A 股，执行日历固定为 A 股；不能因为指数在海外就换成海外日历。
        return IndexSeries(
            index_id=canonical, name=cfg.etf_name or f'{cfg.name}({code})',
            market=cfg.clock_market, bars=tuple(bars),
            source=meta.get('source') or 'cache', is_proxy=False,
            proxy_for=canonical, proxy_code=code,
            source_verified=bool(meta.get('source_verified', True)), warning=warning,
        )

    # -- 版本标识 --------------------------------------------------------
    def input_version(self, index_id: str, *, completed_on: str,
                      basis: str = INDEX_BASIS) -> str:
        """该 (标的, 口径) 在 ``completed_on`` 时点的输入版本。

        版本覆盖规范化 OHLCV 与来源，因此价格修订必然产生新版本；不含 factor/peer 输入时
        只能用于单标的判定。组合判定请用 ``evaluation_version``。
        """
        canonical = normalize_index_id(index_id)
        cfg = get_config(canonical)
        target, cutoff, _ = self.decision_window(completed_on, cfg.market if basis == INDEX_BASIS else cfg.clock_market)
        bars, meta = self.cached(canonical, basis, as_of=target, acquired_before=cutoff)
        body = payload_hash(index_id=canonical, basis=basis, source=str(meta.get('source') or ''),
                            source_verified=bool(meta.get('source_verified', False)), bars=bars)
        return sha256((body + json.dumps(meta['provenance'], sort_keys=True)).encode()).hexdigest()[:32]

    def evaluation_version(self, index_id: str, *, completed_on: str,
                           basis: str, peer_versions: dict | None = None) -> str:
        """完整输入版本：主序列 + 同组因子输入 + 配置快照。

        这就是写入 ``strategy_signals.input_version`` 的值：任何一项输入变化（价格修订、
        同组标的行情修订、配置变化）都会产生新的信号版本，而不会复用旧证据。
        """
        canonical = normalize_index_id(index_id)
        own = self.input_version(canonical, completed_on=completed_on, basis=basis)
        peers = json.dumps(peer_versions or {}, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'))
        return sha256(f'{own}|{peers}|{config_fingerprint(canonical)}'.encode()).hexdigest()[:32]


def series_summary(series: Optional[IndexSeries]) -> dict:
    if series is None:
        return {'available': False}
    return {'available': True, 'source': series.source, 'bars': len(series.bars),
            'last_date': series.last_date, 'source_verified': series.source_verified,
            'warning': series.warning}


__all__ = [
    'BASIS_LABELS', 'BASIS_UNITS', 'ETF_ADJUSTED', 'ETF_UNADJUSTED', 'INDEX_BASIS',
    'MIN_TRUSTWORTHY_BARS', 'SeriesError', 'StrategyData', 'basis_unit',
    'canonical_payload', 'config_fingerprint', 'payload_hash', 'series_summary',
]
