"""视频探测与 MediaPipe 逐帧姿态提取。

**硬约束**：只使用 ``mediapipe==0.10.14`` 的 legacy API ``mp.solutions.pose``。
严禁使用 ``mediapipe.tasks`` / ``PoseLandmarker`` / 下载 ``.task`` 模型。
"""

from __future__ import annotations

import logging
import math
from typing import Callable, List, Optional

import cv2
import numpy as np

from . import config, geometry
from .schemas import AnalysisError, ErrorCode, FrameLandmarks, VideoMeta

logger = logging.getLogger(__name__)

#: 进度回调类型：入参为 0.0~1.0 的完成比例
ProgressCb = Optional[Callable[[float], None]]

_mp_pose = None  # 延迟导入，避免 CLI/单测在无需推理时也加载 mediapipe


def _pose_module():
    """延迟导入 ``mediapipe.solutions.pose``。"""
    global _mp_pose
    if _mp_pose is None:
        import mediapipe as mp  # noqa: WPS433 - 延迟导入是刻意设计

        _mp_pose = mp.solutions.pose
    return _mp_pose


# ---------------------------------------------------------------------------
# 视频探测
# ---------------------------------------------------------------------------


def probe_video(path: str) -> VideoMeta:
    """读取视频元信息并做合法性校验。

    Raises:
        AnalysisError: ``BAD_VIDEO`` —— 无法打开 / fps 或帧数非法 / 时长越界。
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise AnalysisError(ErrorCode.BAD_VIDEO, f"cannot open video: {path}")

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()

    if fps <= 0.0 or not math.isfinite(fps):
        raise AnalysisError(ErrorCode.BAD_VIDEO, f"illegal fps: {fps}")
    if frame_count <= 0 or width <= 0 or height <= 0:
        raise AnalysisError(
            ErrorCode.BAD_VIDEO,
            f"illegal geometry: frames={frame_count} size={width}x{height}",
        )

    duration = frame_count / fps
    if duration < config.MIN_DURATION_SEC or duration > config.MAX_DURATION_SEC:
        raise AnalysisError(
            ErrorCode.BAD_VIDEO, f"duration out of range: {duration:.2f}s"
        )

    sample_step = max(1, math.ceil(frame_count / config.MAX_INFER_FRAMES))
    return VideoMeta(
        fps=round(fps, 3),
        duration=round(duration, 3),
        width=width,
        height=height,
        frame_count=frame_count,
        sample_step=sample_step,
        low_fps=fps < config.LOW_FPS_THRESHOLD,
    )


def check_brightness(path: str) -> None:
    """等间隔抽帧做灰度均值探测。

    Raises:
        AnalysisError: ``TOO_DARK`` —— 平均灰度低于阈值。
        AnalysisError: ``BAD_VIDEO`` —— 一帧也读不出来。
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise AnalysisError(ErrorCode.BAD_VIDEO, f"cannot open video: {path}")

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    samples = max(1, min(config.BRIGHTNESS_SAMPLE_FRAMES, total or 1))
    means: List[float] = []
    for k in range(samples):
        pos = int(total * (k + 0.5) / samples) if total > 0 else k
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        means.append(float(np.mean(gray)))
    cap.release()

    if not means:
        raise AnalysisError(ErrorCode.BAD_VIDEO, "no decodable frame for brightness")

    avg = float(np.mean(means))
    logger.info("brightness mean=%.1f over %d samples", avg, len(means))
    if avg < config.DARK_MEAN_THRESHOLD:
        raise AnalysisError(ErrorCode.TOO_DARK, f"mean gray {avg:.1f}")


# ---------------------------------------------------------------------------
# 姿态提取
# ---------------------------------------------------------------------------


def _resize_short_side(image: np.ndarray, short_side: int) -> np.ndarray:
    """等比缩放，使短边不超过 ``short_side``。"""
    h, w = image.shape[:2]
    cur = min(h, w)
    if cur <= short_side:
        return image
    ratio = short_side / float(cur)
    return cv2.resize(
        image,
        (max(1, int(round(w * ratio))), max(1, int(round(h * ratio)))),
        interpolation=cv2.INTER_AREA,
    )


def smooth_window(fps_eff: float) -> int:
    """按 :data:`config.SMOOTH_WIN_SEC` 计算奇数滑动平均窗口。"""
    win = int(round(fps_eff * config.SMOOTH_WIN_SEC))
    if win % 2 == 0:
        win += 1
    return max(3, win)


def moving_average(arr: np.ndarray, window: int) -> np.ndarray:
    """沿 axis=0 的滑动平均，首尾用边缘值填充后再卷积（避免边缘塌陷）。"""
    if window <= 1 or arr.shape[0] <= 2:
        return np.asarray(arr, dtype=np.float64).copy()
    data = np.asarray(arr, dtype=np.float64)
    pad = window // 2
    original_shape = data.shape
    flat = data.reshape(original_shape[0], -1)
    padded = np.pad(flat, ((pad, pad), (0, 0)), mode="edge")
    kernel = np.ones(window, dtype=np.float64) / float(window)
    out = np.empty_like(flat)
    for col in range(flat.shape[1]):
        out[:, col] = np.convolve(padded[:, col], kernel, mode="valid")
    return out.reshape(original_shape)


