"""任务 A 实测驱动：对真实挥杆视频跑完整闭环并落盘结构化 JSON。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/probe_all.py [--render]

与 :func:`app.pipeline._run` 的差异：
    1. **不删除源视频**（``pipeline._cleanup_upload`` 会删掉素材，实测绝不能用）。
    2. 额外输出「原始未 sanitize 的指标值」与「rotation_xz 未乘 ROTATION_SIGN 的裸值」，
       用于符号与量纲核对。
    3. 不依赖 TaskStore / FastAPI。

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

# Windows 控制台默认 GBK，中文阶段名会炸；统一切 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app import config, geometry, metrics, pose_extractor, renderer, segmenter  # noqa: E402
from app.schemas import (  # noqa: E402
    AnalysisError,
    PHASE_META,
    PHASE_ORDER,
    PhaseKey,
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


def _f(value: Any) -> Optional[float]:
    """把 numpy / NaN 统一转成 JSON 安全的 float 或 None。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return round(out, 4)


def _raw_rotation_xz(v_now: np.ndarray, v_ref: np.ndarray) -> float:
    """``geometry.rotation_xz`` 未乘 :data:`config.ROTATION_SIGN` 的裸值。"""
    x1, z1 = float(v_ref[0]), float(v_ref[2])
    x2, z2 = float(v_now[0]), float(v_now[2])
    if (abs(x1) + abs(z1)) < 1e-9 or (abs(x2) + abs(z2)) < 1e-9:
        return float("nan")
    return float(math.degrees(math.atan2(x1 * z2 - z1 * x2, x1 * x2 + z1 * z2)))


def _view_ratio(frames, addr_index: int) -> Optional[float]:
    """Address 帧「图像肩宽 / 图像身高」，用于机位自动判定核对。"""
    if not (0 <= addr_index < len(frames)):
        return None
    norm = frames[addr_index].norm
    sw = float(
        np.linalg.norm(norm[geometry.L_SHOULDER, :2] - norm[geometry.R_SHOULDER, :2])
    )
    ankle_mid_y = (
        float(norm[geometry.L_ANKLE, 1]) + float(norm[geometry.R_ANKLE, 1])
    ) / 2.0
    height = abs(ankle_mid_y - float(norm[geometry.NOSE, 1]))
    if height < 1e-6:
        return None
    return round(sw / height, 4)


