"""QA：CLUB_ENABLED=False 时主链路完整（球杆相关指标剔除、不冒泡）。"""
from __future__ import annotations
import os, sys, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, metrics, pose_extractor, segmenter, risk_engine, view_detector
from app.schemas import CameraView, PHASE_ORDER, PhaseKey

config.CLUB_ENABLED = False

path = r'E:/project/golf/.tools/_probe/samples/正面1.mp4'
meta = pose_extractor.probe_video(path)
frames = pose_extractor.extract(path, meta)
aspect = meta.height / meta.width
sig = segmenter.build_signals(frames, meta.fps, aspect=aspect)
events = segmenter.segment_swing(frames, meta.fps, sig=sig)
addr = next(e.array_index for e in events if e.key is PhaseKey.ADDRESS)
view, warn = view_detector.resolve(CameraView.FACE_ON, frames, meta, addr)
ctx = metrics.build_context(frames, events, sig, meta, view=view, club=None)
phase_metrics = {}
for key in PHASE_ORDER:
    ctx.phase = key
    phase_metrics[key] = metrics.compute_phase_metrics(ctx)
risk_map = risk_engine.evaluate_all(phase_metrics, view)
total = sum(len(v) for v in risk_map.values())
print('view:', view.value, 'warn:', warn)
print('phase metric counts:', {k.value: len(v) for k, v in phase_metrics.items()})
print('risk total:', total)
assert all(m.key != 'shaft_plane_dev' for items in phase_metrics.values() for m in items), 'CLUB off 不应有 shaft_plane_dev'
assert all(m.key != 'swing_plane' for m in phase_metrics[PhaseKey.TOP]), 'face_on TOP 不应有 swing_plane'
assert total >= 1, 'CLUB off 后风险引擎仍应工作'
print('CLUB_ENABLED=False 链路完整 OK')
