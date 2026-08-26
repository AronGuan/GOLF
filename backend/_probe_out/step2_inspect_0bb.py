"""检查 0bb16a97 的 h/speed 轨迹，理解身高制下 h 为何这么小。"""

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

PATH = os.path.join(SAMPLE_DIR, "0bb16a974ef55676cc1b938d8539edfd.mp4")


def main() -> int:
    meta = pose_extractor.probe_video(PATH)
    frames = pose_extractor.extract(PATH, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    sig = segmenter.build_signals(
        frames, meta.fps, aspect=aspect, view=CameraView.DOWN_THE_LINE
    )
    n = sig.n
    print(f"n={n} fps={meta.fps} S={sig.S:.4f} aspect={aspect:.3f}")
    print("帧  h      speed   wrist_y hip_mid_y")
    for k in range(0, n, 10):
        print(
            f"{k:3d} {sig.h[k]:+.3f} {sig.speed[k]:6.3f} "
            f"{sig.wrist_y[k]:.4f} {sig.hip_mid_y[k]:.4f}"
        )
    # 分位数
    print("\nh 分位数:", np.percentile(sig.h, [1, 5, 25, 50, 75, 95, 99]))
    print("h max @", int(np.argmax(sig.h)), "=", float(np.max(sig.h)))
    print("speed max @", int(np.argmax(sig.speed)), "=", float(np.max(sig.speed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
