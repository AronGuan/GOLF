"""QA R2：P1 修复回归——送杆截图必须等于真实送杆帧（正面3 / DTL-4e8d0d7e）。

pipeline 同口径：decode(event ∪ window ∪ possible) → refine → reanchor →
trim(event_frames) → render。断言：
1) 校正后 8 事件帧全部在 frames_bgr（无 renderer fallback）；
2) 07_follow_through.jpg 内容 == 用真帧渲染（逐字节）；
3) 送杆图 != 收杆图内容（旧 bug 已消失）。
"""

from __future__ import annotations

import os
import sys

import cv2
import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config, frame_reader, impact_refiner, pose_extractor, renderer, segmenter, view_detector  # noqa: E402
from app.schemas import CameraView, PhaseKey  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")
CASES = [
    ("正面3", os.path.join(SAMPLE_DIR, "正面3.mp4"), CameraView.FACE_ON),
    ("DTL-4e8d0d7e", os.path.join(SAMPLE_DIR, "4e8d0d7e517a67a2a7698fd1536289eb.mp4"),
     CameraView.DOWN_THE_LINE),
]
OUT = os.path.join(BASE_DIR, "_probe_out", "qa_r2_ft")
os.makedirs(OUT, exist_ok=True)


def run(name, path, view_hint):
    print(f"\n===== {name} =====")
    meta = pose_extractor.probe_video(path)
    frames = pose_extractor.extract(path, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    signals = segmenter.build_signals(frames, meta.fps, aspect=aspect)
    events = segmenter.segment_swing(frames, meta.fps, sig=signals)
    addr_index = next((e.array_index for e in events if e.key is PhaseKey.ADDRESS), 0)
    view, _ = view_detector.resolve(view_hint, frames, meta, addr_index)

    event_frames = [e.frame_index for e in events]
    cand, decode = impact_refiner.plan_refine_frames(events, signals, meta, frames=frames)
    possible = impact_refiner.plan_reanchor_frames(
        events, signals, meta, frames=frames, cand_frames=cand
    )
    union = sorted(set(event_frames) | set(decode) | set(possible))
    print(f"union decode frames = {len(union)} (window={len(cand)} possible={len(possible)})")
    frames_bgr = frame_reader.grab_frames(path, union)
    refine = impact_refiner.refine_impact(path, frames, events, signals, view, meta, frames_bgr=frames_bgr)
    print(f"refine available={refine.available} delta={refine.delta_frames:+d} method={refine.method}")
    if not refine.available:
        print(f"[SKIP] {name} refine 不可用（G0）")
        return True
    new_events = segmenter.reanchor_impact(frames, signals, events, refine.new_array_index)
    assert new_events is not None
    events = new_events
    event_frames = [e.frame_index for e in events]

    # 断言 1：8 事件帧全在解码集（无 fallback）
    missing = [e.key.value for e in events if e.frame_index not in frames_bgr]
    print(f"event frames after reanchor: {[(e.key.value, e.frame_index) for e in events]}")
    print(f"missing from frames_bgr: {missing}")
    assert not missing, f"P1 复发：{missing} 未解码"

    # trim（pipeline 同口径）
    frames_bgr = {k: v for k, v in frames_bgr.items() if k in set(event_frames)}
    out_dir = os.path.join(OUT, name.replace("DTL-", "dtl_").replace("正面", "front_"))
    os.makedirs(out_dir, exist_ok=True)
    images = renderer.render_events(path, events, out_dir, frames, frames_bgr=frames_bgr, view=view)

    ft_event = next(e for e in events if e.key is PhaseKey.FOLLOW_THROUGH)
    fin_event = next(e for e in events if e.key is PhaseKey.FINISH)
    ft_name = images[PhaseKey.FOLLOW_THROUGH]
    fin_name = images[PhaseKey.FINISH]

    # 断言 2：用真帧渲染同一 FT 事件，与产物逐字节一致
    from app import config as cfg
    def render_raw(bgr, ev):
        img, scale = renderer._resize_long_side(bgr, cfg.RENDER_LONG_SIDE)
        lm = frames[ev.array_index]
        renderer._draw_skeleton(img, lm.norm, img.shape[1], img.shape[0])
        if view is CameraView.DOWN_THE_LINE:
            renderer._draw_horizon(img)
        renderer._draw_label(img, f"#{ev.index} f{ev.frame_index} {ev.timestamp:.2f}s")
        return cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), cfg.JPEG_QUALITY])[1].tobytes()

    ft_real = render_raw(frames_bgr[ft_event.frame_index], ft_event)
    ft_produced = open(os.path.join(out_dir, ft_name), "rb").read()
    fin_raw = render_raw(frames_bgr[fin_event.frame_index], fin_event)
    print(f"FT image == render(real FT frame {ft_event.frame_index})? {ft_produced == ft_real}")
    print(f"FT image == render(finish frame {fin_event.frame_index})? {ft_produced == fin_raw}")
    assert ft_produced == ft_real, f"送杆图内容 != 真帧 {ft_event.frame_index}"
    assert ft_produced != fin_raw, "送杆图仍与收杆帧相同（P1 未修复）"
    print(f"[PASS] {name}: 送杆图=真帧，无 fallback")
    return True


def main():
    ok = True
    for name, path, view in CASES:
        if not os.path.exists(path):
            print(f"[skip] {name}: {path}")
            continue
        try:
            ok = run(name, path, view) and ok
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            ok = False
    print(f"\nR2 FT 回归: {'ALL PASS' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
