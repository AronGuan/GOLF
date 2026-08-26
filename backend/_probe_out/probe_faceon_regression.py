"""正面样本回归探针：确认 view=face-on 时 ⑤（及全部 8 事件）与历史逐字节一致。

历史基线 = 改造前 ``segment_swing``（无 view 参数，⑤ 恒用 ``H_DOWNSWING=0.50``）。
改造后 face-on 分支：``view is not DOWN_THE_LINE -> config.H_DOWNSWING``，与历史
同一阈值同一代码路径，应逐字节一致。

本探针对 4 段 face-on 样本（正面1/2/3 + 22030124）跑主链路等价流程：
    segment_swing(view=FACE_ON) -> clublite refine -> reanchor(view=FACE_ON)
输出用户可见最终 ④⑤⑥ + estimated，并断言与「不传 view」（历史默认路径）完全一致。
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

from app import (  # noqa: E402
    config,
    impact_refiner,
    pose_extractor,
    segmenter,
    view_detector,
)
from app.schemas import (  # noqa: E402
    AnalysisError,
    CameraView,
    PhaseKey,
)

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")

CASES: list = [
    ("正面1", os.path.join(SAMPLE_DIR, "正面1.mp4")),
    ("正面2", os.path.join(SAMPLE_DIR, "正面2.mp4")),
    ("正面3", os.path.join(SAMPLE_DIR, "正面3.mp4")),
    ("22030124", os.path.join(SAMPLE_DIR, "22030124ed3bce12cdec7c629d0c6cc8.mp4")),
]


def _by_key(events):
    return {e.key: e for e in events}


def main() -> int:
    print(f"{'case':<12s} {'view':<10s} {'④':>3s} {'⑤':>3s} {'⑤est':>5s} {'⑥':>3s} "
          f"{'⑤-④':>4s} {'⑥-⑤':>4s}  default==face?")
    ok = True
    for name, path in CASES:
        try:
            meta = pose_extractor.probe_video(path)
            frames = pose_extractor.extract(path, meta)
            aspect = meta.height / meta.width if meta.width > 0 else 1.0
            sig = segmenter.build_signals(frames, meta.fps, aspect=aspect)
            # 历史默认路径（不传 view）
            ev_default = segmenter.segment_swing(frames, meta.fps, sig=sig)
            # 显式 face-on
            ev_face = segmenter.segment_swing(
                frames, meta.fps, sig=sig, view=CameraView.FACE_ON
            )
            same_segment = [
                (e.key, e.frame_index, e.estimated) for e in ev_default
            ] == [(e.key, e.frame_index, e.estimated) for e in ev_face]

            # 完整主链路（refine + reanchor），face-on 视图
            view = view_detector.detect_view(frames, meta, 0)
            refine = impact_refiner.refine_impact(
                path, frames, ev_face, sig, view, meta, frames_bgr=None
            )
            final = ev_face
            if refine.available and (
                config.CLUBLITE_MIN_SHIFT_FRAMES <= abs(refine.delta_frames)
                <= config.CLUBLITE_MAX_SHIFT_FRAMES
            ):
                rebuilt = segmenter.reanchor_impact(
                    frames, sig, ev_face, refine.new_array_index,
                    view=CameraView.FACE_ON,
                )
                if rebuilt is not None:
                    final = rebuilt
            bk = _by_key(final)
            top = bk[PhaseKey.TOP].array_index
            ds = bk[PhaseKey.DOWNSWING].array_index
            imp = bk[PhaseKey.IMPACT].array_index
            ds_est = bk[PhaseKey.DOWNSWING].estimated
            print(
                f"{name:<12s} {view.value:<10s} {top:>3d} {ds:>3d} "
                f"{'e' if ds_est else ' ':>5s} {imp:>3d} {ds - top:>4d} "
                f"{imp - ds:>4d}  {str(same_segment):>5s}"
            )
            ok = ok and same_segment
        except AnalysisError as exc:
            print(f"{name:<12s} AnalysisError {exc.code.value}: {exc.detail}")
            ok = False
    print(f"\n[verdict] face-on default==explicit: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
