"""计算 5 个 DTL 样本的 wrist travel（身高制），确定守卫阈值安全范围。"""

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

from app import pose_extractor, segmenter  # noqa: E402
from app.schemas import CameraView  # noqa: E402

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


def main() -> int:
    print(f"{'case':<10s} {'S_dtl':>7s} {'travel_px':>10s} {'travel_h':>9s} "
          f"{'thr_strict':>11s} {'thr_relax':>10s}")
    for name, path in CASES:
        meta = pose_extractor.probe_video(path)
        frames = pose_extractor.extract(path, meta)
        aspect = meta.height / meta.width if meta.width > 0 else 1.0
        sig = segmenter.build_signals(
            frames, meta.fps, aspect=aspect, view=CameraView.DOWN_THE_LINE
        )
        travel = float(np.percentile(sig.wrist_y, 95) - np.min(sig.wrist_y))
        travel_h = travel / sig.S
        thr_strict = 1.07 * 0.26 * sig.S  # 0.278 heights
        thr_relax = 0.15 * sig.S  # 0.15 heights proposed
        print(
            f"{name:<10s} {sig.S:>7.4f} {travel:>10.4f} {travel_h:>9.4f} "
            f"{thr_strict:>11.4f} {thr_relax:>10.4f}"
        )


if __name__ == "__main__":
    raise SystemExit(main())