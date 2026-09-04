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

D 方案（2026-08 用户拍板）：横扫式运动峰偏晚问题。真实视频
22030124ed3bce12cdec7c629d0c6cc8 中，M1 运动峰落在 121（杆身水平横扫跨越
像素最多、帧差最强），但真实击球在 115、M2 杆身最低点在 116。根因：全窗口
找最优时横扫帧 motion 优势压过杆身最低点。改动：把 M2 杆身最低点
（``_shaft_lowest_y`` y 值最大的候选帧）作为**先验锚点**，只在锚点
±:data:`config.CLUBLITE_ANCHOR_WINDOW` 邻域内按综合 score 选帧。回退条件：
M2 不可用 / 邻域内全部 score≈0 / 邻域最优远低于全窗口最优
（:data:`config.CLUBLITE_ANCHOR_MIN_SCORE_RATIO`=0.7，假锚点守卫——实测
0bb16a97/1446d1b9/a4fba3d2 的"杆身最低点"是 Hough 假阳性/弱运动帧，ratio
仅 0.11/0.55/0.36，必须回退 v2 全窗口）。配套
:data:`config.CLUBLITE_USE_ANCHOR` 一键开关（False 即回旧逻辑），
:data:`config.CLUBLITE_IMPACT_OFFSET` 实验定为 0（锚点已向真实接触靠拢，
结论见 docs/VALIDATION-CLUBLITE.md §3）。
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

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
from . import club_detector  # M3 最低点：复用路径 A 的 _detect_hough / _skeleton_segments

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
    view: CameraView = CameraView.FACE_ON,
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
        view: 拍摄机位（face-on / DTL）。传给 :func:`segmenter.reanchor_impact`，
            让预计算的 ⑤ 与主链路最终 reanchor 使用同一机位阈值（DTL 的 ⑤ 更靠后，
            若不传，DTL 视频校正后的 ⑤ 帧可能不在解码并集内、渲染走兜底帧）。
            默认 face-on 保持历史行为。

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
        rebuilt = segmenter.reanchor_impact(
            frames, signals, events, array_index, view=view
        )
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


def _shaft_scan_window(
    cand_frames: Sequence[int],
    frames_bgr: Mapping[int, np.ndarray],
    frames: Sequence[FrameLandmarks],
    cand_indices: Sequence[int],
    width: int,
    height: int,
    club_len_px: float,
    view: CameraView,
) -> Dict[int, float]:
    """**M2 全窗口化**：对 refine 窗口内每一帧独立跑 :func:`_shaft_lowest_y`。

    与 Step 6 的 Top-K 版本有两点本质差异：

    1. **覆盖范围**：不局限于 M1 挑出的 Top-K 运动峰，而是扫描窗口内全部帧。
       这修掉了「真实杆头最低点不在 M1 候选里 -> 锚点被迫落在次优帧」的根因
       （实测横屏 1446d1b9：真实最低点 array 35，M1 候选只有 [34, 39]）。
    2. **握把逐帧化**：Top-K 版本共用 **impact 帧**的握把坐标（候选都在 impact
       附近，握把几乎不动，共用成立）。全窗口跨度更大，下杆/送杆期握把已明显
       移动，继续共用会让 :func:`_shaft_lowest_y` 的「延长线过握把」过滤失效
       —— 因此**每帧用自己那一帧的 landmarks 算握把**。

    Args:
        cand_frames: 窗口内候选帧的**原视频帧号**（升序，与 ``cand_indices`` 平行）。
        frames_bgr: 原帧号 -> BGR 图。
        frames: 姿态序列（按 array 下标索引，提供逐帧 landmarks）。
        cand_indices: 灰度帧偏移 -> array 下标。
        width / height: 视频像素尺寸。
        club_len_px: 杆长先验（像素）。
        view: 机位。

    Returns:
        ``{灰度帧偏移: 杆头端点 y}``，只含 Hough 成功且该帧 landmarks 可用的帧；
        扫描不可用（无帧 / 尺寸非法）时返回空 dict（调用方回退 Top-K 锚点）。
    """
    out: Dict[int, float] = {}
    n = int(len(cand_frames))
    if n == 0 or width <= 0 or height <= 0:
        return out

    # 成本上限：窗口过大时等距抽帧（保首保尾，中间均匀取）
    max_frames = max(1, int(config.CLUBLITE_M2_FULL_MAX_FRAMES))
    if n > max_frames:
        step_f = (n - 1) / float(max_frames - 1) if max_frames > 1 else 0.0
        offsets = sorted({int(round(k * step_f)) for k in range(max_frames)})
    else:
        offsets = list(range(n))

    scale = np.array([float(width), float(height)], dtype=np.float64)
    for off in offsets:
        if not (0 <= off < len(cand_frames)) or off >= len(cand_indices):
            continue
        array_index = int(cand_indices[off])
        if not (0 <= array_index < len(frames)):
            continue
        bgr = frames_bgr.get(int(cand_frames[off]))
        if bgr is None:
            continue
        try:
            ref_norm = frames[array_index].norm
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
                continue
            landmark_px = ref_norm[:, :2] * scale
        except Exception:  # noqa: BLE001 - 单帧 landmarks 缺失只跳过该帧
            continue

        shaft_y = _shaft_lowest_y(
            bgr, landmark_px, grip_px, club_len_px, view
        )
        if shaft_y is not None and math.isfinite(float(shaft_y)):
            out[off] = float(shaft_y)
    return out


