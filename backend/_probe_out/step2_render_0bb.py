"""渲染 0bb16a97 关键帧（含骨架）供目检。"""

from __future__ import annotations

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app import geometry, pose_extractor  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")
PATH = os.path.join(SAMPLE_DIR, "0bb16a974ef55676cc1b938d8539edfd.mp4")

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_step2_frames")
os.makedirs(OUT_DIR, exist_ok=True)


def main() -> int:
    meta = pose_extractor.probe_video(PATH)
    frames = pose_extractor.extract(PATH, meta)
    cap = cv2.VideoCapture(PATH)
    targets = [0, 9, 90, 160, 183, 185, 190, 196, 205, 209]
    frame_map = {f.frame_index: f for f in frames}
    for t in targets:
        cap.set(cv2.CAP_PROP_POS_FRAMES, t)
        ok, bgr = cap.read()
        if not ok:
            print(f"frame {t}: read fail")
            continue
        f = frame_map.get(t)
        if f is not None:
            h_img, w_img = bgr.shape[:2]
            for i in range(geometry.NUM_LANDMARKS):
                x = int(f.norm[i, 0] * w_img)
                y = int(f.norm[i, 1] * h_img)
                cv2.circle(bgr, (x, y), 4, (0, 90, 255), -1)
            for a, b in geometry.SKELETON_EDGES:
                xa, ya = int(f.norm[a, 0] * w_img), int(f.norm[a, 1] * h_img)
                xb, yb = int(f.norm[b, 0] * w_img), int(f.norm[b, 1] * h_img)
                cv2.line(bgr, (xa, ya), (xb, yb), (0, 255, 180), 2)
        out = os.path.join(OUT_DIR, f"0bb16a97_f{t:03d}.jpg")
        cv2.imwrite(out, bgr)
        print(f"wrote {out}")
    cap.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
