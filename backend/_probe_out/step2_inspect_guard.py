"""检查 0bb16a97 / 470057ac 的 wrist_y 轨迹与守卫数据（身高制 NO_SWING 问题）。"""

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

from app import config, pose_extractor, segmenter  # noqa: E402
from app.schemas import CameraView  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")

CASES = [
    ("0bb16a97", os.path.join(SAMPLE_DIR, "0bb16a974ef55676cc1b938d8539edfd.mp4")),
    ("470057ac", os.path.join(SAMPLE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4")),
]


def main() -> int:
    for name, path in CASES:
        print(f"\n=== {name} ===")
        meta = pose_extractor.probe_video(path)
        frames = pose_extractor.extract(path, meta)
        aspect = meta.height / meta.width if meta.width > 0 else 1.0
        sig_face = segmenter.build_signals(frames, meta.fps, aspect=aspect)
        sig_dtl = segmenter.build_signals(
            frames, meta.fps, aspect=aspect, view=CameraView.DOWN_THE_LINE
        )
        travel = float(np.percentile(sig_dtl.wrist_y, 95) - np.min(sig_dtl.wrist_y))
        guard_thr = config.MIN_WRIST_TRAVEL * config.SHOULDER_TO_HEIGHT_RATIO * sig_dtl.S
        print(f"  n={len(frames)} aspect={aspect:.3f}")
        print(f"  S_face(肩宽)={sig_face.S:.4f}  S_dtl(身高)={sig_dtl.S:.4f}")
        print(f"  wrist_y: min={np.min(sig_dtl.wrist_y):.4f} "
              f"p95={np.percentile(sig_dtl.wrist_y, 95):.4f} "
              f"max={np.max(sig_dtl.wrist_y):.4f}")
        print(f"  travel(身高制)={travel:.4f}  guard_thr={guard_thr:.4f}  "
              f"pass={travel >= guard_thr}")
        print(f"  travel/S_dtl(身高单位)={travel / sig_dtl.S:.4f}  "
              f"需 ≥ {config.MIN_WRIST_TRAVEL * config.SHOULDER_TO_HEIGHT_RATIO:.4f}")
        print(f"  travel/S_face(肩宽单位)={travel / sig_face.S:.4f}  "
              f"需 ≥ {config.MIN_WRIST_TRAVEL:.4f}")
        # 找 wrist_y 极值位置
        i_min = int(np.argmin(sig_dtl.wrist_y))
        i_p95 = int(np.argsort(sig_dtl.wrist_y)[int(0.95 * len(sig_dtl.wrist_y))])
        print(f"  wrist_y min @ {i_min} (h={sig_dtl.h[i_min]:.3f})")
        print(f"  wrist_y p95 @ {i_p95} (h={sig_dtl.h[i_p95]:.3f})")
        # 用 speed 峰粗看挥杆位置
        i_peak = int(np.argmax(sig_dtl.speed))
        print(f"  speed 峰 @ {i_peak} (h={sig_dtl.h[i_peak]:.3f})")
        # h 的行程（顶点-站位）
        try:
            i_top = segmenter.locate_top(sig_dtl)
            print(f"  locate_top(身高制) = {i_top} h={sig_dtl.h[i_top]:.3f}")
        except Exception as exc:  # noqa: BLE001
            print(f"  locate_top 异常: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
