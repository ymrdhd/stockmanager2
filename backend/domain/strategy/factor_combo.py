"""多因子组合闸门（买卖模型的辅助裁决层）。

研究来源：`breakout_atr_optimization/out/mf_combos.csv`（8 个时序因子 × 28 个两两组合
× 留一法，10 个指数各自跑日频 0/1 仓位回测）。这里只落地**能仅用 OHLCV 计算**的
两因子组合；依赖无风险利率的 `carry` 因子未接入（缺少与行情同频的资金利率序列）。

口径（与研究报告一致）：
- 因子先做 250 日滚动 z-score，再取组合内各因子 z 的等权平均作为 `score`；
- `score > 0` 判为通过（PASS），否则 FAIL；
- 信号滞后 1 日、成本单边 0.025%、阈值固定 0，未做参数搜索。

**未做走向前验证的边界**：研究结论是全样本内选出的组合，样本外会明显衰减，
因此默认 `INDEX_STRATEGY_FACTOR_COMBO_GATE=warn`（只提示、不阻断正式买入提醒）。
改成 `block` 属于用户显式承担口径风险，不是本模块的默认行为。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import FACTOR_COMBO_BY_INDEX, FACTOR_COMBO_GATE, IndexStrategyConfig
from .market_data import IndexSeries

#: z-score 窗口与最少样本。
Z_WINDOW = 250
#: 打分所需最少 K 线数。等于 z 窗口本身：本地快照缓存只保留 260 根，
#: 若要求 300 根（窗口 + 预热）会让 8/10 只 A 股指数恒为「数据不足」。
#: 单因子层面由 ``_z_latest`` 的 150/60 下限兜底（趋势因子的 MA200 预热更久）。
#: 取 240 而非 250：中证2000 的 tushare 快照只有 243 根，差 7 根就整列「数据不足」不合理。
MIN_BARS = 240

STATUS_DISABLED = "DISABLED"
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"

FACTOR_LABELS: Dict[str, str] = {
    "mom20": "20日动量",
    "mom60": "60日动量",
    "rev5": "5日反转",
    "volume": "量能",
    "breakout": "20日突破",
    "trend": "MA200趋势",
    "lowvol": "低波动",
}


@dataclass(frozen=True)
class ComboSpec:
    """单只指数的组合配置（研究最优解）。"""

    key: str
    label: str
    factors: Tuple[str, ...]
    research_cagr: float
    research_mdd: float
    note: str = ""


#: 每只指数落地的最优组合（不含 carry 的价格/成交量口径最优解）。
COMBO_SPECS: Dict[str, ComboSpec] = {
    "000001.SH": ComboSpec("mom20+volume", "20日动量+量能", ("mom20", "volume"), 0.2200, -0.4108),
    "000300.SH": ComboSpec("mom20+mom60", "20日+60日动量", ("mom20", "mom60"), 0.1719, -0.2545),
    "000905.SH": ComboSpec("volume+breakout", "量能+20日突破", ("volume", "breakout"), 0.1128, -0.3054),
    "000852.SH": ComboSpec("mom20+volume", "20日动量+量能", ("mom20", "volume"), 0.0753, -0.2613),
    "399001.SZ": ComboSpec("mom20+volume", "20日动量+量能", ("mom20", "volume"), 0.1525, -0.4319),
    "399006.SZ": ComboSpec("mom20+volume", "20日动量+量能", ("mom20", "volume"), 0.1132, -0.4863),
    "000688.SH": ComboSpec("mom60+volume", "60日动量+量能", ("mom60", "volume"), 0.1469, -0.2406),
    "932000.CSI": ComboSpec("mom20+volume", "20日动量+量能", ("mom20", "volume"), 0.1809, -0.2073),
    "NDX": ComboSpec("mom20+rev5", "20日动量+5日反转", ("mom20", "rev5"), 0.1121, -0.2865),
}


@dataclass(frozen=True)
class ComboResult:
    """组合闸门的评估结果。"""

    key: Optional[str]
    label: str
    status: str
    score: Optional[float] = None
    detail: str = ""
    factor_z: Dict[str, float] = field(default_factory=dict)
    gate: str = "warn"

    @property
    def blocks_signal(self) -> bool:
        return self.status == STATUS_FAIL

    @property
    def passed(self) -> bool:
        return self.status in {STATUS_PASS, STATUS_DISABLED}

    def display_text(self) -> str:
        if self.status == STATUS_DISABLED:
            return "关闭"
        if self.status == STATUS_DATA_UNAVAILABLE:
            return "数据不足"
        return f"{self.label} · {self.score:+.2f}" if self.score is not None else self.label

    def to_payload(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "score": self.score,
            "detail": self.detail,
            "gate": self.gate,
            "factor_z": dict(self.factor_z),
        }


def _rolling_mean(values: List[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= window:
            total -= values[i - window]
        out.append(total / window if i >= window - 1 else None)
    return out


def _atr_series(highs: List[float], lows: List[float], closes: List[float], window: int = 14) -> List[Optional[float]]:
    trs: List[float] = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[0] - lows[0])
        else:
            prev = closes[i - 1]
            trs.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    return _rolling_mean(trs, window)


def build_factor_series(series: IndexSeries) -> Dict[str, List[Optional[float]]]:
    """只用 OHLCV 构造因子序列（长度与 bars 对齐，预热期为 None）。"""
    closes = [float(v) for v in series.closes]
    highs = [float(v) for v in series.highs]
    volumes = [float(v) for v in series.volumes]
    n = len(closes)

    def pct_change(window: int) -> List[Optional[float]]:
        return [None if i < window or closes[i - window] <= 0 else closes[i] / closes[i - window] - 1
                for i in range(n)]

    mom20 = pct_change(20)
    mom60 = pct_change(60)
    rev5 = [None if v is None else -v for v in pct_change(5)]

    vol_ma20 = _rolling_mean(volumes, 20)
    volume = [
        None if vol_ma20[i] is None or vol_ma20[i] <= 0 or volumes[i] <= 0
        else math.log(volumes[i] / vol_ma20[i])
        for i in range(n)
    ]

    breakout: List[Optional[float]] = []
    for i in range(n):
        if i < 20:
            breakout.append(None)
            continue
        prior_high = max(highs[i - 20:i])
        breakout.append(None if prior_high <= 0 else closes[i] / prior_high - 1)

    ma200 = _rolling_mean(closes, 200)
    trend = [None if ma200[i] is None or ma200[i] <= 0 else closes[i] / ma200[i] - 1 for i in range(n)]

    atr = _atr_series(highs, [float(v) for v in series.lows], closes)
    lowvol = [
        None if atr[i] is None or closes[i] <= 0 else -(atr[i] / closes[i])
        for i in range(n)
    ]

    return {"mom20": mom20, "mom60": mom60, "rev5": rev5, "volume": volume,
            "breakout": breakout, "trend": trend, "lowvol": lowvol}


def _z_latest(values: List[Optional[float]], window: int = Z_WINDOW) -> Optional[float]:
    tail = values[-window:]
    clean = [v for v in tail if v is not None]
    if len(clean) < window * 0.6 or len(clean) < 60:
        return None
    mean = sum(clean) / len(clean)
    var = sum((v - mean) ** 2 for v in clean) / (len(clean) - 1)
    if var <= 0:
        return None
    return (clean[-1] - mean) / math.sqrt(var)


def combo_for_index(index_id: str) -> Optional[ComboSpec]:
    """按指数 ID 取组合配置；``config.FACTOR_COMBO_BY_INDEX`` 是同一事实的声明式副本。"""
    spec = COMBO_SPECS.get(index_id)
    declared = FACTOR_COMBO_BY_INDEX.get(index_id)
    if spec is None or (declared and declared != spec.key):
        return None
    return spec


def evaluate_combo(
    cfg: IndexStrategyConfig,
    series: Optional[IndexSeries],
    *,
    gate: Optional[str] = None,
) -> ComboResult:
    """评估该指数的因子组合闸门。数据不足时返回 DATA_UNAVAILABLE 而不是伪造通过。"""
    gate = (gate or FACTOR_COMBO_GATE).lower()
    spec = combo_for_index(cfg.index_id)
    if spec is None:
        return ComboResult(None, "无", STATUS_DISABLED, detail="该指数未配置因子组合", gate=gate)
    if gate == "off":
        return ComboResult(spec.key, spec.label, STATUS_DISABLED,
                           detail="INDEX_STRATEGY_FACTOR_COMBO_GATE=off，组合闸门未启用",
                           gate=gate)
    if series is None or not series.bars:
        return ComboResult(spec.key, spec.label, STATUS_DATA_UNAVAILABLE,
                           detail="指数行情不可用，无法计算组合分", gate=gate)
    if series.is_proxy:
        return ComboResult(spec.key, spec.label, STATUS_DATA_UNAVAILABLE,
                           detail="行情是 ETF 代理价格，不用于指数组合打分", gate=gate)
    if len(series.bars) < MIN_BARS:
        return ComboResult(spec.key, spec.label, STATUS_DATA_UNAVAILABLE,
                           detail=f"历史仅 {len(series.bars)} 根，少于组合打分所需 {MIN_BARS} 根", gate=gate)

    factor_series = build_factor_series(series)
    zs: Dict[str, float] = {}
    missing: List[str] = []
    for name in spec.factors:
        z = _z_latest(factor_series.get(name) or [])
        if z is None:
            missing.append(FACTOR_LABELS.get(name, name))
        else:
            zs[name] = z
    if missing:
        return ComboResult(spec.key, spec.label, STATUS_DATA_UNAVAILABLE,
                           detail="因子样本不足：" + "、".join(missing), factor_z=zs, gate=gate)

    score = sum(zs.values()) / len(zs)
    ok = score > 0
    depth = ("" if len(series.bars) >= Z_WINDOW + 60
             else f"；样本仅 {len(series.bars)} 根，z 窗口未完全填满，分数偏噪声")
    detail = (f"{spec.label} 组合分 {score:+.3f}（"
              + "、".join(f"{FACTOR_LABELS.get(k, k)} {v:+.2f}" for k, v in zs.items())
              + f"），{'通过' if ok else '不通过'}；研究口径 CAGR {spec.research_cagr:.2%}、"
                f"回撤 {spec.research_mdd:.2%}（同一样本内，未做走向前验证）" + depth)
    return ComboResult(spec.key, spec.label, STATUS_PASS if ok else STATUS_FAIL,
                       score=score, detail=detail, factor_z=zs, gate=gate)


__all__ = [
    "COMBO_SPECS",
    "ComboResult",
    "ComboSpec",
    "FACTOR_LABELS",
    "MIN_BARS",
    "STATUS_DATA_UNAVAILABLE",
    "STATUS_DISABLED",
    "STATUS_FAIL",
    "STATUS_PASS",
    "Z_WINDOW",
    "build_factor_series",
    "combo_for_index",
    "evaluate_combo",
]