def probe_one(name: str, path: str, view: str, do_render: bool) -> Dict[str, Any]:
    """单个视频跑全流程，任何失败都被捕获成结构化记录。"""
    record: Dict[str, Any] = {
        "name": name,
        "path": path,
        "view": view,
        "ok": False,
        "stage_failed": None,
        "error_code": None,
        "error_detail": None,
    }
    started = time.time()

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

    detected = sum(1 for f in frames if f.detected)
    core = list(geometry.CORE_IDS)
    record["extract"] = {
        "sampled": len(frames),
        "detected": detected,
        "miss_ratio": round(1.0 - detected / max(1, len(frames)), 4),
        "avg_core_vis": round(
            float(np.mean([np.mean(f.visibility[core]) for f in frames])), 4
        ),
    }

    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    try:
        signals = segmenter.build_signals(frames, meta.fps, aspect=aspect)
    except AnalysisError as exc:
        record["stage_failed"] = "build_signals"
        record["error_code"] = exc.code.value
        record["error_detail"] = exc.detail
        return record

    travel = float(np.percentile(signals.wrist_y, 95) - np.min(signals.wrist_y))
    record["signals"] = {
        "aspect": round(aspect, 4),
        "n": signals.n,
        "S": round(signals.S, 5),
        "dt": round(signals.dt, 5),
        "fps_eff": round(signals.fps_eff, 3),
        "speed_max": _f(np.nanmax(signals.speed)),
        "speed_median": _f(np.nanmedian(signals.speed)),
        "wrist_travel": round(travel, 5),
        "wrist_travel_in_S": round(travel / max(signals.S, 1e-9), 4),
        "guard_v_peak_min": config.V_PEAK_MIN,
        "guard_min_travel_in_S": config.MIN_WRIST_TRAVEL,
    }

    try:
        events = segmenter.segment_swing(frames, meta.fps, sig=signals)
    except AnalysisError as exc:
        record["stage_failed"] = "segment_swing"
        record["error_code"] = exc.code.value
        record["error_detail"] = exc.detail
        return record

    record["events"] = [
        {
            "index": e.index,
            "key": e.key.value,
            "name_cn": PHASE_META[e.key].name_cn,
            "frame_index": e.frame_index,
            "array_index": e.array_index,
            "timestamp": e.timestamp,
            "estimated": e.estimated,
        }
        for e in events
    ]
    record["estimated_count"] = sum(1 for e in events if e.estimated)
    record["segment_ok"] = True

    # 阶段时长合理性：真实挥杆 上杆 0.7~1.2s / 下杆 0.20~0.35s
    by_key = {e.key: e for e in events}
    record["durations_sec"] = {
        "addr_to_top": round(
            by_key[PhaseKey.TOP].timestamp - by_key[PhaseKey.ADDRESS].timestamp, 3
        ),
        "top_to_impact": round(
            by_key[PhaseKey.IMPACT].timestamp - by_key[PhaseKey.TOP].timestamp, 3
        ),
        "impact_to_finish": round(
            by_key[PhaseKey.FINISH].timestamp - by_key[PhaseKey.IMPACT].timestamp, 3
        ),
    }

    # ---- 指标 --------------------------------------------------------------
    ctx = metrics.build_context(frames, events, signals, meta)
    addr_index = ctx.event_of(PhaseKey.ADDRESS).array_index
    record["scale"] = {
        "S_world_m": round(ctx.S, 5),
        "S_px": round(ctx.S_px, 3),
        "view_shoulder_over_height": _view_ratio(frames, addr_index),
        "view_ratio_dtl_threshold": config.VIEW_SHOULDER_RATIO_DTL,
    }

    phase_block: Dict[str, Any] = {}
    for key in PHASE_ORDER:
        ctx.phase = key
        items = metrics.compute_phase_metrics(ctx)
        phase_block[key.value] = {
            "name_cn": PHASE_META[key].name_cn,
            "metrics": [
                {
                    "key": m.key,
                    "name": m.name,
                    "value": m.value,
                    "unit": m.unit,
                    "ref": [m.ref_min, m.ref_max],
                    "status": m.status.value,
                }
                for m in items
            ],
        }
    record["phases"] = phase_block

    ctx.phase = None
    gm = metrics.compute_global_metrics(ctx)
    record["global"] = {
        "tempo_ratio": gm.tempo_ratio,
        "swing_duration": gm.swing_duration,
        "max_head_drift_pct": gm.max_head_drift_pct,
    }
    record["warnings"] = list(ctx.warnings)

    # ---- 符号诊断：每阶段的裸角度（未 clamp / 未 sanitize）------------------
    addr_frame = ctx.addr
    diag: Dict[str, Any] = {}
    for key in PHASE_ORDER:
        frame = ctx.frame_of(key)
        sh_vec_now = frame.world[geometry.L_SHOULDER] - frame.world[geometry.R_SHOULDER]
        sh_vec_ref = (
            addr_frame.world[geometry.L_SHOULDER] - addr_frame.world[geometry.R_SHOULDER]
        )
        hip_vec_now = frame.world[geometry.L_HIP] - frame.world[geometry.R_HIP]
        hip_vec_ref = addr_frame.world[geometry.L_HIP] - addr_frame.world[geometry.R_HIP]
        shoulder = metrics._shoulder_turn_at(frame, addr_frame)
        hip = metrics._hip_turn_at(frame, addr_frame)
        diag[key.value] = {
            "shoulder_turn_signed": _f(shoulder),
            "hip_turn_signed": _f(hip),
            "x_factor_raw": _f(shoulder - hip),
            "shoulder_rot_raw_no_sign": _f(_raw_rotation_xz(sh_vec_now, sh_vec_ref)),
            "hip_rot_raw_no_sign": _f(_raw_rotation_xz(hip_vec_now, hip_vec_ref)),
            "spine_forward_tilt": _f(metrics._spine_forward_tilt_at(frame)),
            "spine_lateral_tilt": _f(
                geometry.tilt_from_vertical_xy(metrics._spine_vec(frame))
            ),
            "shoulder_vec_world": [_f(v) for v in sh_vec_now],
            "hip_vec_world": [_f(v) for v in hip_vec_now],
        }
    record["diag_raw_angles"] = diag
    record["rotation_sign"] = config.ROTATION_SIGN
    record["target_dir_x"] = config.TARGET_DIR_X

    # ---- 渲染（可选，验证完整闭环）-----------------------------------------
    if do_render:
        render_dir = os.path.join(OUT_DIR, "render", name)
        os.makedirs(render_dir, exist_ok=True)
        try:
            images = renderer.render_events(path, events, render_dir, frames)
            record["render"] = {
                "dir": render_dir,
                "files": {k.value: v for k, v in images.items()},
                "all_exist": all(
                    os.path.exists(os.path.join(render_dir, v)) for v in images.values()
                ),
            }
        except Exception as exc:  # noqa: BLE001 - 探针脚本，渲染失败不阻断
            record["render"] = {"error": f"{type(exc).__name__}: {exc}"}

    record["ok"] = True
    record["elapsed_sec"] = round(time.time() - started, 2)
    return record


