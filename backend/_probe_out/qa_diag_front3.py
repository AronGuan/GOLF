"""诊断：正面3 为何两次 refine 结果不同（探针 available=True delta=+4 vs 复跑 available=False delta=+0）。"""

from __future__ import annotations

import os
import sys

import cv2

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config, frame_reader, geometry, impact_refiner, pose_extractor, segmenter, view_detector  # noqa: E402
from app.schemas import CameraView, PhaseKey  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
path = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples", "正面3.mp4")

for trial in range(3):
    print(f"\n===== trial {trial} =====")
    meta = pose_extractor.probe_video(path)
    frames = pose_extractor.extract(path, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    signals = segmenter.build_signals(frames, meta.fps, aspect=aspect)
    events = segmenter.segment_swing(frames, meta.fps, sig=signals)
    addr_index = next((e.array_index for e in events if e.key is PhaseKey.ADDRESS), 0)
    view, vw = view_detector.resolve(CameraView.FACE_ON, frames, meta, addr_index)
    impact = next(e for e in events if e.key is PhaseKey.IMPACT)
    print(f"view={view.value} impact.array_index={impact.array_index} n={signals.n} fps_eff={signals.fps_eff:.2f}")

    cand, decode = impact_refiner.plan_refine_frames(events, signals, meta, frames=frames)
    print(f"cand={cand} decode={decode}")

    event_frames = [e.frame_index for e in events]
    frames_bgr = frame_reader.grab_frames(path, sorted(set(event_frames) | set(decode)))
    print(f"grab got {len(frames_bgr)} frames, missing cand: {[f for f in cand if f not in frames_bgr]}")

    addr_lm = frames[addr_index]
    width, height = int(meta.width), int(meta.height)
    nose_y = float(addr_lm.norm[geometry.NOSE, 1]) * height
    ankle_y = (float(addr_lm.norm[geometry.L_ANKLE, 1]) + float(addr_lm.norm[geometry.R_ANKLE, 1])) / 2.0 * height
    body_h = geometry.body_height_px(nose_y, ankle_y)
    print(f"body_h_px={body_h}")
    roi = impact_refiner._ground_roi(addr_lm, width, height, body_h, view)
    print(f"roi={roi}")
    gray = [cv2.cvtColor(frames_bgr[f], cv2.COLOR_BGR2GRAY) for f in cand if f in frames_bgr]
    motion = impact_refiner._motion_signal(gray, roi)
    raw = impact_refiner._motion_signal(gray, roi, smooth=False)
    cands = impact_refiner._pick_candidates(motion, config.CLUBLITE_MOTION_MIN_RATIO, config.CLUBLITE_TOP_K)
    print(f"candidates={cands} motion_max={float(motion.max()) if len(motion) else None}")
    print(f"motion={[round(float(v), 3) for v in motion]}")

    r = impact_refiner.refine_impact(path, frames, events, signals, view, meta, frames_bgr=frames_bgr)
    print(f"refine -> available={r.available} delta={r.delta_frames} method={r.method} new={r.new_array_index}")
