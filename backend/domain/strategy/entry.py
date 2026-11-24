"""买入信号（V9 T3）：首次突破窗口、ATR 冻结、次日最高允许买入价。

规则：
- **T3**：首次买入的突破窗口 = ``Nentry = max(1, floor(0.75 × 原始N))``；
  原始 N 保留在 ``cfg.breakout_window``（续期判定只用它，不得用 0.75N）。
- ``Close[T] > MAX(High[T-Nentry : T-1])``，窗口严格不含 T 当日最高价。
- ``BuyMax = SignalClose + 0.5 x SignalATR14``，在信号确认时冻结。
- 下一有效交易日：开盘价 <= BuyMax 才允许买入；开盘价 > BuyMax 取消本次计划；
  缺失开盘价时保持未执行，绝不虚构成交。
- 组合层：最多 8 个不同核心指数；第 9 个一律拒绝（子仓不占槽位）。
- 冷却：初始止损后 3 个完整交易日；G2 起「RANGE 震荡市 + 止损退出」延长到 10 个
  有效交易日（实现见 ``exit.cooldown_required_days``，本模块只负责给出拒绝状态）。
- 不得因为盘中曾低于 BuyMax 就在已经高开越限后追买。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import (
    BUY_MAX_ATR_MULTIPLIER,
    FACTOR_COMBO_GATE,
    PORTFOLIO_MAX_POSITIONS,
    IndexStrategyConfig,
)
from .factor_combo import ComboResult, evaluate_combo
from .factors import FactorResult, evaluate_factor
from .factors import STATUS_UNVERIFIED as FACTOR_STATUS_UNVERIFIED
from .exit import initial_stop_price, trail_trigger_price
from .indicators import breakout_threshold, compute_atr14
from .market_data import IndexSeries, validate_series
from .v9_rules import t3_entry_window

STATUS_SIGNAL = "SIGNAL"
STATUS_NO_SIGNAL = "NO_SIGNAL"
STATUS_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
STATUS_FACTOR_BLOCKED = "FACTOR_BLOCKED"
STATUS_FACTOR_UNVERIFIED = "FACTOR_UNVERIFIED"
STATUS_COOLDOWN = "COOLDOWN"
STATUS_POSITION_OPEN = "POSITION_OPEN"
STATUS_PLAN_PENDING = "PLAN_PENDING"
#: V9：已有 8 个不同核心指数仓位时，第 9 个一律拒绝。
STATUS_PORTFOLIO_FULL = "PORTFOLIO_FULL"

FILL_FILLED = "FILLED"
FILL_CANCELLED = "CANCELLED"
FILL_PENDING = "PENDING"
FILL_NO_PLAN = "NO_PLAN"


@dataclass(frozen=True)
class EntryPlan:
    """一次买入计划（信号确认后的不可变快照）。"""

    index_id: str
    name: str
    market: str
    signal_date: str
    breakout_window: int
    signal_close: Optional[float] = None
    threshold_high: Optional[float] = None
    atr14: Optional[float] = None
    buy_max: Optional[float] = None
    initial_stop_reference: Optional[float] = None
    trail_trigger_reference: Optional[float] = None
    factor: Optional[FactorResult] = None
    combo: Optional[ComboResult] = None
    status: str = STATUS_DATA_UNAVAILABLE
    detail: str = ""
    data_source: str = ""
    is_proxy: bool = False
    source_verified: bool = True
    #: V9 T3：首次买入实际使用的突破窗口 ``max(1, floor(0.75 × 原始N))``。
    #: ``breakout_window`` 仍是原始 N（续期判定与展示都用它）。
    entry_window: int = 0

    @property
    def actionable(self) -> bool:
        return self.status == STATUS_SIGNAL

    def to_payload(self) -> dict:
        payload = dict(self.__dict__)
        factor = payload.get("factor")
        payload["factor"] = {
            "key": factor.key,
            "label": factor.label,
            "status": factor.status,
            "value": factor.value,
            "detail": factor.detail,
            "verified": factor.verified,
        } if factor is not None else None
        combo = payload.get("combo")
        payload["combo"] = combo.to_payload() if combo is not None else None
        return payload


@dataclass(frozen=True)
class FillResult:
    status: str
    entry_price: Optional[float] = None
    reason: str = ""
    executed: bool = False


def evaluate_entry(
    cfg: IndexStrategyConfig,
    series: Optional[IndexSeries],
    *,
    as_of: Optional[str] = None,
    factor: Optional[FactorResult] = None,
    combo: Optional[ComboResult] = None,
    peer_series: Optional[dict] = None,
    cooldown_active: bool = False,
    cooldown_note: str = "",
    has_open_position: bool = False,
    has_pending_plan: bool = False,
    open_position_count: int = 0,
    max_positions: int = PORTFOLIO_MAX_POSITIONS,
    tick: float = 0.0,
) -> EntryPlan:
    """评估单只指数的买入信号（V9 T3；美股独立轨直接用自身 N）。"""
    # 窗口由配置层按轨道给出：A 股轨 = floor(0.75N)，美股轨 = 自身 N。
    entry_window = int(getattr(cfg, "entry_window", 0) or t3_entry_window(cfg.breakout_window))
    us_track = bool(getattr(cfg, "is_us_track", False))
    window_label = (f"美股独立轨 {entry_window} 日（=原始N，不乘0.75）"
                    if us_track else f"T3 首次窗口 {entry_window} 日")
    if series is None or not series.bars:
        return EntryPlan(
            index_id=cfg.index_id,
            name=cfg.name,
            market=cfg.market,
            signal_date="",
            breakout_window=cfg.breakout_window,
            status=STATUS_DATA_UNAVAILABLE,
            detail="行情数据不可用，无法判断突破",
        )

    working = series.truncated(as_of) if as_of else series
    if not working.bars:
        return EntryPlan(
            index_id=cfg.index_id,
            name=cfg.name,
            market=cfg.market,
            signal_date="",
            breakout_window=cfg.breakout_window,
            status=STATUS_DATA_UNAVAILABLE,
            detail=f"目标交易日 {as_of} 之前没有可用行情",
        )

    if working.is_proxy:
        # ETF 代理的人民币价格不是指数点位，绝不能拿来做指数突破信号。
        return EntryPlan(
            index_id=cfg.index_id,
            name=cfg.name,
            market=cfg.market,
            signal_date=working.last_date or "",
            breakout_window=cfg.breakout_window,
            status=STATUS_DATA_UNAVAILABLE,
            detail=f"行情是 ETF 代理（{working.proxy_code or '未标注'}）价格，不是指数本身，已拒绝用于信号",
            data_source=working.source,
            is_proxy=True,
            source_verified=working.source_verified,
        )

    check = validate_series(working, breakout_window=entry_window, as_of=as_of)
    if not check.get("ok"):
        return EntryPlan(
            index_id=cfg.index_id,
            name=cfg.name,
            market=cfg.market,
            signal_date=str(check.get("last_date") or ""),
            breakout_window=cfg.breakout_window,
            status=STATUS_DATA_UNAVAILABLE,
            detail="行情完整性校验未通过：" + "；".join(check.get("reasons") or []),
            data_source=working.source,
            is_proxy=working.is_proxy,
            source_verified=working.source_verified,
        )

    last_bar = working.bars[-1]
    highs = list(working.highs)
    # A 股轨：首次买入用 0.75N，原始 N 只用于到期续期；
    # 美股轨：首次买入就用自身 N，到期也不做原始N续期。
    threshold = breakout_threshold(highs, entry_window)
    atr = compute_atr14(working.highs, working.lows, working.closes)
    if threshold is None or atr is None:
        return EntryPlan(
            index_id=cfg.index_id,
            name=cfg.name,
            market=cfg.market,
            signal_date=last_bar.date,
            breakout_window=cfg.breakout_window,
            signal_close=last_bar.close,
            threshold_high=threshold,
            status=STATUS_DATA_UNAVAILABLE,
            detail="突破阈值或 ATR14 无法计算（历史根数不足）",
            data_source=working.source,
            is_proxy=working.is_proxy,
            source_verified=working.source_verified,
        )

    buy_max = round(float(last_bar.close) + BUY_MAX_ATR_MULTIPLIER * float(atr), 6)
    reference_entry = float(last_bar.close)
    plan = EntryPlan(
        index_id=cfg.index_id,
        name=cfg.name,
        market=cfg.market,
        signal_date=last_bar.date,
        breakout_window=cfg.breakout_window,
        entry_window=entry_window,
        signal_close=float(last_bar.close),
        threshold_high=float(threshold),
        atr14=float(atr),
        buy_max=buy_max,
        initial_stop_reference=initial_stop_price(cfg, reference_entry, atr, tick=tick),
        trail_trigger_reference=trail_trigger_price(cfg, reference_entry, atr),
        factor=factor,
        status=STATUS_NO_SIGNAL,
        detail=(
            f"收盘 {last_bar.close:.2f} 未突破 {window_label}"
            f"最高 {threshold:.2f}"
        ),
        data_source=working.source,
        is_proxy=working.is_proxy,
        source_verified=working.source_verified,
    )

    # 组合分与信号无关，每行都算（观察池需要展示），闸门判定在突破成立之后。
    combo_result = combo if combo is not None else evaluate_combo(cfg, working)
    plan = EntryPlan(**{**plan.__dict__, "combo": combo_result})

    if float(last_bar.close) <= float(threshold):
        return plan

    # 突破成立。以下为逐项闸门：任一不通过都保留参考价位、但状态不是 SIGNAL。
    factor_result = factor if factor is not None else evaluate_factor(cfg, working, peer_series)
    plan = EntryPlan(**{**plan.__dict__, "factor": factor_result})

    if has_open_position:
        return EntryPlan(**{**plan.__dict__, "status": STATUS_POSITION_OPEN,
                            "detail": "该指数已有持仓，按策略不允许重复进入"})
    if has_pending_plan:
        return EntryPlan(**{**plan.__dict__, "status": STATUS_PLAN_PENDING,
                            "detail": "已存在同一标的的有效买入计划，等待开盘验证"})
    if cooldown_active:
        return EntryPlan(**{**plan.__dict__, "status": STATUS_COOLDOWN,
                            "detail": (cooldown_note or
                                       "处于初始止损后的冷却期，暂不允许新计划")})
    if int(open_position_count or 0) >= int(max_positions or 0) > 0:
        # V9：最多 8 个不同核心指数，不设市场强弱特例；加仓子仓不占槽位。
        return EntryPlan(
            **{**plan.__dict__, "status": STATUS_PORTFOLIO_FULL,
               "detail": f"核心仓已满 {int(max_positions)} 个，第 {int(open_position_count) + 1} 个一律拒绝"
                         "（子仓不占槽位，但也需要真实现金）"}
        )
    if factor_result is not None and factor_result.status == FACTOR_STATUS_UNVERIFIED:
        return EntryPlan(**{**plan.__dict__, "status": STATUS_FACTOR_UNVERIFIED,
                            "detail": factor_result.detail or "因子定义未核实，不发送正式买入提醒"})
    if factor_result is not None and factor_result.blocks_signal:
        return EntryPlan(**{**plan.__dict__, "status": STATUS_FACTOR_BLOCKED,
                            "detail": factor_result.detail or "额外因子不成立"})

    # 因子组合闸门（多因子研究层）：warn 只提示，block 才阻断正式提醒。
    if FACTOR_COMBO_GATE == "block" and combo_result.blocks_signal:
        return EntryPlan(**{**plan.__dict__, "status": STATUS_FACTOR_BLOCKED,
                            "detail": combo_result.detail or "因子组合不通过"})

    detail = (f"收盘 {last_bar.close:.2f} 突破 {window_label}"
              f"最高 {threshold:.2f}，"
              f"ATR14 {atr:.4f}，次日最高允许买入价 {buy_max:.4f}")
    if combo_result.blocks_signal and FACTOR_COMBO_GATE == "warn":
        detail += f"；组合分未通过（{combo_result.score:+.2f}），仅提示不阻断"
    return EntryPlan(**{**plan.__dict__, "status": STATUS_SIGNAL, "detail": detail})


def evaluate_fill(plan: Optional[EntryPlan], next_bar) -> FillResult:
    """用下一交易日开盘价验证买入计划。"""
    if plan is None or plan.status != STATUS_SIGNAL:
        return FillResult(FILL_NO_PLAN, reason="没有待执行的买入计划")
    if plan.buy_max is None:
        return FillResult(FILL_PENDING, reason="计划缺少最高允许买入价，保持未执行")
    if next_bar is None:
        return FillResult(FILL_PENDING, reason="缺失下一有效交易日开盘价，保持未执行（不虚构成交）")
    opened = float(next_bar.open)
    if opened > float(plan.buy_max) + 1e-9:
        return FillResult(
            FILL_CANCELLED,
            reason=f"开盘 {opened:.4f} 高于最高允许买入价 {plan.buy_max:.4f}，本次计划取消",
        )
    return FillResult(
        FILL_FILLED,
        entry_price=opened,
        reason=f"开盘 {opened:.4f} 未超过最高允许买入价 {plan.buy_max:.4f}",
        executed=True,
    )


__all__ = [
    "EntryPlan",
    "FILL_CANCELLED",
    "FILL_FILLED",
    "FILL_NO_PLAN",
    "FILL_PENDING",
    "FillResult",
    "STATUS_COOLDOWN",
    "STATUS_DATA_UNAVAILABLE",
    "STATUS_FACTOR_BLOCKED",
    "STATUS_FACTOR_UNVERIFIED",
    "STATUS_NO_SIGNAL",
    "STATUS_PLAN_PENDING",
    "STATUS_PORTFOLIO_FULL",
    "STATUS_POSITION_OPEN",
    "STATUS_SIGNAL",
    "evaluate_entry",
    "evaluate_fill",
]
