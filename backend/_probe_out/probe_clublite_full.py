"""ClubLite 端到端实测：真实视频跑完整 pipeline（含校正 + 指标 + 风险 + 渲染）。

验收标准（ARCHITECTURE-v3-clublite.md §6.2 #5 / 主理人验收 #5）：
拿 正面1.mp4 + 1 段 DTL 跑完整 pipeline，确认校正生效 + 风险引擎/指标正常
（校正后击球帧指标变化属预期，但量纲要合理）。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/probe_clublite_full.py

与 :func:`app.pipeline._run` 的差异：不删除源视频；逐段打印校正前后对比。
属临时探针产物，不进主链路。
"""

from __future__ import annotations

import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app import (  # noqa: E402
    config,
    frame_reader,
    impact_refiner,
    metrics,
    pose_extractor,
    renderer,
    risk_engine,
    segmenter,
    view_detector,
)
from app.schemas import (  # noqa: E402
    AnalysisError,
    CameraView,
    PHASE_META,
    PHASE_ORDER,
    PhaseKey,
)

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")
OUT_DIR = os.path.join(BASE_DIR, "_probe_out", "clublite_full")

CASES: list = [
    ("正面1", os.path.join(SAMPLE_DIR, "正面1.mp4"), "face-on"),
    ("DTL-4e8d0d7e", os.path.join(
        SAMPLE_DIR, "4e8d0d7e517a67a2a7698fd1536289eb.mp4"), "dtl"),
]


def _view_of(chosen: str) -> CameraView:
    return CameraView.DOWN_THE_LINE if chosen == "dtl" else CameraView.FACE_ON


