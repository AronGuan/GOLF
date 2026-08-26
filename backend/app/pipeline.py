"""分析流水线编排（架构 ARCHITECTURE.md §6 + ARCHITECTURE-v2.md §5）。

进度与步骤映射（v2 重新分段；2026-08 球杆检测下线后再次收拢）::

    step 1 上传/校验        0  -> 8
    step 2 提取身体关键点    8  -> 56
    step 3 识别 8 个挥杆阶段 56 -> 68
    step 4 机位解析/指标/风险 68 -> 86
          渲染 8 张截图      86 -> 96
          装配分析报告       96 -> 100

v2 三处插入点（相对 MVP，其余节点行为不变）：
    1. step3 后插入机位解析（``view_detector.resolve``）；
    2. 指标后插入风险引擎（``risk_engine.evaluate_all``），装进 ``PhaseResult.risks``。

⚠️ 2026-08：球杆检测已下线（原 step4a 的 ``club_detector`` 共享解码块摘除），
主管线不再解码 Top→Impact 窗口采样帧，只解码 8 个事件帧供渲染。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import (
    config,
    frame_reader,
    impact_refiner,
    landmark_cache,
    metrics,
    pose_extractor,
    renderer,
    risk_engine,
    segmenter,
    view_detector,
)
from .schemas import (
    AnalysisError,
    AnalysisResult,
    CameraView,
    ErrorCode,
    FrameLandmarks,
    PHASE_META,
    PHASE_ORDER,
    PhaseKey,
    PhaseResult,
    SwingEvent,
    SwingSignals,
    TaskStatus,
    VideoMeta,
)
from .task_store import task_store

logger = logging.getLogger(__name__)

#: 并发软限流
CONCURRENCY_SEM = threading.Semaphore(config.MAX_CONCURRENT_TASKS)

#: 进度分界点（v2 分段，球杆检测下线后去掉 _P_CLUB_END）
_P_PROBE_DONE = 8
_P_EXTRACT_END = 56
_P_SEGMENT_END = 68
_P_METRIC_END = 86
_P_RENDER_END = 96
_P_DONE = 100

#: SwingNet（GolfDB）事件名 -> 8 阶段契约 :class:`PhaseKey`。
#: 顺序与 :data:`PHASE_ORDER` 完全一致（Address→Takeaway→…→Finish），
#: 迭代 ``items()`` 即按挥杆时序排列。
_SWINGNET_PHASE_MAP: Dict[str, PhaseKey] = {
    "Address": PhaseKey.ADDRESS,
    "Toe-up": PhaseKey.TAKEAWAY,
    "Mid-backswing": PhaseKey.BACKSWING,
    "Top": PhaseKey.TOP,
    "Mid-downswing": PhaseKey.DOWNSWING,
    "Impact": PhaseKey.IMPACT,
    "Mid-follow-through": PhaseKey.FOLLOW_THROUGH,
    "Finish": PhaseKey.FINISH,
}


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


def _detect_dtl_events_swingnet(
    video_path: str,
    meta: VideoMeta,
    frames: List[FrameLandmarks],
    signals: SwingSignals,
) -> Optional[List[SwingEvent]]:
    """DTL 视频用 SwingNet 检测 8 事件，返回对齐采样序列的 :class:`SwingEvent`。

    返回 ``None`` 表示检测不可用/不可信，调用方应回退规则引擎。三重回退守卫
    （任一命中即返回 ``None`` 并记 warning）：

    1. 异常/权重缺失：SwingNet 惰性加载失败、视频不可解码、torch 运行时异常；
    2. Impact 置信度低于 :data:`config.SWINGNET_MIN_IMPACT_CONF`；
    3. 8 事件 ``frame_index`` 不严格单调递增（时序乱，说明非单次挥杆）。

    Args:
        video_path: 原视频路径（SwingNet 逐帧读原视频，``frame_index`` 为原视频帧号）。
        meta: 视频元信息（``fps`` / ``sample_step``）。
        frames: :func:`pose_extractor.extract` 的采样序列（用于还原 ``array_index``）。
        signals: 切分信号包。当前 SwingNet 事件映射不读 signals，仅保留入参
            以对齐调用方语义（避免"要不要 build signals"在 pipeline 里分叉）。

    Returns:
        恒 8 个、帧号严格递增的 :class:`SwingEvent`（``estimated=False``）；
        检测失败/不可信返回 ``None``。
    """
    # 惰性导入：torch 只在 DTL SwingNet 路径按需加载，face-on 绝不 import torch
    # （与 ``app.ai.__init__`` 的懒加载设计一致）。
    from .ai.swingnet_detector import SwingNetDetector

    n = len(frames)
    if n == 0:
        logger.warning("SwingNet: empty frames, fallback to rule engine")
        return None

    try:
        raw = SwingNetDetector().detect(video_path)
    except Exception as exc:  # noqa: BLE001 - 任何检测异常都回退，绝不拖垮主链路
        logger.warning("SwingNet detect failed, fallback to rule engine: %s", exc)
        return None

    try:
        if not raw or len(raw) != len(_SWINGNET_PHASE_MAP):
            logger.warning(
                "SwingNet returned %d events (expected %d), fallback to rule engine",
                len(raw) if raw else 0,
                len(_SWINGNET_PHASE_MAP),
            )
            return None
        if not set(raw) >= set(_SWINGNET_PHASE_MAP):
            logger.warning(
                "SwingNet events incomplete %s, fallback to rule engine", sorted(raw)
            )
            return None

        # 击球置信度守卫
        impact_conf = float(raw["Impact"]["confidence"])
        if impact_conf < config.SWINGNET_MIN_IMPACT_CONF:
            logger.warning(
                "SwingNet impact confidence %.4f < %.2f, fallback to rule engine",
                impact_conf,
                config.SWINGNET_MIN_IMPACT_CONF,
            )
            return None

        # 时序守卫：8 事件 frame_index 必须严格递增
        frame_indices = [int(raw[name]["frame_index"]) for name in _SWINGNET_PHASE_MAP]
        if any(b <= a for a, b in zip(frame_indices, frame_indices[1:])):
            logger.warning(
                "SwingNet events non-monotonic %s, fallback to rule engine",
                frame_indices,
            )
            return None
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("SwingNet result malformed (%s), fallback to rule engine", exc)
        return None

    step = max(1, int(meta.sample_step))
    fps = float(meta.fps)
    events: List[SwingEvent] = []
    for order, (name, key) in enumerate(_SWINGNET_PHASE_MAP.items()):
        frame_index = frame_indices[order]
        # 原视频帧号 -> 采样序列下标：帧号恰落在采样网格时 floor 即精确下标；
        # 非网格帧号取最近采样帧（floor 侧），并夹到合法区间。
        array_index = max(0, min(n - 1, frame_index // step))
        events.append(
            SwingEvent(
                index=PHASE_META[key].index,
                key=key,
                frame_index=frame_index,
                timestamp=round(frame_index / fps, 3) if fps > 0 else 0.0,
                estimated=False,
                array_index=array_index,
            )
        )
    return events


def _segment_events(
    video_path: str,
    meta: VideoMeta,
    frames: List[FrameLandmarks],
    signals: SwingSignals,
    aspect: float,
    view: CameraView,
) -> Tuple[List[SwingEvent], bool]:
    """第二遍切分：face-on 走规则引擎；DTL 走 SwingNet（失败回退规则引擎）。

    这是 M2 的**唯一**切分分叉点：

    - ``FACE_ON``：恒调 :func:`segmenter.segment_swing`（逐字节不变）；
    - ``DOWN_THE_LINE``：先试 SwingNet，不可用/不可信回退规则引擎。

    Returns:
        ``(events, used_swingnet)``——``used_swingnet=True`` 表示 events 来自
        SwingNet（后续跳过 CLUBLITE refine/reanchor，避免对已准的 Impact 引入偏差）。
    """
    if view is CameraView.DOWN_THE_LINE:
        if config.SWINGNET_ENABLED:
            events = _detect_dtl_events_swingnet(video_path, meta, frames, signals)
            if events is not None:
                return events, True
            logger.warning("DTL SwingNet 不可用，回退规则引擎切分")
        else:
            logger.info("SwingNet 已关闭（SWINGNET_ENABLED=False），DTL 回退规则引擎")

    events = segmenter.segment_swing(
        frames, meta.fps, sig=signals, aspect=aspect, view=view
    )
    return events, False


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

    # 落盘关键点序列缓存（结果页手动帧微调的数据底座；失败不阻断主链路，
    # 仅该功能降级为 5000）。
    try:
        landmark_cache.save_landmarks(out_dir, frames, meta)
    except Exception:  # noqa: BLE001 - 缓存失败只影响手动帧微调，不拖垮分析
        logger.exception("landmark cache save failed: %s", task_id)

    # ---- step 3：切分 8 阶段 ----------------------------------------------
    task_store.set_progress(task_id, 3, _P_EXTRACT_END + 2, "正在识别挥杆阶段...")
    # aspect = H/W：把归一化坐标换算成各向同性单位，否则横竖屏阈值会漂移
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    signals = segmenter.build_signals(frames, meta.fps, aspect=aspect)
    # 第一遍切分（默认 face-on）只用于取 Address 帧 → 机位判定。机位判定需要
    # Address 帧的「图像肩宽/图像身高」（DTL 双肩前后重叠 < 0.13），而 Address
    # 锚点本身与 view 无关（locate_top/address/impact/finish 均不读 view），
    # 因此第一遍的 Address 对 DTL 视频同样成立。
    prelim_events = segmenter.segment_swing(frames, meta.fps, sig=signals)
    addr_index = next(
        (e.array_index for e in prelim_events if e.key is PhaseKey.ADDRESS), 0
    )
    view, view_warning = view_detector.resolve(
        state.camera_view, frames, meta, addr_index
    )
    meta.camera_view = view
    # 第二遍切分：传解析后的机位。⑤ 下杆阈值分机位（face-on=H_DOWNSWING 与历史
    # 逐字节一致；DTL=H_DOWNSWING_DTL，偏离顶点更靠后）。纯函数重跑，秒级。
    # ⚠️ DTL 分标尺（2026-08 用户拍板）：DTL 双肩前后重叠、投影肩宽被压缩
    # （实测 0.005~0.014，约正面 1/20），第一遍的肩宽标尺信号会让 h 爆炸
    # （实测 20~34）→ locate_impact 穿越判据永不触发 → 击球帧兜底 estimated；
    # 因此 DTL 改用**身高**标尺重建信号，并把该信号传给下游全部消费方
    # （impact_refiner / reanchor_impact / metrics.build_context），保证
    # 切分、校正、指标三处量纲一致。face-on 保持第一遍信号对象，逐字节不变。
    if view is CameraView.DOWN_THE_LINE:
        signals = segmenter.build_signals(frames, meta.fps, aspect=aspect, view=view)
    # M2：DTL 切 SwingNet（失败回退规则引擎），face-on 保持规则引擎逐字节不变。
    events, used_swingnet = _segment_events(
        video_path, meta, frames, signals, aspect, view
    )
    task_store.set_progress(task_id, 3, _P_SEGMENT_END, "阶段识别完成")
    _check_timeout(created_at)

    # ---- step 4a：机位解析 + 共享解码 + 击球帧校正 + 解码 8 个事件帧 -------
    task_store.set_progress(
        task_id, 4, _P_SEGMENT_END + 2, "正在解析机位与解码事件帧...",
        step_text=config.STEP_TEXTS[4],
    )
    event_frames = [e.frame_index for e in events]

    # 击球帧校正（CLUBLITE）：与 renderer 共享同一次第 2 趟解码。
    # - 解码集 = 8 事件帧 ∪ 校正窗口帧（≤12 帧窗口 + 前一帧 + Address 帧）；
    # - refine 后立即裁剪 frames_bgr 只留校正后的 8 个事件帧（内存峰值锁回 8 帧）；
    # - G0（refine 不可用 / reanchor 冲突）→ events 保持原状，不影响主链路。
    refine_warning: Optional[str] = None
    refine_markers: Optional[Dict[int, Tuple[int, int]]] = None
    # ⚠️ DTL + SwingNet（used_swingnet=True）跳过 CLUBLITE 校正：SwingNet 的
    # Impact 已 ≈1 帧误差，CLUBLITE 是针对规则引擎运动峰偏差做的帧级校正，
    # 对 SwingNet 反而可能引入偏差；此时直接走 else 分支只解码 8 个事件帧。
    # face-on 与 DTL 回退规则引擎（used_swingnet=False）保持校正现状不变。
    if config.CLUBLITE_ENABLED and not used_swingnet:
        _cand_frames, _decode_frames = impact_refiner.plan_refine_frames(
            events, signals, meta, frames=frames
        )
        # QA P1 修复：reanchor 可能把 ⑦ 送杆移到旧事件帧之外 → 解码前预计算
        # 全部可能的事件帧集并入解码集，保证校正后 8 事件帧必在解码集内
        # （纯函数无 IO，解码仍为 1 趟，opens=1）。
        _possible_frames = impact_refiner.plan_reanchor_frames(
            events, signals, meta, frames=frames, cand_frames=_cand_frames, view=view
        )
        frames_bgr = frame_reader.grab_frames(
            video_path,
            sorted(set(event_frames) | set(_decode_frames) | set(_possible_frames)),
            orientation=meta.orientation,
        )
        refine = impact_refiner.refine_impact(
            video_path, frames, events, signals, view, meta, frames_bgr=frames_bgr,
        )
        if refine.available and (
            config.CLUBLITE_MIN_SHIFT_FRAMES
            <= abs(refine.delta_frames)
            <= config.CLUBLITE_MAX_SHIFT_FRAMES
        ):
            new_events = segmenter.reanchor_impact(
                frames, signals, events, refine.new_array_index, view=view
            )
            if new_events is not None:
                events = new_events
                if abs(refine.delta_frames) >= config.CLUBLITE_WARN_THRESHOLD_FRAMES:
                    refine_warning = config.WARN_IMPACT_REFINED
                logger.info(
                    "impact refined (task=%s): %d -> %d delta=%+d method=%s conf=%.2f",
                    task_id, refine.old_array_index, refine.new_array_index,
                    refine.delta_frames, refine.method, refine.confidence,
                )
        if config.CLUBLITE_DRAW_MARKER and refine.ball_center_px is not None:
            impact_event = next(
                (e for e in events if e.key is PhaseKey.IMPACT), None
            )
            if impact_event is not None:
                refine_markers = {impact_event.frame_index: refine.ball_center_px}
        # 校正可能移动 impact / 中间帧 → 以校正后的 8 事件帧为准
        event_frames = [e.frame_index for e in events]
    else:
        # 球杆检测下线：只解码 8 个事件帧供 renderer，解码趟数锁 1 趟（共享）
        frames_bgr = frame_reader.grab_frames(
            video_path, event_frames, orientation=meta.orientation
        )

    # 🔑 只保留 8 个事件帧，内存峰值锁 8 帧
    frames_bgr = {k: v for k, v in frames_bgr.items() if k in set(event_frames)}

    # ---- step 4b：指标计算（机位过滤 + fn_key 分派 + 五态判定）------------
    task_store.set_progress(task_id, 4, _P_SEGMENT_END + 4, "正在计算姿态指标...")
    ctx = metrics.build_context(frames, events, signals, meta, view=view)

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

    # ---- step 4c：风险引擎（机位门控 → 条件匹配 → 文案渲染）---------------
    task_store.set_progress(task_id, 4, _P_METRIC_END, "正在匹配损伤风险...")
    risk_map = risk_engine.evaluate_all(phase_metrics, view)

    # ---- step 4d：渲染截图（共享解码帧 + DTL 水平参考线）------------------
    task_store.set_progress(task_id, 4, _P_METRIC_END + 2, "正在生成阶段截图...")
    images = renderer.render_events(
        video_path, events, out_dir, frames, frames_bgr=frames_bgr, view=view,
        markers=refine_markers,
    )
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
                risks=risk_map.get(event.key, []),
            )
        )
    phases.sort(key=lambda p: p.index)

    warnings: List[str] = list(ctx.warnings)
    if refine_warning and refine_warning not in warnings:
        warnings.insert(0, refine_warning)
    if view_warning and view_warning not in warnings:
        warnings.insert(0, view_warning)
    if meta.low_fps and config.WARN_LOW_FPS not in warnings:
        warnings.insert(0, config.WARN_LOW_FPS)

    disclaimer = config.DISCLAIMER
    if view is CameraView.DOWN_THE_LINE:
        disclaimer = disclaimer + config.DISCLAIMER_DTL_SUFFIX

    meta.total_frames = meta.frame_count

    result = AnalysisResult(
        task_id=task_id,
        status=TaskStatus.SUCCESS,
        camera_view=view,
        video_meta=meta,
        global_metrics=global_metrics,
        phases=phases,
        warnings=warnings,
        disclaimer=disclaimer,
    )

    task_store.set_progress(task_id, 4, _P_DONE, "分析完成")
    _cleanup_upload(video_path)
    task_store.succeed(task_id, result)
    logger.info(
        "analysis done: %s in %.2fs (frames=%d view=%s risks=%d)",
        task_id, time.time() - started, len(frames), view.value,
        sum(len(items) for items in risk_map.values()),
    )


def _cleanup_upload(video_path: str) -> None:
    """分析成功后处理原视频。

    - ``KEEP_SOURCE_VIDEO=True``（手动帧微调需要原始像素）：把 ``upload.{ext}``
      改名为 ``source.{ext}`` 留在任务目录（随任务 TTL 一起清理），
      原 ``upload.*`` 仍被移除（满足 PRD Q6 的既有测试断言）；
    - 否则按 PRD Q6 立即删除原视频。
    """
    if not config.DELETE_UPLOAD_AFTER_SUCCESS:
        return
    src = Path(video_path)
    if not src.exists():
        return
    if config.KEEP_SOURCE_VIDEO:
        try:
            os.replace(str(src), str(src.with_name("source" + src.suffix.lower())))
            logger.info("kept source video for frame adjust: %s", src)
            return
        except OSError:  # pragma: no cover - 改名失败退回删除，不影响结果
            logger.exception("rename source video failed: %s", video_path)
    try:
        os.remove(video_path)
    except OSError:  # pragma: no cover - 删除失败不影响结果
        logger.exception("cannot delete upload: %s", video_path)
