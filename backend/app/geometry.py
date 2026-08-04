"""纯几何工具与 MediaPipe 关键点索引常量。

全部为无状态纯函数，入参为 :class:`numpy.ndarray`，便于单测与反复调参。
坐标系约定见架构文档 §10.2；符号约定见 §10.3（``config.ROTATION_SIGN`` /
``config.TARGET_DIR_X``）。
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple

import numpy as np

from . import config

# ---------------------------------------------------------------------------
# BlazePose 33 点索引常量
# ---------------------------------------------------------------------------

NOSE: int = 0
L_EYE: int = 2
R_EYE: int = 5
L_SHOULDER: int = 11
R_SHOULDER: int = 12
L_ELBOW: int = 13
R_ELBOW: int = 14
L_WRIST: int = 15
R_WRIST: int = 16
L_HIP: int = 23
R_HIP: int = 24
L_KNEE: int = 25
R_KNEE: int = 26
L_ANKLE: int = 27
R_ANKLE: int = 28

#: 13 个核心点，用于质量评估与渲染
CORE_IDS: Tuple[int, ...] = (0, 11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28)

#: 关键点总数
NUM_LANDMARKS: int = 33

#: 骨架连线（躯干 + 四肢，剔除面部，正面机位更清爽）
SKELETON_EDGES: Tuple[Tuple[int, int], ...] = (
    (L_SHOULDER, R_SHOULDER),
    (L_SHOULDER, L_HIP),
    (R_SHOULDER, R_HIP),
    (L_HIP, R_HIP),
    (L_SHOULDER, L_ELBOW),
    (L_ELBOW, L_WRIST),
    (R_SHOULDER, R_ELBOW),
    (R_ELBOW, R_WRIST),
    (L_HIP, L_KNEE),
    (L_KNEE, L_ANKLE),
    (R_HIP, R_KNEE),
    (R_KNEE, R_ANKLE),
)

_EPS: float = 1e-9


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def midpoint(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """两点中点。"""
    return (np.asarray(a, dtype=np.float64) + np.asarray(b, dtype=np.float64)) / 2.0


def clamp(value: float, low: float, high: float) -> float:
    """把 ``value`` 夹到 ``[low, high]``。"""
    return float(min(max(value, low), high))


def shoulder_width(world: np.ndarray) -> float:
    """world 坐标下的肩宽（米）。"""
    return float(
        np.linalg.norm(
            np.asarray(world[L_SHOULDER], dtype=np.float64)
            - np.asarray(world[R_SHOULDER], dtype=np.float64)
        )
    )


# ---------------------------------------------------------------------------
# 角度
# ---------------------------------------------------------------------------


def angle_3p(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    """三点夹角（以 ``b`` 为顶点），返回 0~180 度。"""
    u = np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64)
    v = np.asarray(c, dtype=np.float64) - np.asarray(b, dtype=np.float64)
    nu = float(np.linalg.norm(u))
    nv = float(np.linalg.norm(v))
    if nu < _EPS or nv < _EPS:
        return float("nan")
    cos_theta = clamp(float(np.dot(u, v)) / (nu * nv), -1.0, 1.0)
    return float(math.degrees(math.acos(cos_theta)))


def rotation_xz(v_now: np.ndarray, v_ref: np.ndarray) -> float:
    """两向量在水平面(x-z)上的有符号转动角，返回 −180~180 度。

    以 ``v_ref``（Address 基准）为零点，背对目标方向为正
    （由 :data:`config.ROTATION_SIGN` 控制）。
    """
    ref = np.asarray(v_ref, dtype=np.float64)
    now = np.asarray(v_now, dtype=np.float64)
    x1, z1 = float(ref[0]), float(ref[2])
    x2, z2 = float(now[0]), float(now[2])
    if (abs(x1) + abs(z1)) < _EPS or (abs(x2) + abs(z2)) < _EPS:
        return float("nan")
    cross = x1 * z2 - z1 * x2
    dot = x1 * x2 + z1 * z2
    return float(math.degrees(math.atan2(cross, dot)) * config.ROTATION_SIGN)


def tilt_from_vertical_yz(v: np.ndarray) -> float:
    """向量在 y-z 面内与铅垂线的夹角（前倾角），返回 0~90 度。"""
    vec = np.asarray(v, dtype=np.float64)
    if abs(vec[1]) < _EPS and abs(vec[2]) < _EPS:
        return float("nan")
    return float(math.degrees(math.atan2(abs(float(vec[2])), abs(float(vec[1])))))


def tilt_from_vertical_xy(v: np.ndarray) -> float:
    """向量在 x-y 面内与铅垂线的夹角（侧倾角），向**远离目标**为正。"""
    vec = np.asarray(v, dtype=np.float64)
    if abs(vec[0]) < _EPS and abs(vec[1]) < _EPS:
        return float("nan")
    return float(
        -config.TARGET_DIR_X
        * math.degrees(math.atan2(float(vec[0]), -float(vec[1])))
    )


def line_tilt(p_left: np.ndarray, p_right: np.ndarray) -> float:
    """两点连线相对水平线的倾角，右侧低于左侧为正。"""
    a = np.asarray(p_left, dtype=np.float64)
    b = np.asarray(p_right, dtype=np.float64)
    dx = abs(float(b[0]) - float(a[0]))
    dy = float(b[1]) - float(a[1])
    if dx < _EPS and abs(dy) < _EPS:
        return float("nan")
    return float(math.degrees(math.atan2(dy, dx)))


# ---------------------------------------------------------------------------
# 位移（以肩宽归一化）
# ---------------------------------------------------------------------------


def norm_disp_pct(
    p_now: np.ndarray,
    p_ref: np.ndarray,
    scale: float,
    axes: Sequence[int] = (0, 1),
) -> float:
    """两点位移相对 ``scale`` 的百分比（无符号）。"""
    if scale is None or not math.isfinite(scale) or abs(scale) < _EPS:
        return float("nan")
    a = np.asarray(p_now, dtype=np.float64)
    b = np.asarray(p_ref, dtype=np.float64)
    idx = list(axes)
    delta = a[idx] - b[idx]
    return float(np.linalg.norm(delta) / abs(scale) * 100.0)


def signed_shift_pct(p_now: np.ndarray, p_ref: np.ndarray, scale: float) -> float:
    """水平位移百分比，向目标方向为正。"""
    if scale is None or not math.isfinite(scale) or abs(scale) < _EPS:
        return float("nan")
    a = np.asarray(p_now, dtype=np.float64)
    b = np.asarray(p_ref, dtype=np.float64)
    return float(config.TARGET_DIR_X * (float(a[0]) - float(b[0])) / abs(scale) * 100.0)
