"""⑤下杆阈值微调：dump 用户样本 22030124 的 h 轨迹（顶点→击球段）。

只读探针，不改主链路。输出：
- 原始 segment_swing 的 ④⑤⑥
- 全链路（clublite refine + reanchor）后的最终 ④⑤⑥（= 用户可见帧号）
- 顶点→击球窗口内逐帧 h 值（array 下标），并标出 H_HIP=0.18 / H_DOWNSWING=0.05 的首次下穿点
"""

from __future__ import annotations

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

from app import config, impact_refiner, pose_extractor, segmenter, view_detector
from app.schemas import AnalysisError, CameraView, PhaseKey

VIDEO = r"C:\Users\98025\Desktop\视频\22030124ed3bce12cdec7c629d0c6cc8.mp4"


def by_key(events):
    return {e.key: e for e in events}


def main() -> int:
    meta = pose_extractor.probe_video(VIDEO)
    frames = pose_extractor.extract(VIDEO, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    sig = segmenter.build_signals(frames, meta.fps, aspect=aspect)

    # 1) 原始切分
    events = segmenter.segment_swing(frames, meta.fps, sig=sig)
    bk = by_key(events)
    top0 = bk[PhaseKey.TOP].array_index
    imp0 = bk[PhaseKey.IMPACT].array_index
    ds0 = bk[PhaseKey.DOWNSWING].array_index
    print(f"[raw  segment_swing] top={top0} ds={ds0}(est={bk[PhaseKey.DOWNSWING].estimated}) impact={imp0}")

    # 2) 全链路：clublite refine + reanchor（用户可见最终帧）
    view = view_detector.detect_view(frames, meta, bk[PhaseKey.ADDRESS].array_index)
    refine = impact_refiner.refine_impact(VIDEO, frames, events, sig, view, meta)
    new_events = events
    new_impact_idx = imp0
    if refine.available and abs(refine.delta_frames) <= config.CLUBLITE_MAX_SHIFT_FRAMES:
        rebuilt = segmenter.reanchor_impact(frames, sig, events, refine.new_array_index)
        if rebuilt is not None:
            new_events = rebuilt
            new_impact_idx = refine.new_array_index
    bk2 = by_key(new_events)
    top1 = bk2[PhaseKey.TOP].array_index
    imp1 = bk2[PhaseKey.IMPACT].array_index
    ds1 = bk2[PhaseKey.DOWNSWING].array_index
    print(f"[final pipeline   ] top={top1} ds={ds1}(est={bk2[PhaseKey.DOWNSWING].estimated}) impact={imp1} "
          f"(raw impact {imp0} -> refined {new_impact_idx}, delta={new_impact_idx - imp0})")

    # 3) 窗口内逐帧 h
    i_top, i_imp = top1, imp1
    print(f"\n[h dump] window array indices [{i_top}, {i_imp}]  (S={sig.S:.4f})")
    print(f"{'idx':>5} {'frame':>5} {'h':>8} {'h*S':>8}  marks")
    for i in range(max(0, i_top - 2), min(sig.n, i_imp + 3)):
        h = float(sig.h[i])
        marks = []
        if i == i_top:
            marks.append("TOP")
        if i == ds1:
            marks.append("DS_final")
        if i == i_imp:
            marks.append("IMPACT")
        if i > i_top and h <= config.H_HIP and (i == i_top + 1 or float(sig.h[i - 1]) > config.H_HIP):
            marks.append(f"cross_H_HIP({config.H_HIP})")
        if i > i_top and h <= 0.05 and (i == i_top + 1 or float(sig.h[i - 1]) > 0.05):
            marks.append("cross_DS(0.05)")
        print(f"{i:>5} {int(frames[i].frame_index):>5} {h:>8.4f} {h * sig.S:>8.4f}  {' '.join(marks)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AnalysisError as exc:
        print(f"[AnalysisError] {exc.code.value}: {exc.detail}")
        raise SystemExit(1)
