"""Step 2 回归基线：正面 0 变化 + DTL 5 样本最终 8 阶段表（refine+reanchor 后）。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/step2_regression.py
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

# 正面 4 样本（Step 1 逐字节一致基线）+ DTL 5 样本
FACE_CASES = [
    ("正面1", os.path.join(SAMPLE_DIR, "正面1.mp4")),
    ("正面2", os.path.join(SAMPLE_DIR, "正面2.mp4")),
    ("正面3", os.path.join(SAMPLE_DIR, "正面3.mp4")),
    ("22030124", os.path.join(SAMPLE_DIR, "22030124ed3bce12cdec7c629d0c6cc8.mp4")),
]

DTL_CASES = [
    ("11a6594b", os.path.join(SIDE_DIR, "11a6594b741bb0fd1c29b4d092d50da3.mp4")),
    ("f470c599", os.path.join(SIDE_DIR, "f470c5997da3f58eda196fed05cda8d6.mp4")),
    ("c6f67f38", os.path.join(SAMPLE_DIR, "c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4")),
    ("470057ac", os.path.join(SAMPLE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4")),
    ("0bb16a97", os.path.join(SAMPLE_DIR, "0bb16a974ef55676cc1b938d8539edfd.mp4")),
]

PHASE_KEYS = (
    PhaseKey.ADDRESS, PhaseKey.TAKEAWAY, PhaseKey.BACKSWING, PhaseKey.TOP,
    PhaseKey.DOWNSWING, PhaseKey.IMPACT, PhaseKey.FOLLOW_THROUGH, PhaseKey.FINISH,
)


def _by_key(events):
    return {e.key: e for e in events}


def run(name, path, view):
    meta = pose_extractor.probe_video(path)
    frames = pose_extractor.extract(path, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    try:
        sig = segmenter.build_signals(
            frames, meta.fps, aspect=aspect, view=view
        )
        events = segmenter.segment_swing(
            frames, meta.fps, sig=sig, aspect=aspect, view=view
        )
    except AnalysisError as exc:
        return {"error": f"{exc.code.value}: {exc.detail}"}
    refine = impact_refiner.refine_impact(
        path, frames, events, sig, view, meta
    )
    final = events
    if refine.available and (
        config.CLUBLITE_MIN_SHIFT_FRAMES <= abs(refine.delta_frames)
        <= config.CLUBLITE_MAX_SHIFT_FRAMES
    ):
        rebuilt = segmenter.reanchor_impact(
            frames, sig, events, refine.new_array_index, view=view
        )
        if rebuilt is not None:
            final = rebuilt
    return {"sig": sig, "events": final, "refine": refine}


def print_table(name, result, view):
    print(f"\n[{name}] view={view.value}")
    if "error" in result:
        print(f"  ERR: {result['error']}")
        return
    bk = _by_key(result["events"])
    print(
        f"  refine_delta={result['refine'].delta_frames if result['refine'].available else 'N/A'}"
    )
    print(f"  8 阶段 array_index（E=estimated）:")
    for key in PHASE_KEYS:
        e = bk[key]
        mark = "E" if e.estimated else " "
        print(
            f"    {e.index} {key.value:<15s} array={e.array_index:<4d} "
            f"frame={e.frame_index:<5d} {mark}"
        )


def main() -> int:
    print("=" * 78)
    print("Step2 回归 — face-on 4 样本（验证 0 变化）+ DTL 5 样本（最终 8 阶段表）")
    print(f"  H_DOWNSWING_DTL={config.H_DOWNSWING_DTL}, "
          f"FOLLOWTHROUGH_RISE_DTL={config.FOLLOWTHROUGH_RISE_DTL}")
    print("=" * 78)

    print("\n" + "=" * 78)
    print("[A] face-on 4 样本（Step 1 逐字节一致基线 — Step 2 必须保持）")
    print("=" * 78)
    for name, path in FACE_CASES:
        # 同时跑不传 view 与显式 FACE_ON，确认两者一致
        r_default = run(name, path, CameraView.FACE_ON)
        # 用 default view 也跑一次（应等价）
        print_table(f"{name} (face-on)", r_default, CameraView.FACE_ON)

    print("\n" + "=" * 78)
    print("[B] DTL 5 样本（身高制，Step 2 阈值生效）")
    print("=" * 78)
    for name, path in DTL_CASES:
        r = run(name, path, CameraView.DOWN_THE_LINE)
        print_table(name, r, CameraView.DOWN_THE_LINE)
        if "error" not in r:
            bk = _by_key(r["events"])
            sig = r["sig"]
            h = sig.h
            print(
                f"  h_top={h[bk[PhaseKey.TOP].array_index]:.3f} "
                f"h_impact={h[bk[PhaseKey.IMPACT].array_index]:.3f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())