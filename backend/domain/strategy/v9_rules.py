"""V9 规则层：T3 首次突破窗口、S1b+K6 门禁盈利加仓、E2a 延持判定、T5 NDX 辅助仓。

本模块只做**纯计算**，不读数据库、不碰 UI，便于单元测试直接覆盖：

- ``t3_entry_window``：首次买入的突破窗口 ``max(1, floor(0.75 × 原始N))``；
  续期判定必须回到**原始 N**（``cfg.breakout_window``），两者不能混用。
- ``evaluate_pyramid``：已有主仓的盈利加仓，目标金额固定 62,500 元，
  每个主仓生命周期最多**成功**一次；K6 门禁 = ``收盘>SMA20 且 SMA20/SMA60>=1.02``。
  **G2-2**：``RANGE`` 震荡市下即使上述条件全部满足也不允许加仓（不算已成功加仓）。
- ``evaluate_ndx_aux``：NDX 独立辅助仓（15% → 21.5%，上限 25%，超限减到 24.5%），
  最短补仓间隔 10 个美股交易日，退出先于补仓。
- ``base_position_amount`` / ``portfolio_sizing``：基础仓 = 策略总权益 ÷ 8。

这些都是"计划口径"，程序不下单、不虚构成交；现金是否足够一律标记为**未核验**。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

from .config import (
    BASE_POSITION_DIVISOR,
    G2_RANGE_PYRAMID_BLOCKED,
    K6_GATE_MIN_MA_RATIO,
    NDX_AUX_ADD_INTERVAL_DAYS,
    NDX_AUX_CASH_BUFFER,
    NDX_AUX_INITIAL_RATIO,
    NDX_AUX_MAX_RATIO,
    NDX_AUX_MIN_ADD_AMOUNT,
    NDX_AUX_TARGET_RATIO,
    NDX_AUX_TRIM_RATIO,
    PORTFOLIO_MAX_POSITIONS,
    PYRAMID_ATR_MULTIPLE,
    PYRAMID_LOOKBACK_DAYS,
    PYRAMID_MIN_HOLDING_DAYS,
    PYRAMID_TARGET_AMOUNT,
    REGIME_RANGE,
    STRONG_TREND_SLOPE_LAG,
    T3_WINDOW_FACTOR,
)

#: 加仓/辅助仓的计划状态。
PLAN_ALLOWED = "ALLOW"
PLAN_REJECTED = "REJECT"
PLAN_DONE = "DONE"
PLAN_NO_POSITION = "NO_POSITION"
PLAN_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
PLAN_HOLD = "HOLD"

# ---------------------------------------------------------------- 均线与 T3


def sma(values: Sequence[float], window: int) -> Optional[float]:
    """简单移动平均；样本不足返回 ``None``（绝不拿部分窗口冒充）。"""
    window = int(window or 0)
    if window <= 0:
        return None
    seq = [float(v) for v in (values or ()) if v is not None]
    if len(seq) < window:
        return None
    return sum(seq[-window:]) / float(window)


def t3_entry_window(original_window: int) -> int:
    """T3：首次突破窗口 = ``max(1, floor(0.75 × 原始N))``。"""
    try:
        original = int(original_window)
    except (TypeError, ValueError):
        return 1
    if original <= 0:
        return 1
    return max(1, int(math.floor(T3_WINDOW_FACTOR * original)))


@dataclass(frozen=True)
class MaSnapshot:
    """收盘价与 V9 用到的三条均线（都按「截至当日」的收盘序列计算）。"""

    close: Optional[float] = None
    sma20: Optional[float] = None
    sma60: Optional[float] = None
    sma60_lag10: Optional[float] = None

    def available(self) -> bool:
        return None not in (self.sma20, self.sma60, self.sma60_lag10)


def ma_snapshot(closes: Sequence[float], *, close: Optional[float] = None) -> MaSnapshot:
    """从收盘序列算出 ``SMA20 / SMA60 / SMA60(t-10)``。

    ``SMA60(t-10)`` 取「倒数第 11 根及之前」的 60 根均值，只用历史数据。
    """
    seq = [float(c) for c in (closes or ()) if c is not None]
    if not seq:
        return MaSnapshot(close=close)
    last = float(close) if close is not None else seq[-1]
    lag_series = seq[:-STRONG_TREND_SLOPE_LAG] if len(seq) > STRONG_TREND_SLOPE_LAG else []
    return MaSnapshot(
        close=last,
        sma20=sma(seq, 20),
        sma60=sma(seq, 60),
        sma60_lag10=sma(lag_series, 60) if lag_series else None,
    )


def strong_trend(snap: MaSnapshot) -> bool:
    """E2a/S1b 的强趋势：``Close>SMA60、SMA20>SMA60、SMA60(t)>SMA60(t-10)``。"""
    if not snap.available() or snap.close is None:
        return False
    return bool(
        float(snap.close) > float(snap.sma60)
        and float(snap.sma20) > float(snap.sma60)
        and float(snap.sma60) > float(snap.sma60_lag10)
    )


def k6_gate(snap: MaSnapshot) -> Tuple[bool, Optional[float]]:
    """K6 盈利加仓门禁：``Close>SMA20`` 且 ``SMA20/SMA60 >= 1.02``。

    返回 ``(是否通过, MA20/MA60 比值)``；均线缺失时比值为 ``None``。
    """
    if snap.close is None or snap.sma20 is None or snap.sma60 is None:
        return False, None
    if float(snap.sma60) <= 0:
        return False, None
    ratio = float(snap.sma20) / float(snap.sma60)
    passed = bool(float(snap.close) > float(snap.sma20) and ratio >= K6_GATE_MIN_MA_RATIO - 1e-12)
    return passed, ratio


# ---------------------------------------------------------------- 仓位口径


def base_position_amount(equity: Optional[float]) -> Optional[float]:
    """基础新开仓目标金额 = 策略总权益 ÷ 8；权益缺失/非正数返回 ``None``。"""
    try:
        value = float(equity or 0.0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value / float(BASE_POSITION_DIVISOR)


@dataclass(frozen=True)
class PortfolioSizing:
    """组合层仓位快照（只用于展示与拒绝原因，不参与下单）。"""

    equity: Optional[float] = None
    base_amount: Optional[float] = None
    core_count: int = 0
    max_positions: int = PORTFOLIO_MAX_POSITIONS
    pyramid_amount: float = PYRAMID_TARGET_AMOUNT
    cash_reliable: bool = False
    cash_note: str = "可用现金无法从现有口径核验，只展示参考金额"

    @property
    def free_slots(self) -> int:
        return max(0, int(self.max_positions) - int(self.core_count))

    @property
    def full(self) -> bool:
        return int(self.core_count) >= int(self.max_positions)

    def slot_text(self) -> str:
        return f"现持指数仓 {int(self.core_count)}/{int(self.max_positions)}"


def portfolio_sizing(*, equity: Optional[float], core_count: int) -> PortfolioSizing:
    return PortfolioSizing(
        equity=float(equity) if equity else None,
        base_amount=base_position_amount(equity),
        core_count=max(0, int(core_count or 0)),
    )


# ---------------------------------------------------------------- 盈利加仓 S1b + K6


def g2_pyramid_block(regime_state: str) -> Tuple[bool, str]:
    """G2-2：``RANGE`` 震荡市是否禁止盈利加仓（返回 ``(是否禁止, 原因)``）。

    非 RANGE（UP/DOWN/未知）一律返回 ``False``，即继续按原 S1b+K6 判定。
    """
    if not bool(G2_RANGE_PYRAMID_BLOCKED):
        return False, ""
    if str(regime_state or "") != REGIME_RANGE:
        return False, ""
    return True, "G2-2：RANGE 震荡市禁止盈利加仓（原 K6 条件满足也不执行，且不计入成功加仓）"


@dataclass(frozen=True)
class PyramidPlan:
    """一次盈利加仓的判定结果（不是成交）。"""

    status: str = PLAN_NO_POSITION
    allowed: bool = False
    reason: str = ""
    target_amount: float = PYRAMID_TARGET_AMOUNT
    checks: Tuple[Tuple[str, bool, str], ...] = field(default_factory=tuple)
    k6_ratio: Optional[float] = None
    cash_verified: bool = False

    def check_map(self) -> dict:
        return {name: ok for name, ok, _ in self.checks}

    def to_payload(self) -> dict:
        return {
            "status": self.status,
            "allowed": self.allowed,
            "reason": self.reason,
            "target_amount": self.target_amount,
            "checks": [list(item) for item in self.checks],
            "k6_ratio": self.k6_ratio,
            "cash_verified": self.cash_verified,
        }


def _fmt(value: Optional[float], digits: int = 3) -> str:
    if value is None:
        return "--"
    return f"{float(value):.{digits}f}"


def evaluate_pyramid(
    cfg,
    position,
    *,
    close: Optional[float],
    snap: MaSnapshot,
    prior_closes: Sequence[float] = (),
    holding_days: Optional[int] = None,
    regime_state: str = "",
    regime_label: str = "",
) -> PyramidPlan:
    """S1b 盈利加仓 + K6 门禁（G2-2：RANGE 震荡市一律拒绝）。

    ``prior_closes`` 必须是**不含当日**的收盘序列（用于 ``Close_t > max(Close_{t-10..t-1})``）。
    ``regime_state`` 是当日的 G2 市场状态（``RANGE``/``UP``/``DOWN``/``UNKNOWN``）；
    为空或 ``UNKNOWN`` 时按原 V9 行为判定（不额外加限制）。
    """
    if position is None or not getattr(position, "is_open", False):
        return PyramidPlan(status=PLAN_NO_POSITION, reason="没有已成交的主仓，盈利加仓无从谈起")

    if getattr(position, "add_executed", False):
        return PyramidPlan(
            status=PLAN_DONE,
            reason="该主仓已成功加仓一次，V9 每个主仓只允许加一次",
            target_amount=PYRAMID_TARGET_AMOUNT,
        )

    block, block_reason = g2_pyramid_block(regime_state)
    if block:
        # G2 硬禁：不新增任何子仓，``add_executed`` 保持原值（被拒≠已成功加仓）。
        return PyramidPlan(
            status=PLAN_REJECTED,
            reason=block_reason,
            target_amount=PYRAMID_TARGET_AMOUNT,
            checks=(("G2震荡市禁加仓", False,
                     f"当前市场状态 {regime_label or regime_state}；RANGE 下不允许加仓"),),
        )

    days = int(holding_days if holding_days is not None else getattr(position, "holding_days", 0) or 0)
    entry = float(getattr(position, "entry_price", 0.0) or 0.0)
    signal_atr = float(getattr(position, "signal_atr", 0.0) or 0.0)

    if close is None or entry <= 0 or signal_atr <= 0:
        return PyramidPlan(
            status=PLAN_DATA_UNAVAILABLE,
            reason="缺少当日收盘、建仓价或冻结 ATR，无法判定盈利加仓",
        )

    px = float(close)
    profit = px - entry
    need_profit = PYRAMID_ATR_MULTIPLE * signal_atr
    holds_enough = days >= PYRAMID_MIN_HOLDING_DAYS

    window = [float(c) for c in (prior_closes or ())][-PYRAMID_LOOKBACK_DAYS:]
    prior_high = max(window) if len(window) >= PYRAMID_LOOKBACK_DAYS else None
    gate_ok, ratio = k6_gate(snap)

    checks = (
        ("G2市场状态", True,
         f"当前 {regime_label or regime_state or '未判定'}：非 RANGE 震荡市，按 S1b+K6 原条件判定"),
        ("持仓天数", holds_enough,
         f"已持有 {days} 日，要求至少 {PYRAMID_MIN_HOLDING_DAYS} 日（含建仓日，即 3 个完整交易日）"),
        ("浮盈≥1ATR", profit >= need_profit - 1e-12,
         f"浮盈 {profit:+.4f}，要求 ≥ {need_profit:.4f}（1×冻结ATR {signal_atr:.4f}）"),
        ("强趋势", strong_trend(snap),
         f"需 收盘>SMA60、SMA20>SMA60、SMA60 上行；当前 收盘 {_fmt(px)} / SMA20 {_fmt(snap.sma20)} / "
         f"SMA60 {_fmt(snap.sma60)} / SMA60(t-10) {_fmt(snap.sma60_lag10)}"),
        ("10日收盘新高", prior_high is not None and px > prior_high,
         f"收盘 {_fmt(px)} 需高于前 {PYRAMID_LOOKBACK_DAYS} 日最高收盘 {_fmt(prior_high)}"),
        ("K6门禁", gate_ok,
         f"需 收盘>SMA20 且 SMA20/SMA60≥{K6_GATE_MIN_MA_RATIO:.2f}；当前比值 "
         f"{'--' if ratio is None else f'{ratio:.4f}'}"),
    )

    failed = [(name, detail) for name, ok, detail in checks if not ok]
    if failed:
        name, detail = failed[0]
        return PyramidPlan(
            status=PLAN_REJECTED,
            reason=f"{name}未满足：{detail}",
            checks=checks,
            k6_ratio=ratio,
        )

    return PyramidPlan(
        status=PLAN_ALLOWED,
        allowed=True,
        reason=(
            f"条件满足：次日可执行一次性盈利加仓，目标金额 ¥{PYRAMID_TARGET_AMOUNT:,.0f}"
            "（现金与限价需人工核对，程序不校验可用现金）"
        ),
        checks=checks,
        k6_ratio=ratio,
    )


def apply_pyramid_fill(position, *, price: float, shares: float, date: str,
                       atr: Optional[float] = None, cfg=None) -> bool:
    """确认加仓后独立登记成交价、冻结ATR、硬止损和移动止损状态。"""
    if position is None or not getattr(position, "is_open", False) or getattr(position, "add_executed", False):
        return False
    try:
        px, qty, frozen_atr = float(price), float(shares), float(atr)
    except (TypeError, ValueError):
        return False
    if min(px, qty, frozen_atr) <= 0 or not str(date or ""):
        return False
    if cfg is None:
        from .config import get_config
        cfg = get_config(position.index_id)
    from .exit import initial_stop_price
    stop = initial_stop_price(cfg, px, frozen_atr)
    if stop <= 0 or stop >= px:
        return False
    position.adds = list(getattr(position, "adds", []) or [])
    position.adds.append({
        "date": str(date), "price": px, "shares": qty, "signal_atr": frozen_atr,
        "amount": px * qty, "initial_stop": stop, "stop_price": stop,
        "highest_price": px, "pending_stop": None, "pending_stop_effective_date": None,
        "exit_date": None, "exit_price": None, "exit_action": None, "exit_reason": None,
    })
    position.add_executed = True
    position.add_filled_date = str(date)
    return True


# ---------------------------------------------------------------- T5 NDX 辅助仓


@dataclass(frozen=True)
class NdxAuxPlan:
    """NDX 独立辅助仓的展示/计划（不重建财务总账、不下单）。"""

    status: str = PLAN_DATA_UNAVAILABLE
    detail: str = ""
    close: Optional[float] = None
    sma200: Optional[float] = None
    current_ratio: Optional[float] = None
    target_ratio: Optional[float] = None
    target_amount: Optional[float] = None
    days_since_add: Optional[int] = None
    action_date: str = ""

    @property
    def actionable(self) -> bool:
        return self.status in {PLAN_ALLOWED, "TRIM", "EXIT"}


def evaluate_ndx_aux(
    closes: Sequence[float],
    *,
    equity: Optional[float] = None,
    current_ratio: float = 0.0,
    days_since_add: Optional[int] = None,
    last_date: str = "",
) -> NdxAuxPlan:
    """按 T5 评估 NDX 辅助仓。

    - 趋势成立：``Close>SMA200`` 且 ``Close>20 个美股交易日前收盘`` → 首次 15%；
    - 趋势仍有效且距上次补仓 ≥10 个美股交易日：目标 21.5%；比例 >25% 减到 24.5%；
    - ``Close < 0.99×SMA200`` → 下一美股开盘全部退出（退出先于补仓）。
    """
    seq = [float(c) for c in (closes or ()) if c is not None]
    if len(seq) < 201:
        return NdxAuxPlan(status=PLAN_DATA_UNAVAILABLE, detail="美股日线不足 201 根，无法判定 SMA200")
    close = seq[-1]
    sma200 = sum(seq[-200:]) / 200.0
    if sma200 <= 0:
        return NdxAuxPlan(status=PLAN_DATA_UNAVAILABLE, detail="SMA200 非正数，数据异常")

    def _ratio(value: float) -> float:
        return float(value)

    eq = float(equity or 0.0)
    amount = (lambda ratio: eq * ratio if eq > 0 else None)

    if close < 0.99 * sma200:
        return NdxAuxPlan(
            status="EXIT",
            detail=f"收盘 {close:.2f} < 0.99×SMA200 {0.99 * sma200:.2f}，下一美股开盘全部退出",
            close=close, sma200=sma200,
            current_ratio=_ratio(current_ratio),
            target_ratio=0.0, target_amount=0.0,
            days_since_add=days_since_add, action_date=str(last_date or ""),
        )

    trend = close > sma200 and close > seq[-21]
    if not trend:
        return NdxAuxPlan(
            status=PLAN_HOLD if current_ratio > 0 else PLAN_NO_POSITION,
            detail=(
                f"趋势不成立（收盘 {close:.2f} / SMA200 {sma200:.2f} / 20日前收盘 {seq[-21]:.2f}），"
                "辅助仓不新开、不补仓"
            ),
            close=close, sma200=sma200,
            current_ratio=_ratio(current_ratio),
            days_since_add=days_since_add, action_date=str(last_date or ""),
        )

    ratio = float(current_ratio or 0.0)
    if ratio <= 0:
        return NdxAuxPlan(
            status=PLAN_ALLOWED,
            detail=f"趋势成立：下一美股合法开盘首次配置 {NDX_AUX_INITIAL_RATIO:.1%} 策略权益",
            close=close, sma200=sma200, current_ratio=ratio,
            target_ratio=NDX_AUX_INITIAL_RATIO,
            target_amount=amount(NDX_AUX_INITIAL_RATIO),
            days_since_add=days_since_add, action_date=str(last_date or ""),
        )

    if ratio > NDX_AUX_MAX_RATIO:
        return NdxAuxPlan(
            status="TRIM",
            detail=(
                f"辅助仓比例 {ratio:.2%} 超过上限 {NDX_AUX_MAX_RATIO:.1%}，"
                f"减至 {NDX_AUX_TRIM_RATIO:.1%}"
            ),
            close=close, sma200=sma200, current_ratio=ratio,
            target_ratio=NDX_AUX_TRIM_RATIO,
            target_amount=amount(NDX_AUX_TRIM_RATIO),
            days_since_add=days_since_add, action_date=str(last_date or ""),
        )

    if days_since_add is None:
        return NdxAuxPlan(
            status=PLAN_HOLD,
            detail="缺少上次补仓日期，无法确认是否已满 10 个美股交易日，暂不补仓",
            close=close, sma200=sma200, current_ratio=ratio,
            days_since_add=None, action_date=str(last_date or ""),
        )
    if int(days_since_add) < NDX_AUX_ADD_INTERVAL_DAYS:
        return NdxAuxPlan(
            status=PLAN_HOLD,
            detail=(
                f"距上次补仓仅 {int(days_since_add)} 个美股交易日，"
                f"最短间隔 {NDX_AUX_ADD_INTERVAL_DAYS} 日，本次不补仓"
            ),
            close=close, sma200=sma200, current_ratio=ratio,
            days_since_add=int(days_since_add), action_date=str(last_date or ""),
        )
    if ratio >= NDX_AUX_TARGET_RATIO:
        return NdxAuxPlan(
            status=PLAN_HOLD,
            detail=f"辅助仓比例 {ratio:.2%} 已达到目标 {NDX_AUX_TARGET_RATIO:.1%}，不再补仓",
            close=close, sma200=sma200, current_ratio=ratio,
            target_ratio=NDX_AUX_TARGET_RATIO,
            days_since_add=int(days_since_add), action_date=str(last_date or ""),
        )

    target_amount = amount(NDX_AUX_TARGET_RATIO)
    add_amount = (target_amount - eq * ratio) if (target_amount is not None) else None
    if add_amount is not None and add_amount < NDX_AUX_MIN_ADD_AMOUNT:
        return NdxAuxPlan(
            status=PLAN_HOLD,
            detail=f"本次补仓金额 {add_amount:,.0f} 低于 {NDX_AUX_MIN_ADD_AMOUNT:,.0f} 元门槛",
            close=close, sma200=sma200, current_ratio=ratio,
            target_ratio=NDX_AUX_TARGET_RATIO, target_amount=target_amount,
            days_since_add=int(days_since_add), action_date=str(last_date or ""),
        )
    if target_amount is not None and eq > 0 and add_amount is not None:
        if add_amount > max(0.0, eq - NDX_AUX_CASH_BUFFER):
            return NdxAuxPlan(
                status=PLAN_HOLD,
                detail=(
                    f"补仓 {add_amount:,.0f} 元会占用 {NDX_AUX_CASH_BUFFER:,.0f} 元现金缓冲，"
                    "本次不补仓"
                ),
                close=close, sma200=sma200, current_ratio=ratio,
                target_ratio=NDX_AUX_TARGET_RATIO, target_amount=target_amount,
                days_since_add=int(days_since_add), action_date=str(last_date or ""),
            )

    return NdxAuxPlan(
        status=PLAN_ALLOWED,
        detail=(
            f"趋势仍有效且已满 {int(days_since_add)} 个美股交易日："
            f"补仓至 {NDX_AUX_TARGET_RATIO:.1%}（上限 {NDX_AUX_MAX_RATIO:.0%}，"
            f"保留 {NDX_AUX_CASH_BUFFER:,.0f} 元现金缓冲）"
        ),
        close=close, sma200=sma200, current_ratio=ratio,
        target_ratio=NDX_AUX_TARGET_RATIO, target_amount=target_amount,
        days_since_add=int(days_since_add), action_date=str(last_date or ""),
    )


__all__ = [
    "MaSnapshot",
    "NdxAuxPlan",
    "PLAN_ALLOWED",
    "PLAN_DATA_UNAVAILABLE",
    "PLAN_DONE",
    "PLAN_HOLD",
    "PLAN_NO_POSITION",
    "PLAN_REJECTED",
    "PortfolioSizing",
    "PyramidPlan",
    "apply_pyramid_fill",
    "base_position_amount",
    "evaluate_ndx_aux",
    "evaluate_pyramid",
    "g2_pyramid_block",
    "k6_gate",
    "ma_snapshot",
    "portfolio_sizing",
    "sma",
    "strong_trend",
    "t3_entry_window",
]
