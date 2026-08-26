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
from typing import List, Optional, Tuple

from . import config, frame_reader, metrics, renderer, segmenter
from .landmark_cache import find_source_video, load_landmarks
from .schemas import (
    CameraView,
    PhaseKey,
    PhaseResult,
    StageMetric,
    SwingEvent,
    TaskStatus,
)
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


def _resolve_adjusted_frame(result, requested: int) -> int:
    """clamp + 范围限制（``render_frame`` 与新指标接口共用）。

    Args:
        result: 已完成任务的 :class:`AnalysisResult`。
        requested: 原视频帧号（可越界，会 clamp）。

    Returns:
        clamp 到 ``[0, total_frames-1]`` 且与最近事件帧距离 ≤
        ``config.FRAME_ADJUST_RANGE`` 的帧号。

    Raises:
        FrameError: 数据不完整（5000）/ 帧号超出可调整范围（20003）。
    """
    total = int(result.video_meta.total_frames or result.video_meta.frame_count)
    if total <= 0:
        raise FrameError(5000, "任务数据异常", config.PDD_CODE_INTERNAL)

    clamped = max(0, min(total - 1, int(requested)))

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
    return clamped


def _parse_phase(raw: str) -> Optional[PhaseKey]:
    """解析阶段标识：接受 PhaseKey 值（``downswing``）或枚举名（``DOWNSWING``）。"""
    if not raw:
        return None
    value = str(raw).strip().lower()
    for key in PhaseKey:
        if key.value == value or key.name.lower() == value:
            return key
    return None


def _reconstruct_events(result, frames) -> List[SwingEvent]:
    """从 ``AnalysisResult.phases`` + 采样帧序列重建 ``SwingEvent``。

    ``PhaseResult`` 不含 ``array_index``，而 ``build_context`` 需要它来反查定格帧。
    这里用 ``frame_index`` 精确映射回 ``frames`` 下标（事件帧必为采样帧）；极端
    情况下帧号未精确命中时退化为最近采样帧，保证不越界。
    """
    frame_index_to_array = {int(f.frame_index): i for i, f in enumerate(frames)}
    events: List[SwingEvent] = []
    for phase in result.phases:
        array_index = frame_index_to_array.get(int(phase.frame_index))
        if array_index is None:
            array_index = min(
                range(len(frames)),
                key=lambda i: abs(frames[i].frame_index - int(phase.frame_index)),
            )
        events.append(
            SwingEvent(
                index=int(phase.index),
                key=phase.key,
                frame_index=int(phase.frame_index),
                timestamp=float(phase.timestamp),
                estimated=bool(phase.estimated),
                array_index=array_index,
            )
        )
    return events


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

    clamped = _resolve_adjusted_frame(result, frame_index)

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


def phase_metrics(
    task_id: str, phase: str, frame_index: int
) -> Tuple[PhaseKey, int, List[StageMetric]]:
    """手动微调时实时重算目标阶段指标（纯增量，不改核心算法）。

    复用 ``metrics.build_context`` + ``metrics.compute_phase_metrics``，通过
    **克隆 events 并覆盖目标 phase 帧**实现「重算」：关键点取距 ``frame_index``
    最近的采样帧（与 ``render_frame`` 采样对齐一致），保证骨架图与指标同帧。

    只重算目标 phase，其余阶段 events 保持原值（不动 ``task_store`` 中的原始结果）。

    Args:
        task_id: 任务 ID。
        phase: 阶段标识（PhaseKey 值或枚举名，如 ``"downswing"`` / ``"DOWNSWING"``）。
        frame_index: 原视频帧号（可越界，会 clamp）。

    Returns:
        ``(phase_key, actual_frame_index, metrics)``；``metrics`` 为目标阶段在
        调整帧下重算出的 ``List[StageMetric]``。

    Raises:
        FrameError: 任务不存在 / 未完成 / 阶段非法 / 帧号越界 / 数据不完整。
    """
    if not config.FRAME_ADJUST_ENABLED:
        raise FrameError(5000, "手动帧微调未启用", config.PDD_CODE_INTERNAL)

    state = _resolve_task(task_id)
    result = state.result
    if not state.out_dir:
        raise FrameError(5000, "任务数据异常", config.PDD_CODE_INTERNAL)

    phase_key = _parse_phase(phase)
    if phase_key is None:
        raise FrameError(4001, "阶段标识不合法", config.PDD_CODE_PHASE_INVALID)

    clamped = _resolve_adjusted_frame(result, frame_index)

    # 复用同源关键点，快照到最近的采样帧（与 render_frame 采样对齐一致）
    frames = load_landmarks(state.out_dir)
    if not frames:
        raise FrameError(
            5000, "缺少关键点缓存，任务数据不完整", config.PDD_CODE_INTERNAL
        )
    lm_index, lm = min(
        enumerate(frames), key=lambda pair: abs(pair[1].frame_index - clamped)
    )

    # 克隆 events 并覆盖目标 phase：array_index/frame_index 指向调整帧，
    # estimated 标 True（非原算法事件帧）。只影响目标 phase，其余阶段保持原值。
    events_clone: List[SwingEvent] = []
    for event in _reconstruct_events(result, frames):
        events_clone.append(
            SwingEvent(
                index=event.index,
                key=event.key,
                frame_index=event.frame_index,
                timestamp=event.timestamp,
                estimated=event.estimated,
                array_index=event.array_index,
            )
        )
    target = next((e for e in events_clone if e.key is phase_key), None)
    if target is None:
        raise FrameError(4001, "阶段标识不合法", config.PDD_CODE_PHASE_INVALID)

    fps = float(result.video_meta.fps or 1.0)
    target.array_index = lm_index
    target.frame_index = int(lm.frame_index)
    target.timestamp = lm.frame_index / fps if fps > 0 else 0.0
    target.estimated = True

    # 重建信号（build_context 需要 dt/speed；与 pipeline 同源、确定性）
    aspect = (
        float(result.video_meta.height) / float(result.video_meta.width)
        if result.video_meta.width > 0
        else 1.0
    )
    view: CameraView = (
        result.camera_view or result.video_meta.camera_view or CameraView.FACE_ON
    )
    try:
        signals = segmenter.build_signals(frames, fps, aspect=aspect, view=view)
    except Exception as exc:  # noqa: BLE001 - 任务已成功，重建不应失败；兜底不外抛
        logger.exception("rebuild signals failed: %s", task_id)
        raise FrameError(5000, "任务数据异常", config.PDD_CODE_INTERNAL) from exc

    ctx = metrics.build_context(
        frames, events_clone, signals, result.video_meta, view=view
    )
    ctx.phase = phase_key
    return phase_key, int(lm.frame_index), metrics.compute_phase_metrics(ctx)
