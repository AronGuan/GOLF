"""轻量击球帧校正（ARCHITECTURE-v3-clublite.md）。

**只做帧级时序校正（±1~2 采样帧），不做像素级杆头定位**——这是它与已下线的
重球杆检测（club_detector.py，ROI+Hough+ONNX，实测真实视频置信度仅
0.206~0.462、L0 从未出现）的本质区别：校正目标单一（impact 事件帧），
轻量 CV 原语（absdiff + 阈值 + 质心）即可胜任。

方案：
- **M1（主）**：地面 ROI 帧差运动峰 + 质心贴地约束 + 球点加权（球不可见不降级，
  球点仅作评分加分项，Q2）；
- **M2（辅助/兜底）**：对 Top-K 候选帧各做一次简化 Hough（杆身端点验证），
  取「杆头端点 y 最低且贴近地面线」的帧作为评分权重 + 平票 tie-breaker；
- **降级（G0）**：任何失败 -> ``available=False``，调用方保持原 ``locate_impact``
  结果（estimated 不变），不引入新的估算态、不阻断任务。

🔴 模块级硬约束（与 club_detector 相同）：**本模块禁止外抛异常**。任何失败
（解码失败、关键点缺失、OpenCV 报错……）都被 :func:`refine_impact` 吞掉，
统一返回 ``ImpactRefineResult(available=False)``。主链路不能被这个增量特性拖垮。

机位适配（用户 Q1：双机位都要可靠校正）：
- DTL：地面带 ROI 取全宽，杆头在身体侧前方贴地可见，效果好；
- face-on：击球瞬间杆头可能被躯干遮挡，地面 ROI 水平方向收窄到以双踝中点
  为中心的中央带（避开画面两侧的腿部/手臂运动），并给 M2 杆身端点验证更高
  的评分权重作遮挡补偿。

v2 调优（2026-08 用户拍板）：最终选帧时对最优候选统一回退
:data:`config.CLUBLITE_IMPACT_OFFSET`（默认 -1）帧——算法选的"运动峰"帧是
球被杆头加速后的帧，视觉真实接触瞬间在其前 1 帧（30fps = 33ms）。偏移受
物理下界守卫（top + min_gap）约束，越界则 G0；``plan_reanchor_frames`` 的
搜索集同步扩展覆盖偏移目标帧，保证 reanchor 事件帧仍在解码并集内。
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from . import config, geometry
from .frame_reader import grab_frames
from .pose_extractor import moving_average
from .schemas import (
    CameraView,
    FrameLandmarks,
    ImpactRefineResult,
    PhaseKey,
    SwingEvent,
    SwingSignals,
    VideoMeta,
)
from . import segmenter  # 仅使用 segmenter.reanchor_impact（无循环依赖）

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 算法内部私有常量（config §8b 只放对外可调参数，内部调参归本模块）
# ---------------------------------------------------------------------------

#: face-on 地面 ROI 水平收窄比例（以双踝中点为心，避开画面两侧腿部/手臂运动）。
#: face-on 的击球瞬间杆头可能被躯干遮挡（Q1），ROI 水平收窄 + M2 杆身端点
#: 更高评分权重（见 :data:`_W_SHAFT_FACEON`）是机位适配的两个抓手；
#: ROI 上边界两机位共用 :data:`config.CLUBLITE_ROI_TOP_MARGIN_RATIO`。
_FACEON_ROI_WIDTH_RATIO: float = 0.60

#: M2 评分加分权重（乘法基数）：``score = motion × ground × (1 + ball + shaft)``
_W_BALL: float = 0.25
_W_SHAFT_DTL: float = 0.15
_W_SHAFT_FACEON: float = 0.30  # face-on 遮挡补偿：杆身端点验证权重更高（Q1）

#: M2 球点邻近半径（像素），候选帧 diff 质心落在球心该半径内才给球点加分
_BALL_NEAR_PX: float = 60.0

#: M2 杆长先验（像素）= 图像身高 × 系数（7 号铁 ≈ 0.54×身高，一号木 ≈ 0.65×身高）
_CLUB_LEN_RATIO_DTL: float = 0.60
_CLUB_LEN_RATIO_FACEON: float = 0.45  # face-on 投影杆长略短（杆身接近竖直）

#: M2 Hough 与过滤参数（沿用历史 club-detection-design §5.2 的保守口径）
_SHAFT_HOUGH_THRESHOLD: int = 40
_SHAFT_GRIP_DIST_RATIO: float = 0.12  # 线段到握把垂距上限 = 该系数 × club_len_px
_SHAFT_HEAD_Y_BIAS: float = 0.0  # 杆头端点需在握把下方该像素以上（0 = 不强制）


# ---------------------------------------------------------------------------
# 纯函数：窗口规划
# ---------------------------------------------------------------------------


def _window_indices(
    events: Sequence[SwingEvent],
    signals: SwingSignals,
    back_sec: Optional[float],
    fwd_sec: Optional[float],
) -> Tuple[int, int]:
    """返回校正搜索窗口的 array 下标 ``(lo, hi)``（闭区间，均夹在合法范围）。

    腕部估计天然偏早，窗口主要向前（fwd）探测；back 只留少量回退余量。
    """
    back = config.CLUBLITE_SEARCH_BACK_SEC if back_sec is None else float(back_sec)
    fwd = config.CLUBLITE_SEARCH_FWD_SEC if fwd_sec is None else float(fwd_sec)
    impact = next((e for e in events if e.key is PhaseKey.IMPACT), None)
    if impact is None or signals is None or signals.n <= 0:
        return 0, -1
    fe = signals.fps_eff
    if not math.isfinite(fe) or fe <= 0:
        fe = 30.0
    back_n = max(1, int(round(back * fe)))
    fwd_n = max(1, int(round(fwd * fe)))
    lo = max(0, impact.array_index - back_n)
    hi = min(signals.n - 1, impact.array_index + fwd_n)
    if hi < lo:
        hi = lo
    return lo, hi


def plan_refine_frames(
    events: Sequence[SwingEvent],
    signals: SwingSignals,
    meta: VideoMeta,
    back_sec: Optional[float] = None,
    fwd_sec: Optional[float] = None,
    frames: Optional[Sequence[FrameLandmarks]] = None,
) -> Tuple[List[int], List[int]]:
    """规划校正所需帧号（原视频帧号）。

    Args:
        events: 8 事件。
        signals: 切分信号包。
        meta: 视频元信息（``sample_step`` 用于 array 下标 -> 原帧号换算）。
        back_sec / fwd_sec: 覆盖 :data:`config.CLUBLITE_SEARCH_BACK_SEC` /
            :data:`config.CLUBLITE_SEARCH_FWD_SEC`。
        frames: 可选；给出时用帧号映射保证精确换算（纯函数仍无 IO）。

    Returns:
        ``(候选帧号升序, 需解码帧号升序[含前一帧 + Address 帧])``。
        候选帧 = 窗口内所有采样帧；解码帧 = 候选帧 ∪ 各自前一帧 ∪ Address 帧
        （前一帧供帧差起点，Address 帧供球点检测，两者都无需额外解码趟）。
    """
    lo, hi = _window_indices(events, signals, back_sec, fwd_sec)
    if hi < lo:
        return [], []
    step = max(1, int(getattr(meta, "sample_step", 1)))

    if frames is not None:
        index_to_frame: Dict[int, int] = {
            i: f.frame_index for i, f in enumerate(frames)
        }
        cand_frames = sorted(
            index_to_frame[i] for i in range(lo, hi + 1) if i in index_to_frame
        )
    else:
        cand_frames = sorted({i * step for i in range(lo, hi + 1)})

    if not cand_frames:
        return [], []

    decode_frames = set(cand_frames)
    for f in cand_frames:
        prev = f - step
        if prev >= 0:
            decode_frames.add(prev)
    # Address 帧：球点检测的参考帧（已在 8 事件帧里，纳入解码集保证独立调用可用）
    addr = next((e for e in events if e.key is PhaseKey.ADDRESS), None)
    if addr is not None and addr.frame_index >= 0:
        decode_frames.add(addr.frame_index)
    return cand_frames, sorted(decode_frames)


def plan_reanchor_frames(
    events: Sequence[SwingEvent],
    signals: SwingSignals,
    meta: VideoMeta,
    frames: Sequence[FrameLandmarks],
    cand_frames: Sequence[int],
) -> List[int]:
    """预计算校正后可能出现的全部事件帧号（纯函数，无 IO，opens=1 的关键）。

    **为什么需要**：pipeline 在**解码之前**不知道校正后的 impact（要像素运动信号），
    而 reanchor 可能把 ⑦ 送杆移到旧解码集之外（QA P1：送杆截图内容错帧）。
    因为 :func:`segmenter.reanchor_impact` 是纯函数，可以对窗口内**每个候选下标**
    跑一遍 reanchor，收集全部可能的事件帧号取并集——实际校正命中的那个候选的
    8 事件帧必在并集内。解码并集一次（opens 保持 1），校正后无需补解。

    **候选集 = 窗口候选 ∪ 各候选前 1 采样帧**（v2 调优：:data:`config.CLUBLITE_IMPACT_OFFSET`
    默认 -1 会把最终 impact 落在 ``cand_indices[best] - 1``，因此该调整目标的
    reanchor 事件帧也必须纳入并集，否则 QA P1 会复发）。

    Args:
        events: 8 事件（校正前）。
        signals: 切分信号包。
        meta: 视频元信息（保留签名一致性；帧号映射直接用 ``frames``）。
        frames: 姿态序列（array 下标 -> 原帧号映射）。
        cand_frames: :func:`plan_refine_frames` 返回的候选帧号（原视频帧号）。

    Returns:
        可能出现的全部事件帧号（升序、去重）；任何 reanchor 冲突（返回 None）
        的候选被跳过。
    """
    index_to_array: Dict[int, int] = {
        f.frame_index: i for i, f in enumerate(frames)
    }
    # 候选集：窗口候选 ∪ 其前 1 采样帧（CLUBLITE_IMPACT_OFFSET 的调整目标）。
    # 用 frames 序列反查前一采样帧的原帧号，比 ``frame_index - step`` 更稳
    # （step 可能 >1，且候选帧号由 frames 实际采样决定）。
    search_frames: set = set(cand_frames)
    for cand_frame in cand_frames:
        array_index = index_to_array.get(cand_frame)
        if array_index is not None and array_index - 1 >= 0:
            search_frames.add(frames[array_index - 1].frame_index)

    possible: set = set()
    for cand_frame in sorted(search_frames):
        array_index = index_to_array.get(cand_frame)
        if array_index is None:
            continue
        rebuilt = segmenter.reanchor_impact(frames, signals, events, array_index)
        if rebuilt is None:
            continue
        possible.update(e.frame_index for e in rebuilt)
    return sorted(possible)


# ---------------------------------------------------------------------------
# 纯函数：地面 ROI / 运动信号 / 候选 / 球点
# ---------------------------------------------------------------------------


def _ground_roi(
    addr_lm: FrameLandmarks,
    width: int,
    height: int,
    body_h_px: float,
    view: CameraView = CameraView.FACE_ON,
) -> Optional[Tuple[int, int, int, int]]:
    """地面带 ROI ``(x0, y0, x1, y1)``（y 向下，含下边界）。

    上边界 = 踝关节中点 y + 系数 × 图像身高，下到图像底边。踝关节是腿的最下端，
    其下方带 = 脚/地面/球（杆头贴地处），天然把腿部运动（踝上方）排除在 ROI 外。

    机位适配（Q1）：
    - DTL：全宽（``x ∈ [0, W]``）；
    - face-on：水平收窄到以双踝中点为心的中央带（
      :data:`_FACEON_ROI_WIDTH_RATIO`），并让上边界略下移。

    Returns:
        ``(x0, y0, x1, y1)``；关键点缺失 / 非法尺寸时返回 ``None``。
    """
    try:
        if width <= 0 or height <= 0:
            return None
        lx = float(addr_lm.norm[geometry.L_ANKLE, 0])
        ly = float(addr_lm.norm[geometry.L_ANKLE, 1])
        rx = float(addr_lm.norm[geometry.R_ANKLE, 0])
        ry = float(addr_lm.norm[geometry.R_ANKLE, 1])
        if not all(math.isfinite(v) for v in (lx, ly, rx, ry)):
            return None
        ankle_mid_x = (lx + rx) / 2.0
        ankle_mid_y = (ly + ry) / 2.0

        margin = config.CLUBLITE_ROI_TOP_MARGIN_RATIO
        width_ratio = 1.0
        if view is CameraView.FACE_ON:
            width_ratio = _FACEON_ROI_WIDTH_RATIO

        y0 = int(round(ankle_mid_y * height + margin * body_h_px))
        y0 = max(0, min(y0, height - 1))
        if y0 >= height - 1:
            return None

        x_center = ankle_mid_x * width
        half_w = int(round(width * width_ratio / 2.0))
        x0 = max(0, int(round(x_center)) - half_w)
        x1 = min(width, int(round(x_center)) + half_w)
        if x1 <= x0:
            x0, x1 = 0, width
        return (x0, y0, x1, height)
    except (TypeError, ValueError):
        return None


def _motion_signal(
    gray_frames: Sequence[np.ndarray],
    roi: Tuple[int, ...],
    smooth: bool = True,
) -> np.ndarray:
    """相邻帧 ROI 灰度差均值序列（长度 = len(gray_frames)，下标即帧偏移）。

    ``motion[0] = 0``，``motion[i] = mean(|gray[i] - gray[i-1]| 在 ROI 内)``。
    ``smooth=True`` 时用窗口 3 的滑动平均平滑（复用
    :func:`pose_extractor.moving_average`），并强制 ``motion[0] = 0``
    （平滑的边缘填充会把首帧抬出 0，产生边界假峰）。
    """
    n = len(gray_frames)
    motion = np.zeros(n, dtype=np.float64)
    if n < 2:
        return motion
    x0, y0, x1, y1 = (int(v) for v in roi)
    for i in range(1, n):
        prev = gray_frames[i - 1][y0:y1, x0:x1]
        cur = gray_frames[i][y0:y1, x0:x1]
        if prev.size == 0 or cur.size == 0 or prev.shape != cur.shape:
            continue
        motion[i] = float(np.mean(cv2.absdiff(cur, prev)))
    if smooth and n > 3:
        motion = moving_average(motion, 3)
        motion[0] = 0.0
    return motion


def _refine_candidates(
    candidates: Sequence[int], raw_motion: np.ndarray
) -> List[int]:
    """把候选（平滑信号的峰）在**原始信号**局部精修，消除平滑的峰位偏移。

    滑动平均会把单帧尖峰向右抹一帧（实测合成"杆头贴球"视频峰位右移 1 帧），
    导致候选落在「运动已结束、diff 为空」的帧上。对每个候选在 ±1 邻域内取
    原始运动强度的局部 argmax，即可把候选拉回真实的运动峰帧。

    Args:
        candidates: :func:`_pick_candidates` 的候选偏移（升序）。
        raw_motion: 未平滑的运动信号（长度与 ``gray_frames`` 一致）。

    Returns:
        精修后的候选偏移（升序、去重）。
    """
    n = int(len(raw_motion))
    out: List[int] = []
    for c in candidates:
        a = max(1, int(c) - 1)
        b = min(n - 1, int(c) + 1)
        if b < a:
            best = int(c)
        else:
            best = a + int(np.argmax(raw_motion[a : b + 1]))
        if best not in out:
            out.append(best)
    return sorted(out)


def _pick_candidates(
    motion: np.ndarray, min_ratio: float, top_k: int
) -> List[int]:
    """运动峰 Top-K 局部极大值（array 下标 = 相对窗口起点偏移，升序返回）。

    判据（ARCHITECTURE-v3-clublite.md §2.2 A）：
    - 候选强度 >= ``min_ratio × max(motion)``（无显著运动 -> 空列表）；
    - 严格局部极大（``motion[i] >= motion[i-1] 且 motion[i] > motion[i+1]``），
      首/末点按单边极大处理。

    Returns:
        升序候选偏移列表；无候选时返回 ``[]``（调用方降级 G0）。
    """
    n = int(len(motion))
    if n == 0:
        return []
    m_max = float(np.max(motion))
    if not math.isfinite(m_max) or m_max <= 0.0:
        return []
    threshold = m_max * min_ratio

    peaks: List[int] = []
    for i in range(1, n - 1):
        value = float(motion[i])
        if (
            value >= threshold
            and value >= float(motion[i - 1])
            and value > float(motion[i + 1])
        ):
            peaks.append(i)
    if n >= 2:
        if float(motion[0]) >= threshold and float(motion[0]) > float(motion[1]):
            peaks.append(0)
        if (
            float(motion[n - 1]) >= threshold
            and float(motion[n - 1]) > float(motion[n - 2])
        ):
            peaks.append(n - 1)
    if not peaks:
        return []

    peaks.sort(key=lambda i: (-float(motion[i]), i))
    return sorted(peaks[: max(1, int(top_k))])


def _detect_ball(
    addr_bgr: np.ndarray, roi: Tuple[int, ...]
) -> Optional[np.ndarray]:
    """在 Address 帧地面 ROI 内检球。

    策略（ARCHITECTURE-v3-clublite.md §2.2 D，Q2 按"球不一定可见"设计）：
    1. HoughCircles（半径 :data:`config.CLUBLITE_BALL_RADIUS_PX`，
       param2 = :data:`config.CLUBLITE_BALL_PARAM2`）——唯一圆直接采信，
       多圆视为歧义（网笼/多球）不采信；
    2. 白色 blob 兜底（灰度 > 200 + 圆度 + 尺寸范围）——唯一高置信 blob 采信。

    Returns:
        球心全局像素坐标 ``(cx, cy)``；未检出 / 歧义时返回 ``None``。
    """
    try:
        x0, y0, x1, y1 = (int(v) for v in roi)
        if x1 <= x0 or y1 <= y0:
            return None
        patch = addr_bgr[y0:y1, x0:x1]
        if patch.size == 0:
            return None
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        r_min, r_max = config.CLUBLITE_BALL_RADIUS_PX
        circles = cv2.HoughCircles(
            gray,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=max(30, 2 * r_max),
            param1=100,
            param2=int(config.CLUBLITE_BALL_PARAM2),
            minRadius=int(r_min),
            maxRadius=int(r_max),
        )
        if circles is not None and len(circles[0]) == 1:
            cx, cy, _r = np.round(circles[0][0]).astype(int)
            return np.array([x0 + int(cx), y0 + int(cy)], dtype=np.float64)

        # 白色 blob 兜底：阈值 > 200 + 轮廓圆度 + 半径范围
        _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(
            binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        good: List[np.ndarray] = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            perimeter = float(cv2.arcLength(contour, True))
            if area < 12.0 or perimeter <= 0.0:
                continue
            circularity = 4.0 * math.pi * area / (perimeter * perimeter)
            radius = math.sqrt(area / math.pi)
            if circularity < 0.7:
                continue
            if radius < r_min * 0.8 or radius > r_max * 1.2:
                continue
            moments = cv2.moments(contour)
            if moments["m00"] <= 0:
                continue
            cx = int(round(moments["m10"] / moments["m00"]))
            cy = int(round(moments["m01"] / moments["m00"]))
            good.append(np.array([x0 + cx, y0 + cy], dtype=np.float64))
        if len(good) == 1:
            return good[0]
        return None
    except cv2.error:
        return None
    except Exception:  # noqa: BLE001 - 球点只是加分项，失败跳过
        return None


# ---------------------------------------------------------------------------
# M2：候选帧杆身端点验证（简化 Hough）
# ---------------------------------------------------------------------------


def _shaft_lowest_y(
    bgr: np.ndarray,
    landmark_px: np.ndarray,
    grip_px: np.ndarray,
    club_len_px: float,
    view: CameraView = CameraView.FACE_ON,
) -> Optional[float]:
    """单帧简化 Hough：ROI 内找过握把的杆身线，返回杆头端点 y（越低越贴地）。

    只对候选帧调用（候选帧少，可放宽 Hough 阈值），不做全窗口连续跟踪——
    这是与下线重方案（逐帧 Hough + 时序预测）的本质区别。

    Args:
        bgr: 候选帧 BGR 图。
        landmark_px: ``(33, 2)`` 全关键点像素坐标（用于人体掩膜，排除骨架线）。
        grip_px: 握把像素坐标（取双腕中点近似）。
        club_len_px: 杆长先验（像素），用于 Hough ``minLineLength`` / ``maxLineGap``
            与握把过滤容差。
        view: 机位（当前仅影响日志，不改变算法分支）。

    Returns:
        杆头端点 y（图像纵坐标，**向下增大**；y 值越大越接近地面线/图像底部，
        即越"贴地"）；无有效杆身线时返回 ``None``。
    """
    try:
        h, w = bgr.shape[:2]
        if h <= 0 or w <= 0:
            return None
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        edges = cv2.Canny(enhanced, 50, 150)

        min_len = max(8, int(club_len_px * config.CLUB_HOUGH_MIN_LEN_RATIO))
        max_gap = max(2, int(club_len_px * config.CLUB_HOUGH_MAX_GAP_RATIO))
        lines = cv2.HoughLinesP(
            edges,
            1,
            np.pi / 180.0,
            threshold=_SHAFT_HOUGH_THRESHOLD,
            minLineLength=min_len,
            maxLineGap=max_gap,
        )
        if lines is None:
            return None

        grip = np.asarray(grip_px, dtype=np.float64).ravel()
        if not (math.isfinite(grip[0]) and math.isfinite(grip[1])):
            return None
        grip_tol = max(6.0, float(club_len_px) * _SHAFT_GRIP_DIST_RATIO)

        body_mask: Optional[np.ndarray] = None
        try:
            body_mask = geometry.skeleton_polygon_mask(
                landmark_px, (h, w), thickness=max(6, int(h // 120))
            )
        except Exception:  # noqa: BLE001 - 掩膜失败只影响过滤，不致命
            body_mask = None

        best_y: Optional[float] = None
        for line in lines:
            x1, y1, x2, y2 = (int(v) for v in line[0])
            p1 = np.array([x1, y1], dtype=np.float64)
            p2 = np.array([x2, y2], dtype=np.float64)
            if float(np.linalg.norm(p2 - p1)) < 4.0:
                continue
            # 过滤①：杆身延长线应经过握把（垂距容差）
            if geometry.point_line_distance(grip, p1, p2) > grip_tol:
                continue
            # 过滤②：杆头端点应在握把下方（击球瞬间杆身从握把向下伸向地面）
            head_y = float(max(y1, y2))
            if head_y < float(grip[1]) - _SHAFT_HEAD_Y_BIAS:
                continue
            # 过滤③：避开人体骨架（杆身不应与躯干/手臂重叠）
            if body_mask is not None:
                mid = (int(round((x1 + x2) / 2.0)), int(round((y1 + y2) / 2.0)))
                if (
                    0 <= mid[0] < w
                    and 0 <= mid[1] < h
                    and int(body_mask[mid[1], mid[0]]) > 0
                ):
                    continue
            if best_y is None or head_y > best_y:
                best_y = head_y
        return best_y
    except cv2.error:
        return None
    except Exception:  # noqa: BLE001 - M2 是加分项，失败跳过
        return None


# ---------------------------------------------------------------------------
# 评分与主入口
# ---------------------------------------------------------------------------


def _diff_centroid(
    gray_frames: Sequence[np.ndarray], idx: int, roi: Tuple[int, ...]
) -> Optional[np.ndarray]:
    """候选帧 ``idx`` 相对前一帧的 diff 质心（ROI 全局像素坐标）。

    取 ``|gray[idx] - gray[idx-1]| > CLUBLITE_DIFF_THRESH`` 像素的质心；
    无像素（低对比/静止）返回 ``None``。
    """
    if idx <= 0 or idx >= len(gray_frames):
        return None
    x0, y0, x1, y1 = (int(v) for v in roi)
    prev = gray_frames[idx - 1][y0:y1, x0:x1]
    cur = gray_frames[idx][y0:y1, x0:x1]
    if prev.size == 0 or prev.shape != cur.shape:
        return None
    diff = cv2.absdiff(cur, prev)
    mask = diff > config.CLUBLITE_DIFF_THRESH
    ys, xs = np.where(mask)
    if xs.size == 0:
        return None
    return np.array(
        [x0 + float(np.mean(xs)), y0 + float(np.mean(ys))], dtype=np.float64
    )


def refine_impact(
    video_path: str,
    frames: Sequence[FrameLandmarks],
    events: Sequence[SwingEvent],
    signals: SwingSignals,
    view: CameraView,
    meta: VideoMeta,
    frames_bgr: Optional[Dict[int, np.ndarray]] = None,
) -> ImpactRefineResult:
    """击球帧校正主入口。**永不外抛异常**：任何失败 -> ``available=False``。

    Args:
        video_path: 视频路径（``frames_bgr`` 为 ``None`` 时才解码）。
        frames: 姿态提取产出（采样序列）。
        events: 8 事件。
        signals: 切分信号包。
        view: 已解析机位（FACE_ON / DOWN_THE_LINE）。
        meta: 视频元信息。
        frames_bgr: 可选共享解码帧字典（原帧号 -> BGR）；给定时**不打开视频**。

    Returns:
        :class:`ImpactRefineResult`。``available=True`` 仅当：
        M1 选出最优候选且 ``|delta| ∈ [MIN_SHIFT, MAX_SHIFT]``。
    """
    try:
        if not config.CLUBLITE_ENABLED:
            return ImpactRefineResult()

        impact = next((e for e in events if e.key is PhaseKey.IMPACT), None)
        addr = next((e for e in events if e.key is PhaseKey.ADDRESS), None)
        if impact is None or addr is None or signals is None:
            return ImpactRefineResult()
        if not (0 <= impact.array_index < signals.n):
            return ImpactRefineResult()
        if view not in (CameraView.FACE_ON, CameraView.DOWN_THE_LINE):
            return ImpactRefineResult()

        # ---- Step 1~2：窗口规划 + 解码 -----------------------------------
        cand_frames, decode_frames = plan_refine_frames(
            events, signals, meta, frames=frames
        )
        if not cand_frames:
            return ImpactRefineResult()

        if frames_bgr is None:
            frames_bgr = grab_frames(video_path, decode_frames)
        elif not isinstance(frames_bgr, dict):
            return ImpactRefineResult()

        # 必须能拿到全部候选帧（缺帧 -> 校正不可信 -> G0）
        if not all(f in frames_bgr for f in cand_frames):
            return ImpactRefineResult()

        index_to_array: Dict[int, int] = {
            f.frame_index: i for i, f in enumerate(frames)
        }
        # 窗口 array 下标区间（与 plan_refine_frames 同一口径）
        lo, hi = _window_indices(events, signals, None, None)
        cand_indices = [
            index_to_array[f] for f in cand_frames if f in index_to_array
        ]
        cand_indices = [i for i in cand_indices if lo <= i <= hi]
        if not cand_indices:
            return ImpactRefineResult()

        # ---- Step 3：地面 ROI --------------------------------------------
        addr_lm = frames[addr.array_index]
        width = int(meta.width)
        height = int(meta.height)
        if width <= 0 or height <= 0:
            return ImpactRefineResult()
        nose_y = float(addr_lm.norm[geometry.NOSE, 1]) * height
        ankle_y = (
            float(addr_lm.norm[geometry.L_ANKLE, 1])
            + float(addr_lm.norm[geometry.R_ANKLE, 1])
        ) / 2.0 * height
        body_h_px = geometry.body_height_px(nose_y, ankle_y)
        if not math.isfinite(body_h_px) or body_h_px <= 0.0:
            return ImpactRefineResult()
        roi = _ground_roi(addr_lm, width, height, body_h_px, view)
        if roi is None:
            return ImpactRefineResult()

        # ---- Step 4：运动信号 + 候选 -------------------------------------
        gray_frames = [
            cv2.cvtColor(frames_bgr[f], cv2.COLOR_BGR2GRAY)
            for f in cand_frames
            if f in frames_bgr
        ]
        if len(gray_frames) < 2:
            return ImpactRefineResult()
        motion = _motion_signal(gray_frames, roi)
        raw_motion = _motion_signal(gray_frames, roi, smooth=False)
        candidates = _pick_candidates(
            motion, config.CLUBLITE_MOTION_MIN_RATIO, config.CLUBLITE_TOP_K
        )
        if not candidates:
            return ImpactRefineResult()
        # 精修：把平滑峰拉回原始信号的真实峰（消除平滑峰位右移）
        candidates = _refine_candidates(candidates, raw_motion)
        if not candidates:
            return ImpactRefineResult()

        # ---- Step 5：球点（可选，Q2 不依赖）-------------------------------
        ball_center: Optional[np.ndarray] = None
        addr_bgr = frames_bgr.get(addr.frame_index)
        if addr_bgr is not None:
            ball_center = _detect_ball(addr_bgr, roi)

        # ---- Step 6：评分（M1 运动×贴地×球点加权 + M2 杆身加分）----------
        m_max = float(np.max(motion))
        if not math.isfinite(m_max) or m_max <= 0.0:
            return ImpactRefineResult()

        grip_px: Optional[np.ndarray] = None
        landmark_px: Optional[np.ndarray] = None
        club_len_px = body_h_px * (
            _CLUB_LEN_RATIO_DTL
            if view is CameraView.DOWN_THE_LINE
            else _CLUB_LEN_RATIO_FACEON
        )
        try:
            ref_norm = frames[impact.array_index].norm
            grip_px = np.array(
                [
                    (float(ref_norm[geometry.L_WRIST, 0])
                     + float(ref_norm[geometry.R_WRIST, 0])) / 2.0 * width,
                    (float(ref_norm[geometry.L_WRIST, 1])
                     + float(ref_norm[geometry.R_WRIST, 1])) / 2.0 * height,
                ],
                dtype=np.float64,
            )
            if not (math.isfinite(grip_px[0]) and math.isfinite(grip_px[1])):
                grip_px = None
            landmark_px = ref_norm[:, :2] * np.array([width, height])
        except Exception:  # noqa: BLE001
            grip_px = None
            landmark_px = None

        shaft_bonus_weight = (
            _W_SHAFT_FACEON if view is CameraView.FACE_ON else _W_SHAFT_DTL
        )
        scores: List[float] = []
        shaft_ys: Dict[int, float] = {}
        for cand in candidates:
            centroid = _diff_centroid(gray_frames, cand, roi)
            motion_norm = float(motion[cand]) / m_max
            band_h = float(roi[3] - roi[1])
            ground_term = 1.0
            if centroid is not None and band_h > 0:
                # 贴地度：质心越靠带底（地面线）越接近 1
                ground_term = float(
                    np.clip((centroid[1] - roi[1]) / band_h, 0.0, 1.0)
                )
            elif centroid is None:
                ground_term = 0.0  # 无有效运动像素 -> 该候选不可信
            score = motion_norm * ground_term

            # 球点加权：候选帧 diff 质心靠近球心 -> 加分（Q2：纯加分，非必需）
            if (
                ball_center is not None
                and centroid is not None
                and float(np.linalg.norm(centroid - ball_center)) <= _BALL_NEAR_PX
            ):
                score *= 1.0 + _W_BALL

            # M2 杆身端点验证：候选帧杆头端点 y 越低（越贴地）越可信
            if (
                grip_px is not None
                and landmark_px is not None
                and cand < len(gray_frames)
            ):
                bgr_cand = frames_bgr.get(cand_frames[cand])
                if bgr_cand is not None:
                    shaft_y = _shaft_lowest_y(
                        bgr_cand, landmark_px, grip_px, club_len_px, view
                    )
                    if shaft_y is not None:
                        shaft_ys[cand] = shaft_y
                        # 端点贴近地面带（带底附近）才给加分
                        band_bottom = float(roi[3])
                        if shaft_y >= float(roi[1]) - band_h * 0.5:
                            score *= 1.0 + shaft_bonus_weight
            scores.append(score)

        if not scores:
            return ImpactRefineResult()
        # 全部候选得分 ≈ 0（低对比/无有效运动像素）→ 无可信候选 -> G0
        if max(scores) <= 1e-9:
            return ImpactRefineResult()

        # ---- Step 7：采纳判定 ---------------------------------------------
        # 先取分数最高的候选；平票时按 tie-breaker（与评分一致）：
        #   1) 优先有 M2 杆身端点验证的候选；
        #   2) 都有杆身时，优先杆头端点更贴地（shaft_lowest_y 的 y 值越大越
        #      接近图像底部/地面线 —— 与 _shaft_lowest_y 的语义一致）；
        #   3) 都无杆身时，优先更靠后的帧（腕部估计偏早，靠后更接近真实击球）。
        best_local = max(range(len(candidates)), key=lambda k: scores[k])
        best_offset = candidates[best_local]
        best_score = scores[best_local]
        for k, cand in enumerate(candidates):
            if abs(scores[k] - best_score) > 1e-9:
                continue
            if cand == best_offset:
                continue
            cand_has_shaft = cand in shaft_ys
            best_has_shaft = best_offset in shaft_ys
            if cand_has_shaft and not best_has_shaft:
                best_offset, best_local = cand, k
            elif cand_has_shaft and best_has_shaft:
                if shaft_ys[cand] > shaft_ys[best_offset]:
                    best_offset, best_local = cand, k
            elif not cand_has_shaft and not best_has_shaft:
                if cand > best_offset:
                    best_offset, best_local = cand, k

        old_array_index = impact.array_index
        # 运动峰帧（未加偏移）：评分选出的最优候选，array 下标。
        # 注意 cand_indices 与 gray_frames 一一对应，best_offset 是灰度帧偏移，
        # 故 peak_array_index == best_offset + lo（motion_peak_index 同值）。
        peak_array_index = cand_indices[best_offset]

        shaft_lowest_cand: Optional[int] = None
        if shaft_ys:
            shaft_lowest_cand = min(shaft_ys, key=lambda c: shaft_ys[c])

        # 系统偏移（v2 调优）：运动峰帧 -> 视觉接触瞬间。
        # 实测"运动峰"是球被杆头加速后的帧，视觉真实接触在其前 1 帧
        # （CLUBLITE_IMPACT_OFFSET = -1）。0 可回滚到 v1 行为。
        new_array_index = peak_array_index + config.CLUBLITE_IMPACT_OFFSET

        # 物理下界守卫（用户拍板硬约束）：偏移后的 impact 不得早于
        # top + min_gap（与 locate_impact 同口径），否则 reanchor 会挤压出
        # NO_SWING —— 此时宁可 G0（保持原 events），也不返回非法下标。
        top = next((e for e in events if e.key is PhaseKey.TOP), None)
        fe_eff = float(signals.fps_eff)
        if not math.isfinite(fe_eff) or fe_eff <= 0.0:
            fe_eff = 30.0
        min_gap = max(2, int(round(config.MIN_IMPACT_TOP_SEC * fe_eff)))
        lower_ok = (
            top is not None
            and 0 <= new_array_index < signals.n
            and new_array_index - top.array_index >= min_gap
        )
        if not lower_ok:
            logger.info(
                "impact refine rejected: offset impact %d violates top+min_gap "
                "(%d + %d) (video=%s)",
                new_array_index,
                top.array_index if top is not None else -1,
                min_gap,
                video_path,
            )
            result = ImpactRefineResult(
                available=False,
                method="none",
                old_array_index=old_array_index,
                new_array_index=new_array_index,
                delta_frames=new_array_index - old_array_index,
                confidence=float(
                    np.clip(float(motion[best_offset]) / m_max, 0.0, 1.0)
                ),
                ball_detected=ball_center is not None,
                motion_peak_index=best_offset + lo,
                shaft_lowest_index=(
                    shaft_lowest_cand + lo if shaft_lowest_cand is not None else None
                ),
                ball_center_px=(
                    (int(round(ball_center[0])), int(round(ball_center[1])))
                    if ball_center is not None
                    else None
                ),
            )
            return result

        delta = new_array_index - old_array_index

        method = "motion"
        if shaft_ys and best_offset in shaft_ys:
            method = "motion+shaft"

        # 置信度 = 最优候选运动峰归一化强度（0~1，越强越可信）
        confidence = float(
            np.clip(float(motion[best_offset]) / m_max, 0.0, 1.0)
        )

        result = ImpactRefineResult(
            available=True,
            method=method,
            old_array_index=old_array_index,
            new_array_index=new_array_index,
            delta_frames=delta,
            confidence=confidence,
            ball_detected=ball_center is not None,
            motion_peak_index=best_offset + lo,
            shaft_lowest_index=(
                shaft_lowest_cand + lo if shaft_lowest_cand is not None else None
            ),
            ball_center_px=(
                (int(round(ball_center[0])), int(round(ball_center[1])))
                if ball_center is not None
                else None
            ),
        )
        # 采纳判定（v2 语义）：
        # 1) 运动峰位移（未加偏移）须在 [MIN, MAX] —— 候选本身可信，过滤弱信号抖动；
        # 2) 偏移后的最终 delta 不超过 MAX；
        # 3) delta==0（偏移把运动峰拉回原估计，如正面1）视为合法"无操作校正"：
        #    算法确认原 impact 就是视觉接触帧，照常 available=True
        #    （reanchor 对同下标幂等返回原 events，主链路无变化）。
        if not (
            config.CLUBLITE_MIN_SHIFT_FRAMES
            <= abs(peak_array_index - old_array_index)
            <= config.CLUBLITE_MAX_SHIFT_FRAMES
            and abs(delta) <= config.CLUBLITE_MAX_SHIFT_FRAMES
        ):
            result.available = False
            result.method = "none"
            logger.info(
                "impact refine rejected: peak_delta=%+d out of [%d, %d] "
                "or final delta=%+d beyond MAX (video=%s)",
                peak_array_index - old_array_index,
                config.CLUBLITE_MIN_SHIFT_FRAMES,
                config.CLUBLITE_MAX_SHIFT_FRAMES,
                delta,
                video_path,
            )
        else:
            logger.info(
                "impact refined: %d -> %d (delta=%+d, peak=%d, method=%s, "
                "conf=%.2f, ball=%s)",
                old_array_index,
                new_array_index,
                delta,
                peak_array_index,
                method,
                confidence,
                ball_center is not None,
            )
        return result
    except Exception:  # noqa: BLE001 - 模块级硬约束：任何失败 -> available=False
        logger.exception("impact refine failed (video=%s)", video_path)
        return ImpactRefineResult()
