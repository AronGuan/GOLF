"""确认：FT 兜底图实际内容 = finish 帧(101) 还是 impact 帧(74)？

方法：用同一事件(FT)分别渲染「原帧 83 内容」与「帧 101 内容」「帧 74 内容」，
与 pipeline 产出的 07_follow_through.jpg 逐字节对比（标签一致，只有内容不同）。
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

from app import frame_reader, pose_extractor, renderer, segmenter  # noqa: E402
from app.schemas import CameraView, PhaseKey  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
path = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples", "正面3.mp4")
OUT = os.path.join(BASE_DIR, "_probe_out", "qa_p1_ft")

meta = pose_extractor.probe_video(path)
frames = pose_extractor.extract(path, meta)

# 从 JSON 取 after events 不便，直接重跑精简 pipeline
from app import impact_refiner, view_detector  # noqa: E402
aspect = meta.height / meta.width if meta.width > 0 else 1.0
signals = segmenter.build_signals(frames, meta.fps, aspect=aspect)
events = segmenter.segment_swing(frames, meta.fps, sig=signals)
addr_index = next((e.array_index for e in events if e.key is PhaseKey.ADDRESS), 0)
view, _ = view_detector.resolve(CameraView.FACE_ON, frames, meta, addr_index)
event_frames = [e.frame_index for e in events]
_cand, decode = impact_refiner.plan_refine_frames(events, signals, meta, frames=frames)
frames_bgr = frame_reader.grab_frames(path, sorted(set(event_frames) | set(decode)))
refine = impact_refiner.refine_impact(path, frames, events, signals, view, meta, frames_bgr=frames_bgr)
new_events = segmenter.reanchor_impact(frames, signals, events, refine.new_array_index)
events = new_events
event_frames = [e.frame_index for e in events]
frames_bgr = {k: v for k, v in frames_bgr.items() if k in set(event_frames)}
images = renderer.render_events(path, events, OUT, frames, frames_bgr=frames_bgr, view=view)

ft_event = next(e for e in events if e.key is PhaseKey.FOLLOW_THROUGH)
ft_pipe = open(os.path.join(OUT, images[PhaseKey.FOLLOW_THROUGH]), "rb").read()

# 渲染同一 FT 事件但用不同底图
def render_with_frame(bgr):
    tmp = os.path.join(OUT, "_probe_cmp")
    os.makedirs(tmp, exist_ok=True)
    # 复用 _render_one 的底图逻辑：直接调用内部函数需要 event，这里手动拼
    from app import config as cfg
    img, scale = renderer._resize_long_side(bgr, cfg.RENDER_LONG_SIDE)
    lm = frames[ft_event.array_index]
    renderer._draw_skeleton(img, lm.norm, img.shape[1], img.shape[0])
    if view is CameraView.DOWN_THE_LINE:
        renderer._draw_horizon(img)
    renderer._draw_label(img, f"#{ft_event.index} f{ft_event.frame_index} {ft_event.timestamp:.2f}s")
    buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), cfg.JPEG_QUALITY])[1]
    return buf.tobytes()

cmp_83 = render_with_frame(frames_bgr.get(83) or np.zeros((meta.height, meta.width, 3), dtype=np.uint8))
# 帧 83 不在解码集；直接用 grab_frames 取
raw83 = frame_reader.grab_frames(path, [83]).get(83)
cmp_83 = render_with_frame(raw83) if raw83 is not None else None
raw101 = frame_reader.grab_frames(path, [101]).get(101)
cmp_101 = render_with_frame(raw101) if raw101 is not None else None
raw74 = frame_reader.grab_frames(path, [74]).get(74)
cmp_74 = render_with_frame(raw74) if raw74 is not None else None

print(f"FT pipeline image == render(frame83)? {cmp_83 is not None and ft_pipe == cmp_83}")
print(f"FT pipeline image == render(frame101)? {cmp_101 is not None and ft_pipe == cmp_101}")
print(f"FT pipeline image == render(frame74)? {cmp_74 is not None and ft_pipe == cmp_74}")
