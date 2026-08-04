"""参考范围表（架构文档 §8.3）与三态判定。

本模块**只承载数据**（不含计算函数），计算实现放在 :mod:`app.metrics` 的
``METRIC_FUNCS`` 注册表中，两者以 ``key`` 关联。

> 与架构文档 §8.3 的细微偏差：``MetricSpec`` 不再持有 ``fn`` 字段。
> 理由：若 ``MetricSpec.fn`` 直接引用 ``metrics.py`` 的派生量函数，会形成
> ``reference <-> metrics`` 循环导入（架构文档 §10.4 明令避免）。以 ``key``
> 关联后依赖变为单向 ``metrics -> reference``，且同一 ``key`` 在不同阶段可复用
> 同一实现、只换参考范围，反而更简洁。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .schemas import MetricStatus, PhaseKey

#: 角度单位
UNIT_DEG: str = "°"
UNIT_PCT: str = "%"
UNIT_SEC: str = "s"
UNIT_RATIO: str = ":1"
UNIT_NONE: str = ""


@dataclass(frozen=True)
class MetricSpec:
    """一个指标的静态定义。"""

    key: str
    name: str
    unit: str
    ref_min: float
    ref_max: float

    @property
    def ref_mid(self) -> float:
        """参考区间中值，用于异常兜底填充。"""
        return (self.ref_min + self.ref_max) / 2.0


# ---------------------------------------------------------------------------
# 8 阶段 × 4 指标 = 32 项（架构文档 §8.3 表格逐行数据化）
# ---------------------------------------------------------------------------

METRIC_SPECS: Dict[PhaseKey, List[MetricSpec]] = {
    PhaseKey.ADDRESS: [
        MetricSpec("spine_forward_tilt", "脊柱前倾角", UNIT_DEG, 30.0, 40.0),
        MetricSpec("stance_width_ratio", "站姿宽度比", UNIT_NONE, 1.0, 1.3),
        MetricSpec("shoulder_line_tilt", "肩线水平倾角", UNIT_DEG, 5.0, 12.0),
        MetricSpec("knee_flex", "膝部弯曲角", UNIT_DEG, 160.0, 172.0),
    ],
    PhaseKey.TAKEAWAY: [
        MetricSpec("shoulder_turn", "肩部转动角", UNIT_DEG, 25.0, 35.0),
        MetricSpec("hip_turn", "髋部转动角", UNIT_DEG, 8.0, 18.0),
        MetricSpec("head_drift_pct", "头部位移", UNIT_PCT, 0.0, 4.0),
        MetricSpec("lead_arm_straight", "引导臂伸直度", UNIT_DEG, 165.0, 178.0),
    ],
    PhaseKey.BACKSWING: [
        MetricSpec("shoulder_turn", "肩部转动角", UNIT_DEG, 55.0, 72.0),
        MetricSpec("hip_turn", "髋部转动角", UNIT_DEG, 25.0, 38.0),
        MetricSpec("trail_elbow_flex", "后臂弯曲角", UNIT_DEG, 95.0, 125.0),
        MetricSpec("lead_arm_straight", "引导臂伸直度", UNIT_DEG, 155.0, 175.0),
    ],
    PhaseKey.TOP: [
        MetricSpec("shoulder_turn", "肩部转动角", UNIT_DEG, 70.0, 88.0),
        MetricSpec("hip_turn", "髋部转动角", UNIT_DEG, 45.0, 60.0),
        MetricSpec("x_factor", "X-Factor(肩髋分离)", UNIT_DEG, 20.0, 35.0),
        MetricSpec("lead_arm_straight", "引导臂伸直度", UNIT_DEG, 150.0, 172.0),
    ],
    PhaseKey.DOWNSWING: [
        MetricSpec("hip_turn", "髋部转动角", UNIT_DEG, 10.0, 30.0),
        MetricSpec("shoulder_turn", "肩部转动角", UNIT_DEG, 45.0, 65.0),
        MetricSpec("x_factor_retention", "X-Factor 保持率", UNIT_PCT, 85.0, 130.0),
        MetricSpec("pelvis_shift_pct", "骨盆水平位移", UNIT_PCT, 4.0, 12.0),
    ],
    PhaseKey.IMPACT: [
        MetricSpec("hip_open", "髋部开放角", UNIT_DEG, 15.0, 30.0),
        MetricSpec("shoulder_square", "肩部方正度", UNIT_DEG, -5.0, 12.0),
        MetricSpec("spine_tilt_delta", "起身量(脊柱倾角变化)", UNIT_DEG, 0.0, 8.0),
        MetricSpec("pelvis_shift_pct", "骨盆水平位移", UNIT_PCT, 10.0, 20.0),
    ],
    PhaseKey.FOLLOW_THROUGH: [
        MetricSpec("hip_open", "髋部开放角", UNIT_DEG, 40.0, 60.0),
        MetricSpec("shoulder_open", "肩部转动角(开放)", UNIT_DEG, 35.0, 60.0),
        MetricSpec("trail_arm_extend", "后臂伸展度", UNIT_DEG, 150.0, 172.0),
        MetricSpec("spine_lateral_tilt", "脊柱侧倾", UNIT_DEG, 10.0, 20.0),
    ],
    PhaseKey.FINISH: [
        MetricSpec("hip_to_target", "髋部朝向目标角", UNIT_DEG, 75.0, 95.0),
        MetricSpec("shoulder_open", "肩部转动角(总开放)", UNIT_DEG, 85.0, 110.0),
        MetricSpec("pelvis_shift_pct", "骨盆水平位移", UNIT_PCT, 20.0, 35.0),
        MetricSpec("balance_hold_sec", "收杆平衡保持时长", UNIT_SEC, 0.8, 3.0),
    ],
}

#: 全程指标 3 项
GLOBAL_SPECS: List[MetricSpec] = [
    MetricSpec("tempo_ratio", "节奏比", UNIT_RATIO, 2.5, 3.5),
    MetricSpec("swing_duration", "挥杆总时长", UNIT_SEC, 1.0, 1.6),
    MetricSpec("max_head_drift_pct", "头部最大位移", UNIT_PCT, 0.0, 8.0),
]


def judge(value: float, ref_min: float, ref_max: float) -> MetricStatus:
    """三态判定。"""
    if value < ref_min:
        return MetricStatus.LOW
    if value > ref_max:
        return MetricStatus.HIGH
    return MetricStatus.NORMAL


def all_metric_keys() -> Tuple[str, ...]:
    """全部出现过的指标 key（去重，用于自检 METRIC_FUNCS 覆盖完整）。"""
    keys: List[str] = []
    for specs in METRIC_SPECS.values():
        keys.extend(spec.key for spec in specs)
    keys.extend(spec.key for spec in GLOBAL_SPECS)
    return tuple(dict.fromkeys(keys))
