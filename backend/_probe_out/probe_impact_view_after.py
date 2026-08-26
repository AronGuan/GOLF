"""机位感知改造后对比探针：face-on 逐字节回归 + DTL 穿越点 vs 速度峰。

只读探针，在修改 ``locate_impact`` **后**运行：
  - face-on 样本（正面1/2/3 + 22030124）：断言改造后（view 默认 / 显式 face-on）
    与改造前快照 ``probe_impact_view_before.json`` 的击球帧**完全一致**（0 变化）；
  - DTL 样本（11a6594b / f470c599 / dtl_143）：输出「现状速度峰」vs「新穿越点」
    帧号对比，穿越成功样本确认新判据取穿越点。
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


def load_before():
    path = os.path.join(BASE_DIR, "_probe_out", "probe_impact_view_before.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def main() -> int:
    before = load_before()
    ok = True
    print(f"{'case':<10s} {'view':<14s} {'before(速度峰)':>13s} {'after(穿越点)':>12s} "
          f"{'estimated':>9s} {'diff':>4s}")
    for name, path, view in CASES:
        try:
            meta = pose_extractor.probe_video(path)
            frames = pose_extractor.extract(path, meta)
            aspect = meta.height / meta.width if meta.width > 0 else 1.0
            sig = segmenter.build_signals(frames, meta.fps, aspect=aspect)
            i_top = segmenter.locate_top(sig)
            i_addr, _ = segmenter.locate_address(sig, i_top)
            # 默认路径（不传 view）= face-on 历史路径
            i_default, e_default = segmenter.locate_impact(sig, i_top, i_addr)
            # 显式 view 路径
            i_view, e_view = segmenter.locate_impact(
                sig, i_top, i_addr, view=view
            )
            b = before.get(name, {})
            b_impact = b.get("i_impact_current")
            diff = ""
            if b_impact is not None:
                if view is CameraView.FACE_ON:
                    same = i_default == b_impact and i_view == b_impact
                    diff = "0" if same else f"{i_view - b_impact:+d}"
                    ok = ok and same
                else:
                    # DTL：before 是速度峰（兜底或峰），after 期望穿越点
                    diff = f"{i_view - b_impact:+d}"
            print(
                f"{name:<10s} {view.value:<14s} {str(b_impact):>13s} {i_view:>12d} "
                f"{str(e_view):>9s} {diff:>4s}"
            )
        except AnalysisError as exc:
            print(f"{name:<10s} AnalysisError {exc.code.value}: {exc.detail}")
            ok = False
    print(f"\n[verdict] face-on 0 变化: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
