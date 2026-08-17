# -*- coding: utf-8 -*-
"""探测真实素材：哪些视频的事件帧未覆盖全片 -> 可触发 20003 + 顺带验证。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
import uuid

BASE = "http://127.0.0.1:8123"
SAMPLES = [
    r"E:\project\golf\.tools\_probe\samples\087d40a0e808f2c319b8097d89599780.mp4",
    r"E:\project\golf\.tools\_probe\samples\0bb16a974ef55676cc1b938d8539edfd.mp4",
    r"E:\project\golf\.tools\_probe\samples\470057ac3dac2025eb6b0dcd390b6957.mp4",
    r"E:\project\golf\.tools\_probe\samples\4e8d0d7e517a67a2a7698fd1536289eb.mp4",
    r"E:\project\golf\.tools\_probe\samples\707fb04a3dbd91db19b97e0ca4aee959.mp4",
    r"E:\project\golf\.tools\_probe\samples\c6f67f38e5d293a5ce1458e5ff5a6f1b.mp4",
    r"E:\project\golf\.tools\_probe\samples\正面1.mp4",
    r"E:\project\golf\.tools\_probe\samples\正面2.mp4",
    r"E:\project\golf\.tools\_probe\samples\正面3.mp4",
]


def http_json(method, url, body=None, headers=None, timeout=30.0):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def upload_video(path):
    boundary = "----qaboundary" + uuid.uuid4().hex
    with open(path, "rb") as fh:
        content = fh.read()
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"camera_view\"\r\n\r\nface_on\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: video/mp4\r\n\r\n",
    ]
    body = "".join(parts).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    st, hd, raw = http_json("POST", f"{BASE}/api/v1/tasks", body,
                            {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.loads(raw.decode("utf-8"))["data"]["task_id"] if st == 201 else None


def wait_terminal(task_id, timeout=180.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, _hd, raw = http_json("GET", f"{BASE}/api/v1/tasks/{task_id}")
        data = json.loads(raw.decode("utf-8"))["data"]
        if data["status"] in ("success", "failed"):
            return data
        time.sleep(0.5)
    return {"status": "timeout"}


for p in SAMPLES:
    name = os.path.basename(p)
    tid = upload_video(p)
    if tid is None:
        print(f"{name}: upload failed")
        continue
    st = wait_terminal(tid)
    if st["status"] != "success":
        print(f"{name}: {st.get('status')} err={st.get('error_code')} {st.get('error_message')}")
        continue
    st2, _hd, raw = http_json("GET", f"{BASE}/api/v1/tasks/{tid}/result")
    result = json.loads(raw.decode("utf-8"))["data"]
    total = result["video_meta"].get("total_frames") or result["video_meta"]["frame_count"]
    events = sorted(p_["frame_index"] for p_ in result["phases"])
    # 找出全片中距所有事件帧 >30 的帧
    far = None
    for f in range(total):
        if min(abs(f - e) for e in events) > 30:
            far = f
            break
    print(f"{name}: total={total} events={events} step={result['video_meta'].get('sample_step')} "
          f"first_out_of_range={far} view={result.get('camera_view')}")
    if far is not None:
        st3, hd3, raw3 = http_json("GET", f"{BASE}/api/v1/task/{tid}/frame/{far}")
        try:
            code = json.loads(raw3.decode("utf-8")).get("code")
        except Exception:
            code = None
        print(f"    -> frame/{far}: status={st3} code={code}")