def main() -> int:
    """入口。"""
    parser = argparse.ArgumentParser(description="任务 A 真实视频实测探针")
    parser.add_argument("--render", action="store_true", help="同时渲染 8 张骨架图")
    parser.add_argument("--only", default="", help="逗号分隔的用例名过滤")
    parser.add_argument("--tag", default="run", help="输出文件名后缀标签")
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
        # 屏蔽 mediapipe 的 stderr 噪音，但保留异常
        try:
            record = probe_one(name, path, view, args.render)
        except Exception as exc:  # noqa: BLE001 - 探针脚本必须跑完全部用例
            record = {
                "name": name,
                "path": path,
                "view": view,
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

    total = len(results)
    seg_ok = sum(1 for r in results if r.get("segment_ok"))
    print(f"\n{'#' * 78}")
    print(f"切分成功 {seg_ok}/{total} = {seg_ok / max(1, total) * 100:.1f}%")
    for r in results:
        flag = "OK " if r.get("segment_ok") else "FAIL"
        extra = (
            f"estimated={r.get('estimated_count')}"
            if r.get("segment_ok")
            else f"{r.get('stage_failed')}/{r.get('error_code')}: {r.get('error_detail')}"
        )
        print(f"  [{flag}] {r['name']:<16s} {extra}")
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
    if record.get("extract"):
        e = record["extract"]
        print(
            f"  extract: sampled={e['sampled']} miss={e['miss_ratio']} "
            f"vis={e['avg_core_vis']}"
        )
    if record.get("signals"):
        s = record["signals"]
        print(
            f"  signals: n={s['n']} S={s['S']} speed_max={s['speed_max']} "
            f"travel_in_S={s['wrist_travel_in_S']}"
        )
    if not record.get("segment_ok"):
        print(
            f"  >>> FAILED at {record.get('stage_failed')}: "
            f"{record.get('error_code')} - {record.get('error_detail')}"
        )
        return
    for ev in record["events"]:
        print(
            f"    {ev['index']} {ev['key']:<15s} frame={ev['frame_index']:<5d} "
            f"t={ev['timestamp']:.3f} est={ev['estimated']}"
        )
    if record.get("durations_sec"):
        d = record["durations_sec"]
        print(
            f"  时长: 上杆={d['addr_to_top']}s 下杆={d['top_to_impact']}s "
            f"送杆收杆={d['impact_to_finish']}s"
        )
    if record.get("diag_raw_angles"):
        top = record["diag_raw_angles"]["top"]
        print(
            f"  [TOP] shoulder_turn={top['shoulder_turn_signed']} "
            f"hip_turn={top['hip_turn_signed']} x_factor={top['x_factor_raw']} "
            f"(raw_no_sign sh={top['shoulder_rot_raw_no_sign']})"
        )


if __name__ == "__main__":
    raise SystemExit(main())
