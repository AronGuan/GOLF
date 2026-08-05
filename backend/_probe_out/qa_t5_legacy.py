"""QA：legacy 回滚实测 —— 先改 config.API_CODE_STYLE 再导入 app，起真实 uvicorn。"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
PY = r"E:\project\golf\.tools\python312\python.exe"
BASE = "http://127.0.0.1:8014"
OUT = os.path.join(BASE_DIR, "_probe_out", "_qa_t5_legacy.txt")
LINES = []


def log(*a):
    LINES.append(" ".join(str(x) for x in a))


def http(method, url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return -1, f"EXC:{type(exc).__name__}:{exc}"


# 启动器：先翻转常量，再 import app 并 uvicorn.run(app)
launcher = r'''
import sys, os
sys.path.insert(0, r"%s")
from app import config
config.API_CODE_STYLE = "legacy"   # 线上回滚开关：切常量
from app.main import app
import uvicorn
uvicorn.run(app, host="127.0.0.1", port=8014, log_level="warning")
''' % BASE_DIR
launch_file = os.path.join(BASE_DIR, "_probe_out", "_qa_legacy_launcher.py")
with io.open(launch_file, "w", encoding="utf-8") as fh:
    fh.write(launcher)

logf = open(os.path.join(BASE_DIR, "_probe_out", "_qa_server_8014.log"), "wb")
srv = subprocess.Popen([PY, launch_file], stdout=logf, stderr=subprocess.STDOUT)

deadline = time.time() + 30
ok = False
while time.time() < deadline:
    if srv.poll() is not None:
        break
    try:
        with urllib.request.urlopen(BASE + "/api/v1/health", timeout=2):
            ok = True
            break
    except Exception:
        time.sleep(0.4)
log("[boot] legacy server health:", ok)

if ok:
    # 未知任务 -> 旧码 4004
    s, t = http("GET", BASE + "/api/v1/tasks/deadbeefcafe")
    code = json.loads(t).get("code")
    log(f"legacy unknown task: status={s} code={code} (expect 4004) -> {'PASS' if code == 4004 else 'FAIL'}")
    # 坏格式 -> 4001
    boundary = "----lg"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="bad.avi"\r\n'
        f"Content-Type: video/avi\r\n\r\n".encode() + b"xx" + f"\r\n--{boundary}--\r\n".encode()
    )
    s, t = http("POST", BASE + "/api/v1/task/create", data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    code = json.loads(t).get("code")
    log(f"legacy bad format: status={s} code={code} (expect 4001) -> {'PASS' if code == 4001 else 'FAIL'}")
    # 成功响应 code 仍为 0
    boundary2 = "----lg2"
    with open(r"E:\project\golf\.tools\_probe\samples\正面1.mp4", "rb") as fh:
        content = fh.read()
    body2 = (
        f'--{boundary2}\r\nContent-Disposition: form-data; name="file"; filename="s.mp4"\r\n'
        f"Content-Type: video/mp4\r\n\r\n".encode() + content + f"\r\n--{boundary2}--\r\n".encode()
    )
    s, t = http("POST", BASE + "/api/v1/tasks", data=body2,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary2}"})
    code = json.loads(t).get("code")
    log(f"legacy old-path success: status={s} code={code} (expect 0) -> {'PASS' if code == 0 else 'FAIL'}")

srv.terminate()
try:
    srv.wait(timeout=5)
except Exception:
    srv.kill()

with io.open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(LINES))
print("done")
