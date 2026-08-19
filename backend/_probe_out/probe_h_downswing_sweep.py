"""⑤下杆阈值微调：12 段真实视频全链路回归（候选值扫描）。

对 12 段真实视频（10 samples + 2 video）跑与主链路一致的完整流程：
    切分(segment_swing) -> clublite 击球帧校正(refine) -> reanchor 重建
对每个候选 H_DOWNSWING 值输出**用户可见最终帧号** ④⑤⑥，并校验
``④ < ⑤ < ⑥``（⑤ 不撞 ④ 顶点 / ⑥ 击球）。

只读探针，不改主链路；pose 抽取与 refine 解码每段只做一次（候选间复用），
候选仅重跑纯函数 segment_swing + reanchor（秒级）。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/probe_h_downswing_sweep.py
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
    frame_reader,
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
VIDEO_DIR = os.path.join(PROJECT_ROOT, "video")

#: (显示名, 绝对路径, 机位标注)
CASES: list = [
    ("正面1", os.path.join(SAMPLE_DIR, "正面1.mp4"), "face-on"),
    ("正面2", os.path.join(SAMPLE_DIR, "正面2.mp4"), "face-on"),
    ("正面3", os.path.join(SAMPLE_DIR, "正面3.mp4"), "face-on"),
    ("DTL-087d40a0", os.path.join(SAMPLE_DIR, "087d40a0e808f2c319b8097d89599780.mp4"), "dtl"),
    ("DTL-0bb16a97", os.path.join(SAMPLE_DIR, "0bb16a974ef55676cc1b938d8539edfd.mp4"), "dtl"),
    ("DTL-470057ac", os.path.join(SAMPLE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4"), "dtl"),
    ("DTL-4e8d0d7e", os.path.join(SAMPLE_DIR, "4e8d0d7e517a67a2a7698fd1536289eb.mp4"), "dtl"),
    ("DTL-707fb04a", os.path.join(SAMPLE_DIR, "707fb04a3dbd91db19b97e0ca4aee959.mp4"), "dtl"),
    ("DTL-c6f67f38", os.path.join(SAMPLE_DIR, "c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4"), "dtl"),
    ("DTL-22030124", os.path.join(SAMPLE_DIR, "22030124ed3bce12cdec7c629d0c6cc8.mp4"), "dtl"),
    ("VID-1446d1b9", os.path.join(VIDEO_DIR, "1446d1b95c4329272f1818d6990f3c4f.mp4"), "dtl"),
    ("VID-a4fba3d2", os.path.join(VIDEO_DIR, "a4fba3d24cf9beb59f9d3b06be26daab.mp4"), "dtl"),
]

CANDIDATES = [0.18, 0.50]  # 0.18 = 旧基线（H_HIP 语义，⑤ 读 H_DOWNSWING 后等价复现）；0.50 = 采用值


def _view_of(chosen: str) -> CameraView:
    return CameraView.DOWN_THE_LINE if chosen == "dtl" else CameraView.FACE_ON


def _by_key(events):
    return {e.key: e for e in events}


def _load(name: str, path: str, chosen: str):
    """pose 抽取 + 原始切分 + refine（一次性，候选间复用）。"""
    meta = pose_extractor.probe_video(path)
    frames = pose_extractor.extract(path, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    sig = segmenter.build_signals(frames, meta.fps, aspect=aspect)
    events = segmenter.segment_swing(frames, meta.fps, sig=sig)
    bk = _by_key(events)
    view = _view_of(chosen)
    refine = impact_refiner.refine_impact(
        path, frames, events, sig, view, meta,
        frames_bgr=None,
    )
    return {
        "name": name, "path": path, "meta": meta, "frames": frames, "sig": sig,
        "events": events, "view": view, "refine": refine,
        "raw_top": bk[PhaseKey.TOP].array_index,
        "raw_impact": bk[PhaseKey.IMPACT].array_index,
        "raw_finish": bk[PhaseKey.FINISH].array_index,
    }


def _final_for(loaded, threshold: float) -> dict:
    """用给定阈值跑 segment_swing + reanchor，返回最终 ④⑤⑥。"""
    config.H_DOWNSWING = threshold
    try:
        sig = loaded["sig"]
        frames = loaded["frames"]
        events = segmenter.segment_swing(frames, loaded["meta"].fps, sig=sig)
        new_events = events
        refine = loaded["refine"]
        if refine.available and (
            config.CLUBLITE_MIN_SHIFT_FRAMES <= abs(refine.delta_frames)
            <= config.CLUBLITE_MAX_SHIFT_FRAMES
        ):
            rebuilt = segmenter.reanchor_impact(
                frames, sig, events, refine.new_array_index
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
            "monotonic": top < ds < imp < fin,
        }
    except AnalysisError as exc:
        return {"error": f"{exc.code.value}: {exc.detail}"}


def main() -> int:
    start = time.time()
    loaded_list = []
    for name, path, chosen in CASES:
        try:
            loaded = _load(name, path, chosen)
            loaded_list.append(loaded)
            print(f"[load] {name:<14s} top={loaded['raw_top']} impact={loaded['raw_impact']} "
                  f"refine_delta={loaded['refine'].delta_frames if loaded['refine'].available else None}")
        except AnalysisError as exc:
            print(f"[load] {name:<14s} AnalysisError {exc.code.value}: {exc.detail}")
    print(f"[load] done in {time.time() - start:.1f}s\n")

    header = f"{'case':<14s}" + "".join(f"  {c:>5.2f} ④⑤⑥" for c in CANDIDATES)
    print(header)
    ok_all = True
    for loaded in loaded_list:
        name = loaded["name"]
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
                    f"  {c:>5.2f} {res['top']:>3}/{res['ds']:>3}{est}/{res['imp']:>3}{tag}"
                )
        print(f"{name:<14s}" + "".join(cells))

    # 恢复默认（回写 config，避免影响后续进程）
    config.H_DOWNSWING = CANDIDATES[0]
    print(f"\n[verdict] all_monotonic={ok_all} (elapsed {time.time() - start:.1f}s)")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
