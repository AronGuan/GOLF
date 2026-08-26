"""⑤下杆 DTL 机位阈值校准：4 段样本 × H_DOWNSWING_DTL 候选扫描。

背景：face-on / DTL 人体投影不同，⑤ 判据本应分机位。face-on 恒用
``H_DOWNSWING=0.50``（正面回归逐字节一致，用户已验收）；DTL 用新常量
``H_DOWNSWING_DTL``。因下杆期 ``h`` 单调递减，**阈值越低触发越晚**（偏离顶点、
更靠后、更接近击球）。

本探针对每段样本：
1. view_detector 判定机位（22030124 已确认是 face-on，见 probe_22030124_view.py）；
2. DTL 样本：扫描 H_DOWNSWING_DTL ∈ {0.50, 0.40, 0.30, 0.25, 0.22, 0.20, 0.18, 0.10}，
   每候选跑「segment_swing(view=DTL) -> clublite refine -> reanchor(view=DTL)」，
   输出用户可见最终 ④⑤⑥ 帧号 + ⑤-④ / ⑥-⑤ 间距；
3. face-on 样本：只验证免疫（改 H_DOWNSWING_DTL 不影响 ⑤）。

只读探针，不改主链路；pose 抽取与 refine 解码每段只做一次（候选间复用）。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/probe_h_downswing_dtl_sweep.py
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

#: 任务指定的 4 段侧面样本（22030124 会在运行时由 view_detector 复核机位）；
#: 另附 4e8d0d7e（samples 目录内另一段可切分 DTL 样本）作稳健性补充。
CASES: list = [
    ("0bb16a97", os.path.join(SAMPLE_DIR, "0bb16a974ef55676cc1b938d8539edfd.mp4")),
    ("470057ac", os.path.join(SAMPLE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4")),
    ("c6f67f38", os.path.join(SAMPLE_DIR, "c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4")),
    ("22030124", os.path.join(SAMPLE_DIR, "22030124ed3bce12cdec7c629d0c6cc8.mp4")),
    ("4e8d0d7e", os.path.join(SAMPLE_DIR, "4e8d0d7e517a67a2a7698fd1536289eb.mp4")),
]

CANDIDATES: list = [0.50, 0.40, 0.30, 0.25, 0.22, 0.20, 0.18, 0.10]


def _by_key(events):
    return {e.key: e for e in events}


def _load(name: str, path: str):
    """pose 抽取 + 机位判定 + 原始切分 + refine（一次性，候选间复用）。"""
    meta = pose_extractor.probe_video(path)
    frames = pose_extractor.extract(path, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    sig = segmenter.build_signals(frames, meta.fps, aspect=aspect)
    events = segmenter.segment_swing(frames, meta.fps, sig=sig)
    bk = _by_key(events)
    addr_index = bk[PhaseKey.ADDRESS].array_index
    view = view_detector.detect_view(frames, meta, addr_index)
    refine = impact_refiner.refine_impact(
        path, frames, events, sig, view, meta,
        frames_bgr=None,
    )
    return {
        "name": name, "path": path, "meta": meta, "frames": frames, "sig": sig,
        "events": events, "view": view, "refine": refine,
        "addr_index": addr_index,
        "raw_top": bk[PhaseKey.TOP].array_index,
        "raw_impact": bk[PhaseKey.IMPACT].array_index,
        "raw_finish": bk[PhaseKey.FINISH].array_index,
    }


def _final_for(loaded, threshold: float) -> dict:
    """用给定 H_DOWNSWING_DTL 跑 segment_swing(view) + reanchor(view)。"""
    config.H_DOWNSWING_DTL = threshold
    view = loaded["view"]
    try:
        sig = loaded["sig"]
        frames = loaded["frames"]
        events = segmenter.segment_swing(
            frames, loaded["meta"].fps, sig=sig, view=view
        )
        new_events = events
        refine = loaded["refine"]
        if refine.available and (
            config.CLUBLITE_MIN_SHIFT_FRAMES <= abs(refine.delta_frames)
            <= config.CLUBLITE_MAX_SHIFT_FRAMES
        ):
            rebuilt = segmenter.reanchor_impact(
                frames, sig, events, refine.new_array_index, view=view
            )
            if rebuilt is not None:
                new_events = rebuilt
        bk = _by_key(new_events)
        top = bk[PhaseKey.TOP].array_index
        ds = bk[PhaseKey.DOWNSWING].array_index
        imp = bk[PhaseKey.IMPACT].array_index
        fin = bk[PhaseKey.FINISH].array_index
        ds_est = bk[PhaseKey.DOWNSWING].estimated
        return {
            "top": top, "ds": ds, "imp": imp, "fin": fin, "ds_est": ds_est,
            "gap_td": ds - top,
            "gap_di": imp - ds,
            "monotonic": top < ds < imp < fin,
        }
    except AnalysisError as exc:
        return {"error": f"{exc.code.value}: {exc.detail}"}


def main() -> int:
    start = time.time()
    loaded_list = []
    for name, path in CASES:
        try:
            loaded = _load(name, path)
            loaded_list.append(loaded)
            print(
                f"[load] {name:<12s} view={loaded['view'].value:<16s} "
                f"top={loaded['raw_top']} impact={loaded['raw_impact']} "
                f"refine_delta={loaded['refine'].delta_frames if loaded['refine'].available else None}"
            )
        except AnalysisError as exc:
            print(f"[load] {name:<12s} AnalysisError {exc.code.value}: {exc.detail}")
    print(f"[load] done in {time.time() - start:.1f}s\n")

    header = f"{'case':<14s}" + "".join(
        f"  {c:>5.2f} ④⑤⑥ Δt5 Δi6" for c in CANDIDATES
    )
    print(header)
    ok_all = True
    for loaded in loaded_list:
        name = loaded["name"]
        view = loaded["view"]
        cells = []
        for c in CANDIDATES:
            res = _final_for(loaded, c)
            if "error" in res:
                cells.append(f"  {c:>5.2f}  ERR")
                ok_all = False
            else:
                tag = "" if res["monotonic"] else "!!"
                if not res["monotonic"]:
                    ok_all = False
                est = "e" if res["ds_est"] else " "
                cells.append(
                    f"  {c:>5.2f} {res['top']:>3}/{res['ds']:>3}{est}/{res['imp']:>3}"
                    f" {res['gap_td']:>2} {res['gap_di']:>2}{tag}"
                )
        note = " (face-on, DTL 阈值免疫)" if view is CameraView.FACE_ON else ""
        print(f"{name:<14s}" + "".join(cells) + note)

    # 恢复默认（回写 config，避免影响后续进程）
    config.H_DOWNSWING_DTL = CANDIDATES[0]
    print(f"\n[verdict] all_monotonic={ok_all} (elapsed {time.time() - start:.1f}s)")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
