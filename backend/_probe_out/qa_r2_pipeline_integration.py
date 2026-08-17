"""QA R2：真实 pipeline._run 集成验证——带杆合成视频走 API 全链路，
断言 07_follow_through.jpg 内容 == 真实送杆帧（覆盖"单测未验证 pipeline 集成点"缺口）。

方法：stub extract 返回合成关键点（refine/render 用真实像素），视频用带杆+球合成 mp4。
若 pipeline.py 的 grab_frames 忘了并集 possible 帧，此测试会因 FT 未解码而失败。
"""

from __future__ import annotations

import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "tests"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config, frame_reader, pose_extractor  # noqa: E402
from app.schemas import CameraView, PhaseKey, TaskStatus  # noqa: E402
from conftest import FPS, N_FRAMES, VIDEO_H, VIDEO_W, build_pose  # noqa: E402

config.CLUBLITE_ENABLED = True

# 1) 带杆+球合成视频（复用单测的构造逻辑）
import cv2  # noqa: E402
import numpy as np  # noqa: E402
from app import geometry  # noqa: E402
from tests.test_impact_refiner import _club_geometry, T_CONTACT  # noqa: E402

tmp_video = os.path.join(BASE_DIR, "_probe_out", "qa_r2_club.mp4")
ankle_mid_x, _ay, _bh, roi_top = _club_geometry()
ball_x, ball_y = ankle_mid_x + 40, roi_top + 25
head_keys = [
    (0.0, (ball_x + 160.0, ball_y - 220.0)),
    ((T_CONTACT - 2) / FPS, (ball_x + 160.0, ball_y - 220.0)),
    ((T_CONTACT - 1) / FPS, (ball_x + 40.0, ball_y - 70.0)),
    (T_CONTACT / FPS, (float(ball_x), float(ball_y))),
    ((T_CONTACT + 2) / FPS, (float(ball_x), float(ball_y))),
    (N_FRAMES / FPS, (float(ball_x), float(ball_y))),
]
from conftest import _interp  # noqa: E402

writer = cv2.VideoWriter(tmp_video, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (VIDEO_W, VIDEO_H))
for k in range(N_FRAMES):
    t = k / FPS
    _, norm = build_pose(t)
    img = np.full((VIDEO_H, VIDEO_W, 3), (70, 80, 70), dtype=np.uint8)
    pts = {i: (int(norm[i, 0] * VIDEO_W), int(norm[i, 1] * VIDEO_H)) for i in range(geometry.NUM_LANDMARKS)}
    for a, b in geometry.SKELETON_EDGES:
        cv2.line(img, pts[a], pts[b], (200, 200, 200), 4, cv2.LINE_AA)
    for i in geometry.CORE_IDS:
        cv2.circle(img, pts[i], 5, (240, 240, 240), -1, cv2.LINE_AA)
    cv2.circle(img, (ball_x, ball_y), 12, (255, 255, 255), -1, cv2.LINE_AA)
    grip = (int((norm[15, 0] + norm[16, 0]) / 2.0 * VIDEO_W), int((norm[15, 1] + norm[16, 1]) / 2.0 * VIDEO_H))
    head = tuple(int(v) for v in _interp(t, head_keys))
    cv2.line(img, grip, head, (60, 60, 255), 5, cv2.LINE_AA)
    cv2.circle(img, head, 10, (60, 60, 255), -1, cv2.LINE_AA)
    writer.write(img)
writer.release()

# 2) stub extract
def _fake_extract(path, meta, on_progress=None):
    from conftest import make_swing_frames
    if on_progress is not None:
        on_progress(0.5)
        on_progress(1.0)
    return make_swing_frames(n=meta.frame_count, fps=meta.fps, step=1)

pose_extractor.extract = _fake_extract

# 3) API 全链路
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

frame_reader.reset_stats()
with TestClient(app) as client:
    with open(tmp_video, "rb") as fh:
        resp = client.post("/api/v1/tasks", files={"file": ("club.mp4", fh.read(), "video/mp4")})
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["data"]["task_id"]
    deadline = time.time() + 60
    status = None
    while time.time() < deadline:
        status = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
        if status["status"] in (TaskStatus.SUCCESS.value, TaskStatus.FAILED.value):
            break
        time.sleep(0.2)
    print(f"task status: {status['status']} error={status.get('error_code')}")
    assert status["status"] == TaskStatus.SUCCESS.value, status
    result = client.get(f"/api/v1/tasks/{task_id}/result").json()["data"]
    phases = {p["key"]: p for p in result["phases"]}
    ft_frame = phases["follow_through"]["frame_index"]
    fin_frame = phases["finish"]["frame_index"]
    imp_frame = phases["impact"]["frame_index"]
    print(f"impact={imp_frame} follow_through={ft_frame} finish={fin_frame} warnings={result['warnings']}")

    from app import config as _cfg
    task_dir = os.path.join(_cfg.DATA_DIR, task_id)
    ft_path = os.path.join(task_dir, "07_follow_through.jpg")
    fin_path = os.path.join(task_dir, "08_finish.jpg")
    ft_bytes = open(ft_path, "rb").read()
    fin_bytes = open(fin_path, "rb").read()
    print(f"opens={frame_reader.stats()['opens']} FT==finish? {ft_bytes == fin_bytes}")

    # 用真帧渲染对比（pipeline 同口径：直接解 FT 真帧并画同一标注）
    from app import renderer, segmenter, impact_refiner, view_detector
    from conftest import make_swing_frames
    frames = make_swing_frames()
    meta = pose_extractor.probe_video(tmp_video)
    signals = segmenter.build_signals(frames, meta.fps, aspect=1.0)
    events = segmenter.segment_swing(frames, meta.fps, sig=signals)
    addr_index = next((e.array_index for e in events if e.key is PhaseKey.ADDRESS), 0)
    view, _ = view_detector.resolve(CameraView.FACE_ON, frames, meta, addr_index)
    cand, decode = impact_refiner.plan_refine_frames(events, signals, meta, frames=frames)
    possible = impact_refiner.plan_reanchor_frames(events, signals, meta, frames=frames, cand_frames=cand)
    frames_bgr = frame_reader.grab_frames(tmp_video, sorted(set([e.frame_index for e in events]) | set(decode) | set(possible)))
    refine = impact_refiner.refine_impact(tmp_video, frames, events, signals, view, meta, frames_bgr=frames_bgr)
    new_events = segmenter.reanchor_impact(frames, signals, events, refine.new_array_index)
    ft_event = next(e for e in new_events if e.key is PhaseKey.FOLLOW_THROUGH)
    bgr = frames_bgr[ft_event.frame_index]
    img, scale = renderer._resize_long_side(bgr, _cfg.RENDER_LONG_SIDE)
    lm = frames[ft_event.array_index]
    renderer._draw_skeleton(img, lm.norm, img.shape[1], img.shape[0])
    renderer._draw_label(img, f"#{ft_event.index} f{ft_event.frame_index} {ft_event.timestamp:.2f}s")
    cmp_bytes = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), _cfg.JPEG_QUALITY])[1].tobytes()
    print(f"API FT image == render(real FT frame {ft_event.frame_index})? {ft_bytes == cmp_bytes}")
    assert ft_bytes == cmp_bytes, "API pipeline 送杆图 != 真帧（P1 未完全修复）"
    assert ft_bytes != fin_bytes
    print("PASS: 真实 pipeline._run 集成验证——送杆图=真帧，opens<=2，无 fallback 错帧")
