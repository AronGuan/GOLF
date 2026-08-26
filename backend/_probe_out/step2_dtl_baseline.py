"""Step2 基线：5 个 DTL 样本（身高制）当前 8 阶段帧位 + h 关键值。

只读探针，不改主链路。对每个 DTL 样本：
1. 抽取 pose（一次）；
2. 用身高标尺构建信号（view=DTL，与 pipeline Step 1 后一致）；
3. 跑 segment_swing(view=DTL) + clublite refine + reanchor(view=DTL)，
   输出用户可见最终 8 阶段帧位；
4. 输出 h 轨迹关键值：Address h / 顶点 h / impact h / 送杆窗 h 最小、
   ⑤⑦ 当前帧位与 estimated 标志。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/step2_dtl_baseline.py
"""

from __future__ import annotations

import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

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
SIDE_DIR = os.path.join(SAMPLE_DIR, "侧面")

#: Step2 任务指定的 5 段 DTL 样本
CASES: list = [
    ("11a6594b", os.path.join(SIDE_DIR, "11a6594b741bb0fd1c29b4d092d50da3.mp4")),
    ("f470c599", os.path.join(SIDE_DIR, "f470c5997da3f58eda196fed05cda8d6.mp4")),
    ("c6f67f38", os.path.join(SAMPLE_DIR, "c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4")),
    ("0bb16a97", os.path.join(SAMPLE_DIR, "0bb16a974ef55676cc1b938d8539edfd.mp4")),
    ("470057ac", os.path.join(SAMPLE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4")),
]

PHASE_ORDER_KEYS = [
    PhaseKey.ADDRESS,
    PhaseKey.TAKEAWAY,
    PhaseKey.BACKSWING,
    PhaseKey.TOP,
    PhaseKey.DOWNSWING,
    PhaseKey.IMPACT,
    PhaseKey.FOLLOW_THROUGH,
    PhaseKey.FINISH,
]


def _by_key(events):
    return {e.key: e for e in events}


def load(name: str, path: str):
    meta = pose_extractor.probe_video(path)
    frames = pose_extractor.extract(path, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    sig_face = segmenter.build_signals(frames, meta.fps, aspect=aspect)
    events_face = segmenter.segment_swing(frames, meta.fps, sig=sig_face)
    bk = _by_key(events_face)
    addr_index = bk[PhaseKey.ADDRESS].array_index
    view = view_detector.detect_view(frames, meta, addr_index)
    return {
        "name": name,
        "path": path,
        "meta": meta,
        "frames": frames,
        "aspect": aspect,
        "view": view,
        "sig_face": sig_face,
        "addr_index": addr_index,
        "events_face": events_face,
    }


def run_dtl(loaded, do_refine: bool = True):
    """身高标尺 + view=DTL 切分（可选 refine+reanchor）。"""
    frames = loaded["frames"]
    meta = loaded["meta"]
    sig = segmenter.build_signals(frames, meta.fps, aspect=loaded["aspect"], view=CameraView.DOWN_THE_LINE)
    events = segmenter.segment_swing(
        frames, meta.fps, sig=sig, aspect=loaded["aspect"], view=CameraView.DOWN_THE_LINE
    )
    refine = None
    if do_refine:
        refine = impact_refiner.refine_impact(
            loaded["path"], frames, events, sig, CameraView.DOWN_THE_LINE, meta
        )
        if refine.available and (
            config.CLUBLITE_MIN_SHIFT_FRAMES <= abs(refine.delta_frames)
            <= config.CLUBLITE_MAX_SHIFT_FRAMES
        ):
            rebuilt = segmenter.reanchor_impact(
                frames, sig, events, refine.new_array_index,
                view=CameraView.DOWN_THE_LINE,
            )
            if rebuilt is not None:
                events = rebuilt
    return sig, events, refine


def main() -> int:
    start = time.time()
    for name, path in CASES:
        print(f"\n{'=' * 78}\n[{name}] {os.path.basename(path)}")
        try:
            loaded = load(name, path)
        except AnalysisError as exc:
            print(f"  [load] AnalysisError {exc.code.value}: {exc.detail}")
            continue
        print(
            f"  view={loaded['view'].value} n={len(loaded['frames'])} "
            f"fps={loaded['meta'].fps} aspect={loaded['aspect']:.3f} "
            f"S_face={loaded['sig_face'].S:.4f}"
        )

        sig, events, refine = run_dtl(loaded)
        bk = _by_key(events)
        print(
            f"  S_dtl(身高)={sig.S:.4f}  "
            f"h_max={float(np.nanmax(sig.h)):.3f} h_min={float(np.nanmin(sig.h)):.3f}"
        )
        if refine is not None:
            print(
                f"  refine: available={refine.available} "
                f"delta={refine.delta_frames if refine.available else None}"
            )

        i_addr = bk[PhaseKey.ADDRESS].array_index
        i_top = bk[PhaseKey.TOP].array_index
        i_impact = bk[PhaseKey.IMPACT].array_index
        i_finish = bk[PhaseKey.FINISH].array_index
        i_take = bk[PhaseKey.TAKEAWAY].array_index
        i_ds = bk[PhaseKey.DOWNSWING].array_index
        i_ft = bk[PhaseKey.FOLLOW_THROUGH].array_index

        h = sig.h
        print(
            f"  h 关键值: Address={h[i_addr]:.3f} Top={h[i_top]:.3f} "
            f"Impact={h[i_impact]:.3f} 送杆窗min={float(np.min(h[i_impact:i_finish + 1])):.3f}"
        )
        # 送杆窗内 h 最小点后的上升情况（FOLLOWTHROUGH_RISE 判据数据）
        seg_h = h[i_impact:i_finish + 1]
        k_min = int(np.argmin(seg_h))
        h_min_val = float(seg_h[k_min])
        print(
            f"  送杆窗: h_min@impact+{k_min} (={h_min_val:.3f}), "
            f"h_max_in_window={float(np.max(seg_h)):.3f}, "
            f"当前阈值 h_min+{config.FOLLOWTHROUGH_RISE:.3f}={h_min_val + config.FOLLOWTHROUGH_RISE:.3f}"
        )

        print("  8 阶段帧位 (raw array_index, estimated):")
        for key in PHASE_ORDER_KEYS:
            e = bk[key]
            marker = "E" if e.estimated else " "
            print(
                f"    {e.index} {key.value:<15s} array={e.array_index:<4d} "
                f"frame={e.frame_index:<5d} {marker}"
            )
        gaps = {
            "②-①": i_take - i_addr,
            "③-②": bk[PhaseKey.BACKSWING].array_index - i_take,
            "④-③": i_top - bk[PhaseKey.BACKSWING].array_index,
            "⑤-④": i_ds - i_top,
            "⑥-⑤": i_impact - i_ds,
            "⑦-⑥": i_ft - i_impact,
            "⑧-⑦": i_finish - i_ft,
        }
        print("  间距: " + "  ".join(f"{k}={v}" for k, v in gaps.items()))
        est_flags = {k: bk[k].estimated for k in PHASE_ORDER_KEYS}
        print(f"  estimated: { {k.value: v for k, v in est_flags.items()} }")

    print(f"\n[elapsed] {time.time() - start:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
