"""分析流水线编排（架构文档 §6）。

进度与步骤映射::

    step 1 上传/校验        0  -> 8
    step 2 提取身体关键点    8  -> 60
    step 3 识别 8 个挥杆阶段 60 -> 72
    step 4 计算姿态指标      72 -> 100
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List

from . import config, metrics, pose_extractor, renderer, segmenter
from .schemas import (
    AnalysisError,
    AnalysisResult,
    ErrorCode,
    PHASE_META,
    PHASE_ORDER,
    PhaseKey,
    PhaseResult,
    TaskStatus,
)
from .task_store import task_store

logger = logging.getLogger(__name__)

#: 并发软限流
CONCURRENCY_SEM = threading.Semaphore(config.MAX_CONCURRENT_TASKS)

#: 进度分界点
_P_PROBE_DONE = 8
_P_EXTRACT_END = 60
_P_SEGMENT_END = 72
_P_METRIC_END = 85
_P_RENDER_END = 95


def _image_url(task_id: str, filename: str) -> str:
    """拼成绝对 URL（前端不做拼接）。"""
    return f"{config.PUBLIC_BASE_URL}/static/{task_id}/{filename}"


def _check_timeout(created_at: float) -> None:
    """超时守卫。

    Raises:
        AnalysisError: ``TIMEOUT``。
    """
    if time.time() - created_at > config.TASK_TIMEOUT_SEC:
        raise AnalysisError(ErrorCode.TIMEOUT, "pipeline exceeded budget")


def run_analysis(task_id: str) -> None:
    """后台分析任务入口（由 ``BackgroundTasks`` 调度）。

    永不抛异常：所有失败都落到 :meth:`TaskStore.fail`。
    """
    with CONCURRENCY_SEM:
        state = task_store.get(task_id)
        if state is None:
            logger.warning("run_analysis: task not found %s", task_id)
            return
        if state.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
            return
        try:
            _run(task_id)
        except AnalysisError as exc:
            logger.warning("analysis failed %s: %s", task_id, exc)
            task_store.fail(task_id, exc.code, config.error_message(exc.code.value))
        except Exception:  # noqa: BLE001 - 兜底，绝不把 traceback 抛给调用方
            logger.exception("analysis crashed: %s", task_id)
            task_store.fail(
                task_id, ErrorCode.INTERNAL, config.error_message("INTERNAL")
            )


def _run(task_id: str) -> None:
    """真实分析流程。"""
    state = task_store.get(task_id)
    if state is None or not state.video_path or not state.out_dir:
        raise AnalysisError(ErrorCode.INTERNAL, "task state incomplete")

    video_path = state.video_path
    out_dir = state.out_dir
    created_at = state.created_at
    started = time.time()

    # ---- step 1：校验视频 -------------------------------------------------
    task_store.set_progress(task_id, 1, 5, "正在校验视频...")
    meta = pose_extractor.probe_video(video_path)
    pose_extractor.check_brightness(video_path)
    task_store.set_progress(task_id, 1, _P_PROBE_DONE, "上传完成")
    _check_timeout(created_at)

    # ---- step 2：提取关键点 -----------------------------------------------
    task_store.set_progress(task_id, 2, _P_PROBE_DONE + 1, "正在提取身体关键点...")

    def _on_progress(ratio: float) -> None:
        value = _P_PROBE_DONE + int((_P_EXTRACT_END - _P_PROBE_DONE) * ratio)
        task_store.set_progress(task_id, 2, value, "正在提取身体关键点...")

    frames = pose_extractor.extract(video_path, meta, on_progress=_on_progress)
    task_store.set_progress(task_id, 2, _P_EXTRACT_END, "关键点提取完成")
    _check_timeout(created_at)

    # ---- step 3：切分 8 阶段 ----------------------------------------------
    task_store.set_progress(task_id, 3, _P_EXTRACT_END + 2, "正在识别挥杆阶段...")
    signals = segmenter.build_signals(frames, meta.fps)
    events = segmenter.segment_swing(frames, meta.fps, sig=signals)
    task_store.set_progress(task_id, 3, _P_SEGMENT_END, "阶段识别完成")
    _check_timeout(created_at)

    # ---- step 4a：指标计算 ------------------------------------------------
    task_store.set_progress(task_id, 4, _P_SEGMENT_END + 2, "正在计算姿态指标...")
    ctx = metrics.build_context(frames, events, signals, meta)

    phase_metrics: Dict[PhaseKey, list] = {}
    total_phases = len(PHASE_ORDER)
    for order, key in enumerate(PHASE_ORDER):
        ctx.phase = key
        phase_metrics[key] = metrics.compute_phase_metrics(ctx)
        value = _P_SEGMENT_END + int(
            (_P_METRIC_END - _P_SEGMENT_END) * (order + 1) / total_phases
        )
        task_store.set_progress(task_id, 4, value, "正在计算姿态指标...")

    global_metrics = metrics.compute_global_metrics(ctx)
    _check_timeout(created_at)

    # ---- step 4b：渲染截图 ------------------------------------------------
    task_store.set_progress(task_id, 4, _P_METRIC_END, "正在生成阶段截图...")
    images = renderer.render_events(video_path, events, out_dir, frames)
    task_store.set_progress(task_id, 4, _P_RENDER_END, "正在生成分析报告...")

    # ---- 组装结果 ----------------------------------------------------------
    phases: List[PhaseResult] = []
    for event in events:
        info = PHASE_META[event.key]
        phases.append(
            PhaseResult(
                index=info.index,
                key=event.key,
                name_cn=info.name_cn,
                name_en=info.name_en,
                frame_index=event.frame_index,
                timestamp=event.timestamp,
                estimated=event.estimated,
                image_url=_image_url(task_id, images[event.key]),
                metrics=phase_metrics[event.key],
            )
        )
    phases.sort(key=lambda p: p.index)

    warnings: List[str] = list(ctx.warnings)
    if meta.low_fps and config.WARN_LOW_FPS not in warnings:
        warnings.insert(0, config.WARN_LOW_FPS)

    result = AnalysisResult(
        task_id=task_id,
        status=TaskStatus.SUCCESS,
        video_meta=meta,
        global_metrics=global_metrics,
        phases=phases,
        warnings=warnings,
        disclaimer=config.DISCLAIMER,
    )

    _cleanup_upload(video_path)
    task_store.succeed(task_id, result)
    logger.info(
        "analysis done: %s in %.2fs (frames=%d)", task_id, time.time() - started, len(frames)
    )


def _cleanup_upload(video_path: str) -> None:
    """分析成功后立即删除原视频（PRD Q6）。"""
    if not config.DELETE_UPLOAD_AFTER_SUCCESS:
        return
    try:
        path = Path(video_path)
        if path.exists():
            os.remove(path)
    except OSError:  # pragma: no cover - 删除失败不影响结果
        logger.exception("cannot delete upload: %s", video_path)
