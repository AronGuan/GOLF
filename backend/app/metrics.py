"""指标计算引擎（架构文档 §8）。

依赖方向严格单向：``metrics -> reference / geometry / schemas / config``。

设计要点
--------
1. 所有**角度**指标基于 world 3D 坐标（米制，原点=双髋中点）。
2. 所有**位移**指标基于归一化图像坐标换算的像素坐标，并以图像肩宽（像素）归一化。

   > 与架构文档 §8.2 的偏差（已在交付说明中报备）：文档写的是用 world 坐标算
   > ``pelvis_shift_pct``，但 MediaPipe world landmarks 的原点**就是双髋中点**，
   > ``midpoint(world[23], world[24])`` 恒等于 ``(0,0,0)``，该式恒为 0。
   > 因此骨盆/头部位移改用图像坐标（并按 width/height 还原纵横比，避免各向异性）。

3. 每个指标出口统一过 :func:`_sanitize`，保证无 ``NaN`` / ``inf``，角度夹到 ±180。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np

from . import config, geometry, reference
from .reference import MetricSpec
from .schemas import (
    FrameLandmarks,
    GlobalMetrics,
    PhaseKey,
    StageMetric,
    SwingEvent,
    SwingSignals,
    VideoMeta,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 计算上下文
# ---------------------------------------------------------------------------


@dataclass
class MetricContext:
    """指标计算所需的全部上下文。"""

    frames: List[FrameLandmarks]
    events: List[SwingEvent]
    signals: SwingSignals
    meta: VideoMeta
    #: world 肩宽（米），取 Address 帧
    S: float
    #: 图像肩宽（像素），全片中位数
    S_px: float
    #: 当前阶段（``compute_global_metrics`` 时为 None）
    phase: Optional[PhaseKey] = None
    cache: Dict[str, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    # -- 快捷访问 ---------------------------------------------------------

    @property
    def fps(self) -> float:
        """原视频帧率。"""
        return self.meta.fps

    @property
    def dt(self) -> float:
        """采样序列的帧间隔（秒）。"""
        return self.signals.dt

    def event_of(self, key: PhaseKey) -> SwingEvent:
        """按阶段取事件。"""
        for event in self.events:
            if event.key is key:
                return event
        raise KeyError(f"event not found: {key}")

    def frame_of(self, key: PhaseKey) -> FrameLandmarks:
        """按阶段取定格帧。"""
        return self.frames[self.event_of(key).array_index]

    @property
    def addr(self) -> FrameLandmarks:
        """Address 基准帧。"""
        return self.frame_of(PhaseKey.ADDRESS)

    @property
    def cur(self) -> FrameLandmarks:
        """当前阶段定格帧。"""
        if self.phase is None:
            return self.addr
        return self.frame_of(self.phase)

    def warn(self, text: str) -> None:
        """去重追加 warning。"""
        if text not in self.warnings:
            self.warnings.append(text)


# ---------------------------------------------------------------------------
# 基础换算
# ---------------------------------------------------------------------------


def _img_pt(ctx: MetricContext, frame: FrameLandmarks, idx: int) -> np.ndarray:
    """归一化图像坐标 -> 像素坐标（2D），修正纵横比。"""
    return np.array(
        [
            float(frame.norm[idx, 0]) * ctx.meta.width,
            float(frame.norm[idx, 1]) * ctx.meta.height,
        ],
        dtype=np.float64,
    )


def _img_hip_mid(ctx: MetricContext, frame: FrameLandmarks) -> np.ndarray:
    """双髋中点（像素坐标）。"""
    return geometry.midpoint(
        _img_pt(ctx, frame, geometry.L_HIP), _img_pt(ctx, frame, geometry.R_HIP)
    )


def image_shoulder_width_px(
    frames: List[FrameLandmarks], meta: VideoMeta, ref_index: int = -1
) -> float:
    """位移类指标的像素标尺：**Address 帧**的图像肩宽（像素）。

    为什么不用全片中位数：躯干转动会让肩线在图像上被压缩（顶点/收杆时投影肩宽
    可小到真实值的 1/4），取中位数会把标尺压小、把位移百分比整体放大。
    Address 帧正对镜头，投影肩宽最接近真实肩宽，是最稳的标尺。
    若 Address 帧异常（缺失/被压缩），退回全片 90 分位兜底。

    Args:
        frames: 采样帧序列。
        meta: 视频元信息（提供宽高，用于还原纵横比）。
        ref_index: Address 帧在 ``frames`` 中的下标；<0 表示不指定。

    Returns:
        像素肩宽，恒 > 0。
    """
    values: List[float] = []
    for frame in frames:
        left = np.array(
            [frame.norm[geometry.L_SHOULDER, 0] * meta.width,
             frame.norm[geometry.L_SHOULDER, 1] * meta.height],
            dtype=np.float64,
        )
        right = np.array(
            [frame.norm[geometry.R_SHOULDER, 0] * meta.width,
             frame.norm[geometry.R_SHOULDER, 1] * meta.height],
            dtype=np.float64,
        )
        value = float(np.linalg.norm(left - right))
        if math.isfinite(value) and value > 0.0:
            values.append(value)

    if not values:
        return 1.0

    fallback = float(np.percentile(values, 90))
    if 0 <= ref_index < len(frames):
        left = np.array(
            [frames[ref_index].norm[geometry.L_SHOULDER, 0] * meta.width,
             frames[ref_index].norm[geometry.L_SHOULDER, 1] * meta.height],
            dtype=np.float64,
        )
        right = np.array(
            [frames[ref_index].norm[geometry.R_SHOULDER, 0] * meta.width,
             frames[ref_index].norm[geometry.R_SHOULDER, 1] * meta.height],
            dtype=np.float64,
        )
        addr_width = float(np.linalg.norm(left - right))
        # Address 帧被明显压缩时说明机位不是正面，退回 90 分位
        if math.isfinite(addr_width) and addr_width >= 0.6 * fallback:
            return addr_width
    return fallback if fallback > 0.0 else 1.0


def _spine_vec(frame: FrameLandmarks) -> np.ndarray:
    """脊柱向量：双肩中点 - 双髋中点（world）。"""
    return geometry.midpoint(
        frame.world[geometry.L_SHOULDER], frame.world[geometry.R_SHOULDER]
    ) - geometry.midpoint(frame.world[geometry.L_HIP], frame.world[geometry.R_HIP])


# ---------------------------------------------------------------------------
# 派生量（§8.2）
# ---------------------------------------------------------------------------


def _shoulder_turn_at(frame: FrameLandmarks, addr: FrameLandmarks) -> float:
    """肩部转动角（相对 Address）。"""
    return geometry.rotation_xz(
        frame.world[geometry.L_SHOULDER] - frame.world[geometry.R_SHOULDER],
        addr.world[geometry.L_SHOULDER] - addr.world[geometry.R_SHOULDER],
    )


def _hip_turn_at(frame: FrameLandmarks, addr: FrameLandmarks) -> float:
    """髋部转动角（相对 Address）。"""
    return geometry.rotation_xz(
        frame.world[geometry.L_HIP] - frame.world[geometry.R_HIP],
        addr.world[geometry.L_HIP] - addr.world[geometry.R_HIP],
    )


def _x_factor_at(frame: FrameLandmarks, addr: FrameLandmarks) -> float:
    """X-Factor = 肩转 − 髋转。"""
    return _shoulder_turn_at(frame, addr) - _hip_turn_at(frame, addr)


def _spine_forward_tilt_at(frame: FrameLandmarks) -> float:
    """脊柱前倾角。"""
    return geometry.tilt_from_vertical_yz(_spine_vec(frame))


# ---------------------------------------------------------------------------
# 指标函数（key -> fn(ctx) -> float）
# ---------------------------------------------------------------------------


def m_spine_forward_tilt(ctx: MetricContext) -> float:
    """① 脊柱前倾角。"""
    return _spine_forward_tilt_at(ctx.cur)


def m_stance_width_ratio(ctx: MetricContext) -> float:
    """① 站姿宽度比 = 双踝水平距 / world 肩宽。"""
    world = ctx.cur.world
    span = abs(float(world[geometry.L_ANKLE, 0]) - float(world[geometry.R_ANKLE, 0]))
    if ctx.S <= 1e-9:
        return float("nan")
    return span / ctx.S


def m_shoulder_line_tilt(ctx: MetricContext) -> float:
    """① 肩线水平倾角（右肩低于左肩为正）。"""
    return geometry.line_tilt(
        ctx.cur.world[geometry.L_SHOULDER], ctx.cur.world[geometry.R_SHOULDER]
    )


def m_knee_flex(ctx: MetricContext) -> float:
    """① 膝部弯曲角（左右膝均值）。"""
    world = ctx.cur.world
    left = geometry.angle_3p(
        world[geometry.L_HIP], world[geometry.L_KNEE], world[geometry.L_ANKLE]
    )
    right = geometry.angle_3p(
        world[geometry.R_HIP], world[geometry.R_KNEE], world[geometry.R_ANKLE]
    )
    values = [v for v in (left, right) if math.isfinite(v)]
    return float(np.mean(values)) if values else float("nan")


def m_shoulder_turn(ctx: MetricContext) -> float:
    """肩部转动角。"""
    return _shoulder_turn_at(ctx.cur, ctx.addr)


def m_hip_turn(ctx: MetricContext) -> float:
    """髋部转动角。"""
    return _hip_turn_at(ctx.cur, ctx.addr)


def m_x_factor(ctx: MetricContext) -> float:
    """X-Factor。"""
    return _x_factor_at(ctx.cur, ctx.addr)


def m_lead_arm_straight(ctx: MetricContext) -> float:
    """引导臂（左臂 11-13-15）伸直度。"""
    world = ctx.cur.world
    return geometry.angle_3p(
        world[geometry.L_SHOULDER], world[geometry.L_ELBOW], world[geometry.L_WRIST]
    )


def m_trail_elbow_flex(ctx: MetricContext) -> float:
    """后臂（右臂 12-14-16）弯曲角。"""
    world = ctx.cur.world
    return geometry.angle_3p(
        world[geometry.R_SHOULDER], world[geometry.R_ELBOW], world[geometry.R_WRIST]
    )


def m_trail_arm_extend(ctx: MetricContext) -> float:
    """后臂伸展度（与 :func:`m_trail_elbow_flex` 同口径，语义不同）。"""
    return m_trail_elbow_flex(ctx)


def m_head_drift_pct(ctx: MetricContext) -> float:
    """头部位移（% 肩宽）。"""
    return geometry.norm_disp_pct(
        _img_pt(ctx, ctx.cur, geometry.NOSE),
        _img_pt(ctx, ctx.addr, geometry.NOSE),
        ctx.S_px,
        axes=(0, 1),
    )


def m_pelvis_shift_pct(ctx: MetricContext) -> float:
    """骨盆水平位移（% 肩宽，向目标为正）。"""
    return geometry.signed_shift_pct(
        _img_hip_mid(ctx, ctx.cur), _img_hip_mid(ctx, ctx.addr), ctx.S_px
    )


def m_x_factor_retention(ctx: MetricContext) -> float:
    """⑤ X-Factor 保持率 = X-Factor(⑤) / X-Factor(④) × 100。"""
    top_value = _x_factor_at(ctx.frame_of(PhaseKey.TOP), ctx.addr)
    cur_value = _x_factor_at(ctx.frame_of(PhaseKey.DOWNSWING), ctx.addr)
    if not math.isfinite(top_value) or abs(top_value) < 1e-3:
        ctx.warn("顶点 X-Factor 过小，保持率按 100% 处理")
        return 100.0
    return cur_value / top_value * 100.0


def m_hip_open(ctx: MetricContext) -> float:
    """髋部开放角 = −髋转（向目标打开为正）。"""
    return -m_hip_turn(ctx)


def m_hip_to_target(ctx: MetricContext) -> float:
    """⑧ 髋部朝向目标角 = −髋转。"""
    return -m_hip_turn(ctx)


def m_shoulder_open(ctx: MetricContext) -> float:
    """肩部开放角 = −肩转。"""
    return -m_shoulder_turn(ctx)


def m_shoulder_square(ctx: MetricContext) -> float:
    """⑥ 肩部方正度 = −肩转（正值=已打开）。"""
    return -m_shoulder_turn(ctx)


def m_spine_tilt_delta(ctx: MetricContext) -> float:
    """⑥ 起身量 = 前倾角(Address) − 前倾角(Impact)，负值裁 0。"""
    addr_tilt = _spine_forward_tilt_at(ctx.addr)
    impact_tilt = _spine_forward_tilt_at(ctx.frame_of(PhaseKey.IMPACT))
    if not (math.isfinite(addr_tilt) and math.isfinite(impact_tilt)):
        return float("nan")
    return max(0.0, addr_tilt - impact_tilt)


def m_spine_lateral_tilt(ctx: MetricContext) -> float:
    """⑦ 脊柱侧倾（远离目标为正）。"""
    return geometry.tilt_from_vertical_xy(_spine_vec(ctx.cur))


def m_balance_hold_sec(ctx: MetricContext) -> float:
    """⑧ 收杆平衡保持时长（秒）。"""
    start = ctx.event_of(PhaseKey.FINISH).array_index
    speed = ctx.signals.speed
    count = 0
    i = start
    while i < len(speed) and float(speed[i]) < config.V_STILL:
        count += 1
        i += 1
    if i >= len(speed) and count > 0:
        ctx.warn("视频在收杆后过早结束，平衡保持时长可能被低估")
    return count * ctx.dt


def m_tempo_ratio(ctx: MetricContext) -> float:
    """全程 节奏比 = (①→④ 帧数) / (④→⑥ 帧数)。"""
    i_addr = ctx.event_of(PhaseKey.ADDRESS).array_index
    i_top = ctx.event_of(PhaseKey.TOP).array_index
    i_impact = ctx.event_of(PhaseKey.IMPACT).array_index
    return (i_top - i_addr) / float(max(1, i_impact - i_top))


def m_swing_duration(ctx: MetricContext) -> float:
    """全程 挥杆总时长（秒）= (⑧帧号 − ①帧号) / fps。"""
    f_addr = ctx.event_of(PhaseKey.ADDRESS).frame_index
    f_finish = ctx.event_of(PhaseKey.FINISH).frame_index
    if ctx.fps <= 0:
        return float("nan")
    return (f_finish - f_addr) / ctx.fps


def m_max_head_drift_pct(ctx: MetricContext) -> float:
    """全程 头部最大位移（% 肩宽），区间 ①→⑧。"""
    i_addr = ctx.event_of(PhaseKey.ADDRESS).array_index
    i_finish = ctx.event_of(PhaseKey.FINISH).array_index
    addr_pt = _img_pt(ctx, ctx.addr, geometry.NOSE)
    best = 0.0
    for i in range(i_addr, min(i_finish, len(ctx.frames) - 1) + 1):
        value = geometry.norm_disp_pct(
            _img_pt(ctx, ctx.frames[i], geometry.NOSE), addr_pt, ctx.S_px, axes=(0, 1)
        )
        if math.isfinite(value):
            best = max(best, value)
    return best


#: key -> 计算函数
METRIC_FUNCS: Dict[str, Callable[[MetricContext], float]] = {
    "spine_forward_tilt": m_spine_forward_tilt,
    "stance_width_ratio": m_stance_width_ratio,
    "shoulder_line_tilt": m_shoulder_line_tilt,
    "knee_flex": m_knee_flex,
    "shoulder_turn": m_shoulder_turn,
    "hip_turn": m_hip_turn,
    "x_factor": m_x_factor,
    "lead_arm_straight": m_lead_arm_straight,
    "trail_elbow_flex": m_trail_elbow_flex,
    "trail_arm_extend": m_trail_arm_extend,
    "head_drift_pct": m_head_drift_pct,
    "pelvis_shift_pct": m_pelvis_shift_pct,
    "x_factor_retention": m_x_factor_retention,
    "hip_open": m_hip_open,
    "hip_to_target": m_hip_to_target,
    "shoulder_open": m_shoulder_open,
    "shoulder_square": m_shoulder_square,
    "spine_tilt_delta": m_spine_tilt_delta,
    "spine_lateral_tilt": m_spine_lateral_tilt,
    "balance_hold_sec": m_balance_hold_sec,
    "tempo_ratio": m_tempo_ratio,
    "swing_duration": m_swing_duration,
    "max_head_drift_pct": m_max_head_drift_pct,
}

# 启动即自检：参考表里的每个 key 都必须有实现
_MISSING = [k for k in reference.all_metric_keys() if k not in METRIC_FUNCS]
if _MISSING:  # pragma: no cover - 配置错误应在导入期立刻暴露
    raise RuntimeError(f"METRIC_FUNCS missing implementations: {_MISSING}")


# ---------------------------------------------------------------------------
# 数值卫生与装配
# ---------------------------------------------------------------------------


def _sanitize(value: Optional[float], spec: MetricSpec, ctx: MetricContext) -> float:
    """保障无 NaN / inf，角度夹到 ±180，统一 round(1)。"""
    try:
        result = float(value) if value is not None else float("nan")
    except (TypeError, ValueError):
        result = float("nan")

    if math.isnan(result) or math.isinf(result):
        result = spec.ref_mid
        ctx.warn(f"{spec.name} 计算异常，已按参考中值填充")

    if spec.unit == reference.UNIT_DEG:
        result = geometry.clamp(result, -180.0, 180.0)
    return round(result, 1)


def _build_metric(spec: MetricSpec, ctx: MetricContext) -> StageMetric:
    """执行单个指标并封装成 :class:`StageMetric`（带缓存）。"""
    cache_key = f"{ctx.phase.value if ctx.phase else 'global'}:{spec.key}"
    if cache_key in ctx.cache:
        value = ctx.cache[cache_key]
    else:
        func = METRIC_FUNCS[spec.key]
        try:
            value = _sanitize(func(ctx), spec, ctx)
        except Exception:  # noqa: BLE001 - 单指标失败不应中断整份报告
            logger.exception("metric failed: %s", spec.key)
            value = _sanitize(float("nan"), spec, ctx)
        ctx.cache[cache_key] = value

    return StageMetric(
        key=spec.key,
        name=spec.name,
        value=value,
        unit=spec.unit,
        ref_min=spec.ref_min,
        ref_max=spec.ref_max,
        status=reference.judge(value, spec.ref_min, spec.ref_max),
    )


def compute_phase_metrics(ctx: MetricContext) -> List[StageMetric]:
    """计算 ``ctx.phase`` 阶段的全部指标。"""
    if ctx.phase is None:
        raise ValueError("MetricContext.phase is required")
    return [_build_metric(spec, ctx) for spec in reference.METRIC_SPECS[ctx.phase]]


def compute_global_metrics(ctx: MetricContext) -> GlobalMetrics:
    """计算 3 项全程指标。

    > 与类图 ``compute_global_metrics(frames, events, S)`` 的签名偏差：改为接收
    > :class:`MetricContext`（它已封装 frames / events / S / signals / meta），
    > 避免重复传参并复用 warning 与缓存机制。
    """
    ctx.phase = None
    metrics = [_build_metric(spec, ctx) for spec in reference.GLOBAL_SPECS]
    by_key = {m.key: m.value for m in metrics}
    return GlobalMetrics(
        tempo_ratio=by_key["tempo_ratio"],
        swing_duration=by_key["swing_duration"],
        max_head_drift_pct=by_key["max_head_drift_pct"],
        metrics=metrics,
    )


def build_context(
    frames: List[FrameLandmarks],
    events: List[SwingEvent],
    signals: SwingSignals,
    meta: VideoMeta,
) -> MetricContext:
    """装配 :class:`MetricContext`。"""
    addr_index = next(
        (e.array_index for e in events if e.key is PhaseKey.ADDRESS), 0
    )
    world_scale = geometry.shoulder_width(frames[addr_index].world)
    if not math.isfinite(world_scale) or world_scale <= 1e-6:
        candidates = [
            geometry.shoulder_width(f.world)
            for f in frames
            if math.isfinite(geometry.shoulder_width(f.world))
        ]
        world_scale = float(np.median(candidates)) if candidates else 1.0
    return MetricContext(
        frames=frames,
        events=events,
        signals=signals,
        meta=meta,
        S=world_scale,
        S_px=image_shoulder_width_px(frames, meta, ref_index=addr_index),
    )
