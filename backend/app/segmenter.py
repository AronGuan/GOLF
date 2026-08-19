"""8 阶段切分算法（架构文档 §7）。

纯函数、无 IO，便于用真实视频批量调参。自测入口::

    E:/project/golf/.tools/python312/python.exe -m app.segmenter <video>

算法流程::

    build_signals -> _guard_no_swing
                  -> locate_top -> locate_address -> locate_impact -> locate_finish
                  -> locate_intermediate -> 单调性校正 -> _assemble
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import config, geometry
from .pose_extractor import moving_average, smooth_window
from .schemas import (
    AnalysisError,
    ErrorCode,
    FrameLandmarks,
    PHASE_META,
    PHASE_ORDER,
    PhaseKey,
    SwingEvent,
    SwingSignals,
)

logger = logging.getLogger(__name__)

#: 四锚点在 PHASE_ORDER 中的 key
_ANCHOR_KEYS = (PhaseKey.ADDRESS, PhaseKey.TOP, PhaseKey.IMPACT, PhaseKey.FINISH)

#: ⑦送杆局部最小搜索窗（秒）。击球后紧邻的腕最低点 = 送杆刚启动（30fps 下
#: ≈5 帧）。不取全窗 ``[i_impact, i_finish]`` 全局 argmin：送杆/收杆后期腕位
#: 会再次下探（实测 4e8d0d7e 全局最小在 impact+51、c6f67f38 在 +90、
#: 1446d1b9 在 +42），全局最小会把 ⑦ 甩到收杆前；短窗限在「杆身水平前一刻」。
_FOLLOW_MIN_WIN_SEC: float = 0.15


# ---------------------------------------------------------------------------
# S1~S8 信号构建
# ---------------------------------------------------------------------------


def _sample_step_of(frames: Sequence[FrameLandmarks]) -> int:
    """从帧号差还原降采样步长。"""
    if len(frames) < 2:
        return 1
    diffs = np.diff([f.frame_index for f in frames])
    positive = diffs[diffs > 0]
    if positive.size == 0:
        return 1
    return int(max(1, round(float(np.median(positive)))))


def build_signals(
    frames: Sequence[FrameLandmarks], fps: float, aspect: float = 1.0
) -> SwingSignals:
    """构建切分所需的一维信号（架构文档 §7.1）。

    Args:
        frames: :func:`pose_extractor.extract` 的产出。
        fps: 原视频帧率。
        aspect: 画幅纵横比 ``height / width``，用于把归一化坐标换算到**各向同性**
            的「图像宽度」单位。

            为什么必须传：``norm`` 的 x、y 都在 ``[0,1]``，但一个单位的 x 是
            ``W`` 像素、一个单位的 y 是 ``H`` 像素。竖屏 720×1280 下两者差
            1.78 倍，而本函数把「竖直行程」除以「近似水平的肩宽」来做归一化，
            不校正就等于所有肩宽制阈值随手机横竖屏漂移 1.78²≈3.2 倍
            （实测：竖屏样本 ``travel_in_S≈1.9~2.6``，横屏样本 ``≈3.1``，
            换算回真实像素后恰好反过来）。默认 ``1.0`` 保持历史行为，
            真实视频调用方**必须**传 ``meta.height / meta.width``。

    Raises:
        AnalysisError: ``NO_SWING`` —— 帧数过少或肩宽标尺异常。
    """
    n = len(frames)
    if n == 0:
        raise AnalysisError(ErrorCode.NO_SWING, "empty frame sequence")

    step = _sample_step_of(frames)
    dt = step / float(fps) if fps > 0 else 1.0 / 30.0

    raw = np.stack([f.norm for f in frames], axis=0).astype(np.float64)  # (n,33,3)
    ratio = float(aspect) if math.isfinite(float(aspect)) and aspect > 0 else 1.0
    # 只拉伸 y：换算后 x/y 同为「图像宽度」单位，各向同性
    norm = raw * np.array([1.0, ratio, 1.0], dtype=np.float64)

    # S3 肩宽标尺：全片中位数（各向同性图像坐标，单位=图像宽度）
    shoulder_vec = norm[:, geometry.L_SHOULDER, :2] - norm[:, geometry.R_SHOULDER, :2]
    widths = np.linalg.norm(shoulder_vec, axis=1)
    widths = widths[np.isfinite(widths)]
    scale = float(np.median(widths)) if widths.size else 0.0
    if not np.isfinite(scale) or scale < config.MIN_SHOULDER_SCALE:
        raise AnalysisError(ErrorCode.NO_SWING, f"illegal shoulder scale: {scale}")

    # S4/S5 平滑
    win = smooth_window(1.0 / dt)
    wrist_xy = moving_average(norm[:, geometry.L_WRIST, :2], win)
    wrist_x = wrist_xy[:, 0]
    wrist_y = wrist_xy[:, 1]
    shoulder_mid_y = moving_average(
        (norm[:, geometry.L_SHOULDER, 1] + norm[:, geometry.R_SHOULDER, 1]) / 2.0, win
    )
    hip_mid_y = moving_average(
        (norm[:, geometry.L_HIP, 1] + norm[:, geometry.R_HIP, 1]) / 2.0, win
    )

    # S6 高度信号（向上为正，肩宽归一化）
    h = (hip_mid_y - wrist_y) / scale

    # S7 速度：中心差分，两端用前/后向差分
    speed = _central_diff_speed(wrist_xy, dt) / scale
    # S8 速度再平滑
    speed = moving_average(speed, win)

    return SwingSignals(
        n=n,
        fps=float(fps),
        dt=float(dt),
        S=scale,
        wrist_x=wrist_x,
        wrist_y=wrist_y,
        shoulder_mid_y=shoulder_mid_y,
        hip_mid_y=hip_mid_y,
        h=h,
        speed=speed,
    )


def _central_diff_speed(points: np.ndarray, dt: float) -> np.ndarray:
    """对 ``(n,2)`` 轨迹求速率，单位 = 原坐标单位/秒。"""
    n = points.shape[0]
    if n == 1:
        return np.zeros(1, dtype=np.float64)
    velocity = np.zeros_like(points, dtype=np.float64)
    velocity[1:-1] = (points[2:] - points[:-2]) / (2.0 * dt)
    velocity[0] = (points[1] - points[0]) / dt
    velocity[-1] = (points[-1] - points[-2]) / dt
    return np.linalg.norm(velocity, axis=1)


# ---------------------------------------------------------------------------
# §7.6 前置 NO_SWING 判据 1~3
# ---------------------------------------------------------------------------


def _guard_no_swing(sig: SwingSignals) -> None:
    """前置判据：帧数 / 速度峰值 / 手腕垂直行程。"""
    fe = sig.fps_eff
    min_frames = max(10, int(round(0.5 * fe)))
    if sig.n < min_frames:
        raise AnalysisError(
            ErrorCode.NO_SWING, f"too few frames: {sig.n} < {min_frames}"
        )

    peak = float(np.nanmax(sig.speed)) if sig.n else 0.0
    if not np.isfinite(peak) or peak < config.V_PEAK_MIN:
        raise AnalysisError(ErrorCode.NO_SWING, f"speed peak too low: {peak:.3f}")

    travel = float(np.percentile(sig.wrist_y, 95) - np.min(sig.wrist_y))
    if travel < config.MIN_WRIST_TRAVEL * sig.S:
        raise AnalysisError(
            ErrorCode.NO_SWING,
            f"wrist travel too small: {travel:.4f} < {config.MIN_WRIST_TRAVEL * sig.S:.4f}",
        )


# ---------------------------------------------------------------------------
# §7.2 四锚点定位
# ---------------------------------------------------------------------------


def _runs_below(values: np.ndarray, threshold: float) -> List[Tuple[int, int]]:
    """返回所有满足 ``values < threshold`` 的连续段 ``[(start, end_inclusive), ...]``。"""
    runs: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for i, value in enumerate(values):
        below = bool(np.isfinite(value) and value < threshold)
        if below and start is None:
            start = i
        elif not below and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(values) - 1))
    return runs


def locate_top(sig: SwingSignals) -> int:
    """④ 顶点：手位相对髋部最高处附近的速度反向点。

    在 §7.2 基础上补了两条工程修正：

    1. **顶点必然早于全局速度峰（≈击球）**。很多球手收杆时手位与顶点等高甚至
       更高，只取「窗口内最高点」会把顶点误判到收杆，进而触发假 ``NO_SWING``；
       用速度峰收窄搜索上界即可消除该失效模式。
    2. 用 **髋部相对高度 ``h``** 取代绝对图像纵坐标 ``wrist_y``。绝对纵坐标会被
       人体整体平移与镜头晃动污染：实测样本 ``0bb16a97`` 中，站位阶段
       ``wrist_y=0.4905`` 反而比真实顶点 ``wrist_y=0.4948`` 更"高"（球手全程
       在画面内缓慢下移），顶点被定位到第 10 帧，直接误报 ``NO_SWING``；
       改用 ``h``（站位 0.4 / 真实顶点 1.07）后定位正确。``h`` 天然抵消平移。
    """
    n = sig.n
    lo = int(round(config.TOP_SEARCH_MARGIN * n))
    hi = int(round((1.0 - config.TOP_SEARCH_MARGIN) * n))
    lo = max(0, min(lo, n - 1))
    hi = max(lo + 1, min(hi, n))

    i_peak = int(np.argmax(sig.speed))
    min_span = max(2, int(round(config.MIN_TOP_ADDR_SEC * sig.fps_eff)))
    if i_peak - lo >= min_span:
        hi = max(lo + 1, min(hi, i_peak))

    i_y = lo + int(np.argmax(sig.h[lo:hi]))
    radius = max(1, int(round(config.TOP_REFINE_SEC * sig.fps_eff)))
    a = max(lo, i_y - radius)
    b = min(hi, i_y + radius + 1)
    if b <= a:
        return i_y
    return a + int(np.argmin(sig.speed[a:b]))


def locate_address(sig: SwingSignals, i_top: int) -> Tuple[int, bool]:
    """① 准备：顶点前最后一段静止的末帧。

    Raises:
        AnalysisError: ``NO_SWING`` —— 顶点离起点过近。
    """
    fe = sig.fps_eff
    if i_top <= 0:
        raise AnalysisError(ErrorCode.NO_SWING, "top at sequence head")

    segment = sig.speed[:i_top]
    min_len = max(2, int(round(config.STILL_MIN_SEC_ADDR * fe)))
    # 候选静止段：长度达标「且」末帧手位贴近髋线（低 h）。
    # 过滤掉顶点前减速微停——它发生在 h≈2（手已高举），会被 V_STILL 误判成静止，
    # 导致 Address 被定位到顶点前几帧、Address→Top 挤压成 1 帧触发假 NO_SWING。
    runs = [
        r
        for r in _runs_below(segment, config.V_STILL)
        if r[1] - r[0] + 1 >= min_len
        and float(sig.h[r[1]]) <= config.ADDR_H_MAX
    ]

    if runs:
        i_addr = runs[-1][1]
        estimated = False
    else:
        i_addr = int(np.argmin(segment))
        estimated = True

    min_gap = max(3, int(round(config.MIN_TOP_ADDR_SEC * fe)))
    if i_top - i_addr < min_gap:
        raise AnalysisError(
            ErrorCode.NO_SWING,
            f"address->top too short: {i_top - i_addr} < {min_gap}",
        )
    return i_addr, estimated


def locate_impact(sig: SwingSignals, i_top: int, i_addr: int) -> Tuple[int, bool]:
    """⑥ 击球：下杆窗口内「手位回落到 Address 高度」处的速度峰。

    相对 §7.2 的两处工程修正（均由真实视频实测反推）：

    1. **搜索区间被限制在物理可行的下杆时长内**（:data:`config.MAX_DOWNSWING_SEC`）。
       原实现在 ``(i_top, n)`` 全区间找首个高度回落点，实测样本 ``470057ac``
       因顶点后手位一直高于容差带，首个"回落"发生在 95 帧之后 —— 算出 3.17s
       的下杆，比真实值大一个数量级。
    2. **高度判据改用髋部相对高度 ``h``**，理由同 :func:`locate_top`：绝对
       ``wrist_y`` 会被人体整体平移污染。
    3. 窗口内**没有**回落穿越时回退到速度峰。实测 9 段视频中速度峰分支给出的
       下杆时长全部落在 0.20~0.30s（真实值 0.25~0.30s），可靠性显著高于
       高度穿越分支，故窗口收紧后二者结论一致。

    Raises:
        AnalysisError: ``NO_SWING`` —— 下杆时长异常。
    """
    fe = sig.fps_eff
    n = sig.n
    if i_top >= n - 1:
        raise AnalysisError(ErrorCode.NO_SWING, "top at sequence tail")

    min_gap = max(2, int(round(config.MIN_IMPACT_TOP_SEC * fe)))
    # 下杆搜索上界：物理上限，且至少要容纳 min_gap 帧
    span = max(min_gap + 1, int(round(config.MAX_DOWNSWING_SEC * fe)))
    hi = min(n, i_top + 1 + span)
    if hi <= i_top + 1:
        hi = min(n, i_top + 2)

    h_addr = float(sig.h[i_addr])
    window = sig.h[i_top + 1 : hi]
    crossed = np.where(window <= h_addr + config.IMPACT_Y_TOL)[0]

    if crossed.size > 0:
        i_cross = int(i_top + 1 + crossed[0])
        radius = max(1, int(round(config.IMPACT_WIN_SEC * fe)))
        a = max(i_top + 1, i_cross - radius)
        b = min(hi, i_cross + radius + 1)
        if b <= a:
            b = min(hi, a + 1)
        i_impact = a + int(np.argmax(sig.speed[a:b]))
        estimated = False
    else:
        i_impact = i_top + 1 + int(np.argmax(sig.speed[i_top + 1 : hi]))
        estimated = True

    if i_impact - i_top < min_gap:
        raise AnalysisError(
            ErrorCode.NO_SWING,
            f"top->impact too short: {i_impact - i_top} < {min_gap}",
        )
    return i_impact, estimated


def locate_finish(sig: SwingSignals, i_impact: int) -> Tuple[int, bool]:
    """⑧ 收杆：击球后首段静止的首帧。"""
    fe = sig.fps_eff
    n = sig.n
    if i_impact >= n - 1:
        return n - 1, True

    segment = sig.speed[i_impact + 1 : n]
    min_len = max(2, int(round(config.STILL_MIN_SEC_FINISH * fe)))
    runs = [r for r in _runs_below(segment, config.V_STILL) if r[1] - r[0] + 1 >= min_len]
    if runs:
        return i_impact + 1 + runs[0][0], False

    if (n - 1) - i_impact >= int(round(config.FINISH_FALLBACK_SEC * fe)):
        return i_impact + 1 + int(np.argmin(segment)), True

    return n - 1, True


# ---------------------------------------------------------------------------
# §7.3 中间四帧插值定位
# ---------------------------------------------------------------------------


def _first_true(mask: np.ndarray, offset: int) -> Optional[int]:
    """返回 ``mask`` 中首个 True 的全局下标。"""
    hits = np.where(mask)[0]
    return int(offset + hits[0]) if hits.size > 0 else None


def _first_rising_cross(
    values: np.ndarray, threshold: float, offset: int
) -> Optional[int]:
    """首个「先低于阈值、再上穿阈值」的全局下标；未发生真实上穿则返回 None。

    为什么不直接用 ``values >= threshold``：站位帧手腕本就在髋线附近，
    直接取「首个不低于阈值」会让 ② 起杆退化成 ``i_addr + 1``（旧 ⑦ 送杆
    判据同理；2026-08 起 ⑦ 已改用 ``h`` 局部最小点，不再调用本函数）。
    """
    below_seen = False
    for i, value in enumerate(values):
        if not np.isfinite(value):
            continue
        if value < threshold:
            below_seen = True
        elif below_seen:
            return offset + i
    return None


def _first_falling_cross(
    values: np.ndarray, threshold: float, offset: int
) -> Optional[int]:
    """首个「先高于阈值、再下穿阈值」的全局下标；未发生真实下穿则返回 None。

    与 :func:`_first_rising_cross` 方向相反：用于下杆等**从上方降到阈值以下**
    的穿越（方案 A ⑤ 判据）。返回的是**首个满足 ``value <= threshold`` 且此前
    出现过 ``value > threshold``** 的下标（含恰好等于阈值的帧）。
    """
    above_seen = False
    for i, value in enumerate(values):
        if not np.isfinite(value):
            continue
        if value > threshold:
            above_seen = True
        elif above_seen:
            return offset + i
    return None


def _ratio_frame(start: int, end: int, ratio: float) -> int:
    """按比例在 ``[start, end]`` 内取帧。"""
    return int(round(start + ratio * (end - start)))


def locate_intermediate(
    sig: SwingSignals, anchors: Tuple[int, int, int, int]
) -> Dict[PhaseKey, Tuple[int, bool]]:
    """②③⑤⑦：解剖高度判据 + 兜底比例，未命中走兜底。

    - ② 起杆：手腕上穿髋线（:func:`_first_rising_cross`，阈值
      :data:`config.H_HIP`）。
    - ③ 上杆：手腕首次升过肩线（``wrist_y <= shoulder_mid_y``）。
    - ⑤ 下杆（方案 A，2026-08 用户拍板）：手腕高度 ``h`` 首次**下穿**髋线
      （:func:`_first_falling_cross`，阈值 :data:`config.H_HIP`）；相对旧判据
      「腕降肩」更靠后，且带 ⑤/⑥ 间距守卫（⑤ 必须严格早于 ⑥）。
    - ⑦ 送杆（2026-08 用户拍板）：``h`` 在击球后短窗 ``(i_impact, i_impact+W]``
      内的**局部最小值**（``h`` 最小 = 杆头最低 = 杆身水平前一刻 = 送杆刚启动，
      ``W`` 见 :data:`_FOLLOW_MIN_WIN_SEC`）；窗口从 ``i_impact + 1`` 起搜，
      天然保证 ``⑦ >= impact + 1``，未命中走兜底比例。

    Args:
        sig: 信号包。
        anchors: ``(i_addr, i_top, i_impact, i_finish)``。
    """
    i_addr, i_top, i_impact, i_finish = anchors
    r2, r3, r5, r7 = config.FALLBACK_RATIO
    out: Dict[PhaseKey, Tuple[int, bool]] = {}

    anchor_only = config.ANCHOR_ONLY_MODE

    # ② 起杆：手腕首次上穿髋线
    idx: Optional[int] = None
    if not anchor_only and i_top > i_addr:
        idx = _first_rising_cross(sig.h[i_addr : i_top + 1], config.H_HIP, i_addr)
    if idx is None:
        out[PhaseKey.TAKEAWAY] = (_ratio_frame(i_addr, i_top, r2), True)
    else:
        out[PhaseKey.TAKEAWAY] = (idx, False)
    i_take = out[PhaseKey.TAKEAWAY][0]

    # ③ 上杆：手腕首次升过肩线（从 ② 之后开始搜，防倒序）
    idx = None
    if not anchor_only and i_top > i_take:
        window = slice(i_take, i_top + 1)
        idx = _first_true(sig.wrist_y[window] <= sig.shoulder_mid_y[window], i_take)
    if idx is None:
        out[PhaseKey.BACKSWING] = (_ratio_frame(i_addr, i_top, r3), True)
    else:
        out[PhaseKey.BACKSWING] = (idx, False)

    # ⑤ 下杆：手腕首次回落到髋线（方案 A，2026-08 用户拍板；原判据为
    # 「手腕回落穿过肩线」——实测偏早 2 帧，如 22030124 得 111 而视觉为 113）。
    # 语义：``h = (hip_mid_y - wrist_y)/S`` 向上为正；顶点时腕在髋上
    # （``h≈2``），下杆期 ``h`` 单调递减，**首次下穿 ``H_HIP``** 即「腕降到髋」。
    # 相比旧判据（``wrist_y`` 下穿肩线）更靠后，更接近击球。
    # ⚠️ ⑤/⑥ 间距守卫：⑤ 必须严格早于 ⑥（间隔 ≥ 1 帧），否则说明判据未在
    # 击球前真正命中（窗口内 ``h`` 未降到髋线），回退兜底比例——避免 ⑤≥⑥
    # 触发 :func:`enforce_monotonic_indices` 把 impact 锚点向前挤（降级污染）。
    idx = None
    if not anchor_only and i_impact > i_top:
        window = slice(i_top, i_impact + 1)
        idx = _first_falling_cross(sig.h[window], config.H_HIP, i_top)
        if idx is not None and idx >= i_impact:
            idx = None
    if idx is None:
        out[PhaseKey.DOWNSWING] = (_ratio_frame(i_top, i_impact, r5), True)
    else:
        out[PhaseKey.DOWNSWING] = (idx, False)

    # ⑦ 送杆（2026-08 用户拍板）：杆身刚到水平时。旧判据「腕升髋线」
    # （``_first_rising_cross``）在多数 DTL 样本上腕位始终高于髋线、命中不了
    # 真实上穿，退化成兜底比例（实测 4e8d0d7e=267、c6f67f38=224、1446d1b9=58，
    # 远离击球）；新判据取 ``h`` 在击球后短窗 ``(i_impact, i_impact + W]`` 内的
    # **局部最小值**——``h`` 最小 = 杆头最低 = 击球瞬间 ~ 杆身水平**前一刻** =
    # 送杆刚启动。
    # ⚠️ 为什么是短窗而非全窗 ``[i_impact, i_finish]`` 全局 argmin：
    #  1. 窗口从 ``i_impact + 1`` 开始——``h`` 全局最小值恰在击球帧（腕最低 =
    #     击球瞬间；实测 22030124 的 ``h`` 最小值就在 refined impact 115），若包含
    #     ``i_impact`` 会命中 ⑦=⑥ 破坏单调性；从击球后一帧起搜保证
    #     ``⑦ >= impact + 1``（无需 :func:`enforce_monotonic_indices` 强排）。
    #  2. 不上限收尾——送杆/收杆后期腕位会再次下探（实测 4e8d0d7e 全局最小在
    #     impact+51、c6f67f38 在 +90、1446d1b9 在 +42），全局 argmin 会把 ⑦
    #     甩到收杆前；短窗把搜索限制在「击球后紧邻的腕最低点」= 送杆刚启动。
    idx = None
    if not anchor_only and i_finish > i_impact:
        w = max(2, int(round(_FOLLOW_MIN_WIN_SEC * sig.fps_eff)))
        window_h = sig.h[i_impact + 1 : min(i_finish + 1, i_impact + 1 + w)]
        if len(window_h) > 0:
            idx = i_impact + 1 + int(np.argmin(window_h))
    if idx is None:
        out[PhaseKey.FOLLOW_THROUGH] = (_ratio_frame(i_impact, i_finish, r7), True)
    else:
        out[PhaseKey.FOLLOW_THROUGH] = (idx, False)

    return out


# ---------------------------------------------------------------------------
# §7.4 单调性校正
# ---------------------------------------------------------------------------


def enforce_monotonic_indices(
    indices: List[int], estimated: List[bool], n: int
) -> Tuple[List[int], List[bool]]:
    """在**数组下标空间**保证严格递增且落在 ``[0, n-1]``。

    Raises:
        AnalysisError: ``NO_SWING`` —— 反向挤压后仍冲突。
    """
    idx = list(indices)
    est = list(estimated)

    idx[0] = max(0, min(idx[0], n - 1))
    for k in range(1, len(idx)):
        if idx[k] <= idx[k - 1]:
            idx[k] = idx[k - 1] + 1
            est[k] = True

    if idx[-1] > n - 1:
        idx[-1] = n - 1
        est[-1] = True
        for k in range(len(idx) - 2, -1, -1):
            if idx[k] >= idx[k + 1]:
                idx[k] = idx[k + 1] - 1
                est[k] = True
        if idx[0] < 0:
            raise AnalysisError(
                ErrorCode.NO_SWING, "cannot fit 8 monotonic events into sequence"
            )

    for k in range(1, len(idx)):
        if idx[k] <= idx[k - 1]:
            raise AnalysisError(ErrorCode.NO_SWING, "event order conflict after squeeze")
    return idx, est


def enforce_monotonic(events: List[SwingEvent]) -> List[SwingEvent]:
    """对已组装的事件做最终校验：``frame_index`` 必须严格递增。

    Raises:
        AnalysisError: ``NO_SWING``。
    """
    ordered = sorted(events, key=lambda e: e.index)
    for k in range(1, len(ordered)):
        if ordered[k].frame_index <= ordered[k - 1].frame_index:
            raise AnalysisError(
                ErrorCode.NO_SWING,
                f"non-monotonic frames at phase {ordered[k].index}",
            )
    return ordered


# ---------------------------------------------------------------------------
# 组装与主入口
# ---------------------------------------------------------------------------


def _assemble(
    frames: Sequence[FrameLandmarks],
    indices: Sequence[int],
    estimated: Sequence[bool],
) -> List[SwingEvent]:
    """把数组下标映射回原视频帧号，生成 8 个 :class:`SwingEvent`。"""
    events: List[SwingEvent] = []
    for order, key in enumerate(PHASE_ORDER):
        i = int(indices[order])
        frame = frames[i]
        events.append(
            SwingEvent(
                index=PHASE_META[key].index,
                key=key,
                frame_index=int(frame.frame_index),
                timestamp=round(float(frame.timestamp), 3),
                estimated=bool(estimated[order]),
                array_index=i,
            )
        )
    return events


def segment_swing(
    frames: Sequence[FrameLandmarks],
    fps: float,
    sig: Optional[SwingSignals] = None,
    aspect: float = 1.0,
) -> List[SwingEvent]:
    """8 阶段切分主入口。

    Args:
        frames: :func:`pose_extractor.extract` 的产出。
        fps: 原视频帧率。
        sig: 可选的预构建信号包（避免重复计算）。传入时 ``aspect`` 被忽略。
        aspect: 画幅纵横比 ``height / width``，含义见 :func:`build_signals`。

    Returns:
        恒定 8 个、帧号严格递增的 :class:`SwingEvent`。

    Raises:
        AnalysisError: ``NO_SWING``。
    """
    signals = sig if sig is not None else build_signals(frames, fps, aspect=aspect)
    _guard_no_swing(signals)

    i_top = locate_top(signals)
    i_addr, e_addr = locate_address(signals, i_top)
    i_impact, e_impact = locate_impact(signals, i_top, i_addr)
    i_finish, e_finish = locate_finish(signals, i_impact)
    mid = locate_intermediate(signals, (i_addr, i_top, i_impact, i_finish))

    ordered_pairs: List[Tuple[int, bool]] = [
        (i_addr, e_addr),
        mid[PhaseKey.TAKEAWAY],
        mid[PhaseKey.BACKSWING],
        (i_top, False),
        mid[PhaseKey.DOWNSWING],
        (i_impact, e_impact),
        mid[PhaseKey.FOLLOW_THROUGH],
        (i_finish, e_finish),
    ]
    indices = [p[0] for p in ordered_pairs]
    estimated = [p[1] for p in ordered_pairs]
    indices, estimated = enforce_monotonic_indices(indices, estimated, signals.n)

    events = _assemble(frames, indices, estimated)
    return enforce_monotonic(events)


def reanchor_impact(
    frames: Sequence[FrameLandmarks],
    signals: SwingSignals,
    events: Sequence[SwingEvent],
    new_impact_array_index: int,
) -> Optional[List[SwingEvent]]:
    """用校正后的击球帧重建 8 事件（ARCHITECTURE-v3-clublite.md §4.2）。

    校正只移动 impact 事件帧本身；为保持 8 阶段语义，需要以新 impact 为边界
    重跑 ②③⑤⑦（:func:`locate_intermediate`），再做单调性校正后重建事件。

    流程：
    1. 用新 impact 替换旧 impact（``estimated=False``，有真实杆头/球证据）；
    2. 重跑 :func:`locate_intermediate`（②③⑤⑦ 依赖 impact 边界）→ 新中间四帧；
    3. :func:`enforce_monotonic_indices` + :func:`_assemble` 重建事件；
    4. 任何冲突（:class:`AnalysisError` / 下标非法）→ 返回 ``None``，
       调用方保持原 events（保守降级，绝不因校正破坏主链路）。

    Args:
        frames: 姿态提取产出。
        signals: 切分信号包（与 :func:`segment_swing` 同源）。
        events: 8 事件（原 ``segment_swing`` 产出）。
        new_impact_array_index: 校正后的 impact 数组下标（array 下标）。

    Returns:
        重建后的 8 事件；冲突时返回 ``None``。

    Note:
        本函数是**纯函数、无 IO**，且**不改动** :func:`locate_impact`
        （349 个既有测试覆盖的粗定位保持不变，校正作为后置精修叠加）。
    """
    try:
        by_key = {e.key: e for e in events}
        addr = by_key[PhaseKey.ADDRESS]
        top = by_key[PhaseKey.TOP]
        impact = by_key[PhaseKey.IMPACT]
        finish = by_key[PhaseKey.FINISH]

        new_idx = int(new_impact_array_index)
        if new_idx < 0 or new_idx >= signals.n:
            logger.warning(
                "reanchor_impact: new impact out of range %d (n=%d)", new_idx, signals.n
            )
            return None
        # 物理边界守卫：impact 必须严格在 top 与 finish 之间
        if new_idx <= top.array_index or new_idx >= finish.array_index:
            logger.warning(
                "reanchor_impact: new impact %d not in (top=%d, finish=%d)",
                new_idx,
                top.array_index,
                finish.array_index,
            )
            return None
        if new_idx == impact.array_index:
            # 无实际移动：直接返回原 events（防御，正常流程不会走到）
            return list(events)

        mid = locate_intermediate(
            signals, (addr.array_index, top.array_index, new_idx, finish.array_index)
        )
        ordered_pairs: List[Tuple[int, bool]] = [
            (addr.array_index, addr.estimated),
            mid[PhaseKey.TAKEAWAY],
            mid[PhaseKey.BACKSWING],
            (top.array_index, False),
            mid[PhaseKey.DOWNSWING],
            (new_idx, False),  # 校正后有真实杆头/球证据
            mid[PhaseKey.FOLLOW_THROUGH],
            (finish.array_index, finish.estimated),
        ]
        indices = [p[0] for p in ordered_pairs]
        estimated = [p[1] for p in ordered_pairs]
        indices, estimated = enforce_monotonic_indices(indices, estimated, signals.n)
        rebuilt = _assemble(frames, indices, estimated)
        return enforce_monotonic(rebuilt)
    except AnalysisError:
        logger.warning("reanchor_impact: monotonic conflict, keeping original events")
        return None
    except (KeyError, IndexError, TypeError, ValueError):
        logger.warning("reanchor_impact: invalid input, keeping original events")
        return None


# ---------------------------------------------------------------------------
# CLI 自测入口
# ---------------------------------------------------------------------------


def _cli(argv: Sequence[str]) -> int:
    """``python -m app.segmenter <video>`` 自测。"""
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format=config.LOG_FORMAT)

    if len(argv) < 2:
        print("usage: python -m app.segmenter <video-path>")
        return 2

    from .pose_extractor import check_brightness, extract, probe_video

    path = argv[1]
    try:
        meta = probe_video(path)
        print(
            f"[meta] fps={meta.fps} duration={meta.duration}s "
            f"{meta.width}x{meta.height} frames={meta.frame_count} "
            f"step={meta.sample_step} low_fps={meta.low_fps}"
        )
        check_brightness(path)
        frames = extract(path, meta)
        detected = sum(1 for f in frames if f.detected)
        avg_vis = float(
            np.mean([np.mean(f.visibility[list(geometry.CORE_IDS)]) for f in frames])
        )
        print(
            f"[extract] sampled={len(frames)} detected={detected} "
            f"miss_ratio={1 - detected / max(1, len(frames)):.3f} avg_core_vis={avg_vis:.3f}"
        )

        aspect = meta.height / meta.width if meta.width > 0 else 1.0
        signals = build_signals(frames, meta.fps, aspect=aspect)
        print(
            f"[signals] n={signals.n} S={signals.S:.4f} dt={signals.dt:.4f} "
            f"speed_max={float(np.max(signals.speed)):.3f} aspect={aspect:.3f}"
        )

        events = segment_swing(frames, meta.fps, sig=signals)
        print("[events]")
        for event in events:
            meta_info = PHASE_META[event.key]
            print(
                f"  {event.index}  {event.key.value:<15s} {meta_info.name_cn:<3s} "
                f"frame={event.frame_index:<5d} t={event.timestamp:.3f}s "
                f"estimated={event.estimated}"
            )
        return 0
    except AnalysisError as exc:
        print(f"[AnalysisError] {exc.code.value}: {exc.detail}")
        print(f"[user message] {config.error_message(exc.code.value)}")
        return 1


def run_cli(video_path: str) -> int:
    """公开的单视频自检入口（供 ``run.py segment <video>`` 复用）。

    Args:
        video_path: 视频文件路径。

    Returns:
        0 表示切分成功；1 表示业务异常（NO_PERSON / NO_SWING 等）。
    """
    return _cli(["app.segmenter", video_path])


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_cli(sys.argv))