def _interpolate_missing(frames: List[FrameLandmarks]) -> None:
    """对 NaN 段按帧号线性插值；首尾用最近有效帧外推（原地修改）。"""
    n = len(frames)
    if n == 0:
        return
    valid = np.array([f.detected for f in frames], dtype=bool)
    if not valid.any():
        return
    xs = np.arange(n, dtype=np.float64)
    valid_x = xs[valid]

    for attr in ("norm", "world", "visibility"):
        stack = np.stack([getattr(f, attr) for f in frames], axis=0).astype(np.float64)
        shape = stack.shape
        flat = stack.reshape(n, -1)
        for col in range(flat.shape[1]):
            series = flat[:, col]
            good = valid & np.isfinite(series)
            if not good.any():
                flat[:, col] = 0.0
                continue
            flat[:, col] = np.interp(xs, xs[good], series[good])
        stack = flat.reshape(shape)
        for i, frame in enumerate(frames):
            setattr(frame, attr, stack[i])
    _ = valid_x  # 保留可读性：插值锚点即为 valid_x


def _smooth(frames: List[FrameLandmarks], window: int) -> None:
    """对 ``norm`` / ``world`` 做滑动平均（原地修改）。"""
    if len(frames) < 3 or window <= 1:
        return
    for attr in ("norm", "world"):
        stack = np.stack([getattr(f, attr) for f in frames], axis=0)
        smoothed = moving_average(stack, window)
        for i, frame in enumerate(frames):
            setattr(frame, attr, smoothed[i])


def _empty_landmarks() -> np.ndarray:
    """返回 ``(33, 3)`` 的 NaN 数组。"""
    return np.full((geometry.NUM_LANDMARKS, 3), np.nan, dtype=np.float64)


def extract(
    path: str,
    meta: VideoMeta,
    on_progress: ProgressCb = None,
) -> List[FrameLandmarks]:
    """逐帧提取 33 关键点。

    Args:
        path: 视频路径。
        meta: :func:`probe_video` 的产出。
        on_progress: 进度回调，入参为 0.0~1.0。

    Returns:
        采样后的 :class:`FrameLandmarks` 列表，``frame_index`` 已还原为原视频帧号。

    Raises:
        AnalysisError: ``BAD_VIDEO`` / ``NO_PERSON`` / ``LOW_QUALITY``。
    """
    pose_module = _pose_module()
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise AnalysisError(ErrorCode.BAD_VIDEO, f"cannot open video: {path}")

    step = max(1, int(meta.sample_step))
    expected = max(1, math.ceil(meta.frame_count / step))
    frames: List[FrameLandmarks] = []
    missing = 0
    low_vis_frames = 0
    last_reported = -1.0

    try:
        with pose_module.Pose(**config.POSE_KW) as pose:
            raw_index = 0
            while True:
                grabbed = cap.grab()
                if not grabbed:
                    break
                if raw_index % step != 0:
                    raw_index += 1
                    continue
                ok, bgr = cap.retrieve()
                if not ok or bgr is None:
                    raw_index += 1
                    continue

                small = _resize_short_side(bgr, config.INFER_SHORT_SIDE)
                rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                rgb.flags.writeable = False
                result = pose.process(rgb)

                norm = _empty_landmarks()
                world = _empty_landmarks()
                visibility = np.full(geometry.NUM_LANDMARKS, np.nan, dtype=np.float64)
                detected = result.pose_landmarks is not None

                if detected:
                    for i, lm in enumerate(result.pose_landmarks.landmark):
                        if i >= geometry.NUM_LANDMARKS:
                            break
                        norm[i] = (lm.x, lm.y, lm.z)
                        visibility[i] = lm.visibility
                    if result.pose_world_landmarks is not None:
                        for i, lm in enumerate(result.pose_world_landmarks.landmark):
                            if i >= geometry.NUM_LANDMARKS:
                                break
                            world[i] = (lm.x, lm.y, lm.z)
                    else:
                        world = norm.copy()
                    core_vis = float(
                        np.nanmean(visibility[list(geometry.CORE_IDS)])
                        if np.isfinite(visibility[list(geometry.CORE_IDS)]).any()
                        else 0.0
                    )
                    if core_vis < config.LOW_VIS_THRESHOLD:
                        low_vis_frames += 1
                else:
                    missing += 1

                frames.append(
                    FrameLandmarks(
                        frame_index=raw_index,
                        timestamp=raw_index / meta.fps,
                        detected=detected,
                        norm=norm,
                        world=world,
                        visibility=np.nan_to_num(visibility, nan=0.0),
                    )
                )

                if on_progress is not None:
                    ratio = len(frames) / float(expected)
                    if ratio - last_reported >= 0.02:
                        last_reported = ratio
                        on_progress(min(1.0, ratio))
                raw_index += 1
    finally:
        cap.release()

    if on_progress is not None:
        on_progress(1.0)

    total = len(frames)
    if total == 0:
        raise AnalysisError(ErrorCode.BAD_VIDEO, "no frame decoded")

    miss_ratio = missing / float(total)
    low_vis_ratio = low_vis_frames / float(total)
    logger.info(
        "extract done: frames=%d miss=%.3f low_vis=%.3f", total, miss_ratio, low_vis_ratio
    )

    if miss_ratio > config.MISS_RATIO_NO_PERSON:
        raise AnalysisError(ErrorCode.NO_PERSON, f"miss_ratio={miss_ratio:.3f}")
    if miss_ratio > config.MISS_RATIO_LOW_QUALITY:
        raise AnalysisError(ErrorCode.LOW_QUALITY, f"miss_ratio={miss_ratio:.3f}")
    if low_vis_ratio > config.LOW_VIS_FRAME_RATIO:
        raise AnalysisError(ErrorCode.LOW_QUALITY, f"low_vis_ratio={low_vis_ratio:.3f}")

    _interpolate_missing(frames)
    _smooth(frames, smooth_window(meta.fps / step))
    return frames
