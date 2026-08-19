"""⑦送杆判据回归探针：「腕升髋线」→「h 局部最小值」改前/改后对照（全链路）。

对 12 段真实视频（10 samples + 2 video，含用户样本 22030124）跑 **与主链路一致**
的完整流程:
    切分(segment_swing) -> clublite 击球帧校正(refine) -> reanchor 重建
输出:
- 锚点 top / impact（原始 + 校正后）
- ⑦送杆：原始切分 / 校正重建后（= 用户看到的最终帧号）
- 改前⑦（旧判据：``h`` 首次上穿 ``H_HIP``）与 改后⑦（新判据：``h`` 窗口内
  ``np.argmin`` 局部最小值，含「严格晚于击球」守卫）的**内联复刻**
  （用校正后的 impact 作窗口下界，与 reanchor 同口径）
- 单调性 / 范围校验：``impact < ⑦ < finish`` 且 8 事件严格递增

判据在**本探针内联复刻**，因此改代码前后运行结果可直接对比；改代码后再用
「pipeline ⑦ == 改后⑦」的一致性校验实现正确性。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/probe_follow_through.py [--only 名称]

属临时探针产物，不进主链路。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

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
    PhaseKey,
    SwingEvent,
)

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")
VIDEO_DIR = os.path.join(PROJECT_ROOT, "video")
OUT_DIR = os.path.join(BASE_DIR, "_probe_out")

#: (显示名, 绝对路径, 机位标注)
CASES: List[tuple] = [
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


def _view_of(chosen: str):
    from app.schemas import CameraView

    return CameraView.DOWN_THE_LINE if chosen == "dtl" else CameraView.FACE_ON


def _ft_old(sig, i_impact: int, i_finish: int) -> Tuple[int, bool]:
    """复刻旧判据：手腕首次上穿髋线（改前 ``segmenter`` ⑦ 逻辑）。"""
    idx: Optional[int] = None
    if i_finish > i_impact:
        idx = segmenter._first_rising_cross(
            sig.h[i_impact : i_finish + 1], config.H_HIP, i_impact
        )
    if idx is None:
        return (
            segmenter._ratio_frame(i_impact, i_finish, config.FALLBACK_RATIO[3]),
            True,
        )
    return idx, False


def _ft_new(sig, i_impact: int, i_finish: int) -> Tuple[int, bool]:
    """复刻新判据：``h`` 在击球后短窗 ``(i_impact, i_impact+W]`` 内局部最小值。

    - 窗口从 ``i_impact + 1`` 起搜，天然保证 ``⑦ >= impact + 1``；
    - ``W = max(2, round(_FOLLOW_MIN_WIN_SEC * fps_eff))``（30fps ≈ 5 帧）；
    - 不取全窗全局 argmin——送杆/收杆后期腕位再次下探会把 ⑦ 甩到收杆前。
    """
    idx: Optional[int] = None
    if i_finish > i_impact:
        w = max(2, int(round(segmenter._FOLLOW_MIN_WIN_SEC * sig.fps_eff)))
        window_h = sig.h[i_impact + 1 : min(i_finish + 1, i_impact + 1 + w)]
        if len(window_h) > 0:
            idx = i_impact + 1 + int(np.argmin(window_h))
    if idx is None:
        return (
            segmenter._ratio_frame(i_impact, i_finish, config.FALLBACK_RATIO[3]),
            True,
        )
    return idx, False


def _check(ev: List[SwingEvent]) -> Dict[str, Any]:
    idx = [e.array_index for e in ev]
    byk = {e.key: e.array_index for e in ev}
    return {
        "monotonic": all(b > a for a, b in zip(idx, idx[1:])),
        "in_range": byk[PhaseKey.IMPACT] < byk[PhaseKey.FOLLOW_THROUGH] < byk[PhaseKey.FINISH],
    }


def probe_one(name: str, path: str, chosen_view: str) -> Dict[str, Any]:
    """单个视频：全链路切分 + 改前/改后 ⑦ 复刻对照。"""
    record: Dict[str, Any] = {
        "name": name,
        "path": path,
        "chosen_view": chosen_view,
        "ok": False,
        "stage_failed": None,
        "error_code": None,
        "error_detail": None,
    }
    started = time.time()
    frame_reader.reset_stats()

    try:
        meta = pose_extractor.probe_video(path)
        pose_extractor.check_brightness(path)
        frames = pose_extractor.extract(path, meta)
    except AnalysisError as exc:
        record["stage_failed"] = "extract"
        record["error_code"] = exc.code.value
        record["error_detail"] = exc.detail
        record["ok"] = True
        record["elapsed_sec"] = round(time.time() - started, 2)
        return record

    record["meta"] = {
        "fps": meta.fps,
        "width": meta.width,
        "height": meta.height,
        "frame_count": meta.frame_count,
        "sample_step": meta.sample_step,
    }
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    try:
        signals = segmenter.build_signals(frames, meta.fps, aspect=aspect)
        events = segmenter.segment_swing(frames, meta.fps, sig=signals)
    except AnalysisError as exc:
        record["stage_failed"] = "segment_swing"
        record["error_code"] = exc.code.value
        record["error_detail"] = exc.detail
        record["ok"] = True  # NO_SWING 是合法结果（残缺视频），仍计入对照
        record["elapsed_sec"] = round(time.time() - started, 2)
        return record

    record["signals"] = {"n": signals.n, "S": round(signals.S, 5), "aspect": round(aspect, 4)}
    record["segment_ok"] = True
    events_before = events
    ft_before = next(e for e in events_before if e.key is PhaseKey.FOLLOW_THROUGH)

    # ---- 与主链路一致：clublite 击球帧校正 + reanchor ----------------------
    addr_index = next(
        (e.array_index for e in events if e.key is PhaseKey.ADDRESS), 0
    )
    view, _view_warning = view_detector.resolve(_view_of(chosen_view), frames, meta, addr_index)
    meta.camera_view = view
    record["view"] = view.value

    reanchor_ok = False
    refine_info: Dict[str, Any] = {"available": False}
    if config.CLUBLITE_ENABLED:
        cand_frames, decode_frames = impact_refiner.plan_refine_frames(
            events, signals, meta, frames=frames
        )
        possible_frames = impact_refiner.plan_reanchor_frames(
            events, signals, meta, frames=frames, cand_frames=cand_frames
        )
        frames_bgr = frame_reader.grab_frames(
            path,
            sorted(
                set([e.frame_index for e in events])
                | set(decode_frames) | set(possible_frames)
            ),
        )
        refine = impact_refiner.refine_impact(
            path, frames, events, signals, view, meta, frames_bgr=frames_bgr,
        )
        refine_info = {
            "available": refine.available,
            "method": refine.method,
            "old_array_index": refine.old_array_index,
            "new_array_index": refine.new_array_index,
            "delta_frames": refine.delta_frames,
            "motion_peak_index": refine.motion_peak_index,
            "shaft_lowest_index": refine.shaft_lowest_index,
        }
        if refine.available and (
            config.CLUBLITE_MIN_SHIFT_FRAMES
            <= abs(refine.delta_frames)
            <= config.CLUBLITE_MAX_SHIFT_FRAMES
        ):
            new_events = segmenter.reanchor_impact(
                frames, signals, events, refine.new_array_index
            )
            if new_events is not None:
                events = new_events
                reanchor_ok = True
    record["refine"] = refine_info
    record["reanchor_ok"] = reanchor_ok

    # ---- 最终事件（用户看到的结果）----------------------------------------
    by_key = {e.key: e for e in events}
    ft_final = by_key[PhaseKey.FOLLOW_THROUGH]
    i_top = by_key[PhaseKey.TOP].array_index
    i_addr = by_key[PhaseKey.ADDRESS].array_index
    i_impact_final = by_key[PhaseKey.IMPACT].array_index
    i_finish = by_key[PhaseKey.FINISH].array_index
    i_impact_raw = next(
        e for e in events_before if e.key is PhaseKey.IMPACT
    ).array_index

    # ---- 改前/改后 ⑦ 内联复刻（窗口下界用**校正后** impact，与 reanchor 同口径）
    ft_old_idx, ft_old_est = _ft_old(signals, i_impact_final, i_finish)
    ft_new_idx, ft_new_est = _ft_new(signals, i_impact_final, i_finish)
    # 若 reanchor 未生效，pipeline ⑦ 来自原始切分（窗口下界=原始 impact）
    ft_pipe_impact = i_impact_final if reanchor_ok else i_impact_raw

    record["anchors"] = {
        "top": i_top,
        "impact_raw": i_impact_raw,
        "impact_final": i_impact_final,
        "addr": i_addr,
        "finish": i_finish,
    }
    record["follow_through"] = {
        "pipeline_raw": ft_before.array_index,
        "pipeline_final": ft_final.array_index,
        "pipeline_final_estimated": ft_final.estimated,
        "old": ft_old_idx,
        "old_estimated": ft_old_est,
        "new": ft_new_idx,
        "new_estimated": ft_new_est,
    }
    record["checks"] = {
        "final": _check(events),
        "final_in_range_vs_pipe_impact": (
            ft_pipe_impact < ft_final.array_index < i_finish
        ),
        "replicated_matches_pipeline": (
            ft_new_idx == ft_final.array_index
            and ft_new_est == ft_final.estimated
        ),
    }
    record["ok"] = True
    record["elapsed_sec"] = round(time.time() - started, 2)
    return record


def main() -> int:
    """入口。"""
    parser = argparse.ArgumentParser(description="⑦送杆判据改前/改后对照探针（全链路）")
    parser.add_argument("--only", default="", help="逗号分隔的用例名过滤")
    parser.add_argument("--tag", default="follow_through", help="输出文件名后缀标签")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    wanted = {s.strip() for s in args.only.split(",") if s.strip()}

    results: List[Dict[str, Any]] = []
    for name, path, view in CASES:
        if wanted and name not in wanted:
            continue
        if not os.path.exists(path):
            print(f"[skip] {name}: file not found -> {path}")
            continue
        print(f"\n{'=' * 78}\n[{name}] {view}  {path}\n{'=' * 78}", flush=True)
        try:
            record = probe_one(name, path, view)
        except Exception as exc:  # noqa: BLE001 - 探针必须跑完全部用例
            record = {
                "name": name,
                "path": path,
                "chosen_view": view,
                "ok": False,
                "stage_failed": "unexpected",
                "error_code": type(exc).__name__,
                "error_detail": str(exc),
            }
        results.append(record)
        _print_brief(record)

    out_path = os.path.join(OUT_DIR, f"probe_{args.tag}.json")
    with io.open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    _print_table(results)
    print(f"JSON -> {out_path}")
    return 0


def _print_brief(record: Dict[str, Any]) -> None:
    """控制台简报。"""
    if not record.get("segment_ok"):
        print(
            f"  >>> FAILED at {record.get('stage_failed')}: "
            f"{record.get('error_code')} - {record.get('error_detail')}"
        )
        return
    f = record["follow_through"]
    a = record["anchors"]
    print(
        f"  top={a['top']} imp_raw={a['impact_raw']} imp_final={a['impact_final']} "
        f"(reanchor={record.get('reanchor_ok')}) | "
        f"⑦ raw={f['pipeline_raw']} final={f['pipeline_final']} | "
        f"复刻旧={f['old']}(est={f['old_estimated']}) 新={f['new']}(est={f['new_estimated']})"
    )


def _print_table(results: List[Dict[str, Any]]) -> None:
    """汇总对照表。"""
    print(f"\n{'#' * 78}\n改前/改后 ⑦送杆 对照表（array 下标 = 原始帧号，step=1 样本）")
    print(
        f"{'名称':<14s} {'imp_f':>5s} {'fin':>4s} {'⑦旧':>4s} {'⑦新':>4s} {'Δ':>4s} "
        f"{'pipe旧':>5s} {'pipe新':>5s} {'新-imp':>6s} {'单调':>4s} {'范围':>4s} {'复核':>4s}"
    )
    for r in results:
        if not r.get("segment_ok"):
            print(
                f"{r['name']:<14s} {'--':>5s} {'--':>4s} {'--':>4s} {'--':>4s} {'--':>4s} "
                f"{'--':>5s} {'--':>5s} {'--':>6s} FAIL "
                f"{r.get('stage_failed')}/{r.get('error_code')}"
            )
            continue
        f = r["follow_through"]
        a = r["anchors"]
        c = r["checks"]
        print(
            f"{r['name']:<14s} {a['impact_final']:>5d} {a['finish']:>4d} "
            f"{f['old']:>4d} {f['new']:>4d} {f['new'] - f['old']:>+4d} "
            f"{f['pipeline_raw']:>5d} {f['pipeline_final']:>5d} "
            f"{f['new'] - a['impact_final']:>+6d} "
            f"{str(c['final']['monotonic']):>4s} {str(c['final_in_range_vs_pipe_impact']):>4s} "
            f"{str(c['replicated_matches_pipeline']):>4s}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
