"""检查 4 个 DTL 样本送杆窗 h 轨迹与 FOLLOWTHROUGH_RISE 触发条件。"""

from __future__ import annotations

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

from app import (  # noqa: E402
    config,
    impact_refiner,
    pose_extractor,
    segmenter,
)
from app.schemas import CameraView, PhaseKey  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")
SIDE_DIR = os.path.join(SAMPLE_DIR, "侧面")

CASES = [
    ("11a6594b", os.path.join(SIDE_DIR, "11a6594b741bb0fd1c29b4d092d50da3.mp4")),
    ("f470c599", os.path.join(SIDE_DIR, "f470c5997da3f58eda196fed05cda8d6.mp4")),
    ("c6f67f38", os.path.join(SAMPLE_DIR, "c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4")),
    ("470057ac", os.path.join(SAMPLE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4")),
]


def main() -> int:
    for name, path in CASES:
        print(f"\n=== {name} ===")
        meta = pose_extractor.probe_video(path)
        frames = pose_extractor.extract(path, meta)
        aspect = meta.height / meta.width if meta.width > 0 else 1.0
        sig = segmenter.build_signals(
            frames, meta.fps, aspect=aspect, view=CameraView.DOWN_THE_LINE
        )
        events = segmenter.segment_swing(
            frames, meta.fps, sig=sig, aspect=aspect,
            view=CameraView.DOWN_THE_LINE,
        )
        bk = {e.key: e for e in events}
        i_impact = bk[PhaseKey.IMPACT].array_index
        i_finish = bk[PhaseKey.FINISH].array_index
        print(f"  impact(原始)={i_impact} finish={i_finish} win={i_finish - i_impact + 1}")

        # refine + reanchor
        refine = impact_refiner.refine_impact(
            path, frames, events, sig, CameraView.DOWN_THE_LINE, meta
        )
        ev = events
        if refine.available and (
            config.CLUBLITE_MIN_SHIFT_FRAMES <= abs(refine.delta_frames)
            <= config.CLUBLITE_MAX_SHIFT_FRAMES
        ):
            rebuilt = segmenter.reanchor_impact(
                frames, sig, events, refine.new_array_index,
                view=CameraView.DOWN_THE_LINE,
            )
            if rebuilt is not None:
                ev = rebuilt
        bk = {e.key: e for e in ev}
        i_impact = bk[PhaseKey.IMPACT].array_index
        i_finish = bk[PhaseKey.FINISH].array_index
        print(f"  impact(refined)={i_impact} finish={i_finish} "
              f"refine_delta={refine.delta_frames if refine.available else 0}")

        h = sig.h
        seg_h = h[i_impact:i_finish + 1]
        k_min = int(np.argmin(seg_h))
        h_min_val = float(seg_h[k_min])
        print(f"  送杆窗 h_min @ impact+{k_min} (=frame {i_impact + k_min}) = {h_min_val:.4f}")
        print(f"  送杆窗 h_max @ {i_impact + int(np.argmax(seg_h))} = {float(np.max(seg_h)):.4f}")
        print(f"  窗内 h (frame: h):")
        for k in range(len(seg_h)):
            mark = " <-MIN" if k == k_min else ""
            print(f"    {i_impact + k:3d}: {seg_h[k]:+.4f}{mark}")

        # 对每个候选阈值，检查首次穿越
        for thr in [0.05, 0.10, 0.15, 0.20, 0.30]:
            target = h_min_val + thr
            after = seg_h[k_min + 1:]
            hits = np.where(after >= target)[0]
            if hits.size > 0:
                cand_local = k_min + 1 + int(hits[0])
                cand_global = i_impact + cand_local
                inside = i_impact < cand_global < i_finish
                print(f"  thr={thr:.2f} target={target:.4f} 首次穿越 frame={cand_global} "
                      f"strictly_inside={inside}")
            else:
                print(f"  thr={thr:.2f} target={target:.4f} 无穿越")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())