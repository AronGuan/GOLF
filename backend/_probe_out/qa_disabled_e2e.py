"""QA：CLUBLITE_ENABLED=False 时整条 pipeline 链路完整（回退开关，端到端）。

复用 test_pipeline_e2e 的思路：stub extract + 真实 HTTP 链路，仅把开关关掉。
"""

from __future__ import annotations

import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "tests"))

os.environ["GOLF_DATA_DIR"] = os.path.join(BASE_DIR, "_probe_out", "_qa_tmp_data")
os.makedirs(os.environ["GOLF_DATA_DIR"], exist_ok=True)
os.environ.setdefault("GOLF_PUBLIC_BASE_URL", "http://127.0.0.1:8000")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app import config, frame_reader, pose_extractor  # noqa: E402
from app.schemas import TaskStatus  # noqa: E402
from conftest import FPS, N_FRAMES, VIDEO_H, VIDEO_W, build_pose  # noqa: E402

# 关掉开关
config.CLUBLITE_ENABLED = False
print(f"CLUBLITE_ENABLED={config.CLUBLITE_ENABLED}")

# 生成合成视频（骨架，无杆/球）
tmp_video = os.path.join(BASE_DIR, "_probe_out", "qa_off_swing.mp4")
writer = cv2.VideoWriter(tmp_video, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (VIDEO_W, VIDEO_H))
for k in range(N_FRAMES):
    _, norm = build_pose(k / FPS)
    img = np.full((VIDEO_H, VIDEO_W, 3), (70, 80, 70), dtype=np.uint8)
    from app import geometry  # noqa: E402
    pts = {i: (int(norm[i, 0] * VIDEO_W), int(norm[i, 1] * VIDEO_H)) for i in range(geometry.NUM_LANDMARKS)}
    for a, b in geometry.SKELETON_EDGES:
        cv2.line(img, pts[a], pts[b], (200, 200, 200), 4, cv2.LINE_AA)
    for i in geometry.CORE_IDS:
        cv2.circle(img, pts[i], 5, (240, 240, 240), -1, cv2.LINE_AA)
    writer.write(img)
writer.release()

# stub extract
def _fake_extract(path, meta, on_progress=None):
    from conftest import make_swing_frames
    if on_progress is not None:
        on_progress(0.5)
        on_progress(1.0)
    return make_swing_frames(n=meta.frame_count, fps=meta.fps, step=1)

pose_extractor.extract = _fake_extract

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

frame_reader.reset_stats()
with TestClient(app) as client:
    with open(tmp_video, "rb") as fh:
        resp = client.post("/api/v1/tasks", files={"file": ("swing.mp4", fh.read(), "video/mp4")})
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["data"]["task_id"]
    deadline = time.time() + 60
    status = None
    while time.time() < deadline:
        status = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
        if status["status"] in (TaskStatus.SUCCESS.value, TaskStatus.FAILED.value):
            break
        time.sleep(0.2)
    print(f"task status: {status['status']} progress={status['progress']} error={status.get('error_code')}")
    assert status["status"] == TaskStatus.SUCCESS.value, status
    result = client.get(f"/api/v1/tasks/{task_id}/result").json()["data"]
    phases = result["phases"]
    assert len(phases) == 8
    assert all(p["image_url"] for p in phases)
    print(f"warnings={result['warnings']}")
    stats = frame_reader.stats()
    print(f"opens={stats['opens']} retrieved={stats['retrieved']}")
    assert stats["opens"] <= 2, f"opens 超标: {stats}"
    # 关闭时应无校正提示
    assert all("校正" not in w for w in result["warnings"])
    print("PASS: CLUBLITE_ENABLED=False 全链路 8 阶段成功，无校正 warning，opens <= 2")
