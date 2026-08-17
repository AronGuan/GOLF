"""手动帧微调接口测试（ARCHITECTURE-v4-frameadjust.md）。

覆盖：
- 分析成功后任务目录产物：``landmarks.npz`` 关键点缓存 + ``source.mp4`` 保留、
  ``upload.mp4`` 移除（PRD Q6 兼容）；
- ``GET /api/v1/task/{id}/frame/{idx}``（+ 旧别名 ``/tasks/{id}/frame/{idx}``）：
  返回可解码 PNG、``X-Frame-Index`` 回传实际帧号、双路径逐字节等价；
- 帧号 clamp（负数 -> 0）与范围限制（事件帧 ±30 之外 -> 20003）；
- 错误码：任务不存在 20001、任务未完成 20002。

复用 test_pipeline_e2e 的合成挥杆套路：``pose_extractor.extract`` 打桩为合成
关键点序列，probe/segmenter/metrics/renderer/HTTP 层全走真实实现。
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from app import config, pose_extractor
from app.schemas import TaskStatus

from conftest import make_swing_frames


@pytest.fixture(scope="session")
def probe_bytes():
    """读取合成测试视频（1.0s 匀速灰阶 -> 后台判 BAD_VIDEO，快速失败）。"""
    import os

    probe = r"E:\project\golf\.tools\_probe\t.mp4"
    assert os.path.exists(probe), f"缺少测试素材: {probe}"
    with open(probe, "rb") as handle:
        return handle.read()


@pytest.fixture()
def stub_extract(monkeypatch):
    """把 MediaPipe 推理替换为合成关键点（帧号与合成视频严格对齐）。"""

    def _fake_extract(path, meta, on_progress=None):
        if on_progress is not None:
            on_progress(0.5)
            on_progress(1.0)
        return make_swing_frames(n=meta.frame_count, fps=meta.fps, step=1)

    monkeypatch.setattr(pose_extractor, "extract", _fake_extract)
    return _fake_extract


def _wait_terminal(client, task_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    data: dict = {}
    while time.time() < deadline:
        data = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
        if data["status"] in (TaskStatus.SUCCESS.value, TaskStatus.FAILED.value):
            return data
        time.sleep(0.2)
    return data


@pytest.fixture()
def finished(api_client, synth_video, stub_extract):
    """跑完一次成功的分析，返回 ``(task_id, result_data)``。"""
    with open(synth_video, "rb") as handle:
        content = handle.read()
    resp = api_client.post(
        "/api/v1/tasks", files={"file": ("swing.mp4", content, "video/mp4")}
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["data"]["task_id"]

    status = _wait_terminal(api_client, task_id)
    assert status["status"] == TaskStatus.SUCCESS.value, status

    result_resp = api_client.get(f"/api/v1/tasks/{task_id}/result")
    assert result_resp.status_code == 200, result_resp.text
    return task_id, result_resp.json()["data"]


def _decode_png(content: bytes) -> np.ndarray:
    """把 PNG 字节解码回 BGR 图；失败抛 AssertionError。"""
    import cv2

    img = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None, "返回内容不是可解码图像"
    return img


class TestPipelineArtifacts:
    """分析成功后手动帧微调所需产物必须落盘。"""

    def test_landmark_cache_written(self, finished):
        task_id, _result = finished
        path = Path(config.DATA_DIR) / task_id / config.LANDMARK_CACHE_FILENAME
        assert path.is_file(), f"缺少关键点缓存 {config.LANDMARK_CACHE_FILENAME}"
        assert path.stat().st_size > 0

    def test_source_video_kept_upload_removed(self, finished):
        """upload.mp4 移除（PRD Q6 兼容），source.mp4 保留供动态帧渲染。"""
        task_id, _result = finished
        task_dir = Path(config.DATA_DIR) / task_id
        assert not (task_dir / config.UPLOAD_FILENAME).exists(), "upload.mp4 应已移除"
        assert (task_dir / config.SOURCE_FILENAME).is_file(), "缺少 source.mp4"

    def test_landmark_cache_loads_back(self, finished):
        from app.landmark_cache import load_landmarks

        task_id, result = finished
        frames = load_landmarks(str(Path(config.DATA_DIR) / task_id))
        assert len(frames) == result["video_meta"]["frame_count"]
        assert all(f.norm.shape == (33, 3) for f in frames)


class TestFrameEndpoint:
    """``GET /api/v1/task/{id}/frame/{idx}``（+ 旧别名）。"""

    @pytest.mark.parametrize("path_fmt", [
        "/api/v1/task/{}/frame/{}",
        "/api/v1/tasks/{}/frame/{}",
    ])
    def test_returns_png_at_event_frames(self, api_client, finished, path_fmt):
        task_id, result = finished
        for phase in result["phases"]:
            resp = api_client.get(path_fmt.format(task_id, phase["frame_index"]))
            assert resp.status_code == 200, (phase["key"], resp.text)
            assert resp.headers["content-type"].startswith("image/png")
            assert resp.headers.get("X-Frame-Index") == str(phase["frame_index"])
            img = _decode_png(resp.content)
            assert img.shape[0] > 0 and img.shape[1] > 0
            assert max(img.shape[:2]) <= config.RENDER_LONG_SIDE

    def test_dual_paths_identical_bytes(self, api_client, finished):
        task_id, result = finished
        idx = result["phases"][5]["frame_index"]  # impact
        old = api_client.get(f"/api/v1/tasks/{task_id}/frame/{idx}")
        new = api_client.get(f"/api/v1/task/{task_id}/frame/{idx}")
        assert old.status_code == new.status_code == 200
        assert old.content == new.content

    def test_negative_frame_clamped_to_zero(self, api_client, finished):
        """负数帧 clamp 到 0（Address 事件帧在 ±30 内，合法）。"""
        task_id, _result = finished
        resp = api_client.get(f"/api/v1/task/{task_id}/frame/-5")
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("X-Frame-Index") == "0"

    def test_frame_out_of_range_returns_20003(self, api_client, finished):
        """事件帧 +31 帧 -> 超出可调整范围 -> 400 + PDD 20003。"""
        task_id, result = finished
        finish_frame = max(p["frame_index"] for p in result["phases"])
        far = finish_frame + config.FRAME_ADJUST_RANGE + 1
        resp = api_client.get(f"/api/v1/task/{task_id}/frame/{far}")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == config.PDD_CODE_FRAME_OUT_OF_RANGE  # 20003
        assert body["data"] is None

    def test_unknown_task_returns_20001(self, api_client):
        resp = api_client.get("/api/v1/task/deadbeefcafe/frame/10")
        assert resp.status_code == 404
        assert resp.json()["code"] == config.PDD_CODE_TASK_NOT_FOUND

    def test_unfinished_task_returns_20002(self, api_client, probe_bytes):
        """t.mp4 会快速失败（BAD_VIDEO）-> 任务非 SUCCESS -> 20002。"""
        resp = api_client.post(
            "/api/v1/tasks", files={"file": ("swing.mp4", probe_bytes, "video/mp4")}
        )
        assert resp.status_code == 201, resp.text
        task_id = resp.json()["data"]["task_id"]
        _wait_terminal(api_client, task_id)
        frame_resp = api_client.get(f"/api/v1/task/{task_id}/frame/10")
        assert frame_resp.status_code == 409
        assert frame_resp.json()["code"] == config.PDD_CODE_TASK_PENDING


class TestFrameRendererUnit:
    """``renderer.render_frame_png`` 单测（不依赖任务）。"""

    def test_renders_png_bytes(self, synth_video):
        import cv2

        from app import renderer, segmenter

        frames = make_swing_frames()
        events = segmenter.segment_swing(frames, 30.0)
        event = events[5]  # impact
        cap = cv2.VideoCapture(synth_video)
        cap.set(cv2.CAP_PROP_POS_FRAMES, event.frame_index)
        ok, bgr = cap.read()
        cap.release()
        assert ok and bgr is not None

        lm = next(f for f in frames if f.frame_index == event.frame_index)
        png = renderer.render_frame_png(bgr, event, lm)
        assert isinstance(png, bytes) and len(png) > 0
        img = _decode_png(png)
        assert max(img.shape[:2]) <= config.RENDER_LONG_SIDE
