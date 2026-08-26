"""M2 杆身检测（shaft_lowest_y）在 DTL 真实视频上的可用性诊断探针。

只读探针：**不改任何评分逻辑、不 commit、不动 git 元数据**。目的：验证方案 D2
的前提——「DTL 下直接用 ``_shaft_lowest_y`` 最大值的帧作为击球帧（杆头最低点 =
接触瞬间），motion 降为 tie-breaker」是否可靠。

对每个 DTL 视频：
1. 复刻 pipeline（DTL 身高标尺 build_signals + segment_swing view=DTL）；
2. 复刻 ``refine_impact`` 的 Step 1~4（窗口规划 + 解码 + 地面 ROI + 运动信号 +
   候选），对每个候选帧调用真实的 ``impact_refiner._shaft_lowest_y``；
3. 输出候选帧 shaft_y 表，并比对「shaft_y 最大帧（方案 D2 会选）」 vs
   「motion 峰帧（当前算法会选）」 vs 「用户视觉击球帧」；
4. 提取三帧截图（含 ROI / 握把 / 检测杆身线叠加）到 ``backend/_probe_out/dtl_m2_check/``。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/dtl_m2_probe.py
"""

from __future__ import annotations

import json
import math
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

import cv2  # noqa: E402

from app import (  # noqa: E402
    config,
    geometry,
    impact_refiner,
    pose_extractor,
    segmenter,
    view_detector,
)
from app.frame_reader import grab_frames  # noqa: E402
from app.schemas import AnalysisError, CameraView, PhaseKey  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
SIDE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples", "侧面")
OUT_DIR = os.path.join(BASE_DIR, "_probe_out", "dtl_m2_check")

