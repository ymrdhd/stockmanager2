"""V9/G2 策略应用服务：观察池、持仓决策、历史回放。

调用链（与旧 ``service.evaluate_index`` 等价，但不读写旧 kv 账本）：

1. 取指数点位日线（G2 基准与 V9 突破都用**指数点位**）；
2. 有持仓 → 用已完成日线逐日推进 ``advance_position``；
3. 有待执行计划 → 用下一交易日开盘价 ``evaluate_fill``；
4. 无持仓 → ``evaluate_entry`` 判定信号，再按现金/组合槽位决定是否保留计划；
5. 汇总 G2 市场状态、因子、组合因子、盈利加仓、NDX 辅助仓、冷却。

所有"建议"都必须带价格口径与单位；任何一环缺数据就降级为 ``DATA_UNAVAILABLE``
并写清原因，绝不编造行情或把模型价格当成券商成交。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import logging
from typing import Optional

from backend.application.market_calendar import CalendarUnavailable, market_window
from backend.application.portfolio import PortfolioService
from backend.application.strategy.series import (
    ETF_ADJUSTED, INDEX_BASIS, SeriesError, StrategyData, series_summary,
)
from backend.application.strategy.store import StrategyStore
from backend.application.trading.service import cash_balance, one, rows, utc
from backend.domain.strategy.config import (
    INDEX_STRATEGY_VERSION, MARKET_CN, PORTFOLIO_MAX_POSITIONS, all_configs, get_config,
)
from backend.domain.strategy.entry import (
    STATUS_COOLDOWN, STATUS_DATA_UNAVAILABLE, STATUS_NO_SIGNAL, STATUS_PLAN_PENDING,
    STATUS_PORTFOLIO_FULL, STATUS_POSITION_OPEN, STATUS_SIGNAL, evaluate_entry, evaluate_fill,
    FILL_CANCELLED, FILL_FILLED,
)
from backend.domain.strategy.exit import (
    ACTION_CLOSED, ACTION_HOLD, PositionState, advance_position, cooldown_required_days,
    cooldown_until_date, initial_stop_price, trail_trigger_price,
)
from backend.domain.strategy.factor_combo import evaluate_combo
from backend.domain.strategy.factors import evaluate_factor
from backend.domain.strategy.indicators import compute_atr14
from backend.domain.strategy.calendars import market_clock
from backend.domain.strategy.market_data import validate_series
from backend.domain.strategy.regime import (
    REGIME_SHORT, RegimeSnapshot, market_regime,
)
from backend.domain.strategy.symbols import (
    configured_index_ids, etf_code_of, normalize_index_id, peers_of,
)
from backend.domain.strategy.v9_rules import (
    base_position_amount, evaluate_ndx_aux, evaluate_pyramid, ma_snapshot,
)
from backend.infrastructure.database import Database

logger = logging.getLogger(__name__)

INDEX_TICK = 0.001


def _payload_key(value) -> Optional[str]:
    return None if value is None else str(value)


class StrategyService:
    """一个账户视角下的 V9/G2 观察池与持仓决策。"""

    def __init__(self, db: Database, *, data: StrategyData | None = None,
                 store: StrategyStore | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
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
                series_map[index_id] = self.data.index_series(
                    index_id, completed_on=as_of, allow_network=allow_network)
            except SeriesError as exc:
                warnings.append(f'{index_id}:{exc.reason}')
        snapshot = market_regime(series_map, as_of=as_of)
        if warnings and not snapshot.warning:
            snapshot = RegimeSnapshot(
                state=snapshot.state, er60=snapshot.er60, change_pct=snapshot.change_pct,
                as_of=snapshot.as_of, components=snapshot.components, points=snapshot.points,
                warning='；'.join(warnings[:6]))
        return snapshot

    def pool(self, account_id: str, *, as_of: str | None = None,
             allow_network: bool = True) -> dict:
        """策略观察池：每只指数的状态行 + 组合层汇总。"""
        day = self._as_of(as_of)
        regime = self.regime(as_of=day, allow_network=allow_network)
        equity, cash, quality, reasons = self._equity(account_id, day)
        base_amount = base_position_amount(equity)
        open_rows = self.store.open_positions(account_id)
        core_used = sum(1 for row in open_rows if get_config(row['index_id']).counts_against_core_slots)
        items = []
        for index_id in configured_index_ids():
            items.append(self.evaluate(index_id, account_id=account_id, as_of=day,
                                       regime=regime, equity=equity, cash=cash,
                                       core_used=core_used, allow_network=allow_network))
        return {
            'as_of': day,
            'strategy_version': INDEX_STRATEGY_VERSION,
            'regime': self._regime_payload(regime),
            'equity_fen': equity, 'cash_fen': cash, 'equity_quality': quality,
            'equity_reasons': reasons,
            'base_amount_fen': base_amount,
            'core_positions': core_used, 'max_positions': PORTFOLIO_MAX_POSITIONS,
            'items': items,
            'data': {
                'allow_network': allow_network,
                'price_basis': INDEX_BASIS,
                'note': '指数点位用于信号；ETF 复权价只用于研究口径；真实市值用未复权价。',
            },
        }

    def evaluate(self, index_id: str, *, account_id: str, as_of: str,
                 regime: RegimeSnapshot | None = None, equity: int | None = None,
                 cash: int | None = None, core_used: int = 0,
                 allow_network: bool = True) -> dict:
        canonical = normalize_index_id(index_id)
        cfg = get_config(canonical)
        clock = market_clock(cfg.clock_market)
        warnings: list[str] = []
        try:
            series = self.data.index_series(canonical, completed_on=as_of,
                                            allow_network=allow_network)
        except SeriesError as exc:
            return {'index_id': canonical, 'name': cfg.name, 'status': STATUS_DATA_UNAVAILABLE,
                    'detail': f'指数点位行情不可用：{exc.reason}',
                    'reason_codes': ['index_series_unavailable'], 'price_unit': cfg.price_unit,
                    'etf_code': cfg.etf_code, 'warnings': [str(exc.detail or exc.reason)],
                    'signal_date': '', 'series': {'available': False}}
        if series.warning:
            warnings.append(series.warning)
        if not series.source_verified:
            warnings.append('指数行情来源未核实，仅作参考')

        regime = regime or self.regime(as_of=series.last_date, allow_network=allow_network)
        if equity is None or cash is None:
            equity, cash, _quality, _reasons = self._equity(account_id, as_of)

        position = self.store.load_position(account_id, canonical)
        exit_decision = None
        if position is not None and position.is_open:
            position, exit_decision, warnings = self._advance(
                position, series, cfg, clock, warnings)
            self.store.save_position(account_id, position)
            if position.exit_filled:
                self._finish_cooldown(account_id, canonical, position, clock, regime)

        fill = None
        plan_row = self.store.active_plan(canonical)
        if plan_row is not None and not (position and position.is_open):
            fill = self._evaluate_pending_plan(canonical, cfg, series, plan_row, clock, warnings)

        cooldown = self._cooldown_state(account_id, canonical, series.last_date, clock,
                                        regime.state)
        has_open = bool(position and position.is_open)
        live_qty = self._live_quantity(account_id, canonical, as_of)
        if live_qty > 0:
            has_open = True

        peers = {}
        if cfg.factor_key:
            peers = self._peer_series(canonical, as_of, allow_network)
        factor = evaluate_factor(cfg, series, peers)
        combo = evaluate_combo(cfg, series)
        plan = evaluate_entry(
            cfg, series, as_of=as_of, factor=factor, combo=combo,
            cooldown_active=bool(cooldown['active']), cooldown_note=cooldown['note'],
            has_open_position=has_open, has_pending_plan=bool(plan_row) and not has_open,
            open_position_count=core_used,
            max_positions=PORTFOLIO_MAX_POSITIONS if cfg.counts_against_core_slots else 0,
            tick=INDEX_TICK,
        )
        status = plan.status
        detail = plan.detail
        reason_codes = [status.lower()]
        if status == STATUS_SIGNAL and not base_amount_ok(equity, cash):
            base = base_position_amount(equity)
            status = 'CASH_BLOCKED'
            if cash is None or base is None:
                detail = detail + '；账户可用现金或权益无法核实，不得生成买入计划'
                reason_codes.append('cash_unverified')
            else:
                detail = (detail + f'；账本可用现金 ¥{cash/100:,.2f} 不足基础仓目标 '
                                   f'¥{base/100:,.2f}，不得生成买入计划')
                reason_codes.append('cash_shortfall')

        if status == STATUS_SIGNAL:
            signal_row = self._record_signal(account_id, canonical, cfg, series, plan, regime,
                                             factor, combo)
            self.store.save_plan(
                index_id=canonical, signal_date=plan.signal_date,
                strategy_version=INDEX_STRATEGY_VERSION, status=STATUS_SIGNAL,
                signal_id=signal_row['signal_id'], account_id=account_id,
                signal_close=plan.signal_close, threshold_high=plan.threshold_high,
                atr14=plan.atr14, buy_max=plan.buy_max,
                initial_stop_reference=plan.initial_stop_reference,
                trail_trigger_reference=plan.trail_trigger_reference, detail=plan.detail,
            )
        elif fill is not None and fill.status == FILL_CANCELLED:
            self.store.clear_plan(canonical, reason=fill.reason)

        pyramid = None
        ndx_aux = None
        if position is not None and position.is_open:
            ma = ma_snapshot(list(series.closes), close=float(series.closes[-1]))
            pyramid = evaluate_pyramid(
                cfg, position, close=float(series.closes[-1]), snap=ma,
                prior_closes=list(series.closes[:-1]), holding_days=position.holding_days,
                regime_state=regime.state, regime_label=regime.label,
            ).to_payload()
            if cfg.is_us_track:
                aux = self.store.load_ndx_aux(account_id)
                ndx_aux = evaluate_ndx_aux(
                    list(series.closes), equity=(equity or 0) / 100.0,
                    current_ratio=float(aux.get('ratio') or 0.0),
                    days_since_add=self._days_since(aux.get('last_add_date'), series.last_date),
                    last_date=series.last_date,
                ).to_payload()

        atr = compute_atr14(series.highs, series.lows, series.closes)
        price_now = float(series.closes[-1])
        gap = None
        if plan.threshold_high is not None and atr:
            gap_abs = float(plan.threshold_high) - price_now
            gap = {'abs': gap_abs, 'pct': gap_abs / price_now * 100 if price_now else None,
                   'atr_multiple': gap_abs / atr}
        return {
            'index_id': canonical, 'name': cfg.name, 'market': cfg.market,
            'track': cfg.track_label, 'etf_code': cfg.etf_code,
            'price_unit': cfg.price_unit, 'price_digits': cfg.price_digits,
            'status': status, 'detail': detail, 'reason_codes': reason_codes,
            'signal_date': plan.signal_date,
            'signal_close': plan.signal_close, 'threshold_high': plan.threshold_high,
            'atr14': plan.atr14, 'buy_max': plan.buy_max,
            'entry_window': plan.entry_window, 'breakout_window': cfg.breakout_window,
            'factor': _factor_payload(factor), 'combo': combo.to_payload(),
            'regime': self._regime_payload(regime),
            'cooldown': cooldown, 'gap': gap,
            'fill': _fill_payload(fill),
            'position': self._position_payload(position, exit_decision, cfg),
            'pyramid': pyramid, 'ndx_aux': ndx_aux,
            'live_quantity': live_qty,
            'series': series_summary(series), 'warnings': warnings,
            'price_basis': INDEX_BASIS,
        }

    def position_rows(self, account_id: str, *, as_of: str | None = None,
                      allow_network: bool = True) -> dict:
        """持仓决策视图：真实持仓 + 策略建议 + 未核对证据。"""
        day = self._as_of(as_of)
        rows_ = []
        for record in self.store.open_positions(account_id):
            item = self.evaluate(record['index_id'], account_id=account_id, as_of=day,
                                 allow_network=allow_network)
            rows_.append(item)
        custody = self._custody(account_id, day)
        return {
            'as_of': day, 'account_id': account_id, 'strategy_version': INDEX_STRATEGY_VERSION,
            'items': rows_, 'custody': custody,
            'note': '策略建议不等于真实成交；真实主仓/子仓/退出只能经 TradeService 确认。',
        }

    def _custody(self, account_id: str, as_of: str) -> list[dict]:
        """真实账本中带策略标记的批次（主仓/子仓），用于和策略建议对照。"""
        cutoff = utc(_midday(as_of))
        with self.db.read() as conn:
            return rows(conn, '''SELECT l.id AS lot_id, l.lot_role, l.strategy_id, l.signal_date,
                       l.frozen_atr, i.code, i.market, i.kind,
                       l.opened_qty-COALESCE(SUM(a.quantity),0) AS remaining
                       FROM trade_lots l
                       LEFT JOIN trade_executions t ON t.id=l.open_execution_id
                       LEFT JOIN opening_positions o ON o.id=l.opening_id
                       JOIN instruments i ON i.id=COALESCE(t.instrument_id,o.instrument_id)
                       LEFT JOIN lot_allocations a ON a.lot_id=l.id
                       WHERE COALESCE(t.account_id,o.account_id)=?
                         AND COALESCE(t.executed_at,o.occurred_at)<=?
                         AND l.lot_role IN ('main','sub')
                       GROUP BY l.id HAVING remaining>0 ORDER BY i.code, l.rowid''',
                       (account_id, cutoff))

    # -- 内部 ------------------------------------------------------------
    def current_day(self, as_of: str | None = None) -> str:
        """已完成交易日：默认取本地窗口最近一个已收盘交易日。"""
        return self._as_of(as_of)

    def _as_of(self, as_of: str | None) -> str:
        if as_of:
            return str(as_of)[:10]
        try:
            return market_window(self.clock()).latest_completed.isoformat()
        except CalendarUnavailable:
            return self.clock().astimezone().date().isoformat()

    def _regime_payload(self, regime: RegimeSnapshot) -> dict:
        return {'state': regime.state, 'label': regime.label, 'short': REGIME_SHORT.get(regime.state, '--'),
                'er60': regime.er60, 'change_pct': regime.change_pct, 'components': regime.components,
                'points': regime.points, 'as_of': regime.as_of, 'warning': regime.warning,
                'known': regime.known}

    def _equity(self, account_id: str, as_of: str) -> tuple[int | None, int | None, str, list[str]]:
        try:
            valuation = self.portfolio.valuation(account_id, _midday(as_of))
        except Exception as exc:  # noqa: BLE001 - 降级为不可用，不编造权益
            return None, None, 'unavailable', [f'equity_unavailable:{type(exc).__name__}']
        return (valuation.get('equity_fen'), valuation.get('cash_fen'),
                valuation.get('quality', 'derived_unverified'), list(valuation.get('reasons') or []))

    def _peer_series(self, index_id: str, as_of: str, allow_network: bool) -> dict:
        peers = {}
        for peer in peers_of(index_id):
            try:
                peer_cfg = get_config(peer)
                series = self.data.index_series(peer, completed_on=as_of,
                                                allow_network=allow_network)
                peers[peer_cfg.etf_code or peer] = {'closes': list(series.closes)}
            except (SeriesError, KeyError):
                continue
        return peers

    def _live_quantity(self, account_id: str, index_id: str, as_of: str) -> int:
        """真实账本里该指数对应 ETF 的剩余持仓（策略槽位与合计口径都用它）。

        ``as_of`` 必须是本次判定的信号日，不能用"当前时刻"：历史回放时
        读取信号日之后的持仓就是使用未来数据。
        """
        code = etf_code_of(index_id)
        if not code:
            return 0
        cutoff = utc(_midday(as_of))
        with self.db.read() as conn:
            instrument = rows(conn, 'SELECT id FROM instruments WHERE code=?', (code,))
            if not instrument:
                return 0
            lots = self.portfolio._lots(conn, account_id, cutoff)
        return int(sum(lot['quantity'] for lot in lots
                       if lot['instrument_id'] == instrument[0]['id'] and lot['quantity'] > 0))

    def _advance(self, position: PositionState, series, cfg, clock, warnings: list[str]):
        start_after = position.last_trade_date or position.entry_date
        pending = [bar for bar in series.bars if str(bar.date) > str(start_after)]
        if not pending and position.entry_date == series.last_date:
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
            decision = advance_position(
                position, bar, cfg, trade_date=str(bar.date), current_atr=atr,
                next_trading_day=clock.next_trading_day(bar.date), tick=INDEX_TICK,
                ma=snap, renewal_threshold=threshold,
            )
            if position.exit_filled:
                break
        if decision is None:
            decision = advance_position(
                position, series.bars[-1], cfg, trade_date=str(series.bars[-1].date),
                current_atr=None, tick=INDEX_TICK,
                next_trading_day=clock.next_trading_day(series.bars[-1].date))
        if decision.events:
            warnings.extend(decision.events)
        return position, decision, warnings

    def _finish_cooldown(self, account_id: str, index_id: str, position: PositionState,
                         clock, regime: RegimeSnapshot) -> None:
        required = cooldown_required_days(regime_state=regime.state,
                                          exit_action=position.last_exit_action)
        days = clock.trading_days_after(position.closed_date or position.last_trade_date,
                                        required + 6)
        until = cooldown_until_date(position.closed_date or position.last_trade_date or '',
                                    days, required_days=required)
        position.cooldown_until = until
        self.store.save_cooldown(
            account_id, index_id, exit_date=position.closed_date or '',
            exit_action=position.last_exit_action or ACTION_CLOSED,
            exit_reason=position.last_exit_reason or '', required_days=required,
            cooldown_until=until, regime='SIGNAL_DATE_DYNAMIC', extended=False)

    def _cooldown_state(self, account_id: str, index_id: str, last_date: str, clock,
                        regime_state: str) -> dict:
        row = self.store.load_cooldown(account_id, index_id)
        if not row:
            return {'active': False, 'until': None, 'remaining': 0, 'note': ''}
        required = cooldown_required_days(regime_state=regime_state,
                                          exit_action=row['exit_action'])
        days = clock.trading_days_after(row['exit_date'], required + 6)
        until = cooldown_until_date(row['exit_date'], days, required_days=required)
        active = bool(until) and str(last_date) < str(until)
        remaining = sum(1 for day in days if str(last_date) < day < str(until)) if until else 0
        note = ''
        if active:
            label = 'G2-1 震荡市延长' if required > 3 else '止损冷却'
            note = (f'{label}（{row["exit_reason"] or row["exit_action"]}）：需 {required} 个完整交易日，'
                    f'剩余 {remaining} 个，最早 {until} 可再买')
        return {'active': active, 'until': until, 'remaining': remaining, 'note': note,
                'required_days': required, 'exit_date': row['exit_date'],
                'exit_action': row['exit_action']}

    def _evaluate_pending_plan(self, index_id: str, cfg, series, plan_row: dict, clock,
                               warnings: list[str]):
        signal_date = plan_row['signal_date']
        next_day = clock.next_trading_day(signal_date)
        bar = next((item for item in series.bars if item.date == next_day), None)
        if bar is None:
            return None
        plan = _plan_from_row(plan_row, cfg)
        fill = evaluate_fill(plan, bar)
        if fill.status == FILL_FILLED:
            self.store.save_plan(
                index_id=index_id, signal_date=signal_date, strategy_version=INDEX_STRATEGY_VERSION,
                status='AWAITING_CONFIRMATION', account_id=plan_row['account_id'],
                signal_close=plan_row['signal_close'], threshold_high=plan_row['threshold_high'],
                atr14=plan_row['atr14'], buy_max=plan_row['buy_max'],
                initial_stop_reference=plan_row['initial_stop_reference'],
                trail_trigger_reference=plan_row['trail_trigger_reference'],
                detail=(f'{next_day} 开盘 {fill.entry_price:.4f} 通过价格条件；'
                        '须核对实际成交、数量与账户现金后经持仓录入确认，不自动建仓'))
            warnings.append(f'{index_id} 计划已通过开盘价格条件，等待人工确认成交')
        return fill

    def _record_signal(self, account_id: str, index_id: str, cfg, series, plan, regime,
                       factor, combo) -> dict:
        return self.store.save_signal(
            index_id=index_id, signal_date=plan.signal_date,
            input_version=self.data.input_version(index_id, completed_on=series.last_date),
            strategy_version=INDEX_STRATEGY_VERSION, config_version=INDEX_STRATEGY_VERSION,
            status=plan.status, account_id=account_id, instrument_code=cfg.etf_code,
            price_basis=INDEX_BASIS, price_unit=cfg.price_unit,
            reason_codes=[plan.status.lower()], detail=plan.detail,
            breakout_window=plan.breakout_window, signal_close=plan.signal_close,
            threshold_high=plan.threshold_high, atr14=plan.atr14, buy_max=plan.buy_max,
            initial_stop_reference=plan.initial_stop_reference,
            trail_trigger_reference=plan.trail_trigger_reference,
            factor_status=factor.status, factor_detail=factor.detail,
            combo_status=combo.status, combo_detail=combo.detail,
            regime_state=regime.state, regime_er60=regime.er60,
            data_source=series.source, is_proxy=series.is_proxy,
            source_verified=series.source_verified,
        )

    def _position_payload(self, position: Optional[PositionState], decision, cfg) -> dict | None:
        if position is None:
            return None
        return {
            'index_id': position.index_id, 'entry_date': position.entry_date,
            'entry_price': position.entry_price, 'signal_atr': position.signal_atr,
            'initial_stop': position.initial_stop, 'stop_price': position.stop_price,
            'trail_trigger': position.trail_trigger, 'stage': position.stage,
            'highest_price': position.highest_price, 'holding_days': position.holding_days,
            'exit_after_bars': position.exit_after_bars, 'renewals': position.renewals,
            'e2a_extended_days': position.e2a_extended_days, 'extended': position.extended,
            'add_executed': position.add_executed, 'adds': position.adds,
            'exit_filled': position.exit_filled, 'closed_date': position.closed_date,
            'last_exit_action': position.last_exit_action,
            'last_exit_reason': position.last_exit_reason,
            'pending_stop': position.pending_stop,
            'pending_stop_effective_date': position.pending_stop_effective_date,
            'price_unit': cfg.price_unit,
            'action': decision.action if decision else ACTION_HOLD,
            'action_price': decision.price if decision else None,
            'action_reason': decision.reason if decision else '',
            'events': list(decision.events) if decision else [],
            'main_lot_id': position.main_lot_id,
            'execution_source': position.execution_source,
        }

    @staticmethod
    def _days_since(last_add: Optional[str], current: str) -> Optional[int]:
        if not last_add:
            return None
        try:
            from datetime import date as _date
            return max(0, (_date.fromisoformat(str(current)[:10])
                           - _date.fromisoformat(str(last_add)[:10])).days)
        except ValueError:
            return None


def base_amount_ok(equity: Optional[int], cash: Optional[int]) -> bool:
    if equity is None or cash is None:
        return False
    base = base_position_amount(equity)
    return base is not None and cash + 1e-6 >= base


def _midday(day: str) -> datetime:
    from datetime import date as _date, time
    from zoneinfo import ZoneInfo
    return datetime.combine(_date.fromisoformat(str(day)[:10]), time(15, 30),
                            ZoneInfo('Asia/Shanghai'))


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


__all__ = ['StrategyService', 'base_amount_ok']