def run_one(name: str, path: str, chosen_view: str) -> None:
    """完整 pipeline（校正 + 指标 + 风险 + 渲染）。"""
    print(f"\n{'=' * 78}\n[{name}] {chosen_view}  {path}\n{'=' * 78}")
    started = time.time()
    # 每段独立统计 opens（避免上一段累计污染）
    frame_reader.reset_stats()

    meta = pose_extractor.probe_video(path)
    pose_extractor.check_brightness(path)
    frames = pose_extractor.extract(path, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    signals = segmenter.build_signals(frames, meta.fps, aspect=aspect)
    events = segmenter.segment_swing(frames, meta.fps, sig=signals)

    addr_index = next(
        (e.array_index for e in events if e.key is PhaseKey.ADDRESS), 0
    )
    view, view_warning = view_detector.resolve(
        _view_of(chosen_view), frames, meta, addr_index
    )
    meta.camera_view = view

    impact_old = next(e for e in events if e.key is PhaseKey.IMPACT)
    print(f"  校正前 impact: array={impact_old.array_index} "
          f"frame={impact_old.frame_index} t={impact_old.timestamp:.3f}s")

    # ---- 校正（与 pipeline step4a 同口径）---------------------------------
    warnings: list = []
    event_frames = [e.frame_index for e in events]
    cand_frames, decode_frames = impact_refiner.plan_refine_frames(
        events, signals, meta, frames=frames
    )
    # P1 修复：预计算 reanchor 可能产出的事件帧并入解码集（opens=1）
    possible_frames = impact_refiner.plan_reanchor_frames(
        events, signals, meta, frames=frames, cand_frames=cand_frames
    )
    frames_bgr = frame_reader.grab_frames(
        path,
        sorted(set(event_frames) | set(decode_frames) | set(possible_frames)),
    )
    refine = impact_refiner.refine_impact(
        path, frames, events, signals, view, meta, frames_bgr=frames_bgr,
    )
    if refine.available and (
        config.CLUBLITE_MIN_SHIFT_FRAMES
        <= abs(refine.delta_frames)
        <= config.CLUBLITE_MAX_SHIFT_FRAMES
    ):
        new_events = segmenter.reanchor_impact(
            frames, signals, events, refine.new_array_index
        )
        if new_events is not None:
            events = new_events
            if abs(refine.delta_frames) >= config.CLUBLITE_WARN_THRESHOLD_FRAMES:
                warnings.append(config.WARN_IMPACT_REFINED)
    event_frames = [e.frame_index for e in events]
    frames_bgr = {k: v for k, v in frames_bgr.items() if k in set(event_frames)}

    impact_new = next(e for e in events if e.key is PhaseKey.IMPACT)
    print(f"  校正后 impact: array={impact_new.array_index} "
          f"frame={impact_new.frame_index} t={impact_new.timestamp:.3f}s "
          f"delta={refine.delta_frames:+d} method={refine.method} "
          f"conf={refine.confidence:.2f} ball={refine.ball_detected}")
    all_events_decoded = all(e.frame_index in frames_bgr for e in events)
    print(f"  校正后 8 事件帧全在解码集（无渲染 fallback）: {all_events_decoded}")

    # ---- 指标 -------------------------------------------------------------
    ctx = metrics.build_context(frames, events, signals, meta, view=view)
    phase_metrics = {}
    for key in PHASE_ORDER:
        ctx.phase = key
        phase_metrics[key] = metrics.compute_phase_metrics(ctx)
    ctx.phase = None
    global_metrics = metrics.compute_global_metrics(ctx)

    impact_metrics = phase_metrics[PhaseKey.IMPACT]
    print("  impact 阶段指标:")
    for m in impact_metrics:
        print(f"    {m.key:<22s} {m.value:>8.1f} {m.unit:<4s} "
              f"ref[{m.ref_min},{m.ref_max}] {m.status.value}")
    gm = global_metrics
    print(f"  全程指标: tempo_ratio={gm.tempo_ratio:.2f} "
          f"swing_duration={gm.swing_duration:.2f}s "
          f"max_head_drift_pct={gm.max_head_drift_pct:.2f}")

    # ---- 风险 -------------------------------------------------------------
    risk_map = risk_engine.evaluate_all(phase_metrics, view)
    total_risks = sum(len(v) for v in risk_map.values())
    print(f"  风险数: {total_risks}")
    for key in PHASE_ORDER:
        for r in risk_map.get(key, []):
            print(f"    {r.rule_id} [{r.risk_level.value}] {r.risk_name} "
                  f"({r.metric_key}={r.value:.1f})")

    # ---- 渲染 -------------------------------------------------------------
    # 用 ASCII safe 目录（避免 Windows + cv2 非 ASCII 路径报错）
    safe_name = (
        name.replace("正面", "front_").replace("DTL-", "dtl_")
        .replace("VID-", "vid_")
    )
    render_dir = os.path.join(OUT_DIR, safe_name)
    os.makedirs(render_dir, exist_ok=True)
    markers = None
    if config.CLUBLITE_DRAW_MARKER and refine.ball_center_px is not None:
        markers = {impact_new.frame_index: refine.ball_center_px}
    images = renderer.render_events(
        path, events, render_dir, frames, frames_bgr=frames_bgr,
        view=view, markers=markers,
    )
    all_exist = all(
        os.path.exists(os.path.join(render_dir, v)) for v in images.values()
    )

    warnings.extend(list(ctx.warnings))
    if view_warning:
        warnings.insert(0, view_warning)
    print(f"  warnings: {warnings}")
    print(f"  render: {len(images)} 张，all_exist={all_exist}，"
          f"opens={frame_reader.stats()['opens']}，"
          f"墙钟={time.time() - started:.2f}s")
    print(f"  -> {render_dir}")


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, path, view in CASES:
        if not os.path.exists(path):
            print(f"[skip] {name}: {path}")
            continue
        try:
            run_one(name, path, view)
        except AnalysisError as exc:
            print(f"  >>> AnalysisError {exc.code.value}: {exc.detail}")
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"  >>> unexpected {type(exc).__name__}: {exc}")
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