def _shaft_scan_window_fresh(
    cand_frames: Sequence[int],
    frames_bgr: Mapping[int, np.ndarray],
    frames: Sequence[FrameLandmarks],
    cand_indices: Sequence[int],
    width: int,
    height: int,
    club_len_px: float,
    view: CameraView,
) -> Dict[int, float]:
    """**M3 fresh 最低点**：全窗口逐帧独立跑路径 A 的 ``_detect_hough``。

    与 :func:`_shaft_scan_window`（M2 全窗口化，用简化 :func:`_shaft_lowest_y`）
    的本质差异是**信号源**：

    - ``_shaft_lowest_y`` 无 ROI 扇形、无骨架共线过滤，「延长线过握把」过滤
      不约束端点到握把距离 → 击球窗口内被背景直线假阳性淹没（实测 11.mp4
      0/49 命中）；
    - ``_detect_hough`` 带 ROI + 扇形 + body_mask + skeleton 四道过滤，且以
      **fresh 模式**（``pred_dir`` 向下、``fan_deg``/``dir_tol`` 拉满让方向
      约束失效）逐帧独立检测，绕开时序预测级联污染（实测 11.mp4 49/49 命中、
      杆头最低点 = 117 = 用户手工真值）。

    Args:
        cand_frames: 窗口内候选帧的原视频帧号（升序，与 ``cand_indices`` 平行）。
        frames_bgr: 原帧号 -> BGR 图。
        frames: 姿态序列（按 array 下标索引，提供逐帧 landmarks）。
        cand_indices: 灰度帧偏移 -> array 下标。
        width / height: 视频像素尺寸。
        club_len_px: 杆长先验（像素）。
        view: 机位（当前仅影响日志，不改变算法分支）。

    Returns:
        ``{灰度帧偏移: 杆头端点 y}``；只含 Hough 成功且 landmarks 可用的帧。
    """
    out: Dict[int, float] = {}
    n = int(len(cand_frames))
    if n == 0 or width <= 0 or height <= 0:
        return out

    # 成本上限（与 M2 全窗口同口径，等距抽帧保首保尾）
    max_frames = max(1, int(config.CLUBLITE_M2_FULL_MAX_FRAMES))
    if n > max_frames:
        step_f = (n - 1) / float(max_frames - 1) if max_frames > 1 else 0.0
        offsets = sorted({int(round(k * step_f)) for k in range(max_frames)})
    else:
        offsets = list(range(n))

    scale = np.array([float(width), float(height)], dtype=np.float64)
    for off in offsets:
        if not (0 <= off < len(cand_frames)) or off >= len(cand_indices):
            continue
        array_index = int(cand_indices[off])
        if not (0 <= array_index < len(frames)):
            continue
        bgr = frames_bgr.get(int(cand_frames[off]))
        if bgr is None:
            continue
        try:
            ref_norm = frames[array_index].norm
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
                continue
            landmark_px = ref_norm[:, :2] * scale
        except Exception:  # noqa: BLE001 - 单帧 landmarks 缺失只跳过该帧
            continue

        try:
            body_mask = geometry.skeleton_polygon_mask(
                landmark_px, (height, width), thickness=max(6, int(height // 120))
            )
        except Exception:  # noqa: BLE001 - 掩膜失败只影响过滤，不致命
            body_mask = np.zeros((height, width), dtype=np.uint8)
        try:
            skeleton = club_detector._skeleton_segments(landmark_px)
        except Exception:  # noqa: BLE001
            skeleton = []

        # fresh 模式：pred_dir 向下，fan/dir_tol 拉满让方向约束失效
        outcome = club_detector._detect_hough(
            bgr,
            grip_px,
            club_len_px,
            np.array([0.0, 1.0], dtype=np.float64),
            180.0,  # fan_deg：让扇形约束失效
            180.0,  # dir_tol_deg：让方向约束失效
            body_mask,
            skeleton,
        )
        if outcome is None:
            continue
        head_px, _shaft_dir, _conf = outcome
        head_y = float(head_px[1])
        if math.isfinite(head_y):
            out[off] = head_y
    return out


def _pick_full_window_anchor(
    shaft_ys: Mapping[int, float],
    raw_motion: np.ndarray,
    min_ratio: float,
) -> Optional[int]:
    """从全窗口扫描结果里挑锚点：**杆头最低**且**该帧确有运动**。

    单纯取 y 最大会被两类伪影带偏，故加运动支持门槛：

    - **静止帧的 Hough 假阳性**：背景里的直边（球杆袋、地平线、广告牌）在
      静态帧上更清晰，反而比糊掉的击球帧更容易给出"很低"的端点；
    - **下杆早期/送杆期**：杆头确实低，但那不是击球。

    要求锚点帧自身的帧差强度 ≥ ``min_ratio × 窗口最大强度``，即"这一帧确实
    在剧烈运动"。与 :data:`config.CLUBLITE_MOTION_MIN_RATIO` 同口径。

    Args:
        shaft_ys: :func:`_shaft_scan_window` 的结果（偏移 -> 杆头 y）。
        raw_motion: 未平滑的运动信号（与偏移同索引）。
        min_ratio: 运动支持门槛比例。

    Returns:
        锚点的灰度帧偏移；无满足条件的帧时返回 ``None``（调用方回退 Top-K）。
    """
    if not shaft_ys:
        return None
    m_max = float(np.max(raw_motion)) if len(raw_motion) else 0.0
    if not math.isfinite(m_max) or m_max <= 0.0:
        return None
    threshold = m_max * float(min_ratio)
    # y 降序（杆头最低优先），同 y 时取靠前的帧
    for off in sorted(shaft_ys, key=lambda c: (-float(shaft_ys[c]), c)):
        if 0 <= int(off) < len(raw_motion):
            if float(raw_motion[int(off)]) >= threshold:
                return int(off)
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


def _anchor_neighborhood(
    candidates: Sequence[int],
    cand_indices: Sequence[int],
    shaft_ys: Dict[int, float],
    lo: int,
    hi: int,
    window: int,
) -> Optional[Tuple[List[int], int, int, int]]:
    """D 方案：M2 杆身最低点先验锚点，返回其 ±window 邻域内的候选下标。

    锚点 = ``shaft_ys`` 中杆头端点 y 最大的候选（y 越大越接近地面线/图像底部，
    即"杆头最贴地"——真实击球信号）。横扫式运动峰（杆身水平横扫跨越像素
    最多、帧差最强）常晚于真实击球数帧，因此只在锚点邻域内选帧，把横扫帧
    排除在候选集外（CLUBLITE_ANCHOR_WINDOW 默认 3）。

    Args:
        candidates: 候选（灰度帧偏移，升序，与 ``scores`` 平行）。
        cand_indices: 灰度帧偏移 -> array 下标（``cand_indices[offset]``，
            与 ``cand_frames`` / ``gray_frames`` 平行）。
        shaft_ys: 候选偏移 -> 杆头端点 y（:func:`_shaft_lowest_y` 结果）。
        lo / hi: 搜索窗口 array 下标闭区间（邻域 clamp 到该区间）。
        window: 锚点邻域半窗（采样帧，>= 0）。

    Returns:
        ``(selection, anchor_array, win_lo, win_hi)``：``selection`` 为邻域内
        候选在 ``candidates`` 中的下标（升序）；``anchor_array`` 为锚点 array
        下标；``win_lo`` / ``win_hi`` 为 clamp 后的邻域闭区间。
        M2 不可用（``shaft_ys`` 为空）或 ``window < 0`` 时返回 ``None``
        （调用方回退全窗口逻辑）。
    """
    if not shaft_ys or window < 0:
        return None
    anchor_cand = max(shaft_ys, key=lambda c: float(shaft_ys[c]))
    anchor_array = int(cand_indices[anchor_cand])
    w = int(window)
    win_lo = max(int(lo), anchor_array - w)
    win_hi = min(int(hi), anchor_array + w)
    # 注意：cand_indices 按下标（灰度帧偏移）索引，故用候选偏移 c 取 array 下标
    selection = [
        k
        for k, c in enumerate(candidates)
        if win_lo <= int(cand_indices[c]) <= win_hi
    ]
    if not selection:
        return None
    return selection, anchor_array, win_lo, win_hi


def _anchor_window_credible(
    scores: Sequence[float],
    selection: Sequence[int],
    min_ratio: float,
) -> bool:
    """锚点邻域可信度：邻域内最优得分须 ≥ ``min_ratio`` × 全窗口最优得分。

    D 方案校准（12 段真实视频，见 docs/VALIDATION-CLUBLITE.md §3）：横扫式
    运动峰偏晚只在该假设成立时可信——锚点邻域内要有与全窗口最优相当的候选
    （即"横扫帧之外、接触附近的候选仍然可信"）。若邻域最优远低于全窗口最优
    （实测 0bb16a97 邻域最优 0.07 vs 全窗口 0.62；a4fba3d2 0.013 vs 0.035；
    1446d1b9 0.27 vs 0.49），说明锚点是 Hough 假阳性/弱运动帧，应回退全窗口
    （v2 行为）。新样本 22030124 邻域最优 0.606 vs 全窗口 0.644（ratio 0.94）
    通过，锚点生效 -> 116。

    Args:
        scores: 与 ``candidates`` 平行的综合得分。
        selection: 锚点邻域内候选在 ``candidates`` 中的下标（升序）。
        min_ratio: 可信度下限（:data:`config.CLUBLITE_ANCHOR_MIN_SCORE_RATIO`）。

    Returns:
        ``True`` = 邻域可信，可把候选集收缩到锚点邻域；``False`` = 回退全窗口。
    """
    if not selection:
        return False
    best_full = max(float(s) for s in scores)
    if best_full <= 1e-9:
        return False
    best_window = max(float(scores[k]) for k in selection)
    return best_window >= float(min_ratio) * best_full


def _select_best(
    candidates: Sequence[int],
    scores: Sequence[float],
    shaft_ys: Dict[int, float],
    selection: Sequence[int],
) -> Tuple[int, int, float]:
    """在候选下标 ``selection`` 内按综合 score 选最优（含 M2 tie-breaker）。

    平票时按 tie-breaker（与评分一致）：
    1. 优先有 M2 杆身端点验证的候选；
    2. 都有杆身时，优先杆头端点更贴地（``shaft_ys`` 的 y 值越大越接近图像
       底部/地面线 —— 与 :func:`_shaft_lowest_y` 的语义一致）；
    3. 都无杆身时，优先更靠后的帧（腕部估计偏早，靠后更接近真实击球）。

    Args:
        candidates: 候选（灰度帧偏移，升序，与 ``scores`` 平行）。
        scores: 与 ``candidates`` 平行的综合得分。
        shaft_ys: 候选偏移 -> 杆头端点 y（M2 结果；可能为空）。
        selection: 参与选帧的候选在 ``candidates`` 中的下标（升序）。

    Returns:
        ``(best_offset, best_k, best_score)``：``best_offset`` 为最优候选
        （灰度帧偏移）；``best_k`` 为其在 ``candidates`` 中的下标；
        ``best_score`` 为其得分。``selection`` 为空时返回 ``(-1, -1, 0.0)``。
    """
    if not selection:
        return -1, -1, 0.0
    best_k = max(selection, key=lambda k: float(scores[k]))
    best_offset = int(candidates[best_k])
    best_score = float(scores[best_k])
    for k in selection:
        if abs(float(scores[k]) - best_score) > 1e-9:
            continue
        if k == best_k:
            continue
        cand = int(candidates[k])
        cand_has_shaft = cand in shaft_ys
        best_has_shaft = best_offset in shaft_ys
        if cand_has_shaft and not best_has_shaft:
            best_offset, best_k = cand, k
        elif cand_has_shaft and best_has_shaft:
            if float(shaft_ys[cand]) > float(shaft_ys[best_offset]):
                best_offset, best_k = cand, k
        elif not cand_has_shaft and not best_has_shaft:
            if cand > best_offset:
                best_offset, best_k = cand, k
    return best_offset, best_k, best_score


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
            frames_bgr = grab_frames(video_path, decode_frames, orientation=meta.orientation)
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

        # ---- Step 6b：D 方案先验锚点邻域（CLUBLITE_USE_ANCHOR）------------
        # 横扫式运动峰偏晚的根因：全窗口找最优时，横扫帧（杆身水平横扫跨越
        # 像素最多、帧差最强）motion 优势压过杆身最低点。真实击球信号是"杆头
        # 最贴地"（M2 ``_shaft_lowest_y`` y 值最大的候选帧 = 锚点），因此只在
        # 锚点 ±CLUBLITE_ANCHOR_WINDOW 邻域内按综合 score 选帧；回退条件：
        # M2 不可用 / 邻域内全部 score≈0 / 邻域最优远低于全窗口最优
        # （CLUBLITE_ANCHOR_MIN_SCORE_RATIO，假锚点守卫）-> 回退全窗口
        # （v2 行为，原逻辑不变）。
        anchor_array: Optional[int] = None
        anchor_used = False
        anchor_src = "none"
        selection: List[int] = list(range(len(candidates)))
        if config.CLUBLITE_USE_ANCHOR:
            # ---- M2 全窗口化（方案 A）------------------------------------
            # 锚点来源按优先级尝试两级，任一成功即收缩候选集：
            #   1) "full"  全窗口逐帧扫描（_shaft_scan_window）挑出的杆头最低点
            #   2) "topk"  M1 Top-K 候选里的杆头最低点（v3 原行为）
            # 两级都失败 -> 保持全候选集（v2 行为，原逻辑不变）。
            sources: List[Tuple[str, Dict[int, float]]] = []
            full_scanned = 0
            full_anchor_off: Optional[int] = None
            if config.CLUBLITE_M2_FULL_WINDOW and club_len_px > 0.0:
                full_shaft_ys = _shaft_scan_window(
                    cand_frames,
                    frames_bgr,
                    frames,
                    cand_indices,
                    width,
                    height,
                    club_len_px,
                    view,
                )
                full_scanned = len(full_shaft_ys)
                full_off = _pick_full_window_anchor(
                    full_shaft_ys,
                    raw_motion,
                    float(config.CLUBLITE_M2_FULL_MOTION_RATIO),
                )
                if full_off is not None:
                    full_anchor_off = int(full_off)
                    # 只把该帧交给 _anchor_neighborhood：锁定锚点，避免次低的
                    # 全窗口点（可能位于下杆早期/送杆期）抢走锚定权。
                    sources.append(
                        ("full", {full_off: float(full_shaft_ys[full_off])})
                    )
                logger.info(
                    "impact refine M2 full-window scan: %d/%d frames with shaft, "
                    "anchor offset=%s (array=%s) (video=%s)",
                    full_scanned,
                    len(cand_frames),
                    full_off,
                    cand_indices[full_off] if full_off is not None else "n/a",
                    video_path,
                )
            sources.append(("topk", dict(shaft_ys)))

            for src_name, src_ys in sources:
                neighborhood = _anchor_neighborhood(
                    candidates,
                    cand_indices,
                    src_ys,
                    lo,
                    hi,
                    int(config.CLUBLITE_ANCHOR_WINDOW),
                )
                if neighborhood is None:
                    continue
                window_sel, cand_anchor, win_lo, win_hi = neighborhood
                if _anchor_window_credible(
                    scores,
                    window_sel,
                    float(config.CLUBLITE_ANCHOR_MIN_SCORE_RATIO),
                ):
                    selection = window_sel
                    anchor_used = True
                    anchor_array = cand_anchor
                    anchor_src = src_name
                    logger.info(
                        "impact refine anchor: src=%s array=%d window=[%d, %d] "
                        "selection=%s (video=%s)",
                        anchor_src,
                        anchor_array,
                        win_lo,
                        win_hi,
                        selection,
                        video_path,
                    )
                    break

        # ---- Step 7：采纳判定 ---------------------------------------------
        # 在 selection（锚点邻域或全候选集）内取分数最高的候选；平票时按
        # tie-breaker（与评分一致，见 _select_best）：
        #   1) 优先有 M2 杆身端点验证的候选；
        #   2) 都有杆身时，优先杆头端点更贴地（shaft_lowest_y 的 y 值越大越
        #      接近图像底部/地面线 —— 与 _shaft_lowest_y 的语义一致）；
        #   3) 都无杆身时，优先更靠后的帧（腕部估计偏早，靠后更接近真实击球）。
        best_offset, _best_k, _best_score = _select_best(
            candidates, scores, shaft_ys, selection
        )
        if best_offset < 0:
            return ImpactRefineResult()

        old_array_index = impact.array_index
        # 运动峰帧（未加偏移）：评分选出的最优候选，array 下标。
        # 注意 cand_indices 与 gray_frames 一一对应，best_offset 是灰度帧偏移，
        # 故 peak_array_index == best_offset + lo（motion_peak_index 同值）。
        peak_array_index = cand_indices[best_offset]

        shaft_lowest_cand: Optional[int] = None
        if shaft_ys:
            # 杆头最低点 = shaft_lowest_y 的 y 值最大的候选（y 越大越贴地），
            # 与 D 方案锚点同口径（历史实现误用 min，2026-08 修正）。
            shaft_lowest_cand = max(shaft_ys, key=lambda c: float(shaft_ys[c]))
        # 全窗口锚点生效时，用全窗口扫描到的杆头最低点覆盖（它才是真正驱动
        # 选帧的那个锚；Top-K 版本可能只覆盖了送杆期的次优点）。
        if anchor_src == "full" and full_anchor_off is not None:
            shaft_lowest_cand = full_anchor_off

        # 系统偏移（v2 调优 -> D 方案）：运动峰帧 -> 视觉接触瞬间。
        # D 方案（2026-08 实验结论）：锚点法已把选帧拉向真实接触，偏移不再
        # 需要，CLUBLITE_IMPACT_OFFSET = 0（见 docs/VALIDATION-CLUBLITE.md §3）。
        new_array_index = peak_array_index + config.CLUBLITE_IMPACT_OFFSET

        # ---- 物理窗口守卫（三条，任一不满足 -> G0 保持原 events）--------
        # 1) 下界：偏移后的 impact 不得早于 top + min_gap（与 locate_impact
        #    同口径，用户拍板硬约束），否则 reanchor 会挤压出 NO_SWING。
        # 2) 送杆下界（2026-09-04 新增）：impact -> finish 不得短于
        #    CLUBLITE_MIN_FOLLOW_THROUGH_SEC。根因：M1 横扫式运动峰 + M2 杆头
        #    最低点都可能落在送杆期，把 impact 推到 finish 前仅 2~3 帧。
        # 3) 下杆上界（2026-09-04 新增）：top -> impact 不得长于
        #    CLUBLITE_MAX_DOWNSTROKE_SEC。真实下杆 0.20~0.30s；超过即说明
        #    选帧落在送杆期（此时送杆下界可能因 finish 偏远而不触发）。
        top = next((e for e in events if e.key is PhaseKey.TOP), None)
        finish = next((e for e in events if e.key is PhaseKey.FINISH), None)
        fe_eff = float(signals.fps_eff)
        if not math.isfinite(fe_eff) or fe_eff <= 0.0:
            fe_eff = 30.0
        min_gap = max(2, int(round(config.MIN_IMPACT_TOP_SEC * fe_eff)))
        min_follow = max(
            1, int(round(config.CLUBLITE_MIN_FOLLOW_THROUGH_SEC * fe_eff))
        )
        max_down = max(
            min_gap, int(round(config.CLUBLITE_MAX_DOWNSTROKE_SEC * fe_eff))
        )

        in_range = 0 <= new_array_index < signals.n
        lower_ok = (
            top is not None
            and in_range
            and new_array_index - top.array_index >= min_gap
        )
        follow_ok = (
            finish is None
            or finish.array_index - new_array_index >= min_follow
        )
        down_ok = (
            top is None
            or new_array_index - top.array_index <= max_down
        )
        if not (lower_ok and follow_ok and down_ok):
            logger.info(
                "impact refine rejected: impact %d violates guard "
                "(top=%s finish=%s | min_gap=%d min_follow=%d max_down=%d | "
                "lower=%s follow=%s down=%s) (video=%s)",
                new_array_index,
                top.array_index if top is not None else "n/a",
                finish.array_index if finish is not None else "n/a",
                min_gap,
                min_follow,
                max_down,
                lower_ok,
                follow_ok,
                down_ok,
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
                "impact refined: %d -> %d (delta=%+d, peak=%d, anchor=%s, "
                "anchor_used=%s, method=%s, conf=%.2f, ball=%s)",
                old_array_index,
                new_array_index,
                delta,
                peak_array_index,
                anchor_array if anchor_array is not None else "n/a",
                anchor_used,
                method,
                confidence,
                ball_center is not None,
            )
        return result
    except Exception:  # noqa: BLE001 - 模块级硬约束：任何失败 -> available=False
        logger.exception("impact refine failed (video=%s)", video_path)
        return ImpactRefineResult()


def refine_impact_lowest_point(
    video_path: str,
    frames: Sequence[FrameLandmarks],
    events: Sequence[SwingEvent],
    signals: SwingSignals,
    view: CameraView,
    meta: VideoMeta,
    frames_bgr: Optional[Dict[int, np.ndarray]] = None,
) -> Optional[int]:
    """**M3**：以「杆头离地面最近的点」直接作为击球帧，返回校正后 array 下标。

    这是独立于 SwingNet 概率与规则引擎运动峰的**几何信号**。当前只用在
    SwingNet DTL 路径——其 Impact 已 ≈1 帧误差，但杆头最低点更贴近真实接触
    （实测 11.mp4：SwingNet impact=116、最低点=117、用户手工真值=117）。

    信号链：
    1. fresh 模式 ``_detect_hough`` 全窗口扫描（:func:`_shaft_scan_window_fresh`）；
    2. 取杆头端点 y 最大帧（越贴地），用运动支持门槛排除静止帧 Hough 假阳性
       （:func:`_pick_full_window_anchor`）；
    3. 物理窗口守卫（下界 top+min_gap / 送杆下界 / 下杆上界，与
       :func:`refine_impact` Step 7 同口径）；
    4. 位移幅度守卫（|delta| ≤ MAX_SHIFT）。

    Returns:
        校正后 impact 的 array 下标；任何一步不可信 / 守卫拒绝 / 异常均返回
        ``None``（调用方保持原 events 不变，与 ``refine_impact`` 的 G0 语义一致）。
    """
    try:
        if not config.CLUBLITE_M3_FRESH_ANCHOR:
            return None
        impact = next((e for e in events if e.key is PhaseKey.IMPACT), None)
        top = next((e for e in events if e.key is PhaseKey.TOP), None)
        finish = next((e for e in events if e.key is PhaseKey.FINISH), None)
        addr = next((e for e in events if e.key is PhaseKey.ADDRESS), None)
        if impact is None or top is None or addr is None or signals is None:
            return None
        if not (0 <= impact.array_index < signals.n):
            return None
        if view not in (CameraView.FACE_ON, CameraView.DOWN_THE_LINE):
            return None

        # ---- 窗口规划 + 解码（与 refine_impact Step 1~2 同口径）----------
        cand_frames, decode_frames = plan_refine_frames(
            events, signals, meta, frames=frames
        )
        if not cand_frames:
            return None

        if frames_bgr is None:
            frames_bgr = grab_frames(
                video_path, decode_frames, orientation=meta.orientation
            )
        elif not isinstance(frames_bgr, dict):
            return None
        if not all(f in frames_bgr for f in cand_frames):
            return None

        index_to_array: Dict[int, int] = {
            f.frame_index: i for i, f in enumerate(frames)
        }
        lo, hi = _window_indices(events, signals, None, None)
        cand_indices = [
            index_to_array[f] for f in cand_frames if f in index_to_array
        ]
        cand_indices = [i for i in cand_indices if lo <= i <= hi]
        if not cand_indices:
            return None

        # ---- 杆长先验 + 地面 ROI ----------------------------------------
        width = int(meta.width)
        height = int(meta.height)
        if width <= 0 or height <= 0:
            return None
        addr_lm = frames[addr.array_index]
        nose_y = float(addr_lm.norm[geometry.NOSE, 1]) * height
        ankle_y = (
            float(addr_lm.norm[geometry.L_ANKLE, 1])
            + float(addr_lm.norm[geometry.R_ANKLE, 1])
        ) / 2.0 * height
        body_h_px = geometry.body_height_px(nose_y, ankle_y)
        if not math.isfinite(body_h_px) or body_h_px <= 0.0:
            return None
        club_len_px = body_h_px * (
            _CLUB_LEN_RATIO_DTL
            if view is CameraView.DOWN_THE_LINE
            else _CLUB_LEN_RATIO_FACEON
        )
        roi = _ground_roi(addr_lm, width, height, body_h_px, view)
        if roi is None:
            return None

        # ---- fresh 全窗口扫描 + 运动支持门槛 ----------------------------
        shaft_ys = _shaft_scan_window_fresh(
            cand_frames, frames_bgr, frames, cand_indices,
            width, height, club_len_px, view,
        )
        if not shaft_ys:
            return None

        gray_frames = [
            cv2.cvtColor(frames_bgr[f], cv2.COLOR_BGR2GRAY)
            for f in cand_frames
            if f in frames_bgr
        ]
        if len(gray_frames) < 2:
            return None
        raw_motion = _motion_signal(gray_frames, roi, smooth=False)

        off = _pick_full_window_anchor(
            shaft_ys, raw_motion, float(config.CLUBLITE_M2_FULL_MOTION_RATIO)
        )
        if off is None:
            return None
        new_array_index = int(cand_indices[off])

        # ---- 物理窗口守卫（与 refine_impact Step 7 同口径）--------------
        fe_eff = float(signals.fps_eff)
        if not math.isfinite(fe_eff) or fe_eff <= 0.0:
            fe_eff = 30.0
        min_gap = max(2, int(round(config.MIN_IMPACT_TOP_SEC * fe_eff)))
        min_follow = max(
            1, int(round(config.CLUBLITE_MIN_FOLLOW_THROUGH_SEC * fe_eff))
        )
        max_down = max(
            min_gap, int(round(config.CLUBLITE_MAX_DOWNSTROKE_SEC * fe_eff))
        )
        in_range = 0 <= new_array_index < signals.n
        lower_ok = in_range and new_array_index - top.array_index >= min_gap
        follow_ok = (
            finish is None
            or finish.array_index - new_array_index >= min_follow
        )
        down_ok = new_array_index - top.array_index <= max_down
        if not (lower_ok and follow_ok and down_ok):
            logger.info(
                "impact lowest-point rejected: impact %d violates guard "
                "(top=%d finish=%s | min_gap=%d min_follow=%d max_down=%d | "
                "lower=%s follow=%s down=%s) (video=%s)",
                new_array_index,
                top.array_index,
                finish.array_index if finish is not None else "n/a",
                min_gap,
                min_follow,
                max_down,
                lower_ok,
                follow_ok,
                down_ok,
                video_path,
            )
            return None

        # ---- 位移幅度守卫 ------------------------------------------------
        delta = new_array_index - impact.array_index
        if not (
            config.CLUBLITE_MIN_SHIFT_FRAMES
            <= abs(delta)
            <= config.CLUBLITE_MAX_SHIFT_FRAMES
        ):
            logger.info(
                "impact lowest-point rejected: delta=%+d out of [%d, %d] "
                "(video=%s)",
                delta,
                config.CLUBLITE_MIN_SHIFT_FRAMES,
                config.CLUBLITE_MAX_SHIFT_FRAMES,
                video_path,
            )
            return None

        m_max = float(np.max(raw_motion)) if len(raw_motion) else 0.0
        confidence = (
            float(raw_motion[off]) / m_max if m_max > 0.0 else 0.0
        )
        logger.info(
            "impact lowest-point refined: %d -> %d (delta=%+d, conf=%.2f) "
            "(video=%s)",
            impact.array_index,
            new_array_index,
            delta,
            confidence,
            video_path,
        )
        return new_array_index
    except Exception:  # noqa: BLE001 - 模块级硬约束：任何失败 -> None
        logger.exception(
            "impact lowest-point refine failed (video=%s)", video_path
        )
        return None
