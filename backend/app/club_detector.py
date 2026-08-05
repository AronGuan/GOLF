"""球杆几何检测核心（球杆检测技术方案 §4.2，路径 A + 路径 C）。

算法总览::

    Step 1  握把锚点   grip = midpoint(wrist_L, wrist_R)          ← 复用已有姿态数据，免费
    Step 2  杆长先验   DTL: 0.52~0.66 × 图像身高
                       face-on: 2.0~2.8 × 图像肩宽
    Step 3  ROI 构造   Address ±45° 扇形；后续帧用「上一帧杆身方向 + 手腕速度」
                       一阶预测，扇形收窄到 ±25°
    Step 4  Hough 分支 CLAHE → Canny → HoughLinesP → 四道过滤 → argmax(长度×边缘×一致性)
    Step 5  杆头定位   沿最优方向外推 club_len_px，在 0.15×L 邻域内轴向精修
    Step 6  帧差分支   absdiff → 阈值 → 闭运算 → 扣人体粗掩膜 → 环带内最大连通域质心
    Step 7  时序滤波   插值 + 滑动平均；confidence 合成；overall = 关键帧①④⑤⑥ 中位数

**为什么 A 和 C 必须组合**：两者失效区间恰好互补——低速段（Address/Takeaway/Top）
杆身锐利，Hough 强、帧差几乎无信号；高速段（Downswing/Impact）严重运动模糊、
直线边缘被抹掉，Hough 弱而运动残影反而勾勒出杆的扫过区域。门控信号
``SwingSignals.speed`` 由 ``segmenter.build_signals()`` 现成产出，零额外成本。

🔴 **模块级硬约束：本模块禁止外抛异常。**
任何失败（视频读不出、关键点缺失、一条线都没检出、OpenCV 报错……）都必须被
:func:`detect` 吞掉并统一返回 ``ClubTrack(available=False, detections=[])``。
挥杆分析主链路（已跑通的 23 个指标）不能被这个增量特性拖垮。
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from . import config, frame_reader, geometry
from .pose_extractor import moving_average
from .schemas import (
    CameraView,
    ClubDetection,
    ClubTrack,
    FrameLandmarks,
    PhaseKey,
    SwingEvent,
    SwingSignals,
    VideoMeta,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模块内调参常量
#
# 刻意**不**放进 config.py：这些是算法内部实现细节（ROI 半径系数、Canny 档位、
# 连通域环带…），不属于 config 第 8 区那份"对外可调"契约清单，避免污染。
# 真要外露时再上提。
# ---------------------------------------------------------------------------

#: 单次检测最多解码的帧数（8 事件帧 ±1 + Top→Impact 窗口采样）。
#: 由 ``config.CLUB_MAX_DECODE_FRAMES`` 驱动（架构 §5.2 内存护栏）。

#: Top→Impact 窗口的等间隔采样点数
_WINDOW_SAMPLES: int = 16

#: ROI 扇形半径 = 该系数 × club_len_px
_ROI_RADIUS_RATIO: float = 1.3

#: 杆头轴向精修半径 = 该系数 × club_len_px
_HEAD_REFINE_RATIO: float = 0.15

#: 帧差分支保留连通域的环带（× club_len_px）
_DIFF_BAND_RATIO: Tuple[float, float] = (0.40, 1.30)

#: 过滤③：与骨架段「近似共线」的判据（夹角 + 距离系数 × club_len_px）
_SKELETON_PARALLEL_DEG: float = 12.0
_SKELETON_NEAR_RATIO: float = 0.06

#: Canny 双阈值
_CANNY_LO: int = 50
_CANNY_HI: int = 150

#: 帧差二值化阈值（灰度差）
_DIFF_THRESH: int = 25

#: 帧差分支的置信度折扣（质心定位天然比直线拟合粗）
_DIFF_CONF_SCALE: float = 0.85

#: 插值补出来的帧的置信度折扣
_INTERP_CONF_SCALE: float = 0.50

#: 时序滑动平均窗口（锚点序列本就稀疏，窗口不宜大）
_SMOOTH_WINDOW: int = 3

#: 一阶预测里手腕速度方向的权重
_VELOCITY_BLEND: float = 0.30

#: 沿线段采样边缘强度的点数
_EDGE_SAMPLES: int = 24

#: 判定"该帧检测有效"的最低置信度
_MIN_FRAME_CONF: float = 0.05

#: 降级判据取中位数所用的关键帧 ①④⑤⑥
_KEY_PHASES: Tuple[PhaseKey, ...] = (
    PhaseKey.ADDRESS,
    PhaseKey.TOP,
    PhaseKey.DOWNSWING,
    PhaseKey.IMPACT,
)

_EPS: float = 1e-9


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _empty_track(view: CameraView, club_len_px: float = 0.0) -> ClubTrack:
    """统一的"不可用"轨迹。

    ``available=False`` 时恒有 ``detections == []``（模块内不变量），
    调用方只需判断 ``available`` 一处。
    """
    return ClubTrack(
        detections=[],
        club_len_px=float(club_len_px) if math.isfinite(club_len_px) else 0.0,
        overall_confidence=0.0,
        available=False,
        view=view,
        swing_plane_measurable=view is CameraView.DOWN_THE_LINE,
    )


def _unit(vec: np.ndarray) -> Optional[np.ndarray]:
    """单位化二维向量；零向量或含非有限值时返回 ``None``。"""
    arr = np.asarray(vec, dtype=np.float64).ravel()
    if arr.size < 2 or not np.all(np.isfinite(arr[:2])):
        return None
    norm = float(np.linalg.norm(arr[:2]))
    if norm < _EPS:
        return None
    return arr[:2] / norm


def _directed_angle_deg(u: np.ndarray, v: np.ndarray) -> float:
    """两个**有向**向量的夹角，返回 ``[0, 180]`` 度。"""
    a, b = _unit(u), _unit(v)
    if a is None or b is None:
        return float("nan")
    return float(math.degrees(math.acos(geometry.clamp(float(np.dot(a, b)), -1.0, 1.0))))


def _sample_scalar(image: np.ndarray, x: float, y: float) -> float:
    """最近邻取值，越界返回 0。"""
    if not (math.isfinite(x) and math.isfinite(y)):
        return 0.0
    height, width = image.shape[:2]
    xi, yi = int(round(x)), int(round(y))
    if xi < 0 or yi < 0 or xi >= width or yi >= height:
        return 0.0
    return float(image[yi, xi])


def _mean_edge_strength(
    strength: np.ndarray, p1: np.ndarray, p2: np.ndarray
) -> float:
    """沿线段等间隔采样归一化边缘强度的均值（0~1）。"""
    xs = np.linspace(float(p1[0]), float(p2[0]), _EDGE_SAMPLES)
    ys = np.linspace(float(p1[1]), float(p2[1]), _EDGE_SAMPLES)
    values = [_sample_scalar(strength, x, y) for x, y in zip(xs, ys)]
    return float(np.mean(values)) if values else 0.0


def _landmark_px(frame: FrameLandmarks, width: int, height: int) -> np.ndarray:
    """``(33, 2)`` 像素坐标（口径与 ``metrics._img_pt()`` 一致）。"""
    norm = np.asarray(frame.norm, dtype=np.float64)
    out = np.empty((norm.shape[0], 2), dtype=np.float64)
    out[:, 0] = norm[:, 0] * float(width)
    out[:, 1] = norm[:, 1] * float(height)
    return out


def _grip_px(landmark_px: np.ndarray) -> Optional[np.ndarray]:
    """握把锚点 = 双腕中点。"""
    left = landmark_px[geometry.L_WRIST]
    right = landmark_px[geometry.R_WRIST]
    if not (np.all(np.isfinite(left)) and np.all(np.isfinite(right))):
        return None
    return geometry.midpoint(left, right)


# ---------------------------------------------------------------------------
# Step 2｜杆长先验
# ---------------------------------------------------------------------------


def club_length_prior(
    landmark_px: np.ndarray, view: CameraView
) -> float:
    """按机位选标尺，估算杆长（像素）。

    ⚠️ **DTL 机位绝不能用图像肩宽当标尺**：侧面机位下双肩与相机光轴近似共线，
    投影肩宽被全片压缩，``metrics.image_shoulder_width_px()`` 那条"低于 90 分位
    就回落"的守卫也救不了（90 分位本身就是压缩值）。身高方向与光轴垂直，不受影响。

    Args:
        landmark_px: ``(33, 2)`` Address 帧像素坐标。
        view: 机位。

    Returns:
        杆长像素数；两种标尺都不可用时返回 ``nan``。
    """
    nose_y = float(landmark_px[geometry.NOSE, 1])
    ankle_mid_y = float(
        (landmark_px[geometry.L_ANKLE, 1] + landmark_px[geometry.R_ANKLE, 1]) / 2.0
    )
    height_px = geometry.body_height_px(nose_y, ankle_mid_y)

    shoulder_px = float(
        np.linalg.norm(
            landmark_px[geometry.L_SHOULDER] - landmark_px[geometry.R_SHOULDER]
        )
    )

    dtl_lo, dtl_hi = config.CLUB_LEN_RATIO_DTL
    face_lo, face_hi = config.CLUB_LEN_RATIO_FACEON

    if view is CameraView.DOWN_THE_LINE:
        primary = height_px * (dtl_lo + dtl_hi) / 2.0
        backup = shoulder_px * (face_lo + face_hi) / 2.0
    else:
        primary = shoulder_px * (face_lo + face_hi) / 2.0
        backup = height_px * (dtl_lo + dtl_hi) / 2.0

    for candidate in (primary, backup):
        if math.isfinite(candidate) and candidate >= 10.0:
            return float(candidate)
    return float("nan")


# ---------------------------------------------------------------------------
# Step 3｜ROI 扇形
# ---------------------------------------------------------------------------


def _fan_mask(
    shape: Tuple[int, int],
    apex: np.ndarray,
    direction: np.ndarray,
    half_angle_deg: float,
    radius: float,
) -> np.ndarray:
    """以 ``apex`` 为顶点、沿 ``direction`` 张开的扇形掩膜（255 = 搜索区）。"""
    height, width = int(shape[0]), int(shape[1])
    mask = np.zeros((max(1, height), max(1, width)), dtype=np.uint8)
    unit = _unit(direction)
    if unit is None or radius <= 1.0:
        mask[:] = 255
        return mask

    base = math.atan2(float(unit[1]), float(unit[0]))
    half = math.radians(float(half_angle_deg))
    steps = max(6, int(round(half_angle_deg / 5.0)) * 2)
    points: List[Tuple[int, int]] = [
        (int(round(float(apex[0]))), int(round(float(apex[1]))))
    ]
    for k in range(steps + 1):
        theta = base - half + (2.0 * half) * k / float(steps)
        points.append(
            (
                int(round(float(apex[0]) + radius * math.cos(theta))),
                int(round(float(apex[1]) + radius * math.sin(theta))),
            )
        )
    cv2.fillPoly(mask, [np.array(points, dtype=np.int32)], 255, lineType=cv2.LINE_8)
    return mask


# ---------------------------------------------------------------------------
# Step 4 + 5｜Hough 杆身 + 杆头精修
# ---------------------------------------------------------------------------


def _skeleton_segments(landmark_px: np.ndarray) -> List[Tuple[np.ndarray, np.ndarray]]:
    """可用于共线排除的骨架段（肩-肘、肘-腕、髋-膝、膝-踝等）。"""
    segments: List[Tuple[np.ndarray, np.ndarray]] = []
    for a, b in geometry.SKELETON_EDGES:
        pa = landmark_px[a]
        pb = landmark_px[b]
        if not (np.all(np.isfinite(pa)) and np.all(np.isfinite(pb))):
            continue
        if float(np.linalg.norm(pb - pa)) < 1.0:
            continue
        segments.append((np.asarray(pa, dtype=np.float64), np.asarray(pb, dtype=np.float64)))
    return segments


def _collinear_with_skeleton(
    p1: np.ndarray,
    p2: np.ndarray,
    segments: Sequence[Tuple[np.ndarray, np.ndarray]],
    club_len: float,
) -> bool:
    """过滤③：候选线段是否与某条骨架段"既平行又重合"。

    这是**最高频的误检来源**——不排除的话手臂和腿会被整根当成杆身。
    只有「夹角很小」且「位置很近」同时成立才判定为骨架，避免把恰好平行但
    离得很远的真杆身误杀。
    """
    near_tol = _SKELETON_NEAR_RATIO * club_len
    for sa, sb in segments:
        angle = geometry.angle_between_lines(p1, p2, sa, sb)
        if not math.isfinite(angle) or angle > _SKELETON_PARALLEL_DEG:
            continue
        bone_mid = geometry.midpoint(sa, sb)
        if geometry.point_line_distance(bone_mid, p1, p2) <= near_tol:
            return True
    return False


def _refine_head(
    gray: np.ndarray,
    strength: np.ndarray,
    origin: np.ndarray,
    direction: np.ndarray,
    club_len: float,
) -> np.ndarray:
    """杆头轴向精修：在 ``club_len ± 0.15×L`` 范围内找亮度/边缘峰。

    > 与设计 §4.2 Step 5 的细微偏差：精修**只沿杆身轴向**移动，不做横向搜索。
    > 理由：方向来自整段直线的稳健拟合，精度远高于单点峰值；允许横向漂移
    > 0.15×L 会给投影角凭空引入最多 8° 误差，而本方案精度定位就是 ±5~8°。
    > 轴向精修解决的是"杆长先验有偏"，横向漂移只会污染角度。
    """
    unit = _unit(direction)
    if unit is None:
        return np.asarray(origin, dtype=np.float64).copy()

    span = _HEAD_REFINE_RATIO * club_len
    best_score = -1.0
    best_t = club_len
    step = max(1.0, span / 24.0)
    t = club_len - span
    while t <= club_len + span + _EPS:
        px = float(origin[0]) + float(unit[0]) * t
        py = float(origin[1]) + float(unit[1]) * t
        edge = _sample_scalar(strength, px, py)
        bright = _sample_scalar(gray, px, py) / 255.0
        # 高斯先验把落点拉回杆长先验附近，避免被远处杂散高光带跑
        prior = math.exp(-0.5 * ((t - club_len) / max(1.0, span * 0.6)) ** 2)
        score = prior * (0.6 * edge + 0.4 * bright)
        if score > best_score:
            best_score = score
            best_t = t
        t += step
    return geometry.project_along(origin, unit, best_t)


def _detect_hough(
    bgr: np.ndarray,
    grip: np.ndarray,
    club_len: float,
    pred_dir: np.ndarray,
    fan_deg: float,
    dir_tol_deg: float,
    body_mask: np.ndarray,
    skeleton: Sequence[Tuple[np.ndarray, np.ndarray]],
) -> Optional[Tuple[np.ndarray, np.ndarray, float]]:
    """路径 A：低速段的 Hough 杆身拟合。

    Returns:
        ``(head_px, shaft_dir, confidence)``；未检出返回 ``None``。
    """
    height, width = bgr.shape[:2]
    radius = _ROI_RADIUS_RATIO * club_len
    x0 = int(max(0, math.floor(float(grip[0]) - radius)))
    x1 = int(min(width, math.ceil(float(grip[0]) + radius)))
    y0 = int(max(0, math.floor(float(grip[1]) - radius)))
    y1 = int(min(height, math.ceil(float(grip[1]) + radius)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None

    roi = bgr[y0:y1, x0:x1]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi.copy()
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)

    edges = cv2.Canny(gray, _CANNY_LO, _CANNY_HI, L2gradient=True)

    # ROI 扇形（时序预测收窄搜索区）
    apex = np.array([float(grip[0]) - x0, float(grip[1]) - y0], dtype=np.float64)
    edges = cv2.bitwise_and(
        edges, _fan_mask(edges.shape[:2], apex, pred_dir, fan_deg, radius)
    )
    # 过滤④：扣掉人体粗掩膜内部的边缘
    edges[body_mask[y0:y1, x0:x1] > 0] = 0

    min_len = max(8.0, config.CLUB_HOUGH_MIN_LEN_RATIO * club_len)
    max_gap = max(2.0, config.CLUB_HOUGH_MAX_GAP_RATIO * club_len)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=math.pi / 180.0,
        threshold=max(10, int(round(0.15 * club_len))),
        minLineLength=int(round(min_len)),
        maxLineGap=int(round(max_gap)),
    )
    if lines is None or len(lines) == 0:
        return None

    sobel_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(sobel_x, sobel_y)
    peak = float(np.max(magnitude)) if magnitude.size else 0.0
    strength = magnitude / peak if peak > _EPS else magnitude

    grip_tol = config.CLUB_GRIP_DIST_RATIO * club_len
    best: Optional[Tuple[np.ndarray, np.ndarray, float]] = None
    best_score = -1.0

    for line in lines:
        lx1, ly1, lx2, ly2 = (float(v) for v in line[0])
        p1 = np.array([lx1 + x0, ly1 + y0], dtype=np.float64)
        p2 = np.array([lx2 + x0, ly2 + y0], dtype=np.float64)
        seg_len = float(np.linalg.norm(p2 - p1))
        if seg_len < _EPS:
            continue

        # 过滤①：杆身延长线必过握把
        grip_dist = geometry.point_line_distance(grip, p1, p2)
        if grip_dist > grip_tol:
            continue

        # 取远离握把的端点定义**有向**杆身方向
        far = p1 if np.linalg.norm(p1 - grip) >= np.linalg.norm(p2 - grip) else p2
        shaft_dir = _unit(far - grip)
        if shaft_dir is None:
            continue

        # 过滤②：与时序预测方向的（有向）夹角
        align_deg = _directed_angle_deg(shaft_dir, pred_dir)
        if not math.isfinite(align_deg) or align_deg > dir_tol_deg:
            continue

        # 过滤③：排除与骨架段近似共线者（手臂 / 大腿最容易被当成杆身）
        if _collinear_with_skeleton(p1, p2, skeleton, club_len):
            continue

        # 过滤④补充：线段中点落在人体粗掩膜内的直接排除
        mid = geometry.midpoint(p1, p2)
        if _sample_scalar(body_mask, float(mid[0]), float(mid[1])) > 0.0:
            continue

        length_score = geometry.clamp(seg_len / max(1.0, club_len), 0.0, 1.0)
        align_score = geometry.clamp(1.0 - align_deg / max(1.0, dir_tol_deg), 0.0, 1.0)
        edge_score = geometry.clamp(
            _mean_edge_strength(strength, p1 - [x0, y0], p2 - [x0, y0]), 0.0, 1.0
        )
        grip_score = geometry.clamp(1.0 - grip_dist / max(1.0, grip_tol), 0.0, 1.0)

        # 设计 §4.2 Step 4：argmax(长度 × 边缘强度 × 时序一致性)
        score = length_score * max(edge_score, 0.05) * max(align_score, 0.05)
        if score > best_score:
            confidence = geometry.clamp(
                0.35 * length_score
                + 0.30 * align_score
                + 0.20 * edge_score
                + 0.15 * grip_score,
                0.0,
                1.0,
            )
            best_score = score
            best = (far, shaft_dir, confidence)

    if best is None:
        return None

    _far, shaft_dir, confidence = best
    head = _refine_head(gray, strength, grip - [x0, y0], shaft_dir, club_len)
    head = head + np.array([x0, y0], dtype=np.float64)
    return head, shaft_dir, confidence


# ---------------------------------------------------------------------------
# Step 6｜帧差分支（路径 C）
# ---------------------------------------------------------------------------


def _detect_framediff(
    bgr: np.ndarray,
    prev_bgr: Optional[np.ndarray],
    grip: np.ndarray,
    club_len: float,
    pred_dir: np.ndarray,
    fan_deg: float,
    body_mask: np.ndarray,
) -> Optional[Tuple[np.ndarray, np.ndarray, float]]:
    """路径 C：高速段的帧差杆头定位。

    下杆到击球段杆头线速度可超 30 m/s，杆身在单帧内被抹成一片糊影，直线检测失效；
    但**运动残影恰好勾勒出杆扫过的区域**，帧差反而是这一段最稳的信号。

    Returns:
        ``(head_px, shaft_dir, confidence)``；未检出返回 ``None``。
    """
    if prev_bgr is None or prev_bgr.shape[:2] != bgr.shape[:2]:
        return None

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    prev_gray = (
        cv2.cvtColor(prev_bgr, cv2.COLOR_BGR2GRAY) if prev_bgr.ndim == 3 else prev_bgr
    )

    diff = cv2.absdiff(gray, prev_gray)
    _ret, binary = cv2.threshold(diff, _DIFF_THRESH, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    binary[body_mask > 0] = 0

    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    if count <= 1:
        return None

    lo = _DIFF_BAND_RATIO[0] * club_len
    hi = _DIFF_BAND_RATIO[1] * club_len
    best_area = 0.0
    best_center: Optional[np.ndarray] = None
    best_align = 0.0

    for label in range(1, count):
        area = float(stats[label, cv2.CC_STAT_AREA])
        if area < 6.0:
            continue
        center = np.array(
            [float(centroids[label, 0]), float(centroids[label, 1])], dtype=np.float64
        )
        distance = float(np.linalg.norm(center - grip))
        if distance < lo or distance > hi:
            continue
        align_deg = _directed_angle_deg(center - grip, pred_dir)
        if not math.isfinite(align_deg) or align_deg > fan_deg:
            continue
        if area > best_area:
            best_area = area
            best_center = center
            best_align = align_deg

    if best_center is None:
        return None

    shaft_dir = _unit(best_center - grip)
    if shaft_dir is None:
        return None

    area_score = geometry.clamp(best_area / max(1.0, club_len * 2.0), 0.0, 1.0)
    align_score = geometry.clamp(1.0 - best_align / max(1.0, fan_deg), 0.0, 1.0)
    confidence = geometry.clamp(
        _DIFF_CONF_SCALE * (0.45 * area_score + 0.55 * align_score), 0.0, 1.0
    )
    return best_center, shaft_dir, confidence


# ---------------------------------------------------------------------------
# 帧计划
# ---------------------------------------------------------------------------


def plan_frames(
    landmarks: Sequence[FrameLandmarks],
    events: Optional[Sequence[SwingEvent]] = None,
    meta: Optional[VideoMeta] = None,
    budget_bytes: Optional[int] = None,
) -> Tuple[List[int], List[int]]:
    """决定「在哪些帧上检测」与「需要解码哪些帧」。

    稀疏模式（默认）：8 个事件帧；窗口模式：额外补 ``[Top, Impact]`` 区间等间隔
    采样，供 ``shaft_plane_dev`` 做杆头轨迹拟合。每个锚点帧额外解码它的**前一帧**，
    帧差分支需要。

    两道预算护栏（架构 §5.2）：
    1. **锚点预算** = ``config.CLUB_MAX_DECODE_FRAMES // 2``——超出时优先保留
       全部 8 个事件帧，只对窗口采样点等间隔削减；
    2. **字节预算**（``budget_bytes``）——按 ``meta.width*meta.height*3`` 估算
       单帧字节，超 ``config.DECODE_BYTES_BUDGET`` 时继续削减窗口采样点，
       **下限保留 8 个事件帧**（保证 renderer 恒 8 张）。

    Args:
        landmarks: 采样后的关键点序列。
        events: 8 个挥杆事件；``None`` 时退化为对整段等间隔采样。
        meta: 视频元信息，字节预算估算用。
        budget_bytes: 解码字节预算上限；``None`` 时不启用字节护栏。

    Returns:
        ``(锚点帧号升序, 需解码帧号升序)``。
    """
    total = len(landmarks)
    if total == 0:
        return [], []

    event_frames: List[int] = []
    anchors: List[int] = []
    if events:
        event_frames = sorted({int(e.frame_index) for e in events if e.frame_index >= 0})
        anchors.extend(event_frames)
        top = next((e for e in events if e.key is PhaseKey.TOP), None)
        impact = next((e for e in events if e.key is PhaseKey.IMPACT), None)
        if top is not None and impact is not None:
            lo = max(0, min(int(top.array_index), total - 1))
            hi = max(0, min(int(impact.array_index), total - 1))
            if hi < lo:
                lo, hi = hi, lo
            window = [int(landmarks[i].frame_index) for i in range(lo, hi + 1)]
            if len(window) > _WINDOW_SAMPLES:
                picks = np.linspace(0, len(window) - 1, _WINDOW_SAMPLES)
                window = [window[int(round(p))] for p in picks]
            anchors.extend(window)
    else:
        step = max(1, math.ceil(total / float(_WINDOW_SAMPLES)))
        anchors.extend(int(landmarks[i].frame_index) for i in range(0, total, step))

    anchors = sorted({a for a in anchors if a >= 0})

    # ---- 护栏 1：锚点预算（优先保留 8 个事件帧）---------------------------
    budget = max(8, config.CLUB_MAX_DECODE_FRAMES // 2)
    if len(anchors) > budget:
        anchors = _trim_to_budget(anchors, set(event_frames), budget)

    # ---- 护栏 2：字节预算 ------------------------------------------------
    if budget_bytes is not None and meta is not None:
        per_frame = max(1, int(meta.width) * int(meta.height) * 3)
        floor = set(event_frames) if event_frames else set(anchors)
        targets_est = sorted(set(anchors) | {a - 1 for a in anchors if a >= 1})
        while len(targets_est) * per_frame > budget_bytes and len(anchors) > len(floor):
            # 逐级削减非事件锚点（窗口采样点），事件帧恒保留
            anchors = _trim_to_budget(anchors, floor, len(anchors) - 1)
            targets_est = sorted(set(anchors) | {a - 1 for a in anchors if a >= 1})
        if len(targets_est) * per_frame > budget_bytes:
            # 极限：只保留 8 个事件帧本身（丢弃各自前一帧，帧差分支退化，
            # Hough 路径仍可用；renderer 恒 8 张不受影响）
            targets_est = sorted(floor)

    targets = sorted(set(anchors) | {a - 1 for a in anchors if a >= 1})
    if budget_bytes is not None and meta is not None:
        per_frame = max(1, int(meta.width) * int(meta.height) * 3)
        if len(targets) * per_frame > budget_bytes:
            targets = targets_est
    return anchors, targets


def _trim_to_budget(
    anchors: Sequence[int], keep: set, budget: int
) -> List[int]:
    """把锚点削减到 ``budget`` 以内，保留 ``keep`` 集合中的帧。

    非保留锚点按等间隔采样削减（保留首尾，保证 Top/Impact 覆盖）。
    """
    keep_sorted = sorted(a for a in anchors if a in keep)
    extras = [a for a in anchors if a not in keep]
    room = max(0, budget - len(keep_sorted))
    if len(extras) > room:
        if room <= 0:
            extras = []
        else:
            picks = np.linspace(0, len(extras) - 1, room)
            extras = sorted({extras[int(round(p))] for p in picks})
    return sorted(set(keep_sorted) | set(extras))


# ---------------------------------------------------------------------------
# Step 7｜时序滤波
# ---------------------------------------------------------------------------


def _smooth_and_fill(detections: List[ClubDetection]) -> None:
    """对 ``(grip, head)`` 序列做插值 + 滑动平均（原地修改）。

    实现套路直接复用 ``pose_extractor._interpolate_missing()`` /
    :func:`pose_extractor.moving_average`，保持代码风格一致。
    """
    count = len(detections)
    if count == 0:
        return

    heads = np.full((count, 2), np.nan, dtype=np.float64)
    grips = np.full((count, 2), np.nan, dtype=np.float64)
    for i, item in enumerate(detections):
        if item.head is not None:
            heads[i] = item.head
        if item.grip is not None:
            grips[i] = item.grip

    valid = np.isfinite(heads).all(axis=1)
    if not valid.any():
        return

    xs = np.arange(count, dtype=np.float64)
    for col in range(2):
        heads[:, col] = np.interp(xs, xs[valid], heads[valid, col])
        good_grip = np.isfinite(grips[:, col])
        if good_grip.any():
            grips[:, col] = np.interp(xs, xs[good_grip], grips[good_grip, col])

    if count >= _SMOOTH_WINDOW:
        heads = moving_average(heads, _SMOOTH_WINDOW)
        if np.isfinite(grips).all():
            grips = moving_average(grips, _SMOOTH_WINDOW)

    for i, item in enumerate(detections):
        if not valid[i]:
            item.method = "interp"
            item.confidence = float(item.confidence) * _INTERP_CONF_SCALE
        item.head = heads[i].copy()
        if np.all(np.isfinite(grips[i])):
            item.grip = grips[i].copy()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def detect(
    video_path: str,
    landmarks: Sequence[FrameLandmarks],
    signals: Optional[SwingSignals] = None,
    view: CameraView = CameraView.FACE_ON,
    meta: Optional[VideoMeta] = None,
    events: Optional[Sequence[SwingEvent]] = None,
    frames_bgr: Optional[Dict[int, np.ndarray]] = None,
) -> ClubTrack:
    """球杆检测主入口。**永不抛异常。**

    Args:
        video_path: 原视频路径。``frames_bgr`` 已给出时不会被使用。
        landmarks: ``pose_extractor.extract()`` 的产出（提供握把锚点与人体掩膜）。
        signals: ``segmenter.build_signals()`` 的产出，用 ``speed`` 门控分支切换。
        view: 机位。传入 ``AUTO`` 时按 ``FACE_ON`` 处理（自动判定属 T04 范围）。
        meta: 视频元信息，用于归一化坐标 → 像素换算；缺省时从解码帧尺寸推断。
        events: 8 个挥杆事件，决定检测哪些帧与关键帧置信度。
        frames_bgr: 已解码帧字典。**管线应传入它**以与 renderer 共享解码结果，
            把解码趟数锁在 2 趟；为 ``None`` 时本函数会自行调用
            :func:`app.frame_reader.grab_frames`（第 2 趟）。

    Returns:
        :class:`ClubTrack`。失败时恒为 ``available=False`` 且 ``detections == []``。
    """
    try:
        return _detect_impl(
            video_path, landmarks, signals, view, meta, events, frames_bgr
        )
    except Exception:  # noqa: BLE001 - 模块级硬约束：绝不把异常抛给主链路
        logger.exception("club detection failed, degrading to unavailable")
        return _empty_track(view if isinstance(view, CameraView) else CameraView.FACE_ON)


def _detect_impl(
    video_path: str,
    landmarks: Sequence[FrameLandmarks],
    signals: Optional[SwingSignals],
    view: CameraView,
    meta: Optional[VideoMeta],
    events: Optional[Sequence[SwingEvent]],
    frames_bgr: Optional[Dict[int, np.ndarray]],
) -> ClubTrack:
    """真实检测流程，异常一律由 :func:`detect` 兜住。"""
    if not isinstance(view, CameraView):
        view = CameraView.FACE_ON
    # AUTO 是请求入参态；机位自动判定属 T04 范围，本期收到 AUTO 按 face-on 处理
    if view is CameraView.AUTO:
        view = CameraView.FACE_ON

    if not config.CLUB_ENABLED or config.CLUB_MODE == "off":
        logger.info("club detection disabled (mode=%s)", config.CLUB_MODE)
        return _empty_track(view)
    if config.CLUB_MODE != "geom":
        logger.warning(
            "club mode %r not implemented in this build, degrading", config.CLUB_MODE
        )
        return _empty_track(view)
    if not landmarks:
        return _empty_track(view)

    anchors, targets = plan_frames(landmarks, events, meta=meta)
    if not anchors:
        return _empty_track(view)

    decoded: Dict[int, np.ndarray] = (
        dict(frames_bgr) if frames_bgr is not None else frame_reader.grab_frames(
            video_path, targets
        )
    )
    if not decoded:
        logger.warning("club detection: no frame decoded")
        return _empty_track(view)

    sample = next(iter(decoded.values()))
    height, width = int(sample.shape[0]), int(sample.shape[1])
    if meta is not None and int(meta.width) > 0 and int(meta.height) > 0:
        width, height = int(meta.width), int(meta.height)

    lm_by_frame: Dict[int, FrameLandmarks] = {f.frame_index: f for f in landmarks}
    index_by_frame: Dict[int, int] = {
        f.frame_index: i for i, f in enumerate(landmarks)
    }

    # ---- Step 2：杆长先验（取 Address 帧；缺失则用序列首帧）----------------
    addr_frame_index = int(events[0].frame_index) if events else int(
        landmarks[0].frame_index
    )
    addr_lm = lm_by_frame.get(addr_frame_index, landmarks[0])
    club_len = club_length_prior(_landmark_px(addr_lm, width, height), view)
    if not math.isfinite(club_len) or club_len < 10.0:
        logger.warning("club detection: illegal club length prior %.3f", club_len)
        return _empty_track(view)

    speed = np.asarray(signals.speed, dtype=np.float64) if signals is not None else None
    switch = float(config.CLUB_SPEED_SWITCH)
    fan_addr, fan_track = config.CLUB_ROI_FAN_DEG

    detections: List[ClubDetection] = []
    last_dir: Optional[np.ndarray] = None
    last_grip: Optional[np.ndarray] = None

    for frame_index in anchors:
        bgr = decoded.get(frame_index)
        frame_lm = lm_by_frame.get(frame_index)
        if bgr is None or frame_lm is None:
            continue

        landmark_px = _landmark_px(frame_lm, width, height)
        grip = _grip_px(landmark_px)
        if grip is None:
            continue

        # ---- Step 3：时序预测方向与扇形张角 ------------------------------
        if last_dir is not None:
            predicted = np.asarray(last_dir, dtype=np.float64).copy()
            if last_grip is not None:
                velocity = _unit(grip - last_grip)
                if velocity is not None:
                    blended = _unit(predicted + _VELOCITY_BLEND * velocity)
                    if blended is not None:
                        predicted = blended
            fan_deg = float(fan_track)
            dir_tol = float(config.CLUB_DIR_TOL_DEG)
        else:
            # 没有历史方向：Address 帧假设杆身朝下，用宽扇形兜住
            predicted = np.array([0.0, 1.0], dtype=np.float64)
            fan_deg = float(fan_addr)
            dir_tol = float(fan_addr)

        body_mask = geometry.skeleton_polygon_mask(landmark_px, (height, width))
        skeleton = _skeleton_segments(landmark_px)

        array_index = index_by_frame.get(frame_index, -1)
        frame_speed = (
            float(speed[array_index])
            if speed is not None and 0 <= array_index < speed.size
            else 0.0
        )
        prev_bgr = decoded.get(frame_index - 1)

        hough_args = (
            bgr, grip, club_len, predicted, fan_deg, dir_tol, body_mask, skeleton,
        )
        diff_args = (bgr, prev_bgr, grip, club_len, predicted, fan_deg, body_mask)

        # ---- Step 4 / 6：按速度门控选主分支，主分支落空再试互补分支 ------
        if math.isfinite(frame_speed) and frame_speed >= switch:
            outcome = _detect_framediff(*diff_args)
            method = "framediff"
            if outcome is None:
                outcome = _detect_hough(*hough_args)
                method = "hough"
        else:
            outcome = _detect_hough(*hough_args)
            method = "hough"
            if outcome is None:
                outcome = _detect_framediff(*diff_args)
                method = "framediff"

        if outcome is None or outcome[2] < _MIN_FRAME_CONF:
            detections.append(
                ClubDetection(frame_index=frame_index, grip=grip, head=None,
                              confidence=0.0, method="none")
            )
            continue

        head, shaft_dir, confidence = outcome
        detections.append(
            ClubDetection(
                frame_index=frame_index,
                grip=grip,
                head=np.asarray(head, dtype=np.float64),
                confidence=float(confidence),
                method=method,
            )
        )
        last_dir = shaft_dir
        last_grip = grip

    measured = [d for d in detections if d.head is not None]
    if not measured:
        logger.info("club detection: no shaft found in %d anchors", len(detections))
        return _empty_track(view, club_len)

    # ---- Step 7：时序滤波与置信度 ----------------------------------------
    _smooth_and_fill(detections)
    overall = _overall_confidence(detections, events)

    track = ClubTrack(
        detections=detections,
        club_len_px=float(club_len),
        overall_confidence=float(overall),
        available=overall > 0.0,
        view=view,
        swing_plane_measurable=view is CameraView.DOWN_THE_LINE,
    )
    if not track.available:
        return _empty_track(view, club_len)

    logger.info(
        "club detection done: anchors=%d measured=%d conf=%.3f len=%.1fpx view=%s",
        len(detections), len(measured), overall, club_len, view.value,
    )
    return track


def _overall_confidence(
    detections: Sequence[ClubDetection], events: Optional[Sequence[SwingEvent]]
) -> float:
    """关键帧 ①Address ④Top ⑤Downswing ⑥Impact 的 confidence 中位数。

    这是三级降级的唯一判据：这四帧是 ``swing_plane`` 系列真正取值的帧，
    用它们的中位数比全片均值更能反映"我们要用的那几个数准不准"。
    """
    by_frame = {d.frame_index: d for d in detections}
    picked: List[float] = []
    if events:
        wanted = {
            int(e.frame_index) for e in events if e.key in _KEY_PHASES
        }
        picked = [
            float(by_frame[f].confidence) for f in wanted if f in by_frame
        ]
    if not picked:
        picked = [float(d.confidence) for d in detections]
    if not picked:
        return 0.0
    return float(np.median(np.asarray(picked, dtype=np.float64)))
