"""纯几何工具与 MediaPipe 关键点索引常量。

全部为无状态纯函数，入参为 :class:`numpy.ndarray`，便于单测与反复调参。
坐标系约定见架构文档 §10.2；符号约定见 §10.3（``config.ROTATION_SIGN`` /
``config.TARGET_DIR_X``）。
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence, Tuple

import cv2
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


# ---------------------------------------------------------------------------
# 图像平面几何（球杆检测专用，坐标单位 = 像素，y 向下）
#
# ⚠️ 单目投影角近似：以下函数产出的都是**图像投影角**，不是真实空间角。
#    无相机标定不可宣称绝对精度，详见 docs/ADR-001-club-detection.md。
# ---------------------------------------------------------------------------


def line_angle_from_horizontal(p1: np.ndarray, p2: np.ndarray) -> float:
    """两点所在**直线**与图像水平线的夹角，返回 ``[0, 180)`` 度。

    图像坐标 y 向下，为了让读数符合直觉（视觉上「右上倾斜」= 角度增大），
    内部对 dy 取反后再算 ``atan2``；因为描述的是直线而非射线，结果对 180°
    取模，故 ``line_angle_from_horizontal(a, b) == line_angle_from_horizontal(b, a)``。

    Args:
        p1: ``(2,)`` 端点像素坐标。
        p2: ``(2,)`` 端点像素坐标。

    Returns:
        角度（度），两点重合时返回 ``nan``。
    """
    a = np.asarray(p1, dtype=np.float64).ravel()
    b = np.asarray(p2, dtype=np.float64).ravel()
    dx = float(b[0]) - float(a[0])
    dy = float(b[1]) - float(a[1])
    if math.hypot(dx, dy) < _EPS:
        return float("nan")
    return float(math.degrees(math.atan2(-dy, dx)) % 180.0)


def point_line_distance(point: np.ndarray, p1: np.ndarray, p2: np.ndarray) -> float:
    """点到「过 ``p1`` ``p2`` 的无限长直线」的垂距（像素）。

    用于球杆检测过滤①：杆身延长线必须逼近握把。
    ``p1 == p2`` 时退化为点到点距离。
    """
    pt = np.asarray(point, dtype=np.float64).ravel()
    a = np.asarray(p1, dtype=np.float64).ravel()
    b = np.asarray(p2, dtype=np.float64).ravel()
    direction = b - a
    length = float(np.linalg.norm(direction))
    if length < _EPS:
        return float(np.linalg.norm(pt - a))
    cross = float(direction[0] * (a[1] - pt[1]) - direction[1] * (a[0] - pt[0]))
    return abs(cross) / length


def project_along(
    origin: np.ndarray, direction: np.ndarray, distance: float
) -> np.ndarray:
    """从 ``origin`` 沿 ``direction`` 外推 ``distance`` 像素。

    Args:
        origin: ``(2,)`` 起点（通常是握把）。
        direction: ``(2,)`` 方向向量，内部归一化，无需预先单位化。
        distance: 外推距离（像素）。

    Returns:
        ``(2,)`` 落点；``direction`` 为零向量时返回 ``origin`` 副本。
    """
    start = np.asarray(origin, dtype=np.float64).ravel().astype(np.float64)
    vec = np.asarray(direction, dtype=np.float64).ravel().astype(np.float64)
    norm = float(np.linalg.norm(vec))
    if norm < _EPS:
        return start.copy()
    return start + vec / norm * float(distance)


def angle_between_lines(
    a1: np.ndarray, a2: np.ndarray, b1: np.ndarray, b2: np.ndarray
) -> float:
    """两条**直线**的夹角，返回 ``[0, 90]`` 度（无向）。

    用于球杆检测过滤②（与时序预测方向的一致性）与过滤③（与骨架段共线排除）。
    任一直线退化为点时返回 ``nan``。
    """
    u = np.asarray(a2, dtype=np.float64).ravel() - np.asarray(a1, dtype=np.float64).ravel()
    v = np.asarray(b2, dtype=np.float64).ravel() - np.asarray(b1, dtype=np.float64).ravel()
    nu = float(np.linalg.norm(u))
    nv = float(np.linalg.norm(v))
    if nu < _EPS or nv < _EPS:
        return float("nan")
    cos_theta = clamp(abs(float(np.dot(u, v))) / (nu * nv), 0.0, 1.0)
    return float(math.degrees(math.acos(cos_theta)))


def fit_line_2d(points: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """对 ``(k, 2)`` 点集做总体最小二乘直线拟合（PCA 主轴）。

    与 ``numpy.polyfit`` 的区别：不假设 ``y = f(x)``，因此**竖直方向的杆身
    也能正确拟合**（下杆段杆身常常接近铅垂，用 polyfit 会数值爆炸）。

    Args:
        points: ``(k, 2)`` 像素坐标，``k >= 2``。

    Returns:
        ``(centroid, unit_direction)``；点数不足或全部重合时方向返回 ``(nan, nan)``。
    """
    data = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if data.shape[0] < 2:
        centroid = data[0].copy() if data.shape[0] == 1 else np.zeros(2)
        return centroid, np.array([np.nan, np.nan])
    centroid = data.mean(axis=0)
    centered = data - centroid
    if float(np.max(np.abs(centered))) < _EPS:
        return centroid, np.array([np.nan, np.nan])
    # SVD 的第一右奇异向量即主轴方向
    _u, _s, vt = np.linalg.svd(centered, full_matrices=False)
    direction = np.asarray(vt[0], dtype=np.float64)
    norm = float(np.linalg.norm(direction))
    if norm < _EPS:
        return centroid, np.array([np.nan, np.nan])
    return centroid, direction / norm


def body_height_px(nose_y: float, ankle_mid_y: float) -> float:
    """图像身高（像素）= ``|y(鼻) − y(双踝中点)|``。

    这是 **DTL（侧面）机位唯一可靠的像素标尺**：侧面机位下双肩与相机光轴近似
    共线，投影肩宽被全片压缩，``metrics.image_shoulder_width_px()`` 的"低于
    90 分位就回落"守卫救不了（90 分位本身就是压缩值）。身高方向与光轴垂直，
    不受该压缩影响。

    Returns:
        像素身高；入参非有限值时返回 ``nan``。
    """
    try:
        top = float(nose_y)
        bottom = float(ankle_mid_y)
    except (TypeError, ValueError):
        return float("nan")
    if not (math.isfinite(top) and math.isfinite(bottom)):
        return float("nan")
    return abs(bottom - top)


def skeleton_polygon_mask(
    landmarks_px: np.ndarray,
    shape: Tuple[int, int],
    edges: Iterable[Tuple[int, int]] = SKELETON_EDGES,
    thickness: Optional[int] = None,
    dilate: Optional[int] = None,
) -> np.ndarray:
    """由骨架连线生成**人体粗掩膜**（球杆检测过滤③④用）。

    做法：躯干四点（双肩+双髋）填充凸多边形 + 所有 ``edges`` 画粗线，再整体膨胀。
    这是"零成本"方案——刻意**不**开 ``POSE_KW["enable_segmentation"]``
    拿精细掩膜：那会让 MediaPipe 单帧推理 +15%（全片约 +3~5s），性价比不划算。

    Args:
        landmarks_px: ``(33, 2)`` 或 ``(33, 3)`` 像素坐标（多余列被忽略）。
        shape: ``(height, width)`` 目标掩膜尺寸。
        edges: 骨架连线，默认 :data:`SKELETON_EDGES`。
        thickness: 线宽（像素）；``None`` 时按画幅对角线的 2.2% 自适应。
        dilate: 额外膨胀半径（像素）；``None`` 时取 ``thickness``。

    Returns:
        ``(height, width)`` 的 ``uint8`` 掩膜，人体区域 = 255，其余 = 0。
        入参非法时返回全 0 掩膜（安全侧：不误杀任何候选）。
    """
    height, width = int(shape[0]), int(shape[1])
    mask = np.zeros((max(1, height), max(1, width)), dtype=np.uint8)

    pts = np.asarray(landmarks_px, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < NUM_LANDMARKS or pts.shape[1] < 2:
        return mask

    diag = math.hypot(float(width), float(height))
    line_w = int(thickness) if thickness is not None else max(3, int(round(diag * 0.022)))
    line_w = max(1, line_w)
    grow = int(dilate) if dilate is not None else line_w

    def _pt(idx: int) -> Optional[Tuple[int, int]]:
        x, y = float(pts[idx, 0]), float(pts[idx, 1])
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        return int(round(x)), int(round(y))

    # 1) 躯干四边形填充（肩-肩-髋-髋），保证胸腹区域是实心的
    torso_ids = (L_SHOULDER, R_SHOULDER, R_HIP, L_HIP)
    torso = [_pt(i) for i in torso_ids]
    if all(p is not None for p in torso):
        cv2.fillConvexPoly(
            mask, np.array(torso, dtype=np.int32), 255, lineType=cv2.LINE_8
        )

    # 2) 四肢与躯干连线画粗
    for a, b in edges:
        pa, pb = _pt(int(a)), _pt(int(b))
        if pa is None or pb is None:
            continue
        cv2.line(mask, pa, pb, 255, line_w, lineType=cv2.LINE_8)

    # 3) 头部：以鼻为中心画圆（面部点被 SKELETON_EDGES 剔除了）
    nose = _pt(NOSE)
    if nose is not None:
        cv2.circle(mask, nose, max(2, line_w), 255, -1, lineType=cv2.LINE_8)

    # 4) 整体膨胀，覆盖衣物轮廓与姿态抖动
    if grow > 0:
        kernel_size = 2 * grow + 1
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
        )
        mask = cv2.dilate(mask, kernel, iterations=1)
    return mask
