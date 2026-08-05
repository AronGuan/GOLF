"""T5 手工冒烟（自包含）：子进程拉起 uvicorn -> 四项硬要求 -> 收尾杀进程。

四项硬要求：
① POST /api/v1/task/create 上传真实视频（正面1.mp4）走通全流程拿到结果；
② POST /api/v1/tasks（旧路径）也通；
③ 错误场景返回 PDD 错误码（10001/10002/20001/20002）；
④ camera_view 缺省时落 face_on（不硬拒）。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/smoke_t5_full.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

BASE = "http://127.0.0.1:8000"
VIDEO = r"E:\project\golf\.tools\_probe\samples\正面1.mp4"
OUT = os.path.join(BASE_DIR, "_probe_out", "_smoke_t5_result.txt")

PY = r"E:\project\golf\.tools\python312\python.exe"


def http(method, url, data=None, headers=None, timeout=90):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def multipart(path, field, extra=None):
    boundary = "----golfSmoke" + str(int(time.time() * 1000))
    lines = []
    if extra:
        for k, v in extra.items():
            lines.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
            )
    with open(path, "rb") as fh:
        content = fh.read()
    lines.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"smoke.mp4\"\r\n"
        f"Content-Type: video/mp4\r\n\r\n".encode() + content + b"\r\n"
    )
    lines.append(f"--{boundary}--\r\n".encode())
    return b"".join(lines), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def wait_health(server, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.poll() is not None:
            return False, "server exited early"
        try:
            with urllib.request.urlopen(BASE + "/api/v1/health", timeout=2) as r:
                return True, r.read().decode()
        except Exception:
            time.sleep(0.4)
    return False, "health timeout"


def main() -> int:
    lines: list = []
    server = subprocess.Popen(
        [PY, os.path.join(BASE_DIR, "run.py"), "serve", "--host", "127.0.0.1", "--port", "8000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    ok, health_text = wait_health(server)
    lines.append(f"[boot] server health: {ok} -> {health_text[:80]}")
    if not ok:
        lines.append("!! 服务未启动，冒烟中止")
        server.terminate()
        _write(lines)
        return 1

    # ① 新路径 + camera_view=face_on：真实视频全流程
    lines.append("=" * 70)
    lines.append("① POST /api/v1/task/create + camera_view=face_on（真实视频全流程）")
    body, hdr = multipart(VIDEO, "video", {"camera_view": "face_on"})
    status, text = http("POST", BASE + "/api/v1/task/create", data=body, headers=hdr)
    lines.append(f"  create status={status} body={text[:160]}")
    if status != 201:
        lines.append("!! 创建失败")
        server.terminate()
        _write(lines)
        return 1
    task_id = json.loads(text)["data"]["task_id"]

    result = None
    for _ in range(90):
        s, t = http("GET", BASE + f"/api/v1/task/status/{task_id}")
        state = json.loads(t)["data"]
        if state["status"] in ("success", "failed"):
            lines.append(
                f"  poll terminal status={state['status']} step={state['step']} "
                f"step_text={state['step_text']!r} progress={state['progress']}"
            )
            if state["status"] == "success":
                s2, t2 = http("GET", BASE + f"/api/v1/task/result/{task_id}")
                result = json.loads(t2)["data"]
                lines.append(
                    f"  result camera_view={result.get('camera_view')} "
                    f"phases={len(result['phases'])} "
                    f"risks_total={sum(len(p['risks']) for p in result['phases'])} "
                    f"warnings={result.get('warnings')}"
                )
                top = next(p for p in result["phases"] if p["key"] == "top")
                lines.append(
                    f"  top metrics={[(m['key'], m['value'], m['status']) for m in top['metrics']]}"
                )
                ft = next(p for p in result["phases"] if p["key"] == "follow_through")
                lines.append(
                    f"  follow_through metrics={[(m['key'], m['value']) for m in ft['metrics']]} "
                    f"risks={[r['rule_id'] for r in ft['risks']]}"
                )
                # description 字段抽查
                any_desc = any(m.get("description") for p in result["phases"] for m in p["metrics"])
                lines.append(f"  has description field: {any_desc}")
            break
        time.sleep(1)
    else:
        lines.append("  !! 轮询超时（120s）")

    # ② 旧路径兼容
    lines.append("=" * 70)
    lines.append("② POST /api/v1/tasks（旧路径）+ 旧路径状态查询")
    body2, hdr2 = multipart(VIDEO, "file", {"camera_view": "face_on"})
    status2, text2 = http("POST", BASE + "/api/v1/tasks", data=body2, headers=hdr2)
    lines.append(f"  create status={status2} body={text2[:160]}")
    if status2 == 201:
        task2 = json.loads(text2)["data"]["task_id"]
        s3, t3 = http("GET", BASE + f"/api/v1/tasks/{task2}")
        lines.append(f"  old-path status GET code={json.loads(t3)['code']}")
        s3b, t3b = http("GET", BASE + f"/api/v1/task/status/{task2}")
        lines.append(f"  pdd-path status GET code={json.loads(t3b)['code']} "
                     f"(两条路径等价={json.loads(t3)['data'] == json.loads(t3b)['data']})")
    else:
        lines.append("  !! 旧路径创建失败")
        task2 = None

    # ③ 错误场景 PDD 码
    lines.append("=" * 70)
    lines.append("③ PDD 错误码")
    boundary = "----smokeBad"
    bbody = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"bad.avi\"\r\n"
        f"Content-Type: video/avi\r\n\r\n".encode() + b"xxxx" + f"\r\n--{boundary}--\r\n".encode()
    )
    s4, t4 = http("POST", BASE + "/api/v1/task/create", data=bbody,
                  headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    lines.append(f"  bad format(.avi): status={s4} code={json.loads(t4)['code']} "
                 f"msg={json.loads(t4)['message']}  (期望 10002)")
    s5, t5 = http("GET", BASE + "/api/v1/task/status/deadbeefcafe")
    lines.append(f"  unknown task: status={s5} code={json.loads(t5)['code']}  (期望 20001)")
    if task2:
        s6, t6 = http("GET", BASE + f"/api/v1/task/result/{task2}")
        lines.append(f"  result-not-ready: status={s6} code={json.loads(t6)['code']} "
                     f"msg={json.loads(t6)['message']}  (期望 20002)")
    # 10001 文件过大（>20MB）
    big = b"\x00" * (20 * 1024 * 1024 + 1024 * 1024)
    bb = "----smokeBig"
    bigbody = (
        f"--{bb}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"big.mp4\"\r\n"
        f"Content-Type: video/mp4\r\n\r\n".encode() + big + f"\r\n--{bb}--\r\n".encode()
    )
    s7, t7 = http("POST", BASE + "/api/v1/task/create", data=bigbody,
                  headers={"Content-Type": f"multipart/form-data; boundary={bb}"})
    lines.append(f"  oversize: status={s7} code={json.loads(t7)['code']}  (期望 10001)")

    # ④ camera_view 缺省 -> face_on（不硬拒；落值断言由 test_api 覆盖）
    lines.append("=" * 70)
    lines.append("④ camera_view 缺省（不硬拒）")
    body3, hdr3 = multipart(VIDEO, "video")
    s8, t8 = http("POST", BASE + "/api/v1/task/create", data=body3, headers=hdr3)
    lines.append(f"  create without camera_view: status={s8} body={t8[:140]}  (期望 201)")

    lines.append("=" * 70)
    lines.append("SMOKE DONE")
    _write(lines)
    server.terminate()
    try:
        server.wait(timeout=5)
    except Exception:
        server.kill()
    return 0


def _write(lines) -> None:
    with io.open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("written ->", OUT)


if __name__ == "__main__":
    raise SystemExit(main())
