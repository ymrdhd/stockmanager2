"""卖出规则：初始止损、跟踪止盈（峰值回撤比例）、V9 时间退出（续期 / E2a 延持）、冷却。

V9 口径（2026-11-13 冻结）：
- 初始止损 ``StopInitial = 实际成交价 - C x 信号日ATR14``，跳空按开盘价全清。
- 跟踪止盈为**比例**口径：峰值达到 ``成交价 + A x 信号日ATR14`` 后启用，
  档位 ``= 峰值 x (1 - B%)``；峰值只取「截至昨日」的最高价，新档位次日生效。
- 时间退出：基础到期日 = 第 21 个持仓交易日（建仓日计为第 1 日，即满 20 个交易日）：
  1) 先按**原始 N**（``cfg.breakout_window``，不是 0.75N）判断续期；
  2) 不续期但持仓盈利且强趋势（收盘>SMA60、SMA20>SMA60、SMA60 上行）→
     E2a 最多延持 10 个该指数交易日；
  3) 延持期连续两次已完成收盘低于 SMA20 → 下一合法交易时点（次日开盘）退出；
  4) 延持上限按原规则再判一次续期，仍不满足则当日收盘清仓。
- 美股独立轨（NDX，``cfg.is_us_track``）：满 20 个持仓交易日**收盘无条件清仓**，
  不做原始 N 续期、也不套 E2a；止损/跟踪仍按 NDX 自身的 C/A/B。
- 硬止损 / 跳空止损 / 移动止损永远优先于续持。
- 同一交易日多个出口同时成立时取**最低**成交价（保守）。
- 加仓子仓使用各自的买入价、冻结ATR和独立止损；子仓止损时只卖出子仓。
- 主仓止损或到期退出时，同步关闭所有仍未退出的子仓，不允许子仓脱离主仓。

历史语义（v7 及以前，已废弃，代码已删除）：
- 保本档（``breakeven_trigger_atr``）、ATR 距离跟踪（``trail_distance_atr``）；
- v7 到期延长「收盘>MA20 且距峰值回撤<5% 可延到 40 根」；
- v3 及以前的 5/8 日有条件减仓（MFE/浮盈判据）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

from .config import (
    COOLDOWN_TRADING_DAYS,
    E2A_BELOW_MA20_EXIT_STREAK,
    G2_RANGE_COOLDOWN_TRADING_DAYS,
    INDEX_STRATEGY_VERSION,
    REGIME_RANGE,
    TIME_EXIT_SEMANTICS_NOTE,
    IndexStrategyConfig,
)
from .v9_rules import MaSnapshot, strong_trend
from .indicators import ceil_to_tick

ACTION_HOLD = "HOLD"
ACTION_GAP_STOP = "GAP_STOP"
ACTION_STOP = "STOP"
ACTION_EXIT = "EXIT"
ACTION_CLOSED = "CLOSED"
ACTION_ADD_STOP = "ADD_STOP"
ACTION_ADD_GAP_STOP = "ADD_GAP_STOP"

#: 视为「止损类」退出的动作：只有这两类会触发 G2 的延长冷却。
#: 时间退出（正常到期）不在其中——用户明确「正常到期卖出的持仓不延长冷却期」。
STOP_LIKE_ACTIONS = frozenset({ACTION_STOP, ACTION_GAP_STOP})


def is_stop_like_exit(action: Optional[str]) -> bool:
    """该退出动作是否属于普通止损/跳空止损。"""
    return str(action or "") in STOP_LIKE_ACTIONS


def initial_stop_price(cfg: IndexStrategyConfig, entry_price: float, signal_atr: float, *, tick: float = 0.0) -> float:
    """``StopInitial = ActualEntry - k x SignalATR``（按 tick 向上取整，宁可早触发）。"""
    raw = float(entry_price) - float(cfg.initial_stop_atr) * float(signal_atr)
    if tick and float(tick) > 0:
        return ceil_to_tick(raw, float(tick))
    return round(raw, 6)


def trail_trigger_price(cfg: IndexStrategyConfig, entry_price: float, signal_atr: float) -> Optional[float]:
    """跟踪止损的启动价；跟踪止损关闭（启动或回撤比例为空）时返回 None。"""
    if not cfg.trail_enabled:
        return None
    return round(float(entry_price) + float(cfg.trail_trigger_atr) * float(signal_atr), 6)


def trail_drawdown_price(cfg: IndexStrategyConfig, peak: float) -> Optional[float]:
    """比例口径的跟踪档位：``峰值 x (1 - B%)``；未配置比例时返回 None。"""
    pct = cfg.trail_drawdown_pct
    if pct is None:
        return None
    pct = float(pct)
    if pct <= 0 or not math.isfinite(float(peak)) or float(peak) <= 0:
        return None
    return round(float(peak) * (1.0 - pct / 100.0), 6)


@dataclass
class PositionState:
    """单只指数的持仓 / 交易计划状态。"""

    index_id: str
    strategy_version: str = INDEX_STRATEGY_VERSION
    signal_date: str = ""
    entry_date: str = ""
    entry_price: float = 0.0
    signal_atr: float = 0.0
    initial_stop: float = 0.0
    stop_price: float = 0.0
    trail_trigger: Optional[float] = None
    stage: int = 1
    highest_price: float = 0.0
    current_atr: Optional[float] = None
    holding_days: int = 0
    exit_after_bars: int = 0
    #: V9：因「原始 N 突破延续」续期的次数；每次续期把 holding_days 重置为 1。
    renewals: int = 0
    #: V9 E2a：本次续期周期内已延持的交易日数（上限 ``cfg.e2a_max_extend_days``）。
    e2a_extended_days: int = 0
    #: V9 E2a：连续收盘低于 SMA20 的天数（达到 2 即下一开盘退出）。
    below_ma20_streak: int = 0
    #: 是否已进入过 E2a 延持（用于退出原因措辞）。
    extended: bool = False
    #: S1b 盈利加仓子仓：[{date, price, shares, signal_atr, amount, exit_date, exit_price}]。
    #: 子仓不占组合槽位，与主仓分别冻结成交价/ATR/保护止损。
    adds: List[dict] = field(default_factory=list)
    add_executed: bool = False
    add_filled_date: Optional[str] = None
    exit_requested: bool = False
    exit_filled: bool = False
    closed_date: Optional[str] = None
    cooldown_until: Optional[str] = None
    last_trade_date: Optional[str] = None
    #: G2：上次退出的日期 / 动作 / 原因（止损类退出会据此延长冷却）。
    last_exit_date: Optional[str] = None
    last_exit_action: Optional[str] = None
    last_exit_reason: Optional[str] = None
    pending_stop: Optional[float] = None
    pending_stop_effective_date: Optional[str] = None
    pending_action: Optional[str] = None
    pending_action_date: Optional[str] = None
    linked_security_id: Optional[str] = None
    #: 与真实股票账本主仓批次一对一关联，不参与交易信号计算。
    main_lot_id: Optional[str] = None
    shares: float = 0.0
    entry_executed: bool = False
    #: 以前的计划仓默认未核实；只有用户核对成交后才写 ledger_confirmed。
    execution_source: str = "legacy_unverified"
    notes: List[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return bool(self.entry_executed) and not bool(self.exit_filled)

    def to_payload(self) -> dict:
        payload = dict(self.__dict__)
        payload["notes"] = list(self.notes)
        payload["adds"] = [dict(item) for item in (self.adds or [])]
        payload["time_exit_semantics_note"] = TIME_EXIT_SEMANTICS_NOTE
        return payload

    @classmethod
    def from_payload(cls, payload: dict) -> "PositionState":
        data = {k: v for k, v in dict(payload or {}).items() if k in cls.__dataclass_fields__}
        if "notes" in data and data["notes"] is None:
            data["notes"] = []
        if "adds" in data and data["adds"] is None:
            data["adds"] = []
        state = cls(**data)
        state.notes = list(state.notes or [])
        state.adds = [dict(item) for item in (state.adds or []) if isinstance(item, dict)]
        return state


@dataclass
class ExitDecision:
    action: str
    price: Optional[float] = None
    reason: str = ""
    stage_before: int = 1
    stage_after: int = 1
    stop_before: float = 0.0
    stop_after: float = 0.0
    events: List[str] = field(default_factory=list)


def _next_day_stop(new_stop: float, trade_date: str) -> tuple:
    return float(new_stop), str(trade_date)


def renewal_allowed(bar, renewal_threshold: Optional[float]) -> bool:
    """基础到期日续期：**只用原始 N** 的突破价（收盘 > 前 N 日最高价）。

    ``renewal_threshold`` 由调用方按 ``cfg.breakout_window``（原始 N）算好，
    不得传入 T3 的 0.75N 阈值——那会让续期门槛被系统性放宽。
    """
    if renewal_threshold is None:
        return False
    try:
        return float(bar.close) > float(renewal_threshold)
    except (TypeError, ValueError):
        return False


def e2a_extend_allowed(cfg: IndexStrategyConfig, state: "PositionState", bar, snap: MaSnapshot) -> bool:
    """E2a 延持是否可开始/继续：盈利 + 强趋势 + 未超 10 日上限。

    强趋势 = ``Close>SMA60、SMA20>SMA60、SMA60(t)>SMA60(t-10)``；
    ``snap`` 缺失任一条均线时一律返回 False（数据不足按原规则退出，不猜）。
    """
    if not bool(getattr(cfg, "e2a_enabled", False)):
        return False
    max_days = int(getattr(cfg, "e2a_max_extend_days", 0) or 0)
    if int(state.e2a_extended_days) >= max_days:
        return False
    try:
        close = float(bar.close)
        entry = float(state.entry_price)
    except (TypeError, ValueError):
        return False
    if entry <= 0 or close <= entry:
        return False
    return bool(strong_trend(snap))


def _mark_adds_closed(state: "PositionState", trade_date: str, price: float) -> None:
    """主仓退出时同步关闭所有加仓子仓（子仓不允许脱离主仓继续存在）。"""
    for item in state.adds or []:
        if not isinstance(item, dict) or item.get("exit_date"):
            continue
        item["exit_date"] = str(trade_date)
        item["exit_price"] = float(price)
        item["exit_action"] = "MAIN_EXIT"
        item["exit_reason"] = "主仓退出，同时平掉未退出的加仓批次"


def _close_position(
    state: "PositionState", *, trade_date: str, price: float, action: str, reason: str
) -> None:
    """统一的清仓落账：退出标记 + 子仓关闭 + 记录 G2 需要的退出动作/原因。"""
    state.exit_filled = True
    state.closed_date = str(trade_date)
    state.last_exit_date = str(trade_date)
    state.last_exit_action = str(action)
    state.last_exit_reason = str(reason)
    _mark_adds_closed(state, str(trade_date), float(price))



def _advance_add_stops(state: PositionState, bar, cfg: IndexStrategyConfig,
                       *, trade_date: str, next_trading_day: Optional[str],
                       events: List[str]) -> Optional[ExitDecision]:
    """逐笔子仓独立止损；卖出子仓不改变主仓止损/持仓状态。"""
    partial = None
    for add in state.adds or []:
        if not isinstance(add, dict) or add.get("exit_date") or str(trade_date) <= str(add.get("date") or ""):
            continue
        try:
            px, atr, qty = float(add["price"]), float(add["signal_atr"]), float(add["shares"])
        except (KeyError, TypeError, ValueError):
            events.append("加仓批次缺失成交价/冻结ATR/份数，请人工核对止损，不借用主仓成本")
            continue
        if min(px, atr, qty) <= 0:
            events.append("加仓批次冻结ATR无效：独立止损待人工确认")
            continue
        stop = add.get("stop_price")
        if stop is None:
            # 兼容旧加仓记录：只能利用该批已记录的冻结 ATR，不借用主仓的冻结 ATR。
            stop = initial_stop_price(cfg, px, atr)
            add["initial_stop"] = stop
            add["stop_price"] = stop
        pending = add.get("pending_stop")
        effective = str(add.get("pending_stop_effective_date") or "")
        if pending is not None and effective and str(trade_date) >= effective:
            stop = max(float(stop), float(pending))
            add["stop_price"] = stop
            add["pending_stop"] = None
            add["pending_stop_effective_date"] = None
        stop = float(stop)
        if float(bar.open) <= stop:
            action, fill = ACTION_ADD_GAP_STOP, float(bar.open)
            reason = f"加仓批次跳空止损：只卖{qty:g}份加仓，主仓继续持有"
        elif float(bar.low) <= stop:
            action, fill = ACTION_ADD_STOP, stop
            reason = f"加仓批次触及独立止损：只卖{qty:g}份加仓，主仓继续持有"
        else:
            action = ""
        if action:
            add["exit_date"] = str(trade_date)
            add["exit_price"] = fill
            add["exit_action"] = action
            add["exit_reason"] = reason
            events.append(f"{trade_date} {reason} @ {fill:.4f}，加仓成本{px:.4f}，止损{stop:.4f}")
            partial = ExitDecision(action, price=fill, reason=reason, events=events)
            continue
        peak = max(float(add.get("highest_price") or px), float(bar.high))
        add["highest_price"] = peak
        if cfg.trail_enabled and peak >= px + float(cfg.trail_trigger_atr) * atr:
            trail = trail_drawdown_price(cfg, peak)
            pending_value = float(add.get("pending_stop") or 0)
            if trail is not None and trail > max(stop, pending_value) + 1e-9:
                add["pending_stop"] = trail
                add["pending_stop_effective_date"] = str(next_trading_day or trade_date)
    return partial


def advance_position(
    state: PositionState,
    bar,
    cfg: IndexStrategyConfig,
    *,
    trade_date: str,
    current_atr: Optional[float],
    next_trading_day: Optional[str] = None,
    tick: float = 0.0,
    ma: Optional[MaSnapshot] = None,
    renewal_threshold: Optional[float] = None,
) -> ExitDecision:
    """用一根日线推进持仓状态，返回本日应执行的卖出动作。

    - 跳空：开盘价已跌破生效止损 → 按开盘价全清（OPEN_GAP_STOP）。
    - 盘中：最低价触及生效止损 → 按止损价卖出（保守，不按更优价格臆测）。
    - 跟踪档位只在下一交易日生效，且止损永不下降。
    - 时间退出：满 20 个交易日后按 V9 顺序判断「原始N续期 → E2a 延持 → 清仓」；
      止损优先级始终高于续持。
    """
    events: List[str] = []
    stage_before = int(state.stage)
    stop_before = float(state.stop_price)
    previous_trade_date = state.last_trade_date
    if current_atr is not None:
        state.current_atr = float(current_atr)
    state.last_trade_date = str(trade_date)

    if state.exit_filled:
        return ExitDecision(ACTION_CLOSED, reason="已清仓", stage_before=stage_before, stage_after=state.stage,
                            stop_before=stop_before, stop_after=state.stop_price)

    # 1) 陈旧挂单兑现：上一交易日请求的退出在今日开盘执行（模型 v1.0 已无减仓分支）。
    if state.pending_action and state.pending_action_date and str(trade_date) > str(state.pending_action_date):
        action = str(state.pending_action)
        price = float(bar.open)
        if action == ACTION_EXIT and not state.exit_filled:
            reason = "时间退出（次日开盘清仓）"
            _close_position(state, trade_date=trade_date, price=price,
                            action=ACTION_EXIT, reason=reason)
            events.append(f"{trade_date} 开盘执行时间退出，全部清仓 @ {price}")
            return ExitDecision(ACTION_EXIT, price=price, reason=reason,
                                stage_before=stage_before, stage_after=state.stage,
                                stop_before=stop_before, stop_after=state.stop_price, events=events)
        state.pending_action = None
        state.pending_action_date = None

    # 2) 待生效止损在今日生效。
    if state.pending_stop is not None and state.pending_stop_effective_date:
        if str(trade_date) >= str(state.pending_stop_effective_date):
            state.stop_price = max(float(state.stop_price), float(state.pending_stop))
            state.pending_stop = None
            state.pending_stop_effective_date = None

    # 3) 持有日数（含建仓日；同一交易日重复推进不重复计数）。
    if state.entry_date and str(trade_date) >= str(state.entry_date):
        if str(trade_date) == str(state.entry_date):
            state.holding_days = max(int(state.holding_days), 1)
            state.highest_price = max(float(state.highest_price or 0.0), float(bar.high))
        elif str(previous_trade_date or "") != str(trade_date):
            state.holding_days = int(state.holding_days) + 1

    # 4) 跳空止损 / 5) 盘中触及止损。
    #    模型 v1.0：若同一交易日同时到时间退出，按"取最低成交价"保守原则处理。
    exit_bars = int(cfg.exit_after_bars or state.exit_after_bars or 0)
    time_exit_due = bool(exit_bars) and state.holding_days >= exit_bars

    if state.stop_price and float(bar.open) <= float(state.stop_price):
        price = float(bar.open)
        reason = "开盘跳空止损"
        if time_exit_due:
            price = min(price, float(bar.close))
            reason = "开盘跳空止损（同日时间退出，取较低价成交）"
        _close_position(state, trade_date=trade_date, price=price,
                        action=ACTION_GAP_STOP, reason=reason)
        events.append(f"{trade_date} 开盘跳空跌破止损 {state.stop_price}，按 {price} 全清")
        return ExitDecision(ACTION_GAP_STOP, price=price, reason=reason,
                            stage_before=stage_before, stage_after=state.stage,
                            stop_before=stop_before, stop_after=state.stop_price, events=events)

    if state.stop_price and float(bar.low) <= float(state.stop_price):
        price = float(state.stop_price)
        reason = "触及保护止损"
        if time_exit_due:
            price = min(price, float(bar.close))
            reason = "触及保护止损（同日时间退出，取较低价成交）"
        _close_position(state, trade_date=trade_date, price=price,
                        action=ACTION_STOP, reason=reason)
        events.append(f"{trade_date} 盘中触及止损 {state.stop_price}，按 {price} 成交")
        return ExitDecision(ACTION_STOP, price=price, reason=reason,
                            stage_before=stage_before, stage_after=state.stage,
                            stop_before=stop_before, stop_after=state.stop_price, events=events)

    # 主仓硬止损先判定；如未触发，再单独处理加仓的止损。子仓退出不影响主仓续持。
    add_decision = _advance_add_stops(
        state, bar, cfg, trade_date=trade_date,
        next_trading_day=next_trading_day, events=events,
    )

    # 6) 更新最高价并推进止损档位（新档位次日生效）。
    state.highest_price = max(float(state.highest_price or 0.0), float(bar.high))
    entry = float(state.entry_price)

    trail_trigger = (
        entry + float(cfg.trail_trigger_atr) * float(state.signal_atr) if cfg.trail_enabled else None
    )
    if trail_trigger is not None and float(state.highest_price) >= trail_trigger:
        target = trail_drawdown_price(cfg, float(state.highest_price))
        if target is not None and target > max(float(state.stop_price), float(state.pending_stop or 0.0)) + 1e-9:
            effective = next_trading_day or str(trade_date)
            state.pending_stop, state.pending_stop_effective_date = _next_day_stop(target, effective)
            state.trail_trigger = float(state.trail_trigger or trail_trigger)
            state.stage = max(int(state.stage), 3)
            rule = f"峰值回撤{float(cfg.trail_drawdown_pct):g}%"
            events.append(f"{trade_date} 触发跟踪（{rule}，最高价 {state.highest_price:.4f}），"
                          f"止损抬到 {target}，{effective} 起生效")

    # 7) 时间退出（V9）：基础第 21 个持仓交易日 → 原始N续期 → E2a 延持 → 清仓。
    #    美股独立轨：到期即收盘清仓，不续期、不延持（NDX 不套 A 股的到期管理）。
    if time_exit_due and not state.exit_filled and not getattr(cfg, "renewal_enabled", True):
        price = float(bar.close)
        reason = "时间退出（美股独立轨：满20个交易日收盘清仓，不续期不延持）"
        _close_position(state, trade_date=trade_date, price=price,
                        action=ACTION_EXIT, reason=reason)
        events.append(
            f"{trade_date} 已持有 {max(0, int(state.holding_days) - 1)} 个交易日，{reason} @ {price}"
        )
        return ExitDecision(ACTION_EXIT, price=price, reason=reason,
                            stage_before=stage_before, stage_after=state.stage,
                            stop_before=stop_before, stop_after=state.stop_price, events=events)

    if time_exit_due and not state.exit_filled:
        snap = ma or MaSnapshot(close=float(bar.close))
        held = max(0, int(state.holding_days) - 1)
        max_extend = int(getattr(cfg, "e2a_max_extend_days", 0) or 0)

        # 7.1 续期：只用原始 N 的突破价；续期后重新开始一个 20 日周期。
        if renewal_allowed(bar, renewal_threshold):
            state.renewals = int(state.renewals) + 1
            state.holding_days = 1
            state.e2a_extended_days = 0
            state.below_ma20_streak = 0
            state.extended = False
            events.append(
                f"{trade_date} 已持有 {held} 个交易日，收盘 {float(bar.close):.4f} 仍高于"
                f"原始{int(cfg.breakout_window)}日最高 {float(renewal_threshold):.4f}，按原规则续期"
                f"（第{state.renewals}次续期，重新计 20 个交易日）"
            )
        elif state.extended and int(state.e2a_extended_days) >= max_extend:
            # 7.5 延持上限：先判续期（上面已判），仍不满足则收盘清仓。
            price = float(bar.close)
            reason = f"E2a 延持已满{max_extend}个交易日且不满足原始N续期，收盘清仓"
            _close_position(state, trade_date=trade_date, price=price,
                            action=ACTION_EXIT, reason=reason)
            events.append(f"{trade_date} {reason} @ {price}")
            return ExitDecision(ACTION_EXIT, price=price, reason=reason,
                                stage_before=stage_before, stage_after=state.stage,
                                stop_before=stop_before, stop_after=state.stop_price, events=events)
        elif e2a_extend_allowed(cfg, state, bar, snap):
            # 7.3 盈利 + 强趋势 → 延持 1 个交易日。
            state.e2a_extended_days = int(state.e2a_extended_days) + 1
            state.extended = True
            close = float(bar.close)
            ma20 = snap.sma20
            if ma20 is not None and close < float(ma20):
                state.below_ma20_streak = int(state.below_ma20_streak) + 1
            else:
                state.below_ma20_streak = 0
            if int(state.below_ma20_streak) >= E2A_BELOW_MA20_EXIT_STREAK:
                # 7.4 连续两次已完成收盘低于 SMA20 → 下一合法交易时点（次日开盘）退出。
                state.pending_action = ACTION_EXIT
                state.pending_action_date = str(trade_date)
                events.append(
                    f"{trade_date} E2a 连续{int(state.below_ma20_streak)}次收盘低于 SMA20 "
                    f"{float(ma20):.4f}，下一交易日开盘执行退出"
                )
            else:
                events.append(
                    f"{trade_date} E2a 延持 第{state.e2a_extended_days}/{max_extend}日"
                    f"（盈利且强趋势，收盘 {close:.4f} / SMA60 "
                    f"{'--' if snap.sma60 is None else f'{float(snap.sma60):.4f}'}）"
                )
        else:
            # 7.6 既不续期也不能延持 → 当日收盘清仓。
            price = float(bar.close)
            if state.extended:
                reason = "E2a 延持条件失效（趋势/盈利不再满足），收盘清仓"
            else:
                reason = "时间退出（满20个交易日且不满足原始N续期，收盘清仓）"
            _close_position(state, trade_date=trade_date, price=price,
                            action=ACTION_EXIT, reason=reason)
            events.append(f"{trade_date} 已持有 {held} 个交易日，{reason} @ {price}")
            return ExitDecision(ACTION_EXIT, price=price, reason=reason,
                                stage_before=stage_before, stage_after=state.stage,
                                stop_before=stop_before, stop_after=state.stop_price, events=events)

    if add_decision is not None:
        add_decision.stage_before = stage_before
        add_decision.stage_after = state.stage
        add_decision.stop_before = stop_before
        add_decision.stop_after = state.stop_price
        return add_decision
    return ExitDecision(
        ACTION_HOLD,
        reason="持有",
        stage_before=stage_before,
        stage_after=state.stage,
        stop_before=stop_before,
        stop_after=state.stop_price,
        events=events,
    )


def cooling_complete(state: PositionState, trade_date: str) -> bool:
    """冷却期是否已结束（``cooldown_until`` 为最早可再进场交易日）。"""
    if not state.cooldown_until:
        return True
    return str(trade_date) >= str(state.cooldown_until)


def cooldown_until_date(
    exit_date: str,
    trading_days_after: List[str],
    required_days: int = COOLDOWN_TRADING_DAYS,
) -> Optional[str]:
    """给定退出日与随后的交易日序列，返回最早可再进场的交易日。

    ``trading_days_after`` 必须按升序排列且只包含退出日之后的交易日。
    冷却要求 ``required_days`` 个**完整**交易日（V9 默认 3；G2 震荡市止损后为 10），
    因此最早可再进场是其后第 ``required_days + 1`` 个交易日。
    """
    if not trading_days_after:
        return None
    try:
        need = int(required_days)
    except (TypeError, ValueError):
        need = COOLDOWN_TRADING_DAYS
    need = max(1, need)
    if need < len(trading_days_after):
        return str(trading_days_after[need])
    return None


def cooldown_required_days(*, regime_state: str, exit_action: Optional[str]) -> int:
    """G2-1：本次退出要冷却几个完整交易日。

    - ``RANGE`` 震荡市 + 普通止损/跳空止损 → ``G2_RANGE_COOLDOWN_TRADING_DAYS``（10）；
    - 其余（非震荡市，或正常到期/时间退出）→ 原 ``COOLDOWN_TRADING_DAYS``（3）。
    """
    if str(regime_state or "") == REGIME_RANGE and is_stop_like_exit(exit_action):
        return max(COOLDOWN_TRADING_DAYS, int(G2_RANGE_COOLDOWN_TRADING_DAYS))
    return int(COOLDOWN_TRADING_DAYS)


__all__ = [
    "ACTION_CLOSED",
    "ACTION_ADD_STOP",
    "ACTION_ADD_GAP_STOP",
    "ACTION_EXIT",
    "ACTION_GAP_STOP",
    "ACTION_HOLD",
    "ACTION_STOP",
    "ExitDecision",
    "PositionState",
    "STOP_LIKE_ACTIONS",
    "advance_position",
    "cooling_complete",
    "cooldown_required_days",
    "cooldown_until_date",
    "e2a_extend_allowed",
    "initial_stop_price",
    "is_stop_like_exit",
    "renewal_allowed",
    "trail_drawdown_price",
    "trail_trigger_price",
]
