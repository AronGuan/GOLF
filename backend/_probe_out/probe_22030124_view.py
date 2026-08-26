"""22030124 机位判定专项探针：打印 Address 帧肩宽/身高比与判定细节。

回答：22030124 到底算 face-on 还是 DTL？
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

from app import config, geometry, pose_extractor, segmenter, view_detector  # noqa: E402
from app.schemas import PhaseKey  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
PATH = os.path.join(
    PROJECT_ROOT, ".tools", "_probe", "samples",
    "22030124ed3bce12cdec7c629d0c6cc8.mp4",
)


def main() -> int:
    meta = pose_extractor.probe_video(PATH)
    frames = pose_extractor.extract(PATH, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    sig = segmenter.build_signals(frames, meta.fps, aspect=aspect)
    events = segmenter.segment_swing(frames, meta.fps, sig=sig)
    bk = {e.key: e for e in events}
    addr_idx = bk[PhaseKey.ADDRESS].array_index
    print(f"meta: {meta.width}x{meta.height} fps={meta.fps} frames={len(frames)}")
    print(f"address array_index={addr_idx} frame={bk[PhaseKey.ADDRESS].frame_index}")

    # 直接打印肩宽/身高比
    ratio = view_detector._shoulder_height_ratio(frames, meta, addr_idx)
    print(f"shoulder/height ratio @ address = {ratio:.4f}")
    print(f"VIEW_SHOULDER_RATIO_DTL = {config.VIEW_SHOULDER_RATIO_DTL}")

    norm = frames[addr_idx].norm
    left = np.array([norm[geometry.L_SHOULDER, 0] * meta.width,
                     norm[geometry.L_SHOULDER, 1] * meta.height])
    right = np.array([norm[geometry.R_SHOULDER, 0] * meta.width,
                      norm[geometry.R_SHOULDER, 1] * meta.height])
    shoulder_px = float(np.linalg.norm(left - right))
    nose_y = float(norm[geometry.NOSE, 1]) * meta.height
    ankle_mid_y = (
        float(norm[geometry.L_ANKLE, 1]) + float(norm[geometry.R_ANKLE, 1])
    ) / 2.0 * meta.height
    height_px = geometry.body_height_px(nose_y, ankle_mid_y)
    print(f"shoulder_px={shoulder_px:.1f} height_px={height_px:.1f} "
          f"ratio={shoulder_px / height_px:.4f}")

    det = view_detector.detect_view(frames, meta, addr_idx)
    print(f"detect_view = {det.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
