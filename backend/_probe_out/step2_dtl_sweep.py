"""Step2 DTL 阈值扫描：H_HIP_DTL / H_DOWNSWING_DTL / FOLLOWTHROUGH_RISE_DTL 候选。

对 4 个有挥杆的 DTL 样本（11a6594b / f470c599 / c6f67f38 / 470057ac）扫描 DTL
专用阈值候选。0bb16a97 经身高制守卫判定为 NO_SWING（h_max=0.116，运动幅度
极小；详见 step2_inspect_guard.py），属正确行为，不参与扫描。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/step2_dtl_sweep.py
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

CASES: list = [
    ("11a6594b", os.path.join(SIDE_DIR, "11a6594b741bb0fd1c29b4d092d50da3.mp4")),
    ("f470c599", os.path.join(SIDE_DIR, "f470c5997da3f58eda196fed05cda8d6.mp4")),
    ("c6f67f38", os.path.join(SAMPLE_DIR, "c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4")),
    ("470057ac", os.path.join(SAMPLE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4")),
]

# 身高制扫描候选（按任务初值 0.18/0.50/0.95 * 0.26 ≈ 0.047/0.13/0.247 为中点
# 上下扩，但实测 h 范围与肩宽制不同，需实测校准）。
H_HIP_DTL_CANDIDATES: list = [0.02, 0.04, 0.06, 0.08, 0.10]
H_DOWNSWING_DTL_CANDIDATES: list = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
FOLLOWTHROUGH_RISE_DTL_CANDIDATES: list = [
    0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50,
]


def _by_key(events):
    return {e.key: e for e in events}


def load(name: str, path: str):
    meta = pose_extractor.probe_video(path)
    frames = pose_extractor.extract(path, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    sig_face = segmenter.build_signals(frames, meta.fps, aspect=aspect)
    return {
        "name": name, "path": path, "meta": meta, "frames": frames,
        "aspect": aspect, "sig_face": sig_face,
    }


def run_final(
    loaded, h_hip_dtl, h_ds_dtl, ft_rise_dtl, do_refine: bool = True
):
    """用三个 DTL 专用阈值跑 segment_swing(view=DTL) + 可选 refine+reanchor。

    返回 dict 含 8 阶段 array_index / estimated / h 关键值。
    """
    config.H_HIP_DTL = float(h_hip_dtl)
    config.H_DOWNSWING_DTL = float(h_ds_dtl)
    config.FOLLOWTHROUGH_RISE_DTL = float(ft_rise_dtl)
    frames = loaded["frames"]
    meta = loaded["meta"]
    sig = segmenter.build_signals(
        frames, meta.fps, aspect=loaded["aspect"], view=CameraView.DOWN_THE_LINE
    )
    try:
        events = segmenter.segment_swing(
            frames, meta.fps, sig=sig, aspect=loaded["aspect"],
            view=CameraView.DOWN_THE_LINE,
        )
    except AnalysisError as exc:
        return {"error": f"{exc.code.value}: {exc.detail}"}
    refine = None
    if do_refine:
        try:
            refine = impact_refiner.refine_impact(
                loaded["path"], frames, events, sig,
                CameraView.DOWN_THE_LINE, meta,
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
        except Exception as exc:  # noqa: BLE001
            refine = None
    bk = _by_key(events)
    i_addr = bk[PhaseKey.ADDRESS].array_index
    i_top = bk[PhaseKey.TOP].array_index
    i_take = bk[PhaseKey.TAKEAWAY].array_index
    i_bs = bk[PhaseKey.BACKSWING].array_index
    i_ds = bk[PhaseKey.DOWNSWING].array_index
    i_impact = bk[PhaseKey.IMPACT].array_index
    i_ft = bk[PhaseKey.FOLLOW_THROUGH].array_index
    i_finish = bk[PhaseKey.FINISH].array_index
    return {
        "addr": i_addr, "top": i_top, "take": i_take, "bs": i_bs,
        "ds": i_ds, "impact": i_impact, "ft": i_ft, "finish": i_finish,
        "take_e": bk[PhaseKey.TAKEAWAY].estimated,
        "ds_e": bk[PhaseKey.DOWNSWING].estimated,
        "ft_e": bk[PhaseKey.FOLLOW_THROUGH].estimated,
        "refine": refine.delta_frames if refine and refine.available else 0,
        "h": {
            "top": float(sig.h[i_top]),
            "impact": float(sig.h[i_impact]),
            "ds": float(sig.h[i_ds]) if 0 <= i_ds < sig.n else None,
            "ft": float(sig.h[i_ft]) if 0 <= i_ft < sig.n else None,
            "ft_window_min": float(np.min(sig.h[i_impact:i_finish + 1])),
            "ft_window_max": float(np.max(sig.h[i_impact:i_finish + 1])),
        },
        "gaps": {
            "t-a": i_top - i_addr,
            "tk-t": i_take - i_top,
            "bs-tk": i_bs - i_take,
            "t-bs": i_top - i_bs,
            "ds-top": i_ds - i_top,
            "imp-ds": i_impact - i_ds,
            "ft-imp": i_ft - i_impact,
            "fin-ft": i_finish - i_ft,
        },
        "monotonic": (
            i_addr < i_take < i_bs < i_top < i_ds < i_impact < i_ft < i_finish
        ),
    }


def fmt_8phases(res):
    if "error" in res:
        return "ERR"
    nums = [res["addr"], res["take"], res["bs"], res["top"],
            res["ds"], res["impact"], res["ft"], res["finish"]]
    flags = [" "]
    flags.append("e" if res["take_e"] else " ")
    flags.append("e" if False else " ")  # backswing always real in this probe
    flags.append(" ")  # top
    flags.append("e" if res["ds_e"] else " ")
    flags.append(" ")  # impact (refined or not, we report array)
    flags.append("e" if res["ft_e"] else " ")
    flags.append(" ")  # finish
    cells = [f"{n}{f}" for n, f in zip(nums, flags)]
    return " ".join(cells)


def main() -> int:
    start = time.time()
    loaded_list = []
    for name, path in CASES:
        loaded_list.append((name, load(name, path)))
    print(f"[load] {len(loaded_list)} cases in {time.time() - start:.1f}s\n")

    # Baseline (current defaults)
    print("=" * 78)
    print("[baseline 当前默认值] view=DTL, 3 阈值保持旧值（H_HIP=0.18, H_DOWNSWING_DTL=0.25, FOLLOWTHROUGH_RISE=0.95）")
    print("  H_HIP_DTL/FOLLOWTHROUGH_RISE_DTL 不存在 ⇒ 走 face-on 共享阈值（Step 1 现状）")
    print("=" * 78)
    print(f"{'case':<10s} {'8 阶段 (①②③④⑤⑥⑦⑧)':<55s} {'top/imp':<12s} estimated(②⑤⑦)")
    for name, loaded in loaded_list:
        res = run_final(
            loaded,
            h_hip_dtl=getattr(config, "H_HIP_DTL", config.H_HIP),
            h_ds_dtl=config.H_DOWNSWING_DTL,
            ft_rise_dtl=getattr(config, "FOLLOWTHROUGH_RISE_DTL", config.FOLLOWTHROUGH_RISE),
        )
        print(
            f"{name:<10s} {fmt_8phases(res):<55s} "
            f"{res['h']['top']:.3f}/{res['h']['impact']:.3f}    "
            f"{res['take_e']}/{res['ds_e']}/{res['ft_e']}"
            if "error" not in res else
            f"{name:<10s} ERR: {res['error']}"
        )

    # Sweep H_HIP_DTL (others at baseline)
    print("\n" + "=" * 78)
    print("[sweep] H_HIP_DTL (影响 ②起杆)")
    print("=" * 78)
    for name, loaded in loaded_list:
        print(f"\n[{name}]")
        print(f"  {'h_hip':>8s}  {'②':>3s}  {'take_e':>6s}  {'take-top':>9s}")
        for c in H_HIP_DTL_CANDIDATES:
            res = run_final(
                loaded, h_hip_dtl=c,
                h_ds_dtl=config.H_DOWNSWING_DTL,
                ft_rise_dtl=getattr(config, "FOLLOWTHROUGH_RISE_DTL", config.FOLLOWTHROUGH_RISE),
            )
            if "error" in res:
                print(f"  {c:>8.3f}  ERR  ({res['error']})")
                continue
            print(
                f"  {c:>8.3f}  {res['take']:>3d}  {str(res['take_e']):>6s}  "
                f"{res['gaps']['tk-t']:>+9d}"
            )

    # Sweep H_DOWNSWING_DTL
    print("\n" + "=" * 78)
    print("[sweep] H_DOWNSWING_DTL (影响 ⑤下杆)")
    print("=" * 78)
    for name, loaded in loaded_list:
        print(f"\n[{name}] h_top={_safe_h(loaded, 'top'):.3f} h_impact={_safe_h(loaded, 'impact'):.3f}")
        print(
            f"  {'h_ds':>8s}  {'④⑤⑥':>9s}  {'ds_e':>5s}  "
            f"{'ds-top':>7s}  {'imp-ds':>7s}  {'ft-imp':>7s}  ft_e"
        )
        for c in H_DOWNSWING_DTL_CANDIDATES:
            res = run_final(
                loaded, h_hip_dtl=getattr(config, "H_HIP_DTL", config.H_HIP),
                h_ds_dtl=c,
                ft_rise_dtl=getattr(config, "FOLLOWTHROUGH_RISE_DTL", config.FOLLOWTHROUGH_RISE),
            )
            if "error" in res:
                print(f"  {c:>8.3f}  ERR  ({res['error']})")
                continue
            tag = "" if res["monotonic"] else "!!"
            print(
                f"  {c:>8.3f}  {res['top']:>3d}/{res['ds']:>3d}/{res['impact']:>3d}  "
                f"{str(res['ds_e']):>5s}  {res['gaps']['ds-top']:>+7d}  "
                f"{res['gaps']['imp-ds']:>+7d}  {res['gaps']['ft-imp']:>+7d}  "
                f"{res['ft_e']}{tag}"
            )

    # Sweep FOLLOWTHROUGH_RISE_DTL
    print("\n" + "=" * 78)
    print("[sweep] FOLLOWTHROUGH_RISE_DTL (影响 ⑦送杆)")
    print("=" * 78)
    for name, loaded in loaded_list:
        print(f"\n[{name}] 送杆窗 h_min={_safe_h_window(loaded):.3f} h_max={_safe_h_window_max(loaded):.3f}")
        print(
            f"  {'ft_r':>8s}  {'⑥⑦':>7s}  {'ft_e':>5s}  {'ft-imp':>7s}  "
            f"{'ft-window-min+thr':>17s}"
        )
        for c in FOLLOWTHROUGH_RISE_DTL_CANDIDATES:
            res = run_final(
                loaded, h_hip_dtl=getattr(config, "H_HIP_DTL", config.H_HIP),
                h_ds_dtl=config.H_DOWNSWING_DTL,
                ft_rise_dtl=c,
            )
            if "error" in res:
                print(f"  {c:>8.3f}  ERR  ({res['error']})")
                continue
            thr = res["h"]["ft_window_min"] + c
            print(
                f"  {c:>8.3f}  {res['impact']:>3d}/{res['ft']:>3d}  "
                f"{str(res['ft_e']):>5s}  {res['gaps']['ft-imp']:>+7d}  "
                f"{thr:>17.3f}"
            )

    print(f"\n[elapsed] {time.time() - start:.1f}s")
    return 0


def _safe_h(loaded, key: str) -> float:
    sig = segmenter.build_signals(
        loaded["frames"], loaded["meta"].fps,
        aspect=loaded["aspect"], view=CameraView.DOWN_THE_LINE,
    )
    events = segmenter.segment_swing(
        loaded["frames"], loaded["meta"].fps, sig=sig,
        aspect=loaded["aspect"], view=CameraView.DOWN_THE_LINE,
    )
    bk = _by_key(events)
    phase = {"top": PhaseKey.TOP, "impact": PhaseKey.IMPACT}[key]
    return float(sig.h[bk[phase].array_index])


def _safe_h_window(loaded) -> float:
    sig = segmenter.build_signals(
        loaded["frames"], loaded["meta"].fps,
        aspect=loaded["aspect"], view=CameraView.DOWN_THE_LINE,
    )
    events = segmenter.segment_swing(
        loaded["frames"], loaded["meta"].fps, sig=sig,
        aspect=loaded["aspect"], view=CameraView.DOWN_THE_LINE,
    )
    bk = _by_key(events)
    return float(np.min(sig.h[bk[PhaseKey.IMPACT].array_index:bk[PhaseKey.FINISH].array_index + 1]))


def _safe_h_window_max(loaded) -> float:
    sig = segmenter.build_signals(
        loaded["frames"], loaded["meta"].fps,
        aspect=loaded["aspect"], view=CameraView.DOWN_THE_LINE,
    )
    events = segmenter.segment_swing(
        loaded["frames"], loaded["meta"].fps, sig=sig,
        aspect=loaded["aspect"], view=CameraView.DOWN_THE_LINE,
    )
    bk = _by_key(events)
    return float(np.max(sig.h[bk[PhaseKey.IMPACT].array_index:bk[PhaseKey.FINISH].array_index + 1]))


if __name__ == "__main__":
    raise SystemExit(main())