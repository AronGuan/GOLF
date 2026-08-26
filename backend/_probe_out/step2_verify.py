"""Step2 验证：5 个 DTL 样本在 Step 2 阈值下的最终 8 阶段表。"""

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

import numpy as np  # noqa: E402

from app import (  # noqa: E402
    config,
    impact_refiner,
    pose_extractor,
    segmenter,
    view_detector,
)
from app.schemas import (  # noqa: E402
    AnalysisError,
    CameraView,
    PhaseKey,
)

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")
SIDE_DIR = os.path.join(SAMPLE_DIR, "侧面")

CASES = [
    ("11a6594b", os.path.join(SIDE_DIR, "11a6594b741bb0fd1c29b4d092d50da3.mp4")),
    ("f470c599", os.path.join(SIDE_DIR, "f470c5997da3f58eda196fed05cda8d6.mp4")),
    ("c6f67f38", os.path.join(SAMPLE_DIR, "c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4")),
    ("470057ac", os.path.join(SAMPLE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4")),
    ("0bb16a97", os.path.join(SAMPLE_DIR, "0bb16a974ef55676cc1b938d8539edfd.mp4")),
]


def _by_key(events):
    return {e.key: e for e in events}


def main() -> int:
    print("=" * 78)
    print("Step2 验证 5 DTL × 最终 8 阶段表（Step 2 阈值：H_DOWNSWING_DTL=0.40, FOLLOWTHROUGH_RISE_DTL=0.10）")
    print(f"  H_DOWNSWING_DTL={config.H_DOWNSWING_DTL}, FOLLOWTHROUGH_RISE_DTL={config.FOLLOWTHROUGH_RISE_DTL}")
    print("=" * 78)

    for name, path in CASES:
        print(f"\n[{name}]")
        meta = pose_extractor.probe_video(path)
        frames = pose_extractor.extract(path, meta)
        aspect = meta.height / meta.width if meta.width > 0 else 1.0
        try:
            sig = segmenter.build_signals(
                frames, meta.fps, aspect=aspect, view=CameraView.DOWN_THE_LINE
            )
            events = segmenter.segment_swing(
                frames, meta.fps, sig=sig, aspect=aspect,
                view=CameraView.DOWN_THE_LINE,
            )
        except AnalysisError as exc:
            print(f"  AnalysisError {exc.code.value}: {exc.detail}")
            print(f"  （注：身高制守卫误判/正确拒绝 — 视频挥杆幅度不足）")
            continue
        refine = impact_refiner.refine_impact(
            path, frames, events, sig, CameraView.DOWN_THE_LINE, meta
        )
        final = events
        if refine.available and (
            config.CLUBLITE_MIN_SHIFT_FRAMES <= abs(refine.delta_frames)
            <= config.CLUBLITE_MAX_SHIFT_FRAMES
        ):
            rebuilt = segmenter.reanchor_impact(
                frames, sig, events, refine.new_array_index,
                view=CameraView.DOWN_THE_LINE,
            )
            if rebuilt is not None:
                final = rebuilt
        bk = _by_key(final)

        print(f"  view=DTL refine_delta={refine.delta_frames if refine.available else 'N/A'}")
        print(
            f"  h_top={sig.h[bk[PhaseKey.TOP].array_index]:.3f} "
            f"h_impact={sig.h[bk[PhaseKey.IMPACT].array_index]:.3f}"
        )
        print("  8 阶段 array_index（E=estimated）:")
        for key in (
            PhaseKey.ADDRESS, PhaseKey.TAKEAWAY, PhaseKey.BACKSWING,
            PhaseKey.TOP, PhaseKey.DOWNSWING, PhaseKey.IMPACT,
            PhaseKey.FOLLOW_THROUGH, PhaseKey.FINISH,
        ):
            e = bk[key]
            mark = "E" if e.estimated else " "
            print(
                f"    {e.index} {key.value:<15s} array={e.array_index:<4d} "
                f"frame={e.frame_index:<5d} {mark}"
            )
        gaps = {
            "②-①": bk[PhaseKey.TAKEAWAY].array_index - bk[PhaseKey.ADDRESS].array_index,
            "④-③": bk[PhaseKey.TOP].array_index - bk[PhaseKey.BACKSWING].array_index,
            "⑤-④": bk[PhaseKey.DOWNSWING].array_index - bk[PhaseKey.TOP].array_index,
            "⑥-⑤": bk[PhaseKey.IMPACT].array_index - bk[PhaseKey.DOWNSWING].array_index,
            "⑦-⑥": bk[PhaseKey.FOLLOW_THROUGH].array_index - bk[PhaseKey.IMPACT].array_index,
            "⑧-⑦": bk[PhaseKey.FINISH].array_index - bk[PhaseKey.FOLLOW_THROUGH].array_index,
        }
        print("  间距: " + "  ".join(f"{k}={v}" for k, v in gaps.items()))


if __name__ == "__main__":
    main()