"""导出切分信号曲线，用于定位锚点失效的根因。

用法::

    python backend/_probe_out/dump_signals.py 正面1 DTL-087d40a0 ...
"""

from __future__ import annotations

import json
import os
import sys
from typing import List

import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config, pose_extractor, segmenter  # noqa: E402

sys.path.insert(0, os.path.join(BASE_DIR, "_probe_out"))
from probe_all import CASES, OUT_DIR  # noqa: E402


def local_peaks(speed: np.ndarray, min_value: float) -> List[int]:
    """速度序列上高于 ``min_value`` 的局部极大点下标。"""
    out: List[int] = []
    for i in range(1, len(speed) - 1):
        if speed[i] >= speed[i - 1] and speed[i] > speed[i + 1] and speed[i] >= min_value:
            out.append(i)
    return out


def main() -> int:
    """入口。"""
    wanted = set(sys.argv[1:])
    payload = {}
    for name, path, view in CASES:
        if wanted and name not in wanted:
            continue
        if not os.path.exists(path):
            continue
        meta = pose_extractor.probe_video(path)
        frames = pose_extractor.extract(path, meta)
        aspect = meta.height / meta.width if meta.width > 0 else 1.0
        sig = segmenter.build_signals(frames, meta.fps, aspect=aspect)
        speed = np.asarray(sig.speed)
        wrist_y = np.asarray(sig.wrist_y)
        h = np.asarray(sig.h)

        peaks = local_peaks(speed, config.V_PEAK_MIN)
        print(f"\n===== {name} ({view}) n={sig.n} S={sig.S:.5f} fps_eff={sig.fps_eff:.1f}")
        print(f"  speed: max={speed.max():.2f} @ idx={int(speed.argmax())}")
        print(f"  V_STILL={config.V_STILL}  V_PEAK_MIN={config.V_PEAK_MIN}")
        print(f"  wrist_y: min={wrist_y.min():.4f} @ idx={int(wrist_y.argmin())} "
              f"max={wrist_y.max():.4f}")
        print(f"  局部速度峰(>{config.V_PEAK_MIN}): "
              + ", ".join(f"{i}({speed[i]:.1f})" for i in peaks[:40]))
        # 每 5% 打点，快速看形状
        step = max(1, sig.n // 40)
        print("  idx   speed  wrist_y   h")
        for i in range(0, sig.n, step):
            print(f"  {i:4d}  {speed[i]:6.2f}  {wrist_y[i]:7.4f}  {h[i]:6.2f}")

        payload[name] = {
            "n": sig.n,
            "S": sig.S,
            "fps_eff": sig.fps_eff,
            "speed": [round(float(v), 3) for v in speed],
            "wrist_y": [round(float(v), 4) for v in wrist_y],
            "h": [round(float(v), 3) for v in h],
            "shoulder_mid_y": [round(float(v), 4) for v in sig.shoulder_mid_y],
            "peaks": peaks,
        }

    out = os.path.join(OUT_DIR, "signals.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    print(f"\nJSON -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
