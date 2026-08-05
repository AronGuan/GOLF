"""球杆检测下线后的端到端探针（2026-08）。

对真实挥杆视频跑「无球杆」v2 流水线（机位解析 + 8 阶段 + 指标 + 风险），
验证：
1. ``swing_plane``（PDD 版，纯 MediaPipe）仍在结果里（DTL 段有数值）；
2. 球杆增强指标不在结果里（已下线）；
3. 8 阶段 + 风险引擎正常（正面1 应仍触发 2 条风险）；
4. 管线耗时（球杆检测不再跑，耗时应下降）。

与 :func:`app.pipeline._run` 的差异：
- **不删除源视频**（pipeline._cleanup_upload 会删素材，实测绝不能用）；
- 直接以「ground-truth 机位」作为用户所选机位传入 ``view_detector.resolve``。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/probe_no_club.py
"""

from __future__ import annotations

import io
import json
import math
import os
import sys
import time
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
    metrics,
    pose_extractor,
    renderer,
    risk_engine,
    segmenter,
    view_detector,
)
from app.schemas import (  # noqa: E402
    AnalysisError,
    CameraView,
    PHASE_META,
    PHASE_ORDER,
    PhaseKey,
)

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")
OUT_DIR = os.path.join(BASE_DIR, "_probe_out")

#: (显示名, 绝对路径, ground-truth 机位)
CASES: List[tuple] = [
    ("正面1", os.path.join(SAMPLE_DIR, "正面1.mp4"), CameraView.FACE_ON),
    ("DTL-4e8d0d7e", os.path.join(SAMPLE_DIR, "4e8d0d7e517a67a2a7698fd1536289eb.mp4"), CameraView.DOWN_THE_LINE),
]


