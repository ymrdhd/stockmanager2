"""额外入场因子的评估。

五个阈值（0.090218/0.111459、0.857143、0.287831、0.228571、0.198872）来自用户的
10 指数研究配置，但**产生这些阈值的研究代码不在本仓库**。因此：

- 这里按标准公式实现了计算（下行波动率、Kaufman 趋势效率、均线广度、突破广度），
- 但 ``config.FACTOR_SPECS`` 把这些因子标记为 ``verified=False``，
- 默认（``STRICT_FACTOR_VERIFICATION=True`` 且未允许未核实因子）状态下，
  结果一律为 ``UNVERIFIED`` 并阻断正式买入提醒，界面上显示"因子未验证"。

只有用户显式设置 ``INDEX_STRATEGY_ALLOW_UNVERIFIED_FACTORS=1`` 时，才按标准公式的
比较结果放行，且提醒文案必须标注"因子定义未与研究报告核对"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .config import (
    ALLOW_UNVERIFIED_FACTORS,
    FACTOR_BREAKOUT_BREADTH_20,
    FACTOR_DVOL_MAX_20,
    FACTOR_DVOL_RANGE_20,
    FACTOR_MA_BREADTH_60,
    FACTOR_TREND_EFF_20,
    STRICT_FACTOR_VERIFICATION,
    IndexStrategyConfig,
    FactorSpec,
    factor_spec,
)
from .indicators import breakout_breadth, downside_volatility, ma_breadth, trend_efficiency
from .market_data import IndexSeries

STATUS_NOT_REQUIRED = "NOT_REQUIRED"
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_UNVERIFIED = "UNVERIFIED"
STATUS_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"

#: 因子阈值（与用户给定的研究配置严格一致）。
DVOL_RANGE_LOW = 0.090218
DVOL_RANGE_HIGH = 0.111459
DVOL_MAX = 0.198872
TREND_EFF_MIN = 0.287831
MA_BREADTH_MIN = 0.857143
BREAKOUT_BREADTH_MIN = 0.228571

#: 因子窗口。
FACTOR_WINDOW = 20
MA_BREADTH_WINDOW = 60


@dataclass(frozen=True)
class FactorResult:
    key: Optional[str]
    label: str
    status: str
    value: Optional[float] = None
    computed_status: Optional[str] = None
    detail: str = ""
    verified: bool = True
    sample_size: Optional[int] = None

    @property
    def blocks_signal(self) -> bool:
        return self.status in {STATUS_FAIL, STATUS_UNVERIFIED, STATUS_DATA_UNAVAILABLE}

    @property
    def passed(self) -> bool:
        return self.status in {STATUS_PASS, STATUS_NOT_REQUIRED}

    def display_text(self) -> str:
        if self.status == STATUS_NOT_REQUIRED:
            return "无"
        if self.status == STATUS_UNVERIFIED:
            return f"因子未验证（{self.label}）"
        if self.status == STATUS_DATA_UNAVAILABLE:
            return f"因子数据缺失（{self.label}）"
        return self.label


def _strict_mode(strict: Optional[bool]) -> bool:
    if strict is None:
        return bool(STRICT_FACTOR_VERIFICATION) and not bool(ALLOW_UNVERIFIED_FACTORS)
    return bool(strict)


def _result(
    spec: FactorSpec,
    *,
    computed_status: str,
    value: Optional[float],
    detail: str,
    sample_size: Optional[int] = None,
    strict: Optional[bool] = None,
) -> FactorResult:
    if spec.verified or not _strict_mode(strict):
        note = detail if spec.verified else (detail + "（因子定义未与研究报告核对）")
        return FactorResult(
            key=spec.key,
            label=spec.label,
            status=computed_status,
            value=value,
            computed_status=computed_status,
            detail=note,
            verified=spec.verified,
            sample_size=sample_size,
        )
    return FactorResult(
        key=spec.key,
        label=spec.label,
        status=STATUS_UNVERIFIED,
        value=value,
        computed_status=computed_status,
        detail=f"{detail}；{spec.source}",
        verified=False,
        sample_size=sample_size,
    )


def evaluate_factor(
    cfg: IndexStrategyConfig,
    series: Optional[IndexSeries],
    peer_series: Optional[Dict[str, Optional[IndexSeries]]] = None,
    *,
    strict: Optional[bool] = None,
) -> FactorResult:
    """评估单只指数的额外入场因子。"""
    spec = factor_spec(cfg.factor_key)
    if spec is None:
        return FactorResult(
            key=None,
            label="无",
            status=STATUS_NOT_REQUIRED,
            detail="该指数无额外因子条件",
        )

    def unavailable(detail: str) -> FactorResult:
        if spec.verified or not _strict_mode(strict):
            return FactorResult(
                key=spec.key, label=spec.label, status=STATUS_DATA_UNAVAILABLE,
                detail=detail, verified=spec.verified,
            )
        return FactorResult(
            key=spec.key,
            label=spec.label,
            status=STATUS_UNVERIFIED,
            detail=f"{detail}；{spec.source}",
            verified=False,
        )

    if series is None or not series.bars:
        return unavailable("指数行情不可用，无法计算因子")

    closes = list(series.closes)

    if spec.key == FACTOR_DVOL_RANGE_20:
        value = downside_volatility(closes, FACTOR_WINDOW)
        if value is None:
            return unavailable("20 日下行波动率样本不足")
        ok = DVOL_RANGE_LOW <= value <= DVOL_RANGE_HIGH
        return _result(
            spec,
            computed_status=STATUS_PASS if ok else STATUS_FAIL,
            value=value,
            detail=f"20 日下行波动率 {value:.6f}，要求 {DVOL_RANGE_LOW:.6f}~{DVOL_RANGE_HIGH:.6f}",
            strict=strict,
        )

    if spec.key == FACTOR_DVOL_MAX_20:
        value = downside_volatility(closes, FACTOR_WINDOW)
        if value is None:
            return unavailable("20 日下行波动率样本不足")
        ok = value <= DVOL_MAX
        return _result(
            spec,
            computed_status=STATUS_PASS if ok else STATUS_FAIL,
            value=value,
            detail=f"20 日下行波动率 {value:.6f}，要求 <= {DVOL_MAX:.6f}",
            strict=strict,
        )

    if spec.key == FACTOR_TREND_EFF_20:
        value = trend_efficiency(closes, FACTOR_WINDOW)
        if value is None:
            return unavailable("20 日趋势效率样本不足或路径长度为 0")
        ok = value >= TREND_EFF_MIN
        return _result(
            spec,
            computed_status=STATUS_PASS if ok else STATUS_FAIL,
            value=value,
            detail=f"20 日趋势效率 {value:.6f}，要求 >= {TREND_EFF_MIN:.6f}",
            strict=strict,
        )

    peers = {code: payload for code, payload in dict(peer_series or {}).items() if payload is not None}
    if not peers:
        return unavailable("同市场其他指数行情不可用（广度类因子需要横截面数据）")

    peer_payload = {code: {"closes": list(item.closes)} for code, item in peers.items()}

    if spec.key == FACTOR_MA_BREADTH_60:
        data = ma_breadth(peer_payload, MA_BREADTH_WINDOW)
        if not data:
            return unavailable("60 日均线广度样本不足")
        ok = data["ratio"] >= MA_BREADTH_MIN
        return _result(
            spec,
            computed_status=STATUS_PASS if ok else STATUS_FAIL,
            value=data["ratio"],
            detail=f"60 日均线广度 {data['ratio']:.6f}（{data['hits']}/{data['total']}），要求 >= {MA_BREADTH_MIN:.6f}",
            sample_size=data["total"],
            strict=strict,
        )

    if spec.key == FACTOR_BREAKOUT_BREADTH_20:
        data = breakout_breadth(peer_payload, FACTOR_WINDOW)
        if not data:
            return unavailable("20 日突破广度样本不足")
        ok = data["ratio"] >= BREAKOUT_BREADTH_MIN
        return _result(
            spec,
            computed_status=STATUS_PASS if ok else STATUS_FAIL,
            value=data["ratio"],
            detail=f"20 日突破广度 {data['ratio']:.6f}（{data['hits']}/{data['total']}），要求 >= {BREAKOUT_BREADTH_MIN:.6f}",
            sample_size=data["total"],
            strict=strict,
        )

    return unavailable(f"未实现的因子类型: {spec.key}")


__all__ = [
    "BREAKOUT_BREADTH_MIN",
    "DVOL_MAX",
    "DVOL_RANGE_HIGH",
    "DVOL_RANGE_LOW",
    "FactorResult",
    "MA_BREADTH_MIN",
    "STATUS_DATA_UNAVAILABLE",
    "STATUS_FAIL",
    "STATUS_NOT_REQUIRED",
    "STATUS_PASS",
    "STATUS_UNVERIFIED",
    "TREND_EFF_MIN",
    "evaluate_factor",
]
