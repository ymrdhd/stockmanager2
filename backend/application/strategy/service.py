"""V9/G2 策略应用服务：观察池、持仓决策、历史回放。

调用链（与旧 ``service.evaluate_index`` 等价，但不读写旧 kv 账本）：

1. **按配置选择价格口径**（ISS-031）：
   - ``cfg.uses_etf_underlying`` 为真（默认）→ V9 的突破、ATR、BuyMax、初始/跟踪止损、
     时间退出、加仓、因子全部用 **ETF 未复权人民币日线**（旧项目 ``fqt=0`` 口径）；
   - G2 等权基准 / ER60 / 市场状态始终用 **14 只指数的真实点位**。
   两者单位不同（元 vs 点），绝不能混算；指数点位的 ATR 不得写进人民币成交。
2. 有真实持仓 → 用已完成日线逐日推进 ``advance_position``，**在隔离副本上推进**，
   模型止损/退出只形成建议，绝不把 ``exit_filled`` 写回真实执行状态、也不凭模型退出
   释放组合槽位或启动冷却（ISS-032 §5.3）。
3. 有待执行计划 → 用下一交易日开盘价 ``evaluate_fill``。
4. 无持仓 → ``evaluate_entry`` 判定信号，再按现金/组合槽位决定是否保留计划。
5. 汇总 G2 市场状态、因子、组合因子、盈利加仓、NDX 辅助仓、冷却。

权威事实永远是账本：``trade_executions`` / ``trade_lots`` / ``lot_allocations``。
``strategy_positions`` 只是**可从账本重建**的衍生证据，所以本服务会主动从真实批次
重建缺失的策略状态（``sync_from_custody``），而不是反过来假定策略表就是持仓。

所有"建议"都必须带价格口径与单位；任何一环缺数据就降级为 ``DATA_UNAVAILABLE``
并写清原因，绝不编造行情或把模型价格当成券商成交。
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time
from hashlib import sha256
import json
import logging
import math
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

from backend.application.market_calendar import CalendarUnavailable, market_window
from backend.application.portfolio import PortfolioService
from backend.application.strategy.decision import active_decision, scoped
from backend.application.strategy.series import (
    ETF_UNADJUSTED, INDEX_BASIS, SeriesError, StrategyData, basis_unit,
    config_fingerprint, series_summary,
)
from backend.application.strategy.store import CapacityRefused, StrategyStore
from backend.application.trading.service import SCALE, rows, utc, verified_trade_signal, require_account
from backend.domain.strategy.calendars import CalendarCoverageError, market_clock
from backend.domain.strategy.config import (
    INDEX_STRATEGY_VERSION, PORTFOLIO_MAX_POSITIONS, G2_ENABLED, get_config, NDX_AUX_INITIAL_RATIO, NDX_AUX_CASH_BUFFER,
)
from backend.domain.strategy.entry import (
    STATUS_DATA_UNAVAILABLE, STATUS_SIGNAL, FILL_CANCELLED, FILL_FILLED,
    evaluate_entry, evaluate_fill,
)
from backend.domain.strategy.exit import (
    ACTION_CLOSED, ACTION_HOLD, PositionState, advance_position, cooldown_required_days,
    cooldown_until_date, initial_stop_price, trail_trigger_price,
)
from backend.domain.strategy.factor_combo import evaluate_combo
from backend.domain.strategy.factors import evaluate_factor
from backend.domain.strategy.indicators import compute_atr14
from backend.domain.strategy.market_data import min_bars_required, validate_series
from backend.domain.strategy.regime import REGIME_SHORT, RegimeSnapshot, market_regime
from backend.domain.strategy.symbols import (
    configured_index_ids, etf_code_of, normalize_index_id, peers_of,
)
from backend.domain.strategy.v9_rules import (
    base_position_amount, evaluate_pyramid, ma_snapshot, evaluate_ndx_aux,
)
from backend.infrastructure.database import Database

logger = logging.getLogger(__name__)

#: 策略证据的两种来源：账本可核实批次 / 旧数据或缺失证据（待人工核对）。
SOURCE_LEDGER = 'ledger_verified'
SOURCE_UNVERIFIED = 'legacy_unverified'

#: 模型推进时可以向真实证据合并的字段：这些都是**市场观测衍生**状态，
#: 不是执行结果。``exit_filled`` / ``closed_date`` / ``last_exit_*`` 一律不得由此写入。
OBSERVED_FIELDS = (
    'highest_price', 'current_atr', 'trail_trigger', 'stage', 'stop_price',
    'holding_days', 'renewals', 'e2a_extended_days', 'below_ma20_streak', 'extended',
    'pending_stop', 'pending_stop_effective_date',
    'pending_action', 'pending_action_date', 'last_trade_date', 'exit_after_bars',
)


class StrategyCalendarUnavailable(RuntimeError):
    """交易日历不可用：不得生成正式信号，也不得编造下一交易日。"""

    def __init__(self, reason: str, *, detail: str = '') -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class StrategyService:
    """一个账户视角下的 V9/G2 观察池与持仓决策。"""

    def __init__(self, db: Database, *, data: StrategyData | None = None,
                 store: StrategyStore | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC), near_stop_bps: int = 200) -> None:
        if not 0 <= near_stop_bps <= 1000: raise ValueError("near_stop_bps must be 0..1000")
        self.near_stop_bps = near_stop_bps
        self.db = db
        self.clock = clock
        self.data = data if data is not None else StrategyData(db, clock=clock)
        self.store = store if store is not None else StrategyStore(db, clock=clock)
        self.portfolio = PortfolioService(db, clock)

    # -- 公共入口 --------------------------------------------------------
    def regime(self, *, as_of: str, allow_network: bool = True) -> RegimeSnapshot:
        """G2：用 14 只指数的**真实点位**日线构造等权基准与 ER60。"""
        series_map = {}
        warnings = []
        for index_id in configured_index_ids():
            try:
                series = self.data.index_series(index_id, completed_on=as_of, allow_network=allow_network)
                if not self._fresh(series, as_of):
                    warnings.append(f'{index_id}:行情过期或来源未核实')
                    continue
                series_map[index_id] = series
            except SeriesError as exc:
                warnings.append(f'{index_id}:{exc.reason}')
        snapshot = market_regime(series_map, as_of=as_of)
        if warnings and not snapshot.warning:
            snapshot = RegimeSnapshot(
                state=snapshot.state, er60=snapshot.er60, change_pct=snapshot.change_pct,
                as_of=snapshot.as_of, components=snapshot.components, points=snapshot.points,
                warning='；'.join(warnings[:6]))
        return snapshot

    @scoped
    def pool(self, account_id: str, *, as_of: str | None = None,
             allow_network: bool = True, persist: bool = False) -> dict:
        """策略观察池：每只指数的状态行 + 组合层汇总。

        ``persist=False``（默认，供 GET 只读查询）只计算并返回，不写任何正式证据；
        ``persist=True`` 只允许由明确的 POST 刷新入口或受控后台任务使用。
        """
        try:
            day = self._as_of(as_of)
        except (CalendarUnavailable, CalendarCoverageError, StrategyCalendarUnavailable) as exc:
            return self._calendar_unavailable(account_id, exc)
        persist = persist and not self._historical(day)
        allow_network = allow_network and persist
        regime = self.regime(as_of=day, allow_network=allow_network)
        equity, cash, quality, reasons = self._equity(account_id, day)
        base_amount = base_position_amount(equity)
        core_used, core_note = self.core_positions_used(account_id, day)
        if self._historical(day):
            core_note = '历史账本版本不完整，真实槽位无法核验'
        items = []
        deferred = {}
        for index_id in configured_index_ids():
            items.append(self.evaluate(index_id, account_id=account_id, as_of=day,
                                       regime=regime, equity=equity, cash=cash,
                                       core_used=core_used, allow_network=allow_network,
                                       persist=persist, deferred_entries=deferred))
        # Check the aggregate budget before writing any of this refresh's new plans.
        # No arbitrary ordering wins when simultaneous opportunities exceed cash/slots.
        candidates = [item for item in items if item['status'] in (STATUS_SIGNAL, 'PLAN_PENDING')]
        reserved_cash = sum(entry_amount(equity, get_config(item['index_id'])) or 0 for item in candidates)
        reserved_slots = sum(get_config(item['index_id']).counts_against_core_slots for item in candidates)
        competition = bool(candidates and (cash is None or reserved_cash > cash or
                            core_used + reserved_slots > PORTFOLIO_MAX_POSITIONS))
        for item in candidates:
            if competition:
                item['technical_status'] = item['status']
                item['status'] = 'PORTFOLIO_CAPACITY_BLOCKED'
                item['tradable'] = False
                item['tradable_block_reason'] = 'simultaneous_cash_or_slot_competition'
                item['reason_codes'].append('simultaneous_cash_or_slot_competition')
                item['detail'] += '；候选计划合计超过账户现金或核心槽位，不自动选择优先级'
                if persist and item['index_id'] not in deferred:
                    self.store.clear_plan(account_id, item['index_id'], reason='组合容量不足，旧计划不可执行')
            elif item['index_id'] in deferred:
                try:
                    signal = deferred[item['index_id']]()
                except CapacityRefused as exc:
                    item['status'] = 'PORTFOLIO_CAPACITY_BLOCKED'
                    item['tradable'] = False
                    item['tradable_block_reason'] = str(exc)
                    item['reason_codes'].append('transaction_capacity_refused')
                    continue
                if signal is None:
                    item['status'] = 'SIGNAL_CONSUMED'
                    item['reason_codes'].append('signal_already_consumed')
                else:
                    item['signal_id'] = signal['signal_id']
                    item['input_version'] = signal['input_version']
                    item['tradable'] = item['price_basis'] == ETF_UNADJUSTED and bool(item['etf_code'])
                    item['capacity_kind'] = 'plan_capacity_checked_not_cash_escrow'
        # T5 advice is not a reservation. When refreshing a whole pool, record
        # it only after the core plans have actually claimed their capacity.
        if persist:
            ndx=next((item for item in items if item['index_id']=='NDX'),None)
            if ndx and (ndx.get('ndx_aux') or {}).get('actionable'):
                try:
                    ndx_series=self.data.etf_series('NDX',completed_on=day,basis=ETF_UNADJUSTED,allow_network=False)
                    ndx['ndx_aux']=self._ndx_advice(account_id,day,get_config('NDX'),ndx_series,[],persist=True,
                        competing_entry=ndx['status'] in (STATUS_SIGNAL,'PLAN_PENDING','AWAITING_CONFIRMATION'))
                except SeriesError:
                    ndx['ndx_aux']={'status':'DATA_UNAVAILABLE','detail':'记录建议时 ETF 行情无法复核','tradable':False,'actionable':False}
        return {
            'as_of': day, **self._time_payload(day), 'account_id': account_id,
            'strategy_version': INDEX_STRATEGY_VERSION,
            'regime': self._regime_payload(regime),
            'equity_fen': equity, 'cash_fen': cash, 'equity_quality': quality,
            'equity_reasons': reasons,
            'base_amount_fen': base_amount,
            'core_positions': None if self._historical(day) else core_used, 'max_positions': PORTFOLIO_MAX_POSITIONS,
            'items': items,
            'capacity': {'candidate_cash_fen': reserved_cash, 'candidate_core_slots': reserved_slots, 'competition': competition},
            'read_only': not persist,
            'data': {
                'allow_network': allow_network,
                'v9_price_basis': ETF_UNADJUSTED,
                'g2_price_basis': INDEX_BASIS,
                'core_note': core_note,
                'note': ('V9 用 ETF 未复权人民币日线，G2 用指数点位；两者单位不同，不得混算；'
                         '真实市值与成交用未复权价。只读查询不写正式信号。'),
            },
        }

    @scoped
    def evaluate(self, index_id: str, *, account_id: str, as_of: str,
                 regime: RegimeSnapshot | None = None, equity: int | None = None,
                 cash: int | None = None, core_used: int = 0,
                 allow_network: bool = True, persist: bool = False, deferred_entries: dict | None = None) -> dict:
        canonical = normalize_index_id(index_id)
        cfg = get_config(canonical)
        try:
            as_of = self._as_of(as_of)
        except (CalendarUnavailable, CalendarCoverageError, StrategyCalendarUnavailable) as exc:
            return self._unavailable(canonical, cfg, '交易日历不可用', SeriesError(str(exc)), ETF_UNADJUSTED if cfg.uses_etf_underlying else INDEX_BASIS)
        historical = self._historical(as_of)
        with self.db.read() as conn:
            evidence_revision = conn.execute("SELECT ledger_revision FROM ledger_state").fetchone()[0]
        persist = persist and not historical
        allow_network = allow_network and persist
        clock = market_clock(cfg.clock_market)
        warnings: list[str] = []

        # ---- 价格口径：V9 用 ETF 未复权，G2/regime 用指数点位 -----------------
        if cfg.uses_etf_underlying:
            basis = ETF_UNADJUSTED
            try:
                series = self.data.etf_series(canonical, completed_on=as_of, basis=basis,
                                              allow_network=allow_network)
            except SeriesError as exc:
                return self._unavailable(canonical, cfg, 'ETF 未复权日线不可用', exc, basis)
        else:
            basis = INDEX_BASIS
            try:
                series = self.data.index_series(canonical, completed_on=as_of,
                                                allow_network=allow_network)
            except SeriesError as exc:
                return self._unavailable(canonical, cfg, '指数点位行情不可用', exc, basis)
        unit = basis_unit(basis)
        tick = 10.0 ** -int(cfg.price_digits) if cfg.price_digits else 0.001

        if series.warning:
            warnings.append(series.warning)
        if not series.source_verified:
            warnings.append('行情来源未核实，不得视为已核实的正式信号')
        try:
            validation = validate_series(series, breakout_window=cfg.breakout_window, as_of=as_of)
            if not validation["ok"]:
                raise ValueError("；".join(validation["reasons"]))
            if len(series.bars) < min_bars_required(cfg.breakout_window):
                raise ValueError(f'样本不足：需 {min_bars_required(cfg.breakout_window)} 根，'
                                 f'实得 {len(series.bars)} 根')
        except ValueError as exc:
            exc2 = SeriesError('行情质量不足以判定', detail=str(exc))
            return self._unavailable(canonical, cfg, '行情质量不足以判定', exc2, basis)

        regime = regime or self.regime(as_of=as_of, allow_network=allow_network)
        fresh = self._fresh(series, as_of)
        safe = fresh and (regime.known or not G2_ENABLED)
        if not safe:
            warnings.append('行情或 G2 证据不足，仅供参考，不推进执行状态')
        if equity is None or cash is None:
            equity, cash, _quality, _reasons = self._equity(account_id, as_of)

        # ---- 真实持仓：账本优先；缺策略证据就从真实批次重建 -------------------
        custody = self._custody(account_id, as_of)
        all_held = [lot for lot in custody if self._lot_index(lot) == canonical]
        held = [lot for lot in all_held if lot['strategy_id'] == 'INDEX_STRATEGY_ATR']
        position, evidence = self._position_for(account_id, canonical, cfg, held, clock,
                                                as_of, warnings)
        if historical:
            position = None
            evidence = {'status': 'historical_incomplete', 'reason_codes': ['historical_configuration_not_versioned', 'ledger_corrections_not_bitemporal']}

        # ---- 模型推进：隔离副本，只产建议 ------------------------------------
        model_decision = None
        model_exit = None
        if position is not None and position.execution_source == SOURCE_LEDGER and position.entry_executed and not position.exit_filled and safe and held and not historical:
            simulated, model_decision = self._simulate(position, series, cfg, clock, warnings)
            model_exit = self._model_exit_payload(simulated, model_decision)
            position = self._merge_observed(position, simulated)
            if persist and position.execution_source == SOURCE_LEDGER:
                self.store.save_position(account_id, position)

        fill = None
        plan_row = None if historical else self.store.active_plan(account_id, canonical)
        if plan_row:
            proof = self.store.signal_by_id(plan_row['signal_id']) if plan_row['signal_id'] else None
            if not proof or proof['status'] != STATUS_SIGNAL or proof['account_id'] != account_id:
                plan_row = None
        if plan_row:
            next_session = clock.next_trading_day(plan_row['signal_date'])
            today = self._decision_time(as_of).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
            if next_session is not None and today > next_session:
                if persist and safe:
                    self.store.clear_plan(account_id, canonical, reason='计划已超过下一有效交易日')
                warnings.append('旧买入计划已过期，不得用于新成交')
                plan_row = None
        if plan_row and self._plan_used(account_id, plan_row, as_of):
            if persist and safe:
                self.store.clear_plan(account_id, canonical, reason='真实成交已使用该信号，旧计划不再可执行')
            plan_row = None
        if safe and plan_row is not None and not all_held and not (position and position.is_open):
            fill = self._evaluate_pending_plan(account_id, canonical, cfg, series, plan_row,
                                               clock, warnings, persist)

        cooldown = ({'active': None, 'until': None, 'remaining': None, 'note': '历史真实退出证据不可核实'} if historical else self._cooldown_state(account_id, canonical, as_of, clock, regime.state))
        has_open = bool(all_held)
        live_qty = sum(int(lot['remaining'] or 0) for lot in all_held)
        if live_qty > 0:
            has_open = True

        peers: dict = {}
        peer_versions: dict = {}
        if cfg.factor_key:
            peers, peer_versions = self._peer_series(canonical, basis, as_of,
                                                     allow_network)
        factor = evaluate_factor(cfg, series, peers)
        combo = evaluate_combo(cfg, series)
        plan = evaluate_entry(
            cfg, series, as_of=as_of, factor=factor, combo=combo,
            cooldown_active=bool(cooldown['active']), cooldown_note=cooldown['note'],
            has_open_position=has_open, has_pending_plan=bool(plan_row) and not has_open,
            open_position_count=core_used,
            max_positions=PORTFOLIO_MAX_POSITIONS if cfg.counts_against_core_slots else 0,
            tick=tick,
        )
        status = plan.status
        detail = plan.detail
        reason_codes = [status.lower()]
        if status == STATUS_SIGNAL and historical:
            status = 'HISTORICAL_RESEARCH_SIGNAL'
            reason_codes.append('historical_replay_incomplete')
            detail += '；仅为历史行情研究，账本和配置不可完整核实，不可执行'
        elif status == STATUS_SIGNAL and (cash is None or entry_amount(equity, cfg) is None or cash < entry_amount(equity, cfg)):
            base = entry_amount(equity, cfg)
            status = 'CASH_BLOCKED'
            if cash is None or base is None:
                detail = detail + '；账户可用现金或权益无法核实，不得生成买入计划'
                reason_codes.append('cash_unverified')
            else:
                detail = (detail + f'；账本可用现金 ¥{cash/100:,.2f} 不足基础仓目标 '
                                   f'¥{base/100:,.2f}，不得生成买入计划')
                reason_codes.append('cash_shortfall')

        signal_row = None
        # ISS-035/036：过期缓存与未核实来源都不得写成"看似已核实的正式信号"。
        stale = not self._fresh(series, as_of, require_verified=False)
        if stale:
            warnings.append(f'已完成交易日 {as_of} 没有对应日线（最新 {series.last_date}），'
                            '只展示缓存数据，不写正式信号')
        if not series.source_verified:
            warnings.append('行情来源未核实，只作研究参考，不写正式信号')
        if status == STATUS_SIGNAL:
            blockers = []
            if stale:
                blockers.append('stale_series_not_recorded')
            if not series.source_verified:
                blockers.append('unverified_source_not_recorded')
            if G2_ENABLED and not regime.known:
                blockers.append('g2_unavailable')
            if blockers:
                reason_codes.extend(blockers)
                status = 'DATA_UNAVAILABLE'
                signal_row = None
            elif persist:
                if G2_ENABLED:
                    for idx in configured_index_ids():
                        try:
                            peer_versions['G2:'+idx] = self.data.input_version(idx, completed_on=as_of, basis=INDEX_BASIS)
                        except SeriesError:
                            peer_versions['G2:'+idx] = 'unavailable'
                def commit_entry():
                    # Signal and capacity claim publish in ONE transaction. Failed
                    # capacity checks cannot leave an apparently verified orphan.
                    with self.db.transaction() as conn:
                        signal_row = self._record_signal(account_id, canonical, cfg, series, plan,
                                                        regime, factor, combo, basis, unit,
                                                        peer_versions, as_of, connection=conn)
                        if conn.execute("SELECT 1 FROM trade_executions WHERE account_id=? AND side='buy' AND status IN ('effective','reversed') AND json_extract(payload,'$.signal_id')=?",(account_id,signal_row['signal_id'])).fetchone():
                            return None
                        self.store.save_plan(
                            account_id=account_id, index_id=canonical, signal_date=plan.signal_date,
                            strategy_version=INDEX_STRATEGY_VERSION, status=STATUS_SIGNAL,
                            signal_id=signal_row['signal_id'],
                            signal_close=plan.signal_close, threshold_high=plan.threshold_high,
                            atr14=plan.atr14, buy_max=plan.buy_max,
                            initial_stop_reference=plan.initial_stop_reference,
                            trail_trigger_reference=plan.trail_trigger_reference, detail=plan.detail,
                            capacity_check=lambda c: self._check_plan_capacity(c, account_id, canonical, as_of, signal_row['signal_id'], evidence_revision=evidence_revision),
                            connection=conn,
                        )
                    return signal_row
                if deferred_entries is not None:
                    deferred_entries[canonical] = commit_entry
                else:
                    try:
                        signal_row = commit_entry()
                    except CapacityRefused as exc:
                        status = 'PORTFOLIO_CAPACITY_BLOCKED'
                        reason_codes.append('transaction_capacity_refused')
                        detail += '；'+str(exc)
                        signal_row = None
                    if signal_row is None and status != 'PORTFOLIO_CAPACITY_BLOCKED':
                        status = 'SIGNAL_CONSUMED'
                        reason_codes.append('signal_already_consumed')
            else:
                reason_codes.append('read_only_signal_not_recorded')
        elif fill is not None and fill.status == FILL_CANCELLED and persist and safe:
            self.store.clear_plan(account_id, canonical, reason=fill.reason)

        pyramid = None
        ndx_aux = None
        if position is not None and position.execution_source == SOURCE_LEDGER and position.is_open and held and safe and not historical:
            ma = ma_snapshot(list(series.closes), close=float(series.closes[-1]))
            pyramid = evaluate_pyramid(
                cfg, position, close=float(series.closes[-1]), snap=ma,
                prior_closes=list(series.closes[:-1]), holding_days=position.holding_days,
                regime_state=regime.state, regime_label=regime.label,
            ).to_payload()
        # T5 is a separate, non-reserved advisory track. It is never an ATR stop
        # or a broker order; actual fills may link to its own evidence ID.
        if cfg.is_us_track and safe and not historical and basis == ETF_UNADJUSTED:
            ndx_aux = self._ndx_advice(account_id, as_of, cfg, series, held,
                                      persist=persist and deferred_entries is None, competing_entry=status == STATUS_SIGNAL)
        if cfg.is_us_track and ndx_aux is None:
            ndx_aux = {'status':'DATA_UNAVAILABLE','detail':'历史或真实 NDX 策略证据不足，不提供执行建议','tradable':False,'capacity_kind':'advice_only_no_reservation'}

        atr = compute_atr14(series.highs, series.lows, series.closes)
        price_now = float(series.closes[-1])
        gap = None
        if plan.threshold_high is not None and atr:
            gap_abs = float(plan.threshold_high) - price_now
            gap = {'abs': gap_abs, 'pct': gap_abs / price_now * 100 if price_now else None,
                   'atr_multiple': gap_abs / atr}
        signal_atr = plan.atr14
        stop_warning = self._near_stop(account_id, position, held, as_of, evidence_revision) if safe and not historical and basis == ETF_UNADJUSTED and not model_exit else []
        return {
            'index_id': canonical, 'name': cfg.name, 'market': cfg.market,
            **self._time_payload(as_of),
            'track': cfg.track_label, 'etf_code': cfg.etf_code,
            'price_unit': unit, 'price_digits': cfg.price_digits,
            'price_basis': basis,
            'status': status, 'detail': detail, 'reason_codes': reason_codes,
            'signal_date': plan.signal_date,
            'signal_close': plan.signal_close, 'threshold_high': plan.threshold_high,
            'atr14': signal_atr, 'buy_max': plan.buy_max,
            'entry_window': plan.entry_window, 'breakout_window': cfg.breakout_window,
            'factor': _factor_payload(factor), 'combo': combo.to_payload(),
            'regime': self._regime_payload(regime),
            'cooldown': cooldown, 'gap': gap,
            'fill': _fill_payload(fill),
            'position': self._position_payload(position, model_decision, cfg, unit),
            'model_exit': model_exit, 'near_stop': stop_warning,
            'pyramid': pyramid, 'ndx_aux': ndx_aux,
            'live_quantity': None if historical else live_qty,
            'evidence': evidence,
            'signal_id': signal_row['signal_id'] if signal_row else None,
            'input_version': (signal_row['input_version'] if signal_row else None),
            'config_version': config_fingerprint(canonical),
            'tradable': bool((basis == ETF_UNADJUSTED and cfg.etf_code and live_qty >= 0
                              and safe and not historical and signal_row is not None)),
            'tradable_block_reason': ('历史回放不可执行' if historical else _tradable_block_reason(basis, cfg, series, live_qty)),
            'series': series_summary(series), 'warnings': warnings,
            'capacity_kind': 'plan_capacity_checked_not_cash_escrow' if signal_row else 'advice_only_no_reservation',
            'read_only': not persist,
        }

    @scoped
    def position_rows(self, account_id: str, *, as_of: str | None = None,
                      allow_network: bool = True, persist: bool = False) -> dict:
        """持仓决策视图：**真实持仓驱动** + 策略建议 + 未核对证据。

        真实批次是权威：即使 ``strategy_positions`` 里没有任何记录，只要账本里有
        ``lot_role in ('main','sub')`` 的批次，这里就必须列出并给出决策。
        """
        try:
            day = self._as_of(as_of)
        except (CalendarUnavailable, CalendarCoverageError, StrategyCalendarUnavailable) as exc:
            return self._calendar_unavailable(account_id, exc)
        persist = persist and not self._historical(day)
        allow_network = allow_network and persist
        custody = self._custody(account_id, day)
        by_index: dict[str, list[dict]] = {}
        orphans: list[dict] = []
        for lot in custody:
            index_id = self._lot_index(lot)
            if index_id is None:
                orphans.append(lot)
                continue
            by_index.setdefault(index_id, []).append(lot)
        items = []
        for index_id in sorted(by_index):
            items.append(self.evaluate(index_id, account_id=account_id, as_of=day,
                                       allow_network=allow_network, persist=persist))
        # 只有策略证据、账本里已没有剩余批次的（例如模型已建议卖出但还没实际卖出）
        evidence_only = []
        for record in ([] if self._historical(day) else self.store.positions_for(account_id)):
            if record['index_id'] in by_index:
                continue
            if not record['entry_executed'] or record['exit_filled']:
                continue
            evidence_only.append({'index_id': record['index_id'],
                                  'signal_date': record['signal_date'],
                                  'entry_date': record['entry_date'],
                                  'entry_price': record['entry_price'],
                                  'signal_atr': record['signal_atr'],
                                  'execution_source': record['execution_source'],
                                  'note': '账本已无剩余批次；该策略证据仅作历史记录，不构成持仓'})
        # 模型给出、但账本尚未确认的退出/加仓建议：只作证据，不构成成交。
        unrealized = []
        for item in items:
            model = item.get('model_exit')
            if not model:
                continue
            unrealized.append({'index_id': item['index_id'], 'etf_code': item.get('etf_code'),
                               'action': model.get('action'), 'action_price': model.get('price'),
                               'action_reason': model.get('reason') or '',
                               'price_basis': item.get('price_basis'),
                               'model_only': True,
                               'note': model.get('note', '')})
        return {
            'as_of': day, **self._time_payload(day), 'account_id': account_id, 'strategy_version': INDEX_STRATEGY_VERSION,
            'items': items, 'custody': custody, 'custody_quality': 'historical_unavailable' if self._historical(day) else 'current_ledger', 'evidence_only': evidence_only,
            'orphan_lots': orphans,
            'unrealized_model_actions': unrealized,
            'read_only': not persist,
            'note': ('策略建议不等于真实成交；真实主仓/子仓/退出只能经 TradeService 确认。'
                     '模型退出只形成建议，不释放组合槽位、不启动冷却。'),
        }

    # -- 真实批次 -> 策略证据 -------------------------------------------
    @scoped
    def sync_from_custody(self, account_id: str, *, as_of: str | None = None) -> dict:
        """从真实批次重建缺失的策略证据（ISS-032）。

        只在 ``frozen_atr`` 与 ``signal_date`` 都可核实、且能映射到配置指数时才写库；
        否则返回待人工核对的条目，绝不编造冻结 ATR 或信号日。
        """
        day = self._as_of(as_of)
        if self._historical(day):
            raise StrategyCalendarUnavailable('历史回放不允许写入策略证据')
        custody = self._custody(account_id, day)
        created, pending = [], []
        for index_id, lots in self._group_by_index(custody).items():
            cfg = get_config(index_id)
            atr_lots = [lot for lot in lots if lot['strategy_id'] == 'INDEX_STRATEGY_ATR']
            if not atr_lots:
                continue  # T5 derives directly from actual executions; no ATR state
            state, reason = self._state_from_lots(index_id, cfg, atr_lots)
            if state is None or state.execution_source != SOURCE_LEDGER:
                pending.append({'index_id': index_id, 'lots': [lot['lot_id'] for lot in lots],
                                'reason': reason or ('strategy_deviation' if any(lot.get('strategy_deviation') for lot in atr_lots) else 'signal_link_unverified')})
                continue
            self.store.save_position(account_id, state)
            created.append({'index_id': index_id, 'signal_date': state.signal_date,
                            'entry_date': state.entry_date, 'shares': state.shares,
                            'main_lot_id': state.main_lot_id,
                            'execution_source': state.execution_source})
        return {'account_id': account_id, 'as_of': day, 'created': created, 'pending': pending}

    def core_positions_used(self, account_id: str, as_of: str) -> tuple[int, str]:
        """真实主仓占用的核心槽位数（并上策略证据，防止两边都漏）。

        - 只有主仓（``lot_role='main'``）占核心槽位，子仓不额外占；
        - NDX 独立轨（``counts_against_core_slots`` 为假）不占核心槽位；
        - 账本已无剩余批次时，策略证据里仍 open 的也不占槽位（真实仓位已不存在）。
        """
        custody = self._custody(account_id, as_of)
        from_lots = set()
        for lot in custody:
            if str(lot['lot_role']) != 'main' or lot['strategy_id'] != 'INDEX_STRATEGY_ATR':
                continue
            index_id = self._lot_index(lot)
            if index_id and get_config(index_id).counts_against_core_slots:
                from_lots.add(index_id)
        return len(from_lots), f'真实主仓 {len(from_lots)} 个（不计模型证据）'

    # -- 内部 ------------------------------------------------------------
    def current_day(self, as_of: str | None = None) -> str:
        return self._as_of(as_of)

    def _as_of(self, as_of: str | None) -> str:
        """已完成交易日。日历不可用时**抛错**，绝不拿本地日期冒充交易日（ISS-035）。"""
        try:
            context = active_decision.get()
            latest = market_window(context.now if context else self.clock()).latest_completed.isoformat()
            if not as_of:
                if context and context.mode == 'historical':
                    raise StrategyCalendarUnavailable('历史回放必须指定截止日期')
                return latest
            if context and context.mode == 'current' and as_of != latest:
                raise StrategyCalendarUnavailable('当前模式不能指定非最新已完成交易日')
            day = date.fromisoformat(str(as_of)).isoformat()
            if day > latest or not market_clock('CN').is_trading_day(day):
                raise StrategyCalendarUnavailable('请求日期不是已完成交易日')
            return day
        except (CalendarUnavailable, CalendarCoverageError, ValueError) as exc:
            raise StrategyCalendarUnavailable('交易日历不可用，无法确定已完成交易日', detail=str(exc)) from exc

    def _historical(self, day: str) -> bool:
        context = active_decision.get()
        if context is not None:
            return context.mode == 'historical'
        return day != market_window(self.clock()).latest_completed.isoformat()

    def _decision_time(self, day: str) -> datetime:
        context = active_decision.get()
        if context is not None:
            return context.cutoff(day)
        if not self._historical(day):
            return self.clock()
        return min(self.clock(), datetime.combine(date.fromisoformat(day), time.max, ZoneInfo('Asia/Shanghai')))

    def _time_payload(self, day: str) -> dict:
        historical = self._historical(day)
        return {'mode': 'historical' if historical else 'current',
                'market_session': day, 'ledger_cutoff': self._decision_time(day).isoformat(),
                'historical_replay_complete': False if historical else None,
                'historical_limitations': ['historical_configuration_not_versioned',
                    'ledger_corrections_not_bitemporal'] if historical else []}

    def _fresh(self, series, day: str, *, require_verified: bool = True) -> bool:
        try:
            target = market_clock(series.market).last_completed_session(now=self._decision_time(day))
            return bool(target and series.last_date == target and (series.source_verified or not require_verified))
        except (CalendarCoverageError, StrategyCalendarUnavailable):
            return False

    def _calendar_unavailable(self, account_id: str, exc: Exception) -> dict:
        detail = getattr(exc, 'reason', None) or str(exc)
        extra = getattr(exc, 'detail', '') or ''
        return {
            'as_of': None, 'account_id': account_id, 'strategy_version': INDEX_STRATEGY_VERSION,
            'status': STATUS_DATA_UNAVAILABLE, 'reason_codes': ['calendar_unavailable'],
            'detail': f'{detail}；在可信交易日历恢复前不生成正式信号，也不推算下一交易日',
            'calendar_detail': extra, 'items': [], 'custody': [], 'read_only': True,
            'regime': None, 'core_positions': 0, 'max_positions': PORTFOLIO_MAX_POSITIONS,
        }

    def _unavailable(self, canonical: str, cfg, title: str, exc: SeriesError,
                     basis: str) -> dict:
        return {
            'index_id': canonical, 'name': cfg.name, 'market': cfg.market,
            'track': cfg.track_label, 'etf_code': cfg.etf_code,
            'price_unit': basis_unit(basis), 'price_digits': cfg.price_digits,
            'price_basis': basis, 'status': STATUS_DATA_UNAVAILABLE,
            'detail': f'{title}：{exc.reason}',
            'reason_codes': ['series_unavailable'], 'warnings': [str(exc.detail or exc.reason)],
            'signal_date': '', 'series': {'available': False}, 'tradable': False,
            'position': None, 'model_exit': None, 'evidence': None, 'signal_id': None,
            'input_version': None, 'config_version': config_fingerprint(canonical),
        }

    def _regime_payload(self, regime: RegimeSnapshot) -> dict:
        return {'state': regime.state, 'label': regime.label,
                'short': REGIME_SHORT.get(regime.state, '--'),
                'er60': regime.er60, 'change_pct': regime.change_pct,
                'components': regime.components, 'points': regime.points,
                'as_of': regime.as_of, 'warning': regime.warning, 'known': regime.known,
                'price_basis': INDEX_BASIS, 'price_unit': basis_unit(INDEX_BASIS)}

    def _equity(self, account_id: str, as_of: str) -> tuple[int | None, int | None, str, list[str]]:
        if self._historical(as_of):
            return None, None, 'historical_incomplete', ['historical_valuation_availability_not_verified']
        try:
            valuation = self.portfolio.valuation(account_id, self._decision_time(as_of))
        except Exception as exc:  # noqa: BLE001 - 降级为不可用，不编造权益
            return None, None, 'unavailable', [f'equity_unavailable:{type(exc).__name__}']
        return (valuation.get('equity_fen'), valuation.get('cash_fen') if valuation.get('cash_quality') == 'valid' else None,
                valuation.get('quality', 'derived_unverified'),
                list(valuation.get('reasons') or []))

    def _peer_series(self, index_id: str, basis: str, as_of: str,
                     allow_network: bool) -> tuple[dict, dict]:
        """同组因子输入。``evaluate_factor`` 要求 ``Dict[str, IndexSeries]``（ISS-036）。"""
        peers: dict = {}
        versions: dict = {}
        for peer in peers_of(index_id):
            try:
                peer_cfg = get_config(peer)
                if basis == INDEX_BASIS:
                    series = self.data.index_series(peer, completed_on=as_of,
                                                    allow_network=allow_network)
                else:
                    series = self.data.etf_series(peer, completed_on=as_of, basis=basis,
                                                  allow_network=allow_network)
            except (SeriesError, KeyError):
                continue
            if not self._fresh(series, as_of):
                continue
            key = peer_cfg.etf_code or peer
            peers[key] = series
            versions[key] = self.data.input_version(peer, completed_on=as_of, basis=basis)
        return peers, versions

    def _check_plan_capacity(self, conn, account_id: str, index_id: str, day: str, signal_id: str, *, evidence_revision=None):
        """Under BEGIN IMMEDIATE, recalculate actual cash, lots and all active plans.

        Active plans count as dynamic capacity claims, not cash movements. Real
        executions remain authoritative even when they exceed a prior claim.
        """
        if evidence_revision is not None and conn.execute("SELECT ledger_revision FROM ledger_state").fetchone()[0] != evidence_revision:
            raise CapacityRefused("ledger changed during signal evaluation; refresh")
        at = self._decision_time(day)
        cutoff = utc(at)
        try:
            window = market_window(at)
            valuation = self.portfolio._valuation(conn, account_id, at, window,
                                                   self.portfolio._revision(conn, None))
        except Exception as exc:
            raise CapacityRefused('current valuation unavailable') from exc
        equity, cash = valuation['equity_fen'], valuation['cash_fen']
        if equity is None or cash is None or valuation['cash_quality'] != 'valid':
            raise CapacityRefused('current cash or equity unavailable')
        if conn.execute("SELECT 1 FROM trade_executions WHERE account_id=? AND side='buy' AND status IN ('effective','reversed') AND json_extract(payload,'$.signal_id')=?", (account_id, signal_id)).fetchone():
            raise CapacityRefused('signal already consumed')
        held = set()
        for lot in self.portfolio._lots(conn, account_id, cutoff):
            if lot['quantity'] <= 0:
                continue
            code = conn.execute('SELECT code FROM instruments WHERE id=?', (lot['instrument_id'],)).fetchone()[0]
            idx = self._lot_index({'code':code})
            if index_id == 'NDX' and idx == 'NDX':
                raise CapacityRefused('actual NDX holding already exists')
            if lot['lot_role'] != 'main' or lot['strategy_id'] != 'INDEX_STRATEGY_ATR':
                continue
            if idx is None:
                raise CapacityRefused('unmapped actual strategy holding')
            held.add(idx)
        if index_id in held:
            raise CapacityRefused('actual holding already exists')
        active = {row[0] for row in conn.execute("SELECT index_id FROM strategy_plans WHERE account_id=? AND status IN ('SIGNAL','AWAITING_CONFIRMATION')", (account_id,))}
        claims = (active - held) | {index_id}
        try:
            slots = sum(get_config(idx).counts_against_core_slots for idx in held | claims)
            budget = sum(math.ceil(entry_amount(equity,get_config(idx))) for idx in claims)
        except (KeyError, TypeError, ValueError) as exc:
            raise CapacityRefused('unverified capacity claim') from exc
        buffer = math.ceil(NDX_AUX_CASH_BUFFER*100) if any(get_config(idx).is_us_track for idx in claims) else 0
        if slots > PORTFOLIO_MAX_POSITIONS or budget + buffer > cash:
            raise CapacityRefused('account cash or core slots exhausted by active plans')

    def _ndx_advice(self, account_id, as_of, cfg, series, held, *, persist=False, competing_entry=False):
        """T5 reference amounts from a single ledger snapshot, no cash reservation.

        Explicit T5-linked actuals reconstruct the active cycle on every request,
        including fully sold sub-lots. Never turn an ATR signal into T5 proof.
        """
        unavailable = {'status':'DATA_UNAVAILABLE','detail':'NDX 实际持仓/现金/行情证据不足',
                       'current_ratio':None,'target_ratio':None,'target_amount':None,
                       'tradable':False,'actionable':False,'capacity_kind':'advice_only_no_reservation'}
        try:
            if self._historical(as_of) or not self._fresh(series, as_of) or series.is_proxy or series.market != 'CN':
                return unavailable
            if not validate_series(series, breakout_window=cfg.breakout_window, as_of=as_of)['ok']:
                return unavailable
            cn = market_clock('CN'); us = market_clock('US')
            at = self._decision_time(as_of); cutoff = utc(at)
            completed = us.last_completed_session(now=at)
            next_session = cn.next_trading_day(series.last_date)
            if not completed or not next_session or at.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() > next_session:
                return unavailable
            # The original evaluator uses the selected unadjusted ETF series,
            # while elapsed add days are completed US exchange sessions.
            if len(series.bars) < 201 or any(cn.next_trading_day(a.date)!=b.date for a,b in zip(series.bars[-201:-1],series.bars[-200:])):
                return unavailable
            context = self.db.transaction() if persist else self.db.read()
            with context as conn:
                require_account(conn, account_id, securities=True, at=cutoff)
                value = self.portfolio._valuation(conn,account_id,at,market_window(at),self.portfolio._revision(conn,None))
                equity,cash = value['equity_fen'],value['cash_fen']
                if equity is None or equity<=0 or cash is None or value['cash_quality']!='valid':
                    return unavailable
                positions = [p for p in value['positions'] if p['code']==cfg.etf_code]
                if len(positions)>1 or any(p['market_value_fen'] is None for p in positions):
                    return unavailable
                qty = sum(p['quantity'] for p in positions)
                mv = sum(p['market_value_fen'] for p in positions)
                lots = [l for p in positions for l in p['lots']]
                if any(l['strategy_id']!='NDX_AUX_T5' for l in lots):
                    return {**unavailable,'detail':'已有普通或 ATR NDX 仓位；不得自动归为 T5 辅助仓'}
                # Corporate actions may invalidate strategy quantities or scales.
                if qty and conn.execute("""SELECT 1 FROM company_events e JOIN instruments i ON i.id=e.instrument_id
                        WHERE e.account_id=? AND i.code=? AND e.voided_at IS NULL AND e.strategy_evidence_invalid=1
                        AND e.occurred_at BETWEEN ? AND ?""",(account_id,cfg.etf_code,min(l['opened_at'] for l in lots),cutoff)).fetchone():
                    return unavailable
                facts = rows(conn,"""SELECT t.*, CASE WHEN t.side='buy' THEN t.quantity ELSE COALESCE((SELECT SUM(a.quantity)
                    FROM lot_allocations a JOIN trade_lots l ON l.id=a.lot_id WHERE a.sell_execution_id=t.id
                    AND l.strategy_id='NDX_AUX_T5' AND l.status!='reversed'),0) END AS t5_qty
                    FROM trade_executions t JOIN instruments i ON i.id=t.instrument_id
                    WHERE t.account_id=? AND i.code=? AND t.status='effective'
                    AND (t.side='sell' OR t.strategy_id='NDX_AUX_T5')
                    AND t.executed_at<=? ORDER BY t.executed_at,t.rowid""",(account_id,cfg.etf_code,cutoff))
                cycle=[]; running=0
                for fact in facts:
                    if fact['side']=='buy':
                        running+=fact['quantity']; cycle.append(fact)
                    else:
                        running-=fact['t5_qty']
                    if running<0: return unavailable
                    if running==0: cycle=[]
                if running!=qty or (qty and (not cycle or cycle[0]['lot_role']!='main' or any(not verified_trade_signal(conn,json.loads(f['payload'])) for f in cycle))):
                    return unavailable
                # Distinct refreshed advice IDs for the same action/day cannot
                # establish which target the actual fills were following.
                linked={}
                for fact in cycle:
                    payload=json.loads(fact['payload'])
                    key=(payload.get('signal_date'),fact['lot_role'])
                    linked.setdefault(key,set()).add(payload.get('signal_id'))
                if any(len(ids)>1 for ids in linked.values()):
                    return {**unavailable,'detail':'同日同角色关联多个建议版本，实际成交保留，策略证据待核对'}
                # Initial actual fill anchors the first interval; later actual
                # adds (even if trimmed away) reset it. No guessed KV timestamp.
                last=max((f['trading_day'] for f in cycle),default=None)
                elapsed=None
                if last and us.covered(last) and last<=completed:
                    elapsed=sum(d<=completed for d in us.trading_days_after(last,10))
                elif last and us.covered(last) and last>completed:
                    elapsed=0
                plan=asdict(evaluate_ndx_aux(series.closes,equity=equity/100,current_ratio=mv/equity,
                            days_since_add=elapsed,last_date=series.last_date))
                plan['detail']=plan['detail'].replace('下一美股合法开盘','下一合法 ETF 交易日').replace('下一美股开盘','下一合法 ETF 交易日')
                plan.update({'tradable':False,'capacity_kind':'advice_only_no_reservation',
                    'actual_cash_fen':cash,'actual_market_value_fen':mv,'actual_quantity':qty,
                    'last_actual_buy_day':last,'us_completed_session':completed,'next_etf_session':next_session,
                    'verified_advice':True,'actionable':False,'signal_id':None,'strategy_id':'NDX_AUX_T5'})
                if plan['status']=='EXIT' and not qty:
                    plan.update(status='NO_POSITION',detail='无真实 T5 持仓，无需退出')
                if plan['status']=='ALLOW' and plan.get('target_amount') is not None:
                    active={r[0] for r in conn.execute("SELECT index_id FROM strategy_plans WHERE account_id=? AND status IN ('SIGNAL','AWAITING_CONFIRMATION')",(account_id,))}
                    if 'NDX' in active or competing_entry:
                        plan.update(status='CAPACITY_BLOCKED',detail='NDX 已有或同时出现 ATR 计划；不得重复配置同一标的')
                        return plan
                    claims=sum(math.ceil(entry_amount(equity,get_config(idx))) for idx in active)
                    need=max(0,math.ceil(plan['target_amount']*100)-mv)
                    if need+claims+math.ceil(NDX_AUX_CASH_BUFFER*100)>cash:
                        plan.update(status='CASH_BLOCKED',detail='实际现金扣除已有容量计划后不足以保留 25 万元缓冲')
                        return plan
                    plan['actionable']=True
                    if persist:
                        role='ndx_aux_add' if qty else 'ndx_aux_initial'
                        version=self.data.input_version('NDX',completed_on=as_of,basis=ETF_UNADJUSTED)
                        fingerprint=sha256(json.dumps([version,role,value['ledger_revision'],value['quote_revision'],plan['target_amount']],separators=(',',':')).encode()).hexdigest()
                        signal=self.store.save_signal(account_id=account_id,index_id='NDX',instrument_code=cfg.etf_code,
                            signal_date=series.last_date,input_version='t5:'+fingerprint,strategy_version=INDEX_STRATEGY_VERSION,
                            config_version=config_fingerprint('NDX'),status='NDX_AUX_ADVICE',reason_codes=[role],detail=plan['detail'],
                            price_basis=ETF_UNADJUSTED,price_unit='元',signal_close=plan['close'],data_source=series.source,
                            is_proxy=False,source_verified=True,connection=conn)
                        plan.update(signal_id=signal['signal_id'],signal_date=series.last_date,lot_role='sub' if qty else 'main')
                elif plan['status'] in ('TRIM','EXIT') and qty:
                    plan['actionable']=True
                return plan
        except (ValueError,KeyError,TypeError,CalendarCoverageError,CalendarUnavailable,SeriesError):
            return unavailable

    def _custody(self, account_id: str, as_of: str) -> list[dict]:
        """真实账本中带策略标记的批次（主仓/子仓），用于和策略建议对照。

        口径与 :meth:`PortfolioService._lots` 保持一致：被作废/更正的批次、尚未生效的
        成交、以及更正重放过程中的中间态一律不算真实持仓，否则"更正后仍显示旧数量"
        这类错误会直接被当作权威事实。
        """
        if self._historical(as_of):
            return []  # no bitemporal ledger projection; unknown is not an empty position
        cutoff = utc(self._decision_time(as_of))
        result = []
        with self.db.read() as conn:
            for lot in self.portfolio._lots(conn, account_id, cutoff):
                if lot['lot_role'] not in ('main','sub') or lot['quantity'] <= 0 or lot['strategy_id'] not in ('INDEX_STRATEGY_ATR','NDX_AUX_T5'):
                    continue
                instrument = rows(conn, 'SELECT code,market,kind FROM instruments WHERE id=?', (lot['instrument_id'],))[0]
                trade = rows(conn, 'SELECT * FROM trade_executions WHERE id=?', (lot['open_execution_id'],)) if lot['open_execution_id'] else []
                import json
                payload = json.loads(trade[0]['payload']) if trade else {}
                proof = verified_trade_signal(conn, payload) if trade and not self._historical(as_of) else None
                scale_invalid = bool(conn.execute("SELECT 1 FROM company_events WHERE account_id=? AND instrument_id=? AND occurred_at>=? AND occurred_at<=? AND voided_at IS NULL AND strategy_evidence_invalid=1", (account_id,lot['instrument_id'],lot['opened_at'],cutoff)).fetchone())
                result.append({**lot, **instrument, 'remaining':lot['quantity'], 'entry_price_fen':trade[0]['price'] if trade else None,
                               'execution_id':lot['open_execution_id'], 'signal_id':payload.get('signal_id'), 'signal_verified':proof is not None, 'strategy_deviation':bool(payload.get('strategy_deviation')),
                               'deviation_note':payload.get('note') if payload.get('strategy_deviation') else None,
                               'scale_invalid':scale_invalid})
        return result

    @staticmethod
    def _lot_index(lot: dict) -> Optional[str]:
        from backend.domain.strategy.symbols import IndexCodeError, index_for_etf
        try:
            return index_for_etf(lot['code']) if lot.get('code') else None
        except (IndexCodeError, KeyError):
            return None

    def _group_by_index(self, custody: list[dict]) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for lot in custody:
            index_id = self._lot_index(lot)
            if index_id is None:
                continue
            grouped.setdefault(index_id, []).append(lot)
        return grouped

    def _position_for(self, account_id: str, index_id: str, cfg, lots: list[dict], clock,
                      as_of: str, warnings: list[str]) -> tuple[Optional[PositionState], dict]:
        """取该指数的策略证据：已有记录优先，缺失则由真实批次重建（不落库）。"""
        if not lots:
            return None, {'status': 'none', 'reason_codes': [], 'source': 'none'}
        state, reason = self._state_from_lots(index_id, cfg, lots)
        if state is None:
            warnings.append(f'{index_id} 存在真实策略批次但证据不完整（{reason}），'
                            '请人工核对冻结 ATR 与信号日')
            return None, {'status': 'unverified', 'reason_codes': [reason],
                          'source': 'trade_lots'}
        warnings.append(f'{index_id} 策略状态由真实批次重建（尚未写入策略证据表）')
        return state, {'status': state.execution_source, 'reason_codes': [] if state.execution_source == SOURCE_LEDGER else (['strategy_deviation'] if any(lot.get('strategy_deviation') for lot in lots) else ['signal_link_unverified']),
                       'source': 'trade_lots'}

    def _state_from_lots(self, index_id: str, cfg,
                         lots: list[dict]) -> tuple[Optional[PositionState], str]:
        """把真实批次还原成 ``PositionState``；证据不全时返回 (None, 原因)。"""
        mains = [lot for lot in lots if lot['lot_role'] == 'main']
        if any(lot.get('scale_invalid') for lot in lots):
            return None, 'company_action_scale_unverified'
        if len(mains) > 1:
            return None, 'multiple_main_lots_require_individual_review'
        main = mains[0] if mains else None
        if main is None:
            return None, 'main_lot_missing'
        if main['frozen_atr'] is None:
            return None, 'frozen_atr_missing'
        if not main['signal_date']:
            return None, 'signal_date_missing'
        try:
            signal_atr = float(main['frozen_atr']) / SCALE
            entry_price = (float(main['entry_price_fen']) / SCALE
                           if main['entry_price_fen'] is not None else None)
        except (TypeError, ValueError):
            return None, 'lot_price_or_atr_invalid'
        if not signal_atr or signal_atr <= 0 or not entry_price or entry_price <= 0:
            return None, 'lot_price_or_atr_invalid'
        tick = 10.0 ** -int(cfg.price_digits) if cfg.price_digits else 0.001
        state = PositionState(
            index_id=index_id, strategy_version=INDEX_STRATEGY_VERSION,
            signal_date=str(main['signal_date'])[:10],
            entry_date=str(main['opened_at'])[:10],
            entry_price=entry_price, signal_atr=signal_atr,
            initial_stop=initial_stop_price(cfg, entry_price, signal_atr, tick=tick),
            stop_price=initial_stop_price(cfg, entry_price, signal_atr, tick=tick),
            trail_trigger=trail_trigger_price(cfg, entry_price, signal_atr),
            highest_price=entry_price, current_atr=signal_atr,
            main_lot_id=main['lot_id'], shares=int(main['remaining'] or 0),
            entry_executed=True, execution_source=SOURCE_LEDGER if main.get('signal_verified') else SOURCE_UNVERIFIED,
            last_trade_date=str(main['opened_at'])[:10],
        )
        for lot in lots:
            if str(lot['lot_role']) != 'sub':
                continue
            if not lot.get('signal_verified'):
                state.execution_source = SOURCE_UNVERIFIED
            add_atr = (float(lot['frozen_atr']) / SCALE) if lot['frozen_atr'] else None
            add_price = (float(lot['entry_price_fen']) / SCALE
                         if lot['entry_price_fen'] is not None else None)
            if not add_atr or not add_price:
                state.execution_source = SOURCE_UNVERIFIED
                state.notes.append('子仓批次缺少冻结 ATR 或成交价，独立止损待人工核对')
                continue
            state.adds.append({
                'date': str(lot['opened_at'])[:10], 'price': add_price,
                'signal_atr': add_atr, 'shares': int(lot['remaining'] or 0),
                'lot_id': lot['lot_id'],
                'initial_stop': initial_stop_price(cfg, add_price, add_atr, tick=tick),
                'stop_price': initial_stop_price(cfg, add_price, add_atr, tick=tick),
                'highest_price': add_price,
            })
        state.add_executed = bool(state.adds)
        if state.adds:
            state.add_filled_date = max(add['date'] for add in state.adds)
        return state, ''

    def _simulate(self, position: PositionState, series, cfg, clock,
                  warnings: list[str]) -> tuple[PositionState, Optional[object]]:
        """在**隔离副本**上推进模型（ISS-032 §5.3）。

        ``advance_position`` 是可变状态机；直接作用于真实证据就会把模拟成交写进去。
        """
        simulated = deepcopy(position)
        start_after = simulated.last_trade_date or simulated.entry_date
        pending = [bar for bar in series.bars if str(bar.date) > str(start_after)]
        if not pending and simulated.entry_date == series.last_date:
            pending = [bar for bar in series.bars if bar.date == series.last_date]
        decision = None
        for bar in pending:
            upto = [item for item in series.bars if item.date <= bar.date]
            highs = [item.high for item in upto]
            lows = [item.low for item in upto]
            closes = [item.close for item in upto]
            atr = compute_atr14(highs, lows, closes)
            snap = ma_snapshot(closes, close=float(bar.close))
            threshold = None
            if len(highs) >= cfg.breakout_window + 1:
                threshold = max(highs[-(cfg.breakout_window + 1):-1])
            tick = 10.0 ** -int(cfg.price_digits) if cfg.price_digits else 0.001
            decision = advance_position(
                simulated, bar, cfg, trade_date=str(bar.date), current_atr=atr,
                next_trading_day=clock.next_trading_day(bar.date), tick=tick,
                ma=snap, renewal_threshold=threshold,
            )
            if simulated.exit_filled:
                break
        if decision is None:
            tick = 10.0 ** -int(cfg.price_digits) if cfg.price_digits else 0.001
            decision = advance_position(
                simulated, series.bars[-1], cfg, trade_date=str(series.bars[-1].date),
                current_atr=None, tick=tick,
                next_trading_day=clock.next_trading_day(series.bars[-1].date))
        if decision is not None and decision.events:
            warnings.extend(decision.events)
        return simulated, decision

    @staticmethod
    def _merge_observed(position: PositionState, simulated: PositionState) -> PositionState:
        """把模拟推进的**观测衍生字段**合并回真实证据；执行结果字段一概不写。"""
        for name in OBSERVED_FIELDS:
            setattr(position, name, deepcopy(getattr(simulated, name)))
        return position

    @staticmethod
    def _model_exit_payload(simulated: PositionState, decision) -> Optional[dict]:
        if not simulated.exit_filled:
            return None
        return {
            'model_only': True,
            'note': '模型止损/退出建议，仅作证据；实际卖出经 TradeService 确认前仓位仍然存在',
            'action': simulated.last_exit_action or ACTION_CLOSED,
            'price': decision.price if decision else None,
            'date': simulated.closed_date,
            'reason': simulated.last_exit_reason or '',
            'price_unit': '',
        }

    def _plan_used(self, account_id: str, plan: dict, as_of: str) -> bool:
        """A filled signal cannot become an active plan again after a real exit."""
        cutoff = utc(self._decision_time(as_of))
        with self.db.read() as conn:
            return bool(conn.execute("""SELECT 1 FROM trade_executions WHERE account_id=?
                AND side='buy' AND status IN ('effective','reversed') AND executed_at<=?
                AND json_extract(payload,'$.signal_id')=? LIMIT 1""",
                (account_id, cutoff, plan['signal_id'])).fetchone())

    def _cooldown_state(self, account_id: str, index_id: str, as_of: str, clock,
                        regime_state: str) -> dict:
        """Rebuild cooldown from effective, actually closed main lots, never a model exit.

        An unknown actual exit reason requires an explicit corrected execution. Old
        strategy_cooldowns rows remain audit evidence, not executable facts.
        """
        cutoff = utc(self._decision_time(as_of))
        with self.db.read() as conn:
            closed = []
            for lot in self.portfolio._lots(conn, account_id, cutoff):
                if lot['lot_role'] != 'main' or lot['quantity'] != 0 or lot['strategy_id'] != 'INDEX_STRATEGY_ATR':
                    continue
                instrument = rows(conn,'SELECT code FROM instruments WHERE id=?',(lot['instrument_id'],))[0]
                if self._lot_index(instrument) != index_id:
                    continue
                exits = rows(conn,"""SELECT t.trading_day,t.executed_at,t.payload FROM lot_allocations a
                    JOIN trade_executions t ON t.id=a.sell_execution_id
                    WHERE a.lot_id=? AND t.status='effective' AND t.executed_at<=?
                    ORDER BY t.executed_at DESC,t.rowid DESC LIMIT 1""",(lot['lot_id'],cutoff))
                closed.extend(exits)
        if not closed:
            return {'active':False,'until':None,'remaining':0,'note':''}
        last = max(closed,key=lambda row:row['executed_at'])
        action = json.loads(last['payload']).get('strategy_exit_action')
        if action not in ('STOP','GAP_STOP','EXIT','CLOSED'):
            return {'active':True,'until':None,'remaining':None,'exit_date':last['trading_day'],
                    'note':'实际主仓已退出，但退出原因未核实；请更正真实卖出记录后计算冷却期'}
        if G2_ENABLED and regime_state == 'UNKNOWN':
            return {'active':True,'until':None,'remaining':None,'exit_date':last['trading_day'],
                    'note':'G2 状态未知，冷却期限不可核实'}
        required = cooldown_required_days(regime_state=regime_state,exit_action=action)
        try:
            days = clock.trading_days_after(last['trading_day'], required + 6)
        except (CalendarCoverageError, CalendarUnavailable):
            days = []
        until = cooldown_until_date(last['trading_day'], days, required_days=required)
        if not until:
            return {'active':True,'until':None,'remaining':None,'exit_date':last['trading_day'],
                    'note':'交易日历覆盖不足，冷却完成日未知'}
        return {'active':as_of < str(until),'until':until,
                'remaining':sum(1 for day in days if as_of < day < str(until)),
                'required_days':required,'exit_date':last['trading_day'],'exit_action':action,
                'note':f'实际主仓退出（{action}），需 {required} 个完整交易日，最早 {until} 可再买'}

    def _evaluate_pending_plan(self, account_id: str, index_id: str, cfg, series,
                               plan_row: dict, clock, warnings: list[str],
                               persist: bool):
        signal_date = plan_row['signal_date']
        next_day = clock.next_trading_day(signal_date)
        if next_day is None:
            warnings.append(f'{index_id} 交易日历不可用，无法判定计划是否成交')
            return None
        bar = next((item for item in series.bars if item.date == next_day), None)
        if bar is None:
            return None
        plan = _plan_from_row(plan_row, cfg)
        fill = evaluate_fill(plan, bar)
        if fill.status == FILL_FILLED and persist:
            try:
                self.store.save_plan(
                    account_id=account_id, index_id=index_id, signal_date=signal_date,
                    strategy_version=INDEX_STRATEGY_VERSION, status='AWAITING_CONFIRMATION', signal_id=plan_row['signal_id'],
                    signal_close=plan_row['signal_close'], threshold_high=plan_row['threshold_high'],
                    atr14=plan_row['atr14'], buy_max=plan_row['buy_max'],
                    initial_stop_reference=plan_row['initial_stop_reference'],
                    trail_trigger_reference=plan_row['trail_trigger_reference'],
                    detail=(f'{next_day} 开盘 {fill.entry_price:.4f} 通过价格条件；'
                            '须核对实际成交、数量与账户现金后经持仓录入确认，不自动建仓'),
                    capacity_check=lambda conn: self._check_plan_capacity(conn,account_id,index_id,series.last_date,plan_row['signal_id']))
                warnings.append(f'{index_id} 计划已通过开盘价格条件，等待人工确认成交')
            except CapacityRefused as exc:
                self.store.clear_plan(account_id,index_id,reason=str(exc))
                return replace(fill,status=FILL_CANCELLED,entry_price=None,executed=False,reason='实际容量不足：'+str(exc))
        elif fill.status == FILL_FILLED:
            warnings.append(f'{index_id} 计划已通过开盘价格条件（只读查询，未写入计划状态）')
        return fill

    def _record_signal(self, account_id: str, index_id: str, cfg, series, plan, regime,
                       factor, combo, basis: str, unit: str, peer_versions: dict, as_of: str, connection=None) -> dict:
        input_version = self.data.evaluation_version(
            index_id, completed_on=as_of, basis=basis, peer_versions=peer_versions)
        return self.store.save_signal(
            index_id=index_id, signal_date=plan.signal_date,
            input_version=input_version,
            strategy_version=INDEX_STRATEGY_VERSION,
            config_version=config_fingerprint(index_id),
            status=plan.status, account_id=account_id, instrument_code=cfg.etf_code,
            price_basis=basis, price_unit=unit,
            reason_codes=[plan.status.lower()], detail=plan.detail,
            breakout_window=plan.breakout_window, signal_close=plan.signal_close,
            threshold_high=plan.threshold_high, atr14=plan.atr14, buy_max=plan.buy_max,
            initial_stop_reference=plan.initial_stop_reference,
            trail_trigger_reference=plan.trail_trigger_reference,
            factor_status=factor.status, factor_detail=factor.detail,
            combo_status=combo.status, combo_detail=combo.detail,
            regime_state=regime.state, regime_er60=regime.er60,
            data_source=series.source, is_proxy=series.is_proxy,
            source_verified=series.source_verified, connection=connection,
        )

    def _near_stop(self, account, position, held, as_of, evidence_revision):
        """Display-only distance from verified current protections; not a model exit."""
        if not position or position.execution_source != SOURCE_LEDGER or not position.is_open or not held:
            return []
        try:
            at=self._decision_time(as_of)
            with self.db.read() as conn:
                if conn.execute('SELECT ledger_revision FROM ledger_state').fetchone()[0]!=evidence_revision:
                    return []
                valid=[l for l in held if l['signal_verified'] and not l['scale_invalid'] and not l['strategy_deviation']]
                if len(valid)!=len(held) or len({l['instrument_id'] for l in valid})!=1: return []
                quote,quality=self.portfolio._current_quote(conn,valid[0]['instrument_id'],utc(at),market_window(at),self.portfolio._revision(conn,None))
                if quality!='valid' or not quote: return []
                price=Decimal(quote['price'])/SCALE
                result=[]
                candidates=[('main',position.stop_price)]+[('sub',a.get('stop_price')) for a in position.adds if a.get('stop_price') is not None]
                for role,stop in candidates:
                    if not any(l['lot_role']==role for l in valid): continue
                    state=stop_distance(price,stop,self.near_stop_bps)
                    if state:
                        result.append({'role':role,'status':state,'stop_price':stop,'quote_price':str(price),'quote_at':quote['effective_at'],'quote_source':quote['source'],'threshold_bps':self.near_stop_bps,'display_only':True})
                return result
        except (CalendarUnavailable,InvalidOperation,TypeError,ValueError):
            return []

    def _position_payload(self, position: Optional[PositionState], decision, cfg,
                          unit: str) -> dict | None:
        if position is None:
            return None
        verified = position.execution_source == SOURCE_LEDGER
        return {
            'index_id': position.index_id, 'entry_date': position.entry_date,
            'entry_price': position.entry_price, 'signal_atr': position.signal_atr,
            'initial_stop': position.initial_stop if verified else None, 'stop_price': position.stop_price if verified else None,
            'trail_trigger': position.trail_trigger if verified else None, 'stage': position.stage,
            'highest_price': position.highest_price, 'holding_days': position.holding_days,
            'exit_after_bars': position.exit_after_bars, 'renewals': position.renewals,
            'e2a_extended_days': position.e2a_extended_days, 'extended': position.extended,
            'add_executed': position.add_executed, 'adds': position.adds if verified else [{**a, 'initial_stop': None, 'stop_price': None} for a in position.adds],
            'unverified_reference': None if verified else {'initial_stop': position.initial_stop, 'adds': position.adds, 'note': '仅供核对公式，不是当前保护指令'},
            'exit_filled': position.exit_filled, 'closed_date': position.closed_date,
            'last_exit_action': position.last_exit_action,
            'last_exit_reason': position.last_exit_reason,
            'pending_stop': position.pending_stop if verified else None,
            'pending_stop_effective_date': position.pending_stop_effective_date if verified else None,
            'price_unit': unit,
            'action': decision.action if decision else (ACTION_HOLD if verified else 'UNVERIFIED'),
            'action_price': decision.price if decision else None,
            'action_reason': decision.reason if decision else '',
            'events': list(decision.events) if decision else [],
            'main_lot_id': position.main_lot_id,
            'shares': position.shares,
            'execution_source': position.execution_source,
            'notes': position.notes if verified else ['未核实信号链接；无可执行保护价或模型动作。', *position.notes],
            'model_only': True,
        }

    @staticmethod
    def _days_since(last_add: Optional[str], current: str) -> Optional[int]:
        if not last_add:
            return None
        try:
            return max(0, (date.fromisoformat(str(current)[:10])
                           - date.fromisoformat(str(last_add)[:10])).days)
        except ValueError:
            return None


def stop_distance(price, stop, threshold_bps):
    if stop is None: return None
    p, s=Decimal(str(price)),Decimal(str(stop))
    if not p.is_finite() or not s.is_finite() or p<=0 or s<=0: return None
    if p<=s: return 'protection_reached'
    if (p-s)*10000<=s*threshold_bps: return 'near_stop'
    return None


def entry_amount(equity: Optional[int], cfg) -> Optional[float]:
    if equity is None or equity <= 0:
        return None
    # T5's independent initial allocation is 15%, not the core 1/8.
    return equity * NDX_AUX_INITIAL_RATIO if cfg.is_us_track else base_position_amount(equity)


def base_amount_ok(equity: Optional[int], cash: Optional[int]) -> bool:
    if equity is None or cash is None:
        return False
    base = base_position_amount(equity)
    return base is not None and cash + 1e-6 >= base


def _midday(day: str) -> datetime:
    return datetime.combine(date.fromisoformat(str(day)[:10]), time(15, 30),
                            ZoneInfo('Asia/Shanghai'))


def _tradable_block_reason(basis: str, cfg, series, live_qty: int) -> str | None:
    """为什么这条信号不能预填真实成交表单（ISS-031：绝不把指数点位 ATR 填成人民币）。"""
    if basis != ETF_UNADJUSTED:
        return 'signal_is_on_index_points_not_etf_prices'
    if not cfg.etf_code:
        return 'configured_index_has_no_etf_mapping'
    if not series.source_verified:
        return 'series_source_unverified'
    if live_qty < 0:
        return 'live_quantity_unavailable'
    return None


def _plan_from_row(row: dict, cfg):
    from backend.domain.strategy.entry import EntryPlan
    return EntryPlan(
        index_id=row['index_id'], name=cfg.name, market=cfg.market,
        signal_date=row['signal_date'], breakout_window=cfg.breakout_window,
        entry_window=cfg.entry_window, signal_close=row['signal_close'],
        threshold_high=row['threshold_high'], atr14=row['atr14'], buy_max=row['buy_max'],
        initial_stop_reference=row['initial_stop_reference'],
        trail_trigger_reference=row['trail_trigger_reference'], status=STATUS_SIGNAL,
    )


def _factor_payload(factor) -> dict:
    return {'key': factor.key, 'label': factor.label, 'status': factor.status,
            'value': factor.value, 'detail': factor.detail, 'verified': factor.verified,
            'blocks_signal': factor.blocks_signal}


def _fill_payload(fill) -> dict | None:
    if fill is None:
        return None
    return {'status': fill.status, 'entry_price': fill.entry_price, 'reason': fill.reason,
            'executed': fill.executed}


__all__ = ['SOURCE_LEDGER', 'SOURCE_UNVERIFIED', 'StrategyCalendarUnavailable',
           'StrategyService', 'base_amount_ok']