def _f(value: Any) -> Optional[float]:
    """把 numpy / NaN 统一转成 JSON 安全的 float 或 None。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return round(out, 4)


def probe_one(name: str, path: str, chosen_view: CameraView) -> Dict[str, Any]:
    """单个视频跑无球杆 v2 全流程，任何失败都被捕获成结构化记录。"""
    record: Dict[str, Any] = {
        "name": name,
        "path": path,
        "chosen_view": chosen_view.value,
        "ok": False,
        "stage_failed": None,
        "error_code": None,
        "error_detail": None,
    }
    started = time.time()
    baseline_opens = frame_reader.stats()["opens"]

    try:
        meta = pose_extractor.probe_video(path)
        record["meta"] = {
            "fps": meta.fps, "duration": meta.duration, "width": meta.width,
            "height": meta.height, "frame_count": meta.frame_count,
            "sample_step": meta.sample_step, "low_fps": meta.low_fps,
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

    detected = sum(1 for f in frames if f.detected)
    record["extract"] = {
        "sampled": len(frames),
        "detected": detected,
        "miss_ratio": round(1.0 - detected / max(1, len(frames)), 4),
    }

    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    try:
        signals = segmenter.build_signals(frames, meta.fps, aspect=aspect)
        events = segmenter.segment_swing(frames, meta.fps, sig=signals)
    except AnalysisError as exc:
        record["stage_failed"] = "segment"
        record["error_code"] = exc.code.value
        record["error_detail"] = exc.detail
        return record

    record["events"] = [
        {"index": e.index, "key": e.key.value, "frame_index": e.frame_index,
         "timestamp": e.timestamp, "estimated": e.estimated}
        for e in events
    ]

    # ---- 机位解析（用户所选 = ground-truth）-------------------------------
    addr_index = next((e.array_index for e in events if e.key is PhaseKey.ADDRESS), 0)
    view, view_warning = view_detector.resolve(chosen_view, frames, meta, addr_index)
    meta.camera_view = view
    record["view"] = {
        "chosen": chosen_view.value,
        "resolved": view.value,
        "warning": view_warning,
    }

    # ---- 解码 8 个事件帧（供 renderer；无球杆不再解码窗口采样帧）----------
    event_frames = [e.frame_index for e in events]
    frames_bgr = frame_reader.grab_frames(path, event_frames)
    frames_bgr = {k: v for k, v in frames_bgr.items() if k in set(event_frames)}
    record["decode"] = {
        "decoded": len(frames_bgr),
        "decode_opens": frame_reader.stats()["opens"] - baseline_opens,
    }

    # ---- 指标 + 风险 ------------------------------------------------------
    ctx = metrics.build_context(frames, events, signals, meta, view=view)
    record["scale"] = {
        "S_world_m": _f(ctx.S),
        "S_px": _f(ctx.S_px),
        "body_h_px": _f(ctx.body_h_px),
        "scale_px": _f(ctx.scale_px),
    }

    phase_block: Dict[str, Any] = {}
    risk_block: Dict[str, Any] = {}
    phase_metrics: Dict[PhaseKey, list] = {}
    for key in PHASE_ORDER:
        ctx.phase = key
        items = metrics.compute_phase_metrics(ctx)
        phase_metrics[key] = items
        phase_block[key.value] = {
            "name_cn": PHASE_META[key].name_cn,
            "metrics": [
                {
                    "key": m.key, "name": m.name, "value": m.value, "unit": m.unit,
                    "ref": [m.ref_min, m.ref_max], "status": m.status.value,
                    "source": m.source.value, "estimated": m.estimated,
                    "confidence": m.confidence, "description": m.description,
                }
                for m in items
            ],
        }
    risk_map = risk_engine.evaluate_all(phase_metrics, view)
    for key in PHASE_ORDER:
        risk_block[key.value] = [
            {
                "rule_id": r.rule_id, "risk_name": r.risk_name,
                "risk_level": r.risk_level.value, "metric_key": r.metric_key,
                "value": r.value, "trigger_description": r.trigger_description,
                "manual_page": r.manual_page,
            }
            for r in risk_map.get(key, [])
        ]
    record["phases"] = phase_block
    record["risks"] = risk_block
    record["risk_count"] = sum(len(v) for v in risk_block.values())
    record["warnings"] = list(ctx.warnings)
    if view_warning and view_warning not in record["warnings"]:
        record["warnings"].insert(0, view_warning)

    # 关键数值提取（方便人工核对）
    record["key_metrics"] = {
        "top_swing_plane": next(
            (_f(m.value) for m in phase_metrics[PhaseKey.TOP] if m.key == "swing_plane"),
            None,
        ),
        "impact_spine_tilt_change": next(
            (_f(m.value) for m in phase_metrics[PhaseKey.IMPACT] if m.key == "spine_tilt_change"),
            None,
        ),
        "follow_through_shoulder_turn": next(
            (_f(m.value) for m in phase_metrics[PhaseKey.FOLLOW_THROUGH]
             if m.key == "shoulder_turn"),
            None,
        ),
        "risks_016": risk_block["follow_through"],
    }
    # 已下线指标应不在任何阶段指标列表里
    record["removed_metric_present"] = any(
        any(m.key == "shaft_plane_dev" for m in items)
        for items in phase_metrics.values()
    )

    record["ok"] = True
    record["elapsed_sec"] = round(time.time() - started, 2)
    return record


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    results: List[Dict[str, Any]] = []
    for name, path, chosen_view in CASES:
        if not os.path.exists(path):
            print(f"[skip] {name}: file not found -> {path}")
            continue
        print(f"\n{'=' * 78}\n[{name}] {chosen_view.value}  {path}\n{'=' * 78}", flush=True)
        try:
            record = probe_one(name, path, chosen_view)
        except Exception as exc:  # noqa: BLE001 - 探针脚本必须跑完全部用例
            record = {
                "name": name, "path": path, "ok": False,
                "stage_failed": "unexpected", "error_code": type(exc).__name__,
                "error_detail": str(exc),
            }
        results.append(record)
        _print_brief(record)

    out_path = os.path.join(OUT_DIR, "probe_no_club.json")
    with io.open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    print(f"\n{'#' * 78}")
    ok = sum(1 for r in results if r.get("ok"))
    print(f"流水线成功 {ok}/{len(results)}")
    for r in results:
        flag = "OK " if r.get("ok") else "FAIL"
        extra = ""
        if r.get("ok"):
            km = r.get("key_metrics", {})
            extra = (
                f"view={r['view']['resolved']} "
                f"swing_plane={km.get('top_swing_plane')} "
                f"risks={r.get('risk_count')} "
                f"removed_metric_present={r.get('removed_metric_present')} "
                f"elapsed={r.get('elapsed_sec')}s"
            )
        else:
            extra = f"{r.get('stage_failed')}/{r.get('error_code')}: {r.get('error_detail')}"
        print(f"  [{flag}] {r['name']:<16s} {extra}")
    print(f"JSON -> {out_path}")
    return 0


def _print_brief(record: Dict[str, Any]) -> None:
    if record.get("meta"):
        m = record["meta"]
        print(f"  meta: {m['width']}x{m['height']} fps={m['fps']} dur={m['duration']}s")
    if not record.get("ok"):
        print(
            f"  >>> FAILED at {record.get('stage_failed')}: "
            f"{record.get('error_code')} - {record.get('error_detail')}"
        )
        return
    print(f"  view: {record['view']}")
    km = record.get("key_metrics", {})
    print(
        f"  swing_plane(④)={km.get('top_swing_plane')} "
        f"spine_tilt_change(⑥)={km.get('impact_spine_tilt_change')} "
        f"follow_through shoulder_turn(⑦)={km.get('follow_through_shoulder_turn')}"
    )
    print(
        f"  risks={record.get('risk_count')} "
        f"removed_metric_present={record.get('removed_metric_present')} "
        f"elapsed={record.get('elapsed_sec')}s"
    )


if __name__ == "__main__":
    raise SystemExit(main())
