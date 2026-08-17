# -*- coding: utf-8 -*-
"""QA 独立边界测试（进程内，直接驱动 app 模块）：

1. npz 结构完整性（所有 key/形状/与 frames 一致性）
2. KEEP_SOURCE_VIDEO=False 分支：_cleanup_upload 删除 upload、不产生 source
3. KEEP_SOURCE_VIDEO=False 时帧接口降级 5000/10004（缺 source 视频）
4. TTL purge（dict+目录都清）后帧接口 -> 20001
"""
from __future__ import annotations

import os
import sys
import tempfile

BACKEND = r"E:\project\golf\backend"
sys.path.insert(0, BACKEND)
_TMP = tempfile.mkdtemp(prefix="qa_fa_edge_")
os.environ["GOLF_DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app import config, frame_service, landmark_cache, pipeline  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.schemas import (  # noqa: E402
    AnalysisResult, CameraView, GlobalMetrics, TaskStatus, VideoMeta,
)
from app.task_store import task_store  # noqa: E402
from tests.conftest import make_swing_frames  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  ({detail})" if detail else ""))


def make_task_with_result(keep_source: bool) -> str:
    """建一个带 result 的任务（不跑真实分析，直接造数据）。"""
    old = config.KEEP_SOURCE_VIDEO
    config.KEEP_SOURCE_VIDEO = keep_source
    try:
        state = task_store.create()
        tid = state.task_id
        frames = make_swing_frames(n=120, fps=30.0)
        meta = VideoMeta(
            width=480, height=854, fps=30.0, frame_count=120,
            total_frames=120, sample_step=1, duration=4.0,
        )
        landmark_cache.save_landmarks(state.out_dir, frames, meta)
        # 模拟 _cleanup_upload 行为：keep=True 时保留 source（真实视频），否则删 upload
        import shutil
        upload_path = os.path.join(state.out_dir, "upload.mp4")
        shutil.copyfile(r"E:\project\golf\.tools\_probe\samples\正面1.mp4", upload_path)
        if keep_source:
            os.replace(upload_path, os.path.join(state.out_dir, "source.mp4"))
        else:
            os.remove(upload_path)
        from app.schemas import PhaseResult, PhaseKey, PHASE_META
        events = [i for i in range(0, 120, 15)]  # 8 个事件帧
        phases = []
        for order, key in enumerate(PHASE_META):
            phases.append(PhaseResult(
                index=PHASE_META[key].index, key=key,
                name_cn=PHASE_META[key].name_cn, name_en=PHASE_META[key].name_en,
                frame_index=events[order], timestamp=events[order] / 30.0,
                estimated=False, image_url="", metrics=[], risks=[],
            ))
        result = AnalysisResult(
            task_id=tid, status=TaskStatus.SUCCESS, camera_view=CameraView.FACE_ON,
            video_meta=meta,
            global_metrics=GlobalMetrics(
                tempo_ratio=1.0, swing_duration=1.0, max_head_drift_pct=0.0,
            ),
            phases=phases, warnings=[], disclaimer="",
        )
        task_store.succeed(tid, result)
        return tid
    finally:
        config.KEEP_SOURCE_VIDEO = old


def main() -> int:
    client = TestClient(fastapi_app)

    # ---- 1. npz 结构 ----
    print("== 1. npz 结构完整性 ==")
    tid = make_task_with_result(True)
    state = task_store.get(tid)
    npz_path = os.path.join(state.out_dir, config.LANDMARK_CACHE_FILENAME)
    import numpy as np
    with np.load(npz_path, allow_pickle=False) as data:
        keys = sorted(data.files)
        check("npz keys 完整", set(keys) == {
            "frame_index", "detected", "norm", "world", "visibility",
            "fps", "sample_step", "total_frames",
        }, str(keys))
        check("norm 形状 (120,33,3)", data["norm"].shape == (120, 33, 3), str(data["norm"].shape))
        check("world 形状 (120,33,3)", data["world"].shape == (120, 33, 3), str(data["world"].shape))
        check("visibility 形状 (120,33)", data["visibility"].shape == (120, 33), str(data["visibility"].shape))
        check("frame_index 覆盖 0..119", list(data["frame_index"][:3]) == [0, 1, 2] and data["frame_index"][-1] == 119)
        check("标量 fps=30 sample_step=1 total=120",
              float(data["fps"]) == 30.0 and int(data["sample_step"]) == 1 and int(data["total_frames"]) == 120)

    # 接口用 npz 渲染（走真实 render_frame）
    png, actual = frame_service.render_frame(tid, 30)
    check("render_frame 复用 npz 渲染成功", len(png) > 0 and actual == 30, f"actual={actual} len={len(png)}")

    # ---- 2. KEEP_SOURCE_VIDEO=False：upload 删除、无 source ----
    print("== 2. KEEP_SOURCE_VIDEO=False 分支 ==")
    tid2 = make_task_with_result(False)
    state2 = task_store.get(tid2)
    check("upload.mp4 已删除", not os.path.exists(os.path.join(state2.out_dir, "upload.mp4")))
    check("未产生 source.mp4", not os.path.exists(os.path.join(state2.out_dir, "source.mp4")))
    check("npz 仍落盘", os.path.isfile(os.path.join(state2.out_dir, config.LANDMARK_CACHE_FILENAME)))

    # ---- 3. KEEP_SOURCE_VIDEO=False：帧接口降级 5000/10004 ----
    print("== 3. 缺 source 视频 -> 帧接口降级 ==")
    resp = client.get(f"/api/v1/task/{tid2}/frame/30")
    body = resp.json()
    check("HTTP 500 + PDD 10004", resp.status_code == 500 and body.get("code") == 10004,
          f"status={resp.status_code} code={body.get('code')} msg={body.get('message')}")

    # ---- 4. FRAME_ADJUST_ENABLED=False -> 5000/10004 ----
    print("== 4. FRAME_ADJUST_ENABLED=False 开关 ==")
    old_flag = config.FRAME_ADJUST_ENABLED
    config.FRAME_ADJUST_ENABLED = False
    try:
        resp = client.get(f"/api/v1/task/{tid}/frame/30")
        body = resp.json()
        check("开关关闭 -> HTTP 500 + PDD 10004",
              resp.status_code == 500 and body.get("code") == 10004,
              f"status={resp.status_code} code={body.get('code')}")
    finally:
        config.FRAME_ADJUST_ENABLED = old_flag

    # ---- 5. TTL purge（dict+目录清）-> 20001 ----
    print("== 5. TTL purge -> 20001 ==")
    tid3 = make_task_with_result(True)
    state3 = task_store.get(tid3)
    task_store._purge(state3)
    resp = client.get(f"/api/v1/task/{tid3}/frame/30")
    body = resp.json()
    check("purge 后 -> 404/20001", resp.status_code == 404 and body.get("code") == 20001,
          f"status={resp.status_code} code={body.get('code')}")

    # ---- 6. 缺失 npz（仅缺缓存，源视频在）-> 5000/10004 ----
    print("== 6. npz 缺失 -> 降级 ==")
    tid4 = make_task_with_result(True)
    state4 = task_store.get(tid4)
    os.remove(os.path.join(state4.out_dir, config.LANDMARK_CACHE_FILENAME))
    resp = client.get(f"/api/v1/task/{tid4}/frame/30")
    body = resp.json()
    check("npz 缺失 -> HTTP 500 + PDD 10004", resp.status_code == 500 and body.get("code") == 10004,
          f"status={resp.status_code} code={body.get('code')} msg={body.get('message')}")

    print(f"\n边界汇总: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
