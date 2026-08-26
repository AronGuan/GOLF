"""DTL 样本深度检查：h 剖面 / 穿越点 / 窗口边界 / 附近帧速度。

只读探针：打印 11a6594b / f470c599（及第三个侧面样本）在
顶点→下杆窗口内的 ``h``、``speed``、``h_addr + IMPACT_Y_TOL`` 等，
用于确认「穿越点是否存在、落在哪」。
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

from app import config, pose_extractor, segmenter  # noqa: E402
from app.schemas import AnalysisError  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")

CASES = [
    ("11a6594b", os.path.join(SAMPLE_DIR, "侧面", "11a6594b741bb0fd1c29b4d092d50da3.mp4")),
    ("f470c599", os.path.join(SAMPLE_DIR, "侧面", "f470c5997da3f58eda196fed05cda8d6.mp4")),
    ("dtl_143", os.path.join(SAMPLE_DIR, "侧面", "微信视频2026-08-26_104443_143.mp4")),
]


def main() -> int:
    for name, path in CASES:
        print(f"\n===== {name} =====")
        try:
            meta = pose_extractor.probe_video(path)
            frames = pose_extractor.extract(path, meta)
            aspect = meta.height / meta.width if meta.width > 0 else 1.0
            sig = segmenter.build_signals(frames, meta.fps, aspect=aspect)
            i_top = segmenter.locate_top(sig)
            i_addr, _ = segmenter.locate_address(sig, i_top)
            fe = sig.fps_eff
            min_gap = max(2, int(round(config.MIN_IMPACT_TOP_SEC * fe)))
            span = max(min_gap + 1, int(round(config.MAX_DOWNSWING_SEC * fe)))
            hi = min(sig.n, i_top + 1 + span)
            if hi <= i_top + 1:
                hi = min(sig.n, i_top + 2)
            h_addr = float(sig.h[i_addr])
            tol = h_addr + config.IMPACT_Y_TOL
            print(f"n={sig.n} fps={meta.fps} fe={fe:.2f} S={sig.S:.4f}")
            print(f"i_top={i_top} i_addr={i_addr} h_addr={h_addr:.4f} tol={tol:.4f}")
            print(f"downswing window=[{i_top + 1}, {hi})  span_frames={hi - i_top - 1}")
            print(f"h min in window={float(np.min(sig.h[i_top + 1:hi])):.4f} at idx="
                  f"{i_top + 1 + int(np.argmin(sig.h[i_top + 1:hi]))}")
            crossed = np.where(sig.h[i_top + 1:hi] <= tol)[0]
            print(f"crossed count={crossed.size}", end="")
            if crossed.size:
                i_cross = int(i_top + 1 + crossed[0])
                print(f" first i_cross={i_cross} (h={float(sig.h[i_cross]):.4f})")
            else:
                print(" (无穿越)")
            # 全局 h 最低点（送杆附近）参考
            i_hmin = int(np.argmin(sig.h))
            print(f"global h min={float(np.min(sig.h)):.4f} at idx={i_hmin}")
            # 顶点→下杆窗口内 h/speed 每 3 帧打印
            print(" idx   h       speed    (窗口内)")
            for i in range(i_top + 1, hi, 3):
                print(f" {i:4d}  {float(sig.h[i]):7.4f}  {float(sig.speed[i]):7.4f}")
        except AnalysisError as exc:
            print(f"AnalysisError {exc.code.value}: {exc.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
