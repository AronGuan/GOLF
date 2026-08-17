"""QA：CLUBLITE 开启时合成 pipeline 的 follow_through 图是否也错帧（P1 复现于主链路）。"""

from __future__ import annotations

import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "tests"))

os.environ["GOLF_DATA_DIR"] = os.path.join(BASE_DIR, "_probe_out", "_qa_tmp_data2")
os.makedirs(os.environ["GOLF_DATA_DIR"], exist_ok=True)
os.environ.setdefault("GOLF_PUBLIC_BASE_URL", "http://127.0.0.1:8000")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app import config, frame_reader, pose_extractor  # noqa: E402
from app.schemas import TaskStatus  # noqa: E402
from conftest import FPS, N_FRAMES, VIDEO_H, VIDEO_W, build_pose  # noqa: E402

config.CLUBLITE_ENABLED = True
print(f"CLUBLITE_ENABLED={config.CLUBLITE_ENABLED}")

tmp_video = os.path.join(BASE_DIR, "_probe_out", "qa_on_swing.mp4")
writer = cv2.VideoWriter(tmp_video, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (VIDEO_W, VIDEO_H))
from app import geometry  # noqa: E402
for k in range(N_FRAMES):
    _, norm = build_pose(k / FPS)
    img = np.full((VIDEO_H, VIDEO_W, 3), (70, 80, 70), dtype=np.uint8)
    pts = {i: (int(norm[i, 0] * VIDEO_W), int(norm[i, 1] * VIDEO_H)) for i in range(geometry.NUM_LANDMARKS)}
    for a, b in geometry.SKELETON_EDGES:
        cv2.line(img, pts[a], pts[b], (200, 200, 200), 4, cv2.LINE_AA)
    for i in geometry.CORE_IDS:
        cv2.circle(img, pts[i], 5, (240, 240, 240), -1, cv2.LINE_AA)
    writer.write(img)
writer.release()

def _fake_extract(path, meta, on_progress=None):
    from conftest import make_swing_frames
    if on_progress is not None:
        on_progress(0.5)
        on_progress(1.0)
    return make_swing_frames(n=meta.frame_count, fps=meta.fps, step=1)

pose_extractor.extract = _fake_extract

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

with TestClient(app) as client:
    with open(tmp_video, "rb") as fh:
        resp = client.post("/api/v1/tasks", files={"file": ("swing.mp4", fh.read(), "video/mp4")})
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
    print(f"impact={imp_frame} follow_through={ft_frame} finish={fin_frame}")
    print(f"warnings={result['warnings']}")

    import re
    from app import config as _cfg
    task_dir = os.path.join(_cfg.DATA_DIR, task_id)
    ft_path = os.path.join(task_dir, "07_follow_through.jpg")
    fin_path = os.path.join(task_dir, "08_finish.jpg")
    a = open(ft_path, "rb").read()
    b = open(fin_path, "rb").read()
    print(f"FT==finish bytes? {a == b}")
    if a == b:
        print("P1 复现：合成 pipeline 中 follow_through 图内容 == finish 图")
    else:
        print("合成 pipeline 中 FT 图与 finish 图不同（可能未触发 reanchor 或 FT 未移动）")