#: (显示名, 路径, 用户视觉击球帧号[原视频帧号；None=未知/待定])
CASES: List[Tuple[str, str, Optional[int]]] = [
    ("4e8d0d7e", os.path.join(SIDE_DIR, "4e8d0d7e517a67a2a7698fd1536289eb.mp4"), 237),
    ("11a6594b", os.path.join(SIDE_DIR, "11a6594b741bb0fd1c29b4d092d50da3.mp4"), 211),
    ("11", os.path.join(SIDE_DIR, "11.mp4"), 119),  # 视觉待定，用算法帧作参照
    ("c6f67f38", os.path.join(SIDE_DIR, "c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4"), None),
    ("470057ac", os.path.join(SIDE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4"), None),
    ("f470c599", os.path.join(SIDE_DIR, "f470c5997da3f58eda196fed05cda8d6.mp4"), None),
]


# ---------------------------------------------------------------------------
# 诊断版 shaft 检测：镜像 _shaft_lowest_y，额外返回最佳杆身线端点（供目检绘制）
# ---------------------------------------------------------------------------
def _shaft_line_diag(
    bgr: np.ndarray,
    landmark_px: np.ndarray,
    grip_px: np.ndarray,
    club_len_px: float,
) -> Tuple[Optional[float], Optional[Tuple[int, int, int, int]]]:
    """与 :func:`impact_refiner._shaft_lowest_y` 逐行同构，额外返回杆身线段。

    只用于目检绘制，数值恒等于真实函数（确定性 Hough，同输入同输出）。
    """
    try:
        h, w = bgr.shape[:2]
        if h <= 0 or w <= 0:
            return None, None
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        edges = cv2.Canny(enhanced, 50, 150)

        min_len = max(8, int(club_len_px * config.CLUB_HOUGH_MIN_LEN_RATIO))
        max_gap = max(2, int(club_len_px * config.CLUB_HOUGH_MAX_GAP_RATIO))
        lines = cv2.HoughLinesP(
            edges,
            1,
            np.pi / 180.0,
            threshold=impact_refiner._SHAFT_HOUGH_THRESHOLD,
            minLineLength=min_len,
            maxLineGap=max_gap,
        )
        if lines is None:
            return None, None

        grip = np.asarray(grip_px, dtype=np.float64).ravel()
        if not (math.isfinite(grip[0]) and math.isfinite(grip[1])):
            return None, None
        grip_tol = max(6.0, float(club_len_px) * impact_refiner._SHAFT_GRIP_DIST_RATIO)

        body_mask: Optional[np.ndarray] = None
        try:
            body_mask = geometry.skeleton_polygon_mask(
                landmark_px, (h, w), thickness=max(6, int(h // 120))
            )
        except Exception:  # noqa: BLE001
            body_mask = None

        best_y: Optional[float] = None
        best_line: Optional[Tuple[int, int, int, int]] = None
        for line in lines:
            x1, y1, x2, y2 = (int(v) for v in line[0])
            p1 = np.array([x1, y1], dtype=np.float64)
            p2 = np.array([x2, y2], dtype=np.float64)
            if float(np.linalg.norm(p2 - p1)) < 4.0:
                continue
            if geometry.point_line_distance(grip, p1, p2) > grip_tol:
                continue
            head_y = float(max(y1, y2))
            if head_y < float(grip[1]) - impact_refiner._SHAFT_HEAD_Y_BIAS:
                continue
            if body_mask is not None:
                mid = (int(round((x1 + x2) / 2.0)), int(round((y1 + y2) / 2.0)))
                if (
                    0 <= mid[0] < w
                    and 0 <= mid[1] < h
                    and int(body_mask[mid[1], mid[0]]) > 0
                ):
                    continue
            if best_y is None or head_y > best_y:
                best_y = head_y
                best_line = (x1, y1, x2, y2)
        return best_y, best_line
    except cv2.error:
        return None, None
    except Exception:  # noqa: BLE001
        return None, None


# ---------------------------------------------------------------------------
# 叠加绘制（目检）
# ---------------------------------------------------------------------------
def _draw_overlay(
    bgr: np.ndarray,
    roi: Tuple[int, int, int, int],
    grip_px: Optional[np.ndarray],
    shaft_line: Optional[Tuple[int, int, int, int]],
    skel_px: Optional[np.ndarray],
) -> np.ndarray:
    """在原图叠加：地面 ROI（绿框）、握把（蓝点）、检测杆身线（黄线）、骨架（浅灰）。"""
    canvas = bgr.copy()
    x0, y0, x1, y1 = (int(v) for v in roi)
    cv2.rectangle(canvas, (x0, y0), (x1, y1), (0, 200, 0), 2)
    if skel_px is not None:
        try:
            for a, b in geometry.SKELETON_EDGES:
                pa = skel_px[a]
                pb = skel_px[b]
                if not (math.isfinite(pa[0]) and math.isfinite(pa[1])):
                    continue
                if not (math.isfinite(pb[0]) and math.isfinite(pb[1])):
                    continue
                cv2.line(
                    canvas,
                    (int(round(pa[0])), int(round(pa[1]))),
                    (int(round(pb[0])), int(round(pb[1]))),
                    (200, 200, 200),
                    1,
                    lineType=cv2.LINE_AA,
                )
        except Exception:  # noqa: BLE001
            pass
    if grip_px is not None:
        gx, gy = int(round(grip_px[0])), int(round(grip_px[1]))
        cv2.circle(canvas, (gx, gy), 7, (255, 80, 0), -1, lineType=cv2.LINE_AA)
        cv2.circle(canvas, (gx, gy), 7, (255, 255, 255), 1, lineType=cv2.LINE_AA)
    if shaft_line is not None:
        x1, y1, x2, y2 = shaft_line
        cv2.line(
            canvas, (x1, y1), (x2, y2), (0, 220, 255), 3, lineType=cv2.LINE_AA
        )
    return canvas


def _label_frame(
    bgr: np.ndarray, name: str, tag: str, frame_no: int, extra: str = ""
) -> np.ndarray:
    """在左上角叠加文字标签。"""
    canvas = bgr.copy()
    text = f"{name}  {tag}  frame={frame_no}  {extra}"
    cv2.putText(
        canvas, text, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA
    )
    cv2.putText(
        canvas, text, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA
    )
    return canvas


# ---------------------------------------------------------------------------
# 单视频诊断
# ---------------------------------------------------------------------------
def probe_one(name: str, path: str, visual_frame: Optional[int]) -> Dict[str, Any]:
    """复刻 pipeline + refine Step 1~4，输出 shaft_y 表并截图。"""
    rec: Dict[str, Any] = {"name": name, "path": path, "visual_frame": visual_frame}
    t0 = time.time()

    # ---- 复刻 pipeline（DTL 身高标尺）------------------------------------
    meta = pose_extractor.probe_video(path)
    frames = pose_extractor.extract(path, meta)
    aspect = meta.height / meta.width if meta.width > 0 else 1.0
    sig = segmenter.build_signals(frames, meta.fps, aspect=aspect, view=CameraView.DOWN_THE_LINE)
    events = segmenter.segment_swing(
        frames, meta.fps, sig=sig, aspect=aspect, view=CameraView.DOWN_THE_LINE
    )
    rec["meta"] = {
        "fps": meta.fps, "width": meta.width, "height": meta.height,
        "n_frames": len(frames), "sample_step": meta.sample_step,
    }

    def by_key(evs, key):
        return next(e for e in evs if e.key is key)

    impact = by_key(events, PhaseKey.IMPACT)
    addr = by_key(events, PhaseKey.ADDRESS)
    rec["old_impact_array"] = impact.array_index
    rec["old_impact_frame"] = impact.frame_index

    # ---- Step 1~2：窗口规划 + 解码 ----------------------------------------
    cand_frames, _decode_frames = impact_refiner.plan_refine_frames(
        events, sig, meta, frames=frames
    )
    # 解码候选帧 ∪ 视觉帧（视觉帧可能不在候选窗内，仅为截图）
    targets = set(cand_frames)
    if visual_frame is not None and 0 <= visual_frame < meta.frame_count:
        targets.add(visual_frame)
    frames_bgr = grab_frames(path, sorted(targets), orientation=meta.orientation)

    # ---- 权威结果：直接调 refine_impact（复用现有评分，不改任何逻辑）-----
    refine = impact_refiner.refine_impact(
        path, frames, events, sig, CameraView.DOWN_THE_LINE, meta, frames_bgr=frames_bgr
    )
    rec["refine"] = {
        "available": refine.available,
        "method": refine.method,
        "old_array_index": refine.old_array_index,
        "new_array_index": refine.new_array_index,
        "delta_frames": refine.delta_frames,
        "motion_peak_index": refine.motion_peak_index,
        "shaft_lowest_index": refine.shaft_lowest_index,
        "confidence": refine.confidence,
        "ball_detected": refine.ball_detected,
    }

    # ---- Step 3：地面 ROI（复刻）-----------------------------------------
    width, height = meta.width, meta.height
    addr_lm = frames[addr.array_index]
    nose_y = float(addr_lm.norm[geometry.NOSE, 1]) * height
    ankle_y = (
        float(addr_lm.norm[geometry.L_ANKLE, 1])
        + float(addr_lm.norm[geometry.R_ANKLE, 1])
    ) / 2.0 * height
    body_h_px = geometry.body_height_px(nose_y, ankle_y)
    roi = impact_refiner._ground_roi(addr_lm, width, height, body_h_px, CameraView.DOWN_THE_LINE)

    # ---- Step 4：运动信号 + 候选（复刻）----------------------------------
    gray_frames = [
        cv2.cvtColor(frames_bgr[f], cv2.COLOR_BGR2GRAY) for f in cand_frames if f in frames_bgr
    ]
    motion = impact_refiner._motion_signal(gray_frames, roi)
    raw_motion = impact_refiner._motion_signal(gray_frames, roi, smooth=False)
    candidates = impact_refiner._pick_candidates(
        motion, config.CLUBLITE_MOTION_MIN_RATIO, config.CLUBLITE_TOP_K
    )
    candidates = impact_refiner._refine_candidates(candidates, raw_motion)

    # ---- grip / landmark / club_len（复刻 refine Step 6 口径）--------------
    club_len_px = body_h_px * impact_refiner._CLUB_LEN_RATIO_DTL
    ref_norm = frames[impact.array_index].norm
    grip_px = np.array(
        [
            (float(ref_norm[geometry.L_WRIST, 0]) + float(ref_norm[geometry.R_WRIST, 0])) / 2.0 * width,
            (float(ref_norm[geometry.L_WRIST, 1]) + float(ref_norm[geometry.R_WRIST, 1])) / 2.0 * height,
        ],
        dtype=np.float64,
    )
    landmark_px = ref_norm[:, :2] * np.array([width, height])

    lo, hi = impact_refiner._window_indices(events, sig, None, None)

    # ---- 对每个候选帧调真实的 _shaft_lowest_y ----------------------------
    rows: List[Dict[str, Any]] = []
    shaft_ys: Dict[int, float] = {}
    m_max = float(np.max(motion)) if len(motion) else 0.0
    for cand in candidates:
        arr_idx = lo + cand
        frame_no = cand_frames[cand] if cand < len(cand_frames) else -1
        bgr_cand = frames_bgr.get(frame_no)
        shaft_y = None
        if bgr_cand is not None:
            shaft_y = impact_refiner._shaft_lowest_y(
                bgr_cand, landmark_px, grip_px, club_len_px, CameraView.DOWN_THE_LINE
            )
            if shaft_y is not None:
                shaft_ys[cand] = float(shaft_y)
        rows.append(
            {
                "cand_offset": int(cand),
                "array_index": int(arr_idx),
                "frame": int(frame_no),
                "shaft_y": (round(float(shaft_y), 1) if shaft_y is not None else None),
                "motion_norm": (round(float(motion[cand]) / m_max, 3) if m_max > 0 else 0.0),
            }
        )

    shaft_max_offset = max(shaft_ys, key=lambda c: float(shaft_ys[c])) if shaft_ys else None
    shaft_max_array = lo + shaft_max_offset if shaft_max_offset is not None else None
    shaft_max_frame = (
        cand_frames[shaft_max_offset] if shaft_max_offset is not None else None
    )
    rec["rows"] = rows
    rec["shaft_ys"] = {int(k): round(float(v), 1) for k, v in shaft_ys.items()}
    rec["n_shaft_detected"] = len(shaft_ys)
    rec["shaft_max"] = {
        "offset": shaft_max_offset,
        "array_index": shaft_max_array,
        "frame": shaft_max_frame,
    }
    rec["motion_peak_array"] = refine.motion_peak_index
    rec["motion_peak_frame"] = (
        frames[refine.motion_peak_index].frame_index
        if refine.motion_peak_index is not None and 0 <= refine.motion_peak_index < len(frames)
        else None
    )
    rec["roi"] = list(roi) if roi else None
    rec["elapsed_sec"] = round(time.time() - t0, 2)

    # ---- 截图（shaft 最大帧 vs 视觉击球帧 vs motion 峰帧）----------------
    os.makedirs(OUT_DIR, exist_ok=True)
    shots: List[Dict[str, Any]] = []

    def save_shot(tag: str, frame_no: Optional[int], extra: str = "") -> None:
        if frame_no is None or frame_no < 0:
            shots.append({"tag": tag, "frame": frame_no, "saved": False, "reason": "no_frame"})
            return
        bgr = frames_bgr.get(frame_no)
        if bgr is None:
            bgr = grab_frames(path, [frame_no], orientation=meta.orientation).get(frame_no)
        if bgr is None:
            shots.append({"tag": tag, "frame": frame_no, "saved": False, "reason": "decode_miss"})
            return
        # 该帧自己的骨架（目检用）
        skel_px = None
        arr = None
        for i, f in enumerate(frames):
            if f.frame_index == frame_no:
                arr = i
                break
        if arr is not None:
            skel_px = frames[arr].norm[:, :2] * np.array([width, height])
        # 检测杆身线（若该帧是候选且有检测）
        line = None
        if frame_no in set(cand_frames):
            _y, line = _shaft_line_diag(bgr, landmark_px, grip_px, club_len_px)
        canvas = _draw_overlay(bgr, roi, grip_px, line, skel_px)
        canvas = _label_frame(canvas, name, tag, frame_no, extra)
        fname = f"{name}_{tag}_f{frame_no}.jpg"
        cv2.imwrite(os.path.join(OUT_DIR, fname), canvas, [cv2.IMWRITE_JPEG_QUALITY, 88])
        shots.append({"tag": tag, "frame": frame_no, "saved": True, "file": fname})

    save_shot("shaftmax", shaft_max_frame, f"y={shaft_ys.get(shaft_max_offset) if shaft_max_offset is not None else None}")
    save_shot("motion", rec["motion_peak_frame"], "motion峰")
    save_shot("visual", visual_frame, "用户视觉")
    rec["shots"] = shots
    return rec


# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------
def _fmt_frame(arr, fr):
    if fr is None:
        return "--"
    return f"{fr}"


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    results: List[Dict[str, Any]] = []
    for name, path, visual in CASES:
        if not os.path.exists(path):
            print(f"[skip] {name}: 文件不存在 {path}")
            continue
        print(f"\n{'=' * 84}\n视频: {name}  ({os.path.basename(path)})\n{'=' * 84}", flush=True)
        try:
            rec = probe_one(name, path, visual)
        except AnalysisError as exc:
            print(f"  AnalysisError {exc.code.value}: {exc.detail}")
            results.append({"name": name, "error": f"{exc.code.value}: {exc.detail}"})
            continue
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"  unexpected: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            results.append({"name": name, "error": str(exc)})
            continue
        results.append(rec)

        r = rec["refine"]
        print(f"  meta: {rec['meta']['width']}x{rec['meta']['height']} "
              f"fps={rec['meta']['fps']} n={rec['meta']['n_frames']} "
              f"step={rec['meta']['sample_step']}")
        print(f"  refine(当前算法): old={r['old_array_index']} "
              f"peak={r['motion_peak_index']} shaft={r['shaft_lowest_index']} "
              f"new={r['new_array_index']} delta={r['delta_frames']} "
              f"available={r['available']} method={r['method']}")
        print(f"  候选帧 (array下标 / frame号 / shaft_lowest_y / motion_norm):")
        for row in rec["rows"]:
            sy = "None" if row["shaft_y"] is None else f"{row['shaft_y']:.1f}"
            print(f"    arr={row['array_index']:>4d}  frame={row['frame']:>5d}  "
                  f"shaft_y={sy:>8s}  motion_norm={row['motion_norm']:.3f}")
        sm = rec["shaft_max"]
        print(f"  shaft_y 最大值的帧 = arr {sm['array_index']} / frame {sm['frame']}  "
              f"[方案 D2 会选的击球帧]  (检测到杆身 {rec['n_shaft_detected']}/{len(rec['rows'])} 候选)")
        print(f"  motion 峰值的帧 = arr {r['motion_peak_index']} / "
              f"frame {rec['motion_peak_frame']}  [当前算法会选]")
        if visual is not None:
            print(f"  用户视觉击球帧 = frame {visual}")
        for s in rec["shots"]:
            print(f"    [shot] {s['tag']:<9s} frame={s.get('frame')} -> {s.get('file') or s.get('reason')}")

    # ---- 统计结论 ---------------------------------------------------------
    print(f"\n{'#' * 84}\n统计结论\n{'#' * 84}")
    detected = [r for r in results if r.get("rows") is not None and r.get("n_shaft_detected", 0) > 0]
    print(f"M2 检测到杆身（shaft_ys 非空）: {len(detected)}/{len([r for r in results if 'rows' in r])} 个 DTL 视频")
    devs = []
    for r in results:
        vis = r.get("visual_frame")
        smf = (r.get("shaft_max") or {}).get("frame")
        if vis is not None and smf is not None:
            d = smf - vis
            devs.append(d)
            print(f"  {r['name']}: shaft_max_frame={smf} vs 视觉={vis} -> 偏差 {d:+d} 帧")
    if devs:
        print(f"shaft_y 最大帧与用户视觉击球帧的平均偏差 = {sum(devs)/len(devs):+.1f} 帧 (n={len(devs)})")
    print(f"截图目录: {OUT_DIR}")
    with open(os.path.join(BASE_DIR, "_probe_out", "dtl_m2_probe.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("[written] backend/_probe_out/dtl_m2_probe.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
