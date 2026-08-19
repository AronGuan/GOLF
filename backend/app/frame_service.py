"""手动帧微调服务（ARCHITECTURE-v4-frameadjust.md §4）。

职责：校验任务状态与帧号范围 → 复用同源关键点 → 解码单帧像素 → 渲染 PNG。
供 ``main.py`` 的 ``GET /api/v1/task/{task_id}/frame/{frame_index}``
（+ 旧别名 ``/tasks/{task_id}/frame/{frame_index}``）调用。

错误模型：抛 :class:`FrameError`，由 main 层映射为统一错误包
（内部语义码 4001/4004/4009/5000 -> HTTP 400/404/409/500；对外 PDD 码）。

帧号规则：
- **clamp** 到 ``[0, total_frames-1]``（防止越界，-5 -> 0）；
- **范围限制**：与最近事件帧的距离须 ≤ ``config.FRAME_ADJUST_RANGE``（默认 30），
  超出抛 20003（帧号超出可调整范围）；
- **采样对齐**：降采样视频（``sample_step>1``）中间帧无关键点，快照到最近的有
  关键点的采样帧渲染，实际帧号经响应头 ``X-Frame-Index`` 回传前端。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

from . import config, frame_reader, renderer
from .landmark_cache import find_source_video, load_landmarks
from .schemas import CameraView, PhaseResult, SwingEvent, TaskStatus
from .task_store import task_store

logger = logging.getLogger(__name__)


class FrameError(Exception):
    """帧接口业务异常（main 层映射为统一错误包）。

    Args:
        code: 内部语义码（4001/4004/4009/5000），决定 HTTP 状态。
        message: 用户可见中文文案。
        pdd_code: 对外 PDD 码；``None`` 时回落为 ``code``。
    """

    def __init__(self, code: int, message: str, pdd_code: Optional[int] = None) -> None:
        self.code = code
        self.message = message
        self.pdd_code = pdd_code or code
        super().__init__(message)


def _resolve_task(task_id: str):
    """取任务并校验「存在 + 已完成」；失败抛 :class:`FrameError`。"""
    state = task_store.get(task_id)
    if state is None:
        raise FrameError(
            4004, "任务不存在或已过期", config.PDD_CODE_TASK_NOT_FOUND
        )
    if state.status is not TaskStatus.SUCCESS or state.result is None:
        raise FrameError(
            4009, "任务尚未完成", config.PDD_CODE_TASK_PENDING
        )
    return state


def _nearest_phase(
    phases, frame_index: int
) -> Optional[PhaseResult]:
    """取事件帧与 ``frame_index`` 最近的阶段（用于标签展示阶段号）。"""
    if not phases:
        return None
    return min(phases, key=lambda p: abs(int(p.frame_index) - frame_index))


def render_frame(task_id: str, frame_index: int) -> Tuple[bytes, int]:
    """渲染指定帧的骨架叠加图。

    Args:
        task_id: 任务 ID。
        frame_index: 原视频帧号（可越界，会 clamp）。

    Returns:
        ``(png_bytes, actual_frame_index)``；``actual_frame_index`` 是采样对齐后
        实际渲染的帧号（响应头 ``X-Frame-Index``）。

    Raises:
        FrameError: 任务不存在 / 未完成 / 帧号超出范围 / 数据不完整。
    """
    if not config.FRAME_ADJUST_ENABLED:
        raise FrameError(
            5000, "手动帧微调未启用", config.PDD_CODE_INTERNAL
        )

    state = _resolve_task(task_id)
    result = state.result
    if not state.out_dir:
        raise FrameError(5000, "任务数据异常", config.PDD_CODE_INTERNAL)

    total = int(result.video_meta.total_frames or result.video_meta.frame_count)
    if total <= 0:
        raise FrameError(5000, "任务数据异常", config.PDD_CODE_INTERNAL)

    requested = int(frame_index)
    clamped = max(0, min(total - 1, requested))

    # 范围限制：与最近事件帧的距离 ≤ FRAME_ADJUST_RANGE
    event_frames = [int(p.frame_index) for p in result.phases]
    if event_frames:
        nearest = min(event_frames, key=lambda f: abs(f - clamped))
        if abs(clamped - nearest) > config.FRAME_ADJUST_RANGE:
            raise FrameError(
                4001,
                f"帧号超出可调整范围（事件帧±{config.FRAME_ADJUST_RANGE}帧内）",
                config.PDD_CODE_FRAME_OUT_OF_RANGE,
            )

    # 复用同源关键点（与分析时完全一致），快照到最近的采样帧
    frames = load_landmarks(state.out_dir)
    if not frames:
        raise FrameError(
            5000, "缺少关键点缓存，任务数据不完整", config.PDD_CODE_INTERNAL
        )
    lm = min(frames, key=lambda f: abs(f.frame_index - clamped))

    # 解码单帧像素（来源：保留的 source.* 副本）
    source = find_source_video(state.out_dir)
    if source is None:
        raise FrameError(
            5000, "缺少源视频，任务数据不完整", config.PDD_CODE_INTERNAL
        )
    # EXIF 旋转贯穿：与 pipeline 主链路共享 meta.orientation，保证手动帧微调
    # 渲染的像素方向与事件帧图、MediaPipe 关键点完全一致。
    orientation = int(getattr(result.video_meta, "orientation", 0) or 0)
    decoded = frame_reader.grab_frames(str(source), [lm.frame_index], orientation=orientation)
    bgr = decoded.get(lm.frame_index)
    if bgr is None:
        raise FrameError(5000, "帧解码失败，请重试", config.PDD_CODE_INTERNAL)

    # 标签复用最近阶段的编号/键；帧号写实际渲染帧（与既有事件帧图风格一致）
    phase = _nearest_phase(result.phases, clamped)
    if phase is None:
        raise FrameError(5000, "任务数据异常", config.PDD_CODE_INTERNAL)
    fps = float(result.video_meta.fps or 1.0)
    event = SwingEvent(
        index=int(phase.index),
        key=phase.key,
        frame_index=lm.frame_index,
        timestamp=lm.frame_index / fps if fps > 0 else 0.0,
        estimated=False,
    )
    view: CameraView = result.camera_view or CameraView.FACE_ON

    png = renderer.render_frame_png(bgr, event, lm, view=view)
    return png, lm.frame_index
