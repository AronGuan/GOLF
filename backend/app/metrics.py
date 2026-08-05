"""指标计算引擎（架构 ARCHITECTURE.md §8 + ARCHITECTURE-v2.md §5）。

依赖方向严格单向：``metrics -> reference / geometry / schemas / config``。

设计要点
--------
1. 所有**角度**指标基于 world 3D 坐标（米制，原点=双髋中点）。
2. 所有**位移**指标基于归一化图像坐标换算的像素坐标，并以 **``ctx.scale_px``**
   归一化：face-on = Address 帧图像肩宽（``S_px``）；DTL = 图像身高 ×
   ``config.SHOULDER_TO_HEIGHT_RATIO``（侧面双肩投影被压缩，不能用图像肩宽）。
3. 每个指标出口统一过 :func:`_sanitize`，保证无 ``NaN`` / ``inf``，角度夹到 ±180；
   对 ``allow_drop=True`` 的指标（``swing_plane``）失败时
   **整项剔除**（返回 ``None``），绝不填 ``ref_mid`` 造假绿值。
4. 指标函数按 ``MetricSpec.impl_key`` 分派（``fn_key or key``），对外 key 与
   实现 key 的映射收敛在 ``reference.py``，本模块零映射逻辑。

> ⚠️ 2026-08：球杆检测已下线（相关指标移除，见 config.py §8 说明）。
> ``swing_plane``（PDD 版，纯 MediaPipe 左肩 11→左腕 15）不依赖球杆，原样保留。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import config, geometry, reference
from .reference import MetricSpec
from .schemas import (
    CameraView,
    FrameLandmarks,
    GlobalMetrics,
    MetricSource,
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
    #: 图像肩宽（像素），全片中位数（face-on 位移标尺）
    S_px: float
    #: 当前阶段（``compute_global_metrics`` 时为 None）
    phase: Optional[PhaseKey] = None
    cache: Dict[str, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    # ---- v2 新增 ----
    #: 实际解析后的机位（进入本模块前必须已是 FACE_ON / DOWN_THE_LINE 之一）
    view: CameraView = CameraView.FACE_ON
    #: Address 帧图像身高（像素），DTL 位移标尺的基准
    body_h_px: float = 0.0
    #: 位移类指标的像素标尺：face_on = ``S_px``；DTL = ``body_h_px × 0.25``
    scale_px: float = 1.0
    #: 指标函数副作用写回：``{spec.key: (MetricSource, confidence)}``
    #: （L1 代理 / L0 实测置信度回传，避免改所有指标函数返回类型）
    source_of: Dict[str, Tuple[MetricSource, float]] = field(default_factory=dict)

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

    ⚠️ **DTL 机位禁止使用本函数做归一化**（双肩前后重叠，投影肩宽被全片压缩，
    90 分位本身就是压缩值）。DTL 标尺见 :func:`build_context` 的 ``scale_px``。

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


def _body_height_px_px(frame: FrameLandmarks, meta: VideoMeta) -> float:
    """单帧图像身高（像素）= |y(鼻) − y(双踝中点)|。"""
    nose_y = float(frame.norm[geometry.NOSE, 1]) * meta.height
    ankle_mid_y = (
        float(frame.norm[geometry.L_ANKLE, 1])
        + float(frame.norm[geometry.R_ANKLE, 1])
    ) / 2.0 * meta.height
    return geometry.body_height_px(nose_y, ankle_mid_y)


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


def _spine_forward_tilt_at(
    frame: FrameLandmarks, view: CameraView = CameraView.FACE_ON
) -> float:
    """脊柱前倾角（按机位分派投影面，架构 §5.6 A7）。

    - face-on：前倾发生在 world y-z 面（球手正对相机，前倾 = 肩髋连线沿深度
      方向偏移）→ ``tilt_from_vertical_yz``；
    - DTL：球手侧对相机，前倾发生在图像 x-y 面（肩髋连线沿画面水平方向偏移）
      → ``abs(tilt_from_vertical_xy)``（取幅值，方向无关）。
    """
    vec = _spine_vec(frame)
    if view is CameraView.DOWN_THE_LINE:
        return abs(geometry.tilt_from_vertical_xy(vec))
    return geometry.tilt_from_vertical_yz(vec)


# ---------------------------------------------------------------------------
# 指标函数（fn_key -> fn(ctx) -> float；失败返回 NaN，不抛异常）
# ---------------------------------------------------------------------------


def m_spine_forward_tilt(ctx: MetricContext) -> float:
    """① 脊柱前倾角（DTL 专属，按机位分派投影面）。"""
    return _spine_forward_tilt_at(ctx.cur, ctx.view)


def m_stance_width_ratio(ctx: MetricContext) -> float:
    """① 站姿宽度比 = 双踝水平距 / world 肩宽。"""
    world = ctx.cur.world
    span = abs(float(world[geometry.L_ANKLE, 0]) - float(world[geometry.R_ANKLE, 0]))
    if ctx.S <= 1e-9:
        return float("nan")
    return span / ctx.S


def m_shoulder_line_tilt(ctx: MetricContext) -> float:
    """① 肩线水平倾角（右肩低于左肩为正）。

    ⚠️ v2 起不再被任何 ``MetricSpec`` 引用（① ``spine_tilt_side`` 换用
    ``m_spine_lateral_tilt``），保留函数仅为兼容探针/历史调用。
    """
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
    """头部位移（% 标尺；标尺 = ``ctx.scale_px`` 按机位切换）。"""
    return geometry.norm_disp_pct(
        _img_pt(ctx, ctx.cur, geometry.NOSE),
        _img_pt(ctx, ctx.addr, geometry.NOSE),
        ctx.scale_px,
        axes=(0, 1),
    )


def m_pelvis_shift_pct(ctx: MetricContext) -> float:
    """骨盆水平位移（% 标尺，向目标为正）。"""
    return geometry.signed_shift_pct(
        _img_hip_mid(ctx, ctx.cur), _img_hip_mid(ctx, ctx.addr), ctx.scale_px
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
    """肩部开放角 = −肩转（正值）。"""
    return -m_shoulder_turn(ctx)


def m_shoulder_square(ctx: MetricContext) -> float:
    """⑥ 肩部方正度 = −肩转（正值=已打开）。"""
    return -m_shoulder_turn(ctx)


def m_spine_tilt_delta(ctx: MetricContext) -> float:
    """⑥ 起身量 = 前倾角(Address) − 前倾角(Impact)，负值裁 0。

    ⚠️ 公式以现有实现为准（PDD 写成「击球 − Address」会得负值，与其自己的
    ``≥10°`` 正阈值矛盾，判为 PDD 笔误，架构 §10 待明确 #A2）。
    """
    addr_tilt = _spine_forward_tilt_at(ctx.addr, ctx.view)
    impact_tilt = _spine_forward_tilt_at(ctx.frame_of(PhaseKey.IMPACT), ctx.view)
    if not (math.isfinite(addr_tilt) and math.isfinite(impact_tilt)):
        return float("nan")
    return max(0.0, addr_tilt - impact_tilt)


def m_spine_lateral_tilt(ctx: MetricContext) -> float:
    """脊柱侧倾（远离目标为正）。"""
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
    """全程 头部最大位移（% 标尺），区间 ①→⑧。"""
    i_addr = ctx.event_of(PhaseKey.ADDRESS).array_index
    i_finish = ctx.event_of(PhaseKey.FINISH).array_index
    addr_pt = _img_pt(ctx, ctx.addr, geometry.NOSE)
    best = 0.0
    for i in range(i_addr, min(i_finish, len(ctx.frames) - 1) + 1):
        value = geometry.norm_disp_pct(
            _img_pt(ctx, ctx.frames[i], geometry.NOSE), addr_pt, ctx.scale_px, axes=(0, 1)
        )
        if math.isfinite(value):
            best = max(best, value)
    return best


# ---------------------------------------------------------------------------
# v2 新增：swing_plane（纯 MediaPipe，不依赖球杆）
# ---------------------------------------------------------------------------


def m_swing_plane(ctx: MetricContext) -> float:
    """④ 顶点时引导臂（左肩 11 → 左腕 15）与图像水平线的夹角（°）。

    - 用【图像像素坐标】而非 world：PDD 口径是"与水平面的夹角"，DTL 机位下
      图像水平线即地平线代理（拍摄指引强制手机保持水平）。
    - 结果落在 [0, 180)，取锐角侧：``value > 90`` 时用 ``180 − value``。
    - 关键点可见度守卫：左肩/左腕任一 ``visibility < 0.5`` -> 返回 NaN
      -> ``allow_drop`` 整项剔除（绝不填 ``ref_mid`` 造假绿值）。
    """
    top = ctx.frame_of(PhaseKey.TOP)
    if (
        not math.isfinite(float(top.visibility[geometry.L_SHOULDER]))
        or not math.isfinite(float(top.visibility[geometry.L_WRIST]))
        or float(top.visibility[geometry.L_SHOULDER]) < 0.5
        or float(top.visibility[geometry.L_WRIST]) < 0.5
    ):
        ctx.warn("顶点关键点可见度不足，挥杆平面角无法计算，已跳过该指标")
        return float("nan")

    a = _img_pt(ctx, top, geometry.L_SHOULDER)
    b = _img_pt(ctx, top, geometry.L_WRIST)
    ang = geometry.line_angle_from_horizontal(a, b)
    if not math.isfinite(ang):
        ctx.warn("顶点引导臂夹角异常，挥杆平面角无法计算，已跳过该指标")
        return float("nan")
    return 180.0 - ang if ang > 90.0 else ang


#: key -> 计算函数（key = 实现 key / fn_key）
METRIC_FUNCS: Dict[str, Callable[[MetricContext], float]] = {
    "spine_forward_tilt": m_spine_forward_tilt,
    "stance_width_ratio": m_stance_width_ratio,
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
    # ---- v2 新增 ----
    "swing_plane": m_swing_plane,
}

# 启动即自检：参考表里的每个实现 key 都必须有实现
_MISSING = [k for k in reference.all_metric_keys() if k not in METRIC_FUNCS]
if _MISSING:  # pragma: no cover - 配置错误应在导入期立刻暴露
    raise RuntimeError(f"METRIC_FUNCS missing implementations: {_MISSING}")


# ---------------------------------------------------------------------------
# 数值卫生与装配
# ---------------------------------------------------------------------------


def _sanitize(value: Optional[float], spec: MetricSpec, ctx: MetricContext) -> Optional[float]:
    """保障无 NaN / inf，角度夹到 ±180，统一 round(1)。

    ``allow_drop=True`` 的指标遇到 NaN/inf -> 返回 ``None``（整项剔除）并告警；
    其余指标保持既有兜底行为（填 ``ref_mid`` + 告警），现有 23 个指标零变化。
    """
    try:
        result = float(value) if value is not None else float("nan")
    except (TypeError, ValueError):
        result = float("nan")

    if math.isnan(result) or math.isinf(result):
        if spec.allow_drop:
            ctx.warn(f"{spec.name} 计算异常，已跳过该指标")
            return None
        result = spec.ref_mid
        ctx.warn(f"{spec.name} 计算异常，已按参考中值填充")

    if spec.unit == reference.UNIT_DEG:
        result = geometry.clamp(result, -180.0, 180.0)
    return round(result, 1)


def _build_metric(spec: MetricSpec, ctx: MetricContext) -> Optional[StageMetric]:
    """执行单个指标并封装成 :class:`StageMetric`（带缓存）。

    返回 ``None`` 表示该指标被 ``allow_drop`` 剔除，调用方应过滤。
    """
    cache_key = f"{ctx.phase.value if ctx.phase else 'global'}:{spec.key}"
    if cache_key in ctx.cache:
        value = ctx.cache[cache_key]
        if value is None:
            return None
    else:
        func = METRIC_FUNCS[spec.impl_key]
        try:
            value = _sanitize(func(ctx), spec, ctx)
        except Exception:  # noqa: BLE001 - 单指标失败不应中断整份报告
            logger.exception("metric failed: %s", spec.key)
            value = _sanitize(float("nan"), spec, ctx)
        ctx.cache[cache_key] = value
        if value is None:
            return None

    # ---- 来源 / 置信度 / L1 代理参考区间放宽 ------------------------------
    ref_min = spec.ref_min
    ref_max = spec.ref_max
    estimated = False
    source = MetricSource.MEASURED
    confidence = 1.0
    if spec.key in ctx.source_of:
        source, confidence = ctx.source_of[spec.key]
        estimated = source is MetricSource.PROXY
        if source is MetricSource.PROXY and spec.proxy_ref_pad > 0.0:
            ref_min -= spec.proxy_ref_pad
            ref_max += spec.proxy_ref_pad

    return StageMetric(
        key=spec.key,
        name=spec.name,
        value=value,
        unit=spec.unit,
        ref_min=ref_min,
        ref_max=ref_max,
        status=reference.judge5(value, ref_min, ref_max, spec.critical),
        estimated=estimated,
        source=source,
        confidence=confidence,
        description=spec.description,
    )


def _specs_for(ctx: MetricContext) -> List[MetricSpec]:
    """机位过滤的唯一实现点（架构 §9.5 铁律 2）。"""
    return [s for s in reference.METRIC_SPECS[ctx.phase] if s.supports(ctx.view)]


def compute_phase_metrics(ctx: MetricContext) -> List[StageMetric]:
    """计算 ``ctx.phase`` 阶段在该机位下适用的全部指标（含 allow_drop 剔除）。"""
    if ctx.phase is None:
        raise ValueError("MetricContext.phase is required")
    return [
        m for m in (_build_metric(spec, ctx) for spec in _specs_for(ctx)) if m is not None
    ]


def compute_global_metrics(ctx: MetricContext) -> GlobalMetrics:
    """计算 3 项全程指标。"""
    ctx.phase = None
    metrics = [
        m for m in (_build_metric(spec, ctx) for spec in reference.GLOBAL_SPECS) if m is not None
    ]
    by_key = {m.key: m.value for m in metrics}
    return GlobalMetrics(
        tempo_ratio=by_key["tempo_ratio"],
        swing_duration=by_key["swing_duration"],
        max_head_drift_pct=by_key["max_head_drift"],
        metrics=metrics,
    )


def build_context(
    frames: List[FrameLandmarks],
    events: List[SwingEvent],
    signals: SwingSignals,
    meta: VideoMeta,
    view: CameraView = CameraView.FACE_ON,
) -> MetricContext:
    """装配 :class:`MetricContext`。

    Args:
        frames / events / signals / meta: 与 MVP 相同。
        view: 实际解析后的机位（进入本模块前必须已是 FACE_ON / DOWN_THE_LINE）。
    """
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

    s_px = image_shoulder_width_px(frames, meta, ref_index=addr_index)
    addr_frame = frames[addr_index]
    body_h_px = _body_height_px_px(addr_frame, meta)

    # ---- DTL 等效肩宽标尺（架构 §5.6 A6）--------------------------------
    scale_px = s_px
    if view is CameraView.DOWN_THE_LINE:
        if math.isfinite(body_h_px) and body_h_px > 0.0:
            # DTL 下双肩前后重叠、投影肩宽被压缩，改用 图像身高 × 0.25 作等效肩宽
            scale_px = body_h_px * config.SHOULDER_TO_HEIGHT_RATIO
        # 身高不可用 → 回退图像肩宽（极少见），保持数值卫生
        else:
            scale_px = s_px

    return MetricContext(
        frames=frames,
        events=events,
        signals=signals,
        meta=meta,
        S=world_scale,
        S_px=s_px,
        view=view,
        body_h_px=body_h_px,
        scale_px=scale_px,
    )
