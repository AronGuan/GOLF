"""参考范围表（架构 ARCHITECTURE-v2.md §3.2 / §3.3）与五态判定。

本模块**只承载数据**（不含计算函数），计算实现放在 :mod:`app.metrics` 的
``METRIC_FUNCS`` 注册表中，两者以 ``MetricSpec.impl_key`` 关联。

v2 变更（相对 MVP）：
- ``MetricSpec`` 新增 ``fn_key``（对外 key → 实现 key 的显式映射）、
  ``description``（术语解释行）、``critical``（是否参与五态重度判定）、
  ``proxy_ref_pad``（L1 代理降级时参考区间双向放宽量）；
- 指标 key 全部对齐 PDD v2.0（§3.3 映射表），``views`` 按机位归属落值；
- 新增 ④ ``swing_plane`` 与 ⑤ ``shaft_plane_dev``；
- ``judge()`` 保留为三态薄封装，新增 ``judge5()`` 五态判定（区间宽度倍数）。

> 依赖方向：``reference -> schemas / config``（config 仅为 ``judge5`` 读
> ``CRITICAL_SPAN_RATIO``）。**不** import ``metrics``，避免循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Tuple

from . import config
from .schemas import CameraView, MetricStatus, PhaseKey

#: 角度单位
UNIT_DEG: str = "°"
UNIT_PCT: str = "%"
UNIT_SEC: str = "s"
UNIT_RATIO: str = ":1"
UNIT_NONE: str = ""

#: 双机位全集。``CameraView.AUTO`` 是请求入参态，**不**出现在这里——
#: 进入指标计算前必须已被解析成 face-on / DTL 之一。
ALL_VIEWS: FrozenSet[CameraView] = frozenset(
    {CameraView.FACE_ON, CameraView.DOWN_THE_LINE}
)

#: 机位简写（仅便于本模块内书写）
_F = frozenset({CameraView.FACE_ON})
_D = frozenset({CameraView.DOWN_THE_LINE})


@dataclass(frozen=True)
class MetricSpec:
    """一个指标的静态定义。

    Attributes:
        key: 【对外 key】= PDD v2.0 的 key，出现在 ``StageMetric.key`` /
            ``RiskRule.metric_key`` / 小程序。
        fn_key: 【计算实现 key】= ``METRIC_FUNCS`` 的键；``""`` 表示与 ``key`` 同名。
        views: 该指标适用的机位。
        allow_drop: 计算失败时是否允许**整项剔除**而不是回落到 ``ref_mid``。
        description: 卡片下方术语解释行（PDD §4.2 原文；缺失为 ``""``）。
        critical: 是否参与 critical_low / critical_high 判定。
        proxy_ref_pad: L1 代理降级时参考区间双向放宽量（球杆指标专用）。
    """

    key: str
    name: str
    unit: str
    ref_min: float
    ref_max: float
    views: FrozenSet[CameraView] = field(default=ALL_VIEWS)
    allow_drop: bool = False
    # ---- v2 新增 4 个字段 ----
    fn_key: str = ""
    description: str = ""
    critical: bool = True
    proxy_ref_pad: float = 0.0

    @property
    def ref_mid(self) -> float:
        """参考区间中值，用于异常兜底填充。"""
        return (self.ref_min + self.ref_max) / 2.0

    @property
    def impl_key(self) -> str:
        """计算实现 key = ``fn_key or key``。"""
        return self.fn_key or self.key

    def supports(self, view: CameraView) -> bool:
        """该指标在 ``view`` 机位下是否适用。"""
        if view is CameraView.AUTO:
            return True
        return view in self.views


# ---------------------------------------------------------------------------
# 术语解释行（PDD §4.2 逐字抄录 20 条；缺失的 3 条留空，严禁编造）
# ---------------------------------------------------------------------------

DESCRIPTIONS: Dict[str, str] = {
    "shoulder_turn": "肩部相对准备位置向后转动的角度",
    "hip_turn": "髋部相对准备位置向后转动的角度",
    "x_factor": "肩部与髋部转动角度之差，数值越大'上弦'越紧，力量越足",
    "x_factor_retention": "下杆初期X-Factor的保留比例，保留越多越能蓄力释放",
    "lead_arm_straightness": "左臂伸直程度，180°为完全伸直，越接近越好",
    "trail_arm_flexion": "右肘弯曲角度，正常折叠利于力量传导",
    "spine_tilt_side": "脊柱向远离目标方向的侧倾角度",
    "spine_tilt_fwd": "脊柱相对垂直线的向前倾斜角度",
    "spine_tilt_change": "击球时相比准备时站直了多少，数值越接近0越好",
    "stance_width_ratio": "双脚间距与肩宽的比值",
    "knee_flexion": "膝盖弯曲角度，180°为完全伸直",
    "hip_open_angle": "击球时髋部朝向目标的开放角度",
    "shoulder_squareness": "击球时肩部朝向目标的方正程度",
    "pelvis_shift": "重心向目标方向移动的距离（以肩宽百分比表示）",
    "head_drift": "头部相对起始位置的晃动幅度（以肩宽百分比表示）",
    "swing_plane": "顶点时手臂与水平面的夹角",
    "balance_hold": "收杆后站稳的时间，越长代表平衡性越好",
    "tempo_ratio": "上杆时间与下杆时间的比值，接近3:1为理想节奏",
    "swing_duration": "从准备到收杆的总时长",
}


def _desc(key: str) -> str:
    """取术语解释行；缺失返回空串（前端不渲染该行）。"""
    return DESCRIPTIONS.get(key, "")


# ---------------------------------------------------------------------------
# 8 阶段指标（架构 §3.3 映射表逐行数据化；ref 数值与 MVP 完全一致）
# ---------------------------------------------------------------------------

METRIC_SPECS: Dict[PhaseKey, List[MetricSpec]] = {
    PhaseKey.ADDRESS: [
        MetricSpec(
            "spine_tilt_side", "脊柱侧倾角", UNIT_DEG, 5.0, 12.0,
            views=_F, fn_key="spine_lateral_tilt",
            description=_desc("spine_tilt_side"),
            # ⚠️ B8：几何量由「肩线水平倾角」换成「脊柱侧倾角」，ref 沿用 5~12 待标定
        ),
        MetricSpec(
            "stance_width_ratio", "站姿宽度比", UNIT_NONE, 1.0, 1.3,
            views=_F, description=_desc("stance_width_ratio"),
        ),
        MetricSpec(
            "knee_flexion", "膝部弯曲角", UNIT_DEG, 160.0, 172.0,
            views=ALL_VIEWS, fn_key="knee_flex",
            description=_desc("knee_flexion"),
        ),
        MetricSpec(
            "spine_tilt_fwd", "脊柱前倾角", UNIT_DEG, 30.0, 40.0,
            views=_D, fn_key="spine_forward_tilt",
            description=_desc("spine_tilt_fwd"),
        ),
    ],
    PhaseKey.TAKEAWAY: [
        MetricSpec(
            "shoulder_turn", "肩部转动角", UNIT_DEG, 25.0, 35.0,
            views=_F, description=_desc("shoulder_turn"),
        ),
        MetricSpec(
            "hip_turn", "髋部转动角", UNIT_DEG, 8.0, 18.0,
            views=_F, description=_desc("hip_turn"),
        ),
        MetricSpec(
            "head_drift", "头部位移", UNIT_PCT, 0.0, 4.0,
            views=ALL_VIEWS, fn_key="head_drift_pct",
            description=_desc("head_drift"),
        ),
        MetricSpec(
            "lead_arm_straightness", "引导臂伸直度", UNIT_DEG, 165.0, 178.0,
            views=ALL_VIEWS, fn_key="lead_arm_straight",
            description=_desc("lead_arm_straightness"),
        ),
    ],
    PhaseKey.BACKSWING: [
        MetricSpec(
            "shoulder_turn", "肩部转动角", UNIT_DEG, 55.0, 72.0,
            views=_F, description=_desc("shoulder_turn"),
        ),
        MetricSpec(
            "hip_turn", "髋部转动角", UNIT_DEG, 25.0, 38.0,
            views=_F, description=_desc("hip_turn"),
        ),
        MetricSpec(
            "trail_arm_flexion", "后臂弯曲角", UNIT_DEG, 95.0, 125.0,
            views=ALL_VIEWS, fn_key="trail_elbow_flex",
            description=_desc("trail_arm_flexion"),
        ),
        MetricSpec(
            "lead_arm_straightness", "引导臂伸直度", UNIT_DEG, 155.0, 175.0,
            views=ALL_VIEWS, fn_key="lead_arm_straight",
            description=_desc("lead_arm_straightness"),
        ),
    ],
    PhaseKey.TOP: [
        MetricSpec(
            "shoulder_turn", "肩部转动角", UNIT_DEG, 70.0, 88.0,
            views=_F, description=_desc("shoulder_turn"),
        ),
        MetricSpec(
            "hip_turn", "髋部转动角", UNIT_DEG, 45.0, 60.0,
            views=_F, description=_desc("hip_turn"),
        ),
        MetricSpec(
            "x_factor", "X-Factor(肩髋分离)", UNIT_DEG, 20.0, 35.0,
            views=_F, description=_desc("x_factor"),
        ),
        MetricSpec(
            "lead_arm_straightness", "引导臂伸直度", UNIT_DEG, 150.0, 172.0,
            views=ALL_VIEWS, fn_key="lead_arm_straight",
            description=_desc("lead_arm_straightness"),
        ),
        # 🆕 全新：纯 MediaPipe（左肩 11→左腕 15 与图像水平线夹角），DTL 专属
        MetricSpec(
            "swing_plane", "挥杆平面角", UNIT_DEG, 55.0, 65.0,
            views=_D, allow_drop=True,
            description=_desc("swing_plane"),
        ),
    ],
    PhaseKey.DOWNSWING: [
        MetricSpec(
            "hip_turn", "髋部转动角", UNIT_DEG, 10.0, 30.0,
            views=_F, description=_desc("hip_turn"),
        ),
        MetricSpec(
            "shoulder_turn", "肩部转动角", UNIT_DEG, 45.0, 65.0,
            views=_F, description=_desc("shoulder_turn"),
        ),
        MetricSpec(
            "x_factor_retention", "X-Factor 保持率", UNIT_PCT, 85.0, 130.0,
            views=_F, critical=False, description=_desc("x_factor_retention"),
        ),
        MetricSpec(
            "pelvis_shift", "骨盆水平位移", UNIT_PCT, 4.0, 12.0,
            views=_F, fn_key="pelvis_shift_pct",
            description=_desc("pelvis_shift"),
        ),
        # 🆕 全新·球杆增强：⑤ 下杆杆头轨迹相对 base plane 偏差，DTL 专属
        MetricSpec(
            "shaft_plane_dev", "杆面平面偏差", UNIT_DEG, -5.0, 10.0,
            views=_D, allow_drop=True, proxy_ref_pad=5.0, critical=False,
            description="",
        ),
    ],
    PhaseKey.IMPACT: [
        MetricSpec(
            "hip_open_angle", "髋部开放角", UNIT_DEG, 15.0, 30.0,
            views=_F, fn_key="hip_open",
            description=_desc("hip_open_angle"),
        ),
        MetricSpec(
            "shoulder_squareness", "肩部方正度", UNIT_DEG, -5.0, 12.0,
            views=_F, fn_key="shoulder_square",
            description=_desc("shoulder_squareness"),
        ),
        MetricSpec(
            "pelvis_shift", "骨盆水平位移", UNIT_PCT, 10.0, 20.0,
            views=_F, fn_key="pelvis_shift_pct",
            description=_desc("pelvis_shift"),
        ),
        MetricSpec(
            "spine_tilt_change", "脊柱前倾变化量(起身量)", UNIT_DEG, 0.0, 8.0,
            views=_D, fn_key="spine_tilt_delta",
            description=_desc("spine_tilt_change"),
            # 仅 high 侧有效：critical_low 需 value < 0-8=-8，m_spine_tilt_delta
            # 恒 ≥ 0，天然不可达（架构 §3.5 决策 3）。
        ),
    ],
    PhaseKey.FOLLOW_THROUGH: [
        MetricSpec(
            "hip_open_angle", "髋部开放角", UNIT_DEG, 40.0, 60.0,
            views=_F, fn_key="hip_open",
            description=_desc("hip_open_angle"),
        ),
        # 🚨 RISK-016 符号陷阱在此拆除：对外叫 shoulder_turn，实算 m_shoulder_open
        #    = −肩转（正值开放角），引擎按对外 key 查表零特判。
        MetricSpec(
            "shoulder_turn", "肩部转动角(开放)", UNIT_DEG, 35.0, 60.0,
            views=_F, fn_key="shoulder_open",
            description=_desc("shoulder_turn"),
        ),
        MetricSpec(
            "trail_arm_flexion", "后臂伸展度", UNIT_DEG, 150.0, 172.0,
            views=ALL_VIEWS, fn_key="trail_arm_extend",
            description=_desc("trail_arm_flexion"),
        ),
        MetricSpec(
            "spine_tilt_side", "脊柱侧倾", UNIT_DEG, 10.0, 20.0,
            views=_F, fn_key="spine_lateral_tilt",
            description=_desc("spine_tilt_side"),
        ),
    ],
    PhaseKey.FINISH: [
        MetricSpec(
            "hip_toward_target", "髋部朝向目标角", UNIT_DEG, 75.0, 95.0,
            views=_F, fn_key="hip_to_target",
            description=_desc("hip_toward_target"),  # 缺失 → ""
        ),
        MetricSpec(
            "shoulder_total_open", "肩部转动角(总开放)", UNIT_DEG, 85.0, 110.0,
            views=_F, fn_key="shoulder_open",
            description=_desc("shoulder_total_open"),  # 缺失 → ""
        ),
        MetricSpec(
            "pelvis_shift", "骨盆水平位移", UNIT_PCT, 20.0, 35.0,
            views=_F, fn_key="pelvis_shift_pct",
            description=_desc("pelvis_shift"),
        ),
        MetricSpec(
            "balance_hold", "收杆平衡保持时长", UNIT_SEC, 0.8, 3.0,
            views=ALL_VIEWS, fn_key="balance_hold_sec", critical=False,
            description=_desc("balance_hold"),
        ),
    ],
}

#: 全程指标 3 项
GLOBAL_SPECS: List[MetricSpec] = [
    MetricSpec(
        "tempo_ratio", "节奏比", UNIT_RATIO, 2.5, 3.5,
        views=ALL_VIEWS, description=_desc("tempo_ratio"),
    ),
    MetricSpec(
        "swing_duration", "挥杆总时长", UNIT_SEC, 1.0, 1.6,
        views=ALL_VIEWS, description=_desc("swing_duration"),
    ),
    MetricSpec(
        "max_head_drift", "头部最大位移", UNIT_PCT, 0.0, 8.0,
        views=ALL_VIEWS, fn_key="max_head_drift_pct",
        description=_desc("max_head_drift"),  # 缺失 → ""
    ),
]


# ---------------------------------------------------------------------------
# 五态判定（架构 §3.5 —— 区间宽度倍数，不用 PDD 乘法规则）
# ---------------------------------------------------------------------------


def judge5(
    value: float,
    ref_min: float,
    ref_max: float,
    critical: bool = True,
) -> MetricStatus:
    """五态判定：先判 critical、再判普通（否则 critical_low 永远走不到）。

    ``critical`` 区间 = 参考区间向两侧各放宽 ``span × CRITICAL_SPAN_RATIO``
    （默认 1.0 = 一个完整区间宽度）。对 ``ref_min <= 0`` 与 ``ref_min < 0``
    的指标数学天然安全（乘法规则在此崩坏，见架构 §3.5）。
    """
    span = ref_max - ref_min
    if span <= 0:
        span = max(abs(ref_max), 1.0) * 0.3  # 与小程序 decorate() 同源
    pad = span * config.CRITICAL_SPAN_RATIO
    if critical and value < ref_min - pad:
        return MetricStatus.CRITICAL_LOW
    if critical and value > ref_max + pad:
        return MetricStatus.CRITICAL_HIGH
    if value < ref_min:
        return MetricStatus.LOW
    if value > ref_max:
        return MetricStatus.HIGH
    return MetricStatus.NORMAL


def judge(value: float, ref_min: float, ref_max: float) -> MetricStatus:
    """三态判定（旧语义薄封装，供旧测试与兼容路径使用）。"""
    return judge5(value, ref_min, ref_max, critical=False)


def all_metric_keys() -> Tuple[str, ...]:
    """全部出现过的【实现】key（``impl_key`` 去重，用于自检 ``METRIC_FUNCS`` 覆盖）。"""
    keys: List[str] = []
    for specs in METRIC_SPECS.values():
        keys.extend(spec.impl_key for spec in specs)
    keys.extend(spec.impl_key for spec in GLOBAL_SPECS)
    return tuple(dict.fromkeys(keys))
