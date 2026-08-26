"""手动微调实时重算指标接口测试（纯增量，不改核心算法）。

覆盖：
- ``GET /api/v1/task/{id}/phase_metrics/{phase}/{idx}``（+ 旧别名）：
  返回目标 phase 指标、``frame_index`` 采样对齐正确、指标值随帧变化；
- 事件帧处重算结果与原始结果逐项一致（复用 build_context 口径正确）；
- 只重算目标 phase：调用后完整结果（其他阶段）不受影响；
- 错误码：任务不存在 20001、未完成 20002、帧号越界 20003、非法 phase 20004；
- phase 大小写/枚举名兼容、双路径等价。

复用 test_frame_adjust 的合成挥杆套路：``pose_extractor.extract`` 打桩为合成
关键点序列，probe/segmenter/metrics 全走真实实现。``finished`` 为 session 级，
只跑一次完整流水线（手动补丁 extract，避免 monkeypatch 的 function 作用域限制）。
"""

from __future__ import annotations

import os
import time

import pytest

from app import config
from app.schemas import TaskStatus

from conftest import make_swing_frames

PROBE_MP4 = r"E:\project\golf\.tools\_probe\t.mp4"


def _wait_terminal(client, task_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    data: dict = {}
    while time.time() < deadline:
        data = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
        if data["status"] in (TaskStatus.SUCCESS.value, TaskStatus.FAILED.value):
            return data
        time.sleep(0.2)
    return data


@pytest.fixture(scope="session")
def probe_bytes() -> bytes:
    """读取合成测试视频（1.0s 匀速灰阶 -> 后台判 BAD_VIDEO，快速失败）。"""
    assert os.path.exists(PROBE_MP4), f"缺少测试素材: {PROBE_MP4}"
    with open(PROBE_MP4, "rb") as handle:
        return handle.read()


@pytest.fixture(scope="session")
def session_client():
    """session 级 TestClient（供 session 级 ``finished`` 使用，避免作用域冲突）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def finished(session_client, synth_video):
    """跑完一次成功分析，返回 ``(task_id, result_data)``（session 级，只跑一次）。"""
    from app import pose_extractor

    original = pose_extractor.extract

    def _fake_extract(path, meta, on_progress=None):
        if on_progress is not None:
            on_progress(0.5)
            on_progress(1.0)
        return make_swing_frames(n=meta.frame_count, fps=meta.fps, step=1)

    pose_extractor.extract = _fake_extract
    try:
        with open(synth_video, "rb") as handle:
            content = handle.read()
        resp = session_client.post(
            "/api/v1/tasks", files={"file": ("swing.mp4", content, "video/mp4")}
        )
        assert resp.status_code == 201, resp.text
        task_id = resp.json()["data"]["task_id"]

        status = _wait_terminal(session_client, task_id)
        assert status["status"] == TaskStatus.SUCCESS.value, status

        result_resp = session_client.get(f"/api/v1/tasks/{task_id}/result")
        assert result_resp.status_code == 200, result_resp.text
        return task_id, result_resp.json()["data"]
    finally:
        pose_extractor.extract = original


def _metrics_map(metrics):
    """``{key: StageMetric dict}`` 快查表。"""
    return {m["key"]: m for m in metrics}


def _phase(result: dict, key: str) -> dict:
    return next(p for p in result["phases"] if p["key"] == key)


class TestPhaseMetricsEndpoint:
    """``GET /api/v1/task/{id}/phase_metrics/{phase}/{idx}``（+ 旧别名）。"""

    def test_event_frame_matches_original(self, api_client, finished):
        """在事件帧处重算，结果应与原始 8 阶段指标逐项一致（口径正确）。"""
        task_id, result = finished
        for phase in result["phases"]:
            key = phase["key"]
            fi = phase["frame_index"]
            resp = api_client.get(
                f"/api/v1/task/{task_id}/phase_metrics/{key}/{fi}"
            )
            assert resp.status_code == 200, (key, resp.text)
            body = resp.json()
            assert body["code"] == 0
            data = body["data"]
            assert data["phase"] == key
            assert data["frame_index"] == fi

            got = _metrics_map(data["metrics"])
            want = _metrics_map(phase["metrics"])
            assert set(got) == set(want), (key, set(got) ^ set(want))
            for k in want:
                assert got[k]["value"] == want[k]["value"], (key, k)
                assert got[k]["status"] == want[k]["status"], (key, k)

    def test_metrics_change_when_frame_moves(self, api_client, finished):
        """调整帧 ≠ 事件帧时，目标阶段指标值应发生变化。"""
        task_id, result = finished
        phase = _phase(result, "downswing")
        fi = phase["frame_index"]
        target = fi + 3

        resp = api_client.get(
            f"/api/v1/task/{task_id}/phase_metrics/downswing/{target}"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["phase"] == "downswing"
        assert data["frame_index"] == target  # sample_step=1，精确命中

        got = _metrics_map(data["metrics"])
        want = _metrics_map(phase["metrics"])
        changed = [
            k for k in want if k in got and got[k]["value"] != want[k]["value"]
        ]
        assert changed, "调整帧后指标值应发生变化"

    def test_other_phases_unaffected(self, api_client, finished):
        """只重算目标 phase，不改动任务内其他阶段的原始结果。"""
        task_id, result = finished
        phase = _phase(result, "downswing")
        resp = api_client.get(
            f"/api/v1/task/{task_id}/phase_metrics/downswing/{phase['frame_index'] + 2}"
        )
        assert resp.status_code == 200, resp.text

        again = api_client.get(f"/api/v1/tasks/{task_id}/result").json()["data"]
        assert again["phases"] == result["phases"]

    @pytest.mark.parametrize("path_fmt", [
        "/api/v1/task/{}/phase_metrics/{}/{}",
        "/api/v1/tasks/{}/phase_metrics/{}/{}",
    ])
    def test_dual_paths_equivalent(self, api_client, finished, path_fmt):
        task_id, result = finished
        phase = _phase(result, "impact")
        fi = phase["frame_index"]
        old = api_client.get(path_fmt.format(task_id, "impact", fi))
        new = api_client.get(f"/api/v1/task/{task_id}/phase_metrics/impact/{fi}")
        assert old.status_code == new.status_code == 200
        assert old.json() == new.json()

    def test_uppercase_phase_name_accepted(self, api_client, finished):
        """phase 支持枚举名（DOWNSWING）与值（downswing），二者等价。"""
        task_id, result = finished
        fi = _phase(result, "downswing")["frame_index"]
        lower = api_client.get(
            f"/api/v1/task/{task_id}/phase_metrics/downswing/{fi}"
        )
        upper = api_client.get(
            f"/api/v1/task/{task_id}/phase_metrics/DOWNSWING/{fi}"
        )
        assert lower.status_code == upper.status_code == 200
        assert lower.json() == upper.json()

    def test_invalid_phase_returns_20004(self, api_client, finished):
        task_id, result = finished
        fi = result["phases"][0]["frame_index"]
        resp = api_client.get(
            f"/api/v1/task/{task_id}/phase_metrics/bogus/{fi}"
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == config.PDD_CODE_PHASE_INVALID  # 20004
        assert body["data"] is None

    def test_frame_out_of_range_returns_20003(self, api_client, finished):
        task_id, result = finished
        finish_frame = max(p["frame_index"] for p in result["phases"])
        far = finish_frame + config.FRAME_ADJUST_RANGE + 1
        resp = api_client.get(
            f"/api/v1/task/{task_id}/phase_metrics/downswing/{far}"
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == config.PDD_CODE_FRAME_OUT_OF_RANGE  # 20003
        assert body["data"] is None

    def test_unknown_task_returns_20001(self, api_client):
        resp = api_client.get("/api/v1/task/deadbeefcafe/phase_metrics/downswing/10")
        assert resp.status_code == 404
        assert resp.json()["code"] == config.PDD_CODE_TASK_NOT_FOUND

    def test_unfinished_task_returns_20002(self, api_client, probe_bytes):
        """t.mp4 快速失败（BAD_VIDEO）-> 任务非 SUCCESS -> 20002。"""
        resp = api_client.post(
            "/api/v1/tasks", files={"file": ("swing.mp4", probe_bytes, "video/mp4")}
        )
        assert resp.status_code == 201, resp.text
        task_id = resp.json()["data"]["task_id"]
        _wait_terminal(api_client, task_id)
        frame_resp = api_client.get(
            f"/api/v1/task/{task_id}/phase_metrics/downswing/10"
        )
        assert frame_resp.status_code == 409
        assert frame_resp.json()["code"] == config.PDD_CODE_TASK_PENDING
