"""QA 实测：reanchor 后 follow_through 帧不在解码集 → 渲染兜底错帧（P1 验证）。

对比 pipeline step4a 同口径：decode(event ∪ window) → refine → reanchor →
trim(event_frames) → render。检查 07_follow_through.jpg 是否与 08_finish.jpg
逐字节相同（若是，说明送杆图实际用了收杆帧内容，帧号标注却为 f<new_ft>）。
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
path = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples", "正面3.mp4")
OUT = os.path.join(BASE_DIR, "_probe_out", "qa_p1_ft")
os.makedirs(OUT, exist_ok=True)

meta = pose_extractor.probe_video(path)
frames = pose_extractor.extract(path, meta)
aspect = meta.height / meta.width if meta.width > 0 else 1.0
signals = segmenter.build_signals(frames, meta.fps, aspect=aspect)
events = segmenter.segment_swing(frames, meta.fps, sig=signals)
addr_index = next((e.array_index for e in events if e.key is PhaseKey.ADDRESS), 0)
view, _ = view_detector.resolve(CameraView.FACE_ON, frames, meta, addr_index)

event_frames = [e.frame_index for e in events]
_cand, decode = impact_refiner.plan_refine_frames(events, signals, meta, frames=frames)
frames_bgr = frame_reader.grab_frames(path, sorted(set(event_frames) | set(decode)))
refine = impact_refiner.refine_impact(path, frames, events, signals, view, meta, frames_bgr=frames_bgr)
print(f"refine available={refine.available} delta={refine.delta_frames:+d}")
assert refine.available
new_events = segmenter.reanchor_impact(frames, signals, events, refine.new_array_index)
assert new_events is not None
events = new_events
event_frames = [e.frame_index for e in events]
ft = next(e for e in events if e.key is PhaseKey.FINISH)
print(f"new events: impact={next(e.frame_index for e in events if e.key is PhaseKey.IMPACT)} "
      f"ft={next(e.frame_index for e in events if e.key is PhaseKey.FOLLOW_THROUGH)} finish={ft.frame_index}")

frames_bgr = {k: v for k, v in frames_bgr.items() if k in set(event_frames)}
print(f"trimmed frames_bgr keys: {sorted(frames_bgr.keys())}")
print(f"follow_through {next(e.frame_index for e in events if e.key is PhaseKey.FOLLOW_THROUGH)} in frames_bgr? "
      f"{next(e.frame_index for e in events if e.key is PhaseKey.FOLLOW_THROUGH) in frames_bgr}")

images = renderer.render_events(path, events, OUT, frames, frames_bgr=frames_bgr, view=view)
ft_name = images[PhaseKey.FOLLOW_THROUGH]
finish_name = images[PhaseKey.FINISH]
impact_name = images[PhaseKey.IMPACT]
a = open(os.path.join(OUT, ft_name), "rb").read()
b = open(os.path.join(OUT, finish_name), "rb").read()
c = open(os.path.join(OUT, impact_name), "rb").read()
print(f"FT image {ft_name} == finish image? {a == b} (len {len(a)} vs {len(b)})")
print(f"FT image == impact image? {a == c}")
