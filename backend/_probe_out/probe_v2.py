"""任务 B/C 实测驱动：对真实挥杆视频跑 v2 完整流水线（机位解析 + 球杆 + 风险）。

与 :func:`app.pipeline._run` 的差异：
    1. **不删除源视频**（pipeline._cleanup_upload 会删素材，实测绝不能用）；
    2. 直接以「ground-truth 机位」作为用户所选机位传入 ``view_detector.resolve``，
       模拟 T5 之后 main.py 的行为（走 state.camera_view）；
    3. 额外输出：机位判定、`swing_plane` / `spine_tilt_change` / `shaft_plane_dev`
       数值与来源、每阶段风险列表、球杆 overall_confidence、肩宽/身高比（校准用）、
       端到端耗时增量。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/probe_v2.py [--render]

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
    club_detector,
    config,
    frame_reader,
    geometry,
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
VIDEO_DIR = os.path.join(PROJECT_ROOT, "video")
OUT_DIR = os.path.join(BASE_DIR, "_probe_out")

#: (显示名, 绝对路径, ground-truth 机位)
CASES: List[tuple] = [
    ("正面1", os.path.join(SAMPLE_DIR, "正面1.mp4"), CameraView.FACE_ON),
    ("正面2", os.path.join(SAMPLE_DIR, "正面2.mp4"), CameraView.FACE_ON),
    ("正面3", os.path.join(SAMPLE_DIR, "正面3.mp4"), CameraView.FACE_ON),
    ("DTL-087d40a0", os.path.join(SAMPLE_DIR, "087d40a0e808f2c319b8097d89599780.mp4"), CameraView.DOWN_THE_LINE),
    ("DTL-0bb16a97", os.path.join(SAMPLE_DIR, "0bb16a974ef55676cc1b938d8539edfd.mp4"), CameraView.DOWN_THE_LINE),
    ("DTL-470057ac", os.path.join(SAMPLE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4"), CameraView.DOWN_THE_LINE),
    ("DTL-4e8d0d7e", os.path.join(SAMPLE_DIR, "4e8d0d7e517a67a2a7698fd1536289eb.mp4"), CameraView.DOWN_THE_LINE),
    ("DTL-707fb04a", os.path.join(SAMPLE_DIR, "707fb04a3dbd91db19b97e0ca4aee959.mp4"), CameraView.DOWN_THE_LINE),
    ("DTL-c6f67f38", os.path.join(SAMPLE_DIR, "c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4"), CameraView.DOWN_THE_LINE),
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


def _shoulder_height_ratio(frame, meta) -> Optional[float]:
    """Address 帧「图像肩宽 / 图像身高」（像素口径，机位校准用）。"""
    norm = frame.norm
    sw = float(
        np.linalg.norm(
            np.array([norm[geometry.L_SHOULDER, 0] * meta.width,
                      norm[geometry.L_SHOULDER, 1] * meta.height])
            - np.array([norm[geometry.R_SHOULDER, 0] * meta.width,
                        norm[geometry.R_SHOULDER, 1] * meta.height])
        )
    )
    nose_y = float(norm[geometry.NOSE, 1]) * meta.height
    ankle_mid_y = (
        float(norm[geometry.L_ANKLE, 1]) + float(norm[geometry.R_ANKLE, 1])
    ) / 2.0 * meta.height
    height_px = geometry.body_height_px(nose_y, ankle_mid_y)
    if sw <= 1e-9 or not math.isfinite(height_px) or height_px <= 1e-9:
        return None
    return round(sw / height_px, 4)


def probe_one(name: str, path: str, chosen_view: CameraView, do_render: bool) -> Dict[str, Any]:
    """单个视频跑 v2 全流程，任何失败都被捕获成结构化记录。"""
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
        "detected_ratio": _shoulder_height_ratio(frames[addr_index], meta),
        "ratio_dtl_threshold": config.VIEW_SHOULDER_RATIO_DTL,
        "warning": view_warning,
    }

    # ---- 共享解码 + 球杆检测 ----------------------------------------------
    event_frames = [e.frame_index for e in events]
    if config.CLUB_ENABLED:
        anchors, targets = club_detector.plan_frames(
            frames, events, meta=meta, budget_bytes=config.DECODE_BYTES_BUDGET
        )
        frames_bgr = frame_reader.grab_frames(
            path, sorted(set(targets) | set(event_frames))
        )
        club = club_detector.detect(
            path, frames, signals, view, meta, events, frames_bgr=frames_bgr
        )
    else:
        frames_bgr = frame_reader.grab_frames(path, event_frames)
        club = None
    frames_bgr = {k: v for k, v in frames_bgr.items() if k in set(event_frames)}

    record["club"] = {
        "enabled": config.CLUB_ENABLED,
        "available": club.available if club else False,
        "overall_confidence": _f(club.overall_confidence) if club else None,
        "club_len_px": _f(club.club_len_px) if club else None,
        "anchors": len(anchors) if config.CLUB_ENABLED else 0,
        "decoded": len(targets) if config.CLUB_ENABLED else len(event_frames),
        "decode_opens": frame_reader.stats()["opens"] - baseline_opens,
    }

    # ---- 指标 + 风险 ------------------------------------------------------
    ctx = metrics.build_context(frames, events, signals, meta, view=view, club=club)
    record["scale"] = {
        "S_world_m": _f(ctx.S),
        "S_px": _f(ctx.S_px),
        "body_h_px": _f(ctx.body_h_px),
        "scale_px": _f(ctx.scale_px),
        "SHOULDER_TO_HEIGHT_RATIO": config.SHOULDER_TO_HEIGHT_RATIO,
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
        "downswing_shaft_plane_dev": next(
            (
                {"value": _f(m.value), "source": m.source.value,
                 "ref": [m.ref_min, m.ref_max]}
                for m in phase_metrics[PhaseKey.DOWNSWING] if m.key == "shaft_plane_dev"
            ),
            None,
        ),
        "follow_through_shoulder_turn": next(
            (_f(m.value) for m in phase_metrics[PhaseKey.FOLLOW_THROUGH]
             if m.key == "shoulder_turn"),
            None,
        ),
        "risks_016": risk_block["follow_through"],
    }

    # ---- 渲染（可选）-------------------------------------------------------
    if do_render:
        render_dir = os.path.join(OUT_DIR, "render_v2", name)
        os.makedirs(render_dir, exist_ok=True)
        try:
            images = renderer.render_events(
                path, events, render_dir, frames, frames_bgr=frames_bgr,
                club=club, view=view,
            )
            record["render"] = {
                "dir": render_dir,
                "files": {k.value: v for k, v in images.items()},
                "all_exist": all(
                    os.path.exists(os.path.join(render_dir, v)) for v in images.values()
                ),
            }
        except Exception as exc:  # noqa: BLE001 - 探针脚本
            record["render"] = {"error": f"{type(exc).__name__}: {exc}"}

    record["ok"] = True
    record["elapsed_sec"] = round(time.time() - started, 2)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="任务 B/C 真实视频 v2 探针")
    parser.add_argument("--render", action="store_true", help="同时渲染 8 张骨架图")
    parser.add_argument("--only", default="", help="逗号分隔的用例名过滤")
    parser.add_argument("--tag", default="v2", help="输出文件名后缀标签")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    wanted = {s.strip() for s in args.only.split(",") if s.strip()}

    results: List[Dict[str, Any]] = []
    for name, path, chosen_view in CASES:
        if wanted and name not in wanted:
            continue
        if not os.path.exists(path):
            print(f"[skip] {name}: file not found -> {path}")
            continue
        print(f"\n{'=' * 78}\n[{name}] {chosen_view.value}  {path}\n{'=' * 78}", flush=True)
        try:
            record = probe_one(name, path, chosen_view, args.render)
        except Exception as exc:  # noqa: BLE001 - 探针脚本必须跑完全部用例
            record = {
                "name": name, "path": path, "ok": False,
                "stage_failed": "unexpected", "error_code": type(exc).__name__,
                "error_detail": str(exc), "traceback": traceback.format_exc(),
            }
        results.append(record)
        _print_brief(record)

    out_path = os.path.join(OUT_DIR, f"probe_{args.tag}.json")
    with io.open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    total = len(results)
    seg_ok = sum(1 for r in results if r.get("ok"))
    print(f"\n{'#' * 78}")
    print(f"流水线成功 {seg_ok}/{total}")
    for r in results:
        flag = "OK " if r.get("ok") else "FAIL"
        extra = ""
        if r.get("ok"):
            km = r.get("key_metrics", {})
            extra = (
                f"view={r['view']['resolved']} "
                f"swing_plane={km.get('top_swing_plane')} "
                f"shaft_dev={km.get('downswing_shaft_plane_dev')} "
                f"risks={r.get('risk_count')}"
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
    print(f"  club: {record['club']}")
    km = record.get("key_metrics", {})
    print(
        f"  swing_plane(④)={km.get('top_swing_plane')} "
        f"spine_tilt_change(⑥)={km.get('impact_spine_tilt_change')} "
        f"shaft_plane_dev(⑤)={km.get('downswing_shaft_plane_dev')}"
    )
    print(f"  follow_through shoulder_turn(⑦)={km.get('follow_through_shoulder_turn')} "
          f"risks_016={km.get('risks_016')}")
    print(f"  risks={record.get('risk_count')} warnings={record.get('warnings')}")


if __name__ == "__main__":
    raise SystemExit(main())
