"""机位感知改造前快照：捕获 face-on / DTL 样本当前击球帧（速度峰路径）。

只读探针，在修改 ``locate_impact`` 前运行，记录：
  - face-on：穿越点 i_cross、当前 i_impact（速度峰）、estimated；
  - DTL：穿越点 i_cross、当前 i_impact（速度峰）、estimated。
输出 JSON 供改造后对比（正面必须逐字节一致；DTL 期望 i_impact -> i_cross）。
"""

from __future__ import annotations

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app import pose_extractor, segmenter  # noqa: E402
from app.schemas import AnalysisError, CameraView  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")

CASES = [
    ("正面1", os.path.join(SAMPLE_DIR, "正面1.mp4"), CameraView.FACE_ON),
    ("正面2", os.path.join(SAMPLE_DIR, "正面2.mp4"), CameraView.FACE_ON),
    ("正面3", os.path.join(SAMPLE_DIR, "正面3.mp4"), CameraView.FACE_ON),
    ("22030124", os.path.join(SAMPLE_DIR, "22030124ed3bce12cdec7c629d0c6cc8.mp4"), CameraView.FACE_ON),
    ("11a6594b", os.path.join(SAMPLE_DIR, "侧面", "11a6594b741bb0fd1c29b4d092d50da3.mp4"), CameraView.DOWN_THE_LINE),
    ("f470c599", os.path.join(SAMPLE_DIR, "侧面", "f470c5997da3f58eda196fed05cda8d6.mp4"), CameraView.DOWN_THE_LINE),
    ("dtl_143", os.path.join(SAMPLE_DIR, "侧面", "微信视频2026-08-26_104443_143.mp4"), CameraView.DOWN_THE_LINE),
]


def locate_impact_inspect(sig, i_top, i_addr):
    """复刻 locate_impact 内部状态（不依赖改造）：返回穿越点/速度峰两条候选。"""
    import numpy as np
    from app import config

    fe = sig.fps_eff
    n = sig.n
    min_gap = max(2, int(round(config.MIN_IMPACT_TOP_SEC * fe)))
    span = max(min_gap + 1, int(round(config.MAX_DOWNSWING_SEC * fe)))
    hi = min(n, i_top + 1 + span)
    if hi <= i_top + 1:
        hi = min(n, i_top + 2)
    h_addr = float(sig.h[i_addr])
    window = sig.h[i_top + 1 : hi]
    crossed = np.where(window <= h_addr + config.IMPACT_Y_TOL)[0]
    if crossed.size > 0:
        i_cross = int(i_top + 1 + crossed[0])
        radius = max(1, int(round(config.IMPACT_WIN_SEC * fe)))
        a = max(i_top + 1, i_cross - radius)
        b = min(hi, i_cross + radius + 1)
        if b <= a:
            b = min(hi, a + 1)
        i_peak = a + int(np.argmax(sig.speed[a:b]))
        return i_cross, i_peak, False, hi
    i_peak = i_top + 1 + int(np.argmax(sig.speed[i_top + 1 : hi]))
    return None, i_peak, True, hi


def main() -> int:
    out = {}
    print(f"{'case':<10s} {'view':<14s} {'i_cross':>7s} {'i_impact(速度峰)':>14s} {'estimated':>9s}")
    for name, path, view in CASES:
        try:
            meta = pose_extractor.probe_video(path)
            frames = pose_extractor.extract(path, meta)
            aspect = meta.height / meta.width if meta.width > 0 else 1.0
            sig = segmenter.build_signals(frames, meta.fps, aspect=aspect)
            i_top = segmenter.locate_top(sig)
            i_addr, _ = segmenter.locate_address(sig, i_top)
            i_cross, i_peak, est, hi = locate_impact_inspect(sig, i_top, i_addr)
            print(f"{name:<10s} {view.value:<14s} {str(i_cross):>7s} {i_peak:>14d} {str(est):>9s}")
            out[name] = {
                "view": view.value,
                "i_top": i_top,
                "i_addr": i_addr,
                "i_cross": i_cross,
                "i_impact_current": i_peak,
                "estimated_current": est,
                "hi": hi,
                "fps": meta.fps,
                "n": sig.n,
            }
        except AnalysisError as exc:
            print(f"{name:<10s} AnalysisError {exc.code.value}: {exc.detail}")
            out[name] = {"error": f"{exc.code.value}: {exc.detail}"}
    with open(os.path.join(BASE_DIR, "_probe_out", "probe_impact_view_before.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n[written] probe_impact_view_before.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
