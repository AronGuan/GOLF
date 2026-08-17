"""ClubLite 击球帧校正真实视频实测（ARCHITECTURE-v3-clublite.md §5 T04）。

对 11 段真实视频（3 正面 + 6 DTL + 2 DTL 补充）跑完整 pipeline（含校正），
输出逐段 delta 表，供 VALIDATION-CLUBLITE.md 校准。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/probe_clublite.py

与 :func:`app.pipeline._run` 的差异：
    1. **不删除源视频**（pipeline._cleanup_upload 会删素材，实测绝不能用）；
    2. 单独 reset frame_reader 统计，逐段记录 opens / 墙钟增量；
    3. 记录校正前后 impact 帧号与 reanchor 后的事件一致性。

属临时探针产物，不进主链路。
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

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
    metrics,
    pose_extractor,
    segmenter,
    view_detector,
)
from app.schemas import (  # noqa: E402
    AnalysisError,
    CameraView,
    PHASE_ORDER,
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
    ("VID-1446d1b9", os.path.join(VIDEO_DIR, "1446d1b95c4329272f1818d6990f3c4f.mp4"), "dtl"),
    ("VID-a4fba3d2", os.path.join(VIDEO_DIR, "a4fba3d24cf9beb59f9d3b06be26daab.mp4"), "dtl"),
]


def _view_of(chosen: str) -> CameraView:
    return (
        CameraView.DOWN_THE_LINE
        if chosen == "dtl"
        else CameraView.FACE_ON
    )


def probe_one(name: str, path: str, chosen_view: str) -> Dict[str, Any]:
    """单个视频跑 切分 -> 校正 全流程，任何失败捕获成结构化记录。"""
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
        record["meta"] = {
            "fps": meta.fps,
            "duration": meta.duration,
            "width": meta.width,
            "height": meta.height,
            "frame_count": meta.frame_count,
            "sample_step": meta.sample_step,
            "low_fps": meta.low_fps,
        }
    except AnalysisError as exc:
        record["stage_failed"] = "probe_video"
        record["error_code"] = exc.code.value
        record["error_detail"] = exc.detail
        return record

    try:
        pose_extractor.check_brightness(path)
    except AnalysisError as exc:
        record["stage_failed"] = "check_brightness"
        record["error_code"] = exc.code.value
        record["error_detail"] = exc.detail
        return record

    try:
        frames = pose_extractor.extract(path, meta)
    except AnalysisError as exc:
        record["stage_failed"] = "extract"
        record["error_code"] = exc.code.value
        record["error_detail"] = exc.detail
        return record

    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    try:
        signals = segmenter.build_signals(frames, meta.fps, aspect=aspect)
        events = segmenter.segment_swing(frames, meta.fps, sig=signals)
    except AnalysisError as exc:
        record["stage_failed"] = "segment_swing"
        record["error_code"] = exc.code.value
        record["error_detail"] = exc.detail
        return record

    record["segment_ok"] = True
    impact_old = next(e for e in events if e.key is PhaseKey.IMPACT)
    record["old_impact"] = {
        "array_index": impact_old.array_index,
        "frame_index": impact_old.frame_index,
        "timestamp": impact_old.timestamp,
    }
    record["events_before"] = [
        {
            "key": e.key.value,
            "array_index": e.array_index,
            "frame_index": e.frame_index,
            "estimated": e.estimated,
        }
        for e in events
    ]

    # ---- 机位解析（与 pipeline 同口径）-----------------------------------
    addr_index = next(
        (e.array_index for e in events if e.key is PhaseKey.ADDRESS), 0
    )
    view, view_warning = view_detector.resolve(
        _view_of(chosen_view), frames, meta, addr_index
    )
    meta.camera_view = view
    record["view"] = view.value
    record["view_warning"] = view_warning

    # ---- 校正 --------------------------------------------------------------
    t_refine_start = time.time()
    if config.CLUBLITE_ENABLED:
        cand_frames, decode_frames = impact_refiner.plan_refine_frames(
            events, signals, meta, frames=frames
        )
        # QA P1 修复：预计算 reanchor 可能产出的事件帧并入解码集（opens=1）
        possible_frames = impact_refiner.plan_reanchor_frames(
            events, signals, meta, frames=frames, cand_frames=cand_frames
        )
        record["window"] = {
            "candidate_frames": cand_frames,
            "decode_frames": decode_frames,
            "possible_frames": possible_frames,
            "n_window_frames": len(cand_frames),
            "n_union_frames": len(
                set([e.frame_index for e in events])
                | set(decode_frames) | set(possible_frames)
            ),
        }
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
        record["refine"] = {
            "available": refine.available,
            "method": refine.method,
            "old_array_index": refine.old_array_index,
            "new_array_index": refine.new_array_index,
            "delta_frames": refine.delta_frames,
            "confidence": round(refine.confidence, 4),
            "ball_detected": refine.ball_detected,
            "motion_peak_index": refine.motion_peak_index,
            "shaft_lowest_index": refine.shaft_lowest_index,
        }
        new_events: Optional[List[SwingEvent]] = None
        if refine.available and (
            config.CLUBLITE_MIN_SHIFT_FRAMES
            <= abs(refine.delta_frames)
            <= config.CLUBLITE_MAX_SHIFT_FRAMES
        ):
            new_events = segmenter.reanchor_impact(
                frames, signals, events, refine.new_array_index
            )
            record["reanchor_ok"] = new_events is not None
            if new_events is not None:
                events = new_events
                impact_new = next(
                    e for e in events if e.key is PhaseKey.IMPACT
                )
                record["new_impact"] = {
                    "array_index": impact_new.array_index,
                    "frame_index": impact_new.frame_index,
                    "timestamp": impact_new.timestamp,
                    "estimated": impact_new.estimated,
                }
                record["events_after"] = [
                    {
                        "key": e.key.value,
                        "array_index": e.array_index,
                        "frame_index": e.frame_index,
                        "estimated": e.estimated,
                    }
                    for e in events
                ]
                # P1 回归：校正后 8 事件帧必须全部在解码集内（无渲染 fallback）
                record["all_events_decoded"] = all(
                    e.frame_index in frames_bgr for e in events
                )
        else:
            record["reanchor_ok"] = False
    else:
        record["refine"] = {"available": False, "reason": "CLUBLITE_ENABLED=False"}

    record["refine_wall_sec"] = round(time.time() - t_refine_start, 3)
    record["opens"] = frame_reader.stats()["opens"]
    record["elapsed_sec"] = round(time.time() - started, 2)

    # ---- 校正后事件单调性校验 ---------------------------------------------
    indices = [e.array_index for e in events]
    record["monotonic_after"] = all(
        b > a for a, b in zip(indices, indices[1:])
    )
    record["ok"] = True
    return record


def main() -> int:
    """入口。"""
    parser = argparse.ArgumentParser(description="ClubLite 真实视频校正探针")
    parser.add_argument("--only", default="", help="逗号分隔的用例名过滤")
    parser.add_argument("--tag", default="clublite", help="输出文件名后缀标签")
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
                "traceback": traceback.format_exc(),
            }
        results.append(record)
        _print_brief(record)

    out_path = os.path.join(OUT_DIR, f"probe_{args.tag}.json")
    with io.open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    print(f"\n{'#' * 78}")
    print("逐段 delta 表（校正后 impact 相对校正前）:")
    print(f"{'名称':<14s} {'机位':<6s} {'old':>4s} {'new':>4s} {'delta':>6s} "
          f"{'method':<13s} {'conf':>6s} {'ball':>5s} {'opens':>5s}")
    g1 = 0
    seg_ok = 0
    deltas: List[int] = []
    for r in results:
        if not r.get("segment_ok"):
            print(f"{r['name']:<14s} {'--':<6s} FAIL "
                  f"{r.get('stage_failed')}/{r.get('error_code')}")
            continue
        seg_ok += 1
        refine = r.get("refine") or {}
        old = (r.get("old_impact") or {}).get("array_index", -1)
        new = (r.get("new_impact") or {}).get("array_index", old)
        delta = new - old if r.get("reanchor_ok") else 0
        if refine.get("available") and r.get("reanchor_ok"):
            g1 += 1
        if r.get("reanchor_ok"):
            deltas.append(delta)
        print(
            f"{r['name']:<14s} {r.get('view','--'):<6s} {old:>4d} {new:>4d} "
            f"{delta:>+6d} {str(refine.get('method','none')):<13s} "
            f"{refine.get('confidence',0.0):>6.2f} "
            f"{str(refine.get('ball_detected',False)):>5s} {r.get('opens',0):>5d}"
        )
    print(f"\n切分成功 {seg_ok}/{len(results)}；G1 校正成功 {g1}/{seg_ok}")
    decoded_ok = sum(1 for r in results if r.get("all_events_decoded"))
    print(
        f"校正后 8 事件帧全在解码集（无渲染 fallback）: "
        f"{decoded_ok}/{g1}"
    )
    if deltas:
        print(
            f"delta 分布: min={min(deltas)} max={max(deltas)} "
            f"mean={sum(deltas)/len(deltas):.2f} "
            f"正移数={sum(1 for d in deltas if d > 0)} "
            f"∈[-2,+12] 比例={sum(1 for d in deltas if -2 <= d <= 12)/len(deltas)*100:.0f}%"
        )
    print(f"JSON -> {out_path}")
    return 0


def _print_brief(record: Dict[str, Any]) -> None:
    """控制台简报。"""
    if record.get("meta"):
        m = record["meta"]
        print(
            f"  meta: {m['width']}x{m['height']} fps={m['fps']} "
            f"dur={m['duration']}s frames={m['frame_count']} step={m['sample_step']}"
        )
    if not record.get("segment_ok"):
        print(
            f"  >>> FAILED at {record.get('stage_failed')}: "
            f"{record.get('error_code')} - {record.get('error_detail')}"
        )
        return
    old = (record.get("old_impact") or {}).get("array_index", -1)
    new = (record.get("new_impact") or {}).get("array_index", old)
    refine = record.get("refine") or {}
    print(
        f"  impact {old} -> {new} (delta={new - old:+d}) "
        f"available={refine.get('available')} method={refine.get('method')} "
        f"conf={refine.get('confidence')} ball={refine.get('ball_detected')} "
        f"opens={record.get('opens')} refine_wall={record.get('refine_wall_sec')}s"
    )


if __name__ == "__main__":
    raise SystemExit(main())
