"""机位判定探针：对全部 12 段样本跑 view_detector.detect_view。

用途：确认各样本的真实机位分类（face-on / DTL），特别是 22030124。
只读探针，不改主链路。
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

from app import pose_extractor, segmenter, view_detector  # noqa: E402
from app.schemas import PhaseKey  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")
VIDEO_DIR = os.path.join(PROJECT_ROOT, "video")

CASES: list = [
    ("正面1", os.path.join(SAMPLE_DIR, "正面1.mp4")),
    ("正面2", os.path.join(SAMPLE_DIR, "正面2.mp4")),
    ("正面3", os.path.join(SAMPLE_DIR, "正面3.mp4")),
    ("087d40a0", os.path.join(SAMPLE_DIR, "087d40a0e808f2c319b8097d89599780.mp4")),
    ("0bb16a97", os.path.join(SAMPLE_DIR, "0bb16a974ef55676cc1b938d8539edfd.mp4")),
    ("470057ac", os.path.join(SAMPLE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4")),
    ("4e8d0d7e", os.path.join(SAMPLE_DIR, "4e8d0d7e517a67a2a7698fd1536289eb.mp4")),
    ("707fb04a", os.path.join(SAMPLE_DIR, "707fb04a3dbd91db19b97e0ca4aee959.mp4")),
    ("c6f67f38", os.path.join(SAMPLE_DIR, "c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4")),
    ("22030124", os.path.join(SAMPLE_DIR, "22030124ed3bce12cdec7c629d0c6cc8.mp4")),
    ("1446d1b9", os.path.join(VIDEO_DIR, "1446d1b95c4329272f1818d6990f3c4f.mp4")),
    ("a4fba3d2", os.path.join(VIDEO_DIR, "a4fba3d24cf9beb59f9d3b06be26daab.mp4")),
]


def main() -> int:
    for name, path in CASES:
        try:
            meta = pose_extractor.probe_video(path)
            frames = pose_extractor.extract(path, meta)
            aspect = meta.height / meta.width if meta.width > 0 else 1.0
            sig = segmenter.build_signals(frames, meta.fps, aspect=aspect)
            events = segmenter.segment_swing(frames, meta.fps, sig=sig)
            bk = {e.key: e for e in events}
            addr_idx = bk[PhaseKey.ADDRESS].array_index
            det = view_detector.detect_view(frames, meta, addr_idx)
            print(
                f"{name:<12s} w={meta.width} h={meta.height} addr_idx={addr_idx} "
                f"detect_view={det.value}"
            )
        except Exception as exc:  # noqa: BLE001 - 探针要展示所有样本
            print(f"{name:<12s} ERROR {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
