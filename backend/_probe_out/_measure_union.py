"""估算 plan_reanchor_frames 帧数上界 + 修复后新事件帧是否全在解码集。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from app import impact_refiner, pose_extractor, segmenter  # noqa: E402
from app.schemas import CameraView, PhaseKey  # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE)
SAMPLE_DIR = os.path.join(PROJECT_ROOT, ".tools", "_probe", "samples")
VIDEO_DIR = os.path.join(PROJECT_ROOT, "video")

CASES = [
    ("正面1", os.path.join(SAMPLE_DIR, "正面1.mp4"), CameraView.FACE_ON),
    ("正面2", os.path.join(SAMPLE_DIR, "正面2.mp4"), CameraView.FACE_ON),
    ("正面3", os.path.join(SAMPLE_DIR, "正面3.mp4"), CameraView.FACE_ON),
    ("DTL-0bb16a97", os.path.join(SAMPLE_DIR, "0bb16a974ef55676cc1b938d8539edfd.mp4"), CameraView.DOWN_THE_LINE),
    ("DTL-470057ac", os.path.join(SAMPLE_DIR, "470057ac3dac2025eb6b0dcd390b6957.mp4"), CameraView.DOWN_THE_LINE),
    ("DTL-4e8d0d7e", os.path.join(SAMPLE_DIR, "4e8d0d7e517a67a2a7698fd1536289eb.mp4"), CameraView.DOWN_THE_LINE),
    ("DTL-c6f67f38", os.path.join(SAMPLE_DIR, "c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4"), CameraView.DOWN_THE_LINE),
    ("VID-1446d1b9", os.path.join(VIDEO_DIR, "1446d1b95c4329272f1818d6990f3c4f.mp4"), CameraView.DOWN_THE_LINE),
    ("VID-a4fba3d2", os.path.join(VIDEO_DIR, "a4fba3d24cf9beb59f9d3b06be26daab.mp4"), CameraView.DOWN_THE_LINE),
]

out_lines = []
for name, path, _view in CASES:
    meta = pose_extractor.probe_video(path)
    frames = pose_extractor.extract(path, meta)
    aspect = meta.height / meta.width
    signals = segmenter.build_signals(frames, meta.fps, aspect=aspect)
    events = segmenter.segment_swing(frames, meta.fps, sig=signals)
    cand, decode = impact_refiner.plan_refine_frames(events, signals, meta, frames=frames)
    possible = impact_refiner.plan_reanchor_frames(
        events, signals, meta, frames=frames, cand_frames=cand
    )
    event_frames = [e.frame_index for e in events]
    union = sorted(set(event_frames) | set(decode) | set(possible))

    # 用当前校正结果验证：新 8 事件帧是否全在 union 内
    idx_to_arr = {f.frame_index: i for i, f in enumerate(frames)}
    lo, hi = impact_refiner._window_indices(events, signals, None, None)
    # 模拟 refine 结果：取窗口内运动最强候选（用上一轮真实 delta 位置近似不可行，
    # 这里直接验证「任意窗口候选的 reanchor 结果都在 union 内」）
    all_inside = True
    for cand_frame in cand:
        arr = idx_to_arr.get(cand_frame)
        if arr is None or not (lo <= arr <= hi):
            continue
        rebuilt = segmenter.reanchor_impact(frames, signals, events, arr)
        if rebuilt is None:
            continue
        if not all(e.frame_index in set(union) for e in rebuilt):
            all_inside = False
            break

    out_lines.append(
        f"{name:<14s} event={len(event_frames)} window={len(cand)} "
        f"possible={len(possible)} union={len(union)} all_candidates_inside={all_inside}"
    )

with open(os.path.join(BASE, "_probe_out", "_union_sizes.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(out_lines) + "\n")
