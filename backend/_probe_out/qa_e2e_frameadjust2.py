# -*- coding: utf-8 -*-
"""QA 独立端到端验收 part2 修订：20003 边界（真实素材）+ 降采样快照。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
import uuid

BASE = "http://127.0.0.1:8123"
SAMPLE_LATE = r"E:\project\golf\.tools\_probe\samples\0bb16a974ef55676cc1b938d8539edfd.mp4"
SAMPLE = r"E:\project\golf\.tools\_probe\samples\正面1.mp4"
DATA_DIR = r"E:\project\golf\backend\_probe_out\qa_frameadjust_data"

import cv2
import numpy as np

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  ({detail})" if detail else ""))


def http_json(method, url, body=None, headers=None, timeout=60.0):
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
    data = json.loads(raw.decode("utf-8"))
    assert st == 201, (st, raw[:500])
    return data["data"]["task_id"]


def wait_terminal(task_id, timeout=240.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, _hd, raw = http_json("GET", f"{BASE}/api/v1/tasks/{task_id}")
        data = json.loads(raw.decode("utf-8"))["data"]
        if data["status"] in ("success", "failed"):
            return data
        time.sleep(0.5)
    raise TimeoutError(task_id)


def get_result(task_id):
    st, _hd, raw = http_json("GET", f"{BASE}/api/v1/tasks/{task_id}/result")
    assert st == 200, (st, raw[:500])
    return json.loads(raw.decode("utf-8"))["data"]


def frame_image(task_id, idx):
    return http_json("GET", BASE + f"/api/v1/task/{task_id}/frame/{idx}")


def code_of(raw):
    try:
        return json.loads(raw.decode("utf-8")).get("code")
    except Exception:
        return None


def build_concat(path_out, copies=10, fps=26.0):
    cap = cv2.VideoCapture(SAMPLE)
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    h, w = frames[0].shape[:2]
    writer = cv2.VideoWriter(path_out, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for _ in range(copies):
        for f in frames:
            writer.write(f)
    writer.release()
    return len(frames) * copies


def main() -> int:
    print("=" * 70)
    print("E2E-2: 20003 边界（真实素材 0bb16，事件帧集中在后段）")
    print("=" * 70)
    task_id = upload_video(SAMPLE_LATE)
    st = wait_terminal(task_id)
    check("分析成功", st["status"] == "success", st.get("error_code", ""))
    result = get_result(task_id)
    total = result["video_meta"].get("total_frames") or result["video_meta"]["frame_count"]
    events = sorted(p["frame_index"] for p in result["phases"])
    addr, finish = events[0], events[-1]
    print(f"total={total} events={events}")

    # 边界：addr-30 = 138 应 200；addr-31 = 137 应 20003
    st, hd, raw = frame_image(task_id, addr - 30)
    check(f"frame/{addr - 30}（addr-30）-> 200", st == 200,
          f"status={st} x={hd.get('X-Frame-Index')}")
    st, hd, raw = frame_image(task_id, addr - 31)
    check(f"frame/{addr - 31}（addr-31）-> 20003", st == 400 and code_of(raw) == 20003,
          f"status={st} code={code_of(raw)}")
    st, hd, raw = frame_image(task_id, 0)
    check("frame/0（远早于事件帧）-> 20003", st == 400 and code_of(raw) == 20003,
          f"status={st} code={code_of(raw)}")
    # clamp 交互：-5 -> clamp 0 -> 越界 -> 20003（clamp 先于范围校验，设计如此）
    st, hd, raw = frame_image(task_id, -5)
    check("frame/-5 -> clamp 0 -> 越界 20003", st == 400 and code_of(raw) == 20003,
          f"status={st} code={code_of(raw)}")
    # 999 -> clamp 209（finish 209 在 ±30 内）-> 200
    st, hd, raw = frame_image(task_id, 999)
    check(f"frame/999 -> clamp {total - 1} -> 200", st == 200 and hd.get("X-Frame-Index") == str(total - 1),
          f"status={st} x={hd.get('X-Frame-Index')} total-1={total - 1}")

    print("\n" + "=" * 70)
    print("E2E-3: 降采样快照（正面1 ×8 -> 504 帧, sample_step=2, 19.4s≤20s）")
    print("=" * 70)
    concat = r"E:\project\golf\backend\_probe_out\qa_concat.mp4"
    n_total2 = build_concat(concat, copies=8)  # 504 帧 19.4s 且 >480 -> step=2
    print(f"concat total={n_total2}")
    task_id2 = upload_video(concat)
    st2 = wait_terminal(task_id2, timeout=300)
    check("拼接视频分析成功", st2["status"] == "success", st2.get("error_code", ""))
    if st2["status"] != "success":
        print("  -> 跳过降采样断言（任务失败）")
        print(f"  E2E 汇总: PASS={PASS} FAIL={FAIL}")
        return 0 if FAIL == 0 else 1
    result2 = get_result(task_id2)
    vm2 = result2["video_meta"]
    total2 = vm2.get("total_frames") or vm2.get("frame_count")
    step = vm2.get("sample_step")
    events2 = sorted(p["frame_index"] for p in result2["phases"])
    print(f"total={total2} sample_step={step} events={events2}")
    check("sample_step=2（630 帧 > 480）", step == 2, f"step={step}")

    # 中间帧 37 -> 快照到最近采样帧（偶数）
    st, hd, raw = frame_image(task_id2, 37)
    hdr_idx = int(hd.get("X-Frame-Index")) if hd.get("X-Frame-Index") else -1
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    check("frame/37 -> 200 且 X-Frame-Index 为最近采样帧",
          st == 200 and hdr_idx in (36, 38), f"status={st} hdr={hdr_idx}")
    check("实际渲染帧为偶数（采样帧）", hdr_idx % 2 == 0, f"hdr={hdr_idx}")
    check("PNG 可解码", img is not None and img.shape[0] > 0,
          str(None if img is None else img.shape))
    # 采样帧精确命中
    st, hd, raw = frame_image(task_id2, 36)
    check("frame/36（采样帧）-> X-Frame-Index=36",
          st == 200 and hd.get("X-Frame-Index") == "36",
          f"status={st} hdr={hd.get('X-Frame-Index')}")
    # 事件帧精确命中
    ev = events2[5]
    st, hd, raw = frame_image(task_id2, ev)
    check(f"frame/{ev}（事件帧）-> X-Frame-Index={ev}",
          st == 200 and hd.get("X-Frame-Index") == str(ev),
          f"status={st} hdr={hd.get('X-Frame-Index')}")
    # 大帧号：clamp 到 total-1 -> 最近采样帧（偶数）
    st, hd, raw = frame_image(task_id2, 9999)
    expect_last_sampled = (total2 - 1) if (total2 - 1) % 2 == 0 else (total2 - 2)
    if st == 200:
        check(f"frame/9999 -> clamp {total2 - 1} -> 快照 {expect_last_sampled}",
              hd.get("X-Frame-Index") == str(expect_last_sampled),
              f"status={st} hdr={hd.get('X-Frame-Index')} expect={expect_last_sampled}")
    else:
        check("frame/9999 -> 尾部无事件 -> 20003 优雅", st == 400 and code_of(raw) == 20003,
              f"status={st} code={code_of(raw)}")

    print("\n" + "=" * 70)
    print(f"E2E-2/3 汇总: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
